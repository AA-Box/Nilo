# Upstream provenance and synchronisation

Kivo started from the open-source infrastructure of
[`xinnan-tech/xiaozhi-esp32-server`](https://github.com/xinnan-tech/xiaozhi-esp32-server)
(MIT). This page is the engineering record of that origin; it is deliberately not part of the
product description.

## What was taken

The repository's root commit `c685c4d` (2026-09-11, "Initial commit") is a byte-identical
snapshot of upstream `main` at `6afc54a17d`. Upstream's own commit history was not imported.

Subsystems that remain upstream-derived (all under `main/kivo-server/`):

| Area | Path | Notes |
|---|---|---|
| Session server | `core/connection.py`, `core/websocket_server.py`, `core/handle/` | per-connection handler, text/audio message routing |
| HTTP API | `core/http_server.py`, `core/api/` | OTA bootstrap, vision explain |
| Providers | `core/providers/{asr,tts,llm,vllm,vad,memory,intent}` | provider adapters |
| Tool system | `core/providers/tools/` | server plugins, device IoT, device MCP, MCP endpoint, server MCP |
| Config and logging | `config/` | YAML layering, loguru setup, Opus loader |
| Plugins | `plugins/`, `plugins_func/` | interceptor plugins and `@register_function` tools |
| Benchmarks | `performance_tester/` | provider latency testers |
| Models | `models/` | Silero VAD, SenseVoice configuration (weights not tracked) |

Kivo-owned code lives in `main/kivo-server/robot/` and in the tests; Kivo also owns the
documentation, Docker files, CI and the top-level configuration semantics (`config.yaml` was
translated and restructured and is no longer a drop-in copy of upstream's).

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

* Never commit Kivo work onto `upstream`; it only receives upstream trees.
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
| `main/xiaozhi-server/...` vs `main/kivo-server/...` | git rename detection handles most files; raise `merge.renameLimit` (e.g. `git -c merge.renameLimit=10000 merge upstream`) if it gives up |
| Deleted components (`main/manager-*`, `main/digital-human`, `docs/*`) modified upstream | keep them deleted: `git rm -r` the paths git re-adds |
| Comment-only hunks in `core/` (upstream Chinese vs. our English) | keep ours; take upstream's code lines |
| `config.yaml` | do not merge blindly; diff upstream's two versions and port provider additions by hand |
| `requirements.txt` | port version bumps by hand; re-run the resolver (`uv pip install --dry-run -r requirements.txt`) |

Upstream synchronisation is now selective porting, not wholesale merging: Kivo's direction is
independence, and the value of each upstream release is in individual provider fixes.

## Legacy protocol compatibility

Existing Xiaozhi-family firmware connects to `/xiaozhi/ota/` and `/xiaozhi/v1/`. Kivo keeps
those routes as the `legacy_xiaozhi` protocol ([protocol.md](protocol.md)). Message names,
headers, the OTA response shape and the device MCP semantics are unchanged.

## Licence obligations

* The original MIT notice (`Copyright (c) 2025 xinnan-tech`) stays in `LICENSE`; Kivo's own
  copyright line was added beneath it.
* Substantial portions of `main/kivo-server/core/` are upstream code and remain under that
  notice. Kivo does not claim the code was written from scratch.
* Third-party model files under `models/` keep their own licences (Silero VAD: MIT;
  SenseVoiceSmall configuration: Apache-2.0 per its `configuration.json`).
