import json
import sys
from pathlib import Path

import pytest

import provision as p
from conftest import CONFIG_DEL_PAQUETE, PLUGINS_HABILITADOS, escribir


def _config(memoria: bool = False, perfil: bool = False, gating: bool = True, del_paquete: bool = True,
            plugins: list[str] | None = None, **extra) -> str:
    """Un config.yaml (JSON es YAML válido) con la memoria, el control de comandos y lo que el paquete pide."""
    datos: dict = {"memory": {"memory_enabled": memoria, "user_profile_enabled": perfil},
                   "security": {"allow_private_urls": False, "allow_lazy_installs": False}}
    if gating:
        datos["gateway"] = {"platforms": {"telegram": {"extra": {"allow_admin_from": ["111"], "user_allowed_commands": ["new", "retry", "undo"]}}}}
        datos["platforms"] = {"telegram": {"home_channel": {"chat_id": "111"}}}
    if del_paquete:
        datos.update(json.loads(json.dumps(CONFIG_DEL_PAQUETE)))
        datos["plugins"] = {"enabled": PLUGINS_HABILITADOS if plugins is None else plugins}
    datos.update(extra)
    return json.dumps(datos)


def _perfil_admin(home: Path) -> Path:
    carpeta = home / "profiles" / "admin"
    carpeta.mkdir(parents=True)
    return carpeta


# --- copiar el paquete al volumen

def test_check_en_volumen_vacio_falla(origen, home):
    assert p.comando_check(origen, home) == 1


def test_apply_deja_el_volumen_igual_al_paquete(origen, home):
    assert p.comando_apply(origen, home, recargar=False) == 0

    assert (home / "SOUL.md").read_bytes() == b"# Identidad\nSos el asistente.\n"  # LF, sin CRLF
    assert (home / "skills/cliente/cli-alta/references/x.md").read_text() == "ref\n"
    assert (home / "skills/cliente/cli-marca/SKILL.md").exists()  # la de la segunda carpeta también
    assert (home / "plugins/nucleo-acceso/plugin.yaml").exists()  # el del runtime
    assert not (home / "plugins/nucleo-acceso/__pycache__").exists()
    assert (home / "plugins/cli-extra/__init__.py").read_text() == "y = 2\n"  # el propio del cliente
    assert json.loads((home / "textos/nucleo-acceso.json").read_text(encoding="utf-8")) == {"pendiente": "Esperá la aprobación."}


def test_apply_es_idempotente(origen, home):
    p.comando_apply(origen, home, recargar=False)
    assert not (home / "backups").exists()  # primera vez: no había nada que respaldar

    assert p.comando_apply(origen, home, recargar=False) == 0
    assert not (home / "backups").exists()  # segunda: nada que cambiar, ni respaldo


def test_apply_respalda_lo_que_pisa(origen, home):
    escribir(home / "SOUL.md", "el SOUL por defecto de Hermes")

    p.comando_apply(origen, home, recargar=False)

    respaldos = list((home / "backups").glob("provision-*/SOUL.md"))
    assert len(respaldos) == 1
    assert respaldos[0].read_text() == "el SOUL por defecto de Hermes"


def test_detecta_cambio_hecho_a_mano_y_lo_corrige(origen, home):
    p.comando_apply(origen, home, recargar=False)
    escribir(home / "plugins/nucleo-acceso/__init__.py", "x = 'editado por el agente'\n")

    assert p.comando_check(origen, home) == 1
    assert p.comando_apply(origen, home, recargar=False) == 0
    assert (home / "plugins/nucleo-acceso/__init__.py").read_text() == "x = 1\n"


def test_quita_skills_sobrantes_y_los_plugins_que_el_paquete_retira(origen, home):
    escribir(home / "skills/cliente/cli-diagnostico/SKILL.md", "viejo")
    escribir(home / "plugins/viejo-diag/plugin.yaml", "name: viejo-diag\n")

    p.comando_apply(origen, home, recargar=False)

    assert not (home / "skills/cliente/cli-diagnostico").exists()
    assert not (home / "plugins/viejo-diag").exists()
    assert list((home / "backups").glob("provision-*/skills/cliente/cli-diagnostico/SKILL.md"))


def test_no_toca_skills_del_bundle_de_hermes_ni_plugins_ajenos(origen, home, capsys):
    escribir(home / "skills/research/arxiv/SKILL.md", "de hermes")
    escribir(home / "plugins/otro-ajeno/plugin.yaml", "name: otro-ajeno\n")

    assert p.comando_apply(origen, home, recargar=False) == 0  # el ajeno es aviso, no falla

    assert (home / "skills/research/arxiv/SKILL.md").exists()
    assert (home / "plugins/otro-ajeno/plugin.yaml").exists()
    assert "plugin otro-ajeno: no está en el paquete ni en el runtime" in capsys.readouterr().out


def test_un_plugin_que_instalo_la_version_anterior_y_esta_no_trae_se_quita(armar, fuente, home):
    assert p.main(["apply", "--source", str(armar("0.1.0")), "--home", str(home)]) == 0
    assert (home / "plugins/cli-extra").exists()

    import shutil
    shutil.rmtree(fuente / "plugins-propios" / "cli-extra")
    assert p.main(["apply", "--source", str(armar("0.2.0")), "--home", str(home)]) == 0

    assert not (home / "plugins/cli-extra").exists()
    assert p.leer_registro(home)["plugins"] == ["nucleo-acceso"]


def test_los_textos_se_quitan_si_el_paquete_ya_no_los_trae(armar, fuente, home):
    p.main(["apply", "--source", str(armar("0.1.0")), "--home", str(home)])
    (fuente / "textos" / "nucleo-acceso.json").unlink()
    (fuente / "agent-pack.toml").write_text((fuente / "agent-pack.toml").read_text().replace('textos = "textos"\n', ""))

    assert p.main(["apply", "--source", str(armar("0.2.0")), "--home", str(home)]) == 0
    assert not (home / "textos").exists()


@pytest.mark.parametrize("toolset", ["terminal", "web", "vision", "tts", "image_gen", "file"])
def test_falla_si_un_canal_tiene_un_toolset_no_permitido(origen, home, capsys, toolset):
    p.comando_apply(origen, home, recargar=False)
    escribir(home / "config.yaml", _config(platform_toolsets={"telegram": ["skills", toolset]}))

    assert p.comando_check(origen, home) == 1
    assert f"el canal telegram tiene toolsets no permitidos: {toolset}" in capsys.readouterr().out


def test_los_canales_locales_no_tienen_la_restriccion_de_toolsets(origen, home):
    p.comando_apply(origen, home, recargar=False)
    escribir(home / "config.yaml", _config(platform_toolsets={"telegram": ["skills", "clarify"], "cli": ["terminal", "web"]}))

    assert p.comando_check(origen, home) == 0


def test_avisa_si_la_memoria_no_esta_vacia(origen, home, capsys):
    p.comando_apply(origen, home, recargar=False)
    escribir(home / "memories/MEMORY.md", "algo")

    assert p.comando_check(origen, home) == 0  # aviso, no falla
    assert "memories/ no está vacía" in capsys.readouterr().out


def test_bundle_y_apply_por_la_linea_de_comandos(armar, home):
    paquete_zip = armar()

    assert p.main(["apply", "--source", str(paquete_zip), "--home", str(home)]) == 0
    assert (home / "skills/cliente/cli-marca/SKILL.md").exists()


def test_una_declaracion_sin_soul_da_un_error_claro(fuente, clave, tmp_path):
    (fuente / "SOUL.md").unlink()
    with pytest.raises(SystemExit, match="contenido.soul"):
        p.main(["bundle", "--decl", str(fuente / "agent-pack.toml"), "--out", str(tmp_path / "x.zip"), "--version", "1.0.0",
                "--clave-privada", str(clave)])


def test_bundle_sin_clave_privada_no_arma_nada(fuente, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_PACK_SIGNING_KEY", raising=False)
    with pytest.raises(SystemExit, match="clave privada"):
        p.main(["bundle", "--decl", str(fuente / "agent-pack.toml"), "--out", str(tmp_path / "x.zip"), "--version", "1.0.0"])
    assert not (tmp_path / "x.zip").exists()


def test_el_paquete_no_puede_traer_un_plugin_con_el_nombre_de_uno_del_runtime(fuente, clave, tmp_path):
    escribir(fuente / "plugins-propios" / "nucleo-acceso" / "plugin.yaml", "name: nucleo-acceso\n")
    with pytest.raises(SystemExit, match="es del runtime"):
        p.main(["bundle", "--decl", str(fuente / "agent-pack.toml"), "--out", str(tmp_path / "x.zip"), "--version", "1.0.0",
                "--clave-privada", str(clave)])


# --- configuración: memoria, la del paquete y los plugins habilitados

def test_apply_escribe_la_config_que_falta_y_respalda_config_yaml(origen, home, monkeypatch):
    escribir(home / "config.yaml", "memory:\n  memory_enabled: true\n")
    pedidos = []
    monkeypatch.setattr(p, "escribir_config", lambda h, pedido: pedidos.append(pedido) or True)

    p.comando_apply(origen, home, recargar=False)

    assert len(pedidos) == 1
    assert pedidos[0]["forzado"] == {"memory.memory_enabled": False, "memory.user_profile_enabled": False, "security.allow_private_urls": False, "security.allow_lazy_installs": False}
    assert pedidos[0]["config"] == CONFIG_DEL_PAQUETE
    assert pedidos[0]["plugins"] == PLUGINS_HABILITADOS
    assert pedidos[0]["retirar"] == ["viejo-diag"]
    assert pedidos[0]["gating"] is None  # sin --admins no se toca el control de comandos
    assert list((home / "backups").glob("provision-*/config.yaml"))


def test_check_falla_si_la_memoria_esta_encendida_y_pasa_si_esta_apagada(origen, home):
    p.comando_apply(origen, home, recargar=False)

    escribir(home / "config.yaml", _config(memoria=True))
    assert p.comando_check(origen, home) == 1

    escribir(home / "config.yaml", _config())
    assert p.comando_check(origen, home) == 0


def test_check_falla_si_la_config_del_paquete_no_esta_aplicada(origen, home, capsys):
    p.comando_apply(origen, home, recargar=False)
    escribir(home / "config.yaml", _config(del_paquete=False))

    assert p.comando_check(origen, home) == 1
    salida = capsys.readouterr().out
    assert "mcp_servers.nucleo.url no coincide con la configuración del paquete" in salida
    assert "el plugin cli-extra no está en plugins.enabled" in salida


def test_la_config_del_paquete_se_compara_sin_expandir_las_variables(origen, home, monkeypatch):
    p.comando_apply(origen, home, recargar=False)
    monkeypatch.setenv("MCP_KIT_TOKEN", "valor-real")
    escribir(home / "config.yaml", _config())

    assert p.config_del_paquete_pendiente(home, origen) == []


def test_plugins_enabled_puede_tener_otros_ademas_de_los_del_paquete(origen, home):
    p.comando_apply(origen, home, recargar=False)
    escribir(home / "config.yaml", _config(plugins=["algo-del-operador", *PLUGINS_HABILITADOS]))

    assert p.comando_check(origen, home) == 0


def test_sin_config_yaml_no_se_escribe_config(origen, home, monkeypatch, capsys):
    monkeypatch.setattr(p, "escribir_config", lambda *a: pytest.fail("no debería escribir sin config.yaml"))

    assert p.comando_apply(origen, home, recargar=False) == 0
    assert "config.yaml: no existe" in capsys.readouterr().out


def _perfil_bot_listo(origen, home):
    p.comando_apply(origen, home, recargar=False)
    escribir(home / "config.yaml", _config())


def test_perfil_admin_inexistente_falla_en_check(armar, home):
    assert p.main(["check", "--source", str(armar()), "--home", str(home), "--perfil", "admin"]) == 1


def test_apply_admin_crea_el_perfil_si_falta(armar, home, monkeypatch):
    creados = []
    monkeypatch.setattr(p, "crear_perfil_admin", lambda base: (creados.append(base), _perfil_admin(base)))

    assert p.main(["apply", "--source", str(armar()), "--home", str(home), "--perfil", "admin"]) == 0

    assert creados == [home]
    assert (home / "profiles/admin/SOUL.md").read_text() == "# Admin\n"


def test_apply_admin_limpia_lo_que_trae_el_clon_y_no_toca_al_bot(origen, home, armar):
    _perfil_bot_listo(origen, home)
    admin = _perfil_admin(home)
    # lo que deja un `profile create --clone`
    escribir(admin / "skills/cliente/cli-alta/SKILL.md", "copia")
    escribir(admin / "plugins/nucleo-acceso/plugin.yaml", "name: nucleo-acceso\n")
    escribir(admin / "plugins/cli-extra/plugin.yaml", "name: cli-extra\n")
    escribir(admin / "plugins/otro-ajeno/plugin.yaml", "name: x\n")
    escribir(admin / "skills/research/arxiv/SKILL.md", "de hermes")

    assert p.main(["apply", "--source", str(armar()), "--home", str(home), "--perfil", "admin"]) == 0

    assert not (admin / "skills/cliente/cli-alta").exists()
    assert not (admin / "plugins/nucleo-acceso").exists()
    assert not (admin / "plugins/cli-extra").exists()
    assert (admin / "plugins/otro-ajeno").exists()  # ajeno: aviso, no se toca
    assert (admin / "skills/research/arxiv/SKILL.md").exists()
    assert (home / "skills/cliente/cli-alta/SKILL.md").exists()  # el perfil del bot sigue igual
    assert p.comando_check(origen, home) == 0


def test_admin_tiene_la_memoria_encendida_y_el_bot_apagada(origen, home):
    admin = _perfil_admin(home)
    p.comando_apply(origen, admin, recargar=False, perfil="admin")

    escribir(admin / "config.yaml", _config(gating=False, del_paquete=False))
    assert p.comando_check(origen, admin, "admin") == 1  # el admin necesita memoria

    escribir(admin / "config.yaml", _config(True, True, gating=False, del_paquete=False))
    assert p.comando_check(origen, admin, "admin") == 0
    # terminal en un canal no falla en admin (solo se vigila en el bot)
    escribir(admin / "config.yaml", _config(True, True, gating=False, del_paquete=False, platform_toolsets={"telegram": ["terminal"]}))
    assert p.comando_check(origen, admin, "admin") == 0


def test_el_perfil_admin_no_recibe_la_config_del_paquete(origen, home, monkeypatch):
    admin = _perfil_admin(home)
    escribir(admin / "config.yaml", _config(gating=False, del_paquete=False))
    pedidos = []
    monkeypatch.setattr(p, "escribir_config", lambda h, pedido: pedidos.append(pedido) or True)

    p.comando_apply(origen, admin, recargar=False, perfil="admin")

    assert pedidos == [{"forzado": {"memory.memory_enabled": True, "memory.user_profile_enabled": True,
                                    "security.allow_private_urls": False, "security.allow_lazy_installs": False}}]


def test_un_paquete_sin_soul_admin_no_aprovisiona_el_perfil_admin(fuente, clave, home, tmp_path):
    (fuente / "SOUL.admin.md").unlink()
    (fuente / "agent-pack.toml").write_text((fuente / "agent-pack.toml").read_text().replace('soul_admin = "SOUL.admin.md"\n', ""))
    zip_ = tmp_path / "sin-admin.zip"
    p.main(["bundle", "--decl", str(fuente / "agent-pack.toml"), "--out", str(zip_), "--version", "1.0.0", "--clave-privada", str(clave)])
    _perfil_admin(home)

    with pytest.raises(SystemExit, match="SOUL.admin.md"):
        p.main(["apply", "--source", str(zip_), "--home", str(home), "--perfil", "admin"])


# --- control de comandos y canal principal

def test_check_falla_si_no_hay_administradores_configurados(origen, home):
    p.comando_apply(origen, home, recargar=False)
    escribir(home / "config.yaml", _config(gating=False))

    assert p.comando_check(origen, home) == 1  # sin allow_admin_from, Hermes no restringe ningún comando


def test_check_pasa_con_el_control_de_comandos_completo(origen, home):
    _perfil_bot_listo(origen, home)

    assert p.comando_check(origen, home) == 0
    assert p.comando_check(origen, home, admins=("111",)) == 0
    assert p.comando_check(origen, home, admins=("222",)) == 1  # otro administrador del que dice --admins


def test_apply_con_admins_fija_el_control_de_comandos_y_respalda_la_config(origen, home, monkeypatch):
    escribir(home / "config.yaml", _config(gating=False))
    pedidos = []
    monkeypatch.setattr(p, "escribir_config", lambda h, pedido: pedidos.append(pedido) or True)

    p.comando_apply(origen, home, recargar=False, admins=("111", "222"))

    assert pedidos[0]["gating"] == {"admins": ["111", "222"], "comandos": ["new", "retry", "undo"]}
    assert list((home / "backups").glob("provision-*/config.yaml"))


def test_apply_no_toca_la_config_si_ya_esta_en_su_lugar(origen, home, monkeypatch):
    _perfil_bot_listo(origen, home)
    monkeypatch.setattr(p, "escribir_config", lambda *a: pytest.fail("no debería volver a escribir"))

    assert p.comando_apply(origen, home, recargar=False, admins=("111",)) == 0


def test_admins_no_numericos_se_rechazan(armar, home):
    with pytest.raises(SystemExit, match="IDs numéricos"):
        p.main(["check", "--source", str(armar()), "--home", str(home), "--admins", "nacho"])


@pytest.mark.parametrize("variable", ["KIT_ADMIN_TELEGRAM_IDS", "IGA_ADMIN_TELEGRAM_IDS"])
def test_los_admins_salen_de_la_variable_de_entorno(armar, home, monkeypatch, variable):
    monkeypatch.setenv(variable, "nacho")
    with pytest.raises(SystemExit, match="IDs numéricos"):
        p.main(["check", "--source", str(armar()), "--home", str(home)])


def test_el_perfil_admin_no_lleva_control_de_comandos(origen, home):
    admin = _perfil_admin(home)
    p.comando_apply(origen, admin, recargar=False, perfil="admin")
    escribir(admin / "config.yaml", _config(True, True, gating=False, del_paquete=False))

    assert p.comando_check(origen, admin, "admin", ("111",)) == 0


def test_escribir_config_corre_el_escritor_con_el_python_de_hermes(monkeypatch, home):
    llamadas = []

    class Resultado:
        returncode = 0
        stderr = ""

    def falso(cmd, env, **kw):
        llamadas.append((cmd, env))
        return Resultado()

    monkeypatch.setattr(p.subprocess, "run", falso)
    monkeypatch.setattr(p, "repo_de_hermes", lambda: Path("/opt/hermes"))
    pedido = {"forzado": {"memory.memory_enabled": False}, "gating": {"admins": ["111", "222"], "comandos": ["new"]}}

    assert p.escribir_config(home, pedido) is True

    cmd, env = llamadas[0]
    assert cmd[-1].endswith("hermes_config.py")
    assert env["HERMES_HOME"] == str(home) and env["HERMES_REPO"] == str(Path("/opt/hermes"))
    assert json.loads(env["AGENT_PROVISION_JSON"]) == pedido


def test_escribir_config_avisa_si_no_encuentra_hermes(home, monkeypatch, capsys):
    monkeypatch.setattr(p, "repo_de_hermes", lambda: None)

    assert p.escribir_config(home, {"forzado": {}}) is False
    assert "no encuentro el código de Hermes" in capsys.readouterr().out


# --- admisión de usuarios nuevos: lo decide el entorno del proceso, no el YAML

def test_check_falla_si_el_gateway_no_admite_a_los_usuarios_nuevos(origen, home, monkeypatch, capsys):
    p.comando_apply(origen, home, recargar=False)
    monkeypatch.setattr(p, "entorno_del_gateway", lambda: {"TELEGRAM_ALLOWED_USERS": "111"})

    assert p.comando_check(origen, home) == 1
    assert "TELEGRAM_ALLOW_ALL_USERS no está en el entorno del gateway" in capsys.readouterr().out


@pytest.mark.parametrize("valor", ["true", "1", "True"])
def test_check_pasa_si_el_gateway_admite_a_los_usuarios_nuevos(origen, home, monkeypatch, valor):
    p.comando_apply(origen, home, recargar=False)
    monkeypatch.setattr(p, "entorno_del_gateway", lambda: {"TELEGRAM_ALLOW_ALL_USERS": valor})

    assert p.comando_check(origen, home) == 0


def test_si_no_puede_leer_el_entorno_solo_avisa(origen, home, monkeypatch, capsys):
    p.comando_apply(origen, home, recargar=False)
    monkeypatch.setattr(p, "entorno_del_gateway", lambda: None)

    assert p.comando_check(origen, home) == 0
    assert "confirmar a mano" in capsys.readouterr().out


def test_el_perfil_admin_no_revisa_la_admision(origen, home, monkeypatch):
    admin = _perfil_admin(home)
    p.comando_apply(origen, admin, recargar=False, perfil="admin")
    monkeypatch.setattr(p, "entorno_del_gateway", lambda: {})

    assert p.comando_check(origen, admin, "admin") == 0


def test_la_clave_del_yaml_no_se_usa_para_abrir_el_gateway():
    assert "gateway.allow_all_users" not in p.CONFIG_POR_PERFIL["bot"]
    assert "gateway.allow_all_users" not in p.CONFIG_POR_PERFIL["admin"]


@pytest.mark.parametrize("donde", ["entorno", "env"])
def test_check_falla_si_hay_una_lista_cerrada_que_anula_el_allow_all(origen, home, monkeypatch, capsys, donde):
    p.comando_apply(origen, home, recargar=False)
    entorno = {"TELEGRAM_ALLOW_ALL_USERS": "true"}
    if donde == "entorno":
        entorno["TELEGRAM_ALLOWED_USERS"] = "true"
    else:
        escribir(home / ".env", "OTRA=1\nTELEGRAM_ALLOWED_USERS=111\n")
    monkeypatch.setattr(p, "entorno_del_gateway", lambda: entorno)

    assert p.comando_check(origen, home) == 1
    salida = capsys.readouterr().out
    assert "TELEGRAM_ALLOWED_USERS tiene una lista" in salida
    assert ("entorno del gateway" if donde == "entorno" else ".env del volumen") in salida


def test_una_lista_vacia_en_el_env_no_molesta(origen, home, monkeypatch):
    p.comando_apply(origen, home, recargar=False)
    escribir(home / ".env", "TELEGRAM_ALLOWED_USERS=\n")
    monkeypatch.setattr(p, "entorno_del_gateway", lambda: {"TELEGRAM_ALLOW_ALL_USERS": "true"})

    assert p.comando_check(origen, home) == 0


# --- versión aplicada

def test_apply_registra_la_version_y_check_la_muestra(armar, home, capsys):
    paquete_zip = armar("0.7.0")

    assert p.main(["apply", "--source", str(paquete_zip), "--home", str(home)]) == 0

    registro = p.leer_registro(home)
    assert registro["version"] == "0.7.0" and registro["nombre"] == "cliente"
    assert registro["clave"] and registro["plugins"] == PLUGINS_HABILITADOS
    capsys.readouterr()
    p.main(["check", "--source", str(paquete_zip), "--home", str(home)])
    assert "Paquete aplicado: 0.7.0" in capsys.readouterr().out


def test_una_actualizacion_reemplaza_la_version_registrada(armar, fuente, home):
    p.main(["apply", "--source", str(armar("0.7.0")), "--home", str(home)])
    escribir(fuente / "skills" / "cli-alta" / "SKILL.md", "alta, versión nueva\n")

    p.main(["apply", "--source", str(armar("0.8.0")), "--home", str(home)])

    assert p.leer_registro(home)["version"] == "0.8.0"
    assert (home / "skills/cliente/cli-alta/SKILL.md").read_text(encoding="utf-8") == "alta, versión nueva\n"


def test_la_huella_esperada_del_zip_se_verifica(armar, home):
    paquete_zip = armar()
    huella = p.paquete.huella_archivo(paquete_zip)

    assert p.main(["check", "--source", str(paquete_zip), "--home", str(home), "--sha256", huella.upper()]) == 1  # coincide: corre (y falla por estar vacío)
    with pytest.raises(SystemExit, match="no coincide con la esperada"):
        p.main(["check", "--source", str(paquete_zip), "--home", str(home), "--sha256", "0" * 64])


def test_una_carpeta_no_se_acepta_como_origen(fuente, home):
    """Solo se aplica un paquete firmado: ya no se puede aplicar directo desde la carpeta del cliente."""
    with pytest.raises(SystemExit, match="no es un archivo"):
        p.main(["apply", "--source", str(fuente), "--home", str(home)])
    assert not (home / "SOUL.md").exists()


# --- las dos disposiciones de la imagen de Hermes (plantilla de Railway y oficial)

def test_encuentra_el_codigo_de_hermes_por_la_variable_o_por_las_rutas_conocidas(tmp_path, monkeypatch):
    codigo = tmp_path / "hermes"
    (codigo / "hermes_cli").mkdir(parents=True)
    monkeypatch.setenv("HERMES_REPO", str(codigo))

    assert p.repo_de_hermes() == codigo

    monkeypatch.setenv("HERMES_REPO", str(tmp_path / "no-existe"))
    assert p.repo_de_hermes() in (None, Path("/opt/hermes"), Path("/opt/hermes/repo"))


def test_python_de_hermes_prefiere_la_variable_y_cae_al_del_proceso(tmp_path, monkeypatch):
    falso = tmp_path / "python"
    falso.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_PYTHON", str(falso))
    assert p.python_de_hermes() == str(falso)

    monkeypatch.setenv("HERMES_PYTHON", str(tmp_path / "no-existe"))
    assert p.python_de_hermes() in (sys.executable, "/opt/hermes/.venv/bin/python", "/opt/hermes/venv/bin/python")


def test_la_version_del_runtime_sale_de_overlay_version(monkeypatch, tmp_path):
    monkeypatch.undo()  # sin el reemplazo del conftest
    archivo = tmp_path / "VERSION"
    archivo.write_text("0.22.2-3\n")
    monkeypatch.setattr(p, "ARCHIVO_VERSION", archivo)
    assert p.version_del_runtime() == "0.22.2-3"
    monkeypatch.setattr(p, "ARCHIVO_VERSION", tmp_path / "no-existe")
    assert p.version_del_runtime() == "0.0.0-0"  # sin versión, ningún paquete es compatible


def test_el_overlay_real_tiene_version_y_plugins():
    raiz = Path(__file__).resolve().parents[2]
    assert p.paquete.version_runtime((raiz / "VERSION").read_text().strip())
    assert {"nucleo-acceso", "nucleo-identidad", "solo-lectura", "hermes-session-reset-policy"} <= set(p.plugins_de(raiz / "plugins"))


# --- valores por defecto del runtime: toolsets, seguridad, sesiones y panel web

def test_sin_config_en_el_paquete_quedan_los_valores_por_defecto(armar, fuente, home, monkeypatch):
    (fuente / "agent-pack.toml").write_text((fuente / "agent-pack.toml").read_text().replace('config = "config.json"\n', ""))
    o, _, tmp = p.abrir_paquete(str(armar("0.3.0")))
    escribir(home / "config.yaml", json.dumps({"platform_toolsets": {"telegram": ["web", "vision", "skills"], "discord": ["web"],
                                                                     "cli": ["terminal"]}}))
    pedidos = []
    monkeypatch.setattr(p, "escribir_config", lambda h, pedido: pedidos.append(pedido) or True)

    p.comando_apply(o, home, recargar=False)
    tmp.cleanup()

    assert pedidos[0]["config"] == {
        "platform_toolsets": {"telegram": ["skills", "clarify", "todo"], "discord": ["skills", "clarify", "todo"]},
        "session_reset": {"mode": "idle", "idle_minutes": 120},
    }


def test_el_paquete_puede_cambiar_el_corte_de_sesion_y_achicar_los_toolsets(origen, home, monkeypatch):
    escribir(home / "config.yaml", "{}")
    pedidos = []
    monkeypatch.setattr(p, "escribir_config", lambda h, pedido: pedidos.append(pedido) or True)

    p.comando_apply(origen, home, recargar=False)

    assert pedidos[0]["config"]["platform_toolsets"]["telegram"] == ["skills", "clarify"]   # lo que pide el paquete de prueba
    assert pedidos[0]["config"]["session_reset"] == {"mode": "idle", "idle_minutes": 120}


def test_check_falla_si_la_seguridad_no_esta_fijada(origen, home, capsys):
    p.comando_apply(origen, home, recargar=False)
    datos = json.loads(_config())
    datos["security"]["allow_private_urls"] = True
    del datos["security"]["allow_lazy_installs"]
    escribir(home / "config.yaml", json.dumps(datos))

    assert p.comando_check(origen, home) == 1
    salida = capsys.readouterr().out
    assert "security.allow_private_urls debería ser false" in salida and "security.allow_lazy_installs debería ser false" in salida


@pytest.mark.parametrize("valor, falla", [("true", True), ("1", True), ("0", False), ("", False)])
def test_check_falla_si_el_panel_web_esta_encendido(origen, home, monkeypatch, capsys, valor, falla):
    _perfil_bot_listo(origen, home)
    monkeypatch.setattr(p, "entorno_del_gateway", lambda: {"TELEGRAM_ALLOW_ALL_USERS": "true", "HERMES_DASHBOARD": valor})

    assert p.comando_check(origen, home) == (1 if falla else 0)
    assert ("HERMES_DASHBOARD está encendida" in capsys.readouterr().out) is falla
