# Xray Core Invariants

## Ownership

- `XRayConfig` owns node-specific rewriting of controller-supplied JSON.
- `XRayCore` owns the local Xray subprocess and its logs.
- `rest_service.py` and `rpyc_service.py` translate protocol calls into those two abstractions.

## Injected Configuration

- API tag: `API`
- API inbound tag: `API_INBOUND`
- API services: `HandlerService`, `StatsService`, `LoggerService`
- API inbound protocol: `dokodemo-door`
- API inbound address target: `127.0.0.1`
- API route sources: `127.0.0.1` and the controller peer IP
- TLS certificate and key: `SSL_CERT_FILE` and `SSL_KEY_FILE`

## Runtime

- Command shape: `<xray> run -config stdin:`
- Asset environment variable: `XRAY_LOCATION_ASSET`
- Retained log history: 100 lines
- Startup callers currently wait up to 3 seconds for `Xray <version> started`

## Coupling Checks

When changing a public method or state field, inspect both services for use of:

- `core.started`
- `core.version`
- `core.get_logs()`
- `core.start(config)`
- `core.stop()`
- `core.restart(config)`
