# superpowers/ — 设计规格与实施计划（全部已归档）

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → 本文

> ⚠️ **全部已 archive。当前 authority 不在本目录。**
> 这里记录的是**当时的**设计意图与实施步骤；规格落地后代码可能已经偏离规格
> （或者规格本身被后续批次修正）。**当前事实**看：
> [architecture.md](../architecture.md) · [architecture/invariants.md](../architecture/invariants.md) ·
> [electron-backend-protocol-v2.md](../electron-backend-protocol-v2.md) ·
> [todo.md](../../todo.md) · 各子系统 README。

## 目录结构

```
docs/superpowers/
├── specs/                      # 顶层为空——所有设计规格都已归档
│   └── archive/<YYYY-MM>/      # 按文件名日期前缀分月（2026-06 ~ 2026-09）
└── plans/                      # 顶层为空——所有实施计划都已归档
    └── archive/<YYYY-MM>/      # 同上
```

| 类型 | 命名（约定） | 内容 |
|---|---|---|
| **spec** | `YYYY-MM-DD-<主题>-design.md` | 设计规格：问题、方案、取舍、不变量、验收判据 |
| **plan** | `YYYY-MM-DD-<主题>-plan.md`（推荐后缀） | 实施计划：任务分解、执行顺序、逐项验收 |

**命名不是强制的**：历史上 spec 有 18 份没有 `-design.md` 后缀（尤其整个
`electron-backend-batch0..8c` 家族），plan 里多数也没有 `-plan.md`。
**plans 常常内嵌 spec 内容**——很多特性只有一份 plan，设计就写在里面。
所以查历史设计时**两类都要搜**，不要只搜 `-design.md`。

跨月特性（如 character stage a–g）整串归入**收口月**，不拆散；月份目录按
**文件名日期前缀**定，不按文件内容。

## 怎么查历史设计

```bash
# 按主题搜（文件名 + 正文一起搜）
grep -rl "关键词" docs/superpowers/ | sort

# 按月份看某段时间的全部设计
ls docs/superpowers/specs/archive/2026-08/

# 找"某个现在还在用的机制"当初怎么定的
grep -rln "机制名" docs/superpowers/specs/archive/
```

**为什么这些"归档"文档仍然值得读**：它们不只是历史——**生产代码的 docstring 直接引用
它们作为设计基线**（`core/text/{parts,tags,splitter,tokenizer,stream_processor}.py`、
`core/storage/session_repo.py` 等）。换言之：**"已归档" ≠ "与当前实现无关"**，
但它也**不**代表当前事实——两者要分开看。

> 例：`specs/archive/2026-08/2026-08-01-agent-public-readonly-interface-design.md`
> 是 `Agent` 公开接口的设计依据，仍然被 [architecture.md](../architecture.md) §4.2.1
> 与 [CLAUDE.md](../../CLAUDE.md) 引用；但**当前**的 `@property` 计数与枚举权威在
> `core/README.md`（由 `core/tests/test_doc_counts.py` 机械核对）。

## 状态标注

本目录的文档**不加**逐文件 banner（316 个文件，机械加 banner 只会制造噪音）；
"这是历史"由本 README + [docs/README.md](../README.md) 的 Historical Records 节统一声明。
如果你需要判断某份 spec 是否已实施：看 [todo.md](../../todo.md)（未实施项在册）
与 [`docs/architecture.md`](../architecture.md)（当前实现）。

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [../README.md](../README.md) | docs 索引；含「未实施的设计稿」清单（task-system 两篇 + provider runtime） |
| [../review/README.md](../review/README.md) | 审查报告与 correctness hardening 执行记录（**另一类**历史） |
| [../archive/README.md](../archive/README.md) | 更早的时代归档（Electron 契约 / TaskSystem / 迁移 / 交接） |
| [../architecture.md](../architecture.md) | **当前**系统架构权威 |
