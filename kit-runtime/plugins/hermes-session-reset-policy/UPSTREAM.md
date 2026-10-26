# hermes-session-reset-policy (copia fijada)

Copia sin modificar del plugin de catálogo de Hermes (`plugin-catalog/hermes-session-reset-policy.yaml`).

| | |
|---|---|
| Origen | https://github.com/fastfinge/hermes-session-reset-policy |
| Commit | `493df020496d2782d3fe15308cc3b6672c2224c4` (versión 0.3.0) |
| Licencia | MIT (`LICENSE`, copiado tal cual: permite copiar y redistribuir conservando el aviso) |
| Archivos copiados | `__init__.py`, `plugin.yaml`, `LICENSE` (no se copian sus tests ni su CI) |

Huellas sha256 de lo copiado (verificar contra el commit al actualizar):

```
f2a6a66202ccc32b3e938936064f4d77b434985bd099fc2166fde1a965679726  __init__.py
649c3a2358e81b8289caee1fcc627a9ab94ddbf7ee606e09380a28fb752a206a  plugin.yaml
30946f30a06f2d9a7a20fe3c0718f44f3e3991c3aa3f239c9e1d6f0ed0df5705  LICENSE
```

Revisado: solo biblioteca estándar, sin red, sin hilos ni escrituras propias; importa internos de Hermes de forma diferida. No hace nada
si `config.yaml` no tiene el bloque `session_reset` (por ejemplo `session_reset: {mode: idle, idle_minutes: 120}`), que va en la
configuración del paquete del cliente.

Se copia en vez de instalarlo en el build para que la imagen no dependa de la red ni de que el repo de origen siga existiendo. Para
actualizarlo: bajar los tres archivos del commit nuevo, revisar el diff completo, actualizar esta tabla y las huellas.
