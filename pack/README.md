# pack/

Esta carpeta queda vacía a propósito: el runtime es genérico y no lleva el paquete de ningún cliente.

El paquete (`agent-pack.zip`, firmado) va en la imagen **del cliente**, que parte de la de este repo:

```dockerfile
FROM ghcr.io/<owner>/hermes-runtime:0.22.2-1
COPY pack/agent-pack.zip /opt/hermes/pack/agent-pack.zip
```

O llega al arrancar por `AGENT_PACK_URL` + `AGENT_PACK_SHA256`. Cómo se arma y se firma: `RUNTIME.md`; un paquete de ejemplo neutro:
`kit-runtime/ejemplo/`.
