# Service Contracts

## REST Routes

| Route | Method | Session required | Purpose |
| --- | --- | --- | --- |
| `/` | POST | No | Return node status |
| `/connect` | POST | No | Acquire control and return a UUID |
| `/disconnect` | POST | No | Release control and stop Xray |
| `/ping` | POST | Yes | Validate the active session |
| `/start` | POST | Yes | Transform config and start Xray |
| `/stop` | POST | Yes | Stop Xray |
| `/restart` | POST | Yes | Transform config and restart Xray |
| `/geo-resources` | POST | Yes | List local `.dat` Xray assets |
| `/geo-resources/upload` | POST | Yes | Atomically upload a local `.dat` asset |
| `/geo-resources/download` | POST | Yes | Return a local `.dat` asset as base64 |
| `/geo-resources/rename` | POST | Yes | Atomically rename a local `.dat` asset |
| `/geo-resources/delete` | POST | Yes | Delete validated local `.dat` assets |
| `/logs` | WebSocket | Query UUID | Stream or batch logs |

REST status payloads include `connected`, `started`, and `core_version`.

## RPyC Surface

- `start(config)`
- `stop()`
- `restart(config)`
- `fetch_xray_version()`
- `fetch_logs(callback)`
- `list_geo_resources()`
- `upload_geo_resource(filename, content, overwrite=False)`
- `download_geo_resource(filename)`
- `rename_geo_resource(filename, new_filename, overwrite=False)`
- `delete_geo_resources(filenames)`

The connection object stores the controller peer IP. That IP is used by `XRayConfig` to authorize API routing.

## Error Boundaries

- Transport validation errors belong to the service.
- Invalid JSON becomes a client-visible validation error in REST.
- Subprocess failures become service failures and must leave status truthful.
- Callback failures are logged and must not crash the core.
