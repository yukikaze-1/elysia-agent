.PHONY: test test-module test-core test-tools test-all test-quick test-normal test-full test-workflow-engine lint start

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
# WorkflowEngine 尚未完善、非当前开发重点，CI + 日常跳过
TASK_SYSTEM_IGNORE = --ignore=Module/workflow_engine/tests

test-quick:
	conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short $(TASK_SYSTEM_IGNORE)

test-normal:
	conda run -n Elysia python -m pytest -m "not slow" -q --tb=short $(TASK_SYSTEM_IGNORE)

test-full:
	conda run -n Elysia python -m pytest -q

# workflow_engine / workflow_contract 独立迭代门禁（不含 integration——需真实 LLM/DB）
test-workflow-engine:
	conda run -n Elysia python -m pytest Module/workflow_engine/tests Module/workflow_contract/tests -m "unit or service" -q --tb=short

check:
	conda run -n $(CONDA_ENV) python app.py --check

# 导入风格门禁：禁相对导入（TID252），__init__.py / __main__.py 豁免。见 ruff.toml
lint:
	ruff check core/ Module/ --exclude __pycache__

start:
	bash scripts/start.sh

check-full:
	conda run -n $(CONDA_ENV) python app.py --check --full
