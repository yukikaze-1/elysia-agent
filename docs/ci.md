# CI 架构与信任边界

> 文档导航：[项目文档](../README.md) → [docs 索引](README.md) → 本文

本文是 **CI 的唯一权威说明**：job 布局、为什么全部跑在 self-hosted 上、信任边界规则、
以及**开放 PR 时的恢复清单**。`ci.yml` / `contract-frames.yml` 的文件头注释只保留要点并
指向本文——避免同一份清单在 YAML、`docs/testing.md` 里各存一份然后漂移。

## 现状（2026-09-16 起）

**两个 workflow 都只监听 `push: [main]`，全部 job 跑在 self-hosted runner 上。**

| workflow | job | 触发 | runs-on | 内容 |
|---|---|---|---|---|
| `ci.yml` | `backend` | push | `self-hosted` | 静态门禁（lint / pyright / 契约漂移 / 架构 / 超时预算 / 契约可达性 / debug 鉴权 / CI 隔离 / 密钥扫描 / 集合代数 / compose 安全策略 / 环境锁 / 依赖与静态安全扫描 `make audit`）+ 离线用例 `make test-normal` |
| `ci.yml` | `real-mongo` | push | `[self-hosted, Linux, X64]` | 真实 MongoDB 层：job 内自起 `docker-compose.test.yml`，`make test-real-mongo`，收尾 `down` |
| `ci.yml` | `frontend` | push | `[self-hosted, Linux, X64]` | `npm ci` + `vue-tsc --noEmit && vite build` |
| `contract-frames.yml` | `frames` | push（路径过滤） | `[self-hosted, Linux, X64]` | golden frames + 契约三产物漂移检查 |
| `nightly.yml` | `full` / `external` | `workflow_dispatch` | `self-hosted` | 手工触发的全量 / 真实 LLM·DB E2E |

**已移除（2026-09-16）**：`ci.yml` 的 `pr-gate`（PR 侧静态门禁）与 `frontend-pr`
（Frontend PR 侧），以及两个 workflow 的 `pull_request` 触发器。

> ⚠️ **删 job 会带走它独有的步骤**：`pr-gate` 里有两步在 push 侧**没有对应物**——
> `check_env_lock.py` 与 `make audit`。删掉它俩之后，这两道门禁**没有任何自动执行点**，
> 而无人察觉。现状见下节（一道已接回，一道刻意不接）。

## 门禁的接线：声明 ≠ 执行

`Makefile` 里的门禁目标必须在某个 workflow 里**真的被调用**。"声明了却没接线"与
"永远不触发的门禁"是同一类假绿——它比没有门禁更糟，因为它会被人当成已经有网了。
2026-09-16 机械核对（逐目标 grep 全部 workflow）实测出三处：

| 门禁 | 原状 | 处置 |
|---|---|---|
| `check-compose` | **从未**接过 CI，只在本地手跑 | ✅ 已接进 `backend`（纯静态解析、~0.1s） |
| `check-env-lock` | 只活在已删除的 `pr-gate` 里，随删 PR 被静默带走 | ✅ 已接进 `backend`，并补上 `make check-env-lock` 目标——此前**只有脚本、没有目标**，这正是它容易掉队的原因 |
| `audit`（pip-audit + bandit） | 同上，随 `pr-gate` 一起消失 | ✅ 已接进 `backend`（2026-09-16），见下 |

**`audit` 的接线（2026-09-16 完成）**：原状是 `make audit` 在自托管 env 上为红——
Elysia env 的 `pip 26.1.2` **自身**命中 `PYSEC-2026-3721`（修复版 26.2），而
`environment.lock.yml` 也 pin 着 26.1.2；接线会让 main 因**与本仓提交无关**的原因变红。
处置：升级 env 的 pip → `26.2.1` + `make lock-env` 重生成锁 → 接进 `backend` job
（**没有**用 `--ignore-vuln` 消除红色）。

> ⚠️ **豁免里写的解除条件不完整（实测发现，值得记住）**：只升级 pip 并不足以让
> `make audit` 变绿——`bandit` 对**任何**发现（含 Low）都返回退出码 1，而本仓有 19 项
> Low（B603/B607 subprocess 固定参数 17 + B112 try/except/continue 2）。照原解除条件
> 直接接线，CI 仍然红，只是红的原因换了一个。
> 现在的判据**写在 `Makefile` 里**：pip-audit 0 项未豁免漏洞 + bandit **Medium 及以上
> 0 项**（`-ll`，与本项目一贯声明的"0 Medium / 0 High"一致）；19 项 Low 是**已知基线**，
> 逐项列在 `CODE_REVIEW_REPORT.md` 的残余清单，要收紧到零容忍需逐处 `# nosec` + 理由。

> 这条纪律由 `scripts/tests/test_check_ci_isolation.py` 的
> `TestRealRepoWorkflows::test_every_declared_gate_is_wired_to_ci` 机械强制：新门禁必须
> 接线，或进该文件的 `_GATES_NOT_IN_CI` 写明理由与解除条件；**登记陈旧也会红**
> （已接线却还挂着豁免，等于让"已豁免"的理由继续冒充事实）。

## 为什么全部走 self-hosted：账单

GitHub 托管 runner **按分钟计费**。账号额度/账单被卡之后，这类 job **根本不会被分发**——
它在 0～3 秒内"失败"，注解原文：

> The job was not started because recent account payments have failed or your spending limit
> needs to be increased. Please check the 'Billing & plans'…

这**与代码无关**，却会给每次运行挂一个红叉。那比没有这个 job 更糟：**它训练人忽略红色**，
而本仓的门禁文化正是靠"红 = 有事"支撑的。

同一个原因本仓已经踩过两次，处置办法也一样（push 侧迁 self-hosted）：

- 2026-09-14 `frontend` job（见 git 历史里那次 Frontend 迁移批次）；
- 2026-09-16 `real-mongo` job（托管 `services:` → job 内 `docker compose`）；
- 2026-09-16 `contract-frames` 的 `frames` job。**这一条尤其不能拖**：它是 P0 契约漂移门禁，
  而它存在的原因正是"`ci.yml` 的 `paths-ignore: docs/**` 让 golden frames 的守卫永不执行、
  漂移无声放行"——用一个"起不来的门禁"替换一个"不触发的门禁"，等于没修。

**判据**：一个**永远不会启动**的门禁与一个**永远不触发**的门禁，在"给人虚假安全感"这件事上
是等价的。遇到前者，要么让它能跑，要么明确删掉并写清恢复条件——不要留着它挂红叉。

## 信任边界（`scripts/check_ci_isolation.py` 静态强制）

宿主上有**真实凭据**（`.env`）、**持久 workspace**（跨 job 不清理）与**真实用户身份**
（runner 以宿主用户运行）。因此：

- **`pull_request` 可达的 job 一律 GitHub-hosted**，且不得引用宿主绝对路径凭据。
  恶意 PR 的 `Makefile`/`pytest`/`npm ci` 的 postinstall 会在持久 runner 上以宿主权限执行
  任意代码——这是**明确拒绝**的取舍，不是遗漏。
- **只有 push 到 main（受信 ref）** 才允许 self-hosted + 宿主 Elysia env + 真实 `.env`。
- 门禁还管三件容易被绕过的事：① **死 job**（守卫引用的事件必须在 `on:` 里声明过——
  否则"写一个永不触发的守卫"就是绕过本门禁的静默通道）；② **表达式 `runs-on`**
  （`${{ vars.CI_RUNNER }}` 无法静态判定 → **拒答**，不默认它安全）；③ **self-hosted
  清理必须密闭、受守卫、fail-closed**：禁止裸 glob（不匹配 dotfile）、清空 workspace
  根目录必须带 `${GITHUB_WORKSPACE:?` 与拒绝 `/` 两道守卫、且清理命令的**退出码必须被
  显式检查**（`set -e` 不算——那会改变 retry loop 的控制流）。

**无 `if` 的 job 被按 PR 可达保守判定**（无守卫 = 任何事件都为真）。所以 self-hosted job
必须**显式**写 `if: github.event_name == 'push'`——判据方向是对的：不写清楚就当作不受信。

> ⚠️ **清理是承重的，不是卫生问题**：`backend` job 的 `Load .env` 明确依赖"上一步已把
> workspace 清空"，清理一旦失败或漏掉点文件，上一个 job 留下的凭据就会跨 job 泄漏给下一个。

## 开放 PR 时的恢复清单

**不是把 `pull_request` 加回去就完事。** 按顺序做完这五条：

1. **恢复两个 PR 侧 job**：
   - `pr-gate`（名 `PR Gate (backend lint + types + contract + quick)`）：lint / pyright /
     契约漂移 / check-arch / 超时预算 / 契约可达性 / debug 鉴权 / check-ci-isolation /
     check-secrets / 测试集合代数 / `pytest -m "unit or service" -x`。它曾是 PR 侧**唯一**
     的静态门禁。（`check-env-lock` 与 `check-compose` 现已常驻 push 侧 `backend`，
     PR 侧**不必**重复；`audit` 已于 2026-09-16 接进 push 侧 `backend`，PR 侧**不必**重复。）
   - `frontend-pr`（名 `Frontend (vue-tsc + build)`，**必须 `ubuntu-latest`**）：与 push 侧
     `frontend` 逐字等价的步骤。**PR 侧不得 self-hosted**——宿主上没有隔离/临时/untrusted 的
     PR runner。要自建化，必须先有 ephemeral / 容器 / VM 隔离 runner。
2. **恢复 `on: pull_request`**（`ci.yml` 与 `contract-frames.yml` 两处）。
3. **检查分支保护**（最容易忘、后果最硬）：required status check 按 **job 名**匹配，且
   **只在 PR 上评估**。当前 `Frontend (vue-tsc + build)` 与 `Real MongoDB (storage layer)`
   **都不在 PR 侧上报**；若它们仍挂在 required 列表里，PR 会**永远等不到上报而卡死**。
   开放前先到 Settings → Branches 对齐（要么补回同名 job，要么取消勾选）。
4. **跑 `make check-ci-isolation`**：静态校验信任边界（含上面那三条易绕过项）。
5. **想清楚账单**：PR 侧要覆盖就只能用 GitHub-hosted（见下节）。若额度仍是问题，
   替代路径是先建**隔离 runner**（ephemeral / 容器 / VM），再谈把 PR 侧自建化。

## job 内为什么这么写（几条不是洁癖的约定）

- **checkout 用 `find` 而不是 glob、且显式检查退出码**：shell glob `*` 不匹配前导点，
  实测在 runner 的持久 workspace 里留下过 `.pytest_cache` / `.ruff_cache` / `.env`；
  `|| true` 会把真实失败吞掉（`.git` 侥幸留下 → `git remote add` 报 "already exists" →
  四次重试全部同样失败，却被归因成网络）。
- **不用 `set -e`**：它属于"两件事都变了"，会改动 checkout retry loop 与容器收尾的**控制流**
  （里面有故意允许失败的命令），所以门禁要求**显式**判退出码。
- **`real-mongo` 等 healthy 才跑用例**：`RUN_REAL_MONGO_TESTS=1` 的语义是"连不上就 **fail**"，
  但"容器还没起来"会把 fail 变成一串连接错误；等不到 healthy 就 fail-closed 退出，
  **绝不退化成 skip**（那正是"环境不满足"型假绿）。
- **收尾走 `always()` + 显式退出码 + 断言容器确实没了**：`down` 返回 0 不等于清理生效，
  残留会污染下一次运行。
- **部分 job 刻意不加载 `.env`**（`frontend` / `real-mongo` / `frames`）：它们零凭据需求，
  少一个 `.env` 就少一条外泄路径。只有 `backend` 加载（受信 push ref + 已密闭清理）。
- **唯一一个 runner**：本仓只注册了 1 个 self-hosted runner，所以"并列 job 并行跑"是假的
  ——见 `docs/testing.md` 的 CI 性能专项，那批把 `Quick` + `Normal` 合并成了单个 `backend`。

## 相关文档

| 文档 | 内容 |
|---|---|
| [testing.md](testing.md) | 测试分层体系与档位命令（Quick / Normal / remainder / full / real-mongo） |
| [credential-handling.md](credential-handling.md) | 凭据处理规范与泄露响应（`check-secrets` 门禁与已知边界） |
| [../.github/workflows/ci.yml](../.github/workflows/ci.yml) | 工作流本体（文件头保留要点并指向本文） |
| `scripts/check_ci_isolation.py` | 信任边界门禁实现（含各条规则的理由） |
| `make check-ci-isolation` | 本地等价命令（纯静态解析，秒级） |
