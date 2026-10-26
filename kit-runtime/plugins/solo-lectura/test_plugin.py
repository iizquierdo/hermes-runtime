import importlib.util
import sqlite3
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("solo_lectura", Path(__file__).with_name("__init__.py"))
sl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sl)


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    ruta = tmp_path / "state.db"
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    con.executemany(
        "INSERT INTO sessions VALUES (?, ?)",
        [("s-telegram", "telegram"), ("s-tui", "tui"), ("s-vacia", "")],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(sl, "_DB", ruta)
    monkeypatch.setattr(sl, "_ACTIVO", True)
    sl._por_sesion.clear()


def llamar(tool, sesion, args=None):
    return sl.antes_de_la_tool(tool_name=tool, session_id=sesion, args=args)


def test_bloquea_una_tool_mutadora_en_telegram():
    resultado = llamar("terminal", "s-telegram")
    assert resultado["action"] == "block"
    assert "solo lectura" in resultado["message"]


@pytest.mark.parametrize("tool", ["skill_manage", "memory", "write_file", "browser_click", "cronjob_manage"])
def test_bloquea_el_resto_de_la_superficie_de_cambio(tool):
    assert llamar(tool, "s-telegram")["action"] == "block"


def test_el_administrador_por_tui_no_se_bloquea():
    assert llamar("terminal", "s-tui") is None


@pytest.mark.parametrize("tool", ["mcp__nucleo__get_landing", "web_search", "clarify", "skill_view"])
def test_las_tools_de_consulta_y_el_mcp_pasan_en_telegram(tool):
    assert llamar(tool, "s-telegram") is None


def test_bloquea_una_mutadora_escondida_en_un_tool_call_diferido():
    args = {"calls": [{"name": "get_landing"}, {"name": "terminal"}]}
    assert llamar("tool_call", "s-telegram", args)["action"] == "block"


def test_un_tool_call_diferido_hacia_el_mcp_pasa():
    assert llamar("tool_call", "s-telegram", {"calls": [{"name": "mcp__nucleo__get_landing"}]}) is None


@pytest.mark.parametrize("sesion", ["", "no-existe", "s-vacia"])
def test_falla_abierto_solo_si_no_se_sabe_la_plataforma(sesion):
    assert llamar("terminal", sesion) is None


def test_falla_abierto_si_state_db_no_se_puede_leer(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "_DB", tmp_path / "no-existe.db")
    assert llamar("terminal", "s-telegram") is None


def test_apagado_no_bloquea_nada(monkeypatch):
    monkeypatch.setattr(sl, "_ACTIVO", False)
    assert llamar("terminal", "s-telegram") is None


def test_register_engancha_pre_tool_call():
    enganches = []

    class Ctx:
        def register_hook(self, nombre, funcion):
            enganches.append((nombre, funcion))

    sl.register(Ctx())
    assert enganches == [("pre_tool_call", sl.antes_de_la_tool)]


def test_el_texto_de_bloqueo_es_neutro_y_lo_puede_poner_el_paquete(tmp_path, monkeypatch):
    assert "(PT:" in sl.TEXTOS_POR_DEFECTO["bloqueo"] and "TUI" not in sl.TEXTOS_POR_DEFECTO["bloqueo"]
    (tmp_path / "solo-lectura.json").write_text('{"bloqueo": "Solo consulto."}', encoding="utf-8")
    monkeypatch.setenv("KIT_TEXTOS_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("solo_lectura_textos", Path(__file__).with_name("__init__.py"))
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    assert modulo.TEXTO_BLOQUEO == "Solo consulto."
