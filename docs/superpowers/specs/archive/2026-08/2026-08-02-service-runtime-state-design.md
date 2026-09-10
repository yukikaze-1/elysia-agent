# 服务运行时状态 + 热插拔设计

> 修订记录：2026-08-02 首版 → 评审 11 条（commit 1d01ce4）→ **二轮评审 5 条 + 4 条低于门限项，全部核实采纳**（本版）。

## Goal

把 STT/TTS 的启用/禁用从「前端本地偏好 + 启动期配置」提升为**后端运行时状态**，支持前端热插拔（运行时开关服务、热切换模型、热添加自定义 provider），并**跨重启持久化**当前模型与模式。

## 背景与问题

1. **TTS/STT 没有运行时模式**：`config.tts.enabled` / `config.stt.enabled` 是启动期硬开关。前端 `ttsEnabled` 只是**本机播放**开关（localStorage only），后端完全不知道——用户关掉 TTS，后端照常合成音频、照常发 sentence-mode 流，浪费算力且拿不到逐字快流。
2. **STT 不是活槽位**：`transcribe_wrapper` 是 run_server 启动期固化的闭包（捕获旧后端）。`switch_stt` 只改 `config.stt.model`，运行时不生效——既有 gap（provider-availability spec 已记录 TODO）。
3. **状态不持久**：当前模型 / 模式重启回 config 默认。
4. **capabilities 扁平 + 只建连发一次**：服务状态变化（切换、开关）无通知，前端不知道要刷新。
5. **STT 无生命周期**：全仓无 `STTBackend.close()` 调用（`_stop_*` 只关 LLM/TTS），启动建后端与运行时切换各建一个 → 孤儿化。

## 架构

```
config.yaml (enabled/model 默认值)
      │
      ▼
agent.service (llm | stt | tts)  槽位：available / enabled / model / default / backend
      │                                   ▲
      │  set_service / switch_model        │  capabilities 嵌套 per-service + 全量重播
      ▼                                   │
ws_handler / cognitive loop  ─────────────┘
      │
ServiceState (SessionMemory 专用字段，MongoDB 30d TTL)  ← 启动加载 → 决定启动后端 → capabilities 反映
```

**核心原则**：`available`（后端连不连得上）与 `enabled`（用户要不要用）**分离**。

> **ServiceSlot 是概念模式，不是新类**（YAGNI）：三个服务形状差异大，不引入统一类。落地为——llm 用现有 `agent.llm`；tts 用现有 `agent.tts` + `_tts_mgr.set_enabled`；stt 新增 `agent.stt` 公开槽位。capabilities 由 `build_capabilities(agent)` 组装。

> **跨模块访问守则**：agent 的 STT/TTS 状态全部走**公开 `@property`**（`agent.stt` / `agent.stt_enabled` / `agent.tts_enabled` / `agent.stt_emotion_state`），禁止 `agent._xxx`（CLAUDE.md 公开接口规则）。`build_capabilities`、ws_handler 只读公开属性。

## 数据模型：ServiceState

新文件 `core/schema/service_state.py`（与 `core/schema/character.py` 同级，typed session 字段惯例）：

```python
class ServiceState(BaseModel):
    """服务运行时状态（SessionMemory 专用字段，MongoDB 30d TTL，重启存活）。"""
    llm_model: str = ""                 # 空 = 用 config.yaml 默认
    stt_model: str = ""
    tts_model: str = ""
    stt_enabled: bool | None = None     # None = 用户未显式改 → 回退 config.stt.enabled
    tts_enabled: bool | None = None     # None = 用户未显式改 → 回退 config.tts.enabled
```

**有效 enabled**（建门与 capabilities 用，非字段本身）：`有效 = 显式值 if 非 None else config.<svc>.enabled`。

> **为什么 None 而非 bool 默认 true**：bool 默认 true + OR 建门会**架空 config.<svc>.enabled=false**——全新安装 YAML 硬关 TTS，门 = false OR true 仍建后端；且首轮 session.save()（loop.py:891）无条件落库 `tts_enabled:true`，"从未动过"与"显式开启"字节级相同，OR 门永久为真，后续改 YAML 无效。None 语义下"从未动过"序列化为 null，与显式 true/false 可区分。

**持久化机制**：`set_param/get_param` 不存在（幽灵 API），实际惯例是 **SessionMemory 专用类型字段**，经 `save()` 同步、`load()` 恢复，需**同时改 save 与 load 两侧**：

- `SessionMemory.__init__` 加 `service_state: ServiceState | None = None`（缺省自动建默认实例，仿 `character_state`，session.py:94）。
- **save 侧**（session.py:231-266）：`_repo.save(..., service_state=self.service_state)`；JSONL fallback doc（session.py:257-264）同样加 `service_state` 字段。
- **load 侧**（session.py:345-374）：`_repo.load(sid)` 返回元组从 3 元 `(messages, cs, rs)` 扩为 **4 元 `(messages, cs, rs, ss)`**，恢复 `self.service_state`（仿 `cs` 的 field-by-field 原地更新）；`load_from_jsonl` 同步读取。**只改 save 侧、漏 load 侧 → "跨重启持久化"静默失败**。
- **既有 3 元解包调用点一并改写**（否则 `ValueError: too many values to unpack`）：
  - `core/input/debug_routes/archive.py:193` `_, character_state, _ = result` → `_, character_state, _, _ = result`
  - `:223` `raw_msgs, character_state, _ = result` → 4 元
  - 全仓 grep `= result` / `result = await.*\.load(` 附近 3 元解包逐一检查。
- **持久化时机**：`set_service` / `switch_model` 低频，变更后 **`await session.save()`**（**不是 fire-and-forget**——避免与每轮保存 loop.py:891 并发产生 last-write-wins 覆盖新值；低频操作 await 的开销可忽略）。

## 后端改动

### 1. STT 槽位化（公开接口 + 生命周期 + 模型标识）

- **agent 新增公开槽位** `agent.stt`（`STTBackend | None`）、`agent.stt_enabled`、`agent.tts_enabled`、`agent.stt_emotion_state`（dict，@property，禁 `_xxx`）。`stt_emotion_state` **返回存储实例**（非新 dict）——naive 每次返回新 dict 会让 `stt_transcribe` 的写入丢失；agent.py 现有 27 个 @property 全部返回存储实例（tts→`self._tts`），沿用此惯例。
- **可写性**：`agent.stt` 是**只读 @property → `self._stt`**（对齐 27 个只读属性惯例，agent.py 无 setter）。写入（生命周期 `self._stt = 后端`、switch、失败复位 `self._stt = None`）一律走 `self._stt`；测试注入用 `agent._stt = fake`（直接 `agent.stt = fake` 会 AttributeError）。
- **STTBackend protocol 补 `provider_name` + `model`**（对齐 TTSBackend，Module/tts/protocol.py:62-78）：
  ```python
  @property
  @abstractmethod
  def provider_name(self) -> str: ...  # "sensevoice" / "custom"

  @property
  def model(self) -> str:
      name = getattr(self, "_model", "")
      return f"{self.provider_name}/{name}" if name else ""
  ```
  **`STTFactory.create` 必须把 provider 传给后端构造**（当前 registry.py:59-62 只传 `model`）——否则 `_model` 是裸名 "sense-voice"，`model` 属性产出 "sense-voice"（无 provider 段），**capabilities 下发 → 前端 `switch_model("sense-voice")` → `STTFactory.create` 因缺 `/` 抛 ValueError**，往返断裂。各后端补 `provider_name`（sensevoice→"sensevoice"，OpenAICompatSTT→"custom"）。
- **`switch_stt` 重写**（对齐 `switch_tts`，**先建后关**防宕）：
  1. `STTFactory.create(model_id, **resolve_model_params("stt", model_id))` + **1s 连通性测试**（对齐启动路径 server.py:186）
  2. **建成功后才** close 旧后端 + 替换 `agent.stt` + `_stt_ok = True`（对账 debug）+ 写 ServiceState.stt_model（**不写 config**——config 是"默认"，ServiceState 是"当前"，保住「恢复默认」）
  3. **建失败** → 保留旧后端不动（**不先关**，避免关掉正常工作的后端）→ `_stt_ok = False` + 返回 `{"ok": false, "error": ...}`，ServiceState 不写入失败值
- **`switch_tts` 一并改为先建后关**（当前 server.py:137-145 先关后建，建失败丢旧后端），顺序：
  1. `TTSFactory.create(model_id, **resolve_model_params("tts", model_id), rate_limit_rpm=...)` + 1s 连通性测试
  2. **建成功后**按序：**替换 `self._tts` → `self._cognitive.configure(tts=self._tts)` → 才 close 旧后端**（server.py:146 的 configure 是 `_tts_mgr` 后端引用的**唯一接线**，loop.py:256-259——**configure 必须先于 close**：若 close 先执行，`_tts_mgr._tts` 会在 close-await 窗口指向已关后端，在途 `synthesize`（tts_adapter.py:247）/`stream_format`（:293）跨切换即坏）+ 写 ServiceState.tts_model（不写 config）
  3. **建失败** → 保留旧后端 + 返回 `{"ok": false, "error": ...}`
- **`switch_llm` 与 `LLMAdapter.switch_model` 一并改先建后关 + 写 ServiceState**（当前 server.py:89-109 → llm_adapter.py:185-196 是 fire-and-forget 先关旧再建新——create 抛错时 `_backend` 指向正在关闭的旧客户端，无回滚；且 **switch_llm 从不写 ServiceState.llm_model**，导致 §6 LLM 应用永不触发、运行时 LLM 热切换重启即丢）：
  ```python
  # LLMAdapter.switch_model：先建后换
  def switch_model(self, new_model_id, **kwargs):
      new_backend = LLMFactory.create(new_model_id, **kwargs)  # 先建——失败时旧后端不动
      old, self._backend, self._requested_model = self._backend, new_backend, new_model_id
      if hasattr(old, "close"):  # 换完后异步关旧
          try: asyncio.get_running_loop().create_task(old.close())
          except RuntimeError: pass
  ```
  `switch_llm`：成功后写 `ServiceState.llm_model` + `await session.save()`（不写 config）。
- **transcribe wrapper 收进 agent 公开方法**，运行时动态读 `agent.stt`（而非启动期闭包捕获）：
  ```python
  async def stt_transcribe(self, audio: bytes) -> str:
      """STT 转写（公开接口）。读取当前 agent.stt 后端，支持热切换。"""
      backend = self.stt  # 调用时读
      if backend is None:
          return ""  # 空守卫：shutdown 窗口 _stop_llm_and_tts 置 None 后、
                     # _stop_output 才关 channel——期间 BINARY 过 stt_enabled 防御后
                     # None.transcribe 会 AttributeError
      text = await backend.transcribe(audio)
      self.stt_emotion_state["emotion"] = getattr(backend, "last_emotion", "NEUTRAL")
      self.stt_emotion_state["events"] = list(getattr(backend, "last_events", []))
      return text
  ```
  > **`stt_emotion_state` 是 agent 公开属性**（非幽灵）：当前是 run_server 局部变量（server.py:178）经闭包被 ws_handler `do_transcription`（ws_handler.py:128-130,280-284）消费——收进 agent 后，ws_handler 从 `agent.stt_emotion_state` 读，emotion/events 不丢。
- **生命周期（含 None 复位，STT 与 TTS 对称）**：
  - **STT**：`_stop_*` 关闭列表加 STT 后端（`_lifecycle_services.py`）**并置 `self._stt = None`**；**STT 建后端失败路径同样置 `self._stt = None`**——否则 /restart 后先前连过、本次重建失败时 `agent.stt` 挂着已关闭旧后端 → capabilities `available=true`（读 `agent.stt is not None`）→ audio_buf 对着死后端建。
  - **TTS**：**`_stop_llm_and_tts`（:95-100）close 后置 `self._tts = None` + `_tts_mgr.set_backend(None)`**（当前只 close 不置 None，`_tts_mgr._tts` 仍指已关后端）——否则禁用→restart（建门"config=true+显式 false→不建"跳过重建）→ re-enable 走 branch 2"后端已存在"不重建 → `synthesize` 对着已关后端 → 静默 text-only。TTS 失败路径已置 `_tts = None`（_lifecycle_services.py:81-84），stop 路径对齐。
  启动时由 §6 决定启动模型，**只建一个后端**。
- **ws_handler**：`create_app` 不再接收固定 `stt_transcribe`/`stt_emotion_state` 闭包，改读 `agent.stt` + `agent.stt_transcribe` + `agent.stt_emotion_state`（建连时 `agent.stt is not None` 才建 audio_buf）。**既有连接的 provider 热切换**因 `stt_transcribe` 动态读 `agent.stt` 自然生效。

### 2. set_service 消息 + service_mode_result

WS 新增 Client→Server 消息，`ws_handler.handle_json_message` 加分支：

```json
{"type": "set_service", "service": "tts", "enabled": false}
```

`agent.set_service(service, enabled)`：
- `service == "llm"` → 拒绝（`{"ok": false, "error": "LLM 不可禁用"}`）
- `service == "stt"` / `"tts"`（**每次置位同时写 ServiceState AND 运行时槽位 `self._stt_enabled` / `self._tts_enabled`**；**tts 分支额外调 `_tts_mgr.set_enabled(enabled)`**——loop 972/979 gate 读的是 `_tts_mgr.enabled`（§3 唯一来源），`set_enabled` 只在 `_start_tts`/`_apply_service_state` 建时调用过，**set_service 不调则禁用机制（flush_on_cancel、flush_emojis、`_enabled` 守卫）整体不触发**，loop 继续走 startup 时的值发 TTS 句子流。ws_handler 后端防御读运行时槽位，只写 ServiceState 会 stale（第三方客户端不受前端 capabilities 门控影响））：
  1. **禁用** → 槽位置 false + tts 分支 `await _tts_mgr.set_enabled(False)` → **持久化 `enabled` 值：`false` if false ≠ config.<svc>.enabled else `None`** → 生效（§3/§4）→ `await session.save()` → 重播 capabilities
  2. **启用且后端已存在** → 槽位置 true + tts 分支 `await _tts_mgr.set_enabled(True)` → **持久化 `enabled` 值：`true` if true ≠ config.<svc>.enabled else `None`** → 生效 → `await session.save()` → 重播
  3. **启用且后端为 None（热插拔"开"路径）** → **先补建后端**（stt 走 `switch_stt(ServiceState.stt_model or config.stt.model)`；**tts 对称走 `switch_tts(ServiceState.tts_model or config.tts.model)`**）：
     - **补建成功** → 槽位置 true + tts 分支 `await _tts_mgr.set_enabled(True)` → **持久化 `enabled` 值：`true` if true ≠ config.<svc>.enabled else `None`** → `await session.save()` → 重播 capabilities（available 变 true）
     - **补建失败** → **不持久化 enabled**（无 stale true）、不重播假状态 → 返回 `{"ok": false, "enabled": 有效 enabled, "error": ...}`，available 保持 false
  > **统一规则（三分支共用）**：ServiceState 持久化的 enabled 值是**显式 bool，但若等于 `config.<svc>.enabled` 则写 None**（回到"未显式改"，config 重新权威）——否则任何一次 toggle 都会留下永久显式值，显式值==config 后 `ServiceState.enabled` 永非 None，之后改 YAML enabled=false 被显式 true 覆盖，"保留 YAML 硬关"失效。运行时槽位与 `_tts_mgr.set_enabled` 用**原始 bool**（不写 None）。
  > **顺序约束**：后端补建成功**之后**才持久化 enabled——否则 enabled=true 先落库，重启后"enabled-without-backend"，capabilities 读到 stale true（反向加固）。

响应广播给所有 channel（**失败响应也带 `enabled`**——前端乐观更新后靠 `service_mode_result.ok=false` + `enabled` 回滚本地状态，失败路径没有 capabilities 重播纠正）：

```json
{"type": "service_mode_result", "service": "tts", "enabled": false, "ok": true, "error": null}
{"type": "service_mode_result", "service": "tts", "enabled": false, "ok": false, "error": "连接失败"}
```

### 3. TTS 模式门控（保留 available/enabled 分离 + 不丢句子）

- `TTSStreamManager` 新增 `_enabled` 标志 + **`async def set_enabled(bool)`**（禁用分支含 drain + `flush_emojis()` 的 await，见下——**必须 async**，否则禁用路径整体不执行）。**`active` 保持"后端是否可用"语义不变**（`self._tts is not None`，字段是 `self._tts` 不是 `self._backend`），**不重定义**。
- loop.py 两处分支**分别改**（**不可统一替换**）：
  ```python
  # 972 行：SENTENCE 分支
  if self._tts_mgr.active and self._tts_mgr.enabled:
      self._tts_mgr.stream_sentence(sentence)
  else:
      await self._output.send("")          # text-only：句子边界

  # 979 行：TEXT_DELTA 分支（注意是 not——取反整体，不能字面替换）
  if not (self._tts_mgr.active and self._tts_mgr.enabled) and event.text:
      await self._output._broadcast("on_text_delta", event.text, ...)
  ```
  > 979 若按"统一替换为 `active and enabled`"会把逻辑取反：TTS 禁用但后端存活时（enabled=false, active=true）TEXT_DELTA 不再广播、句子文本整体消失——正是本节要修的 bug。
- `stream_sentence` / `_download_and_forward` 内部加 `_enabled` 守卫（防御：enabled=false 时不再新下载）。
- **每任务自持 display_text + 禁用取消时自刷**（修复中途关 TTS 丢句子；**不用共享 display_cache**——共享缓存经三轮演进（无数据源 → 双播竞态 → pop 丢句窗口）证明竞态无法根除：pop 后、广播 await 前，并发 `set_enabled(False)` flush 既跳过该句又取消其任务 → 句子静默丢失，`except CancelledError: raise` 不触发文本兜底）：
  - `display_text` 在 `stream_sentence` enqueue 时计算一次，**作为任务局部变量传入 `_download_and_forward`**（不再只在内部计算/不共享注册表）。任务自持文本 → 数据源与广播者同一，无跨任务竞态。
  - `_download_and_forward` 加 `text_broadcast: bool` 局部标志：**文本广播前置 true**（release gate 之后）。`except asyncio.CancelledError` 分支改为：若 **`self.flush_on_cancel`（per-task）** 且 `not text_broadcast` → 先广播 `display_text` 再 `raise`（禁用兜底）；若 `text_broadcast` 已 true（音频流中取消）→ 直接 raise，不重复广播。
  - **per-task `flush_on_cancel` 标志（不用全局 `_disabling`）**：`set_enabled(False)` 对**当前所有任务置 `task.flush_on_cancel = True`** 然后 `cancel_all()`。**读法**：CancelledError 分支里 `self` 是 TTSStreamManager 不是任务，必须 `getattr(asyncio.current_task(), "flush_on_cancel", False)`——读 `self.flush_on_cancel` 会复活全局竞态，且打断/异常取消的任务无该属性会 AttributeError。消除两个全局标志竞态：(a) disable→re-enable 快于被取消任务处理 CancelledError → 全局标志已复位 → 该任务不刷、丢句；(b) 非禁用路径取消（打断/异常）的任务延迟处理后撞上无关的 `_disabling=true` → 刷上上轮 stale 文本。新任务 / `reset_for_turn` 默认 false。
  - **纯 emoji 句显式 flush（须先 drain 被取消任务）**：emoji-only 句**不建任务**（stream_sentence :123-126 直接入 `_pending_emojis` + return），每任务自刷覆盖不到。`set_enabled(False)` 顺序：**先捕获 `pending = [t for t in self._tasks if not t.done()]`**（**必须在 `cancel_all()` 之前捕获**——cancel_all 会 `_tasks.clear()`，之后 gather 拿到的是空列表，drain 变 no-op、双播/早 flush 竞态原样复活）→ 给 `pending` 置 `flush_on_cancel` → `cancel_all()` → **drain**：
    ```python
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=3.0)
        await self.flush_emojis()   # drain 完成 → 才 flush emoji
    except asyncio.TimeoutError:
        logger.warning("TTS disable drain 超时，跳过 emoji flush")
        # 超时分支：不清除 _pending_emojis——被僵尸客户端卡住的任务稍后
        # 完成时仍会广播其 display_text，此时 flush emoji 会让 emoji 提前
        # 发出、依附到上一句（顺序破坏）。跳过 flush 保序，残留 emoji 随
        # reset_for_turn 丢弃。
    ```
    **drain 用 wait_for 对齐 _lifecycle_services.py:91/98 模式**——无超时会因僵尸客户端的 `_broadcast`/`send("")` 阻塞 set_service 近 60s receive_timeout。**TimeoutError 必须被 set_enabled 内吞掉**（log warning）——`flush_on_cancel` 已置、`cancel_all` 已发，禁用本身已生效，异常若冒泡到 set_service 会跳过 `session.save()` + capabilities 重播，禁用流程静默断。不能 cancel 后立即 flush——被取消任务的自刷文本要等它们下一次 await 才执行，立即 flush 会让 emoji 以无 stream_id 方式先广播，违反 flush_emojis docstring 的"wait_all 之后"前提（tts_adapter.py:154-156）；且运行中任务 `pop(sid-1)` 与 flush 并发遍历同一 dict → 同 emoji 双播。
  - **`cancel_all()` 本身清空任务引用**（它还被 loop.py:839 工具异常、loop.py:1028 打断、_lifecycle_tools.py:93 shutdown 调用——这些路径非"禁用"，本就不该刷文本，直接丢弃）；`reset_for_turn`（tts_adapter.py:96）复位 `_pending_emojis`。只有 set_enabled(False) 置 `flush_on_cancel`，保证只有禁用路径触发文本兜底。
- 后端**保持存活**（不 teardown），快速再启用；只有 switch 到别的 provider 才关闭旧后端。

### 4. STT 模式门控（前端门控 + 后端防御 + 唤醒词耦合显式化）

- **前端门控**：`stt.available && stt.enabled` 为 false → 麦克风按钮禁用 + 停止录音 + 停发 BINARY。
- **后端防御**：ws_handler BINARY 分支加 `if audio_buf and agent.stt_enabled:` 再 `feed`。
- **唤醒词耦合（显式取舍）**：`AudioBuffer.feed()` 内同时跑唤醒词检测（audio_buffer.py:217-224）。**STT 关闭 → 音频输入路径整体关闭 → 唤醒词一并失效**——二者共享输入路径，无法只关 STT 保留唤醒。写入「范围外」，不做独立解耦。
- `set_service('stt', false)` → 置位 + 重播 capabilities → 前端收到后停麦。

### 5. capabilities 嵌套 + 全量重播

新形状（**破坏性协议变更**，见「协议」节）：

```json
{
  "type": "capabilities",
  "live2d_static_image": "/static/Elysia.png",
  "services": {
    "llm": {"available": true, "enabled": true, "model": "deepseek/deepseek-v4-flash", "default": "deepseek/deepseek-v4-flash"},
    "stt": {"available": true, "enabled": true, "model": "sensevoice/sense-voice", "default": "sensevoice/sense-voice"},
    "tts": {"available": true, "enabled": false, "model": "minimax/speech-2.8-hd", "default": "minimax/speech-2.8-hd"}
  }
}
```

- **`model` 的 null 契约（保留旧 ws_handler.py:73-75 语义）**：`available=false` 的服务 `model` 为 `null`（不读 `agent.tts.model` 等——`agent.tts`/`agent.stt` 为 None 时读 `.model` 会 AttributeError，且前端拿到假 model 会显示"选中"不可用模型）。仅 available=true 才填 model。
- `available`/`model` 从**公开槽位**读：llm → `agent.llm.model`；tts → `agent.tts` + `agent.tts.model`；stt → `agent.stt` + `agent.stt.model`（§1 的 `provider_name`/`model` 补齐后产出完整 `provider/model`）。`default` 从 config 读。`enabled` 用**有效 enabled**（显式值 or config 回退，见「数据模型」）。
- **重播时机**：建连（现有）+ `set_service` + `switch_model` 成功后。抽出 `build_capabilities(agent)` helper（core/input，与 provider_registry 同层），经 `agent.output` 广播到所有 channel。

### 6. 持久化加载/生效（挂钩生命周期启动，非 run_server）

**挂钩点**：ServiceState 应用必须挂在 **agent 生命周期 `start()` 的启动步骤**（`_STARTUP_STEPS`，lifecycle.py:71-80），**不是 run_server**——TTS 由 `start()` 里的 `_start_tts` 建（_lifecycle_services.py:67-85），LLM 在 agent.py:166 构造时已建，run_server（server.py:169 `await self.start()` 先行）对它们已经太晚。挂生命周期还满足 **/restart（server.py:238-257 stop()→start()）每轮重跑启动步骤 → ServiceState 天然重应用**（session 保留），否则 ServiceState.tts_model/enabled 在 /restart 被静默绕过，违背 Goal。

**新增启动步骤**：`_STARTUP_STEPS` 加 `("service_state", "_apply_service_state")`（**位于 tts 步骤之前**）：
- session 加载后读 `ServiceState`（session 里没有 → 默认实例，enabled 字段为 **None** → 有效值回退 config）；
- 设公开槽位 `self._stt_enabled` / `self._tts_enabled`（由后续建后端步骤消费）；
- LLM：`llm_model` 非空且 ≠ config.llm.model → `await self.switch_llm(llm_model)`（switch_llm 现写 ServiceState.llm_model，§1）。

各服务建后端与应用点：

| 服务 | 建后端位置 | ServiceState 应用 |
|------|-----------|------------------|
| LLM | agent.py:166（构造时，config.llm.model） | `_apply_service_state` 内 `switch_llm(ServiceState.llm_model)`（先建后关 + 写 ServiceState，§1） |
| TTS | `_start_tts`（start() 生命周期） | 模型 `startup_tts_model = ServiceState.tts_model or config.tts.model`；**建门 = 有效 enabled（`ServiceState.tts_enabled` 非 None 用之，否则 `config.tts.enabled`）**；建后 `await _tts_mgr.set_enabled(有效 enabled)`——**只建一个** |
| STT | run_server STT 块（server.py:179-200，每轮 /restart 重进） | 模型 `startup_stt_model = session.service_state.stt_model or config.stt.model`；**建门 = 有效 enabled（同 TTS）**；**显式 `self._stt = 后端`**——**只建一个** |

步骤：
1. `_apply_service_state`（session 加载后、tts 步骤前）：读 ServiceState → 设模式槽位 → LLM 应用。
2. TTS/STT 建后端时用各自 `startup_model` + 建门 = **有效 enabled**（只建一个——不做"建 config 默认再替换"）。
3. TTS 建后 `await _tts_mgr.set_enabled(self._tts_enabled)`；STT 的 `_stt_enabled` 由 ws_handler feed 防御消费。
4. capabilities 首次发送（建连）反映 ServiceState。

> **显式删除/改写 run_server 局部 STT 块**（server.py:177-196）：删局部 `stt_backend` / `transcribe_wrapper` / `stt_emotion_state`，块内建的后端**显式赋 `self._stt`**（`self.stt_emotion_state` 为 agent 公开属性，§1）；**banner（server.py:221）改读 `agent.stt`**（不再读局部 `stt_transcribe`/`stt_model`）。照字面在块外加 ServiceState 应用、或留局部变量 → 双建 STT / `agent.stt` 恒为 None。
> **建门 = 有效 enabled（显式值优先，None 回退 config）**，不是 OR——三态语义一次覆盖：
> - config=false + 未显式改（None）→ 有效 false → **不建**（保留 YAML 硬关语义，全新安装不被架空）；
> - config=false + 用户显式开启（true）→ 有效 true → **建**（热插拔"开"跨重启存活，修复假死开关）；
> - config=true + 用户显式关闭（false）→ 有效 false → **不建**（尊重用户关闭，且免"建了再立即禁用"的浪费连接测试）；
> - config=true + 未显式改（None）→ 有效 true → **建**（现状行为）。
> stale 的 `ServiceState.stt_model`（服务已死）→ 建后端失败 → available=false + 模式保持用户设置，前端引导重配（不宕机、不丢配置）。

## 协议变更

| 方向 | 消息 | 变更 |
|------|------|------|
| Client→Server | `set_service {service, enabled}` | **新增** |
| Server→Client | `service_mode_result {service, enabled, ok, error}` | **新增** |
| Server→Client | `capabilities` | **形状变更**：扁平字段 → `services` 嵌套 per-service |
| Server→Client | `capabilities` | **触发时机变更**：建连 + 状态变化重播 |

`frontend-api.md` 同步更新（.NET 等第三方客户端为**破坏性**变更，标注协议版本）。

## 前端改动

### protocol.ts

- `Capabilities` 类型：扁平 → `services: {llm/stt/tts: {available, enabled, model: string|null, default}}` + `live2d_static_image`。
- 新增 `SetService`（client）、`ServiceModeResult`（server）类型。

### settingsStore.ts

- `ttsEnabled` **语义变更**：本机播放 → 后端 TTS 模式。改由 capabilities `services.tts.enabled` 播种；toggle 发 `set_service`。
- 新增 `sttEnabled`：同上。
- **乐观更新**（对齐 `switchModel` 现状——它是显式乐观的：立即写 state + localStorage、fire-and-forget 发 WS，settingsStore.ts:97-112；失败由后端 capabilities 重播纠正，不悲观等待）。
- **旧值迁移**：`elysia_tts === 'off'` 本地用户 → 首次建连时按此播种 `ttsEnabled=false` 并发 `set_service('tts', false)`（一次性，避免语义变化后旧偏好丢失）。
- `updateCapabilities` 改为**幂等、可随时调用**（不再只建连）：处理嵌套形状，`available`/`enabled`/`model`/`default` 全量回填。
- localStorage 降级为**连上前的一次性缓存**（服务端持久化后本地不再权威）。
- 新增 `llmDefault`/`sttDefault`/`ttsDefault`（"当前/默认"显示）。

### useWebSocket.ts

- 路由 `service_mode_result` → 更新对应 store / 显示错误。
- `capabilities` 不再只在建连处理——**任何时刻到达都调 updateCapabilities**。

### SettingsPanel.vue

- 新增「STT 语音」启用 toggle；「TTS 语音」toggle 改为后端模式（发 `set_service`）。
- **toggle 在 available=false 时不禁用**（启用即触发后端补建连接）；仅当该服务**完全无 provider 可连**（未配置且无 local）时才禁用并引导配置。
- 模型下拉显示「当前 / 默认」（可选「恢复默认」→ `switch_model(default)`）。
- 移除旧「本机播放」语义残留（audioStore gate 变防御，见下）。

### InputBar.vue

- 麦克风门控：`sttAvailable && sttEnabled`（替代仅查 `sttAvailable`）。
- `sttEnabled` 变 false 且正在录音 → 停止录音 + 停发 BINARY。

### audioStore.ts

- `if (!ttsEnabled) return` gate **保留作为防御**（模式关掉后端本来无音可播）。
- `ttsEnabled` 变 false 且正在播放 → 取消当前播放 + 清队列（capabilities 重播触发）。

## 错误处理与 guardrails

- `set_service`：未知 service → 400；`llm` → 拒绝；`enabled` 非 bool → 拒绝。
- `switch_stt` / `switch_tts` 失败 → **保留旧后端**（先建后关）+ `{"ok": false, "error": ...}`，ServiceState 不写入失败值；`set_service` 补建失败 → **不持久化 enabled** + `service_mode_result.ok=false`，available 保持 false。
- `switch_stt` 成功/失败对账 `_stt_ok`（debug 面板不再自相矛盾）。
- capabilities 重播失败（channel 已关）→ 静默跳过（现有 `_send_json` 打 warning 的兜底）。
- `set_enabled(False)` 的 drain `wait_for` 超时（僵尸客户端）→ **`set_enabled` 内吞掉 TimeoutError**（log warning + 跳过 flush_emojis）——`flush_on_cancel` 已置、`cancel_all` 已发，禁用已生效；异常若冒泡到 set_service 会跳过 `session.save()` + capabilities 重播，禁用流程静默断。
- ServiceState 加载失败（类型校验）→ 忽略，回 config 默认；stale 启动模型建后端失败 → available=false + 模式保持，不宕机。
- **已过滤项顺带修复**：`switch_stt` 不再改写 `config.stt.model`（"恢复默认"依赖 config 恒为默认）；建后端补连通性测试。

## 测试策略

后端 unit/service（新测试**加 marker**：unit/service，按 docs/testing.md）：
- `ServiceState` 持久化 round-trip：**save + load 两侧**（save 同步 MongoDB + JSONL fallback doc；load 4 元组解包 + load_from_jsonl 恢复）+ 无字段时自动建默认实例。
- `set_service`：llm 拒绝 / tts、stt 禁用 → 置位 + save + 重播；**tts 分支传播到 `_tts_mgr.set_enabled`**（loop gate 生效，禁用后逐字文本流）；**显式值 == config 时持久化 None**（复位到未显式改，config 重新权威）；**启用补建**：后端 None 时开成功（持久化 + 重播 available=true）/ 失败（**不持久化 enabled** + ok=false）两分支；未知 service 400。
- `switch_stt`：先建后关（**建失败保留旧后端**）+ 连通性测试 + `_stt_ok` 对账 + 写 ServiceState 不写 config；`model`/`provider_name` 属性产出完整 `provider/model`。
- `switch_llm` / `LLMAdapter.switch_model`：**先建后换**（create 失败旧后端不动 + 不关）；成功后**写 ServiceState.llm_model** + save 触发（运行时 LLM 切换重启保留）。
- `TTSStreamManager.set_enabled`：loop 972/979 分别断言（`active && enabled` / `not (active && enabled)`）；**禁用取消时每任务按 `flush_on_cancel` 自刷 display_text**（排队未播句不丢、已广播句不双播、`text_broadcast` 生效）；**`cancel_all`（打断/异常/shutdown）不刷文本**、`reset_for_turn` 复位；**`set_enabled(False)` 显式 `flush_emojis()`**（纯 emoji 句无任务，自刷覆盖不到）；per-task 标志消除全局 `_disabling` 两竞态。
- `set_service`：**置位同时写 ServiceState 显式值 AND 运行时槽位 `agent.stt_enabled`/`agent.tts_enabled`**（后端防御读槽位——只写 ServiceState 会 stale）。
- `_apply_service_state` 启动步骤：session 加载后设模式槽位 + LLM 应用（`llm_model` ≠ config 才 switch）；位于 tts 步骤之前。
- **建门 = 有效 enabled 三态**：config=false+None → 不建（硬关保留）；config=false+显式 true → 建（假死开关修复）；config=true+显式 false → 不建（免浪费连接测试）；config=true+None → 建（现状）。只建一个（无孤儿）。
- **`agent.stt` None 复位**：建后端失败 / `_stop_*` 后 `agent.stt` 为 None（对齐 TTS `_tts=None`），capabilities available 不误报。
- **archive.py 3 元解包改写**：`:193` / `:223` 扩为 4 元，session-detail debug 端点不再 ValueError。
- `stt_transcribe` 动态解析：切换 `agent.stt` 后同一 wrapper 走新后端；emotion/events 写入 `agent.stt_emotion_state`。
- `build_capabilities`：嵌套形状 + available=false 时 `model: null`（不读 None.model）+ STT 完整 model 不 AttributeError。
- 启动加载：`startup_model = ServiceState.model or config.model` **只建一个后端**（无孤儿）+ 模式应用 + capabilities 首发反映 ServiceState。

**改写既有测试**（capabilities 形状变更 / protocol 新增抽象方法破坏的断言）：
- `core/input/tests/test_server.py:239 test_ws_connect_capabilities_includes_models`（`cap["llm_model"]` / `cap["stt_model"]` → 嵌套 `services` 断言）。
- `:270 test_ws_connect_capabilities_stt_model_when_connected`（`cap["stt"] is True` / `cap["stt_model"]` → 嵌套断言）。
- **`Module/stt/tests/test_stt.py:42-56` 的 `GoodBackend`**（`pytestmark = service`，跑进 test-quick）只实现 transcribe/transcribe_stream/test/close——`provider_name` 设为 @abstractmethod 后 `GoodBackend()`（:55）TypeError。需给 GoodBackend 补 `provider_name` 实现（或 protocol 改 `@property` 非 abstract + 默认空串）。
- **`core/input/tests/test_server.py:282`** `create_app(agent, serve_static=False, stt_transcribe=fake_transcribe)`——§1 移除 `stt_transcribe` 参数后 TypeError。STT 注入从 create_app 参数改为 **fake `agent.stt` 槽位**（建连时 `agent.stt is not None` 才建 audio_buf）。
- 全仓 grep 其余扁平 `cap["*"]` / `capabilities` 断言、`stt_transcribe=` 调用点一并改写。

前端：无单测基建 → `vue-tsc --noEmit` + `npm run build` 验证。

回归：`make test-quick` 全绿。

## 范围外

- **唤醒词**：开关维持现状（`wake_toggle` per-connection 不动）；**STT 关闭会连带禁用唤醒词**（共享音频输入路径，本版显式接受，独立解耦后续再做）。
- STT 后端**从 None 插拔到已连接**时，既有 WS 连接不重建 audio_buf（新连接生效）；provider 切换对既有连接生效（`stt_transcribe` 动态读 `agent.stt`）。
- 多角色 / 多会话各自独立的 ServiceState（单会话近似全局，30d TTL 自然衰减）。
- 运行时服务状态的可视化（debug 面板展示 available/enabled/model）——可后续加。
