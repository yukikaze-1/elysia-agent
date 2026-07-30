.PHONY: test test-module test-core test-tools test-all test-quick test-normal test-full start

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

# 测试分层命令
# TaskSystem 尚未完善、非当前开发重点，CI + 日常跳过
TASK_SYSTEM_IGNORE = --ignore=Module/task_system/tests

test-quick:
	conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short $(TASK_SYSTEM_IGNORE)

test-normal:
	conda run -n Elysia python -m pytest -m "not slow" -q --tb=short $(TASK_SYSTEM_IGNORE)

test-full:
	conda run -n Elysia python -m pytest -q

check:
	conda run -n $(CONDA_ENV) python app.py --check

start:
	bash scripts/start.sh

check-full:
	conda run -n $(CONDA_ENV) python app.py --check --full
