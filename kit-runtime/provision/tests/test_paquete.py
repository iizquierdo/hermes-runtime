"""Formato v2 y firma: todo paquete sin firma válida, alterado o para otro runtime se rechaza sin escribir nada."""
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import paquete
import provision as p
from conftest import escribir


def _copiar_zip(origen: Path, destino: Path, cambiar=None, agregar: dict | None = None, quitar: tuple = ()) -> Path:
    with zipfile.ZipFile(origen) as zo, zipfile.ZipFile(destino, "w") as zd:
        for nombre in zo.namelist():
            if nombre in quitar:
                continue
            datos = zo.read(nombre)
            zd.writestr(nombre, cambiar(nombre, datos) if cambiar else datos)
        for nombre, datos in (agregar or {}).items():
            info = zipfile.ZipInfo("x")
            info.filename = nombre  # sin la normalización de ZipInfo (en Windows cambia "\\" por "/")
            zd.writestr(info, datos)
    return destino


def _rechazado(zip_: Path, home: Path, mensaje: str) -> None:
    with pytest.raises(SystemExit, match=mensaje):
        p.main(["apply", "--source", str(zip_), "--home", str(home)])
    assert list(home.iterdir()) == []  # no se escribió nada


def test_el_paquete_lleva_manifiesto_firmado_con_la_huella_de_cada_archivo(armar):
    with zipfile.ZipFile(armar()) as zf:
        manifiesto = json.loads(zf.read("manifest.json"))
        firma = json.loads(zf.read("manifest.sig"))
        assert manifiesto["formato"] == 2 and manifiesto["version"] == "1.2.3" and manifiesto["nombre"] == "cliente"
        assert manifiesto["runtime"] == [">=0.22.2-1", "<0.23"]
        assert manifiesto["retirar"] == {"plugins": ["viejo-diag"]}
        assert set(manifiesto["archivos"]) == set(zf.namelist()) - {"manifest.json", "manifest.sig"}
        for rel, huella in manifiesto["archivos"].items():
            assert hashlib.sha256(zf.read(rel)).hexdigest() == huella
        assert firma["algoritmo"] == "ed25519" and len(firma["clave"]) == 16


def test_el_paquete_solo_lleva_datos_del_cliente(armar):
    nombres = set(zipfile.ZipFile(armar()).namelist())
    assert {"SOUL.md", "SOUL.admin.md", "config.json", "textos/nucleo-acceso.json", "skills/cli-alta/SKILL.md",
            "skills/cli-alta/references/x.md", "skills/cli-marca/SKILL.md", "plugins/cli-extra/plugin.yaml"} <= nombres
    assert not [n for n in nombres if n.endswith((".sh", "provision.py", ".pyc")) or "nucleo-acceso/" in n]


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "latest", ""])
def test_la_version_del_paquete_tiene_formato_x_y_z(fuente, clave, tmp_path, version):
    decl = paquete.leer_declaracion(fuente / "agent-pack.toml")
    with pytest.raises(SystemExit, match="X.Y.Z"):
        paquete.armar(decl, version, tmp_path / "x.zip", clave)


def test_un_paquete_sin_firma_se_rechaza(armar, home, tmp_path):
    _rechazado(_copiar_zip(armar(), tmp_path / "sin-firma.zip", quitar=("manifest.sig",)), home, "no está firmado")


def test_un_paquete_firmado_con_otra_clave_se_rechaza(fuente, home, tmp_path):
    ajena = tmp_path / "ajena" / "otra.pem"
    paquete.generar_claves(ajena, tmp_path / "ajena" / "otra.pub")  # la pública NO va a las claves de la imagen
    paquete.generar_claves(tmp_path / "x" / "x.pem", p.CARPETA_CLAVES / "prueba.pub")
    zip_ = tmp_path / "ajeno.zip"
    paquete.armar(paquete.leer_declaracion(fuente / "agent-pack.toml"), "1.0.0", zip_, ajena)

    _rechazado(zip_, home, "clave que esta imagen no reconoce")


def test_sin_claves_en_la_imagen_ningun_paquete_se_aplica(armar, home):
    zip_ = armar()
    for clave in p.CARPETA_CLAVES.glob("*.pub"):
        clave.unlink()
    _rechazado(zip_, home, "no tiene ninguna clave pública")


def test_un_manifiesto_modificado_invalida_la_firma(armar, home, tmp_path):
    def cambiar(nombre, datos):
        if nombre != "manifest.json":
            return datos
        manifiesto = json.loads(datos)
        manifiesto["version"] = "9.9.9"
        return json.dumps(manifiesto, indent=2).encode()

    _rechazado(_copiar_zip(armar(), tmp_path / "adulterado.zip", cambiar), home, "firma del paquete no es válida")


def test_una_firma_ilegible_se_rechaza(armar, home, tmp_path):
    zip_ = _copiar_zip(armar(), tmp_path / "rota.zip", lambda n, d: b"no es json" if n == "manifest.sig" else d)
    _rechazado(zip_, home, "firma del paquete no se puede leer")


def test_un_archivo_modificado_no_se_aplica(armar, home, tmp_path):
    zip_ = _copiar_zip(armar(), tmp_path / "adulterado.zip",
                       lambda n, d: d + b"\n# agregado a mano" if n == "SOUL.md" else d)
    _rechazado(zip_, home, r"dañado o fue modificado \(SOUL.md\)")


def test_un_archivo_de_mas_en_el_paquete_tambien_se_rechaza(armar, home, tmp_path):
    zip_ = _copiar_zip(armar(), tmp_path / "extra.zip", agregar={"plugins/intruso/plugin.yaml": "name: intruso\n"})
    _rechazado(zip_, home, "plugins/intruso")


def test_un_archivo_de_menos_tambien_se_rechaza(armar, home, tmp_path):
    _rechazado(_copiar_zip(armar(), tmp_path / "falta.zip", quitar=("skills/cli-marca/SKILL.md",)), home, "skills/cli-marca")


@pytest.mark.parametrize("ruta", ["../fuera.txt", "/absoluta.txt", "skills\\x\\SKILL.md", "C:/x.txt", "a//b.txt"])
def test_rutas_peligrosas_se_rechazan_antes_de_extraer(armar, home, tmp_path, ruta):
    # En Windows zipfile ya convierte "\\" en "/" al leer: ahí la ruta es segura y se rechaza por no estar en el manifiesto.
    _rechazado(_copiar_zip(armar(), tmp_path / "ruta.zip", agregar={ruta: "x"}), home, "rutas inválidas|dañado o fue modificado")


def test_un_zip_roto_se_rechaza(home, tmp_path):
    roto = tmp_path / "roto.zip"
    roto.write_bytes(b"no es un zip")
    _rechazado(roto, home, "no es un zip válido")


def test_un_paquete_demasiado_grande_se_rechaza(armar, home, tmp_path, monkeypatch):
    monkeypatch.setattr(paquete, "MAX_BYTES", 10)
    _rechazado(armar(), home, "demasiado grande")


@pytest.mark.parametrize("version_runtime, compatible", [
    ("0.22.2-1", True), ("0.22.9-4", True), ("0.22.2-0", False), ("0.22.1-9", False), ("0.23.0-1", False),
])
def test_el_paquete_solo_se_aplica_en_los_runtimes_que_declara(armar, home, monkeypatch, version_runtime, compatible):
    zip_ = armar()
    monkeypatch.setattr(p, "version_del_runtime", lambda: version_runtime)
    if compatible:
        assert p.main(["apply", "--source", str(zip_), "--home", str(home)]) == 0
    else:
        _rechazado(zip_, home, f"esta imagen es {version_runtime}")


def test_un_paquete_de_formato_1_se_rechaza(clave, home, tmp_path):
    """El paquete de la primera versión (sin firma, con provision.py adentro) no se aplica en esta imagen."""
    archivos = {"SOUL.md": b"# x\n"}
    manifiesto = json.dumps({"nombre": "agent-pack", "version": "0.1.0", "archivos": {
        k: hashlib.sha256(v).hexdigest() for k, v in archivos.items()}}).encode()
    zip_ = tmp_path / "v1.zip"
    with zipfile.ZipFile(zip_, "w") as zf:
        zf.writestr("SOUL.md", archivos["SOUL.md"])
        zf.writestr("manifest.json", manifiesto)
        zf.writestr("manifest.sig", paquete.firmar(manifiesto, clave))
    _rechazado(zip_, home, "formato None")


def test_la_configuracion_del_paquete_se_vuelve_a_validar_en_la_imagen(clave, home, tmp_path):
    """Aunque esté bien firmado, un paquete con una configuración prohibida no se aplica."""
    archivos = {"SOUL.md": b"# x\n", "config.json": json.dumps({"platform_toolsets": {"telegram": ["terminal"]}}).encode()}
    manifiesto = json.dumps({"formato": 2, "nombre": "cliente", "version": "1.0.0", "runtime": [">=0.22"],
                             "archivos": {k: hashlib.sha256(v).hexdigest() for k, v in archivos.items()}}).encode()
    zip_ = tmp_path / "config-mala.zip"
    with zipfile.ZipFile(zip_, "w") as zf:
        for nombre, datos in archivos.items():
            zf.writestr(nombre, datos)
        zf.writestr("manifest.json", manifiesto)
        zf.writestr("manifest.sig", paquete.firmar(manifiesto, clave))
    _rechazado(zip_, home, "solo se permiten")


# --- la configuración del paquete

@pytest.mark.parametrize("config, mensaje", [
    ({"mcp_servers": {"nucleo": {"headers": {"Authorization": "Bearer abc123"}}}}, "parece un secreto"),
    ({"providers": {"x": {"api_key": "sk-real"}}}, "parece un secreto"),
    ({"platform_toolsets": {"telegram": ["web", "terminal"]}}, "solo se permiten skills, clarify, todo"),
    ({"platform_toolsets": {"discord": ["skills", "vision"]}}, r"sobran: vision"),
    ({"platform_toolsets": {"telegram": ["image_gen", "tts"]}}, r"sobran: image_gen, tts"),
    ({"memory": {"memory_enabled": True}}, "la fija el runtime"),
    ({"security": {"allow_private_urls": True}}, "la fija el runtime"),
    ({"security": {"allow_lazy_installs": True}}, "la fija el runtime"),
    ({"plugins": {"enabled": "todos"}}, "lista de nombres"),
])
def test_bundle_rechaza_una_configuracion_prohibida(fuente, clave, tmp_path, config, mensaje):
    escribir(fuente / "config.json", json.dumps(config))
    with pytest.raises(SystemExit, match=mensaje):
        paquete.armar(paquete.leer_declaracion(fuente / "agent-pack.toml"), "1.0.0", tmp_path / "x.zip", clave)
    assert not (tmp_path / "x.zip").exists()


def test_los_secretos_como_referencia_a_variables_si_se_aceptan():
    assert paquete.problemas_de_config({"mcp_servers": {"n": {"headers": {"Authorization": "Bearer ${MCP_KIT_TOKEN}"}}}}) == []


def test_la_configuracion_puede_escribirse_en_yaml(fuente, clave, tmp_path):
    # PyYAML es opcional en la máquina que arma (con config.json no hace falta), pero estos tests lo requieren
    escribir(fuente / "config.yaml", "session_reset:\n  mode: idle\n  idle_minutes: 60\n")
    toml = fuente / "agent-pack.toml"
    toml.write_text(toml.read_text().replace('config = "config.json"', 'config = "config.yaml"'))
    zip_ = tmp_path / "yaml.zip"
    paquete.armar(paquete.leer_declaracion(toml), "1.0.0", zip_, clave)
    assert json.loads(zipfile.ZipFile(zip_).read("config.json")) == {"session_reset": {"mode": "idle", "idle_minutes": 60}}


@pytest.mark.parametrize("texto, mensaje", [
    ('runtime = [">=0.22"]\n[contenido]\nsoul = "SOUL.md"\n', "nombre"),
    ('nombre = "Mal Nombre"\nruntime = [">=0.22"]\n[contenido]\nsoul = "SOUL.md"\n', "nombre"),
    ('nombre = "x-y"\n[contenido]\nsoul = "SOUL.md"\n', "runtime"),
    ('nombre = "x-y"\nruntime = ["~0.22"]\n[contenido]\nsoul = "SOUL.md"\n', "restricción"),
    ('nombre = "x-y"\nruntime = [">=0.22"]\n[contenido]\nsoul = "SOUL.md"\nskills = ["no-existe"]\n', "no existe"),
    ('nombre = "x-y"\nruntime = [">=0.22"]\n[contenido]\nsoul = "SOUL.md"\n[retirar]\nplugins = ["../x"]\n', "retirar"),
])
def test_la_declaracion_se_valida(fuente, texto, mensaje):
    (fuente / "agent-pack.toml").write_text(texto)
    with pytest.raises(SystemExit, match=mensaje):
        paquete.leer_declaracion(fuente / "agent-pack.toml")


def test_skills_repetidas_entre_carpetas_se_rechazan(fuente, clave, tmp_path):
    escribir(fuente / "compartidas" / "cli-alta" / "SKILL.md", "otra\n")
    with pytest.raises(SystemExit, match="repetida"):
        paquete.armar(paquete.leer_declaracion(fuente / "agent-pack.toml"), "1.0.0", tmp_path / "x.zip", clave)


def test_textos_que_no_son_un_objeto_de_textos_se_rechazan(fuente, clave, tmp_path):
    escribir(fuente / "textos" / "solo-lectura.json", '{"bloqueo": 3}')
    with pytest.raises(SystemExit, match="clave -> texto"):
        paquete.armar(paquete.leer_declaracion(fuente / "agent-pack.toml"), "1.0.0", tmp_path / "x.zip", clave)


# --- versiones y claves

@pytest.mark.parametrize("version, restricciones, esperado", [
    ("0.22.2-1", [">=0.22.2-1", "<0.23"], True),
    ("0.22.2", [">=0.22.2-1"], False),
    ("0.22.10-1", [">0.22.9"], True),
    ("1.0.0-2", ["==1.0.0-2"], True),
    ("1.0.0-2", ["<=1.0.0-1"], False),
])
def test_compatibilidad_de_versiones(version, restricciones, esperado):
    assert paquete.es_compatible(version, restricciones) is esperado


@pytest.mark.parametrize("malo", [[], ">=0.22", ["0.22"], [">=latest"]])
def test_restricciones_invalidas(malo):
    with pytest.raises(ValueError):
        paquete.validar_restricciones(malo)


def test_keygen_no_pisa_una_clave_existente(tmp_path):
    privada, publica = tmp_path / "k.pem", tmp_path / "k.pub"
    assert p.main(["keygen", "--privada", str(privada), "--publica", str(publica)]) == 0
    assert b"PRIVATE KEY" in privada.read_bytes() and b"PUBLIC KEY" in publica.read_bytes()
    with pytest.raises(SystemExit, match="no se pisa"):
        p.main(["keygen", "--privada", str(privada), "--publica", str(tmp_path / "otra.pub")])


def test_las_claves_de_la_imagen_que_no_se_pueden_leer_se_ignoran(tmp_path):
    (tmp_path / "rota.pub").write_text("no es una clave")
    paquete.generar_claves(tmp_path / "privada.pem", tmp_path / "buena.pub")
    assert len(paquete.cargar_claves(tmp_path)) == 1


def test_el_repo_no_tiene_claves_privadas():
    raiz = Path(__file__).resolve().parents[2]
    privadas = [r for r in raiz.rglob("*") if r.is_file() and r.suffix in (".pem", ".pub", ".key")
                and b"PRIVATE KEY" in r.read_bytes()]
    assert privadas == []


def test_el_paquete_de_ejemplo_se_arma_y_se_aplica(clave, home, tmp_path, monkeypatch):
    ejemplo = Path(__file__).resolve().parents[2] / "ejemplo"
    monkeypatch.setattr(p, "CARPETA_PLUGINS_RUNTIME", Path(__file__).resolve().parents[2] / "plugins")  # los plugins reales
    zip_ = tmp_path / "ejemplo.zip"
    assert p.main(["bundle", "--decl", str(ejemplo / "agent-pack.toml"), "--out", str(zip_), "--version", "0.1.0",
                   "--clave-privada", str(clave)]) == 0

    assert p.main(["apply", "--source", str(zip_), "--home", str(home)]) == 0
    assert (home / "skills/ejemplo/consulta-basica/SKILL.md").exists()
    assert {d.name for d in (home / "plugins").iterdir()} == {"nucleo-acceso", "nucleo-identidad", "solo-lectura", "comandos-al-agente",
                                                              "hermes-session-reset-policy"}



def test_un_canal_local_puede_tener_cualquier_toolset_en_el_paquete():
    assert paquete.problemas_de_config({"platform_toolsets": {"cli": ["terminal", "web"], "telegram": ["todo"]}}) == []
