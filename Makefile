.PHONY: test test-module test-core test-tools test-all test-quick

CONDA_ENV = Elysia
PYTEST = conda run -n $(CONDA_ENV) python -m pytest

test: test-module test-core test-tools

test-module:
	$(PYTEST) Module/ -v

test-core:
	$(PYTEST) core/tests/ -v

test-tools:
	$(PYTEST) core/tools/ -v

test-all:
	$(PYTEST) Module/ core/tests/ core/tools/ -v --tb=short

test-quick:
	$(PYTEST) Module/ core/tests/ core/tools/ -q --tb=line
