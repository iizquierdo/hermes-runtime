"""El filtro de acceso corta sin LLM y falla cerrado. Sin Hermes real: fakes de `source` y un servidor HTTP local como núcleo."""
import importlib.util
import json
import threading
import time
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

TOKEN = "token-de-prueba"


class Nucleo:
    """Núcleo falso: `respuestas[(canal, id)] = (http, cuerpo)`; guarda los pedidos recibidos."""

    def __init__(self):
        self.respuestas, self.pedidos, self.demora = {}, [], 0
        nucleo = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                p = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(p.query))
                nucleo.pedidos.append((p.path, q, self.headers.get("Authorization")))
                time.sleep(nucleo.demora)
                codigo, cuerpo = nucleo.respuestas.get((q.get("channel"), q.get("id")), (200, {"status": "unregistered"}))
                datos = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
                try:
                    self.send_response(codigo)
                    self.end_headers()
                    self.wfile.write(datos)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.servidor = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.servidor.server_port}"
        threading.Thread(target=self.servidor.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def fijar(self, estado, canal="telegram", id_="111"):
        self.respuestas[(canal, id_)] = (200, {"status": estado})


@pytest.fixture
def nucleo():
    n = Nucleo()
    yield n
    n.servidor.shutdown()
    n.servidor.server_close()


@pytest.fixture
def plugin(monkeypatch, nucleo, tmp_path):
    monkeypatch.setenv("KIT_CORE_URL", nucleo.url)
    monkeypatch.setenv("KIT_CORE_TOKEN", TOKEN)
    for v in ("ACCESO_MAX_MENSAJES_NO_REGISTRADO", "ACCESO_CACHE_SEGUNDOS", "ACCESO_TIMEOUT_SEGUNDOS", "CORE_URL", "CORE_TOKEN"):
        monkeypatch.delenv(f"IGA_{v}", raising=False)
    for v in ("ACCESO_MAX_MENSAJES_NO_REGISTRADO", "ACCESO_CACHE_SEGUNDOS", "ACCESO_TIMEOUT_SEGUNDOS"):
        monkeypatch.delenv(f"KIT_{v}", raising=False)
    monkeypatch.setenv("KIT_TEXTOS_DIR", str(tmp_path / "sin-textos"))
    m = _cargar()
    m.reloj = types.SimpleNamespace(t=1000.0)
    m._ahora = lambda: m.reloj.t
    return m


def _cargar(nombre="nucleo_acceso_plugin"):
    spec = importlib.util.spec_from_file_location(nombre, Path(__file__).parent / "__init__.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _msg(usuario=111, plataforma="telegram", tipo="dm", chat=None):
    fuente = types.SimpleNamespace(platform=plataforma, user_id=usuario, chat_type=tipo, chat_id=usuario if chat is None else chat)
    return dict(session_key="S", platform=plataforma, source=fuente, message_id="1", text="hola")


def test_source_como_diccionario_como_en_el_hermes_real(plugin, nucleo):
    """T9: en Hermes `source` llega como dict; leerlo con getattr daba chat_type=None y cortaba en silencio."""
    nucleo.fijar("active")
    mensaje = _msg()
    mensaje["source"] = {"platform": "telegram", "user_id": 111, "chat_id": 111, "chat_type": "dm"}
    assert plugin.al_admitir(**mensaje) is None
    assert nucleo.pedidos[0][1] == {"channel": "telegram", "id": "111"}
    nucleo.fijar("pending", id_="222")
    otro = _msg(usuario=222)
    otro["source"] = {"platform": "telegram", "user_id": 222, "chat_id": 222, "chat_type": "dm"}
    assert plugin.al_admitir(**otro)["action"] == "handled"


def test_active_pasa_y_no_es_handled(plugin, nucleo):
    nucleo.fijar("active")
    assert plugin.al_admitir(**_msg()) is None
    assert nucleo.pedidos[0] == ("/identity/status", {"channel": "telegram", "id": "111"}, f"Bearer {TOKEN}")


def test_unregistered_pasa(plugin, nucleo):
    nucleo.fijar("unregistered")
    assert plugin.al_admitir(**_msg()) is None


def test_pending_corta_con_texto_fijo(plugin, nucleo):
    nucleo.fijar("pending")
    r = plugin.al_admitir(**_msg())
    assert r == {"action": "handled", "reply": plugin.TEXTO_PENDIENTE}
    assert "pendiente" in r["reply"]
    assert "te avisamos" not in r["reply"]  # nadie le avisa a la persona cuando la aprueban


@pytest.mark.parametrize("estado", ["suspended", "rejected"])
def test_suspendido_o_rechazado_corta(plugin, nucleo, estado):
    nucleo.fijar(estado)
    assert plugin.al_admitir(**_msg()) == {"action": "handled", "reply": plugin.TEXTO_NO_HABILITADO}


@pytest.mark.parametrize("codigo,cuerpo", [(401, {"error": "no_autorizado"}), (404, {"error": "not_found"}), (400, {"error": "x"}),
                                           (500, {}), (200, b"no es json"), (200, {"status": "inventado"}), (200, [1, 2]), (200, {})])
def test_falla_cerrado_ante_respuestas_malas(plugin, nucleo, codigo, cuerpo):
    nucleo.respuestas[("telegram", "111")] = (codigo, cuerpo)
    assert plugin.al_admitir(**_msg()) == {"action": "handled", "reply": plugin.TEXTO_NO_VERIFICABLE}


def test_falla_cerrado_por_timeout(plugin, nucleo, monkeypatch):
    monkeypatch.setenv("KIT_ACCESO_TIMEOUT_SEGUNDOS", "0.3")
    nucleo.demora = 1.0
    assert plugin.al_admitir(**_msg()) == {"action": "handled", "reply": plugin.TEXTO_NO_VERIFICABLE}


def test_falla_cerrado_si_el_nucleo_no_responde(plugin, monkeypatch):
    monkeypatch.setenv("KIT_CORE_URL", "http://127.0.0.1:1")
    assert plugin.al_admitir(**_msg())["reply"] == plugin.TEXTO_NO_VERIFICABLE


@pytest.mark.parametrize("falta", ["KIT_CORE_URL", "KIT_CORE_TOKEN"])
def test_falla_cerrado_sin_configurar(plugin, monkeypatch, falta):
    monkeypatch.delenv(falta)
    assert plugin.al_admitir(**_msg())["reply"] == plugin.TEXTO_NO_VERIFICABLE


def test_los_errores_no_se_cachean(plugin, nucleo):
    nucleo.respuestas[("telegram", "111")] = (500, {})
    assert plugin.al_admitir(**_msg())["reply"] == plugin.TEXTO_NO_VERIFICABLE
    nucleo.fijar("active")
    assert plugin.al_admitir(**_msg()) is None
    assert len(nucleo.pedidos) == 2


def test_cache_de_active_y_vence(plugin, nucleo):
    nucleo.fijar("active")
    plugin.al_admitir(**_msg())
    plugin.al_admitir(**_msg())
    assert len(nucleo.pedidos) == 1
    plugin.reloj.t += 31
    plugin.al_admitir(**_msg())
    assert len(nucleo.pedidos) == 2


def test_cache_de_pending(plugin, nucleo):
    nucleo.fijar("pending")
    plugin.al_admitir(**_msg())
    plugin.al_admitir(**_msg())
    assert len(nucleo.pedidos) == 1


@pytest.mark.parametrize("estado", ["unregistered", "suspended", "rejected"])
def test_no_se_cachean_los_demas_estados(plugin, nucleo, estado):
    nucleo.fijar(estado)
    plugin.al_admitir(**_msg())
    plugin.al_admitir(**_msg())
    assert len(nucleo.pedidos) == 2


def test_el_alta_se_nota_enseguida(plugin, nucleo):
    nucleo.fijar("unregistered")
    assert plugin.al_admitir(**_msg()) is None
    nucleo.fijar("pending")
    assert plugin.al_admitir(**_msg())["reply"] == plugin.TEXTO_PENDIENTE


def test_limite_de_no_registrados_ventana_deslizante(plugin, nucleo, monkeypatch):
    monkeypatch.setenv("KIT_ACCESO_MAX_MENSAJES_NO_REGISTRADO", "3")
    nucleo.fijar("unregistered")
    nucleo.fijar("unregistered", id_="222")
    assert [plugin.al_admitir(**_msg()) for _ in range(3)] == [None, None, None]
    assert plugin.al_admitir(**_msg()) == {"action": "handled", "reply": plugin.TEXTO_LIMITE}
    assert plugin.al_admitir(**_msg(222)) is None                   # el límite es por persona
    plugin.reloj.t += 3601
    assert plugin.al_admitir(**_msg()) is None                      # la ventana se desliza


def test_limite_por_defecto_30(plugin, nucleo):
    nucleo.fijar("unregistered")
    resultados = [plugin.al_admitir(**_msg()) for _ in range(31)]
    assert resultados[:30] == [None] * 30 and resultados[30]["action"] == "handled"


def test_active_no_cuenta_para_el_limite(plugin, nucleo, monkeypatch):
    monkeypatch.setenv("KIT_ACCESO_MAX_MENSAJES_NO_REGISTRADO", "1")
    nucleo.fijar("active")
    assert all(plugin.al_admitir(**_msg()) is None for _ in range(5))


@pytest.mark.parametrize("tipo", ["group", "channel", "thread", "supergroup"])
def test_grupos_corte_silencioso_sin_consultar(plugin, nucleo, tipo):
    assert plugin.al_admitir(**_msg(tipo=tipo)) == {"action": "handled"}
    assert nucleo.pedidos == []


def test_sin_chat_type_se_infiere_por_chat_id(plugin, nucleo):
    nucleo.fijar("active")
    assert plugin.al_admitir(**_msg(tipo=None)) is None
    assert plugin.al_admitir(**_msg(tipo=None, chat=-100123)) == {"action": "handled"}


def test_sin_usuario_no_puede_verificar(plugin, nucleo):
    assert plugin.al_admitir(**_msg(usuario=None, tipo="dm", chat=5))["reply"] == plugin.TEXTO_NO_VERIFICABLE
    assert plugin.al_admitir(**_msg(plataforma="Raro!"))["reply"] == plugin.TEXTO_NO_VERIFICABLE
    assert nucleo.pedidos == []


def test_sin_source_no_deja_pasar(plugin):
    assert plugin.al_admitir(session_key="S", text="hola")["action"] == "handled"


def test_plataforma_enum_y_mayusculas(plugin, nucleo):
    nucleo.fijar("active", canal="whatsapp")
    assert plugin.al_admitir(**_msg(plataforma=types.SimpleNamespace(value="WhatsApp"))) is None


def test_registra_el_hook(plugin):
    hooks = {}
    plugin.register(types.SimpleNamespace(register_hook=lambda n, f: hooks.setdefault(n, f)))
    assert hooks == {"post_gateway_admission": plugin.al_admitir}


# --- textos: neutros por defecto, reemplazables por el paquete del cliente

def test_los_textos_por_defecto_son_neutros_con_una_linea_en_portugues(plugin):
    assert plugin.TEXTO_PENDIENTE == plugin.TEXTOS_POR_DEFECTO["pendiente"]
    for texto in plugin.TEXTOS_POR_DEFECTO.values():
        assert "(PT:" in texto
        assert "probá" not in texto and "hablá" not in texto.lower() and "iga" not in texto.lower().split()


def test_el_paquete_reemplaza_los_textos(monkeypatch, tmp_path, nucleo):
    carpeta = tmp_path / "textos"
    carpeta.mkdir()
    (carpeta / "nucleo-acceso.json").write_text(json.dumps({"pendiente": "Esperá la aprobación.", "otra": "x", "limite": 3}),
                                                encoding="utf-8")
    monkeypatch.setenv("KIT_TEXTOS_DIR", str(carpeta))
    monkeypatch.setenv("KIT_CORE_URL", nucleo.url)
    monkeypatch.setenv("KIT_CORE_TOKEN", TOKEN)
    m = _cargar("nucleo_acceso_textos")
    nucleo.fijar("pending")

    assert m.al_admitir(**_msg()) == {"action": "handled", "reply": "Esperá la aprobación."}
    assert m.TEXTO_LIMITE == m.TEXTOS_POR_DEFECTO["limite"]          # un valor que no es texto conserva el de por defecto


def test_los_textos_se_buscan_en_hermes_home(monkeypatch, tmp_path):
    (tmp_path / "textos").mkdir()
    (tmp_path / "textos" / "nucleo-acceso.json").write_text('{"no_habilitado": "Sin acceso."}', encoding="utf-8")
    monkeypatch.delenv("KIT_TEXTOS_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert _cargar("nucleo_acceso_home").TEXTO_NO_HABILITADO == "Sin acceso."


@pytest.mark.parametrize("contenido", ["no es json", "[1, 2]"])
def test_un_archivo_de_textos_roto_no_rompe_el_plugin(monkeypatch, tmp_path, contenido):
    (tmp_path / "nucleo-acceso.json").write_text(contenido, encoding="utf-8")
    monkeypatch.setenv("KIT_TEXTOS_DIR", str(tmp_path))
    m = _cargar("nucleo_acceso_roto")
    assert m.TEXTO_PENDIENTE == m.TEXTOS_POR_DEFECTO["pendiente"]


# --- compatibilidad: si no está la KIT_*, vale la IGA_* del primer despliegue

def test_las_variables_iga_siguen_valiendo(plugin, nucleo, monkeypatch):
    monkeypatch.delenv("KIT_CORE_URL")
    monkeypatch.delenv("KIT_CORE_TOKEN")
    monkeypatch.setenv("IGA_CORE_URL", nucleo.url)
    monkeypatch.setenv("IGA_CORE_TOKEN", TOKEN)
    monkeypatch.setenv("IGA_ACCESO_MAX_MENSAJES_NO_REGISTRADO", "1")
    nucleo.fijar("unregistered")
    assert plugin.al_admitir(**_msg()) is None
    assert plugin.al_admitir(**_msg()) == {"action": "handled", "reply": plugin.TEXTO_LIMITE}
    assert nucleo.pedidos[0][2] == f"Bearer {TOKEN}"


def test_la_variable_kit_gana_sobre_la_iga(plugin, nucleo, monkeypatch):
    monkeypatch.setenv("IGA_CORE_URL", "http://127.0.0.1:1")      # no responde: si se usara, fallaría cerrado
    nucleo.fijar("active")
    assert plugin.al_admitir(**_msg()) is None
