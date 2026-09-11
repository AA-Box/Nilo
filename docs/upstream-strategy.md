# Upstream strategy

Cozmo vendors [`xinnan-tech/xiaozhi-esp32-server`](https://github.com/xinnan-tech/xiaozhi-esp32-server).
Upstream's own commit history is not imported, but upstream tracking still works
like normal git, because of the `upstream` branch.

## The `upstream` branch

`upstream` is a vendor branch. Its first commit is this repository's root commit,
whose tree is byte-identical to upstream `main` at `6afc54a17d`. Each later
upstream release is appended to `upstream` as a single squashed commit holding
that release's tree.

```
upstream:  root ──► upstream@next ──► upstream@next+1
             │                             │
             │ shared ancestor             │ git merge upstream
             ▼                             ▼
develop:   root ──► PR merges ──► robot work ──► merged
```

Because `root` is a real shared ancestor, `git merge upstream` is an ordinary
three-way merge: git sees what upstream changed, what we changed, and only
conflicts where both touched the same lines.

Rules:

* Never commit Cozmo work onto `upstream`. It only ever receives upstream trees.
* Never rebase or rewrite `upstream`. Its commits are the merge bases.

## Syncing

```bash
./.cozmo/sync-upstream.sh          # upstream main
./.cozmo/sync-upstream.sh v0.9.6   # a specific tag
git merge upstream
```

Merge into `develop`, resolve there, then promote to `main`.

## Keeping merges cheap

Upstream merges stay cheap only if we avoid editing upstream files. Robot code
lives in its own tree:

```
main/xiaozhi-server/

├── core/                 # upstream — minimize changes
├── models/               # upstream
├── plugins_func/         # upstream
│
├── robot/                # ours
│   ├── actions/
│   ├── agent/
│   ├── behavior/
│   ├── devices/
│   ├── events/
│   ├── memory/
│   ├── personality/
│   ├── protocol/
│   ├── safety/
│   ├── simulator/
│   ├── state/
│   └── vision/
│
└── app.py
```

Integration with Xiaozhi is one thin seam, not logic sprinkled through
`core/`, `handlers/`, `providers/`, `utils/`, `websocket/`, `audio/`:

```
Xiaozhi connection
       │
       ▼
RobotSessionAdapter
       │
       ▼
robot/
```

Every line added to an upstream file is a line that can conflict on the next
sync. When an upstream file must change, prefer the smallest possible hook that
calls into `robot/`.

## Inherited components

`manager-api`, `manager-web`, `manager-mobile` and `digital-human` are kept
as-is for now. Removing them saves little and makes upstream diffs noisier.
Revisit once the robot dashboard exists.

## Unmerged upstream PRs

Upstream pull requests that do not merge cleanly are archived as patches in
`.cozmo/unmerged-prs/`, listed in
[`.cozmo/unmerged-prs/INDEX.md`](../.cozmo/unmerged-prs/INDEX.md).
