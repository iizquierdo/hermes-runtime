"""Los comandos que declara el paquete llegan al agente como texto; los de Hermes no se tocan."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


def _cargar(monkeypatch, tmp_path, comandos="newUser", entorno="", conocidos=("new", "pause", "help")):
    (tmp_path / "comandos-al-agente.json").write_text(json.dumps({"comandos": comandos}), encoding="utf-8")
    monkeypatch.setenv("KIT_TEXTOS_DIR", str(tmp_path))
    monkeypatch.delenv("IGA_COMANDOS_AL_AGENTE", raising=False)
    if entorno:
        monkeypatch.setenv("KIT_COMANDOS_AL_AGENTE", entorno)
    else:
        monkeypatch.delenv("KIT_COMANDOS_AL_AGENTE", raising=False)
    falso = types.ModuleType("hermes_cli.commands")
    falso.is_gateway_known_command = lambda n: n in conocidos
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.commands", falso)
    spec = importlib.util.spec_from_file_location("comandos_al_agente", Path(__file__).parent / "__init__.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _evento(texto):
    return {"event": types.SimpleNamespace(text=texto), "gateway": None, "session_store": None}


def test_newuser_llega_al_agente_como_texto(monkeypatch, tmp_path):
    m = _cargar(monkeypatch, tmp_path)
    assert m.al_despachar(**_evento("/newUser")) == {"action": "rewrite", "text": "Comando del usuario: /newUser"}
    assert m.al_despachar(**_evento("/newuser@mi_bot Ana Pérez")) == {"action": "rewrite",
                                                                       "text": "Comando del usuario: /newuser Ana Pérez"}


@pytest.mark.parametrize("texto", ["hola", "/otro", "newUser", "/newUserX", "", "texto /newUser"])
def test_lo_demas_no_se_toca(monkeypatch, tmp_path, texto):
    assert _cargar(monkeypatch, tmp_path).al_despachar(**_evento(texto)) is None


def test_un_comando_de_hermes_nunca_se_reescribe(monkeypatch, tmp_path):
    m = _cargar(monkeypatch, tmp_path, comandos="newUser,pause")
    assert m.al_despachar(**_evento("/pause")) is None      # sigue bajo el control de comandos de Hermes


def test_si_no_se_sabe_que_comandos_tiene_hermes_no_se_reescribe(monkeypatch, tmp_path):
    m = _cargar(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_cli.commands", None)       # import falla
    assert m.al_despachar(**_evento("/newUser")) is None


def test_sin_comandos_configurados_no_hace_nada(monkeypatch, tmp_path):
    m = _cargar(monkeypatch, tmp_path, comandos="")
    assert m.COMANDOS == frozenset() and m.al_despachar(**_evento("/newUser")) is None


def test_la_variable_suma_comandos_y_el_prefijo_es_configurable(monkeypatch, tmp_path):
    (tmp_path / "comandos-al-agente.json").write_text('{"comandos": "newUser", "prefijo": "La persona escribió:"}', encoding="utf-8")
    monkeypatch.setenv("KIT_TEXTOS_DIR", str(tmp_path))
    monkeypatch.setenv("KIT_COMANDOS_AL_AGENTE", "/Baja")
    spec = importlib.util.spec_from_file_location("comandos_al_agente_2", Path(__file__).parent / "__init__.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "es_de_hermes", lambda n: False)
    assert m.COMANDOS == {"newuser", "baja"}
    assert m.al_despachar(**_evento("/baja ya"))["text"] == "La persona escribió: /baja ya"


def test_registra_el_hook(monkeypatch, tmp_path):
    m = _cargar(monkeypatch, tmp_path)
    hooks = {}
    m.register(types.SimpleNamespace(register_hook=lambda n, f: hooks.setdefault(n, f)))
    assert hooks == {"pre_gateway_dispatch": m.al_despachar}
