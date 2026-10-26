"""Lo que hermes_config.py escribe en config.yaml (la parte pura; el guardado lo hace Hermes)."""
import hermes_config as hc

CRUDO = {
    "model": {"default": "otro"},
    "mcp_servers": {"viejo": {"url": "http://x"}},
    "plugins": {"enabled": ["del-operador", "viejo-diag"]},
    "memory": {"memory_enabled": True, "provider": "x"},
}


def test_la_config_del_paquete_se_fusiona_encima():
    nuevo = hc.aplicar(CRUDO, {"config": {"mcp_servers": {"nucleo": {"url": "${CORE_MCP_URL}"}}, "model": {"default": "m"}}})
    assert nuevo["mcp_servers"] == {"viejo": {"url": "http://x"}, "nucleo": {"url": "${CORE_MCP_URL}"}}
    assert nuevo["model"] == {"default": "m"}
    assert CRUDO["model"] == {"default": "otro"}  # no modifica lo que recibe


def test_lo_que_fija_el_runtime_gana_sobre_el_paquete():
    nuevo = hc.aplicar(CRUDO, {"config": {"memory": {"memory_enabled": True}}, "forzado": {"memory.memory_enabled": False,
                                                                                         "memory.user_profile_enabled": False}})
    assert nuevo["memory"] == {"memory_enabled": False, "provider": "x", "user_profile_enabled": False}


def test_los_plugins_se_agregan_sin_duplicar_y_los_retirados_salen():
    nuevo = hc.aplicar(CRUDO, {"plugins": ["nucleo-acceso", "del-operador"], "retirar": ["viejo-diag"]})
    assert nuevo["plugins"]["enabled"] == ["del-operador", "nucleo-acceso"]


def test_el_control_de_comandos_va_en_el_extra_de_telegram():
    nuevo = hc.aplicar({}, {"gating": {"admins": ["111", "222"], "comandos": ["new", "retry", "undo"]}})
    assert nuevo["gateway"]["platforms"]["telegram"]["extra"] == {
        "allow_admin_from": ["111", "222"], "user_allowed_commands": ["new", "retry", "undo"]}


def test_sin_gating_no_se_toca_el_bloque_de_telegram():
    assert "gateway" not in hc.aplicar({}, {"gating": None})


def test_se_preserva_todo_lo_que_escribe_el_pedido():
    pedido = {"config": {"session_reset": {"mode": "idle", "idle_minutes": 120}, "x": {}}, "forzado": {"memory.memory_enabled": False},
              "plugins": ["a"]}
    assert hc.claves_a_preservar(pedido) == {("session_reset", "mode"), ("session_reset", "idle_minutes"), ("x",),
                                             ("memory", "memory_enabled"), ("plugins", "enabled")}
