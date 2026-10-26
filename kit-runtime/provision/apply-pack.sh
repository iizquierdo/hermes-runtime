#!/bin/sh
# Aplica el paquete del agente al arrancar el contenedor, ANTES de levantar el gateway de Hermes, y reporta el estado al núcleo.
# Lo llama el gancho cont-init.d/05-agent-pack como el usuario `hermes`.
#
# Nunca corta el arranque: si el paquete falla (no se descarga, la huella no coincide, la firma no es válida, está dañado o no es
# para esta versión del runtime), el agente arranca con lo que ya tenía y el motivo queda en el log con el prefijo [agent-pack].
# Un paquete malo no debe dejar el bot caído.
#
# La lógica es de la imagen: provision.py y las claves públicas viven en /opt/hermes/kit-runtime; del paquete solo se toman datos.
# La firma se verifica siempre, venga el paquete de donde venga.
#
# De dónde sale el paquete, por orden:
#   AGENT_PACK_URL       descarga el zip de esa URL. Exige AGENT_PACK_SHA256 (la huella que se entrega junto con el zip)
#   KIT_PACK_FROM_CORE=1 lo pide al núcleo (GET /runtime/pack, con KIT_CORE_URL y KIT_CORE_TOKEN). Si el núcleo dice que no cambió,
#                        o no responde, se vuelve a aplicar el último aplicado (la copia del volumen); si no tiene paquete activo,
#                        se sigue con el de la imagen
#   AGENT_PACK_FILE      zip dentro de la imagen (por defecto /opt/hermes/pack/agent-pack.zip: lo agrega la imagen del cliente)
# Otras variables: HERMES_HOME (la imagen lo fija en /opt/data), HERMES_PYTHON, KIT_ADMIN_TELEGRAM_IDS (administradores del bot si el
# núcleo no los da; si no está, IGA_ADMIN_TELEGRAM_IDS).
set -u

log() { echo "[agent-pack] $*"; }

AQUI="$(cd "$(dirname "$0")" && pwd)"
HOME_HERMES="${HERMES_HOME:-/opt/data}"
COPIA="$HOME_HERMES/.agent-pack.zip"
PY="${HERMES_PYTHON:-/opt/hermes/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
[ -n "$PY" ] || { log "no encuentro Python: se sigue sin aplicar el paquete"; exit 0; }

# Reporta al núcleo cómo terminó (si hay núcleo configurado) y sale sin cortar el arranque.
terminar() {
  HERMES_HOME="$HOME_HERMES" "$PY" "$AQUI/provision.py" report --home "$HOME_HERMES" --aplicacion "$1" || true
  exit 0
}

TMP="$(mktemp -d)" || { log "no se pudo crear una carpeta temporal: se sigue sin aplicar el paquete"; terminar fallo; }
trap 'rm -rf "$TMP"' EXIT

ZIP=""
SHA="${AGENT_PACK_SHA256:-}"
if [ -n "${AGENT_PACK_URL:-}" ]; then
  if [ -z "$SHA" ]; then
    log "AGENT_PACK_URL requiere AGENT_PACK_SHA256: se omite la actualización y se sigue con la versión actual"
    terminar fallo
  fi
  ZIP="$TMP/agent-pack.zip"
  if ! curl -fsSL --max-time 120 -o "$ZIP" "$AGENT_PACK_URL"; then
    log "no se pudo descargar el paquete; se sigue con la versión actual"
    terminar fallo
  fi
elif [ "${KIT_PACK_FROM_CORE:-}" = "1" ]; then
  HERMES_HOME="$HOME_HERMES" "$PY" "$AQUI/provision.py" pack-del-nucleo --home "$HOME_HERMES" --out "$TMP/nucleo.zip"
  case "$?" in
    0)  ZIP="$TMP/nucleo.zip"; SHA="" ;;                      # la huella ya la verificó contra X-Pack-Sha256 y el ETag
    11) ;;                                                     # el núcleo no tiene paquete activo: el de la imagen
    *)  if [ -f "$COPIA" ]; then ZIP="$COPIA"; SHA=""; fi ;;  # sin cambios o sin respuesta: el último aplicado
  esac
fi
if [ -z "$ZIP" ]; then
  ZIP="${AGENT_PACK_FILE:-/opt/hermes/pack/agent-pack.zip}"
fi

if [ ! -f "$ZIP" ]; then
  log "no hay paquete en $ZIP: se sigue con la configuración actual"
  terminar sin-paquete
fi

# provision.py verifica, antes de tocar nada: la huella del zip (si hay una esperada), la firma ed25519 del manifiesto con las
# claves de la imagen, la versión del runtime y la huella de cada archivo. Los administradores salen del núcleo si está configurado.
if HERMES_HOME="$HOME_HERMES" "$PY" "$AQUI/provision.py" apply --source "$ZIP" --home "$HOME_HERMES" --sha256 "$SHA"; then
  log "paquete aplicado"
  terminar ok
fi
log "el paquete NO se aplicó (ver el detalle arriba): se sigue con la versión actual"
terminar fallo
