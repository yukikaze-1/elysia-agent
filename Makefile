.PHONY: test test-module test-core test-tools test-all test-quick test-normal test-remainder test-full test-external test-real-mongo test-workflow-engine lint pyright lock-env audit start gen-types gen-frames check-types check-compose check-env-lock check-arch check-debug-auth check-timeouts check-contract check-secrets check-secrets-history check-ci-isolation check-test-sets check check-full

CONDA_ENV = Elysia
PYTEST = conda run -n $(CONDA_ENV) python -m pytest

# pytest-xdist worker 数（2026-09-14 CI 性能专项）。CI 与本地同源，避免两套行为；
# 取值是**实测 sweet spot**（档位表见 docs/testing.md「并行执行」）而非"越多越好"——
# 每个 worker 都要独立 import torch/sentence-transformers（数百 MB），
# 且 `-n 24`(≈auto) 并不比 `-n 12` 快，还曾在竞争下少报过一条用例（RC 复核未复现，
# 见 docs/testing.md）。**CI 明确禁止 `-n auto`**：worker 数不应随 runner 核数漂移。
# 覆盖：`make test-normal PYTEST_N=12`。
PYTEST_N ?= 8
PYTEST_XDIST = -n $(PYTEST_N) --dist loadfile

test: test-module test-core test-tools

test-module:
	$(PYTEST) Module/ -v

test-core:
	$(PYTEST) core/tests/ -v

test-tools:
	$(PYTEST) core/tools/ -v

test-all:
	# F1：真正全仓非 external pytest——合并门禁的可复现全量（旧"部分仓"语义由 test 承接；
	# 真实 LLM/DB 外部 E2E 经 external marker 排除，独立 opt-in）
	$(PYTEST) -m "not external" -q --tb=short

# 测试分层命令
# F1：external marker 取代 --ignore——WorkflowEngine unit/service 入合并门禁，
# 真实 LLM/DB 集成测试标 external、独立 opt-in（test-external）。
#
# 2026-09-14 CI 性能专项——**Quick 与 remainder 互斥**：
#   Quick     = unit or service
#   remainder = not slow and not external and not unit and not service
# 两条恒等式（Quick ∪ remainder == Normal、Quick ∩ remainder == ∅）由
# `make check-test-sets` 强制。此前 CI 的 Quick / Normal 两个 job 里 Quick ⊆ Normal，
# 98.5% 的用例被同一台 runner 跑了两遍（runner 侧实测 5m34s + 5m05s 串行）。
test-quick:
	$(PYTEST) -m "unit or service" -q -x --tb=short $(PYTEST_XDIST)

test-normal:
	$(PYTEST) -m "not slow and not external" -q --tb=short $(PYTEST_XDIST)

# Normal 中**不在** Quick 里的部分（互斥余集）。当前只有 3 条 integration——
# 保留它是因为 Quick/remainder 的互斥性是 CI 分层的前提，而它是该前提的可运行定义。
# 不并行：条数极少，起 worker 的固定成本（每个约 1-2s import）反而更大。
test-remainder:
	$(PYTEST) -m "not slow and not external and not unit and not service" -q --tb=short

test-full:
	$(PYTEST) -q

# F1：外部 E2E 显式 opt-in（真实 LLM/DB，受保护凭据/受信 ref 才能跑）
test-external:
	conda run -n $(CONDA_ENV) RUN_EXTERNAL_TESTS=1 python -m pytest -m "external" -q --tb=short

# S2-R6-1：真实 MongoDB 测试层（独立 marker，不进任何默认档位）。
# RUN_REAL_MONGO_TESTS=1 让"连不上"变成 **fail** 而不是 skip（显式要求了就得做到）；
# 库名必须含 test——本层会 drop 整个库，指错库即数据事故（守卫见 tests/real_mongo/conftest.py）。
# 本地起库：docker compose -f docker-compose.test.yml up -d
test-real-mongo:
	conda run -n $(CONDA_ENV) RUN_REAL_MONGO_TESTS=1 \
		ELYSIA_TEST_MONGODB_URI=$${ELYSIA_TEST_MONGODB_URI:-mongodb://127.0.0.1:27018/elysia_test} \
		python -m pytest -m real_mongo -q --tb=short

# workflow_engine / workflow_contract 独立迭代门禁（不含 external——真实 LLM）
test-workflow-engine:
	conda run -n Elysia python -m pytest Module/workflow_engine/tests Module/workflow_contract/tests -m "unit or service" -q --tb=short

# 依赖锁（F2，统一 conda）：environment.yml 声明源 + environment.lock.yml 精确锁。
# 增删依赖：改 requirements.txt / environment.yml → 重新生成锁 → 提交。
lock-env:
	conda env export -n $(CONDA_ENV) > environment.lock.yml
	@echo "环境锁已重新生成 environment.lock.yml——确认 diff 后提交（含 pip 精确版本）"

# 依赖与静态安全扫描（F2）：pip-audit（已知漏洞）+ Bandit（生产 Python 静态分析）
# PYSEC-2026-3447（setuptools 80.10.2 → 83.0.0）为已接受的已知约束：修复版 83 移除
# pkg_resources，而 webrtcvad 2.0.10 依赖它（requirements 钉 setuptools<81 即因此）。
# 迁移到 webrtcvad-wheels（免 pkg_resources）后再解除此豁免，见 todo.md。
#
# 判据（**写清楚，否则读的人会以为它覆盖 Low**）：pip-audit 0 项未豁免漏洞；
# bandit **Medium 及以上 0 项**（`-ll` = report level medium，退出码随之）。
# 现状：Low 19 项（B603/B607 subprocess 固定参数 17 + B112 try/except/continue 2），
# 逐项列在 CODE_REVIEW_REPORT.md 的残余清单里；它们是**已知基线、不阻断**——
# 要收紧到零容忍，需逐处加 `# nosec B603 B607` + 理由（或改绝对路径），属专项清理。
# 历史：本目标此前因 env 的 pip 26.1.2 自命中 PYSEC-2026-3721 而未进 CI；
# 升级 pip 26.2.1 + 重生成 environment.lock.yml 后接线（2026-09-16，§0.4.31 #10）。
audit:
	conda run -n $(CONDA_ENV) pip-audit --ignore-vuln PYSEC-2026-3447
	conda run -n $(CONDA_ENV) bandit -r core/ Module/ frontend_contract/ scripts/ -q -c bandit.yaml -ll

check:
	conda run -n $(CONDA_ENV) python app.py --check

# 导入风格门禁：禁相对导入（TID252），__init__.py / __main__.py 豁免。见 ruff.toml
# F4：scripts/ 纳入 lint（此前遗漏 17 个 ANN）。
lint:
	ruff check core/ Module/ frontend_contract/ scripts/ --exclude __pycache__

# 类型检查门禁（pyright，全仓生产代码 core + Module + frontend_contract + scripts；须 Elysia env 运行——base env 无 aiohttp 会漏报 25 处）
pyright:
	conda run -n $(CONDA_ENV) pyright

# 前后端契约单源：Pydantic 模型 → contract.generated.ts + contract.schema.json + contract.raw.ts
gen-types:
	python -m frontend_contract

# 契约层 golden frames：ServerMessage 契约模型 → docs/archive/frontend-handoff-v2/traces/ws-v2-*.jsonl
gen-frames:
	python scripts/gen_contract_frames.py

# 漂移检测：ts/schema/raw_ts + frames 生成到临时文件，git diff 校验无漂移
# （改契约不跑 gen-types / gen-frames = CI 红）
check-types:
	python -m frontend_contract --check
	python scripts/gen_contract_frames.py --check
	python scripts/check_error_codes.py

# Compose 安全策略门禁（P1-19）：数据服务仅 loopback 端口、无默认凭据/空 token、
# 生产不发布数据库管理端口。无需 docker/凭据即可运行（静态解析 compose 文件）。
check-compose:
	conda run -n $(CONDA_ENV) python scripts/check_compose_security.py

# 环境锁门禁（F2）：requirements*.txt 的每个顶层包都必须在 environment.lock.yml 里
# 有精确 pin 且版本满足声明。纯 stdlib、~0.1s。
# 存在理由：锁与声明漂移时，"从锁建 env"与"按声明装包"会得到两个不同的环境——
# 而那正是复现失败最贵的一种。2026-09-16：本门禁此前**只**在已删除的 PR job 里跑，
# 于是它一度没有任何自动执行点（见 docs/ci.md）。
check-env-lock:
	python scripts/check_env_lock.py

# 架构边界门禁（ARCH-INC-P1-03）：依赖方向（Module→core / 零依赖层 / 领域层→input）
# + 跨包私有符号 + eager import 环。纯 AST、无外部依赖、秒级。
# 存量泄漏在脚本内 allowlist 逐项登记理由，新增即红。
check-arch:
	python scripts/check_architecture.py

# 超时预算门禁（#47 ①）：R1 词法嵌套递减 / R2 登记的不变量（外层 ≥ 内层）/
# R3 具名预算必须表态（登记为外层 或 叶子+理由）。纯 AST、~1s。
# 存在理由：S2-A3 那类「外层 5s < 内层 17s」的缺陷此前只写在注释里，注释不是机制。
check-timeouts:
	python scripts/check_timeout_budget.py

# 契约可达性门禁（#47 ②）：枚举成员必须有生产者（或在册预留）、字段有读必须有写
# （或在册缺口）。纯 AST、~3s。
# 存在理由（RC-12）：pyright/check-types 只校形状，不会问「这个字段/枚举值真的有人生产吗」。
check-contract:
	python scripts/check_contract_reachability.py

# Debug 前端凭据守卫（S2-F3）：`/debug/api/*` 只认 SERVER_AUTH_TOKEN，
# 所有请求必须经 debug/auth.ts::debugFetch 带 Authorization 头；
# EventSource 协议上无法设置请求头，故一律禁用（SSE 走 debug/sse.ts）。
check-debug-auth:
	python scripts/check_debug_auth.py

# Secret scanning（CR-01 步骤 4）：扫**被跟踪文件**里的凭据形态（key/Bearer/私钥/
# `.env` 入库/文档抄录赋值）。纯 stdlib、秒级，进 CI push 侧静态门禁
# （`ci.yml` 的 `backend` job）阻断——PR 门禁已于 2026-09-16 移除，见 docs/ci.md。
check-secrets:
	python scripts/check_secrets.py

# 全历史 secret scanning（CR-01 步骤 3 的验收器）：**在清历史完成前预期为红** ——
# 2026-06 的 `docs/review-2026-06-01.md` 在 commit f1dcc803 里抄录了真 key。
# 这条不是门禁、是"历史是否已清理"的判据：清完 `git filter-repo` 后应转绿。
# **须完整 clone**：浅克隆（CI 的 checkout 是 `--depth 1`）里只看得到最后一个提交，
# 脚本退出 2 拒答而不是报绿——"没扫到"与"干净"不是一回事。
check-secrets-history:
	python scripts/check_secrets.py --all-refs

# 测试集合代数门禁（2026-09-14 CI 性能专项）：证明 Quick ∪ remainder == Normal
# 且 Quick ∩ remainder == ∅——即"没有测试被漏掉、没有被跑两遍"。
# 需要独立收集（3 次 `pytest --collect-only`，约 35s）才能验证**表达式**本身。
# **必须走 conda run**：本目标内部会起 pytest，用裸 `python` 会落到 runner 的基础
# 解释器（另一份 pytest 与插件集）——那就成了"用别的环境验证这个环境"（判据假绿③）。
# marker 纪律（无 marker / 快慢层交叠）由根 `conftest.py::pytest_collection_modifyitems`
# 在**每个** pytest 进程内零成本断言，见那里的 docstring。
check-test-sets:
	conda run -n $(CONDA_ENV) python scripts/check_test_sets.py

# CI 信任边界门禁（P0-02）：PR 可达 job 禁 self-hosted / 禁宿主 .env 路径 /
# 禁死 job（`if` 守卫引用未在 `on:` 声明的事件）。默认扫描全部 workflow 文件。
check-ci-isolation:
	python scripts/check_ci_isolation.py

start:
	bash scripts/start.sh

check-full:
	conda run -n $(CONDA_ENV) python app.py --check --full
