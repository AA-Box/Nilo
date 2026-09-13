"""docker-compose.yml describes the nilo-server service (no Docker daemon needed)."""
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def test_compose_service_is_nilo_server():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert list(doc["services"]) == ["nilo-server"]
    svc = doc["services"]["nilo-server"]
    assert svc["image"] == "ghcr.io/aa-box/nilo-server:latest"
    assert svc["container_name"] == "nilo-server"
    assert "8000:8000" in svc["ports"] and "8003:8003" in svc["ports"]
    env = dict(e.split("=", 1) for e in svc["environment"])
    assert {"NILO_SERVER_HOST", "NILO_SERVER_PORT", "NILO_HTTP_PORT", "NILO_LOG_LEVEL"} <= set(env)
    assert any(v.startswith("./data:/opt/nilo-server/data") for v in svc["volumes"])


def test_compose_has_no_legacy_names():
    code = "\n".join(l for l in COMPOSE.read_text(encoding="utf-8").splitlines() if not l.strip().startswith("#"))
    code = code.lower()
    assert "xiaozhi" not in code and "manager" not in code and "xinnan" not in code
