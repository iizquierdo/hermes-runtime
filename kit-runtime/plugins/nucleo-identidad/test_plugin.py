"""El plugin firma como espera el servidor y falla cerrado. Necesita el núcleo importable (mcp-kit/src y sus dependencias)."""
import importlib.util
import json
import types
from pathlib import Path

import pytest

from mcp_kit.nucleo.identidad_firmada import verificar

SECRETO = "secreto-de-prueba"
NOMBRES = "nucleo-tools,mcp__nucleo__"


def _cargar(nombre="nucleo_identidad_plugin"):
    spec = importlib.util.spec_from_file_location(nombre, Path(__file__).parent / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(autouse=True)
def entorno(monkeypatch, tmp_path):
    monkeypatch.setenv("MCPKIT_IDENTIDAD_SECRETO", SECRETO)
    monkeypatch.setenv("KIT_MCP_NOMBRES", NOMBRES)
    monkeypatch.setenv("KIT_TEXTOS_DIR", str(tmp_path / "sin-textos"))
    for v in ("IDENTIDAD_HEADER", "IDENTIDAD_CON_CANAL", "MCP_NOMBRES"):
        monkeypatch.delenv(f"IGA_{v}", raising=False)
    for v in ("KIT_IDENTIDAD_HEADER", "KIT_IDENTIDAD_CON_CANAL"):
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def plugin():
    return _cargar()


def _mensaje(usuario, sesion="S1"):
    almacen = types.SimpleNamespace(get_or_create_session=lambda fuente: types.SimpleNamespace(session_id=sesion))
    return dict(event=types.SimpleNamespace(source=types.SimpleNamespace(user_id=usuario)), session_store=almacen)


def test_la_firma_la_acepta_el_servidor(plugin):
    assert verificar(SECRETO, plugin.firmar(SECRETO, "x-channel-id", "2090477055")) == ("x-channel-id", "2090477055")


def test_reconoce_las_tools_por_el_nombre_del_servidor(plugin):
    """Hermes registra las tools de un servidor MCP como `mcp__<servidor>__<tool>`."""
    plugin.al_despachar(**_mensaje(2090477055))
    r = plugin.antes_de_la_tool(tool_name="mcp__nucleo__get_my_branch", session_id="S1", args={})
    assert r["action"] == "modify"
    assert plugin.antes_de_la_tool(tool_name="mcp__playwright__browser_navigate", session_id="S1", args={}) is None


def test_agrega_la_identidad_a_una_tool_del_nucleo(plugin):
    plugin.al_despachar(**_mensaje(2090477055))
    r = plugin.antes_de_la_tool(tool_name="mcp_nucleo-tools_get_landing", session_id="S1", args={})
    assert r["action"] == "modify"
    assert verificar(SECRETO, r["args"]["identidad_firmada"]) == ("x-channel-id", "2090477055")


def test_cada_sesion_usa_su_usuario(plugin):
    plugin.al_despachar(**_mensaje(111, "A"))
    plugin.al_despachar(**_mensaje(222, "B"))
    a = plugin.antes_de_la_tool(tool_name="mcp_nucleo-tools_x", session_id="A")
    b = plugin.antes_de_la_tool(tool_name="mcp_nucleo-tools_x", session_id="B")
    assert verificar(SECRETO, a["args"]["identidad_firmada"])[1] == "111"
    assert verificar(SECRETO, b["args"]["identidad_firmada"])[1] == "222"


def test_por_defecto_manda_canal_dos_puntos_id_si_hay_plataforma(plugin):
    mensaje = _mensaje(2090477055)
    mensaje["event"].source.platform = "telegram"
    plugin.al_despachar(**mensaje)
    r = plugin.antes_de_la_tool(tool_name="mcp_nucleo-tools_get_landing", session_id="S1", args={})
    assert verificar(SECRETO, r["args"]["identidad_firmada"]) == ("x-channel-id", "telegram:2090477055")


def test_con_canal_y_otro_header(monkeypatch):
    monkeypatch.setenv("KIT_IDENTIDAD_CON_CANAL", "1")
    monkeypatch.setenv("KIT_IDENTIDAD_HEADER", "x-cliente-chat-id")
    modulo = _cargar("nucleo_identidad_canal")
    for plataforma, esperado in (("telegram", "telegram:2090477055"), (types.SimpleNamespace(value="WhatsApp"), "whatsapp:2090477055"),
                                 (None, "2090477055"), ("Raro!", "2090477055")):          # sin plataforma reconocible: el id a secas
        mensaje = _mensaje(2090477055)
        mensaje["event"].source.platform = plataforma
        modulo.al_despachar(**mensaje)
        r = modulo.antes_de_la_tool(tool_name="mcp_nucleo-tools_get_landing", session_id="S1", args={})
        assert verificar(SECRETO, r["args"]["identidad_firmada"]) == ("x-cliente-chat-id", esperado)


@pytest.mark.parametrize("valor, esperado", [(None, "telegram:2090477055"), ("1", "telegram:2090477055"), ("0", "2090477055")])
def test_el_canal_va_por_defecto_y_se_apaga_con_cero(monkeypatch, valor, esperado):
    if valor is not None:
        monkeypatch.setenv("KIT_IDENTIDAD_CON_CANAL", valor)
    modulo = _cargar("nucleo_identidad_defecto")
    mensaje = _mensaje(2090477055)
    mensaje["event"].source.platform = "telegram"
    modulo.al_despachar(**mensaje)
    r = modulo.antes_de_la_tool(tool_name="mcp__nucleo__get_my_branch", session_id="S1", args={})
    assert verificar(SECRETO, r["args"]["identidad_firmada"]) == ("x-channel-id", esperado)


def test_no_toca_las_tools_de_otros(plugin):
    plugin.al_despachar(**_mensaje(1))
    assert plugin.antes_de_la_tool(tool_name="terminal", session_id="S1") is None
    assert plugin.antes_de_la_tool(tool_name="mcp_otro_servidor_x", session_id="S1") is None


def test_sin_usuario_conocido_bloquea(plugin):
    plugin.al_despachar(**_mensaje(None))
    r = plugin.antes_de_la_tool(tool_name="mcp_nucleo-tools_x", session_id="desconocida")
    assert r == {"action": "block", "message": plugin.TEXTOS_POR_DEFECTO["sin_usuario"]}


def test_sin_secreto_bloquea(plugin, monkeypatch):
    monkeypatch.delenv("MCPKIT_IDENTIDAD_SECRETO")
    plugin.al_despachar(**_mensaje(1))
    assert plugin.antes_de_la_tool(tool_name="mcp_nucleo-tools_x", session_id="S1")["action"] == "block"


def test_sin_nombres_de_servidor_no_firma_nada_y_lo_avisa(monkeypatch, caplog):
    monkeypatch.delenv("KIT_MCP_NOMBRES")
    modulo = _cargar("nucleo_identidad_sin_nombres")
    modulo.al_despachar(**_mensaje(1))
    assert modulo.antes_de_la_tool(tool_name="mcp__nucleo__x", session_id="S1") is None
    modulo.register(types.SimpleNamespace(register_hook=lambda n, f: None))
    assert "KIT_MCP_NOMBRES" in caplog.text


def test_los_textos_de_bloqueo_los_pone_el_paquete(monkeypatch, tmp_path):
    (tmp_path / "nucleo-identidad.json").write_text(json.dumps({"sin_usuario": "No sé quién sos."}), encoding="utf-8")
    monkeypatch.setenv("KIT_TEXTOS_DIR", str(tmp_path))
    modulo = _cargar("nucleo_identidad_textos")
    modulo.al_despachar(**_mensaje(None))
    assert modulo.antes_de_la_tool(tool_name="mcp__nucleo__x", session_id="otra")["message"] == "No sé quién sos."


def test_registra_los_dos_hooks(plugin):
    hooks = {}
    plugin.register(types.SimpleNamespace(register_hook=lambda n, f: hooks.setdefault(n, f)))
    assert hooks == {"pre_gateway_dispatch": plugin.al_despachar, "pre_tool_call": plugin.antes_de_la_tool}


def test_las_variables_iga_siguen_valiendo_y_la_kit_gana(monkeypatch):
    monkeypatch.delenv("KIT_MCP_NOMBRES")
    monkeypatch.setenv("IGA_MCP_NOMBRES", "iga-tools,mcp__iga__")
    monkeypatch.setenv("IGA_IDENTIDAD_HEADER", "x-iga-chat-id")
    modulo = _cargar("nucleo_identidad_compat")
    modulo.al_despachar(**_mensaje(7))
    r = modulo.antes_de_la_tool(tool_name="mcp__iga__get_my_branch", session_id="S1")
    assert verificar(SECRETO, r["args"]["identidad_firmada"]) == ("x-iga-chat-id", "7")

    monkeypatch.setenv("KIT_IDENTIDAD_HEADER", "x-channel-id")
    modulo = _cargar("nucleo_identidad_compat_kit")
    modulo.al_despachar(**_mensaje(7))
    r = modulo.antes_de_la_tool(tool_name="mcp__iga__get_my_branch", session_id="S1")
    assert verificar(SECRETO, r["args"]["identidad_firmada"])[0] == "x-channel-id"
