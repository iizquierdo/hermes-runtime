"""apply-pack.sh de punta a punta: orden de fuentes (URL, núcleo, imagen), nunca corta el arranque y reporta al núcleo.

Corre el script real con `sh` sobre una copia del aplicador con su propia clave de prueba (las claves de la imagen no se tocan).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import paquete
from conftest import DECLARACION, VARS_DEL_PAQUETE, escribir
from test_nucleo import TOKEN, NucleoFalso, _servir_paquete

RAIZ = Path(__file__).resolve().parents[2]


@pytest.fixture
def runtime_copia(tmp_path):
    """kit-runtime/ copiado (aplicador, VERSION y plugins reales) con una clave de prueba en keys/."""
    destino = tmp_path / "kit-runtime"
    shutil.copytree(RAIZ / "provision", destino / "provision", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    shutil.copytree(RAIZ / "plugins", destino / "plugins", ignore=shutil.ignore_patterns("__pycache__", "test_*.py"))
    shutil.copy(RAIZ / "VERSION", destino / "VERSION")
    privada = tmp_path / "fuera" / "pack.pem"
    paquete.generar_claves(privada, destino / "keys" / "prueba.pub")
    return destino, privada


def _armar(runtime_copia, fuente, tmp_path, version, nombre=None):
    destino, privada = runtime_copia
    salida = tmp_path / (nombre or f"pack-{version}.zip")
    r = subprocess.run([sys.executable, str(destino / "provision" / "provision.py"), "bundle", "--decl", str(fuente / "agent-pack.toml"),
                        "--out", str(salida), "--version", version, "--clave-privada", str(privada)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return salida


def _correr(runtime_copia, home, **entorno):
    destino, _ = runtime_copia
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KIT_", "IGA_", "AGENT_PACK"))}
    env.update({"HERMES_HOME": str(home), "HERMES_PYTHON": sys.executable, "PYTHONIOENCODING": "utf-8", **VARS_DEL_PAQUETE})
    env.update(entorno)
    sh = shutil.which("sh")
    assert sh, "hace falta `sh` para probar apply-pack.sh"
    r = subprocess.run([sh, str(destino / "provision" / "apply-pack.sh")], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    return r.returncode, r.stdout + r.stderr


@pytest.fixture
def entorno_cliente(fuente, tmp_path):
    escribir(fuente / "agent-pack.toml", DECLARACION)
    home = tmp_path / "hermes"
    home.mkdir()
    return fuente, home


def test_aplica_el_zip_de_la_imagen_y_reporta(runtime_copia, entorno_cliente, tmp_path):
    fuente, home = entorno_cliente
    zip_ = _armar(runtime_copia, fuente, tmp_path, "1.0.0")
    falso = NucleoFalso()
    falso.respuestas["/runtime/status"] = (200, {"ok": True}, {})
    try:
        codigo, salida = _correr(runtime_copia, home, AGENT_PACK_FILE=str(zip_), KIT_CORE_URL=falso.url, KIT_CORE_TOKEN=TOKEN)
    finally:
        falso.servidor.shutdown()
    assert codigo == 0 and "[agent-pack] paquete aplicado" in salida
    assert json.loads((home / ".agent-pack.json").read_text())["version"] == "1.0.0"
    reporte = json.loads(falso.recibidos("/runtime/status")[0][3])
    assert reporte["pack_version"] == "1.0.0"


def test_el_paquete_del_nucleo_gana_sobre_el_de_la_imagen(runtime_copia, entorno_cliente, tmp_path):
    fuente, home = entorno_cliente
    imagen = _armar(runtime_copia, fuente, tmp_path, "1.0.0")
    del_nucleo = _armar(runtime_copia, fuente, tmp_path, "2.0.0")
    falso = NucleoFalso()
    _servir_paquete(falso, del_nucleo)
    try:
        codigo, salida = _correr(runtime_copia, home, AGENT_PACK_FILE=str(imagen), KIT_PACK_FROM_CORE="1",
                                 KIT_CORE_URL=falso.url, KIT_CORE_TOKEN=TOKEN, AGENT_PACK_SHA256="0" * 64)
    finally:
        falso.servidor.shutdown()
    assert codigo == 0, salida
    assert json.loads((home / ".agent-pack.json").read_text())["version"] == "2.0.0"


def test_sin_paquete_activo_en_el_nucleo_se_usa_el_de_la_imagen(runtime_copia, entorno_cliente, tmp_path):
    fuente, home = entorno_cliente
    imagen = _armar(runtime_copia, fuente, tmp_path, "1.0.0")
    falso = NucleoFalso()
    falso.respuestas["/runtime/pack"] = (204, b"", {})
    try:
        _correr(runtime_copia, home, AGENT_PACK_FILE=str(imagen), KIT_PACK_FROM_CORE="1", KIT_CORE_URL=falso.url, KIT_CORE_TOKEN=TOKEN)
    finally:
        falso.servidor.shutdown()
    assert json.loads((home / ".agent-pack.json").read_text())["version"] == "1.0.0"


def test_si_el_nucleo_no_responde_sigue_el_ultimo_aplicado_y_no_el_de_la_imagen(runtime_copia, entorno_cliente, tmp_path):
    fuente, home = entorno_cliente
    imagen = _armar(runtime_copia, fuente, tmp_path, "1.0.0")
    nuevo = _armar(runtime_copia, fuente, tmp_path, "2.0.0")
    _correr(runtime_copia, home, AGENT_PACK_FILE=str(nuevo))                      # quedó aplicado el 2.0.0
    codigo, salida = _correr(runtime_copia, home, AGENT_PACK_FILE=str(imagen), KIT_PACK_FROM_CORE="1",
                             KIT_CORE_URL="http://127.0.0.1:1", KIT_CORE_TOKEN=TOKEN)
    assert codigo == 0 and "no se pudo bajar el paquete del núcleo" in salida
    assert json.loads((home / ".agent-pack.json").read_text())["version"] == "2.0.0"


def test_un_paquete_malo_no_corta_el_arranque_y_se_reporta_la_falla(runtime_copia, entorno_cliente, tmp_path):
    fuente, home = entorno_cliente
    roto = tmp_path / "roto.zip"
    roto.write_bytes(b"no es un zip")
    falso = NucleoFalso()
    falso.respuestas["/runtime/status"] = (200, {"ok": True}, {})
    try:
        codigo, salida = _correr(runtime_copia, home, AGENT_PACK_FILE=str(roto), KIT_CORE_URL=falso.url, KIT_CORE_TOKEN=TOKEN)
    finally:
        falso.servidor.shutdown()
    assert codigo == 0 and "el paquete NO se aplicó" in salida
    assert not (home / "SOUL.md").exists()
    reporte = json.loads(falso.recibidos("/runtime/status")[0][3])
    assert any("no se aplicó en este arranque" in c["text"] for c in reporte["checks"])


def test_url_sin_huella_no_se_descarga(runtime_copia, entorno_cliente):
    _, home = entorno_cliente
    codigo, salida = _correr(runtime_copia, home, AGENT_PACK_URL="http://127.0.0.1:1/x.zip")
    assert codigo == 0 and "requiere AGENT_PACK_SHA256" in salida
