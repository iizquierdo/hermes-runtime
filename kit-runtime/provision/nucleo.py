"""Cliente del plano de control del núcleo (ver `mcp-kit/docs/runtime-control-plane.md`). Solo biblioteca estándar.

    GET  /runtime/config   administradores (`<canal>:<id>`) y comandos permitidos a quien no es administrador
    POST /runtime/status   versión del runtime y del paquete, chequeos de `check` y última actividad
    GET  /runtime/pack     el paquete activo: 200 zip (X-Pack-Version, X-Pack-Sha256, ETag "<sha256>"), 304, 204

Todo con el token de runtime (`Authorization: Bearer`). Regla del contrato: cualquier respuesta que no sea la esperada, un
error de red o un timeout es "no sé": nunca "todo permitido", y nunca corta el arranque. Las funciones no lanzan
excepciones por eso: devuelven None / False / un resultado con el motivo, y quien llama decide y lo deja en el log.

Configuración (variables de entorno; si una `KIT_*` no está, se lee la `IGA_*` del mismo nombre, por compatibilidad):
    KIT_CORE_URL, KIT_CORE_TOKEN   URL base del núcleo y token del runtime (sin las dos, no se habla con el núcleo)
    KIT_RUNTIME_ID                 id de esta instancia en el panel (por defecto `runtime`, como el núcleo)
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

TIMEOUT_SEGUNDOS = 5.0
TIMEOUT_PAQUETE_SEGUNDOS = 120.0
MAX_CONFIG_BYTES = 256 * 1024
MAX_PAQUETE_BYTES = 50 * 1024 * 1024
MAX_CHEQUEOS = 200
MAX_TEXTO = 500

IDENTIDAD = re.compile(r"^[a-z][a-z0-9_-]{1,20}:[^\s:]{1,128}$")
COMANDO = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ID_RUNTIME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
VERSION = re.compile(r"^[\w.+-]{1,64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _env(nombre: str, defecto: str = "") -> str:
    return os.environ.get(f"KIT_{nombre}") or os.environ.get(f"IGA_{nombre}") or defecto


@dataclass(frozen=True)
class Nucleo:
    url: str
    token: str

    @classmethod
    def del_entorno(cls) -> "Nucleo | None":
        """El núcleo configurado, o None si falta la URL o el token (o la URL no es http/https)."""
        url, token = _env("CORE_URL").rstrip("/"), _env("CORE_TOKEN")
        if not url or not token or not url.lower().startswith(("http://", "https://")):
            return None
        return cls(url, token)

    def _pedido(self, ruta: str, metodo: str = "GET", cuerpo: bytes | None = None, headers: dict | None = None):
        encabezados = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", **(headers or {})}
        if cuerpo is not None:
            encabezados["Content-Type"] = "application/json"
        return urllib.request.Request(f"{self.url}{ruta}", data=cuerpo, method=metodo, headers=encabezados)


@dataclass(frozen=True)
class ConfigDelNucleo:
    admins: tuple[str, ...]            # identidades `<canal>:<id>`
    comandos: tuple[str, ...]

    def admins_de(self, canal: str) -> tuple[str, ...]:
        """Los ids de un canal (`telegram:123` -> `123`), en el orden en que vinieron."""
        prefijo = f"{canal}:"
        return tuple(a[len(prefijo):] for a in self.admins if a.startswith(prefijo))


def pedir_config(nucleo: Nucleo, timeout: float = TIMEOUT_SEGUNDOS) -> tuple[ConfigDelNucleo | None, str]:
    """(config, "") si el núcleo respondió bien; (None, motivo) ante cualquier otra cosa."""
    try:
        with urllib.request.urlopen(nucleo._pedido("/runtime/config"), timeout=timeout) as r:
            if r.status != 200:
                return None, f"respondió {r.status}"
            datos = json.loads(r.read(MAX_CONFIG_BYTES + 1)[:MAX_CONFIG_BYTES].decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, f"respondió {e.code}"
    except (OSError, ValueError) as e:
        return None, f"no respondió bien ({type(e).__name__})"
    if not isinstance(datos, dict):
        return None, "la respuesta no es un objeto"
    admins, comandos = datos.get("admins"), datos.get("allowed_commands")
    if not isinstance(admins, list) or not all(isinstance(a, str) and IDENTIDAD.match(a) for a in admins):
        return None, "`admins` no es una lista de identidades <canal>:<id>"
    if not isinstance(comandos, list) or not all(isinstance(c, str) and COMANDO.match(c) for c in comandos):
        return None, "`allowed_commands` no es una lista de comandos"
    return ConfigDelNucleo(tuple(admins), tuple(comandos)), ""


def armar_reporte(runtime_version: str, pack_version: str | None, chequeos: list[tuple[str, str]],
                  ultima_actividad: str | None = None, id_: str | None = None) -> dict:
    """El cuerpo de POST /runtime/status, ya recortado a los límites del contrato."""
    id_ = (id_ or _env("RUNTIME_ID", "runtime")).strip().lower()
    reporte = {
        "id": id_ if ID_RUNTIME.match(id_) else "runtime",
        "runtime": "hermes",
        "runtime_version": runtime_version if VERSION.match(runtime_version or "") else "desconocida",
        "pack_version": pack_version if pack_version and VERSION.match(pack_version) else None,
        "checks": [{"level": nivel, "text": texto.strip()[:MAX_TEXTO]}
                   for nivel, texto in chequeos if nivel in ("ok", "fail", "warn") and texto.strip()][:MAX_CHEQUEOS],
        "last_activity": ultima_actividad,
    }
    return reporte


def reportar(nucleo: Nucleo, reporte: dict, timeout: float = TIMEOUT_SEGUNDOS) -> tuple[bool, str]:
    cuerpo = json.dumps(reporte, ensure_ascii=False).encode("utf-8")
    try:
        with urllib.request.urlopen(nucleo._pedido("/runtime/status", "POST", cuerpo), timeout=timeout) as r:
            return (r.status == 200), (f"respondió {r.status}" if r.status != 200 else "")
    except urllib.error.HTTPError as e:
        return False, f"respondió {e.code}"
    except OSError as e:
        return False, f"no respondió ({type(e).__name__})"


NUEVO, SIN_CAMBIOS, SIN_PAQUETE, ERROR = "nuevo", "sin-cambios", "sin-paquete", "error"


@dataclass(frozen=True)
class Descarga:
    resultado: str                      # NUEVO | SIN_CAMBIOS | SIN_PAQUETE | ERROR
    version: str = ""
    sha256: str = ""
    motivo: str = ""


def bajar_paquete(nucleo: Nucleo, destino: Path, sha_actual: str = "", timeout: float = TIMEOUT_PAQUETE_SEGUNDOS) -> Descarga:
    """GET /runtime/pack. Solo deja el zip en `destino` si llegó completo y su sha256 coincide con X-Pack-Sha256 y el ETag.

    La firma no se mira acá: la verifica `provision.py apply` como a cualquier otro paquete.
    """
    headers = {"Accept": "application/zip"}
    if sha_actual:
        headers["If-None-Match"] = f'"{sha_actual}"'
    try:
        with urllib.request.urlopen(nucleo._pedido("/runtime/pack", headers=headers), timeout=timeout) as r:
            if r.status == 204:
                return Descarga(SIN_PAQUETE)
            if r.status != 200:
                return Descarga(ERROR, motivo=f"respondió {r.status}")
            version = (r.headers.get("X-Pack-Version") or "").strip()
            esperado = (r.headers.get("X-Pack-Sha256") or "").strip().lower()
            etag = (r.headers.get("ETag") or "").strip()
            datos = r.read(MAX_PAQUETE_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return Descarga(SIN_CAMBIOS, sha256=sha_actual)
        return Descarga(ERROR, motivo=f"respondió {e.code}")
    except OSError as e:
        return Descarga(ERROR, motivo=f"no respondió ({type(e).__name__})")
    if len(datos) > MAX_PAQUETE_BYTES:
        return Descarga(ERROR, motivo="el paquete es demasiado grande")
    if not SHA256.match(esperado) or etag not in (f'"{esperado}"', f'W/"{esperado}"'):
        return Descarga(ERROR, motivo="faltan o no coinciden X-Pack-Sha256 y ETag")
    real = sha256(datos).hexdigest()
    if real != esperado:
        return Descarga(ERROR, motivo="la huella del paquete descargado no coincide con X-Pack-Sha256")
    if not VERSION.match(version):
        return Descarga(ERROR, motivo="falta X-Pack-Version")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(datos)
    return Descarga(NUEVO, version=version, sha256=real)
