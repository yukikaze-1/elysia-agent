# 凭据处理规范与泄露响应

> 文档导航：[项目 README](../README.md) → docs 索引

本文是 **CR-01**（S0：真实 DeepSeek key 明文存在于公开 git 历史且三个月未轮换）的产物。
它回答三件事：**凭据放哪**、**怎么引用**、**泄露了按什么顺序处理**。

---

## 一、凭据放哪

| 载体 | 允许 | 说明 |
|---|---|---|
| `.env`（工作树，权限 600） | ✅ | **唯一**的运行时明文位置。`.gitignore` 覆盖，从未入库 |
| 环境变量（systemd / docker / CI secret） | ✅ | 生产首选；`ConfigField(env=...)` 读取，优先级 env > YAML > 默认值 |
| `config.yaml` | ❌ | 会入库。`ConfigField(secret=True)` 会在加载期拒绝写入 |
| 代码常量 / 测试夹具 | ❌ | 真值一律走 env；测试用**显式占位符**（见下） |
| 文档 / review 报告 / commit message | ❌ | **本次事故的直接成因**——见 §三 |

运行所需凭据的**变量名**清单见 `.env.example`。它只列变量名与占位值，
**不含任何真实值**，是"换机器照它配"的唯一参照。

---

## 二、怎么引用（文档与代码）

**规则：只写变量名 + 掩码，永不写值。**

```text
✅ DEEPSEEK_API_KEY="sk-****"（值见 .env，不入库）
✅ MILVUS_TOKEN 未设置（弱口令，见 §四 步骤 2）
❌ DEEPSEEK_API_KEY="sk-d6cdb4…"      ← 本次事故的形态
```

掩码规则：保留不超过 **6 个**前缀字符 + `****`。这里刻意连"泄露值的前缀"也只在
`CODE_REVIEW_REPORT.md` / 台账里作为**指纹**出现（为了让扫描器能认出来），
其余文档一律用 `sk-****`。

**为什么这条规则是硬的**：2026-06 那次泄露的载体本身是一份「发现 `.env` 被提交进
仓库」的 review 文档——它把明文 key 又抄了一遍，从"发现了问题"变成"散布了凭据"。
**报告里出现值，等于把凭据再发布一次。**

### 测试/夹具里的占位符

`scripts/check_secrets.py` 的判定是"**像真值**"：长度 ≥ 8、非掩码、非模板变量、
不含 `your/xxx/test/changeme/redacted/...`。写占位符时用这些词即可，例如：

```python
os.environ.setdefault("DEEPSEEK_API_KEY", "test")     # ✅ 放行
loader(api_key="<your-key>")                          # ✅ 放行
loader(api_key="sk-1234567890abcdefghij")             # ❌ 会被拦下（像真值）
```

---

## 三、泄露了怎么办（**顺序不可颠倒**）

> 顺序错了就白做：清历史需要时间，而**清历史不会让已泄露的 key 失效**——
> 任何已有 clone / fork / GitHub 缓存对象都保留着。**先止损，再清洁，最后补门禁。**

### 步骤 1 — 立即吊销并轮换（owner 执行，分钟级）

到对应控制台吊销并重新签发：

- **DeepSeek**：API keys 页面 → 删除 `sk-d6cdb4…` → 新建 → 只写入 `.env`
- 判定是否已轮换：比对**哈希**（不打印明文）

  ```bash
  conda run -n Elysia python - <<'PY'
  import hashlib, os
  from dotenv import dotenv_values
  live = dotenv_values(".env").get("DEEPSEEK_API_KEY", "")
  print("live len:", len(live), "sha256[:16]:", hashlib.sha256(live.encode()).hexdigest()[:16])
  PY
  # 与报告 §CR-01 记录的泄露值哈希不同 ⇒ 已轮换
  ```

### 步骤 2 — 轮换弱口令，并核查同族

- `MILVUS_TOKEN="root:Milvus"` 是**默认口令**，已随历史公开 → 换强口令
- 核查 Mongo / MinIO 是否也用默认凭据（`docker-compose.yml` 的默认值只应存在于本地开发）

### 步骤 3 — 清理 git 历史（需要 force-push，owner 执行）

```bash
# 0) 备份（filter-repo 是破坏性操作）
git clone --mirror . /tmp/elysia-backup.git

# 1) 装 git-filter-repo（pip 或系统包），然后按文本替换清理
#    把泄露值换成掩码；表达式写进文件，避免明文进 shell history
cat > /tmp/replacements.txt <<'EOF'
sk-d6cdb4==>sk-****REDACTED****
EOF
git filter-repo --replace-text /tmp/replacements.txt

# 2) 验收：必须为 0 命中（须**完整 clone**——浅克隆下脚本退出 2「无法判定」，不报绿）
python scripts/check_secrets.py --all-refs     # 或 make check-secrets-history

# 3) 强推（会改写全部 commit hash）
git push --force --all
git push --force --tags

# 4) 通知所有协作者重新 clone（旧 clone 里的历史仍含明文）
```

> GitHub 侧：fork 与你仓库的对象缓存可能仍保留；这是**步骤 1 必须在前**的原因。
> 若需要，联系 GitHub Support 清理缓存对象。

### 步骤 4 — 补门禁（已落地）

- `make check-secrets` / CI pr-gate 的 `Secret scanning (CR-01)` 步骤：扫**被跟踪文件**
  里的凭据形态（含本次事故的 key 指纹）
- `make check-secrets-history`：**全历史**判据。清历史完成前**预期为红**——它不是
  PR 门禁，是"步骤 3 是否做完"的验收器（浅克隆下退出 2 拒答；清完 `git filter-repo`
  后转绿，测试的期望值由 `git log -S` 现算，不写死"预期为红"）
- 本次同时修正 `bandit.yaml` 里 B105/B106/B107 的豁免**理由**：原文写"测试密钥占位符"
  不准确（bandit 本就排除 tests/），实测解除后 27 项全是误报（按标识符名猜，命中的是
  参数名 `_config_secret`、枚举值 `token_budget_exceeded`、集合名 `sessions` 等）。
  真正的覆盖由上面的扫描器承担

### 步骤 5 — 规范（本文）

文档引用只在"变量名 + 掩码"层面；`docs/` 全历史已扫描，泄露面被界定为
**一个 key + 一个弱口令**（21,024 个 blob / 186MB 单遍扫描；`tvly-*` 0 命中、
PEM 私钥 0 命中）。

---

## 四、门禁能力与**已知边界**

`scripts/check_secrets.py` 覆盖：

| 规则 | 例子 |
|---|---|
| 已知泄露指纹 | `sk-d6cdb4`（本次事故）|
| 形态 | `sk-…` / `tvly-…` / `ghp_…` / `AKIA…` / `xox[baprs]-…` / PEM 私钥 / 硬编码 `Bearer` |
| 赋值 | `*_API_KEY=*` / `*_TOKEN=*` / `*_SECRET=*` / `*_PASSWORD=*` 后跟字面量 |
| 载体 | 被 git 跟踪的 `.env`（`.env.example` 除外）|

**明确的边界（不假装完备）**：

- 它按**形态**匹配，不是熵分析——自定义格式的密钥（无前缀、无赋值语法）会漏；
  更强的方案是 `gitleaks detect --log-opts="--all"` 或 `trufflehog git --only-verified`，
  需要引入外部二进制（本仓门禁偏好零依赖 + 可在 PR job 跑的纯 stdlib 脚本）
- 它**不扫** `data/`、`log/`、`node_modules/`、`static/assets/`
- `--all-refs`（全历史）在**浅克隆下拒答**：退出码 2、不打印通过标记。CI 的 checkout
  就是 `--depth 1`（self-hosted `git fetch --depth 1`、GitHub-hosted
  `actions/checkout@v4` 默认亦然），那里 `rev-list --all` 只看得到最后一个提交，
  "无命中"只说明没扫到。旧版正是在这种环境里对 2026-06 那次泄露报了"✓ 通过"
  （2026-09-14 CI 红暴露；同一条测试还写死了"预期为红"，清历史后会反过来永远红）
- 占位符判定是启发式：新写占位符若"看起来像真值"会被拦下——**这是有意为之**，
  修法是改占位符而不是加白名单

---

## 五、相关

- 事故记录与完整证据：`CODE_REVIEW_REPORT.md` §CR-01；过程台账
  `docs/review/2026-09-12-code-review-ledger.md`
- 安全总览：`docs/electron-backend-security.md`
- 环境变量清单：`.env.example`
