# 阶段 3 真实联调后端整改：sticker 搜索 + 日期 fail-fast + 逐文件 kind

**文档版本：** v0.1
**文档状态：** 待评审
**日期：** 2026-08-15
**仓库：** `/home/yomu/elysia-agent`
**上游评估：** `docs/review/archive/2026-08/2026-08-15-阶段3真实联调后端整改清单.md`（BE-S3-INT-P0-01 / P0-02 / P1-01）
**前置决策：** 会话讨论敲定 D1-D4（用户定案 D3=B 契约层枚举，其余按推荐）；D5-D9 按推荐定案

---

## 1. 背景

阶段 3 真实联调（B-02 历史/搜索、B-03 附件/多媒体）前，后端存在三处确定性契约错位。前端
契约已冻结（前端清单 `2026-08-15-阶段3真实联调前端整改清单.md`），后端需补齐：

1. **BE-S3-INT-P0-01 贴图历史**：前端 `HistoryCategory.sticker → category=sticker` 已冻结，
   但后端搜索 category 白名单（`session_routes.py:653`）与 repo 的 `_CATEGORY_PART_TYPES`
   （`message_repo.py:23`）均不含 `sticker` → 请求在 handler 层直接 400，贴图历史 tab 无法联调。
2. **BE-S3-INT-P1-01 日期 fail-fast**：后端日期契约为自然日 `YYYY-MM-DD` + IANA timezone，
   前端目标形态已冻结为自然日发送（FE-S3-INT-P0-01，**待前端落地**——当前前端
   `fetchHistoryStream` 经 `localDateToHalfOpenRange`（history-date.ts:23-28）发送的是
   UTC ISO 半开区间如 `2026-08-13T16:00:00.000Z`）；后端对非法日期格式当前是**吞成 200
   空结果**（`query_search` catch 异常返回空），需在 handler 层显式 fail-fast，防契约漂移
   被误判为「没有历史」。**时序约束**：P1-01 必须与前端 FE-S3-INT-P0-01 同步放行，不得
   单独先上线——当前前端发 ISO 时间戳，若后端先行，既有日期筛选查询会从「200 空结果」
   变「400 CONTRACT_VALIDATION」，功能性回归。
3. **BE-S3-INT-P0-02 混合附件逐文件 kind**：前端 `uploadAttachments()` 每文件追加
   `kind, files` 对（real-backend.ts:607-613），后端只保存一个标量 `kind`——新 kind 覆盖旧值，
   整个批次用最后一个 kind 预检/消歧 → 混合批次误分类、误触发大小限制。

## 2. 目标

- 搜索端点接受 `category=sticker`，保持现有 keyset cursor / conversationId / 日期 / 关键词
  组合语义与 `SearchMessageItem` 响应形状；
- 日期参数在 handler 层 fail-fast：非 `YYYY-MM-DD` → 400 `CONTRACT_VALIDATION`，未知时区 →
  400 `INVALID_TIMEZONE`（现有）；不再吞成 200 空结果；**与前端 FE-S3-INT-P0-01 同步放行**
  （后端先行会使当前发 ISO 时间戳的前端日期查询回归 400）；
- `/api/uploads` 按每文件 `kind` 对齐前端，兼容旧「单 kind + 多文件」形态，保持整批原子性；
- category/kind 白名单收敛为**契约层共享枚举**（D3-B），消除三份字符串集合的维护陷阱；
- **零契约响应形状变更**：`SearchMessageItem`/`AttachmentInfo` 字段不变，前端 TS 快照不变。

## 3. 现状（事实基线）

### 3.1 P0-01 sticker 搜索

| 项 | 现状 |
|---|---|
| handler 白名单 | `session_routes.py:653`：`("text", "image", "video", "audio", "file", "link", "tool")`，未知 → 400 `CONTRACT_VALIDATION` |
| repo 集合 | `message_repo.py:23` `_CATEGORY_PART_TYPES = {"text", "image", "link", "video", "audio", "file"}`（加 sticker 前） |
| repo 分支 | `query_search:407-423`：`link` 特判 URL 正则；`category in _CATEGORY_PART_TYPES - {"text","link"}` → `{"type": category}`；`text and not query` → `{"type":"text"}`；`tool` → `role="tool"` |
| Part 类型 | `PartType.STICKER = "sticker"` 与 `StickerPart.type` 一致（`core/schema/part.py:28`）——sticker 加进集合后走通用 `{"type":"sticker"}` 分支，**无需新逻辑** |
| snippet | `_message_text` 只提取 TextPart → 纯贴图消息 `snippet=""`；契约 `SearchMessageItem.snippet: str`（非 Optional）——空串合法（清单 2.2.5） |
| role | 贴图消息经正常管线存档 role=user，handler 的 `isinstance(role, MessageRole)` 过滤不挡 |

### 3.2 P1-01 日期校验

| 项 | 现状 |
|---|---|
| 消费点 | `date_from/date_to` 全仓唯一消费：`session_routes.py` search handler（`:644-645`）→ `query_search` |
| 时区校验 | handler `:659-663` 已有：有日期时 `ZoneInfo(tz_name)` 失败 → 400 `INVALID_TIMEZONE` |
| 日期格式 | `message_repo._build_date_bounds:68-73` 用 `date.fromisoformat`；非法格式抛 `ValueError` → `query_search:437-439` catch **返回空结果**（200） |
| 转换 | 自然日 → 半开 UTC 区间；`fromisoformat` 额外接受紧凑（`20260814`）与 ISO 周历（`2026-W33-4`）等非 `YYYY-MM-DD` 形态（Elysia env 3.12 实测 `2026-8-14` 抛 ValueError），与 `YYYY-MM-DD` 契约不完全对齐 |

### 3.3 P0-02 逐文件 kind

| 项 | 现状 |
|---|---|
| 收集 | `upload_routes.py:142-192`：`kind = ""` 标量，`name == "kind"` 覆盖；`name.startswith("files")` 收集 → `files: list[tuple[str, bytes]]`（无 hint） |
| kind 白名单 | `_ALLOWED_KINDS = ("image", "video", "audio", "file")`（`upload_routes.py:380`），非法 → 400 `CONTRACT_VALIDATION`（`:195-197`） |
| 大小预检 | `:207` `if kind and len(data) > _KIND_LIMITS[kind]` → 413（整批同一 kind） |
| webm 消歧 | `:236-237` `final_kind = "audio" if kind == "audio" else "video"`（整批同一 kind） |
| 归位 | `_MIME_KIND[mime]`；`video/webm` 走 tiebreaker；归位后 `_KIND_LIMITS[final_kind]` 复核（`:240`） |
| 原子性 | 两阶段（阶段 A 全部校验 → 阶段 B 统一写盘/建记录/注册，失败清理）；逐文件 kind 不改此结构 |
| 前端形态 | `real-backend.ts:607-613`：`for f in files { if (f.kind) append('kind', f.kind); append('files', ...) }`——每文件一个 kind part 紧邻其 files；`uploadBatch`（upload-service.ts）经 `sniffKind` **总是填充 kind**（`kind?` 可选仅类型防御声明） |
| 旧形态 | 前端旧版本「单 kind + 多文件」：一个 kind part + N 个 files（清单 3.2.2 要求兼容） |

## 4. 方案

### 4.1 D3-B：契约层共享枚举（frontend_contract/rest.py）

在 `frontend_contract/rest.py`（StrEnum 区，`Role`/`FinishReason` 旁）新增两个 StrEnum：

```python
class SearchCategory(StrEnum):
    """GET /api/search/messages category 白名单（wire 值，8 取值）。"""
    TEXT = "text"
    IMAGE = "image"
    LINK = "link"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"
    TOOL = "tool"
    STICKER = "sticker"


class UploadKind(StrEnum):
    """POST /api/uploads kind 字段合法值（4 取值；sticker 贴图走 /api/stickers，不落附件）。"""
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"
```

**关键约束**：两枚举**不被任何 ContractModel 字段引用**——`gen_ts._referenced_enums`
（gen_ts.py:111-126）只收集被字段引用的枚举，故 TS 快照（contract.generated.ts / schema /
raw_ts）**不变**、前端零影响。枚举仅作为共享白名单单源被 core 消费（类型权威归属：wire
枚举值 → 契约层）。

**白名单统一用「值集合」** `frozenset(m.value for m in SearchCategory)`，不用类成员 `in`
（`category in SearchCategory` 在 Python 3.11 为 False / 3.12 为 True，有版本差异）；wire
`category`/`kind` 是裸 str，与枚举值集合成员判断无版本问题。分支比较可用
`category == SearchCategory.LINK`（StrEnum 继承 str，`==` 与裸 str 相等，跨版本安全）。

消费方改造：

| 消费方 | 现状 → 改造 |
|---|---|
| `session_routes.py:43` `_RESOURCE_CATEGORIES` | `frozenset(m.value for m in SearchCategory) - {"text", "tool"}`（6 值：image/link/video/audio/file/sticker，派生单源） |
| `session_routes.py:653` search 白名单 | `category not in _SEARCH_CATEGORY_VALUES`（模块级 `frozenset(m.value for m in SearchCategory)`，8 值含 sticker） |
| `message_repo.py:23` `_CATEGORY_PART_TYPES` | `frozenset(m.value for m in SearchCategory) - {"tool"}`（7 值：text/image/link/video/audio/file/sticker）；`query_search:410-423` 分支比较可改枚举（`category == SearchCategory.LINK`），`_CATEGORY_PART_TYPES - {"text", "link"}` 得 image/video/audio/file/sticker → `{"type": category}` |
| `upload_routes.py:380` `_ALLOWED_KINDS` | 替换为 `_UPLOAD_KIND_VALUES = frozenset(m.value for m in UploadKind)`（4 值），逐文件 hint 校验用 |

### 4.2 P0-01：搜索支持 sticker

在 4.1 枚举化基础上，`category=sticker` 自动生效（无需新分支）：

1. handler 白名单改 `_SEARCH_CATEGORY_VALUES`（8 值）→ `sticker` 放行；
2. `_CATEGORY_PART_TYPES` 含 `sticker` → `query_search` 走 `{"type": "sticker"}` $elemMatch；
3. `category=sticker` + `query` 关键词 → 拆两条 $elemMatch 用 $and（`文字+贴图` 消息 text part
   命中关键词 + sticker part 存在；纯贴图消息**有** query 时不命中——无 text part 无法命中
   关键词，合理语义）；
4. `snippet` 空串合法（前端按文本行 + category chip 展示，清单 2.2.5）；
5. **契约零改动**：`SearchMessageItem` 不加 kind/url/stickerId（清单 2.2.4）。

### 4.3 P1-01：日期参数 fail-fast

`session_routes.py` search handler 增加日期格式校验（置于现有 `ZoneInfo` 校验前）：

```python
def _validate_date_param(name: str, value: str) -> str | None:
    """严格校验 date_from/date_to。合法返回 None，非法返回公开错误消息。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return f"{name} 必须为 YYYY-MM-DD（收到 {value!r}）"
    try:
        date.fromisoformat(value)
    except ValueError:
        return f"{name} 不是有效日期（收到 {value!r}）"
    return None
```

handler 内（`ZoneInfo` 校验之前）：

```python
for name, value in (("date_from", date_from), ("date_to", date_to)):
    if value:
        err = _validate_date_param(name, value)
        if err:
            return _error_response("CONTRACT_VALIDATION", err, 400)
```

- 双重校验：`^\d{4}-\d{2}-\d{2}$` 正则拒非 `YYYY-MM-DD` 形态（紧凑 `20260814` / ISO 周历
  `2026-W33-4` / ISO datetime `2026-08-13T16:00:00Z`），`date.fromisoformat` 拒非法日期
  （`2026-13-99`）；
- repo `_build_date_bounds` 保持防御 catch（handler 校验后非法输入不可达，双保险不删）；
- **date_from > date_to 保持空结果 200**（D2-A：前端日期选择器不产生倒置，纯防御不扩大范围）；
- 无日期（省略 `date_from/date_to`）不校验；未知时区 → 现有 `INVALID_TIMEZONE`。

需要新增 import：`from datetime import date`（session_routes.py 当前未导入）。

### 4.4 P0-02：混合附件逐文件 kind

`upload_routes.py` 收集段重构（标量 `kind` → 逐文件 hint）：

```python
pending_kinds: list[str] = []   # 未消费 kind 栈（LIFO：最近一个 kind 作用于其后首个文件）
kind_count = 0                  # 批内 kind part 总数（单 kind 特判判定）
files: list[tuple[str, bytes, str | None]] = []   # (原始文件名, 内容, kind_hint | None)
```

收集循环改动：

- `name == "kind"`：限长校验不变（`read_chunk(65)`，>64 → 400 `INVALID_REQUEST`）；解码后
  **邻接判定（MED-2）**：`pending_kinds` 非空时再遇 `kind` part → 400 `CONTRACT_VALIDATION`
  「kind 未紧邻文件」（前一 kind 未被文件消费即来新 kind，违反「kind 紧邻其后文件」，
  覆盖清单 3.3「顺序歧义 400」；前端严格 `kind,files` 交替不触发）；否则 `pending_kinds.append(decoded)`，
  `kind_count += 1`；
- `name.startswith("files")`：读文件逻辑不变；`hint = pending_kinds.pop() if pending_kinds else None`；
  `files.append((filename, bytes(data), hint))`（`startswith("files")` 同时兼容 `files[]`）。

收集后校验（kind 预检替换现有 `:194-197`；**保留现有 `:198-200` 文件数检查 MED-1**）：

1. `len(files) > 10` → 400 `TOO_MANY_FILES`（现有 `:198-200` 不动）；
2. `kind_count > 100` → 400 `CONTRACT_VALIDATION`「kind 字段数量超限」（D6 DoS 防御，前端最多 10）；
3. `pending_kinds` 非空（尾部悬空 kind）→ 400 `CONTRACT_VALIDATION`「kind 后缺少对应文件」（D5）；
4. **单 kind 特判（D1-A）**：`kind_count == 1 and files` → 全部文件 hint 统一为该 kind
   （旧形态「单 kind + 多文件」兼容，清单 3.2.2）。**此分支下 hint=None 路径不适用**——
   整批按该 kind 预检（MED-3：单 kind 一律视为旧形态整批统一，与 D1 决策一致）；
5. 每个 hint（非 None）校验 `hint not in _UPLOAD_KIND_VALUES` → 400 `CONTRACT_VALIDATION`
   「未知 kind」。

hint=None（魔数判定）路径仅对 `kind_count == 0`（全批无 kind）或 `kind_count ≥ 2` 时未紧邻
kind 的文件成立（MED-3 明确：单 kind 时整批统一，None 路径不可达）。

阶段 A 校验（`:205-242`）逐文件改用该文件的 hint：

| 行 | 现状 | 改造 |
|---|---|---|
| `:207` | `if kind and len(data) > _KIND_LIMITS[kind]` | `if hint and len(data) > _KIND_LIMITS[hint]`（逐 hint 预检；无 hint 跳过初步预检，归位复核兜底） |
| `:236-237` | `final_kind = "audio" if kind == "audio" else "video"` | `final_kind = "audio" if hint == "audio" else "video"`（无 hint 的 webm → video） |
| `:239` | `final_kind = _MIME_KIND[mime]` | 不变（魔数与提示冲突时以魔数归位为准，清单 3.2.5） |
| `:240` | `len(data) > _KIND_LIMITS[final_kind]` | 不变（归位后复核兜底） |

**原子性不变**：阶段 B 写盘/建记录/注册/失败清理结构不动；逐文件 kind 只影响阶段 A 的
hint 来源与预检，任一文件失败仍完整回滚（清单 3.2.7）。

**响应形状不变**：`AttachmentInfo.kind = final_kind`（服务端归位权威）；`files` 与旧 `files[]`
字段均可用（`startswith("files")` 已兼容）。

## 5. 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| D1 逐文件配对模型 | **A 邻接配对 + 单 kind 特判** | 前端 `uploadBatch` 经 `sniffKind` 每文件**总是**填 kind（upload-service.ts，`?? 'file'` 恒非空）→ 单 kind part 仅来自旧前端，特判无歧义；kind 未紧邻文件（连续 kind 收集时即拒 / 尾悬空收集后拒）→ 400（清单 3.3 顺序歧义）；无 kind 文件合法（`kind?` 类型防御）→ hint=None 魔数判定，**仅对 kind_count == 0（全批无 kind）或 kind_count ≥ 2 时未紧邻 kind 的文件成立**（单 kind 一律整批统一）；悬空 kind → 400 |
| D2 date_from>date_to | **A 保持空结果 200** | 前端日期选择器不产生倒置，纯防御；清单未要求 400，不扩大范围 |
| D3 category/kind 白名单 | **B 契约层共享枚举**（用户定案） | 消除 handler/repo/upload_routes 三份字符串集合维护陷阱；枚举不被字段引用 → TS 快照不变、前端零影响 |
| D4 批次组织 | **A 合并单批次** | 三整改同服务阶段 3 联调，改动小且关联（P0-01/P1-01 同 handler，P0-02 独立）；按 P0-01→P1-01→P0-02 顺序 |
| D5 悬空/歧义错误码 | 400 `CONTRACT_VALIDATION` | 与非法 kind 现状（`upload_routes.py:195-197`）一致 |
| D6 kind part 数量上限 | 100（`kind_count > 100` → 400） | DoS 防御（pending 栈无限膨胀）；前端合法最多 10 |
| D7 sticker 不进 UploadKind | 保持 4 值 | 贴图走 `/api/stickers` 不落 attachments（清单 2.2.6 已定） |
| D8 日期严格格式 | `^\d{4}-\d{2}-\d{2}$` + `date.fromisoformat` 双重 | 拒非 `YYYY-MM-DD` 形态（紧凑 `20260814` / ISO 周历 `2026-W33-4` / ISO datetime）与非法日期（`2026-13-99`）；Elysia env 3.12 实测 `fromisoformat('2026-8-14')` 抛 ValueError |
| D9 契约响应零改动 | `SearchMessageItem` 不加字段；snippet 空合法 | 清单 2.2.4/2.2.5 已定 |

## 6. 波及面

- 修改：
  - `frontend_contract/rest.py` — 新增 `SearchCategory`/`UploadKind` StrEnum（不被字段引用）
  - `core/input/session_routes.py` — search 白名单枚举化（`:653`）、`_RESOURCE_CATEGORIES`
    枚举派生（`:43`）、日期 fail-fast 校验 + `from datetime import date`
  - `core/storage/message_repo.py` — `_CATEGORY_PART_TYPES` 枚举派生（`:23`）、`query_search`
    分支枚举比较（`:407-423`）、`query_search` docstring `:383` category 取值加 sticker
  - `core/input/upload_routes.py` — 收集段逐文件 kind（`:142-200`）、阶段 A 逐 hint 校验
    （`:205-242`）、`_ALLOWED_KINDS` → `UploadKind`
- 测试：
  - `core/input/tests/test_upload.py` — 混合/逐 kind 用例
  - `core/input/tests/test_session_routes.py` — sticker 搜索 + 日期 fail-fast 用例
- **无**：契约 wire 形状（响应字段/枚举类型不变）、生成产物（TS 快照不变，需 `make
  check-types` 验证）、前端代码

## 7. 验收标准

1. `make test-quick`（unit+service）与 `make test-normal` 全绿；
2. `make lint`（ruff）全绿、`make pyright`（Elysia env）0 errors；
3. `make check-types` 全绿——契约产物（ts/schema/raw_ts + golden frames）**无漂移**（枚举未被字段引用）；
4. 新增测试覆盖清单 2.3（sticker 7 条）、清单 3.3（逐文件 8 条）、清单 §4（日期边界）全部用例；
5. 以下 grep 可机检：
   - `grep -n "category=sticker" docs/review/archive/2026-08/2026-08-15-阶段3真实联调后端整改清单.md`（需求锚点）；
   - `grep -rn '"text", "image", "video", "audio", "file", "link", "tool"' core/input/session_routes.py` 为空（白名单枚举化）；
   - `grep -rn "_ALLOWED_KINDS" core/input/upload_routes.py` 为空（已替换为 UploadKind）。

## 8. 风险

- **P0-02 配对歧义（低）**：单 kind 特判与新前端「部分文件无 kind」并存——已确认前端
  `uploadBatch` 总是填 kind，特判无歧义；无 kind 文件按 hint=None 魔数判定，不 400；
- **枚举化误伤（中）**：`_CATEGORY_PART_TYPES`/`_RESOURCE_CATEGORIES` Set 运算改动牵动
  `query_search` 分支——现有测试（test_session_routes.py 全目录 + test_upload.py）为行为
  锁定安全网；白名单统一用**值集合** `frozenset(m.value for m in ...)`（wire str 成员判断，
  跨 Python 3.11/3.12 无差异），**不用**类成员 `in`（`"image" in SearchCategory` 在 3.11 为
  False、3.12 为 True，版本依赖）；分支相等比较用 StrEnum `==`（继承 str，与裸 str 相等安全）；
- **前端时序回归（中，HIGH-1）**：P1-01 若先于前端 FE-S3-INT-P0-01 落地，当前前端发 UTC
  ISO 时间戳的日期查询会从 200 空结果变 400——放行顺序绑定前端自然日改造（§9）；联调前
  验证 `/api/search/messages` 带 ISO datetime 参数返回 400（而非破坏既有查询）；
- **日期校验回归（低）**：`fromisoformat` 与正则双重校验后合法自然日仍走原转换路径；现有
  测试用严格格式（`2026-08-01`），不受影响；repo 层 catch 保留双保险；
- **契约门禁（低）**：新增枚举若被字段引用会触发 TS 漂移——spec 明确「不被字段引用」，
  `make check-types` 验证。

## 9. 后续衔接

- 本批完成后进入 B-02（历史/搜索/来源跳转）与 B-03（附件/多媒体）真实联调准入；
- **P1-01 放行时序**：须与前端 FE-S3-INT-P0-01（日期自然日发送）同步——后端先行会使
  当前发 ISO 时间戳的前端日期查询从 200 空结果变 400（HIGH-1）；联调报告记录双方 SHA
  与请求参数，验证 `/api/search/messages` 自然日参数行为；
- 前端侧按前端清单 FE-S3-INT-P0-01 与 FE-S3-INT-DEP-01/02 落地后，双仓联调报告记录
  双方 SHA 与请求参数；
- 若后续需要贴图缩略图（搜索结果展示），另开契约增强项（清单 §3 DEP-01 预留）。

## 10. Review 记录（2026-08-15）

- code-review 第 1 轮（general-purpose agent，并行 + 主审查自查）：10 条 issue 全部消化——
  **HIGH-1** 前端日期当前发 ISO 半开区间（非自然日）→ §1/§2/§9 补时序绑定（P1-01 与
  FE-S3-INT-P0-01 同步放行），§8 加前端时序风险；**MED-1** `:198-200` 文件数检查不得丢 →
  §4.4 保留 `TOO_MANY_FILES` 步骤 1；**MED-2** 顺序歧义 400 → §4.4 收集循环加邻接判定
  （kind 未紧邻文件即 400）；**MED-3** 单 kind 特判与 hint=None 矛盾 → §4.4/D1 明确单 kind
  一律整批统一、None 路径仅 kind_count 0/≥2 可达；**LOW-1** `fromisoformat('2026-8-14')`
  在 3.12 抛 ValueError（真实宽松为紧凑/周历形态）→ §3.2/§4.3/D8 修正；**LOW-2** 「纯贴图
  无 query 不命中」措辞反转 → 改「有 query」；**LOW-3** repo docstring `:383` 加 sticker →
  §6 波及面补；**LOW-4** 行号 :142-192 修正。agent 确认事实基线（行号/集合值/gen_ts 仅收集
  被字段引用枚举/gen_schema 只收 BaseModel）全部核对通过，占位符无。
- **总体结论（agent）**：yes（附条件）——P0-01/P0-02 核心方案、契约门禁约束、原子性结构
  验证成立，阻断项（HIGH-1/MED-1）与定案项（MED-2/MED-3）已在上述修复中消化。
