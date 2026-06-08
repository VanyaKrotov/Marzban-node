---
name: marzban-node-services
description: Maintain Marzban-node controller services and protocol compatibility across FastAPI REST and RPyC. Use when changing rest_service.py, rpyc_service.py, main.py protocol startup, sessions, connect or disconnect semantics, start/stop/restart calls, log streaming, WebSockets, TLS client authentication, validation responses, or tests for service behavior.
---

# Marzban Node Services

## Workflow

1. Read `rest_service.py`, `rpyc_service.py`, `main.py`, and the relevant methods in `xray.py`.
2. Classify the change as shared behavior, REST-only transport behavior, or legacy RPyC behavior.
3. Use [references/contracts.md](references/contracts.md) to check existing contracts.
4. Put shared Xray behavior in `XRayConfig` or `XRayCore`; keep serialization, sessions, and transport errors in the service modules.
5. Preserve compatibility unless the task explicitly changes the controller protocol.
6. Add focused service tests with a mocked `XRayCore`; do not launch a real Xray process.

## REST Rules

- Keep a single active controller session represented by `session_id`, `client_ip`, and `connected`.
- Require the session UUID for ping and lifecycle mutations.
- Return status through `response()` so `connected`, `started`, and `core_version` remain consistent.
- Preserve structured 422 errors for invalid JSON and request validation.
- Keep `/logs` session validation before accepting the WebSocket.
- Validate optional log intervals as finite positive values no greater than 10 seconds.
- Avoid blocking the event loop in new async code. Existing synchronous lifecycle methods may remain synchronous unless deliberately refactored.

## RPyC Rules

- Preserve the single-controller policy in `on_connect`.
- Stop Xray when the active controller disconnects.
- Expose remote methods intentionally with `@rpyc.exposed`.
- Treat remote callbacks as unreliable and isolate callback failures from core lifecycle state.
- Stop and join log handler threads deterministically.

## TLS And Startup

- REST requires `SSL_CLIENT_CERT_FILE` and mutual TLS.
- RPyC may start without a client CA, but must retain the security warning.
- Keep protocol selection limited to `rest` and `rpyc`.
- Do not silently downgrade REST authentication.

## Verification

Cover the changed path and its nearest failure cases:

- Connect takeover or rejection.
- Session mismatch and invalid UUID.
- Invalid config JSON.
- Core start/restart failure and stopped status.
- WebSocket disconnect and interval validation.
- Controller disconnect cleanup.

When a behavior exists in both protocols, verify both or explicitly document why it is transport-specific.
