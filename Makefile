.PHONY: test test-fast test-python test-java test-web lint lint-python typecheck help

help:
	@echo "make test-fast   - run all fast unit tests (default)"
	@echo "make test        - alias for test-fast"
	@echo "make test-python - pytest in main/xiaozhi-server"
	@echo "make test-java   - mvn test in main/manager-api"
	@echo "make test-web    - npm run test:unit in main/manager-web"
	@echo "make lint        - ruff check in main/xiaozhi-server"
	@echo "make typecheck   - mypy on main/xiaozhi-server/robot (no-op until it exists)"

test: test-fast

test-fast: test-python test-java test-web

test-python:
	cd main/xiaozhi-server && python -m pytest -x -q

test-java:
	cd main/manager-api && mvn -B -q test -DfailIfNoTests=false

test-web:
	cd main/manager-web && npm ci --no-audit --no-fund && npm test

lint: lint-python

lint-python:
	cd main/xiaozhi-server && ruff check .

typecheck:
	cd main/xiaozhi-server && if [ -d robot ]; then mypy; else echo "robot/ does not exist yet"; fi
