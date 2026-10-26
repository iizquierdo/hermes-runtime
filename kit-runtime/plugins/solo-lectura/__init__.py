"""Solo lectura para canales de mensajería (Telegram y el resto).

Regla del operador: quien escribe por un canal de mensajería es usuario de solo lectura. Puede usar las tools, los
plugins y los servidores MCP, pero no puede cambiar nada del agente ni del sistema: ni la configuración, ni archivos,
ni skills, ni memoria, ni cron, ni servidores MCP. El administrador es quien escribe por la TUI, el CLI o el
escritorio (sesiones locales).

Cómo funciona: hook `pre_tool_call` (kwargs: tool_name, args, session_id, task_id, tool_call_id, ...). La plataforma
de la sesión se lee de la columna `source` de `state.db`, que el runtime escribe para toda sesión, incluidas las
locales (`tui`, `desktop`, `cli`). Si la sesión viene de un canal de mensajería y la tool es de las que mutan, se
devuelve `{"action": "block", "message": ...}`: el modelo ve ese texto como resultado de la tool y el cambio no ocurre.

Falla abierto SOLO cuando no se puede saber la plataforma (sin session_id, fila ausente, `source` vacío, error de
sqlite): deja pasar y lo anota en el log. La contención de superficie ya la hace `platform_toolsets` en config.yaml;
este plugin es la segunda capa y no debe poder dejar sin trabajo a un administrador por un problema de lectura.

Configuración (variables de entorno de Hermes; los valores por defecto están en el código, no en .env):
    HERMES_SOLO_LECTURA=0                  apaga el plugin (por defecto está encendido)
    HERMES_SOLO_LECTURA_CANALES            canales de solo lectura, separados por coma
                                           (por defecto: telegram, discord, whatsapp, whatsapp_cloud, slack, signal,
                                           matrix, mattermost, email, sms, dingtalk, feishu, wecom, wecom_callback,
                                           weixin, bluebubbles, qqbot, yuanbao, teams, google_chat, relay, webhook)
    HERMES_SOLO_LECTURA_DB                 ruta a state.db (por defecto $HERMES_HOME/state.db)
    HERMES_SOLO_LECTURA_TRAZA=1            deja una línea por decisión en logs/solo-lectura.log

Texto: el que ve el modelo cuando se bloquea una tool. El paquete del cliente lo reemplaza con `textos/solo-lectura.json`
(clave `bloqueo`; misma carpeta y formato que los demás plugins del runtime).
"""
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("solo-lectura")

CANALES_POR_DEFECTO = (
    "telegram", "discord", "whatsapp", "whatsapp_cloud", "slack", "signal", "matrix", "mattermost",
    "email", "sms", "dingtalk", "feishu", "wecom", "wecom_callback", "weixin", "bluebubbles",
    "qqbot", "yuanbao", "teams", "google_chat", "relay", "webhook",
)

# Tools que cambian el agente o el sistema. Los MCP (mcp__*) y las tools de plugins no están acá: son el trabajo
# legítimo de la red y no tocan la configuración de Hermes.
BLOQUEADAS = frozenset({
    # sistema / ejecución
    "terminal", "process_manage", "execute_code", "computer_use", "batch_process",
    # archivos (leer también: .env y secretos viven en el home del perfil)
    "read_file", "write_file", "patch", "search_files",
    # agente: skills, memoria, cron, delegación, conectores
    "skill_manage", "skill_hub", "skills_hub", "memory", "cronjob_manage", "delegate_task",
    "manage_connections", "manage_catalog", "session_search",
    # superficie de escritorio / TUI
    "read_terminal", "close_terminal", "desktop_project", "set_theme", "focus_pane",
    "drive_preview", "annotate_preview", "gui_tour", "show_tip", "setup_beat",
})
PREFIJOS_BLOQUEADOS = ("browser_", "kanban_", "video_", "xai_video_")
# Envoltorios de tools diferidas: hay que mirar adentro qué tool piden.
ENVOLTORIOS = frozenset({"tool_call", "tool_describe"})

TEXTOS_POR_DEFECTO = {
    "bloqueo": (
        "Bloqueado: por un canal de mensajería trabajo en solo lectura. Puedo consultar (tools, plugins y MCP), "
        "pero no cambio la configuración del agente, ni archivos, ni skills, ni memoria, ni cron, ni servidores MCP. "
        "Si hace falta un cambio, lo tiene que hacer un administrador desde la consola de administración. "
        "(PT: bloqueado — em canais de mensagem trabalho somente em leitura; mudanças ficam com um administrador.)"
    ),
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


TEXTO_BLOQUEO = cargar_textos("solo-lectura", TEXTOS_POR_DEFECTO)["bloqueo"]

_ACTIVO = os.environ.get("HERMES_SOLO_LECTURA", "1") != "0"
_CANALES = frozenset(
    c.strip().lower()
    for c in os.environ.get("HERMES_SOLO_LECTURA_CANALES", ",".join(CANALES_POR_DEFECTO)).split(",")
    if c.strip()
)
_TRAZA_ACTIVA = os.environ.get("HERMES_SOLO_LECTURA_TRAZA", "0") == "1"
_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
_DB = Path(os.environ.get("HERMES_SOLO_LECTURA_DB") or (_HOME / "state.db"))
_ARCHIVO_TRAZA = _HOME / "logs" / "solo-lectura.log"

_cerrojo = threading.Lock()
_por_sesion: dict[str, str] = {}
_MAX_CACHE = 512


def traza(texto: str) -> None:
    """Deja una línea en logs/solo-lectura.log cuando HERMES_SOLO_LECTURA_TRAZA=1 (nunca lanza)."""
    if not _TRAZA_ACTIVA:
        return
    try:
        _ARCHIVO_TRAZA.parent.mkdir(parents=True, exist_ok=True)
        with open(_ARCHIVO_TRAZA, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {texto}\n")
    except Exception:
        pass


def plataforma_de_la_sesion(sesion: str) -> str:
    """`source` de la sesión en state.db (telegram, tui, desktop, cli, ...) o "" si no se puede saber."""
    if not sesion:
        return ""
    with _cerrojo:
        if sesion in _por_sesion:
            return _por_sesion[sesion]
    try:
        con = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True, timeout=1.0)
        try:
            fila = con.execute("SELECT source FROM sessions WHERE id = ?", (sesion,)).fetchone()
        finally:
            con.close()
        platforma = str(fila[0] or "").strip().lower() if fila else ""
    except Exception as e:                                     # falla abierto, pero queda en el log
        log.warning("solo-lectura: no se pudo leer la plataforma de la sesión (%s)", type(e).__name__)
        return ""
    if platforma:
        with _cerrojo:
            if len(_por_sesion) >= _MAX_CACHE:
                _por_sesion.clear()
            _por_sesion[sesion] = platforma
    return platforma


def es_mutadora(nombre: str) -> bool:
    """¿La tool cambia el agente o el sistema? Los servidores MCP (mcp__…) nunca se bloquean."""
    n = (nombre or "").strip().lower()
    if not n or n.startswith("mcp__"):
        return False
    return n in BLOQUEADAS or n.startswith(PREFIJOS_BLOQUEADOS)


def bloqueada_por_envoltorio(args) -> str:
    """Si la llamada es un `tool_call` diferido que apunta a una tool mutadora, devuelve ese nombre."""
    if not isinstance(args, dict):
        return ""
    llamadas = args.get("calls")
    if not isinstance(llamadas, list):
        return ""
    for llamada in llamadas:
        if isinstance(llamada, dict) and es_mutadora(str(llamada.get("name") or "")):
            return str(llamada.get("name"))
    return ""


def antes_de_la_tool(*args, **kwargs):
    """Una tool está por ejecutarse: si la sesión es de un canal, bloquear las que cambian algo."""
    if not _ACTIVO:
        return None
    nombre = str(kwargs.get("tool_name") or "")
    if es_mutadora(nombre):
        mutadora = nombre
    elif nombre.strip().lower() in ENVOLTORIOS:
        mutadora = bloqueada_por_envoltorio(kwargs.get("args"))
    else:
        return None
    if not mutadora:
        return None
    sesion = str(kwargs.get("session_id") or "")
    plataforma = plataforma_de_la_sesion(sesion)
    if not plataforma:
        log.warning("solo-lectura: sesión sin plataforma conocida; se deja pasar %s (revisar state.db)", mutadora)
        traza(f"tool={mutadora} sesión='{sesion}' plataforma=? → se deja pasar (sin datos)")
        return None
    if plataforma not in _CANALES:
        return None
    log.warning("solo-lectura: bloqueada %s en sesión de %s", mutadora, plataforma)
    traza(f"tool={mutadora} sesión='{sesion}' plataforma={plataforma} → BLOQUEADA")
    return {"action": "block", "message": TEXTO_BLOQUEO}


def register(ctx):
    traza(f"register: activo={_ACTIVO} canales={sorted(_CANALES)} db={_DB}")
    ctx.register_hook("pre_tool_call", antes_de_la_tool)
