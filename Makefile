# Verify-Before-Wire developer commands. Run `make help`.
PY := backend/.venv/bin/python
PG_TEST := postgresql+asyncpg://vbw:vbw_dev_only@127.0.0.1:5433/vbw_test

.PHONY: help up down setup db dev test test-pg lint eval check sap-demo bench
help:            ## list commands
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'
up:              ## whole product in Docker (PostgreSQL + app) on http://localhost:8080
	docker compose up --build -d && echo "→ http://localhost:8080"
down:            ## stop the Docker stack
	docker compose down
setup:           ## local venv, dependencies and demo files
	cd backend && python3.12 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt && .venv/bin/python scripts/make_demo.py
db:              ## local PostgreSQL for development (127.0.0.1:5433)
	docker compose -f docker-compose.dev.yml up -d
dev: db          ## run the app with hot reload against local PostgreSQL
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8080 --reload-exclude 'data/*'
test:            ## tests on memory + SQLite
	cd backend && .venv/bin/python -m pytest -q
test-pg: db      ## tests on real PostgreSQL (append-only audit trigger included)
	cd backend && VBW_TEST_DATABASE_URL=$(PG_TEST) .venv/bin/python -m pytest -q
lint:            ## ruff + bandit
	cd backend && .venv/bin/ruff check app scripts tests migrations && .venv/bin/bandit -r app -ll -q
eval:            ## fraud catch rate / false alarms gate
	cd backend && .venv/bin/python scripts/evaluate.py --min-catch 1.0 --max-false-alarm 0.10
check: lint test test-pg eval   ## everything CI runs locally
sap-demo:        ## SAP F110 run end to end against a running app (signed webhooks)
	cd backend && .venv/bin/python scripts/sap_simulator.py
bench:           ## load test: 300 checks, 30 in flight, against a running app
	cd backend && .venv/bin/python scripts/bench.py --n 300 --concurrency 30
