# hermes-runtime: Hermes oficial + overlay genérico

Este repo es una copia **privada** de [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (licencia MIT, ver `LICENSE`),
fijada a **v0.22.2** (remoto `upstream`). No es un fork de GitHub a propósito: un fork de un repo público es público. La imagen que
publica sí es pública (`ghcr.io/<owner>/hermes-runtime:<hermes>-<revisión>`), así que **no lleva nada de ningún cliente ni nada secreto**.

Todo el árbol de Hermes está sin tocar, salvo un bloque delimitado al final del `Dockerfile`. Lo propio vive en `kit-runtime/`
(no en `runtime/` ni en `hermes-runtime/`: el `.dockerignore` oficial excluye `runtime/` del contexto de build y el `.gitignore` oficial ignora `hermes-*/*`; cambiarlos sería tocar más archivos oficiales):

| Ruta | Qué es |
|---|---|
| `Dockerfile` (bloque `hermes-runtime overlay`) | copia `kit-runtime/`, instala el gancho de arranque, exige una clave pública y fija el comando del contenedor |
| `kit-runtime/VERSION` | versión del runtime, `<versión de Hermes>-<revisión del overlay>`; el tag de git es `v` + esto |
| `kit-runtime/cont-init.d/05-agent-pack` | gancho s6: aplica el paquete como el usuario `hermes` antes del gateway. **Nunca corta el arranque** |
| `kit-runtime/provision/` | `provision.py` (check/apply/bundle/keygen), `paquete.py` (formato v2 y firma), `hermes_config.py`, `apply-pack.sh`, tests |
| `kit-runtime/plugins/` | plugins del runtime, que se instalan y habilitan siempre: `nucleo-acceso`, `nucleo-identidad`, `solo-lectura`, `hermes-session-reset-policy` |
| `kit-runtime/keys/` | claves públicas ed25519 con las que se verifican los paquetes (ver su README) |
| `kit-runtime/ejemplo/` | paquete de ejemplo, neutro: muestra la declaración y cómo se arma |
| `pack/README.md` | dónde va el paquete del cliente (no en este repo) |
| `.github/workflows/hermes-runtime-publish.yml` | publica la imagen en GHCR en cada tag `v<hermes>-<revisión>` |

## Qué hace la imagen al arrancar

1. El setup oficial de Hermes (cont-init 01, 015, 02): usuario, permisos del volumen `/opt/data`, `config.yaml` inicial.
2. `05-agent-pack` corre `kit-runtime/provision/apply-pack.sh` como `hermes`. El paquete sale de `AGENT_PACK_URL` (+ `AGENT_PACK_SHA256`, obligatoria
   con la URL) o de `AGENT_PACK_FILE` (por defecto `/opt/hermes/pack/agent-pack.zip`, que agrega la imagen del cliente).
3. `provision.py apply` verifica **antes de escribir nada**: huella del zip si se pidió, firma ed25519 del manifiesto con `kit-runtime/keys/*.pub`, formato
   2, que el paquete declare compatible esta versión (`kit-runtime/VERSION`), huella de cada archivo (ni de más ni de menos) y que su configuración no
   traiga nada prohibido. Recién entonces copia, con respaldo en `$HERMES_HOME/backups/provision-<fecha>/` de lo que pisa o quita.
4. El gateway (`hermes gateway run`) arranca con lo aplicado. Si algo falló, con lo que había, y el motivo queda en el log con el prefijo `[agent-pack]`.

Qué deja `apply` en el volumen (perfil del bot):

| Destino | Origen |
|---|---|
| `SOUL.md` | `SOUL.md` del paquete |
| `skills/<nombre del paquete>/<skill>/` | `skills/` del paquete; lo que sobre en esa carpeta se quita |
| `plugins/<plugin>/` | los plugins del runtime + los propios del paquete (un plugin del paquete no puede llamarse como uno del runtime) |
| `textos/<plugin>.json` | `textos/` del paquete |
| `config.yaml` | los valores por defecto del runtime, la configuración del paquete encima y lo que fija el runtime (ver *Configuración por defecto*) |
| `.agent-pack.json` | nombre, versión y clave del paquete aplicado, y los plugins que instaló |

Se quitan los plugins que el paquete declara en `retirar.plugins` y los que instaló una versión anterior y la nueva ya no trae. `check` compara todo eso
sin tocar nada y sale con 1 si hay algo que corregir. El perfil `admin` (`--perfil admin`) recibe solo `SOUL.admin.md` y memoria encendida, y se le
quita lo del bot. Detalle de cada regla en el docstring de `provision.py`.

### Configuración por defecto

| Clave | Valor | ¿El paquete la puede cambiar? |
|---|---|---|
| `platform_toolsets.<canal de mensajería>` | `[skills, clarify, todo]` en Telegram y en todo canal de mensajería que ya figure en la config | solo para achicarla: cualquier otro toolset (`web`, `vision`, `tts`, `image_gen`, `terminal`…) es FALLA en `check` y se rechaza al armar. Los canales locales (`cli`, `tui`, `desktop`) quedan libres |
| `session_reset` | `{mode: idle, idle_minutes: 120}` (lo aplica el plugin `hermes-session-reset-policy`) | sí |
| `memory.memory_enabled`, `memory.user_profile_enabled` | `false` en el bot, `true` en el perfil `admin` | no |
| `security.allow_private_urls`, `security.allow_lazy_installs` | `false` en los dos perfiles | no |
| `plugins.enabled` | incluye todos los plugins instalados (los del paquete pueden agregar otros) | agregar sí, quitar los del runtime no |
| control de comandos de Telegram | con `KIT_ADMIN_TELEGRAM_IDS`: `allow_admin_from`, `user_allowed_commands: [new, retry, undo]` y el canal principal | no |

El panel web de Hermes queda apagado: no se define `HERMES_DASHBOARD`, y `check` falla si el gateway la tiene encendida.

## El paquete (formato 2)

Solo datos del cliente; la lógica que lo aplica es de la imagen.

```
manifest.json         {"formato": 2, "nombre", "version": "X.Y.Z", "runtime": [">=0.22.2-1", "<0.23"], "creado",
                       "retirar": {"plugins": [...]}, "archivos": {ruta: sha256}}
manifest.sig          {"algoritmo": "ed25519", "clave": "<16 hex del sha256 de la clave pública>", "firma": "<base64>"}
SOUL.md               obligatorio
SOUL.admin.md         opcional (perfil de administración)
skills/<skill>/...    skills del cliente
plugins/<plugin>/...  plugins propios del cliente
textos/<plugin>.json  textos de los plugins del runtime
config.json           configuración de Hermes (modelo, servidores MCP, toolsets, session_reset…)
```

- **Firma:** ed25519 sobre los bytes exactos de `manifest.json`; como el manifiesto tiene la huella de cada archivo, cubre todo el paquete.
- **Librería:** `cryptography` (50.0.1). Está en las dependencias **base** de Hermes (`pyproject.toml`, `[project].dependencies`, pin con marcador
  `python_version >= '3.14'`; la imagen usa CPython 3.14.7 de `pm/lock.json`), así que viene en `/opt/hermes/.venv`. El build lo comprueba
  (`import cryptography.hazmat.primitives.asymmetric.ed25519`). Alternativa evaluada y descartada: `ssh-keygen -Y verify` (`openssh-client` está en
  los paquetes del sistema de la imagen), porque obligaría a pasar por un proceso externo y un formato de firma más que mantener.
- **Configuración:** se aplica con `read_raw_config()` + `save_config()` de Hermes (`hermes_config.py`): los objetos se mezclan, listas y valores se
  reemplazan, y las referencias `${VARIABLE}` quedan escritas tal cual. Se rechaza (al armar y otra vez en la imagen) una configuración con un secreto
  literal en una clave tipo `token`/`secret`/`password`/`api_key`/`authorization`, con un toolset distinto de `skills`, `clarify` y `todo` en un
  canal de mensajería, o que intente fijar la memoria o `security.*`. **Los secretos van en variables de entorno, nunca en el paquete.**
- **Versiones:** `version` del paquete `X.Y.Z`; `runtime` es la lista de restricciones (`>=`, `>`, `<=`, `<`, `==`) sobre `X.Y[.Z][-R]` que tiene que
  cumplir `kit-runtime/VERSION`.

### Armar y firmar un paquete

Qué entra lo dice `agent-pack.toml` en la carpeta del cliente (rutas relativas a ese archivo; ver `kit-runtime/ejemplo/agent-pack.toml`):

```bash
python kit-runtime/provision/provision.py bundle --decl <carpeta del cliente>/agent-pack.toml --version 0.2.0 \
    --out agent-pack-0.2.0.zip --clave-privada <fuera de todo repo>/agent-pack.pem
```

Imprime el sha256 del zip (va junto con él si se entrega por URL). Para armar hace falta `cryptography` (y PyYAML solo si la configuración es `.yaml`).
La clave se crea una vez con `provision.py keygen` (ver `kit-runtime/keys/README.md`).

### La imagen del cliente

Un repo por cliente (`<cliente>-agent`) con dos líneas de `Dockerfile` y el paquete:

```dockerfile
FROM ghcr.io/<owner>/hermes-runtime:0.22.2-1
COPY pack/agent-pack.zip /opt/hermes/pack/agent-pack.zip
```

O la imagen genérica sola, con el paquete por `AGENT_PACK_URL` + `AGENT_PACK_SHA256`.

## Variables de entorno

| Variable | Quién la usa | Qué es |
|---|---|---|
| `AGENT_PACK_FILE`, `AGENT_PACK_URL`, `AGENT_PACK_SHA256` | `apply-pack.sh` | de dónde sale el paquete (ver arriba) |
| `KIT_ADMIN_TELEGRAM_IDS` | `provision.py` | IDs de Telegram de los administradores, separados por coma; el primero es el canal principal. Provisorio: más adelante los sirve el núcleo (fase G) |
| `TELEGRAM_ALLOW_ALL_USERS=true` | Hermes | obligatoria para que los no registrados lleguen a `nucleo-acceso`; y **ninguna** `TELEGRAM_ALLOWED_USERS` (anula el allow-all) |
| `HERMES_DASHBOARD` | Hermes | **no definirla**: el panel web queda apagado (`check` falla si está encendida) |
| `KIT_CORE_URL`, `KIT_CORE_TOKEN` | `nucleo-acceso` | URL base del núcleo y token del runtime ante él (obligatorias: sin ellas falla cerrado) |
| `KIT_ACCESO_MAX_MENSAJES_NO_REGISTRADO`, `KIT_ACCESO_TIMEOUT_SEGUNDOS`, `KIT_ACCESO_CACHE_SEGUNDOS` | `nucleo-acceso` | 30, 2.5 y 30 por defecto |
| `MCPKIT_IDENTIDAD_SECRETO` | `nucleo-identidad` | el mismo valor que tiene el núcleo (obligatoria; el nombre es el del núcleo, no cambia) |
| `KIT_MCP_NOMBRES` | `nucleo-identidad` | fragmentos del nombre de tool del servidor del núcleo, p. ej. `mcp__nucleo__` (obligatoria: sin ella no se firma nada) |
| `KIT_IDENTIDAD_HEADER`, `KIT_IDENTIDAD_CON_CANAL` | `nucleo-identidad` | `x-channel-id` y `1` por defecto |
| `KIT_TEXTOS_DIR` | los tres plugins | carpeta de textos (por defecto `$HERMES_HOME/textos`) |
| `HERMES_SOLO_LECTURA*` | `solo-lectura` | sin cambios (ver su docstring) |

**Compatibilidad:** cada `KIT_<X>` de la tabla (salvo `KIT_TEXTOS_DIR`) se lee primero y, si no está definida, se usa la `IGA_<X>` del mismo
nombre (`IGA_CORE_URL`, `IGA_ACCESO_CACHE_SEGUNDOS`, `IGA_MCP_NOMBRES`, `IGA_ADMIN_TELEGRAM_IDS`…), que son las del primer despliegue. Lo que sí
cambia: los plugins se llaman `nucleo-acceso` y `nucleo-identidad` (antes `iga-acceso` e `iga-identidad`, que el paquete del cliente retira con
`retirar.plugins`); `KIT_MCP_NOMBRES`/`IGA_MCP_NOMBRES` ya no tiene valor por defecto y el header por defecto es `x-channel-id`, así que un despliegue
que dependía de los valores por defecto viejos tiene que definirlos; los textos por defecto son neutros, y los del cliente van en su paquete.
El registro del volumen pasa de `.iga-pack.json` a `.agent-pack.json` y las skills, de `skills/iga/` a `skills/<nombre del paquete>/`.

## Tests

```bash
python -m pytest kit-runtime/provision/tests                 # aplicador, formato, firma y escritura de config (cryptography + PyYAML)
cd kit-runtime/plugins/nucleo-acceso && python -m pytest
cd kit-runtime/plugins/solo-lectura && python -m pytest
# nucleo-identidad verifica la firma con el núcleo real: con el entorno de mcp-kit
cd kit-runtime/plugins/nucleo-identidad && PYTHONPATH=<mcp-kit>/src <mcp-kit>/.venv/bin/python -m pytest
```

El workflow corre todos menos el de `nucleo-identidad` (necesita mcp-kit, que es un repo privado aparte): correrlo en local antes de cada tag.

## Publicar una versión

1. Ajustar `kit-runtime/VERSION` (`<hermes>-<revisión>`; la revisión sube con cada cambio del overlay, y vuelve a 1 con cada Hermes nuevo).
2. Commit, y tag `v<VERSION>` (por ejemplo `v0.22.2-1`). El workflow verifica que coincidan, que haya una clave pública y ninguna privada, corre los
   tests, escribe el install stamp con la versión de Hermes y publica `ghcr.io/<owner>/hermes-runtime:<VERSION>` (solo `linux/amd64`). Un tag ya
   publicado no se pisa.

## Actualizar Hermes

Lo nuestro es un commit encima de una versión oficial; se mueve a la versión nueva:

```bash
git fetch upstream tag vNUEVA --no-tags
git rebase --onto vNUEVA v0.22.2 <rama>      # v0.22.2 = la versión sobre la que está hoy
```

El único archivo oficial que tocamos es el `Dockerfile`, y solo al final. Después: `kit-runtime/VERSION` a `<NUEVA>-1`, ajustar el `runtime` de los paquetes
que vayan a correr ahí, y correr `provision.py check` en un servicio de prueba: Hermes ya cambió en versiones menores comportamientos que nos afectan
(autorización, comandos, sesiones, carga de plugins).
