# Security policy

## Reporting a vulnerability

The repository is currently **private**, so everyone who can read it can also file an issue: open
one and prefix the title with `[security]`. Do not describe a working exploit in the issue body —
say what is affected and ask a maintainer for a private channel.

Before this repository is made public, a maintainer must enable **private vulnerability reporting**
(Settings → Code security), after which reports go to
`https://github.com/AA-Box/Nilo/security/advisories/new` instead and public issues stop being an
acceptable channel. That switch is tracked as a release blocker.

Include what you can: affected version or commit, configuration (with secrets removed), the
request or message sequence that triggers it, and what an attacker gains.

## Supported versions

Nilo is pre-1.0 and moves fast. Only the tip of `main` receives fixes; there are no backports.

## What is in scope

The server and its device-facing surfaces:

* the WebSocket session endpoint and the OTA bootstrap endpoint (`/nilo/v1/`, `/nilo/ota/`)
* the firmware download route and the vision endpoint (`/mcp/vision/explain`)
* device authentication (HMAC tokens, the device allow-list) and the JWT used by the vision endpoint
* configuration and secret handling
* the tool and MCP surfaces reachable from a connected device

Out of scope: third-party AI providers, vulnerabilities in dependencies that upstream has already
published (report those upstream), and anything requiring an operator to deliberately misconfigure
the server.

## Things worth knowing before you report

These are known and documented, not findings:

* **The deployment owns network exposure.** The server binds `0.0.0.0` by default and speaks plain
  HTTP and WebSocket. Put it behind TLS on anything but a trusted LAN — see
  [`docs/deployment.md`](docs/deployment.md).
* **`server.auth.enabled` defaults to `false`.** With authentication off, any device that can reach
  the port can open a session. [`docs/configuration.md`](docs/configuration.md) covers turning it on.
* **The remote-config mode (`manager-api.url`) is deprecated** and dormant; no server ships with
  Nilo to talk to it.

## Safety, not security

Physical-safety behaviour — motion limits, collision and cliff avoidance, watchdogs, emergency
stop — is documented in [`docs/safety-model.md`](docs/safety-model.md). The backend is a policy filter, not a
real-time guarantee; the device owns safety-critical control. A report that the backend cannot
guarantee a motion deadline is expected behaviour, not a vulnerability.
