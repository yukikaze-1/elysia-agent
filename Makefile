.PHONY: test test-module test-core test-tools test-all test-quick test-normal test-full test-external test-workflow-engine lint pyright lock-env audit start gen-types gen-frames check-types check-compose check-arch check-debug-auth check-timeouts check-contract

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
	# F1：真正全仓非 external pytest——合并门禁的可复现全量（旧"部分仓"语义由 test 承接；
	# 真实 LLM/DB 外部 E2E 经 external marker 排除，独立 opt-in）
	$(PYTEST) -m "not external" -q --tb=short

# 测试分层命令
# F1：external marker 取代 --ignore——WorkflowEngine unit/service 入合并门禁，
# 真实 LLM/DB 集成测试标 external、独立 opt-in（test-external）。
test-quick:
	conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short

test-normal:
	conda run -n Elysia python -m pytest -m "not slow and not external" -q --tb=short

test-full:
	conda run -n Elysia python -m pytest -q

# F1：外部 E2E 显式 opt-in（真实 LLM/DB，受保护凭据/受信 ref 才能跑）
test-external:
	conda run -n Elysia RUN_EXTERNAL_TESTS=1 python -m pytest -m "external" -q --tb=short

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
audit:
	conda run -n $(CONDA_ENV) pip-audit --ignore-vuln PYSEC-2026-3447
	conda run -n $(CONDA_ENV) bandit -r core/ Module/ frontend_contract/ scripts/ -q -c bandit.yaml

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
# `.env` 入库/文档抄录赋值）。纯 stdlib、秒级，进 CI pr-gate 阻断。
check-secrets:
	python scripts/check_secrets.py

# 全历史 secret scanning（CR-01 步骤 3 的验收器）：**在清历史完成前预期为红** ——
# 2026-06 的 `docs/review-2026-06-01.md` 在 commit f1dcc803 里抄录了真 key。
# 这条不是门禁、是"历史是否已清理"的判据：清完 `git filter-repo` 后应转绿。
# **须完整 clone**：浅克隆（CI 的 checkout 是 `--depth 1`）里只看得到最后一个提交，
# 脚本退出 2 拒答而不是报绿——"没扫到"与"干净"不是一回事。
check-secrets-history:
	python scripts/check_secrets.py --all-refs

start:
	bash scripts/start.sh

check-full:
	conda run -n $(CONDA_ENV) python app.py --check --full
