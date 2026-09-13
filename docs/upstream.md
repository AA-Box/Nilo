# Upstream provenance and synchronisation

Nilo started from the open-source infrastructure of
[`xinnan-tech/xiaozhi-esp32-server`](https://github.com/xinnan-tech/xiaozhi-esp32-server)
(MIT). This page is the engineering record of that origin; it is deliberately not part of the
product description.

## What was taken

The repository's root commit `c685c4d` (2026-09-11, "Initial commit") is a byte-identical
snapshot of upstream `main` at `6afc54a17d`. Upstream's own commit history was not imported.

Subsystems that remain upstream-derived (all under `main/nilo-server/`):

| Area | Path | Notes |
|---|---|---|
| Session server | `core/connection.py`, `core/websocket_server.py`, `core/handle/` | per-connection handler, text/audio message routing; the path gate that answers `404` is Nilo's |
| HTTP API | `core/http_server.py`, `core/api/` | OTA bootstrap, vision explain; the route table is now built from `robot/protocol` |
| Session helpers | `core/utils/`, `core/auth.py` | dialogue buffer, Opus/audio helpers, module initialisation, device token signing |
| Providers | `core/providers/{asr,tts,llm,vllm,vad,memory,intent}` | provider adapters |
| Tool system | `core/providers/tools/` | server plugins, device IoT, device MCP, MCP endpoint, server MCP |
| Config and logging | `config/` | YAML layering, loguru setup, Opus loader; the `NILO_*` overrides and `config/placeholders.py` are Nilo's |
| Plugins | `plugins/`, `plugins_func/` | interceptor plugins and `@register_function` tools |
| Benchmarks | `performance_tester/` | provider latency testers |
| Models | `models/` | Silero VAD (weights vendored); SenseVoiceSmall configuration only, its `model.pt` is mounted at runtime |

Nilo-owned code lives in `main/nilo-server/robot/` — today only `robot/protocol/` — and in the
tests; Nilo also owns the documentation, Docker files, CI and the top-level configuration
semantics (`config.yaml` was translated and restructured and is no longer a drop-in copy of
upstream's, and the inherited deprecated-key aliases are gone: `config_loader.DEPRECATED_KEYS`
is now empty).

Removed from the snapshot: the Java management console (`manager-api`), its Vue web UI
(`manager-web`), the uni-app mobile client (`manager-mobile`), the browser "digital human"
test client, the Chinese-template smart-home `preprocess_plugin`, and all inherited
documentation. Reasons are in [migration.md](migration.md).

## The `upstream` branch

`upstream` is a vendor branch: its first commit is the repository's root commit, and each
later upstream release is appended as a single squashed commit holding that release's tree.
Because the root is a real shared ancestor, `git merge upstream` is an ordinary three-way merge.

```
upstream:  root ──► upstream@next ──► upstream@next+1
             │                             │
             │ shared ancestor             │ git merge upstream
             ▼                             ▼
develop:   root ──► migration ──► robot work ──► ...
```

Rules:

* Never commit Nilo work onto `upstream`; it only receives upstream trees.
* Never rebase or rewrite `upstream`; its commits are the merge bases.

### Syncing

```bash
./scripts/sync-upstream.sh          # advance `upstream` to upstream main
./scripts/sync-upstream.sh v0.9.6   # or a specific tag
git merge upstream                  # then resolve on develop
```

Expect these conflict classes after the migration, and resolve them this way:

| Conflict | Resolution |
|---|---|
| `main/xiaozhi-server/...` vs `main/nilo-server/...` | git rename detection handles most files; raise `merge.renameLimit` (e.g. `git -c merge.renameLimit=10000 merge upstream`) if it gives up |
| Deleted components (`main/manager-*`, `main/digital-human`, `docs/*`) modified upstream | keep them deleted: `git rm -r` the paths git re-adds |
| Comment-only hunks in `core/` (upstream Chinese vs. our English) | keep ours; take upstream's code lines |
| `config.yaml` | do not merge blindly; diff upstream's two versions and port provider additions by hand |
| `requirements.txt` | port version bumps by hand; re-run the resolver (`uv pip install --dry-run -r requirements.txt`) |

Upstream synchronisation is now selective porting, not wholesale merging: Nilo's direction is
independence, and the value of each upstream release is in individual provider fixes.

## Protocol divergence

The inherited `/xiaozhi/ota/` and `/xiaozhi/v1/` routes were removed. The legacy protocol module
is deleted, `ALL_PROTOCOLS` is `(NILO,)`, and `ProtocolRegistry.strict` now ships `true`, so a
WebSocket path matching no protocol is answered with `404` instead of being accepted
(`robot/protocol/base.py`, `core/websocket_server.py`). Devices flashed with upstream firmware
can no longer connect; they have to be reflashed against `/nilo/ota/`. There is no fallback and
no compatibility mode — `protocols.strict: false` only restores the permissive path matching, it
does not bring the routes back.

Upstream wire compatibility is therefore no longer a constraint when porting: `nilo` is the only
protocol ([protocol.md](protocol.md)), and upstream work on the retired routes is dropped rather
than merged. What stays inherited is the *shape* of the session, because the handlers under
`core/` are upstream code: the JSON message vocabulary kept as `RESERVED_MESSAGE_TYPES`
(`robot/protocol/nilo.py`), the OTA response body and the device MCP semantics. Changing any of
those is now a Nilo decision, not an upstream one. `scripts/smoke_check.py` probes the retired
routes and expects them to 404.

## Licence obligations

* The original MIT notice (`Copyright (c) 2025 xinnan-tech`) stays in `LICENSE`; Nilo's own
  copyright line was added beneath it.
* Substantial portions of `main/nilo-server/core/` are upstream code and remain under that
  notice. Nilo does not claim the code was written from scratch.
* Third-party model files under `models/` keep the licences of the projects they came from
  (`snakers4/silero-vad`, FunASR's `SenseVoiceSmall`); the vendored trees carry no licence file
  of their own, so check upstream's terms before redistributing them.
