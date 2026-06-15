# Deployment Map

## Runtime Configuration

| Concern | Code/default | Deployment surfaces |
| --- | --- | --- |
| Service bind | `SERVICE_HOST`, `SERVICE_PORT` | `.env.example`, Compose networking |
| Protocol | `SERVICE_PROTOCOL` | `.env.example`, Compose |
| Xray binary/assets | `XRAY_EXECUTABLE_PATH`, `XRAY_ASSETS_PATH` | Dockerfile |
| Xray API bind | `XRAY_API_HOST`, `XRAY_API_PORT` | `.env.example` |
| Server TLS | `SSL_CERT_FILE`, `SSL_KEY_FILE` | `.env.example`, Compose volume |
| Client CA | `SSL_CLIENT_CERT_FILE` | `.env.example`, Compose |
| Inbound selection | `INBOUNDS` | `.env.example` |
| Logging | `DEBUG` | `.env.example` |

## Production Paths

- Application directory: `/var/lib/marzban-node`
- Xray executable: `/usr/local/bin/xray`
- Xray assets: `/usr/local/share/xray`
- Server certificate: `/var/lib/marzban-node/ssl_cert.pem`
- Server key: `/var/lib/marzban-node/ssl_key.pem`
- Client CA example: `/var/lib/marzban-node/ssl_client_cert.pem`

## Packaging

- Docker base: Python slim image, version selected by `PYTHON_VERSION`
- Container command: `python main.py`
- Compose uses `network_mode: host`
- Compose persists `/usr/local/share/xray` in the `xray-assets` volume
- systemd runs `/var/lib/marzban-node/main.py`
- GitHub Actions publishes Docker Hub and GHCR images on version tags
