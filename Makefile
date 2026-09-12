.PHONY: help test lint typecheck check-docs run compose-validate docker-build smoke

SERVER := main/kivo-server
PY ?= python

help:
	@echo "make test             - pytest in $(SERVER) (works with requirements-dev.txt alone)"
	@echo "make lint             - ruff check"
	@echo "make typecheck        - mypy on $(SERVER)/robot"
	@echo "make check-docs       - validate docs links, paths, env vars and branding"
	@echo "make run              - start kivo-server locally (needs requirements.txt)"
	@echo "make compose-validate - docker compose config"
	@echo "make docker-build     - build base + server images locally"
	@echo "make smoke            - hit a running server's OTA/WebSocket routes (HOST, WS_PORT, HTTP_PORT)"

test:
	cd $(SERVER) && $(PY) -m pytest -q

lint:
	cd $(SERVER) && ruff check .

typecheck:
	cd $(SERVER) && mypy

check-docs:
	$(PY) scripts/check_docs.py

run:
	cd $(SERVER) && $(PY) app.py

compose-validate:
	cd $(SERVER) && docker compose -f docker-compose.yml config --quiet && echo "docker-compose.yml OK"

docker-build:
	docker build -f Dockerfile-server-base -t ghcr.io/aa-box/kivo-server:base .
	docker build -f Dockerfile-server -t ghcr.io/aa-box/kivo-server:latest .

smoke:
	$(PY) scripts/smoke_check.py --host $${HOST:-127.0.0.1} --ws-port $${WS_PORT:-8000} --http-port $${HTTP_PORT:-8003}
