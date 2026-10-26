"""Formato del paquete del agente (v2): armado declarativo, firma ed25519 y verificación.

El paquete lleva solo datos del cliente; la lógica que lo aplica vive en la imagen (`provision.py`). Es un zip con:

    manifest.json        {"formato": 2, "nombre", "version", "runtime": [restricciones], "creado",
                          "archivos": {ruta: sha256}, "retirar": {"plugins": [...]}}
    manifest.sig         {"algoritmo": "ed25519", "clave": <id de la clave>, "firma": <base64>}
    SOUL.md              identidad del bot
    SOUL.admin.md        identidad del perfil de administración (opcional)
    skills/<skill>/...   skills del cliente
    plugins/<plugin>/... plugins propios del cliente (no pueden llamarse como uno del runtime)
    textos/<plugin>.json textos de los plugins del runtime (opcional)
    config.json          configuración que se aplica con load_config()/save_config() de Hermes (opcional)

La firma es de los bytes exactos de `manifest.json`, y el manifiesto tiene la huella de cada archivo: con eso alcanza para
que nada del paquete se pueda cambiar sin que se note. La imagen trae las claves públicas en `kit-runtime/keys/*.pub` (PEM) y
rechaza, sin escribir nada, todo paquete que no esté firmado por una de ellas o que no sea para su versión.

Qué entra en el paquete lo dice un archivo declarativo de la carpeta del cliente (`agent-pack.toml`, ver
`kit-runtime/ejemplo/agent-pack.toml`); las rutas que declara son relativas a ese archivo.

Solo biblioteca estándar, salvo la firma: `cryptography` (está en las dependencias base de Hermes, así que la imagen la trae).
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

FORMATO = 2
MANIFIESTO = "manifest.json"
FIRMA = "manifest.sig"
ALGORITMO = "ed25519"
CONFIG = "config.json"
SOUL = "SOUL.md"
SOUL_ADMIN = "SOUL.admin.md"
CARPETA_SKILLS = "skills"
CARPETA_PLUGINS = "plugins"
CARPETA_TEXTOS = "textos"

# Límites para no descomprimir cualquier cosa antes de verificarla.
MAX_BYTES = 50 * 1024 * 1024
MAX_ARCHIVOS = 5000

NOMBRE_VALIDO = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
VERSION_PAQUETE = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_RUNTIME = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:-(\d+))?$")
RESTRICCION = re.compile(r"^(>=|<=|==|>|<)\s*(\S+)$")

IGNORAR_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache"}
IGNORAR_ARCHIVOS = {".DS_Store"}
SUFIJOS_TEXTO = {".md", ".py", ".yaml", ".yml", ".json", ".txt", ".sh", ".toml"}

# Los únicos toolsets de Hermes que puede tener un canal de mensajería. Todo lo demás se hace con las herramientas MCP del
# núcleo, que validan permisos y reglas: `web` podría leer URLs de la red privada, y las imágenes salen por el módulo, no por
# `image_gen`/`vision`/`tts`. Los canales locales (la consola de administración) no tienen esta restricción.
TOOLSETS_PERMITIDOS = ("skills", "clarify", "todo")
CANALES_LOCALES = ("cli", "tui", "desktop")
# Claves que fija el runtime: el paquete no las puede traer (ver CONFIG_POR_PERFIL en provision.py).
CLAVES_DEL_RUNTIME = ("memory.memory_enabled", "memory.user_profile_enabled",
                      "security.allow_private_urls", "security.allow_lazy_installs")
# Una clave con este nombre lleva un secreto: en el paquete solo puede ir como referencia `${VARIABLE}`.
CLAVE_SECRETA = re.compile(r"token|secret|password|passwd|api[_-]?key|authorization|credential", re.IGNORECASE)


def _bytes(ruta: Path) -> bytes:
    """Contenido del archivo; los de texto con fin de línea LF, así Windows y Linux comparan igual."""
    datos = ruta.read_bytes()
    return datos.replace(b"\r\n", b"\n") if ruta.suffix.lower() in SUFIJOS_TEXTO else datos


def _archivos(carpeta: Path) -> list[Path]:
    return sorted(
        p
        for p in carpeta.rglob("*")
        if p.is_file()
        and not (set(p.relative_to(carpeta).parts[:-1]) & IGNORAR_DIRS)
        and p.name not in IGNORAR_ARCHIVOS
        and p.suffix != ".pyc"
    )


# --- versiones del runtime: X.Y.Z-R (versión de Hermes y revisión del overlay)

def version_runtime(texto: str) -> tuple[int, int, int, int]:
    coincide = VERSION_RUNTIME.match(texto.strip())
    if not coincide:
        raise ValueError(f"versión de runtime inválida: {texto!r} (se espera X.Y, X.Y.Z o X.Y.Z-R)")
    return tuple(int(g or 0) for g in coincide.groups())  # type: ignore[return-value]


def _restriccion(texto: str) -> tuple[str, tuple[int, int, int, int]]:
    coincide = RESTRICCION.match(str(texto).strip())
    if not coincide:
        raise ValueError(f"restricción de runtime inválida: {texto!r} (por ejemplo \">=0.22.2-1\" o \"<0.23\")")
    return coincide.group(1), version_runtime(coincide.group(2))


def validar_restricciones(restricciones) -> list[str]:
    if not isinstance(restricciones, list) or not restricciones:
        raise ValueError("`runtime` tiene que ser una lista no vacía de restricciones, por ejemplo [\">=0.22.2-1\", \"<0.23\"]")
    for r in restricciones:
        _restriccion(r)
    return [str(r).strip() for r in restricciones]


def es_compatible(version: str, restricciones: list[str]) -> bool:
    actual = version_runtime(version)
    comparar = {">=": actual.__ge__, ">": actual.__gt__, "<=": actual.__le__, "<": actual.__lt__, "==": actual.__eq__}
    return all(comparar[op](otra) for op, otra in map(_restriccion, restricciones))


# --- claves y firma

def _cripto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as e:  # pragma: no cover - la imagen la trae (dependencia base de Hermes)
        raise SystemExit("Falta el paquete `cryptography`: sin él no se puede firmar ni verificar ningún paquete") from e
    return InvalidSignature, serialization, ed25519


def id_de_clave(publica) -> str:
    """Huella corta de la clave pública: los primeros 16 hex del sha256 de sus 32 bytes."""
    _, serialization, _ = _cripto()
    crudo = publica.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sha256(crudo).hexdigest()[:16]


def generar_claves(privada: Path, publica: Path) -> str:
    """Crea un par ed25519 (PEM). La privada nunca va a un repo: se guarda fuera y se usa solo para firmar."""
    _, serialization, ed25519 = _cripto()
    if privada.exists() or publica.exists():
        raise SystemExit("Ya existe la clave de destino; no se pisa")
    clave = ed25519.Ed25519PrivateKey.generate()
    privada.parent.mkdir(parents=True, exist_ok=True)
    publica.parent.mkdir(parents=True, exist_ok=True)
    privada.write_bytes(clave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    try:
        privada.chmod(0o600)
    except OSError:
        pass
    publica.write_bytes(clave.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return id_de_clave(clave.public_key())


def cargar_claves(carpeta: Path) -> dict[str, object]:
    """Las claves públicas de confianza (`*.pub`, PEM) por su id. Una que no se puede leer se ignora (y no sirve para verificar)."""
    _, serialization, ed25519 = _cripto()
    claves: dict[str, object] = {}
    for ruta in sorted(carpeta.glob("*.pub")) if carpeta.is_dir() else []:
        try:
            clave = serialization.load_pem_public_key(ruta.read_bytes())
        except ValueError:
            continue
        if isinstance(clave, ed25519.Ed25519PublicKey):
            claves[id_de_clave(clave)] = clave
    return claves


def firmar(manifiesto: bytes, ruta_privada: Path) -> bytes:
    _, serialization, ed25519 = _cripto()
    try:
        clave = serialization.load_pem_private_key(ruta_privada.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as e:
        raise SystemExit(f"No se pudo leer la clave privada {ruta_privada}: {type(e).__name__}") from e
    if not isinstance(clave, ed25519.Ed25519PrivateKey):
        raise SystemExit("La clave privada no es ed25519")
    firma = {
        "algoritmo": ALGORITMO,
        "clave": id_de_clave(clave.public_key()),
        "firma": base64.b64encode(clave.sign(manifiesto)).decode("ascii"),
    }
    return json.dumps(firma, indent=2).encode()


def verificar_firma(manifiesto: bytes, firma: bytes | None, claves: dict[str, object]) -> str:
    """Devuelve el id de la clave que firmó; SystemExit si la firma falta, no es válida o es de una clave que no conocemos."""
    invalida, _, _ = _cripto()
    if firma is None:
        raise SystemExit("El paquete no está firmado; no se aplicó nada")
    if not claves:
        raise SystemExit("La imagen no tiene ninguna clave pública para verificar paquetes (kit-runtime/keys/*.pub); no se aplicó nada")
    try:
        datos = json.loads(firma)
        assert isinstance(datos, dict) and datos.get("algoritmo") == ALGORITMO
        clave_id, valor = str(datos["clave"]), base64.b64decode(datos["firma"], validate=True)
    except (ValueError, KeyError, TypeError, AssertionError):
        raise SystemExit("La firma del paquete no se puede leer; no se aplicó nada")
    clave = claves.get(clave_id)
    if clave is None:
        raise SystemExit(f"El paquete está firmado con una clave que esta imagen no reconoce ({clave_id}); no se aplicó nada")
    try:
        clave.verify(valor, manifiesto)  # type: ignore[attr-defined]
    except invalida:
        raise SystemExit("La firma del paquete no es válida; no se aplicó nada")
    return clave_id


# --- configuración del paquete

def _hojas(datos: dict, prefijo: tuple[str, ...] = ()):
    for clave, valor in datos.items():
        ruta = prefijo + (str(clave),)
        if isinstance(valor, dict) and valor:
            yield from _hojas(valor, ruta)
        else:
            yield ruta, valor


def toolsets_no_permitidos(canal: str, toolsets) -> list[str]:
    """Los toolsets de un canal que no están permitidos (vacío para los canales locales)."""
    if str(canal).strip().lower() in CANALES_LOCALES:
        return []
    return [str(t) for t in (toolsets or []) if str(t) not in TOOLSETS_PERMITIDOS]


def problemas_de_config(datos) -> list[str]:
    """Lo que la configuración del paquete no puede traer (vacío = está bien)."""
    if not isinstance(datos, dict):
        return ["la configuración tiene que ser un objeto"]
    problemas: list[str] = []
    for ruta, valor in _hojas(datos):
        clave = ".".join(ruta)
        if clave in CLAVES_DEL_RUNTIME:
            problemas.append(f"{clave}: la fija el runtime, no el paquete")
        if CLAVE_SECRETA.search(ruta[-1]) and isinstance(valor, str) and valor.strip() and "${" not in valor:
            problemas.append(f"{clave}: parece un secreto; en el paquete solo puede ir como referencia ${{VARIABLE}}")
    canales = datos.get("platform_toolsets") or {}
    if not isinstance(canales, dict):
        problemas.append("platform_toolsets tiene que ser un objeto canal -> lista")
    else:
        for canal, toolsets in canales.items():
            sobran = toolsets_no_permitidos(canal, toolsets)
            if sobran:
                problemas.append(f"platform_toolsets.{canal}: en un canal de mensajería solo se permiten "
                                 f"{', '.join(TOOLSETS_PERMITIDOS)} (sobran: {', '.join(sobran)})")
    habilitados = (datos.get("plugins") or {}).get("enabled") if isinstance(datos.get("plugins"), dict) else None
    if habilitados is not None and not (isinstance(habilitados, list) and all(isinstance(n, str) for n in habilitados)):
        problemas.append("plugins.enabled tiene que ser una lista de nombres")
    return problemas


def _leer_config_fuente(ruta: Path) -> dict:
    texto = ruta.read_text(encoding="utf-8")
    if ruta.suffix.lower() == ".json":
        return json.loads(texto)
    if ruta.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            raise SystemExit(f"Para leer {ruta.name} hace falta PyYAML en la máquina que arma el paquete (o usar config.json)")
        return yaml.safe_load(texto) or {}
    raise SystemExit(f"La configuración del paquete tiene que ser .json, .yaml o .yml, no {ruta.name}")


# --- declaración y armado

@dataclass
class Declaracion:
    raiz: Path
    nombre: str
    runtime: list[str]
    soul: Path
    soul_admin: Path | None = None
    skills: list[Path] = field(default_factory=list)
    plugins: list[Path] = field(default_factory=list)
    textos: Path | None = None
    config: Path | None = None
    retirar_plugins: list[str] = field(default_factory=list)


def leer_declaracion(ruta: Path) -> Declaracion:
    try:
        datos = tomllib.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SystemExit(f"No se pudo leer la declaración del paquete {ruta}: {e}")
    raiz = ruta.parent
    contenido = datos.get("contenido") or {}

    def ruta_de(valor, que: str) -> Path:
        if not isinstance(valor, str) or not valor:
            raise SystemExit(f"{ruta.name}: `contenido.{que}` tiene que ser una ruta")
        destino = (raiz / valor).resolve()
        if not destino.exists():
            raise SystemExit(f"{ruta.name}: `contenido.{que}` no existe: {valor}")
        return destino

    def lista(valor, que: str) -> list:
        if valor is None:
            return []
        if not isinstance(valor, list):
            raise SystemExit(f"{ruta.name}: `{que}` tiene que ser una lista")
        return valor

    nombre = datos.get("nombre")
    if not isinstance(nombre, str) or not NOMBRE_VALIDO.match(nombre):
        raise SystemExit(f"{ruta.name}: `nombre` tiene que ser minúsculas, números y guiones (por ejemplo \"mi-cliente\")")
    try:
        runtime = validar_restricciones(datos.get("runtime"))
    except ValueError as e:
        raise SystemExit(f"{ruta.name}: {e}")
    retirar = lista((datos.get("retirar") or {}).get("plugins"), "retirar.plugins")
    if not all(isinstance(n, str) and NOMBRE_VALIDO.match(n) for n in retirar):
        raise SystemExit(f"{ruta.name}: `retirar.plugins` tiene que ser una lista de nombres de plugin")
    return Declaracion(
        raiz=raiz,
        nombre=nombre,
        runtime=runtime,
        soul=ruta_de(contenido.get("soul"), "soul"),
        soul_admin=ruta_de(contenido["soul_admin"], "soul_admin") if contenido.get("soul_admin") else None,
        skills=[ruta_de(v, "skills") for v in lista(contenido.get("skills"), "contenido.skills")],
        plugins=[ruta_de(v, "plugins") for v in lista(contenido.get("plugins"), "contenido.plugins")],
        textos=ruta_de(contenido["textos"], "textos") if contenido.get("textos") else None,
        config=ruta_de(contenido["config"], "config") if contenido.get("config") else None,
        retirar_plugins=list(retirar),
    )


def contenido_del_paquete(decl: Declaracion, plugins_del_runtime: frozenset[str] = frozenset()) -> dict[str, bytes]:
    """Ruta dentro del paquete -> bytes, ya normalizados. Valida lo que se puede validar antes de firmar."""
    contenido: dict[str, bytes] = {SOUL: _bytes(decl.soul)}
    if decl.soul_admin:
        contenido[SOUL_ADMIN] = _bytes(decl.soul_admin)

    vistos: set[str] = set()
    for carpeta in decl.skills:
        for skill in sorted(carpeta.glob("*/SKILL.md")):
            nombre = skill.parent.name
            if nombre in vistos:
                raise SystemExit(f"La skill {nombre} está repetida en el origen")
            vistos.add(nombre)
            for archivo in _archivos(skill.parent):
                contenido[f"{CARPETA_SKILLS}/{nombre}/{archivo.relative_to(skill.parent).as_posix()}"] = _bytes(archivo)

    propios: set[str] = set()
    for carpeta in decl.plugins:
        for manifiesto in sorted(carpeta.glob("*/plugin.yaml")):
            nombre = manifiesto.parent.name
            if nombre in plugins_del_runtime:
                raise SystemExit(f"El plugin {nombre} es del runtime: el paquete no puede traer uno con ese nombre")
            if nombre in propios:
                raise SystemExit(f"El plugin {nombre} está repetido en el origen")
            propios.add(nombre)
            for archivo in _archivos(manifiesto.parent):
                contenido[f"{CARPETA_PLUGINS}/{nombre}/{archivo.relative_to(manifiesto.parent).as_posix()}"] = _bytes(archivo)

    if decl.textos:
        for archivo in sorted(decl.textos.glob("*.json")):
            try:
                datos = json.loads(archivo.read_text(encoding="utf-8"))
            except ValueError:
                raise SystemExit(f"textos/{archivo.name} no es JSON válido")
            if not isinstance(datos, dict) or not all(isinstance(v, str) for v in datos.values()):
                raise SystemExit(f"textos/{archivo.name} tiene que ser un objeto clave -> texto")
            contenido[f"{CARPETA_TEXTOS}/{archivo.name}"] = _bytes(archivo)

    if decl.config:
        try:
            config = _leer_config_fuente(decl.config)
        except ValueError as e:
            raise SystemExit(f"La configuración del paquete no se puede leer: {e}")
        problemas = problemas_de_config(config)
        if problemas:
            raise SystemExit("La configuración del paquete no es válida:\n  " + "\n  ".join(problemas))
        contenido[CONFIG] = json.dumps(config, indent=2, ensure_ascii=False, sort_keys=True).encode()
    return contenido


def armar(decl: Declaracion, version: str, salida: Path, ruta_privada: Path,
          plugins_del_runtime: frozenset[str] = frozenset()) -> dict:
    if not VERSION_PAQUETE.match(version):
        raise SystemExit(f"--version espera el formato X.Y.Z (por ejemplo 0.7.0), no: {version}")
    contenido = contenido_del_paquete(decl, plugins_del_runtime)
    manifiesto = {
        "formato": FORMATO,
        "nombre": decl.nombre,
        "version": version,
        "runtime": decl.runtime,
        "creado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "retirar": {"plugins": decl.retirar_plugins},
        "archivos": {rel: sha256(datos).hexdigest() for rel, datos in sorted(contenido.items())},
    }
    crudo = json.dumps(manifiesto, indent=2, ensure_ascii=False).encode()
    firma = firmar(crudo, ruta_privada)
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, datos in sorted(contenido.items()):
            zf.writestr(rel, datos)
        zf.writestr(MANIFIESTO, crudo)
        zf.writestr(FIRMA, firma)
    return manifiesto


# --- apertura y verificación (lo que hace la imagen antes de tocar nada)

def huella_archivo(ruta: Path) -> str:
    h = sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _ruta_segura(nombre: str) -> bool:
    partes = nombre.split("/")
    return bool(nombre) and "\\" not in nombre and not nombre.startswith("/") and ":" not in partes[0] \
        and all(p not in ("", ".", "..") for p in partes)


def validar_manifiesto(datos, version_del_runtime: str) -> dict:
    if not isinstance(datos, dict) or not isinstance(datos.get("archivos"), dict) or not datos.get("version"):
        raise SystemExit("El manifiesto del paquete está incompleto; no se aplicó nada")
    if datos.get("formato") != FORMATO:
        raise SystemExit(f"El paquete es de formato {datos.get('formato')!r} y esta imagen aplica el formato {FORMATO}; no se aplicó nada")
    if not isinstance(datos.get("nombre"), str) or not NOMBRE_VALIDO.match(datos["nombre"]):
        raise SystemExit("El manifiesto no tiene un nombre de paquete válido; no se aplicó nada")
    try:
        restricciones = validar_restricciones(datos.get("runtime"))
        compatible = es_compatible(version_del_runtime, restricciones)
    except ValueError as e:
        raise SystemExit(f"El manifiesto declara mal las versiones compatibles ({e}); no se aplicó nada")
    if not compatible:
        raise SystemExit(f"El paquete {datos['version']} es para el runtime {' '.join(restricciones)} y esta imagen es "
                         f"{version_del_runtime}; no se aplicó nada")
    retirar = (datos.get("retirar") or {}).get("plugins", [])
    if not isinstance(retirar, list) or not all(isinstance(n, str) and NOMBRE_VALIDO.match(n) for n in retirar):
        raise SystemExit("El manifiesto tiene una lista de plugins a retirar inválida; no se aplicó nada")
    return datos


def abrir(zip_: Path, claves: dict[str, object], version_del_runtime: str,
          esperado: str = "") -> tuple[Path, dict, str, tempfile.TemporaryDirectory[str]]:
    """Verifica el paquete completo en memoria y recién entonces lo extrae a una carpeta temporal.

    Orden: huella del zip (si se pidió), firma del manifiesto, formato y versión del runtime, huella de cada archivo y que no
    sobre ni falte ninguno. Cualquier falla es un SystemExit y no se extrae nada. Devuelve (carpeta, manifiesto, id de la clave
    que firmó, carpeta temporal a limpiar).
    """
    if esperado and huella_archivo(zip_) != esperado.strip().lower():
        raise SystemExit("La huella (sha256) del paquete no coincide con la esperada; no se aplicó nada")
    try:
        zf = zipfile.ZipFile(zip_)
    except (OSError, zipfile.BadZipFile):
        raise SystemExit("El paquete no es un zip válido; no se aplicó nada")
    with zf:
        entradas = [i for i in zf.infolist() if not i.is_dir()]
        if len(entradas) > MAX_ARCHIVOS or sum(i.file_size for i in entradas) > MAX_BYTES:
            raise SystemExit("El paquete es demasiado grande; no se aplicó nada")
        nombres = [i.filename for i in entradas]
        malos = [n for n in nombres if not _ruta_segura(n)]
        if malos or len(set(nombres)) != len(nombres):
            raise SystemExit(f"El paquete tiene rutas inválidas o repetidas ({', '.join(malos[:3]) or 'repetidas'}); no se aplicó nada")
        if MANIFIESTO not in nombres:
            raise SystemExit("El paquete no tiene manifest.json; no se aplicó nada")
        crudo = zf.read(MANIFIESTO)
        clave_id = verificar_firma(crudo, zf.read(FIRMA) if FIRMA in nombres else None, claves)
        try:
            manifiesto = validar_manifiesto(json.loads(crudo), version_del_runtime)
        except ValueError:
            raise SystemExit("El manifiesto del paquete no se puede leer; no se aplicó nada")
        esperados: dict[str, str] = manifiesto["archivos"]
        presentes = set(nombres) - {MANIFIESTO, FIRMA}
        for rel in sorted(set(esperados) | presentes):
            if rel not in presentes or rel not in esperados or sha256(zf.read(rel)).hexdigest() != esperados[rel]:
                raise SystemExit(f"El paquete está dañado o fue modificado ({rel}); no se aplicó nada")
        if SOUL not in presentes:
            raise SystemExit("El paquete no trae SOUL.md; no se aplicó nada")
        if CONFIG in presentes:
            try:
                problemas = problemas_de_config(json.loads(zf.read(CONFIG)))
            except ValueError:
                problemas = ["config.json no es JSON válido"]
            if problemas:
                raise SystemExit("La configuración del paquete no es válida (" + "; ".join(problemas) + "); no se aplicó nada")
        tmp = tempfile.TemporaryDirectory(prefix="agent-pack-")
        try:
            for rel in sorted(presentes):
                destino = Path(tmp.name) / rel
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(zf.read(rel))
        except OSError:
            tmp.cleanup()
            raise SystemExit("No se pudo extraer el paquete a una carpeta temporal; no se aplicó nada")
    return Path(tmp.name), manifiesto, clave_id, tmp
