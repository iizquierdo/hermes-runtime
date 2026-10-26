"""Identidad firmada para las llamadas MCP de Hermes al núcleo.

Hermes manda los mismos headers en todas las llamadas a un servidor MCP, así que no puede decirle al núcleo de qué
chat viene cada una. Este plugin lo resuelve sin pasar por el modelo:

1. `pre_gateway_dispatch`: cuando entra un mensaje, anota quién lo escribió (su `user_id`) y a qué sesión de Hermes
   pertenece.
2. `pre_tool_call`: antes de cada llamada a una tool del servidor MCP del núcleo, agrega el argumento `identidad_firmada`: un
   token firmado con el secreto compartido con el servidor que dice «esta llamada es del chat X». El servidor lo verifica y
   resuelve el usuario como si hubiera llegado el header de identidad. El valor es `<canal>:<id>` (`telegram:2090477055`,
   `whatsapp:+54...`): el directorio de usuarios del núcleo admite varios canales por persona y rechaza el id a secas. Con
   `KIT_IDENTIDAD_CON_CANAL=0` manda el id a secas (que el servidor lee por `canal_por_defecto`).

Falla cerrado: si no se sabe quién escribe, la llamada al núcleo se bloquea; nunca sale sin identidad ni con la de otro.
El modelo no ve ni puede fabricar el token: el hook corre después de que decidió la llamada y el secreto no está en su
contexto.

Configuración (variables de entorno de Hermes; si una `KIT_*` no está, se lee la `IGA_*` del mismo nombre, por compatibilidad):
    MCPKIT_IDENTIDAD_SECRETO    el mismo valor que tiene el servidor (obligatoria; es el nombre que usa el núcleo)
    KIT_MCP_NOMBRES             fragmentos del nombre de tool que identifican al servidor MCP del núcleo, separados por coma
                                (obligatoria; por ejemplo "mcp__nucleo__" si el servidor se llama `nucleo` en config.yaml: las tools
                                se registran como `mcp__<servidor>__<tool>`; guion y guion bajo se tratan igual). Sin ella el plugin
                                no firma nada y el núcleo rechaza las llamadas. No usar un nombre corto que coincida con otras tools
    KIT_IDENTIDAD_HEADER        header que lee el proveedor de identidad (por defecto x-channel-id, el nombre canónico; tiene que ser
                                el mismo `header` de `identidad.config` del despliegue del núcleo)
    KIT_IDENTIDAD_CON_CANAL     por defecto manda `<canal>:<id>`; el canal es `source.platform` del mensaje (verificado: `telegram`).
                                "0" lo apaga y manda el id a secas

Textos (lo que ve el modelo cuando se bloquea una llamada): `textos/nucleo-identidad.json` del paquete, con las claves
`sin_secreto` y `sin_usuario` (ver `cargar_textos` en el plugin nucleo-acceso: mismo formato y misma carpeta).
"""
import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("nucleo-identidad")


def _env(nombre: str, defecto: str = "") -> str:
    """`KIT_<nombre>` y, si no está, `IGA_<nombre>` (los nombres del primer despliegue, por compatibilidad)."""
    return os.environ.get(f"KIT_{nombre}") or os.environ.get(f"IGA_{nombre}") or defecto

NOMBRE = "nucleo-identidad"
PARAMETRO = "identidad_firmada"
HEADER = _env("IDENTIDAD_HEADER", "x-channel-id").lower()
CON_CANAL = _env("IDENTIDAD_CON_CANAL", "1").strip() != "0"
_CANAL_VALIDO = re.compile(r"^[a-z][a-z0-9_-]{1,20}$")
NOMBRES = tuple(n.strip().lower().replace("_", "-") for n in _env("MCP_NOMBRES").split(",") if n.strip())

TEXTOS_POR_DEFECTO = {
    "sin_secreto": "Identidad no configurada: no se puede consultar el servidor. (PT: identidade não configurada.)",
    "sin_usuario": ("No fue posible identificar quién escribe, así que no se consulta el servidor. "
                    "(PT: não foi possível identificar quem escreve.)"),
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


TEXTOS = cargar_textos(NOMBRE, TEXTOS_POR_DEFECTO)

_usuario_actual = contextvars.ContextVar("identidad_usuario", default=None)
_por_sesion: dict[str, str] = {}
_cerrojo = threading.Lock()


def firmar(secreto: str, header: str, valor: str, ahora: float | None = None) -> str:
    """Mismo formato que mcp_kit.nucleo.identidad_firmada.firmar: header|valor|segundos|hmac-sha256."""
    segundos = int(time.time() if ahora is None else ahora)
    firma = hmac.new(secreto.encode(), f"{header}|{valor}|{segundos}".encode(), hashlib.sha256).hexdigest()
    return f"{header}|{valor}|{segundos}|{firma}"


def _es_del_nucleo(tool_name: str) -> bool:
    nombre = (tool_name or "").lower().replace("_", "-")
    return nombre.startswith("mcp") and any(n in nombre for n in NOMBRES)


def _valor_de_identidad(fuente, usuario: str) -> str:
    """El valor que viaja firmado: el id a secas, o `<canal>:<id>` si se pidió y el canal es reconocible."""
    if not CON_CANAL:
        return usuario
    plataforma = getattr(fuente, "platform", None)
    canal = str(getattr(plataforma, "value", plataforma) or "").strip().lower()
    return f"{canal}:{usuario}" if _CANAL_VALIDO.match(canal) else usuario


def al_despachar(*args, **kwargs):
    """Un mensaje entra al gateway: anotar quién es y a qué sesión de Hermes va."""
    evento = kwargs.get("event")
    fuente = getattr(evento, "source", None)
    usuario = getattr(fuente, "user_id", None)
    if not usuario:
        _usuario_actual.set(None)
        return None
    usuario = _valor_de_identidad(fuente, str(usuario))
    _usuario_actual.set(usuario)
    almacen = kwargs.get("session_store")
    obtener = getattr(almacen, "get_or_create_session", None)
    if callable(obtener):
        try:
            entrada = obtener(fuente)
            sesion = getattr(entrada, "session_id", None)
            if sesion:
                with _cerrojo:
                    _por_sesion[str(sesion)] = usuario
        except Exception:                                   # el contextvar sigue sirviendo como respaldo
            log.exception("nucleo-identidad: no se pudo asociar la sesión con el usuario")
    return None


def antes_de_la_tool(*args, **kwargs):
    """Una tool está por ejecutarse: si es del núcleo, agregarle la identidad firmada o bloquearla."""
    if not _es_del_nucleo(kwargs.get("tool_name", "")):
        return None
    secreto = os.environ.get("MCPKIT_IDENTIDAD_SECRETO")
    if not secreto:
        log.error("nucleo-identidad: falta MCPKIT_IDENTIDAD_SECRETO; se bloquea la llamada")
        return {"action": "block", "message": TEXTOS["sin_secreto"]}
    with _cerrojo:
        usuario = _por_sesion.get(str(kwargs.get("session_id"))) or _usuario_actual.get()
    if not usuario:
        log.error("nucleo-identidad: llamada a %s sin usuario conocido (session_id=%r); se bloquea",
                  kwargs.get("tool_name"), kwargs.get("session_id"))
        return {"action": "block", "message": TEXTOS["sin_usuario"]}
    return {"action": "modify", "args": {PARAMETRO: firmar(secreto, HEADER, usuario)}}


def register(ctx):
    if not NOMBRES:
        log.error("nucleo-identidad: KIT_MCP_NOMBRES no está definida; no se firma ninguna llamada y el núcleo las rechazará")
    ctx.register_hook("pre_gateway_dispatch", al_despachar)
    ctx.register_hook("pre_tool_call", antes_de_la_tool)
