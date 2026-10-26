"""Escribe config.yaml con las funciones del propio Hermes. Lo corre `provision.py` con el Python de Hermes.

    AGENT_PROVISION_JSON='{"config": {...}, "forzado": {...}, "plugins": [...], "retirar": [...], "gating": {...}}' \\
    HERMES_HOME=<perfil> HERMES_REPO=/opt/hermes  python hermes_config.py

`hermes config set` no sirve para listas ni bloques anidados, así que todo se escribe junto: se lee la config cruda
(`read_raw_config`, sin expandir `${VAR}` ni mezclar valores por defecto), se le aplica el pedido y se guarda con
`save_config`. Las referencias `${VAR}` del paquete quedan escritas tal cual: los secretos siguen en el entorno.

Orden del pedido (lo de más abajo gana):
    config   la configuración del paquete, fusionada encima de la que hay (los objetos se mezclan; listas y valores se reemplazan)
    forzado  lo que fija el runtime (la memoria del perfil), con la clave en notación de puntos
    plugins  nombres que tienen que quedar en `plugins.enabled` (se agregan a los que haya); `retirar` los saca de la lista
    gating   administradores y comandos permitidos de Telegram, más el canal principal (el primer administrador)
"""

from __future__ import annotations

import copy
import json
import os
import sys


def fusionar(base: dict, encima: dict) -> dict:
    """`encima` sobre `base`, sin modificar ninguno de los dos."""
    salida = copy.deepcopy(base)
    for clave, valor in encima.items():
        if isinstance(valor, dict) and isinstance(salida.get(clave), dict):
            salida[clave] = fusionar(salida[clave], valor)
        else:
            salida[clave] = copy.deepcopy(valor)
    return salida


def _fijar(config: dict, clave: str, valor) -> None:
    *camino, ultima = clave.split(".")
    for parte in camino:
        if not isinstance(config.get(parte), dict):
            config[parte] = {}
        config = config[parte]
    config[ultima] = valor


def rutas_de_hojas(datos: dict, prefijo: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    rutas: set[tuple[str, ...]] = set()
    for clave, valor in datos.items():
        ruta = prefijo + (str(clave),)
        rutas |= rutas_de_hojas(valor, ruta) if isinstance(valor, dict) and valor else {ruta}
    return rutas


def aplicar(crudo: dict, pedido: dict) -> dict:
    """La config resultante de aplicar el pedido sobre la config cruda (función pura, sin Hermes)."""
    config = fusionar(crudo or {}, pedido.get("config") or {})
    for clave, valor in (pedido.get("forzado") or {}).items():
        _fijar(config, clave, valor)
    nombres = list(pedido.get("plugins") or [])
    retirar = set(pedido.get("retirar") or [])
    if nombres or retirar:
        plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
        actuales = [n for n in (plugins.get("enabled") or []) if isinstance(n, str)]
        plugins["enabled"] = [n for n in dict.fromkeys(actuales + nombres) if n not in retirar]
        config["plugins"] = plugins
    gating = pedido.get("gating")
    if gating:
        extra = config
        for parte in ("gateway", "platforms", "telegram", "extra"):
            if not isinstance(extra.get(parte), dict):
                extra[parte] = {}
            extra = extra[parte]
        extra["allow_admin_from"] = list(gating["admins"])
        extra["user_allowed_commands"] = list(gating["comandos"])
    return config


def claves_a_preservar(pedido: dict) -> set[tuple[str, ...]]:
    """Lo que escribe el pedido se guarda aunque coincida con el valor por defecto de Hermes (si no, `check` lo vería faltar)."""
    rutas = rutas_de_hojas(pedido.get("config") or {})
    rutas |= {tuple(clave.split(".")) for clave in (pedido.get("forzado") or {})}
    if pedido.get("plugins") or pedido.get("retirar"):
        rutas.add(("plugins", "enabled"))
    return rutas


def main() -> int:  # pragma: no cover - corre dentro de la imagen, con Hermes
    sys.path.insert(0, os.environ["HERMES_REPO"])
    from hermes_cli.config import read_raw_config, save_config  # type: ignore[import-not-found]

    pedido = json.loads(os.environ["AGENT_PROVISION_JSON"])
    save_config(aplicar(read_raw_config(), pedido), preserve_keys=claves_a_preservar(pedido))
    gating = pedido.get("gating")
    if gating:
        from gateway.config import HomeChannel, Platform, persist_home_channel  # type: ignore[import-not-found]

        primero = gating["admins"][0]
        persist_home_channel(HomeChannel(
            platform=Platform.TELEGRAM, chat_id=primero, name="Administración",
            thread_id=None, user_id=primero, scope_id=None,
        ))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
