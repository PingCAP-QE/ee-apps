PYTHON ?= python3
LOCAL_TIDB_ENV_FILE ?= ./.local/tidb.env

.PHONY: test
test:
	$(PYTHON) -m pytest

.PHONY: test-cov
test-cov:
	PYTHONPATH=src $(PYTHON) -m pytest --cov=src/ci_dashboard --cov-report=term-missing

.PHONY: lint
lint:
	$(PYTHON) -m ruff check src tests

.PHONY: sync-builds
sync-builds:
	$(PYTHON) -m ci_dashboard.jobs.cli sync-builds

.PHONY: sync-pr-events
sync-pr-events:
	$(PYTHON) -m ci_dashboard.jobs.cli sync-pr-events

.PHONY: sync-flaky-issues
sync-flaky-issues:
	$(PYTHON) -m ci_dashboard.jobs.cli sync-flaky-issues

.PHONY: refresh-build-derived
refresh-build-derived:
	$(PYTHON) -m ci_dashboard.jobs.cli refresh-build-derived

.PHONY: api
api:
	PYTHONPATH=src $(PYTHON) -m uvicorn ci_dashboard.api.main:app --reload

.PHONY: prepare-local-tidb-env
prepare-local-tidb-env:
	./scripts/prepare_local_tidb_env.sh

.PHONY: api-tidb
api-tidb:
	@./scripts/prepare_local_tidb_env.sh >/dev/null && \
	set -a && . $(LOCAL_TIDB_ENV_FILE) && set +a && \
	PYTHONPATH=src $(PYTHON) -m uvicorn ci_dashboard.api.main:app --reload

.PHONY: web-install
web-install:
	cd web && npm install

.PHONY: web-dev
web-dev:
	cd web && npm run dev

.PHONY: web-build
web-build:
	cd web && npm run build
