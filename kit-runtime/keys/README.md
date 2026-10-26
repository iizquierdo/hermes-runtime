# Claves públicas de confianza

La imagen acepta solo paquetes firmados por una de las claves `*.pub` (ed25519, PEM) de esta carpeta. **Sin ninguna clave, la imagen
rechaza todo paquete** y el pipeline no publica (`.github/workflows/hermes-runtime-publish.yml` lo verifica).

Crear el par (una sola vez, en una máquina de confianza):

```bash
python kit-runtime/provision/provision.py keygen --privada <fuera de todo repo>/agent-pack.pem --publica kit-runtime/keys/<nombre>.pub
```

- La **privada** no entra nunca a un repo ni a una imagen: se guarda en el gestor de secretos de quien arma los paquetes.
- La **pública** se commitea acá. Pueden convivir varias (rotación): se agrega la nueva, se republica la imagen, se firman los
  paquetes con la nueva y recién después se quita la vieja.
- El id de la clave (16 hex del sha256 de la pública) aparece en el log de `apply` y en `$HERMES_HOME/.agent-pack.json`.
