#!/usr/bin/env python3
"""Aplica el paquete del agente a un Hermes: SOUL.md, skills, plugins, textos y configuración.

La imagen es la que sabe aplicar; el paquete solo trae datos del cliente y tiene que estar firmado (ver `paquete.py`). El
volumen de Hermes (HERMES_HOME) solo guarda estado: este script lo deja como el paquete dice, y correrlo dos veces da el mismo
resultado.

    provision.py check  --source agent-pack.zip    # no toca nada; sale con 1 si hay algo que corregir
    provision.py apply  --source agent-pack.zip    # verifica la firma, copia lo que difiere (con respaldo) y quita lo obsoleto
    provision.py bundle --decl <cliente>/agent-pack.toml --version X.Y.Z --out agent-pack.zip --clave-privada <pem>
    provision.py keygen --privada <fuera del repo>/agent-pack.pem --publica kit-runtime/keys/<nombre>.pub

`apply` y `check` rechazan, sin escribir nada, un paquete sin firma válida de una clave de `kit-runtime/keys/`, con un archivo
cambiado, de más o de menos, o que no declare compatible la versión de este runtime (`kit-runtime/VERSION`).

Además de lo que trae el paquete, `apply` instala los plugins del runtime (`kit-runtime/plugins/`), los habilita en
`plugins.enabled`, apaga la memoria del bot, las URLs privadas y las instalaciones por demanda, deja en los canales de mensajería
solo los toolsets `skills`, `clarify` y `todo`, corta las sesiones tras 120 minutos sin actividad (salvo que el paquete diga otra
cosa) y fija el control de comandos de Telegram si se le pasan administradores. `--home`
por defecto es $HERMES_HOME o /data/.hermes. Solo biblioteca estándar, más `cryptography` para la firma.

No gestiona: `.env`, la memoria del agente (solo avisa) ni los plugins que no conoce: los lista como aviso y no los toca.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hermes_config  # noqa: E402
import paquete  # noqa: E402
from paquete import _archivos, _bytes  # noqa: E402

RAIZ_OVERLAY = Path(__file__).resolve().parents[1]
CARPETA_CLAVES = RAIZ_OVERLAY / "keys"
CARPETA_PLUGINS_RUNTIME = RAIZ_OVERLAY / "plugins"
ARCHIVO_VERSION = RAIZ_OVERLAY / "VERSION"
ESCRITOR_CONFIG = Path(__file__).resolve().parent / "hermes_config.py"

# Qué versión del paquete quedó aplicada, con qué clave y qué plugins instaló (para quitarlos si una versión nueva ya no los trae).
REGISTRO = ".agent-pack.json"

# Dónde cuelga cada cosa dentro de HERMES_HOME. Las skills del paquete viven en `skills/<nombre del paquete>/`: lo que haya ahí y
# no esté en el paquete se considera sobrante y se quita. Los textos de los plugins, en `textos/`.
DESTINO_SOUL = "SOUL.md"
DESTINO_SKILLS = "skills"
DESTINO_PLUGINS = "plugins"
DESTINO_TEXTOS = "textos"

TOOLSETS_PERMITIDOS = paquete.TOOLSETS_PERMITIDOS

# Claves de config.yaml que fija el runtime (el paquete no las puede traer). La memoria del agente se inyecta en el prompt de todos
# los usuarios y es global del perfil (no hay clave por plataforma): apagada en el bot. El perfil `admin` (TUI) sí la tiene
# encendida: ahí el agente puede recordar cómo está montado el despliegue, y como es otro perfil su memoria no llega al prompt de
# los usuarios del bot. En los dos: sin URLs privadas (la red interna del despliegue) y sin instalar dependencias por demanda.
SEGURIDAD = {"security.allow_private_urls": False, "security.allow_lazy_installs": False}
CONFIG_POR_PERFIL = {
    "bot": {"memory.memory_enabled": False, "memory.user_profile_enabled": False, **SEGURIDAD},
    "admin": {"memory.memory_enabled": True, "memory.user_profile_enabled": True, **SEGURIDAD},
}

# Lo que el bot tiene si el paquete no dice otra cosa. Los toolsets por defecto valen para Telegram y para todo canal de mensajería
# que ya figure en `platform_toolsets`; el paquete solo puede achicarlos (ver paquete.TOOLSETS_PERMITIDOS).
TOOLSETS_POR_DEFECTO = list(TOOLSETS_PERMITIDOS)
CONFIG_POR_DEFECTO = {
    "platform_toolsets": {"telegram": TOOLSETS_POR_DEFECTO},
    "session_reset": {"mode": "idle", "idle_minutes": 120},
}

# El panel web de Hermes (servicio s6 `dashboard`) arranca solo si esta variable es verdadera: tiene que quedar apagado.
PANEL_WEB = "HERMES_DASHBOARD"

PERFIL_ADMIN = "admin"

# El adaptador de Telegram convierte esta variable en `allow_from` (plugins/platforms/telegram/adapter.py), y `allow_from` es la única
# autoridad si existe: anula `TELEGRAM_ALLOW_ALL_USERS`. Con alta por chat no puede haber una lista cerrada.
LISTA_CERRADA = "TELEGRAM_ALLOWED_USERS"

# Comandos con barra que puede usar quien NO es administrador (/help y /whoami siempre se permiten). Todo lo demás
# (/pause, /stop, /sethome, /yolo, /restart, /update, /config…) queda para los administradores. Sin `allow_admin_from`
# Hermes no restringe nada, así que un bot sin administradores configurados es un bot abierto.
COMANDOS_USUARIO = ["new", "retry", "undo"]

OK, DIFIERE, FALTA = "ok", "difiere", "falta"


@dataclass(frozen=True)
class Item:
    tipo: str  # soul | skill | plugin | textos
    nombre: str
    origen: Path
    destino: Path


@dataclass(frozen=True)
class Origen:
    """El paquete ya verificado y extraído, más los plugins del runtime."""
    raiz: Path
    manifiesto: dict

    @property
    def plugins_runtime(self) -> Path:
        return CARPETA_PLUGINS_RUNTIME

    @property
    def nombre(self) -> str:
        return self.manifiesto["nombre"]

    @property
    def retirar(self) -> frozenset[str]:
        return frozenset((self.manifiesto.get("retirar") or {}).get("plugins") or [])

    def config(self) -> dict:
        ruta = self.raiz / paquete.CONFIG
        return json.loads(ruta.read_text(encoding="utf-8")) if ruta.is_file() else {}


def huella(ruta: Path) -> str:
    h = sha256()
    if ruta.is_file():
        h.update(_bytes(ruta))
        return h.hexdigest()
    for archivo in _archivos(ruta):
        h.update(archivo.relative_to(ruta).as_posix().encode())
        h.update(b"\0")
        h.update(_bytes(archivo))
        h.update(b"\0")
    return h.hexdigest()


def copiar(origen: Path, destino: Path) -> None:
    if origen.is_file():
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(_bytes(origen))
        return
    if destino.exists():
        shutil.rmtree(destino)
    for archivo in _archivos(origen):
        salida = destino / archivo.relative_to(origen)
        salida.parent.mkdir(parents=True, exist_ok=True)
        salida.write_bytes(_bytes(archivo))


def home_del_perfil(base: Path, perfil: str) -> Path:
    """El bot corre con el perfil default (HERMES_HOME); el de administración vive en profiles/admin."""
    return base if perfil == "bot" else base / "profiles" / PERFIL_ADMIN


def plugins_de(carpeta: Path) -> dict[str, Path]:
    return {m.parent.name: m.parent for m in sorted(carpeta.glob("*/plugin.yaml"))} if carpeta.is_dir() else {}


def plan(origen: Origen, home: Path, perfil: str = "bot") -> list[Item]:
    if perfil == "admin":
        soul_admin = origen.raiz / paquete.SOUL_ADMIN
        if not soul_admin.is_file():
            raise SystemExit(f"El paquete no trae {paquete.SOUL_ADMIN}: no se puede aprovisionar el perfil {PERFIL_ADMIN}")
        return [Item("soul", "SOUL.md (admin)", soul_admin, home / DESTINO_SOUL)]
    items = [Item("soul", "SOUL.md", origen.raiz / paquete.SOUL, home / DESTINO_SOUL)]
    for skill in sorted((origen.raiz / paquete.CARPETA_SKILLS).glob("*/SKILL.md")):
        nombre = skill.parent.name
        items.append(Item("skill", nombre, skill.parent, home / DESTINO_SKILLS / origen.nombre / nombre))
    del_runtime = plugins_de(origen.plugins_runtime)
    del_paquete = plugins_de(origen.raiz / paquete.CARPETA_PLUGINS)
    choque = sorted(set(del_runtime) & set(del_paquete))
    if choque:
        raise SystemExit(f"El paquete trae plugins con el nombre de uno del runtime ({', '.join(choque)}); no se aplicó nada")
    for nombre, carpeta in sorted({**del_runtime, **del_paquete}.items()):
        items.append(Item("plugin", nombre, carpeta, home / DESTINO_PLUGINS / nombre))
    textos = origen.raiz / paquete.CARPETA_TEXTOS
    if textos.is_dir():
        items.append(Item("textos", "textos", textos, home / DESTINO_TEXTOS))
    return items


def estado(item: Item) -> str:
    if not item.destino.exists():
        return FALTA
    return OK if huella(item.origen) == huella(item.destino) else DIFIERE


def plugins_del_bot(origen: Origen) -> frozenset[str]:
    return frozenset(i.nombre for i in plan(origen, Path("/nada"), "bot") if i.tipo == "plugin")


def sobrantes(items: list[Item], home: Path, origen: Origen, perfil: str = "bot") -> tuple[list[Path], list[Path], list[str]]:
    """(skills y textos sobrantes, plugins a quitar, plugins no gestionados).

    Se quitan los plugins que el paquete pide retirar y los que instaló una versión anterior y esta ya no trae. En el perfil
    `admin` no debe haber nada del bot: ni sus skills, ni sus textos, ni sus plugins (clonar el perfil los copia).
    """
    admin = perfil == "admin"
    skills_paquete = {i.nombre for i in items if i.tipo == "skill"}
    plugins_paquete = {i.nombre for i in items if i.tipo == "plugin"}

    carpeta_skills = home / DESTINO_SKILLS / origen.nombre
    quitar = (
        [p for p in sorted(carpeta_skills.iterdir()) if p.is_dir() and p.name not in skills_paquete]
        if carpeta_skills.is_dir()
        else []
    )
    textos = home / DESTINO_TEXTOS
    if textos.is_dir() and not any(i.tipo == "textos" for i in items):
        quitar.append(textos)

    anterior = leer_registro(home) or {}
    instalados_antes = {n for n in anterior.get("plugins", []) if isinstance(n, str)}
    carpeta_plugins = home / DESTINO_PLUGINS
    presentes = [p for p in sorted(carpeta_plugins.iterdir()) if p.is_dir()] if carpeta_plugins.is_dir() else []
    a_quitar = set(origen.retirar) | (instalados_antes - plugins_paquete) | (set(plugins_del_bot(origen)) if admin else set())
    obsoletos = [p for p in presentes if p.name in a_quitar]
    ajenos = [p.name for p in presentes if p.name not in plugins_paquete and p.name not in a_quitar]
    return quitar, obsoletos, ajenos


def python_de_hermes() -> str:
    """El Python con el que corre Hermes. La imagen oficial lo deja en /opt/hermes/.venv; la plantilla de Railway, en /opt/hermes/venv."""
    for candidato in (os.environ.get("HERMES_PYTHON", ""), "/opt/hermes/.venv/bin/python", "/opt/hermes/venv/bin/python"):
        if candidato and Path(candidato).is_file():
            return candidato
    return sys.executable


def repo_de_hermes() -> Path | None:
    """La carpeta con el código de Hermes (`hermes_cli/`): la raíz de /opt/hermes en la imagen oficial, /opt/hermes/repo en la plantilla."""
    for candidato in (os.environ.get("HERMES_REPO", ""), "/opt/hermes", "/opt/hermes/repo"):
        if candidato and (Path(candidato) / "hermes_cli").is_dir():
            return Path(candidato)
    return None


def _config_de_hermes(home: Path, cruda: bool) -> dict | None:
    repo = repo_de_hermes()
    if repo is None:
        return None
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(repo))
    try:
        from hermes_cli import config as hermes  # type: ignore[import-not-found]

        return (hermes.read_user_config_raw() if cruda else hermes.load_config()) or {}
    except Exception:
        return None


def cargar_config(home: Path, cruda: bool = False) -> dict | None:
    """config.yaml como dict. Prueba PyYAML y, si no está (el venv de Hermes no lo trae), el cargador de Hermes.

    `cruda` la lee tal cual está escrita (con las referencias `${VAR}` sin expandir): es lo que se compara con el paquete.
    """
    config = home / "config.yaml"
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except ImportError:
        return _config_de_hermes(home, cruda)


_SIN_VALOR = object()


def _valor(datos: dict, clave: str, defecto=None):
    for parte in clave.split("."):
        if not isinstance(datos, dict) or parte not in datos:
            return defecto
        datos = datos[parte]
    return datos


def config_pendiente(home: Path, perfil: str = "bot") -> list[str]:
    """Claves de CONFIG_POR_PERFIL que no tienen el valor deseado (o no se pudieron leer)."""
    deseado = CONFIG_POR_PERFIL[perfil]
    datos = cargar_config(home)
    if datos is None:
        return list(deseado)
    return [k for k, v in deseado.items() if _valor(datos, k) is not v]


def config_deseada(home: Path, origen: Origen) -> dict:
    """La configuración que tiene que quedar en el bot: los valores por defecto del runtime con la del paquete encima.

    Todo canal de mensajería que ya figure en `platform_toolsets` y el paquete no mencione recibe los toolsets por defecto: así un
    canal viejo con `web` o `vision` encendidos queda corregido.
    """
    base = json.loads(json.dumps(CONFIG_POR_DEFECTO))
    actual = (cargar_config(home, cruda=True) or {}).get("platform_toolsets") if (home / "config.yaml").is_file() else None
    for canal in (actual or {}) if isinstance(actual, dict) else ():
        if str(canal).strip().lower() not in paquete.CANALES_LOCALES:
            base["platform_toolsets"].setdefault(str(canal), list(TOOLSETS_POR_DEFECTO))
    return hermes_config.fusionar(base, origen.config())


def config_del_paquete_pendiente(home: Path, origen: Origen) -> list[str]:
    """Claves de la configuración deseada (runtime + paquete) que config.yaml no tiene así (comparando la config cruda)."""
    deseado = config_deseada(home, origen)
    if not deseado:
        return []
    datos = cargar_config(home, cruda=True)
    if datos is None:
        return [".".join(r) for r, _ in paquete._hojas(deseado)]
    pendientes = []
    for ruta, valor in paquete._hojas(deseado):
        clave = ".".join(ruta)
        actual = _valor(datos, clave, _SIN_VALOR)
        if clave == "plugins.enabled":  # el runtime agrega los suyos: alcanza con que estén los del paquete
            ok = isinstance(actual, list) and set(valor) <= set(actual)
        else:
            ok = actual == valor
        if not ok:
            pendientes.append(clave)
    return pendientes


def plugins_sin_habilitar(datos: dict | None, nombres: frozenset[str]) -> list[str]:
    habilitados = _valor(datos or {}, "plugins.enabled") or []
    return sorted(n for n in nombres if n not in habilitados)


def _extra_telegram(datos: dict) -> dict:
    """El bloque `extra` de Telegram donde Hermes lee el control de comandos (ruta de la doc y la alternativa de nivel superior)."""
    for ruta in (("gateway", "platforms", "telegram", "extra"), ("platforms", "telegram", "extra")):
        extra = _valor(datos, ".".join(ruta))
        if isinstance(extra, dict) and "allow_admin_from" in extra:
            return extra
    return {}


def _canal_principal(datos: dict) -> str:
    home = _valor(datos, "platforms.telegram.home_channel")
    return str(home.get("chat_id") or "") if isinstance(home, dict) else ""


def gating_pendiente(datos: dict, admins: tuple[str, ...]) -> list[str]:
    """Qué falta del control de comandos y del canal principal (vacío = todo en su lugar)."""
    problemas: list[str] = []
    extra = _extra_telegram(datos)
    actuales = [str(a) for a in (extra.get("allow_admin_from") or [])]
    if not actuales:
        problemas.append("allow_admin_from no está definido: cualquier usuario puede correr los 134 comandos (/pause, /sethome, /restart…)")
    elif admins and sorted(actuales) != sorted(admins):
        problemas.append("allow_admin_from no coincide con --admins")
    if sorted(str(c) for c in (extra.get("user_allowed_commands") or [])) != sorted(COMANDOS_USUARIO):
        problemas.append(f"user_allowed_commands debería ser {COMANDOS_USUARIO}")
    if admins and _canal_principal(datos) != admins[0]:
        problemas.append("falta el canal principal de Telegram (sin él, cada usuario recibe el aviso de /sethome)")
    return problemas


def pedido_de_config(home: Path, origen: Origen, perfil: str, admins: tuple[str, ...]) -> dict:
    """Lo que `hermes_config.py` escribe en config.yaml para este perfil."""
    if perfil == "admin":
        return {"forzado": CONFIG_POR_PERFIL["admin"]}
    return {
        "config": config_deseada(home, origen),
        "forzado": CONFIG_POR_PERFIL["bot"],
        "plugins": sorted(plugins_del_bot(origen)),
        "retirar": sorted(origen.retirar),
        "gating": {"admins": list(admins), "comandos": COMANDOS_USUARIO} if admins else None,
    }


def escribir_config(home: Path, pedido: dict) -> bool:
    """Escribe config.yaml con load/save de Hermes, en el Python de Hermes (ver hermes_config.py)."""
    repo = repo_de_hermes()
    if repo is None:
        print("  NO se pudo escribir config.yaml: no encuentro el código de Hermes (definir HERMES_REPO)")
        return False
    entorno = {**os.environ, "HERMES_HOME": str(home), "HERMES_REPO": str(repo), "AGENT_PROVISION_JSON": json.dumps(pedido)}
    resultado = subprocess.run([python_de_hermes(), str(ESCRITOR_CONFIG)], env=entorno, check=False, capture_output=True, text=True)
    if resultado.returncode != 0:
        print(f"  NO se pudo escribir config.yaml:\n{resultado.stderr.strip()[-800:]}")
        return False
    hecho = ["configuración del paquete"] if pedido.get("config") else []
    hecho += [f"{k} = {str(v).lower()}" for k, v in (pedido.get("forzado") or {}).items()]
    if pedido.get("plugins"):
        hecho.append(f"plugins habilitados: {', '.join(pedido['plugins'])}")
    gating = pedido.get("gating")
    if gating:
        hecho.append(f"control de comandos: {len(gating['admins'])} administrador(es), usuarios con "
                     f"{', '.join('/' + c for c in COMANDOS_USUARIO)}; canal principal fijado")
    print("  config.yaml escrito: " + "; ".join(hecho))
    return True


def entorno_del_gateway() -> dict[str, str] | None:
    """Variables de entorno del proceso del gateway (`hermes gateway run`), leídas de /proc; None si no se puede (otro SO o no corre acá).

    La autorización del gateway solo mira variables de entorno: `gateway.allow_all_users` del YAML no cuenta, y las del `.env`
    del volumen se re-siembran al reiniciar. Lo que vale es lo que el servicio le pasa al proceso.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    for carpeta in proc.iterdir():
        if not carpeta.name.isdigit() or int(carpeta.name) == os.getpid():
            continue
        try:
            comando = (carpeta / "cmdline").read_bytes()
            if b"gateway" not in comando or b"run" not in comando or b"provision" in comando:
                continue
            pares = (carpeta / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        return {k.decode(errors="replace"): v.decode(errors="replace") for k, _, v in (p.partition(b"=") for p in pares) if k}
    return None


def _listas_cerradas(entorno: dict[str, str] | None, home: Path) -> list[str]:
    """Dónde hay una `TELEGRAM_ALLOWED_USERS` con valor: el adaptador la convierte en `allow_from`, que manda sobre el allow-all."""
    donde: list[str] = []
    if entorno and entorno.get(LISTA_CERRADA, "").strip():
        donde.append("entorno del gateway")
    archivo = home / ".env"
    if archivo.is_file():
        for linea in archivo.read_text(encoding="utf-8", errors="replace").splitlines():
            nombre, _, valor = linea.partition("=")
            if nombre.strip() == LISTA_CERRADA and valor.strip():
                donde.append(".env del volumen")
    return donde


def revisar_admision(home: Path) -> tuple[list[str], list[str]]:
    """(fallas, avisos) de si el gateway admite a las personas nuevas para que `nucleo-acceso` pueda hacerles el alta."""
    fallas: list[str] = []
    avisos: list[str] = []
    entorno = entorno_del_gateway()
    if entorno is None:
        avisos.append("no pude leer el entorno del gateway: confirmar a mano que TELEGRAM_ALLOW_ALL_USERS=true está en las variables del servicio")
    elif entorno.get("TELEGRAM_ALLOW_ALL_USERS", "").strip().lower() not in ("1", "true", "yes", "on"):
        fallas.append(
            "TELEGRAM_ALLOW_ALL_USERS no está en el entorno del gateway: Hermes descarta a quien no esté en una lista ('Blocked "
            "unauthorized user') antes de nucleo-acceso, y nadie nuevo puede hacer el alta. Cargarla como variable del servicio y reiniciar"
        )
    for donde in _listas_cerradas(entorno, home):
        fallas.append(
            f"{LISTA_CERRADA} tiene una lista ({donde}): el adaptador de Telegram la convierte en allow_from, que manda sobre el allow-all y "
            "descarta a todos los que no estén en ella. Quitarla de las variables del servicio y del .env, y reiniciar"
        )
    return fallas, avisos


def revisar_entorno(home: Path, origen: Origen, perfil: str = "bot", admins: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
    """(fallas, avisos) de la configuración y de lo que el script no gestiona pero conviene vigilar."""
    fallas: list[str] = []
    avisos: list[str] = []
    bot = perfil == "bot"

    if (home / "config.yaml").is_file():
        datos = cargar_config(home)
        if datos is None:
            avisos.append("config.yaml: no pude leerlo (ni PyYAML ni el cargador de Hermes); revisar a mano")
        else:
            for clave in config_pendiente(home, perfil):
                fallas.append(f"config.yaml: {clave} debería ser {str(CONFIG_POR_PERFIL[perfil][clave]).lower()}")
            canales = (datos.get("platform_toolsets") or {}).items() if bot else ()
            for canal, toolsets in canales:
                sobran = paquete.toolsets_no_permitidos(canal, toolsets)
                if sobran:
                    fallas.append(f"config.yaml: el canal {canal} tiene toolsets no permitidos: {', '.join(sobran)} "
                                  f"(solo {', '.join(TOOLSETS_PERMITIDOS)})")
            if bot:
                for clave in config_del_paquete_pendiente(home, origen):
                    fallas.append(f"config.yaml: {clave} no coincide con la configuración del paquete")
                for nombre in plugins_sin_habilitar(datos, plugins_del_bot(origen)):
                    fallas.append(f"config.yaml: el plugin {nombre} no está en plugins.enabled")
                for problema in gating_pendiente(datos, admins):
                    (avisos if (not admins and problema.startswith("falta el canal")) else fallas).append(f"config.yaml: {problema}")
    else:
        avisos.append("config.yaml: no existe en este HERMES_HOME (la imagen lo crea al arrancar; sin él no se aplica la configuración)")

    if bot:
        f, a = revisar_admision(home)
        fallas += f
        avisos += a
        entorno = entorno_del_gateway() or {}
        if entorno.get(PANEL_WEB, "").strip().lower() in ("1", "true", "yes"):
            fallas.append(f"{PANEL_WEB} está encendida: el panel web de Hermes no tiene que correr (quitar la variable y reiniciar)")

    memorias = home / "memories"
    if bot and memorias.is_dir() and any(p.is_file() and p.stat().st_size for p in memorias.rglob("*")):
        avisos.append(
            "memories/ no está vacía: la memoria del agente se inyecta en el prompt de todos los usuarios; "
            "revisarla antes de dejar el bot abierto"
        )
    return fallas, avisos


def respaldar(home: Path, destinos: list[Path]) -> Path | None:
    existentes = [d for d in destinos if d.exists()]
    if not existentes:
        return None
    base = home / "backups" / f"provision-{time.strftime('%Y%m%d_%H%M%S')}"
    for destino in existentes:
        relativo = destino.relative_to(home)
        copia = base / relativo
        copia.parent.mkdir(parents=True, exist_ok=True)
        if destino.is_dir():
            shutil.copytree(destino, copia)
        else:
            shutil.copy2(destino, copia)
    return base


def comando_check(
    origen: Origen, home: Path, perfil: str = "bot", admins: tuple[str, ...] = (), mostrar_paquete: bool = True
) -> int:
    items = plan(origen, home, perfil)
    problemas = 0
    registro = leer_registro(home)
    if mostrar_paquete:
        print(f"  Paquete aplicado: {registro['version']} ({registro['aplicado']})" if registro else "  Paquete aplicado: ninguno registrado")
    for item in items:
        e = estado(item)
        problemas += e != OK
        print(f"  {'OK    ' if e == OK else 'FALLA '} {item.tipo:6} {item.nombre:28} {e}")

    quitar, obsoletos, ajenos = sobrantes(items, home, origen, perfil)
    motivo = "no va en el perfil admin" if perfil == "admin" else None
    for ruta in quitar:
        problemas += 1
        print(f"  FALLA  {'textos' if ruta.name == DESTINO_TEXTOS else 'skill':6} {ruta.name:28} sobrante ({motivo or 'no está en el paquete'})")
    for ruta in obsoletos:
        problemas += 1
        print(f"  FALLA  plugin {ruta.name:28} {motivo or 'obsoleto'}, hay que quitarlo")

    fallas, avisos = revisar_entorno(home, origen, perfil, admins)
    for mensaje in fallas:
        problemas += 1
        print(f"  FALLA  {mensaje}")
    for nombre in ajenos:
        avisos.append(f"plugin {nombre}: no está en el paquete ni en el runtime y no lo gestiono (si es de seguridad, versionarlo)")
    for mensaje in avisos:
        print(f"  AVISO  {mensaje}")

    print(f"\n{'Todo coincide con el paquete.' if not problemas else f'{problemas} cosa(s) para corregir.'}")
    return 1 if problemas else 0


def crear_perfil_admin(base: Path) -> None:
    """Crea el perfil de administración clonando el default (hereda credenciales y config; el provision lo deja limpio)."""
    hermes = shutil.which("hermes") or "/opt/hermes/bin/hermes"
    print(f"  Creando el perfil {PERFIL_ADMIN} (hermes profile create {PERFIL_ADMIN} --clone)")
    resultado = subprocess.run(
        [hermes, "profile", "create", PERFIL_ADMIN, "--clone"], env={**os.environ, "HERMES_HOME": str(base)}, check=False
    )
    if resultado.returncode != 0:
        raise SystemExit(f"No se pudo crear el perfil {PERFIL_ADMIN}; crealo a mano y volvé a correr apply")


def config_a_escribir(home: Path, origen: Origen, perfil: str, admins: tuple[str, ...]) -> bool:
    """¿Hay algo de config.yaml para escribir? Solo si el archivo existe (lo crea la imagen al arrancar)."""
    if not (home / "config.yaml").is_file():
        return False
    if config_pendiente(home, perfil):
        return True
    if perfil == "admin":
        return False
    datos = cargar_config(home)
    return bool(
        config_del_paquete_pendiente(home, origen)
        or plugins_sin_habilitar(datos, plugins_del_bot(origen))
        or (admins and datos is not None and gating_pendiente(datos, admins))
    )


def comando_apply(origen: Origen, home: Path, recargar: bool, perfil: str = "bot", admins: tuple[str, ...] = ()) -> int:
    items = plan(origen, home, perfil)
    pendientes = [i for i in items if estado(i) != OK]
    quitar_skills, obsoletos, _ = sobrantes(items, home, origen, perfil)
    quitar = quitar_skills + obsoletos
    config = config_a_escribir(home, origen, perfil, admins)

    if not pendientes and not quitar and not config:
        print("Nada que cambiar: el volumen ya coincide con el paquete.")
    else:
        respaldo = respaldar(home, [i.destino for i in pendientes] + quitar + ([home / "config.yaml"] if config else []))
        if respaldo:
            print(f"  Respaldo en {respaldo}")
        if config:
            escribir_config(home, pedido_de_config(home, origen, perfil, admins))
        for item in pendientes:
            copiar(item.origen, item.destino)
            print(f"  copiado {item.tipo:6} {item.nombre}")
        for ruta in quitar:
            shutil.rmtree(ruta)
            print(f"  quitado {ruta.parent.name}/{ruta.name}")

    print("\nVerificación posterior:")
    codigo = comando_check(origen, home, perfil, admins, mostrar_paquete=False)
    if perfil == "admin":
        print(f"\nPerfil de administración listo. Entrá a la TUI con: hermes -p {PERFIL_ADMIN}")
    elif recargar and (pendientes or quitar):
        recargar_hermes(home)
    elif pendientes or quitar or config:
        print(
            "\nReiniciá el gateway para que tome SOUL.md, las skills y la configuración, o recargá los plugins con --reload "
            "(un reload borra el mapa sesión-usuario: las tools del núcleo quedan bloqueadas hasta el próximo mensaje)."
        )
    return codigo


def recargar_hermes(home: Path) -> None:
    codigo = (
        "from pathlib import Path; from gateway.control_socket import reload_gateway_plugins; "
        f"print(reload_gateway_plugins(Path({str(home)!r})))"
    )
    entorno = {**os.environ, "HERMES_HOME": str(home)}
    resultado = subprocess.run(
        [python_de_hermes(), "-c", codigo], cwd=repo_de_hermes(), env=entorno, check=False
    )
    print("  plugins recargados" if resultado.returncode == 0 else "  NO se pudo recargar: reiniciar el gateway")


def registrar_paquete(home: Path, origen: Origen, clave: str) -> None:
    registro = {
        "nombre": origen.nombre,
        "version": origen.manifiesto["version"],
        "clave": clave,
        "plugins": sorted(plugins_del_bot(origen)),
        "aplicado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (home / REGISTRO).write_text(json.dumps(registro, indent=2), encoding="utf-8")


def leer_registro(home: Path) -> dict | None:
    try:
        datos = json.loads((home / REGISTRO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return datos if isinstance(datos, dict) and datos.get("version") else None


def version_del_runtime() -> str:
    """`<versión de Hermes>-<revisión del overlay>`, de kit-runtime/VERSION. Sin ese archivo ningún paquete es compatible."""
    try:
        return ARCHIVO_VERSION.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0-0"


def abrir_paquete(zip_: str, esperado: str = "") -> tuple[Origen, str, object]:
    ruta = Path(zip_)
    if not ruta.is_file():
        raise SystemExit(f"--source no existe o no es un archivo: {zip_}")
    raiz, manifiesto, clave, tmp = paquete.abrir(ruta, paquete.cargar_claves(CARPETA_CLAVES), version_del_runtime(), esperado)
    return Origen(raiz, manifiesto), clave, tmp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="comando", required=True)
    for nombre in ("check", "apply"):
        p = sub.add_parser(nombre)
        p.add_argument("--source", required=True, help="el paquete firmado (.zip)")
        p.add_argument(
            "--sha256",
            default=os.environ.get("AGENT_PACK_SHA256", ""),
            help="huella esperada del .zip (o $AGENT_PACK_SHA256); si no coincide no se aplica nada",
        )
        p.add_argument("--home", type=Path, default=None, help="HERMES_HOME (por defecto $HERMES_HOME o /data/.hermes)")
        p.add_argument(
            "--perfil",
            choices=("bot", "admin"),
            default="bot",
            help="bot = perfil default, el que atiende a la red; admin = profiles/admin, el de la TUI de administración",
        )
        p.add_argument(
            "--admins",
            default=os.environ.get("KIT_ADMIN_TELEGRAM_IDS") or os.environ.get("IGA_ADMIN_TELEGRAM_IDS", ""),
            help="IDs de Telegram de los administradores, separados por coma (o $KIT_ADMIN_TELEGRAM_IDS, y si no está "
                 "$IGA_ADMIN_TELEGRAM_IDS); el primero es el canal principal",
        )
        if nombre == "apply":
            p.add_argument("--reload", action="store_true", help="recargar los plugins de Hermes al terminar")
    p = sub.add_parser("bundle", help="arma y firma un paquete a partir de la declaración de la carpeta del cliente")
    p.add_argument("--decl", required=True, type=Path, help="agent-pack.toml de la carpeta del cliente")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--version", required=True, help="versión del paquete, X.Y.Z")
    p.add_argument("--clave-privada", type=Path, default=os.environ.get("AGENT_PACK_SIGNING_KEY") or None,
                   help="clave privada ed25519 en PEM (o $AGENT_PACK_SIGNING_KEY); nunca dentro de un repo")
    p = sub.add_parser("keygen", help="crea el par de claves para firmar paquetes")
    p.add_argument("--privada", required=True, type=Path)
    p.add_argument("--publica", required=True, type=Path)
    args = parser.parse_args(argv)
    for flujo in (sys.stdout, sys.stderr):
        flujo.reconfigure(encoding="utf-8", errors="replace")  # consolas de Windows en cp1252

    if args.comando == "keygen":
        clave = paquete.generar_claves(args.privada, args.publica)
        print(f"Clave {clave}: privada en {args.privada} (guardarla fuera de todo repo), pública en {args.publica}")
        return 0
    if args.comando == "bundle":
        if not args.clave_privada:
            raise SystemExit("bundle necesita la clave privada (--clave-privada o $AGENT_PACK_SIGNING_KEY): un paquete sin firma no se aplica")
        decl = paquete.leer_declaracion(args.decl)
        manifiesto = paquete.armar(decl, args.version, args.out, args.clave_privada, frozenset(plugins_de(CARPETA_PLUGINS_RUNTIME)))
        print(f"Paquete {decl.nombre} {args.version} para el runtime {' '.join(decl.runtime)}: {args.out} "
              f"({len(manifiesto['archivos'])} archivos)\nsha256: {paquete.huella_archivo(args.out)}")
        return 0

    origen, clave, tmp = abrir_paquete(args.source, args.sha256)
    try:
        home = args.home or Path(os.environ.get("HERMES_HOME", "/data/.hermes"))
        if not home.is_dir():
            raise SystemExit(f"HERMES_HOME no existe: {home}")
        base = home
        home = home_del_perfil(base, args.perfil)
        if args.perfil == "admin" and not home.is_dir():
            if args.comando == "apply":
                crear_perfil_admin(base)
            else:
                print(f"FALLA  el perfil {PERFIL_ADMIN} no existe todavía ({home}); lo crea `apply --perfil admin`")
                return 1
        print(f"Paquete: {origen.nombre} {origen.manifiesto['version']} (firmado con {clave})\n"
              f"Runtime: {version_del_runtime()}\nPerfil: {args.perfil}\nHermes: {home}\n")
        admins = tuple(a.strip() for a in args.admins.split(",") if a.strip())
        malos = [a for a in admins if not a.isdigit()]
        if malos:
            raise SystemExit(f"--admins espera IDs numéricos de Telegram, no: {', '.join(malos)}")
        if args.comando == "check":
            return comando_check(origen, home, args.perfil, admins)
        codigo = comando_apply(origen, home, args.reload, args.perfil, admins)
        if codigo == 0 and args.perfil == "bot":
            registrar_paquete(home, origen, clave)
            print(f"Paquete {origen.manifiesto['version']} registrado en {home / REGISTRO}")
        return codigo
    finally:
        tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
