"""Every hook the robot subsystem has in inherited code, enumerated and enforced.

``docs/upstream-changes.md`` says which lines of upstream-owned code Nilo has touched and
why. A page like that is true on the day it is written and wrong a month later, so this
test is the mechanism that keeps it true: it finds every reference to ``robot`` in the
inherited directories and fails when the set changes.

A new hook is not a failure of design — sometimes one is the right answer. It is a failure
to *write it down*, and the fix is one line here and one paragraph there.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2]

#: Directories derived from the upstream project (docs/upstream.md).
INHERITED = ("core", "config", "plugins", "plugins_func")

#: A line that reaches into the robot subsystem, or leaves something on a connection for it.
SEAM = re.compile(r"^\s*(?:from|import)\s+robot\b|nilo_[a-z_]+\s*=")

#: Every seam, as ``path -> what it is for``. Mirrors docs/upstream-changes.md, one row
#: per entry. Adding a hook without adding it here fails this test.
EXPECTED: dict[str, str] = {
    "core/api/ota_handler.py": "the OTA route table comes from the protocol registry",
    "core/connection.py": "attach and detach a session's robot",
    "core/handle/abortHandle.py": "barge-in reaches the voice loop",
    "core/handle/receiveAudioHandle.py": "an utterance reaches the robot agent",
    "core/http_server.py": "the HTTP route table comes from the protocol registry",
    "core/providers/asr/base.py": "recognition latency is left on the connection",
    "core/providers/tools/device_mcp/mcp_handler.py": "telemetry notifications reach the robot",
    "core/websocket_server.py": "the WebSocket path gate comes from the protocol registry",
    "config/logger.py": "the version in every log line",
    # A Nilo-owned *new* file in an inherited directory, which is the extension point
    # `plugins_func/` is for. It conflicts with nothing on a rebase.
    "plugins_func/functions/robot_tools.py": "registers the robot tools as server plugins",
}


def found() -> dict[str, list[str]]:
    """Every seam line in the inherited tree, by path."""
    seams: dict[str, list[str]] = {}
    for root in INHERITED:
        for path in sorted((SERVER / root).rglob("*.py")):
            relative = path.relative_to(SERVER).as_posix()
            hits = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if SEAM.search(line)]
            if hits:
                seams[relative] = hits
    return seams


def test_every_hook_into_inherited_code_is_one_this_repository_documents() -> None:
    seams = found()
    undocumented = sorted(set(seams) - set(EXPECTED))
    assert not undocumented, (
        "these inherited files reach into robot/ and are not in docs/upstream-changes.md: "
        f"{undocumented}"
    )


def test_a_documented_hook_that_has_gone_away_is_removed_from_the_list() -> None:
    """The other direction: the page must not describe a patch that no longer exists."""
    stale = sorted(set(EXPECTED) - set(found()))
    assert not stale, f"docs/upstream-changes.md describes hooks that are gone: {stale}"


def test_the_seam_stays_small() -> None:
    """A budget, not a rule. Every line here is a line that conflicts on the next rebase.

    Nine touched files and one added one is the whole cost of the robot subsystem in
    inherited code. If this number has to go up, the question to ask first is whether the
    work belongs under ``robot/`` behind an existing hook.
    """
    seams = found()
    lines = sum(len(hits) for hits in seams.values())
    assert len(seams) <= 10, sorted(seams)
    assert lines <= 14, {path: hits for path, hits in seams.items() if len(hits) > 1}


def test_the_documentation_page_lists_every_file() -> None:
    page = (SERVER.parents[1] / "docs" / "upstream-changes.md").read_text(encoding="utf-8")
    missing = [path for path in EXPECTED if path not in page]
    assert not missing, f"docs/upstream-changes.md does not name {missing}"
