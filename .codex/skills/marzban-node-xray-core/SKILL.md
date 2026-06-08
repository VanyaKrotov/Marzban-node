---
name: marzban-node-xray-core
description: Maintain Marzban-node Xray configuration rewriting and subprocess lifecycle. Use when changing xray.py, XRayConfig, XRayCore, API inbound or routing injection, INBOUNDS filtering, Xray startup detection, log buffering, restart behavior, executable invocation, or tests for those behaviors.
---

# Marzban Node Xray Core

## Workflow

1. Read `xray.py`, `config.py`, and every service caller affected by the change.
2. Identify whether the change belongs to configuration transformation, process lifecycle, or log delivery.
3. Preserve the invariants in [references/invariants.md](references/invariants.md).
4. Keep changes local to `XRayConfig` or `XRayCore`; change service code only when its contract must change.
5. Add focused tests with mocks or a fake executable. Do not require a real Xray installation for unit tests.
6. Run syntax checks and the available test suite. Exercise start, failure, stop, and restart paths when lifecycle code changes.

## Configuration Changes

- Parse input once and continue exposing the transformed configuration as a `dict`.
- Remove pre-existing `API_INBOUND` and routes targeting the configured API tag before injecting node-owned entries.
- Treat missing `inbounds`, `routing`, and `routing.rules` as valid input.
- Keep selected-inbound filtering conditional: an empty `INBOUNDS` means retain user inbounds.
- Do not mutate a list while iterating over the same live list. Iterate over a copy or build a filtered list.
- Keep the injected API listener TLS-enabled and sourced from configured certificate paths.
- Keep API routing restricted to localhost and the connected controller peer.

## Process Changes

- Keep `started` derived from `Popen.poll()`, not from an independent flag.
- Pass Xray config through stdin and keep `XRAY_LOCATION_ASSET` in the child environment.
- Preserve stdout/stderr handling expected by startup detection and log consumers.
- Make cleanup idempotent: stopping an absent or exited process must not damage later starts.
- Ensure `restarting` is cleared in a `finally` block.
- Avoid callbacks or log threads retaining a stale process after stop or restart.

## Verification

Prefer small tests around:

- Configs with no `inbounds` or no `routing`.
- Existing API inbound and API route removal.
- Empty and populated `INBOUNDS`.
- Successful start marker, immediate child failure, repeated stop, and restart failure.
- Log buffer fan-out and context-manager cleanup.

If dependencies are unavailable, at minimum run `python -m compileall` with an available Python runtime and inspect the diff.
