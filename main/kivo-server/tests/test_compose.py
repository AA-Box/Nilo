"""docker-compose.yml describes the kivo-server service (no Docker daemon needed)."""
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def test_compose_service_is_kivo_server():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert list(doc["services"]) == ["kivo-server"]
    svc = doc["services"]["kivo-server"]
    assert svc["image"] == "ghcr.io/aa-box/kivo-server:latest"
    assert svc["container_name"] == "kivo-server"
    assert "8000:8000" in svc["ports"] and "8003:8003" in svc["ports"]
    env = dict(e.split("=", 1) for e in svc["environment"])
    assert {"KIVO_SERVER_HOST", "KIVO_SERVER_PORT", "KIVO_HTTP_PORT", "KIVO_LOG_LEVEL"} <= set(env)
    assert any(v.startswith("./data:/opt/kivo-server/data") for v in svc["volumes"])


def test_compose_has_no_legacy_names():
    code = "\n".join(l for l in COMPOSE.read_text(encoding="utf-8").splitlines() if not l.strip().startswith("#"))
    code = code.lower()
    assert "xiaozhi-esp32-server" not in code and "manager" not in code and "xinnan" not in code
