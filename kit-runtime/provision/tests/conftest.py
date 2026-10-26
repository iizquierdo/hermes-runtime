import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paquete  # noqa: E402
import provision as p  # noqa: E402

VERSION_RUNTIME = "0.22.2-1"

DECLARACION = """\
nombre = "cliente"
runtime = [">=0.22.2-1", "<0.23"]

[contenido]
soul = "SOUL.md"
soul_admin = "SOUL.admin.md"
skills = ["skills", "compartidas"]
plugins = ["plugins-propios"]
textos = "textos"
config = "config.json"

[retirar]
plugins = ["viejo-diag"]
"""

CONFIG_DEL_PAQUETE = {
    "mcp_servers": {"nucleo": {"url": "${CORE_MCP_URL}", "headers": {"Authorization": "Bearer ${MCP_KIT_TOKEN}"}}},
    "platform_toolsets": {"telegram": ["skills", "clarify"]},
    "session_reset": {"mode": "idle", "idle_minutes": 120},
}

# Las variables que usa CONFIG_DEL_PAQUETE: un gateway bien configurado las tiene en su entorno.
VARS_DEL_PAQUETE = {"CORE_MCP_URL": "http://nucleo:8000/mcp", "MCP_KIT_TOKEN": "token-de-prueba"}

# Los plugins que el bot tiene que tener habilitados con el paquete de los tests: el del runtime falso y el propio del cliente.
PLUGINS_HABILITADOS = ["cli-extra", "nucleo-acceso"]


def escribir(ruta: Path, texto: str) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(texto.encode())


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    """Un runtime de prueba: un plugin propio, su versión y ninguna clave de confianza (las agrega `clave`)."""
    plugins = tmp_path / "runtime" / "plugins"
    escribir(plugins / "nucleo-acceso" / "plugin.yaml", "name: nucleo-acceso\n")
    escribir(plugins / "nucleo-acceso" / "__init__.py", "x = 1\n")
    escribir(plugins / "nucleo-acceso" / "__pycache__" / "x.pyc", "basura")
    monkeypatch.setattr(p, "CARPETA_PLUGINS_RUNTIME", plugins)
    monkeypatch.setattr(p, "CARPETA_CLAVES", tmp_path / "runtime" / "keys")
    monkeypatch.setattr(p, "version_del_runtime", lambda: VERSION_RUNTIME)
    monkeypatch.delenv("AGENT_PACK_SHA256", raising=False)
    monkeypatch.delenv("KIT_ADMIN_TELEGRAM_IDS", raising=False)
    monkeypatch.delenv("IGA_ADMIN_TELEGRAM_IDS", raising=False)
    return plugins


@pytest.fixture
def clave(tmp_path) -> Path:
    """Par de claves de prueba: la pública queda en las claves de la imagen; la privada, fuera de todo repo."""
    privada = tmp_path / "fuera-del-repo" / "agent-pack.pem"
    paquete.generar_claves(privada, p.CARPETA_CLAVES / "prueba.pub")
    return privada


@pytest.fixture
def fuente(tmp_path) -> Path:
    raiz = tmp_path / "cliente"
    escribir(raiz / "SOUL.md", "# Identidad\r\nSos el asistente.\r\n")
    escribir(raiz / "SOUL.admin.md", "# Admin\n")
    escribir(raiz / "skills" / "cli-alta" / "SKILL.md", "alta\n")
    escribir(raiz / "skills" / "cli-alta" / "references" / "x.md", "ref\n")
    escribir(raiz / "compartidas" / "cli-marca" / "SKILL.md", "marca\n")
    escribir(raiz / "plugins-propios" / "cli-extra" / "plugin.yaml", "name: cli-extra\n")
    escribir(raiz / "plugins-propios" / "cli-extra" / "__init__.py", "y = 2\n")
    escribir(raiz / "textos" / "nucleo-acceso.json", json.dumps({"pendiente": "Esperá la aprobación."}))
    escribir(raiz / "config.json", json.dumps(CONFIG_DEL_PAQUETE))
    escribir(raiz / "agent-pack.toml", DECLARACION)
    return raiz


@pytest.fixture
def armar(fuente, clave, tmp_path):
    """armar(version) -> ruta del paquete firmado hecho con `provision.py bundle`."""
    def _armar(version: str = "1.2.3", nombre: str = "pack.zip") -> Path:
        destino = tmp_path / "salida" / version / nombre
        destino.parent.mkdir(parents=True, exist_ok=True)
        args = ["bundle", "--decl", str(fuente / "agent-pack.toml"), "--out", str(destino), "--version", version,
                "--clave-privada", str(clave)]
        assert p.main(args) == 0
        return destino
    return _armar


@pytest.fixture
def origen(armar):
    """El paquete ya verificado y abierto, como lo usa `apply`."""
    o, _, tmp = p.abrir_paquete(str(armar()))
    yield o
    tmp.cleanup()


@pytest.fixture
def home(tmp_path) -> Path:
    carpeta = tmp_path / "hermes"
    carpeta.mkdir()
    return carpeta
