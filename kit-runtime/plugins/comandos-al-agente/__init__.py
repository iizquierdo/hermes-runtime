"""Comandos con barra que tiene que atender el agente (por ejemplo `/newUser` para el alta), no Hermes.

Verificado en el código de Hermes 0.22.2 (`gateway/run_inbound.py`): un `/comando` que no es de Hermes, ni de un plugin, ni el
nombre de una skill **no llega al modelo**: `_hm_unknown_slash_reply` contesta "comando desconocido" (a propósito, para que el
modelo no invente llamadas). Eso pasa con o sin `allow_admin_from`: el control de comandos solo mira los que Hermes conoce.

Este plugin, en `pre_gateway_dispatch` (antes de la autorización y del despacho de comandos), reescribe los comandos que el paquete
declara como texto común: `/newUser Ana` pasa a `Comando del usuario: /newUser Ana`. Ya no empieza con barra, así que sigue el camino
de cualquier mensaje: `nucleo-acceso` lo filtra igual y el modelo lo ve con la barra, que es lo que buscan las skills.

Nunca reescribe un comando que Hermes conoce (`/new`, `/pause`…): esos siguen bajo el control de comandos.

Configuración:
    textos/comandos-al-agente.json del paquete: {"comandos": "newUser,otro", "prefijo": "Comando del usuario:"}
    KIT_COMANDOS_AL_AGENTE   más comandos, separados por coma (si no está, IGA_COMANDOS_AL_AGENTE)
Sin comandos configurados no hace nada. Se comparan sin distinguir mayúsculas (Hermes los pasa a minúsculas).
"""
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("comandos-al-agente")

NOMBRE = "comandos-al-agente"
TEXTOS_POR_DEFECTO = {"comandos": "", "prefijo": "Comando del usuario:"}
_COMANDO = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?:@\S+)?(?:\s+(.*))?$", re.DOTALL)


def _env(nombre: str, defecto: str = "") -> str:
    """`KIT_<nombre>` y, si no está, `IGA_<nombre>` (los nombres del primer despliegue, por compatibilidad)."""
    return os.environ.get(f"KIT_{nombre}") or os.environ.get(f"IGA_{nombre}") or defecto


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
PREFIJO = _TEXTOS["prefijo"].strip()
COMANDOS = frozenset(c.strip().lstrip("/").lower() for c in f"{_TEXTOS['comandos']},{_env('COMANDOS_AL_AGENTE')}".split(",")
                     if c.strip())


def es_de_hermes(nombre: str) -> bool:
    """¿Es un comando que Hermes atiende? Si no se puede saber, se supone que sí (no se reescribe)."""
    try:
        from hermes_cli.commands import is_gateway_known_command
    except Exception:
        return True
    return bool(is_gateway_known_command(nombre) or is_gateway_known_command(nombre.replace("_", "-")))


def al_despachar(*args, **kwargs):
    evento = kwargs.get("event")
    texto = getattr(evento, "text", None)
    if not COMANDOS or not isinstance(texto, str):
        return None
    coincide = _COMANDO.match(texto.strip())
    if not coincide or coincide.group(1).lower() not in COMANDOS:
        return None
    nombre, argumentos = coincide.group(1), (coincide.group(2) or "").strip()
    if es_de_hermes(nombre.lower()):
        log.warning("comandos-al-agente: /%s es un comando de Hermes; no se reescribe", nombre.lower())
        return None
    return {"action": "rewrite", "text": " ".join(x for x in (PREFIJO, f"/{nombre}", argumentos) if x)}


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", al_despachar)
