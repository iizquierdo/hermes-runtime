"""Filtro de acceso para Hermes: decide el núcleo, sin gastar tokens.

Hermes deniega por defecto a quien no está en una allowlist, y así una persona que todavía no está en el directorio de
usuarios del núcleo no podría ni hacer el alta por chat. Se abre el acceso de Hermes (`TELEGRAM_ALLOW_ALL_USERS=true`; solo es
viable porque el perfil del bot no tiene terminal, archivos, web ni ejecución de código) y se filtra acá, antes del LLM, según el
estado del chat en el directorio del núcleo (`GET /identity/status`). El núcleo es la única autoridad de acceso.

Hook `post_gateway_admission` (kwargs: session_key, platform, source, message_id, text). Devolver
`{"action": "handled", "reply": "..."}` corta el turno sin LLM; cualquier otra cosa deja correr el turno normal.

Por mensaje:
    chat que no es privado (grupo/canal)  handled silencioso, sin reply: el bot es solo por mensaje privado (DM)
    no se sabe quién escribe              handled con el texto `no_verificable`
    active                                pasa
    unregistered                          pasa (el agente hace el alta con la skill del cliente), con un límite de mensajes por
                                          persona y por hora; al pasarlo, handled con el texto `limite`
    pending                               handled con el texto `pendiente`
    suspended / rejected                  handled con el texto `no_habilitado`
    cualquier falla (core caído, timeout, 401, 404, 400, JSON roto, estado desconocido, sin configurar)
                                          handled con `no_verificable`: FALLA CERRADO, nunca deja pasar

Cache en memoria de `active` y `pending` (KIT_ACCESO_CACHE_SEGUNDOS, 30 por defecto). Nunca se cachean errores ni
`unregistered` / `suspended` / `rejected` (para que el alta y la aprobación se noten enseguida).

El chat privado se reconoce por `source.chat_type` ("dm" o "private"); si el campo no viene, por `source.chat_id == source.user_id`
(en Telegram un DM tiene el mismo id). Si nada alcanza para afirmar que es privado, se trata como no privado. Verificado con un
Hermes real: `source` llega como diccionario, `chat_type` vale `dm` y `platform` vale `telegram`.

Configuración (variables de entorno de Hermes; si una `KIT_*` no está, se lee la `IGA_*` del mismo nombre, por compatibilidad):
    KIT_CORE_URL                          URL base del núcleo (obligatoria), por ejemplo https://core.interno
    KIT_CORE_TOKEN                        token del runtime ante el núcleo (obligatoria); nunca se escribe en el log
    KIT_ACCESO_MAX_MENSAJES_NO_REGISTRADO mensajes por hora y por persona sin registrar (30 por defecto, ventana deslizante)
    KIT_ACCESO_TIMEOUT_SEGUNDOS           timeout de la consulta al núcleo (2.5 por defecto)
    KIT_ACCESO_CACHE_SEGUNDOS             vigencia de la cache de active/pending (30 por defecto; 0 la apaga)

Textos: los de abajo son los neutros por defecto. El paquete del cliente los reemplaza con `textos/nucleo-acceso.json`
(se instala en `$HERMES_HOME/textos/`; otra carpeta con KIT_TEXTOS_DIR), un objeto con las claves `no_verificable`,
`pendiente`, `no_habilitado` y `limite`. Una clave que falta o no es texto conserva el valor por defecto.
"""
import collections
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("nucleo-acceso")

NOMBRE = "nucleo-acceso"
_CANAL_VALIDO = re.compile(r"^[a-z][a-z0-9_-]{1,20}$")
_VENTANA_SEGUNDOS = 3600
_CACHEABLES = ("active", "pending")
_ESTADOS = ("unregistered", "pending", "active", "suspended", "rejected")

TEXTOS_POR_DEFECTO = {
    "no_verificable": ("No es posible verificar tu acceso en este momento. Intenta de nuevo en unos minutos. "
                       "(PT: não é possível verificar o seu acesso agora; tente novamente em alguns minutos.)"),
    # No promete un aviso: nada le escribe a la persona cuando un administrador aprueba el alta.
    "pendiente": ("Tu solicitud está pendiente de aprobación. Cuando sea aprobada, vuelve a escribir y continuamos. "
                  "(PT: a sua solicitação está pendente de aprovação; quando for aprovada, escreva novamente.)"),
    "no_habilitado": ("Tu acceso no está habilitado. Comunícate con un administrador para resolverlo. "
                      "(PT: o seu acesso não está habilitado; fale com um administrador.)"),
    "limite": ("Enviaste muchos mensajes sin completar el registro. Intenta de nuevo más tarde o comunícate con un administrador. "
               "(PT: muitas mensagens sem concluir o cadastro; tente mais tarde ou fale com um administrador.)"),
}


def cargar_textos(nombre: str, defectos: dict[str, str]) -> dict[str, str]:
    """Los textos del plugin: los por defecto, pisados por `<carpeta de textos>/<nombre>.json` si existe y es válido."""
    carpeta = os.environ.get("KIT_TEXTOS_DIR") or str(Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "textos")
    ruta = Path(carpeta) / f"{nombre}.json"
    textos = dict(defectos)
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return textos
    except (OSError, ValueError) as e:
        log.error("%s: no se pudo leer %s (%s); se usan los textos por defecto", nombre, ruta, type(e).__name__)
        return textos
    if not isinstance(datos, dict):
        log.error("%s: %s no es un objeto; se usan los textos por defecto", nombre, ruta)
        return textos
    for clave, valor in datos.items():
        if clave in textos and isinstance(valor, str) and valor.strip():
            textos[clave] = valor
        else:
            log.warning("%s: se ignora la clave %r de %s", nombre, clave, ruta)
    return textos


_TEXTOS = cargar_textos(NOMBRE, TEXTOS_POR_DEFECTO)
TEXTO_NO_VERIFICABLE = _TEXTOS["no_verificable"]
TEXTO_PENDIENTE = _TEXTOS["pendiente"]
TEXTO_NO_HABILITADO = _TEXTOS["no_habilitado"]
TEXTO_LIMITE = _TEXTOS["limite"]

_cerrojo = threading.Lock()
_cache: dict[tuple[str, str], tuple[str, float]] = {}
_mensajes: dict[tuple[str, str], collections.deque] = {}
_ahora = time.monotonic                                    # inyectable en los tests


def _env(nombre: str, defecto: str = "") -> str:
    """`KIT_<nombre>` y, si no está, `IGA_<nombre>` (los nombres del primer despliegue, por compatibilidad)."""
    return os.environ.get(f"KIT_{nombre}") or os.environ.get(f"IGA_{nombre}") or defecto


def _numero(nombre: str, defecto: float, minimo: float = 0) -> float:
    try:
        return max(minimo, float(_env(nombre) or defecto))
    except ValueError:
        return defecto


def _respuesta(texto: str | None) -> dict:
    return {"action": "handled", "reply": texto} if texto else {"action": "handled"}


def _campo(fuente, nombre):
    """`source` llega como diccionario en el Hermes real; en los tests, también como objeto."""
    return fuente.get(nombre) if isinstance(fuente, dict) else getattr(fuente, nombre, None)


def _canal(fuente, plataforma) -> str:
    p = _campo(fuente, "platform") or plataforma
    canal = str(getattr(p, "value", p) or "").strip().lower()
    return canal if _CANAL_VALIDO.match(canal) else ""


def _es_privado(fuente) -> bool:
    tipo = _campo(fuente, "chat_type")
    tipo = str(getattr(tipo, "value", tipo) or "").strip().lower()
    if tipo:
        return tipo in ("dm", "private")
    chat, usuario = _campo(fuente, "chat_id"), _campo(fuente, "user_id")
    return chat is not None and usuario is not None and str(chat) == str(usuario)


def consultar_estado(canal: str, externo: str) -> str:
    """Pregunta al núcleo. Devuelve uno de _ESTADOS o levanta una excepción ante cualquier problema."""
    base, token = _env("CORE_URL").rstrip("/"), _env("CORE_TOKEN")
    if not base or not token:
        raise RuntimeError("falta KIT_CORE_URL o KIT_CORE_TOKEN")
    if not base.lower().startswith(("http://", "https://")):
        raise RuntimeError("KIT_CORE_URL no es http(s)")
    url = f"{base}/identity/status?" + urllib.parse.urlencode({"channel": canal, "id": externo})
    pedido = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(pedido, timeout=_numero("ACCESO_TIMEOUT_SEGUNDOS", 2.5, 0.1)) as r:   # HTTPError si no es 2xx
        cuerpo = json.loads(r.read(4096).decode("utf-8"))
    estado = cuerpo.get("status") if isinstance(cuerpo, dict) else None
    if estado not in _ESTADOS:
        raise ValueError(f"estado desconocido: {estado!r}")
    return estado


def _estado(clave: tuple[str, str]) -> str:
    vigencia = _numero("ACCESO_CACHE_SEGUNDOS", 30)
    with _cerrojo:
        guardado = _cache.get(clave)
        if guardado and guardado[1] > _ahora():
            return guardado[0]
        _cache.pop(clave, None)
    estado = consultar_estado(*clave)
    if estado in _CACHEABLES and vigencia > 0:
        with _cerrojo:
            _cache[clave] = (estado, _ahora() + vigencia)
    return estado


def _excede_limite(clave: tuple[str, str]) -> bool:
    maximo = int(_numero("ACCESO_MAX_MENSAJES_NO_REGISTRADO", 30))
    ahora = _ahora()
    with _cerrojo:
        cola = _mensajes.setdefault(clave, collections.deque())
        while cola and cola[0] <= ahora - _VENTANA_SEGUNDOS:
            cola.popleft()
        if len(cola) >= maximo:
            return True
        cola.append(ahora)
        return False


def al_admitir(*args, **kwargs):
    """Un mensaje fue admitido por el gateway: decidir, sin LLM, si el turno corre o se corta con un texto fijo."""
    fuente = kwargs.get("source")
    if fuente is None or not _es_privado(fuente):
        return _respuesta(None)                                     # grupos y canales: el bot es solo por DM
    canal, usuario = _canal(fuente, kwargs.get("platform")), _campo(fuente, "user_id")
    if not canal or usuario in (None, ""):
        log.error("nucleo-acceso: no se pudo determinar quién escribe; se corta el turno")
        return _respuesta(TEXTO_NO_VERIFICABLE)
    clave = (canal, str(usuario).strip())
    try:
        estado = _estado(clave)
    except Exception as e:                                          # falla cerrado, sin datos de la persona en el log
        log.error("nucleo-acceso: no se pudo verificar el acceso (%s); se corta el turno", type(e).__name__)
        return _respuesta(TEXTO_NO_VERIFICABLE)
    if estado == "active":
        return None
    if estado == "unregistered":
        return _respuesta(TEXTO_LIMITE) if _excede_limite(clave) else None
    if estado == "pending":
        return _respuesta(TEXTO_PENDIENTE)
    return _respuesta(TEXTO_NO_HABILITADO)                          # suspended / rejected


def register(ctx):
    ctx.register_hook("post_gateway_admission", al_admitir)
