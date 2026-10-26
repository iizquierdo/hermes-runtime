"""Plano de control: config y estado (mcp-kit/docs/runtime-control-plane.md) y el paquete desde el núcleo. Núcleo falso por HTTP."""
import hashlib
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import nucleo
import provision as p
from conftest import VARS_DEL_PAQUETE, escribir

TOKEN = "token-de-runtime"


class NucleoFalso:
    """`respuestas[ruta] = (http, cuerpo, headers)`; guarda (método, ruta, headers, cuerpo) de cada pedido."""

    def __init__(self):
        self.respuestas: dict[str, tuple] = {}
        self.pedidos: list[tuple] = []
        self.demora = 0.0
        falso = self

        class H(BaseHTTPRequestHandler):
            def _responder(self, metodo):
                largo = int(self.headers.get("Content-Length") or 0)
                cuerpo = self.rfile.read(largo) if largo else b""
                falso.pedidos.append((metodo, self.path, dict(self.headers), cuerpo))
                time.sleep(falso.demora)
                codigo, datos, headers = falso.respuestas.get(self.path, (404, {"error": "not_found"}, {}))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    codigo, datos, headers = 401, {"error": "no_autorizado"}, {}
                if callable(datos):
                    codigo, datos, headers = datos(self.headers)
                crudo = datos if isinstance(datos, bytes) else json.dumps(datos).encode()
                try:
                    self.send_response(codigo)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    if codigo not in (204, 304):
                        self.send_header("Content-Length", str(len(crudo)))
                    self.end_headers()
                    if codigo not in (204, 304):
                        self.wfile.write(crudo)
                except OSError:
                    pass

            def do_GET(self):
                self._responder("GET")

            def do_POST(self):
                self._responder("POST")

            def log_message(self, *a):
                pass

        self.servidor = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.servidor.server_port}"
        threading.Thread(target=self.servidor.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def config(self, admins, comandos=("new", "retry", "undo")):
        self.respuestas["/runtime/config"] = (200, {"admins": list(admins), "allowed_commands": list(comandos)}, {})

    def recibidos(self, ruta):
        return [x for x in self.pedidos if x[1] == ruta]


@pytest.fixture
def falso(monkeypatch):
    n = NucleoFalso()
    monkeypatch.setenv("KIT_CORE_URL", n.url)
    monkeypatch.setenv("KIT_CORE_TOKEN", TOKEN)
    for v in ("KIT_RUNTIME_ID", "IGA_CORE_URL", "IGA_CORE_TOKEN", "KIT_ADMIN_TELEGRAM_IDS", "IGA_ADMIN_TELEGRAM_IDS"):
        monkeypatch.delenv(v, raising=False)
    yield n
    n.servidor.shutdown()
    n.servidor.server_close()


@pytest.fixture
def sin_nucleo(monkeypatch):
    for v in ("KIT_CORE_URL", "KIT_CORE_TOKEN", "IGA_CORE_URL", "IGA_CORE_TOKEN"):
        monkeypatch.delenv(v, raising=False)


# --- GET /runtime/config

def test_pide_la_config_con_el_token_de_runtime(falso):
    falso.config(["slack:U1", "telegram:111", "telegram:222"], ["new"])
    config, motivo = nucleo.pedir_config(nucleo.Nucleo.del_entorno())
    assert motivo == "" and config.admins_de("telegram") == ("111", "222") and config.comandos == ("new",)
    assert falso.pedidos[0][2]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("codigo, cuerpo", [(401, {"error": "no_autorizado"}), (404, {"error": "not_found"}), (500, {}),
                                            (200, b"no es json"), (200, [1]), (200, {"admins": "todos", "allowed_commands": []}),
                                            (200, {"admins": ["111"], "allowed_commands": []}),       # sin canal
                                            (200, {"admins": [], "allowed_commands": ["Pause"]})])    # comando con mayúsculas
def test_cualquier_respuesta_rara_es_no_se(falso, codigo, cuerpo):
    falso.respuestas["/runtime/config"] = (codigo, cuerpo, {})
    config, motivo = nucleo.pedir_config(nucleo.Nucleo.del_entorno())
    assert config is None and motivo


def test_un_nucleo_lento_es_no_se(falso):
    falso.config(["telegram:111"])
    falso.demora = 1.0
    assert nucleo.pedir_config(nucleo.Nucleo.del_entorno(), timeout=0.3)[0] is None


def test_sin_url_o_token_no_hay_nucleo(sin_nucleo, monkeypatch):
    assert nucleo.Nucleo.del_entorno() is None
    monkeypatch.setenv("KIT_CORE_URL", "ftp://x")
    monkeypatch.setenv("KIT_CORE_TOKEN", "t")
    assert nucleo.Nucleo.del_entorno() is None


def test_las_variables_iga_siguen_valiendo(sin_nucleo, monkeypatch):
    monkeypatch.setenv("IGA_CORE_URL", "https://core/")
    monkeypatch.setenv("IGA_CORE_TOKEN", "t")
    assert nucleo.Nucleo.del_entorno() == nucleo.Nucleo("https://core", "t")


# --- de dónde salen los administradores

def test_los_administradores_salen_del_nucleo(falso, monkeypatch):
    monkeypatch.setenv("KIT_ADMIN_TELEGRAM_IDS", "999")
    falso.config(["telegram:111", "whatsapp:+54911", "telegram:abc"], ["new", "help2"])
    assert p.resolver_control(None) == (("111",), ("new", "help2"), "núcleo")


@pytest.mark.parametrize("respuesta", [(500, {}, {}), (200, {"admins": [], "allowed_commands": ["new"]}, {})])
def test_si_el_nucleo_no_responde_o_no_tiene_admins_se_usa_la_variable(falso, monkeypatch, capsys, respuesta):
    monkeypatch.setenv("KIT_ADMIN_TELEGRAM_IDS", "999,888")
    falso.respuestas["/runtime/config"] = respuesta
    assert p.resolver_control(None) == (("999", "888"), None, "KIT_ADMIN_TELEGRAM_IDS")
    assert "[plano-de-control]" in capsys.readouterr().out


def test_admins_explicitos_ganan_y_sin_nucleo_vale_la_variable_iga(sin_nucleo, monkeypatch):
    assert p.resolver_control("1,2")[0] == ("1", "2")
    monkeypatch.setenv("IGA_ADMIN_TELEGRAM_IDS", "7")
    assert p.resolver_control(None) == (("7",), None, "KIT_ADMIN_TELEGRAM_IDS")


def test_apply_usa_los_admins_y_comandos_del_nucleo(falso, armar, home, monkeypatch):
    falso.config(["telegram:111"], ["new", "retry"])
    escribir(home / "config.yaml", "{}")
    pedidos = []
    monkeypatch.setattr(p, "escribir_config", lambda h, pedido: pedidos.append(pedido) or True)

    p.main(["apply", "--source", str(armar()), "--home", str(home)])

    assert pedidos[0]["gating"] == {"admins": ["111"], "comandos": ["new", "retry"]}


def test_check_compara_con_los_comandos_del_nucleo(falso, origen, home):
    p.comando_apply(origen, home, recargar=False)
    datos = {"memory": {"memory_enabled": False, "user_profile_enabled": False},
             "gateway": {"platforms": {"telegram": {"extra": {"allow_admin_from": ["111"], "user_allowed_commands": ["new"]}}}}}
    escribir(home / "config.yaml", json.dumps(datos))
    assert not [x for x in p.gating_pendiente(datos, ("111",), ("new",)) if "user_allowed_commands" in x]
    assert [x for x in p.gating_pendiente(datos, ("111",)) if "user_allowed_commands" in x]  # contra los por defecto


# --- POST /runtime/status

def test_apply_guarda_una_copia_del_paquete_aplicado(sin_nucleo, armar, home):
    zip_ = armar()
    p.main(["apply", "--source", str(zip_), "--home", str(home)])
    assert (home / p.COPIA_PAQUETE).read_bytes() == zip_.read_bytes()


def test_report_manda_versiones_y_chequeos(falso, armar, home, monkeypatch):
    p.main(["apply", "--source", str(armar("0.4.0")), "--home", str(home)])
    falso.respuestas["/runtime/status"] = (200, {"ok": True}, {})
    monkeypatch.setattr(p, "entorno_del_gateway", lambda: {**VARS_DEL_PAQUETE, "TELEGRAM_ALLOW_ALL_USERS": "true"})

    assert p.main(["report", "--home", str(home)]) == 0

    metodo, _, headers, cuerpo = falso.recibidos("/runtime/status")[0]
    reporte = json.loads(cuerpo)
    assert metodo == "POST" and headers["Authorization"] == f"Bearer {TOKEN}"
    assert set(reporte) == {"id", "runtime", "runtime_version", "pack_version", "checks", "last_activity"}
    assert reporte["runtime"] == "hermes" and reporte["id"] == "runtime"
    assert reporte["runtime_version"] == "0.22.2-1" and reporte["pack_version"] == "0.4.0"
    niveles = {c["level"] for c in reporte["checks"]}
    assert niveles <= {"ok", "fail", "warn"} and "ok" in niveles
    assert all(1 <= len(c["text"]) <= 500 for c in reporte["checks"])


def test_report_avisa_si_el_paquete_no_se_aplico_y_si_no_hay_ninguno(falso, home):
    falso.respuestas["/runtime/status"] = (200, {"ok": True}, {})
    p.main(["report", "--home", str(home), "--aplicacion", "fallo"])
    reporte = json.loads(falso.recibidos("/runtime/status")[0][3])
    textos = [c["text"] for c in reporte["checks"] if c["level"] == "fail"]
    assert any("no se aplicó en este arranque" in t for t in textos) and any("no hay ningún paquete" in t for t in textos)
    assert reporte["pack_version"] is None


@pytest.mark.parametrize("codigo", [401, 409, 413, 500])
def test_un_reporte_rechazado_no_corta_nada(falso, home, capsys, codigo):
    falso.respuestas["/runtime/status"] = (codigo, {"ok": False}, {})
    assert p.main(["report", "--home", str(home)]) == 0
    assert "se reintenta" in capsys.readouterr().out


def test_sin_nucleo_no_se_reporta(sin_nucleo, home, capsys):
    assert p.main(["report", "--home", str(home)]) == 0
    assert "no se reporta" in capsys.readouterr().out


def test_el_id_de_la_instancia_sale_de_la_variable_y_se_valida(monkeypatch):
    monkeypatch.setenv("KIT_RUNTIME_ID", "branding")
    assert nucleo.armar_reporte("0.22.2-1", None, [])["id"] == "branding"
    monkeypatch.setenv("KIT_RUNTIME_ID", "Con Espacios!")
    assert nucleo.armar_reporte("0.22.2-1", None, [])["id"] == "runtime"


def test_el_reporte_respeta_los_limites_del_contrato():
    reporte = nucleo.armar_reporte("0.22.2-1", "v ersion", [("fail", "x" * 900)] * 250 + [("rara", "y"), ("ok", "  ")])
    assert reporte["pack_version"] is None
    assert len(reporte["checks"]) == 200 and len(reporte["checks"][0]["text"]) == 500


def test_ultima_actividad_es_el_ultimo_mensaje_de_una_persona_por_un_canal(home):
    con = sqlite3.connect(home / "state.db")
    con.executescript("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT);"
                      "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL);")
    con.executemany("INSERT INTO sessions VALUES (?, ?)", [("a", "telegram"), ("b", "tui")])
    con.executemany("INSERT INTO messages (session_id, role, timestamp) VALUES (?, ?, ?)",
                    [("a", "user", 1792000000.0), ("a", "assistant", 1792000500.0), ("b", "user", 1792009999.0)])
    con.commit()
    con.close()
    from datetime import datetime, timezone
    assert p.ultima_actividad(home) == datetime.fromtimestamp(1792000000, timezone.utc).isoformat()   # el de Telegram, no el de la TUI
    assert p.ultima_actividad(home / "no-existe") is None


# --- GET /runtime/pack

def _servir_paquete(falso, zip_: Path, sha=None, etag=None):
    datos = zip_.read_bytes()
    real = hashlib.sha256(datos).hexdigest()

    def responder(headers):
        if headers.get("If-None-Match") == f'"{real}"':
            return 304, b"", {}
        return 200, datos, {"X-Pack-Version": "0.9.0", "X-Pack-Sha256": sha or real, "ETag": etag or f'"{real}"',
                            "Content-Type": "application/zip"}
    falso.respuestas["/runtime/pack"] = (200, responder, {})
    return real


def test_baja_el_paquete_del_nucleo_verificando_su_huella(falso, armar, home, tmp_path):
    zip_ = armar()
    real = _servir_paquete(falso, zip_)
    salida = tmp_path / "bajado.zip"

    assert p.main(["pack-del-nucleo", "--home", str(home), "--out", str(salida)]) == p.SALIDA_NUEVO
    assert hashlib.sha256(salida.read_bytes()).hexdigest() == real
    assert p.main(["apply", "--source", str(salida), "--home", str(home)]) == 0     # la firma se verifica como siempre


def test_si_ya_esta_aplicado_el_nucleo_responde_304(falso, armar, home, tmp_path):
    zip_ = armar()
    _servir_paquete(falso, zip_)
    p.main(["apply", "--source", str(zip_), "--home", str(home)])

    assert p.main(["pack-del-nucleo", "--home", str(home), "--out", str(tmp_path / "x.zip")]) == p.SALIDA_SIN_CAMBIOS
    assert not (tmp_path / "x.zip").exists()


@pytest.mark.parametrize("sha, etag", [("0" * 64, None), (None, '"otro"'), ("no-es-sha", None)])
def test_un_paquete_que_no_coincide_con_sus_headers_no_se_guarda(falso, armar, home, tmp_path, sha, etag):
    _servir_paquete(falso, armar(), sha=sha, etag=etag)
    assert p.main(["pack-del-nucleo", "--home", str(home), "--out", str(tmp_path / "x.zip")]) == p.SALIDA_ERROR
    assert not (tmp_path / "x.zip").exists()


@pytest.mark.parametrize("respuesta, codigo", [((204, b"", {}), p.SALIDA_SIN_PAQUETE), ((500, {}, {}), p.SALIDA_ERROR),
                                               ((401, {}, {}), p.SALIDA_ERROR)])
def test_sin_paquete_activo_o_con_error(falso, home, tmp_path, respuesta, codigo):
    falso.respuestas["/runtime/pack"] = respuesta
    assert p.main(["pack-del-nucleo", "--home", str(home), "--out", str(tmp_path / "x.zip")]) == codigo


def test_pack_del_nucleo_sin_nucleo(sin_nucleo, home, tmp_path):
    assert p.main(["pack-del-nucleo", "--home", str(home), "--out", str(tmp_path / "x.zip")]) == p.SALIDA_SIN_NUCLEO


def test_un_paquete_del_nucleo_sin_firma_valida_se_rechaza_igual(falso, armar, home, tmp_path):
    import zipfile
    adulterado = tmp_path / "adulterado.zip"
    with zipfile.ZipFile(armar()) as zo, zipfile.ZipFile(adulterado, "w") as zd:
        for n in zo.namelist():
            zd.writestr(n, zo.read(n) + (b"x" if n == "SOUL.md" else b""))
    _servir_paquete(falso, adulterado)
    salida = tmp_path / "bajado.zip"
    assert p.main(["pack-del-nucleo", "--home", str(home), "--out", str(salida)]) == p.SALIDA_NUEVO
    with pytest.raises(SystemExit, match="dañado o fue modificado"):
        p.main(["apply", "--source", str(salida), "--home", str(home)])


# --- variables ${VAR} de la configuración

def test_variables_sin_definir(home):
    config = {"mcp_servers": {"n": {"url": "${URL}", "headers": {"Authorization": "Bearer ${env:TOKEN}"}}},
              "x": ["${vault:secreto}", "${EN_ENV}"]}
    escribir(home / ".env", "EN_ENV=1\nVACIA=\n")
    assert p.variables_sin_definir(config, {"URL": "u"}, home) == ["TOKEN"]
    assert p.variables_sin_definir(config, {"URL": "u", "TOKEN": "t"}, home) == []


def test_check_falla_si_la_config_usa_una_variable_que_el_gateway_no_tiene(origen, home, monkeypatch, capsys):
    p.comando_apply(origen, home, recargar=False)
    monkeypatch.setattr(p, "entorno_del_gateway", lambda: {"TELEGRAM_ALLOW_ALL_USERS": "true", "CORE_MCP_URL": "u"})

    assert p.comando_check(origen, home) == 1
    assert "${MCP_KIT_TOKEN} sin definir" in capsys.readouterr().out
