---
name: marzban-node-operations
description: Maintain Marzban-node runtime configuration, TLS setup, dependencies, Docker image, Compose service, systemd unit, and tag-triggered container releases. Use when changing config.py, .env.example, certificate.py, requirements.txt, Dockerfile, docker-compose.yml, marzban.service, main.py deployment startup, or .github/workflows/build.yml.
---

# Marzban Node Operations

## Workflow

1. Read the runtime file being changed and all duplicated declarations listed in [references/deployment-map.md](references/deployment-map.md).
2. Decide whether the change affects local execution, Docker, systemd, release publishing, or more than one target.
3. Keep defaults synchronized across code, `.env.example`, and deployment examples.
4. Preserve Linux production paths unless the task explicitly migrates them.
5. Verify secrets and private keys are never baked into images or committed.
6. Run the narrowest available validation, then inspect the final diff for drift between deployment surfaces.

## Environment Changes

- Define runtime parsing in `config.py`.
- Document user-facing variables in `.env.example`.
- Use explicit casts for integers and booleans.
- Preserve the meaning of empty values, especially `SSL_CLIENT_CERT_FILE` and `INBOUNDS`.
- Check whether Docker Compose needs to expose or comment the variable.

## TLS Changes

- Keep generated key and certificate paths controlled by environment variables.
- Treat the generated certificate as server identity only; REST client authentication still requires `SSL_CLIENT_CERT_FILE`.
- Preserve mutual TLS for REST.
- Avoid logging certificate or key contents.
- Ensure parent-directory assumptions are considered when changing default paths.

## Container And Service Changes

- Keep the image multi-architecture compatible with `linux/amd64` and `linux/arm64`.
- Preserve Xray executable and asset installation in the runtime image.
- Keep `XRAY_EXECUTABLE_PATH` and `XRAY_ASSETS_PATH` consistent with image paths.
- Account for host networking in Compose before changing ports or bind addresses.
- Keep systemd working directory and script path aligned with the install layout.

## Release Changes

- Releases are triggered by tags matching `v*.*.*`.
- Keep Docker Hub and GHCR tags aligned unless explicitly changing publication policy.
- Treat action major-version upgrades as behavior changes and inspect their migration notes when network access is available.
- Do not print registry credentials or add secrets to repository files.

## Verification

Use what is available:

- Python syntax or import checks for config/startup changes.
- `docker compose config` for Compose edits.
- `docker build` only when feasible; it downloads Xray and dependencies.
- YAML inspection for workflow edits.
- A grep or diff pass confirming every renamed variable/path was updated everywhere.

State clearly when network-dependent image or workflow verification could not be run.
