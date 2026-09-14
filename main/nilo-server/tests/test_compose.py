"""Both Compose files describe what they claim to (no Docker daemon needed).

Two files, two jobs: the one beside the server is the deployment (a published image and
mounted model weights), and the one at the repository root is development (`docker compose
up` from a clean clone, building from the working tree).
"""
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
DEV_COMPOSE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


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


def test_the_deployment_secret_is_a_file_not_an_environment_variable():
    """A token in `environment:` is a token in `docker inspect`, in `ps`, and in a crash dump."""
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    svc = doc["services"]["nilo-server"]
    env = dict(e.split("=", 1) for e in svc["environment"])
    assert "NILO_ROBOT_ADMIN_TOKEN" not in env
    assert env["NILO_ROBOT_ADMIN_TOKEN_FILE"].startswith("/run/secrets/")
    assert "nilo_admin_token" in doc["secrets"]


def test_the_deployment_has_a_readiness_healthcheck():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    check = doc["services"]["nilo-server"]["healthcheck"]
    assert any("/ready" in part for part in check["test"])


def test_development_compose_builds_from_the_tree_and_needs_no_model_weights():
    doc = yaml.safe_load(DEV_COMPOSE.read_text(encoding="utf-8"))
    server = doc["services"]["nilo-server"]
    assert server["build"]["dockerfile"] == "Dockerfile-server"
    assert any(volume.endswith(":/opt/nilo-server") for volume in server["volumes"])
    assert not any("model.pt" in volume for volume in server["volumes"]), (
        "`docker compose up` from a clean clone must not need a model download"
    )
    assert any("/ready" in part for part in server["healthcheck"]["test"])
    env = server["environment"]
    assert "NILO_ROBOT_ADMIN_TOKEN" not in env
    assert env["NILO_ROBOT_ADMIN_TOKEN_FILE"].startswith("/run/secrets/")


def test_development_compose_has_no_database_service():
    """SQLite is the only store. A Postgres nobody uses is a Postgres somebody maintains."""
    doc = yaml.safe_load(DEV_COMPOSE.read_text(encoding="utf-8"))
    assert set(doc["services"]) == {"nilo-server", "nilo-simulator"}
    assert doc["services"]["nilo-simulator"]["profiles"] == ["demo"]


def test_compose_has_no_legacy_names():
    code = "\n".join(l for l in COMPOSE.read_text(encoding="utf-8").splitlines() if not l.strip().startswith("#"))
    code = code.lower()
    assert "xiaozhi" not in code and "manager" not in code and "xinnan" not in code
