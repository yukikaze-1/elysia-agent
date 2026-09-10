# 阶段 3 真实联调后端复验修复：视觉工具会话换绑 + 音频 MIME 规范化

**文档版本：** v0.1
**文档状态：** 待评审
**日期：** 2026-08-15
**仓库：** `/home/yomu/elysia-agent`
**上游评估：** `docs/review/archive/2026-08/2026-08-15-阶段3真实联调后端复验问题清单.md`（BE-S3-RUN-P0-01 / P0-02）
**前置决策：** 会话讨论定案 D0-D5（全部按推荐定案）

---

## 1. 背景

阶段 3 真实联调复验（`docs/review/archive/2026-08/2026-08-15-阶段3真实联调后端复验问题清单.md`）在前端
契约闭合、日期/贴图分类/逐文件 kind 三项整改通过后，新发现 2 项后端 P0 阻断项，均需修复
并真实复验后才能把后端阶段 3 标记为「已真实联调」：

1. **BE-S3-RUN-P0-01 视觉工具会话换绑**：`ToolContext.services["session"]` 只在 Agent 启动时
   注入当时的 `self.session`，每轮 `process()` 换绑目标会话时**不同步** tool_context——
   `describe_image` 从 `ctx.get("session")` 读最近用户消息，始终落在启动时旧会话。
   非默认会话上传并发送 PNG 后，消息归档与附件关联均正确，但工具执行时读到图片数 0。
2. **BE-S3-RUN-P0-02 音频 kind/MIME 输出不同源**：上传阶段探测到容器级 MIME（WebM →
   `video/webm`、WAV → `audio/x-wav`）原样写入附件记录与响应；`kind=audio` 消歧后
   `mime=video/webm` 不在 `AudioMime` 权威枚举。前端音频白名单（`audio/wav` 冻结值）
   首次发送判「不支持的文件」；`ws_v2._attachment_part` 构造失败静默回退默认 `audio/wav`
   → 同一附件「上传即时值」与「重载恢复值」不一致。

## 2. 目标

- **P0-01**：每轮目标会话解析后、任何模型/工具执行前，将目标 `SessionMemory` 原子绑定到
  本轮 `ToolContext`（`services["session"]` + 派生 `_session_msg_count`）；v2 显式
  `conversationId` 指向非活动会话时工具使用显式目标；切换会话不残留上一轮 session。
- **P0-02**：`kind` 确定后，面向 wire/持久化/下载的 MIME 规范化为与 kind 一致的契约值：
  `audio+video/webm → audio/webm`、`audio+audio/x-wav → audio/wav`、`video+video/webm → video/webm`；
  写路径与读路径（消息恢复/下载 Content-Type）共用同一规范化函数，存量记录经读路径兼容，
  不再依赖「构造失败后静默用默认值」。
- **零契约响应形状变更**：`AttachmentInfo`/`AudioPart`/`VideoPart` 字段与枚举取值不变
  （`audio/webm`/`audio/wav` 已在 `AudioMime`），前端 TS 快照不变。

## 3. 现状（事实基线）

### 3.1 P0-01 视觉工具会话换绑

| 项 | 现状 |
|---|---|
| 注入点 | `core/agent/_lifecycle_tools.py:67-76`：`_start_tool_context()` 只在启动时把当时的 `self.session` 注入 `extra_services["session"]`，`"_session_msg_count": len(self.session.messages)` 同样为启动快照 |
| 每轮换绑 | `core/cognitive/loop.py:437-452`：`target = await self._session_resolver(...)` → `self._character_engine.attach_session(target)`（`:448`）→ `self._session = target`（`:450`）→ `self._prompt.set_session(target)`（`:452`）；`loop.py:462-463` 只更新 `tool_context.trace_id`，**不更新 `services["session"]`** |
| 工具读取 | `core/tools/vision/plugin.py:89-102`：`session = ctx.get("session")` → `recent_messages(20)` 找最近 UserMessage → 提取 ImageParts——services 指向启动时旧会话 |
| 其他消费者 | `core/tools/self_state/plugin.py:30`：`ctx.get("_session_msg_count", 0)`（会话消息数展示，同样 stale） |
| 消息时序 | 用户消息在 `loop.py:626-631`（`_build_turn_prompt` 内，LLM 流式前）加入 `self._session.messages`——工具执行时当前用户消息**已在目标会话内存队列**；缺的是 services 引用换绑 |
| 副本机制 | `Module/tool/protocol.py:97-119` `clone_for_call()`：`services=dict(self.services)` 浅拷贝；`core/cognitive/tools.py:77-80` 每次调用 clone 且 `session_id=str(self._session.session_id)`（该字段已每轮正确）——只要在 `process()` 换绑基座 services，本 turn 内所有工具调用副本立即看到新 session；**TaskScheduler 路径**（`core/task/scheduler.py:72,410-412`）不经 clone_for_call、直传基座 `self._tool_ctx`——同一对象引用，换绑同样可见（后台任务工具因此读到「最近一轮换绑的会话」而非启动会话，属既有行为边界，§8 划界不扩 scope） |
| 既有模式 | `core/agent/_lifecycle_task_system.py:57` 已有直接写 `tool_context.services["_task_system"]` 先例；`loop.py:462-463` 已内联写 `tool_context.trace_id` |

### 3.2 P0-02 音频 kind/MIME

| 项 | 现状 |
|---|---|
| 探测值 | `core/input/upload_routes.py:238-280`：`mime = ft.mime`（filetype 探测，WebM→`video/webm`、WAV→`audio/x-wav`）；`final_kind` 经 hint tiebreaker 消歧（`:260-266`：`webm` → `kind=audio` 时 audio、否则 video），但 `mime` **原样**写入 doc（`:279` `"mime": mime`）、validated dict（`:294`）与 `AttachmentInfo.mime`（`:354`） |
| 权威枚举 | `core/schema/part.py:54-59`：`AudioMime = {WAV:"audio/wav", MP3:"audio/mp3", MPEG:"audio/mpeg", OGG:"audio/ogg", WEBM:"audio/webm"}`——**无 `audio/x-wav`**；`part.py:62-65`：`VideoMime = {MP4:"video/mp4", WEBM:"video/webm", OGG:"video/ogg"}`；`AudioPart` 默认 `mime=AudioMime.WAV`（`:102-108`） |
| 读路径 | `core/input/ws_v2.py:197-225` `_attachment_part()`：`mime = doc.get("mime") or ""`，`try: cls(**{**data, "mime": mime})` 失败 `except Exception: pass` **静默**回退 Part 默认 mime |
| 下载路径 | `core/input/attachment_routes.py:157-169`：`mime = doc.get("mime", "application/octet-stream")` 直接作 `Content-Type` |
| 文档 | `docs/electron-backend-protocol-v2.md:517` audio 行冻结 `audio/x-wav / audio/mpeg / audio/ogg`；`:520` webm tiebreaker 注释无规范化口径 |
| 既有测试 | `test_upload.py` `TestPerFileKind::test_webm_audio_video_disambiguated`（`:547-555`）已断言同批 audio/video 分别归位，**但未断言 mime** |

## 4. 方案

### 4.1 P0-01：每轮 ToolContext 会话换绑（`loop.py`）

在 `process()` 内、`self._prompt.set_session(target)`（`loop.py:452`）**之后**、trace_id 更新
（`:462-463`）**之前**插入（两者同属 per-turn 上下文边界）：

```python
# BE-S3-RUN-P0-01（复验清单 §3）：ToolContext 会话换绑——describe_image/self_state
# 经 ctx.services["session"]/["_session_msg_count"] 读会话，启动注入后从不更新；
# 每轮目标会话解析后必须原子换绑（clone_for_call 浅拷贝 services，本 turn 内
# 所有工具调用副本立即看到新 session；失败/取消/切换会话由下一轮换绑覆盖，不残留）。
if self._tool_adapter is not None:
    svc = self._tool_adapter.tool_context.services
    svc["session"] = target
    svc["_session_msg_count"] = len(target.messages)
```

要点：

1. **位置**：`process()` 内、目标会话解析后、任何模型/工具执行前（复验清单 §3.3.5「会话归属
   应由 cognitive loop 在统一的 per-turn 上下文边界完成」）；换绑在用户消息加入 session
   （`_build_turn_prompt` `:627`）之前完成，工具执行时 `target.messages` 已含当前消息——
   引用换绑即让 `recent_messages(20)` 读到当前图；
2. **同步范围（D1）**：只同步 `session` + `_session_msg_count`（每会话派生值，复验清单
   §3.3.4「至少」）；`_fsm_state` 启动快照同样 stale 但**与会话无关**（Agent/turn 级），
   记为已知遗留、不扩 scope。**时序说明（LOW）**：换绑在 `_build_turn_prompt`（`:627`）用户
   消息入队**前**执行，`_session_msg_count` 是本 turn 消息入队前的计数、turn 内不刷新——
   仅展示用途（self_state），满足「按目标会话同步」，off-by-one 属可接受快照语义；
3. **原子性（§3.3.3）**：整轮工具执行用同一 `target` 引用——失败/取消后下一轮重新 resolve +
   换绑覆盖，不残留上一轮 session；同一会话重复换绑幂等；
4. **不动 vision 插件**（§3.3.5 禁止插件反查全局 Agent）：会话归属完全由 loop 边界完成。

### 4.2 P0-02：音频 MIME 规范化单源

**D3：规范化函数放 `core/schema/utils.py`**（共享纯函数之家，`_validate_src`/`now_iso` 同处；
写路径 + 两读路径可无环导入）：

```python
# 附件 MIME 规范化表（复验清单 §4.2 冻结）：(kind, 探测/历史别名) → 契约权威值。
# 探测值（filetype 容器判定）可作校验内部值，但 wire/持久化/下载必须与 kind 一致。
_ATTACHMENT_MIME_ALIASES = {
    ("audio", "video/webm"): "audio/webm",   # kind=audio 消歧后的容器探测值
    ("audio", "audio/x-wav"): "audio/wav",   # filetype 探测别名 → AudioMime 权威值
}


def normalize_attachment_mime(kind: str, mime: str) -> str:
    """附件记录 MIME 规范化：kind 确定后，探测/历史别名 → 契约权威值。

    写路径（上传）与读路径（消息恢复 / 下载 Content-Type）共用，保证同一附件
    各视角 mime 一致；未知别名原样返回（调用方决定回退/告警）。

    Args:
        kind: 附件最终 kind（image/video/audio/file）。
        mime: 待规范化的 MIME（探测值或历史记录值）。

    Returns:
        规范化后的 MIME；表中无匹配别名时原样返回。
    """
    return _ATTACHMENT_MIME_ALIASES.get((kind, mime), mime)
```

三个消费点：

| 消费点 | 位置 | 改造 |
|---|---|---|
| 写路径 | `upload_routes.py:266`（`final_kind` 确定后、`doc`/validated dict 构造前） | `mime = normalize_attachment_mime(final_kind, mime)`——规范化后值同时进 `doc["mime"]`、`validated["mime"]`、`AttachmentInfo.mime`（单点，`doc` 与响应天然一致） |
| 读路径 | `ws_v2.py:208-214` `_attachment_part()` | `mime = normalize_attachment_mime(kind, doc.get("mime") or "")` 后再校验；**D4：静默 `except: pass` 改 `logger.warning`**（带 kind/attachment_id/mime），结构化回退默认 mime 保留（不炸整条消息页） |
| 下载路径 | `attachment_routes.py:157-159` | `mime = normalize_attachment_mime(kind, doc.get("mime", "application/octet-stream"))`（存量 `kind=audio + video/webm` 记录下载头也规范化为 `audio/webm`，复验清单 §4.2.1） |

约束（复验清单 §4.2）：

1. `detected_mime` 保留为校验过程内部值（魔数/扩展名/大小/逐 hint kind 复核**不绕过**，
   `upload_routes.py:238-266` 校验序不动）；规范化只发生在 `final_kind` 确定**之后**；
2. **存量兼容（D5）**：纯读路径规范化——`_attachment_part` 与下载 Content-Type 都过
   `normalize_attachment_mime`，存量 `kind=audio + mime=video/webm`→`audio/webm`、
   `audio/x-wav`→`audio/wav` 幂等恢复；**不写离线迁移脚本**（存量记录少、迁移有离线/重启
   时序风险）；
3. **未知 mime（D4）**：规范化后仍不在 Part 枚举 → `logger.warning` 可诊断，不静默伪装默认
   值；回退默认 mime 保留（防整页炸掉，复验清单 §4.3「不会把整个消息页炸掉」）。

### 4.3 文档更新（D6，确认项）

`docs/electron-backend-protocol-v2.md`：

- `:517` audio 行：`audio/x-wav / audio/mpeg / audio/ogg` → `audio/wav / audio/mpeg / audio/ogg /
  audio/webm`（规范输出值）；
- `:520` webm tiebreaker 注释补充规范化口径：`kind=audio → audio/webm，其余 → video/webm`。

## 5. 决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| D0 批次组织 | **A 合并单批次** | 两项同挡 stage-3 联调门、共处一份复验清单；一次 9 阶段流程成本更低，联调门一次闭合 |
| D1 换绑同步范围 | **A 只 `session` + `_session_msg_count`** | 每会话派生值就两个（复验清单 §3.3.4「至少」）；`_fsm_state` 启动快照同样 stale 但 Agent/turn 级、与会话无关，记遗留不扩 scope |
| D2 换绑机制/位置 | **A `process()` 内联写 dict** | 贴合既有 `tool_context.trace_id` 内联先例（`loop.py:462-463`）与 `services["_task_system"]` 先例（`_lifecycle_task_system.py:57`）；两行不为新公开 API；复验清单 §3.3.5 要求 cognitive loop 在统一 per-turn 边界完成 |
| D3 规范化函数归属 | **B `core/schema/utils.py`** | 共享纯函数之家（`_validate_src`/`now_iso` 同处），写路径 + 两读路径可无环导入；放 `upload_routes.py` 会让 ws_v2 反向导入路由模块，不干净 |
| D4 未知 mime 行为 | **A 结构化回退 + `logger.warning`** | 上传后新记录都规范化过，读路径未知 mime 只可能是存量脏数据；warning 满足「可诊断」（§4.2.4），回退满足「不炸消息页」（§4.3） |
| D5 存量兼容方式 | **A 纯读路径规范化** | `_attachment_part` + 下载 Content-Type 过同一函数幂等恢复；不写迁移脚本（存量少、迁移有时序风险） |
| D6 文档同步 | 更新 `electron-backend-protocol-v2.md` §13.5 | 冻结表不再出现 `audio/x-wav`，补 `audio/webm` |

## 6. 波及面

- 修改：
  - `core/cognitive/loop.py` — `process()` 目标会话解析后 ToolContext 会话换绑（`:452` 后插入）
  - `core/schema/utils.py` — 新增 `_ATTACHMENT_MIME_ALIASES` + `normalize_attachment_mime()`
  - `core/input/upload_routes.py` — `final_kind` 确定后 `mime` 规范化（`:266` 后一行）
  - `core/input/ws_v2.py` — `_attachment_part()` 读路径规范化 + 静默 except → `logger.warning`
  - `core/input/attachment_routes.py` — 下载 `Content-Type` 规范化（`from core.schema.utils import normalize_attachment_mime`）
  - `docs/electron-backend-protocol-v2.md` — §13.5 音频 MIME 表 + webm tiebreaker 注释
- 测试：
  - `core/cognitive/tests/test_tool_session_rebind.py` — **新建**（P0-01 复验清单 §3.4 六用例）
  - `core/input/tests/test_upload.py` — `TestPerFileKind` 扩展 mime 断言 + WAV 用例（复用 `_post_kinds`/WEBM/PNG 常量）
  - `core/input/tests/test_attachment_message.py` — ① 存量 `audio/x-wav`/`kind=audio+video/webm`
    记录兼容恢复（复用 `_add_attachment` helper）；② **新上传记录 replay 一致性（M2）**：
    WAV/WebM(audio) 经 `_attachment_part` 恢复的 mime 与上传响应 `AttachmentInfo.mime` 完全一致
    （覆盖复验清单 §4.3 case 3「上传即时消息与重启/REST/WS replay 后完全一致」）
  - `core/input/tests/test_attachment_download.py` — 下载 Content-Type 规范化断言
- **无**：契约 wire 形状（`AttachmentInfo`/`AudioPart`/`VideoPart` 字段与枚举不变）、生成产物
  （TS 快照不变，需 `make check-types` 验证）、前端代码

## 7. 验收标准

1. `make test-quick`（unit+service）与 `make test-normal` 全绿；
2. `make lint`（ruff）全绿、`make pyright`（Elysia env）0 errors；
3. `make check-types` 全绿——契约产物（ts/schema/raw_ts + golden frames）**无漂移**；
4. 新增测试覆盖复验清单 §3.4（P0-01 六用例）与 §4.3（P0-02 六用例）全部要求：
   - P0-01：default 发图 → describe_image 取得当前 1 图；A/B 连续发不同图不串；v2 显式非
     active 会话读显式目标；图/文交替不残留；取消/失败后再发图绑定仍正确；多模态直看图
     分支不回归（describe_image 仍被排除于多模态 tool_defs）；
   - P0-02：`audio/webm + video/webm` 同批四处 mime 各归其位；WAV 四处均 `audio/wav`；
     上传即时与 replay 一致；存量记录兼容恢复不炸消息页；hint 冲突/未知格式/扩展名不匹配
     仍按原契约拒绝；混合批次失败完整回滚（L6：回滚由既有 `TestPerFileKind` 回滚用例覆盖，
     本批写路径规范化发生在持久化前，回滚逻辑未被触碰，验证不回归）；
5. 以下 grep 可机检（M1 修订——探测表/规范化别名表保留探测别名，验收只约束输出侧）：
   - `grep -rn "audio/x-wav" docs/electron-backend-protocol-v2.md` 为空（文档冻结表与说明不再
     出现探测别名）；
   - `grep -rn "audio/x-wav" core/ Module/ --include="*.py" | grep -v "/tests/"` 仅命中两处
     白名单文件——`upload_routes.py`（探测表 `_MIME_KIND`/`_MIME_EXTS`，保留为校验内部值）与
     `core/schema/utils.py`（规范化别名表）；生产输出/响应/持久化/下载均不出现探测别名
     （测试文件允许出现——断言规范化行为）；
   - `grep -rn "pass  # mime 非枚举值" core/input/ws_v2.py` 为空（原静默吞异常注释已随
     `logger.warning` 替换移除；`except Exception:` 行保留，body 改 `logger.warning`）。

## 8. 风险

- **P0-01 换绑遗漏分支（低）**：换绑只在 `process()` 主 turn 路径；TaskScheduler/WorkflowEngine
  后台任务执行工具不在此路径（直传基座 `self._tool_ctx`，读到的是「最近一轮换绑的会话」而非
  启动会话）——复验清单 §3.3.5 明确会话归属由 cognitive loop 统一完成，后台任务执行器不读
  `services["session"]`（describe_image/self_state 均在主 turn），不扩 scope；
- **P0-01 测试基建（中）**：`describe_image` 真实插件 execute 会调视觉 LLM（`LLMFactory.create`
  → `backend.chat`）——测试必须 stub 视觉后端（fake backend 记录收到的 image_url），不能碰
  真实 LLM；主 LLM 用既有 fake 后端模式（`_UsageLLM`，`test_v2_generation.py:32`；工具侧基建
  参考 `test_tool_image_parts.py` 的 `_FakeAdapter` + mock `LLMAdapter`），必要时直接驱动
  `_execute_tools_directly` 绕过工具生成；
- **P0-02 规范化表演进（低）**：`_ATTACHMENT_MIME_ALIASES` 只收复验清单冻结的三行别名；
  未来新增探测别名须同步扩展表（表在 schema 层单源，消费方无感）；
- **P0-02 读路径回退行为（低）**：`_attachment_part` 的 `logger.warning` 对存量脏数据比静默
  更吵但可诊断——属复验清单明确要求（§4.2.4「可诊断日志/错误，不得悄悄」）；回退默认 mime
  保留保证消息页不炸；
- **契约门禁（低）**：本批不新增枚举/字段，`make check-types` 应为无漂移；若意外漂移即为
  回归信号。

## 9. 后续衔接

- 本批修复 + 真实 Electron 回归通过后，后端阶段 3 标记「已真实联调」；
- 真实回归脚本（§6 验收 4）：PNG 视觉（新建非默认会话发图 → describe_image 读到）、
  WAV/WebM 首次发送与切会话/重载/重启后恢复类型一致，音频不再显示「不支持的文件」；
- 前端配套问题（签名相对 URL 解析等）见前端复验清单，不在本批后端范围。

## 10. Review 记录（2026-08-15）

- code-review 第 1 轮（general-purpose agent，逐行核对源码）：**总体结论 yes（附条件）**，
  无 HIGH 阻断项；事实基线（行号/消费点枚举/克隆浅拷贝/规范化表不漏）全部核对通过。
  **MED-1** §7.5 grep 验收字面不可满足（探测表 `_MIME_KIND`/`_MIME_EXTS` + 规范化别名表必然
  保留 `audio/x-wav`）→ §7.5 改写为「文档无 `audio/x-wav` + core 仅两处白名单文件」；
  **MED-2** §4.3 case 3「上传即时与 replay 一致」测试无落点 → §6 `test_attachment_message.py`
  补「新上传记录 replay 一致性」用例（非存量）。**LOW-1** 测试基建引用失实（`test_tool_gate`
  为 `_FakeExecutor`、`test_tool_image_parts` 为 `_FakeAdapter`）→ §8 改引 `_UsageLLM`
  （`test_v2_generation.py:32`）；**LOW-2** clone 推理只覆盖 `_execute_tools_directly`，
  TaskScheduler 直传基座 → §3.1 副本机制行补述 + §8 划界；**LOW-3** `_session_msg_count`
  off-by-one（换绑先于消息入队）→ §4.1 要点 2 补时序说明；**LOW-4** 行号修正
  （`AttachmentInfo.mime` :354、attach/set_session 主语补全）；**LOW-5** 插入位置二义 →
  §4.1 定「:452 后、trace_id :462-463 前」；**LOW-6** 回滚用例属既有覆盖 → §7.4 标注
  「既有 `TestPerFileKind` 回滚用例覆盖，验证不回归」。
- **总体结论（agent）**：yes（附条件）——两项 P0 修复方案正确、范围控制到位、不反向破坏
  §5 已通过契约；两个 MED 条件（§7.5 grep 修订 + §4.3 case 3 测试落点）已在上述修复中消化。
