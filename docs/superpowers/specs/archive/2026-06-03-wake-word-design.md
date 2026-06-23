# 唤醒词功能设计

## 背景

当前项目的语音交互是"按键说话"（push-to-talk）模式：用户点击麦克风按钮开始录音，说完后静音 1.5s 结束录音，STT 转写结果送入 Agent 认知循环。需要新增唤醒词功能，让用户在麦克风开启时可以通过说出唤醒词来激活对话或打断 Agent 输出。

## 设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 交互模式 | 混合模式 | 保留手动麦克风按钮，按钮开启时唤醒词可打断/激活 |
| 检测位置 | 服务端 AudioBuffer 层 | 模型选择灵活（Python 生态），与现有 VAD 流水线自然衔接 |
| 唤醒引擎 | openWakeWord | 完全开源，Python 原生流式推理，无需 API key，Home Assistant 项目验证 |
| 唤醒词来源 | `character_config.py` 的 `wake_words` 字段 | 复用已有配置，不新增冗余 |
| 唤醒行为 | Agent 忙则打断，空闲则激活聆听 | 两条路径都合理，用户选两者都要 |
| 唤醒后超时 | 5 秒 | WOKEN 状态下 5s 内无语音自动回到 AWAITING |
| 音频裁剪 | 不裁剪 | 唤醒词本身有价值，"Elysia, 帮我查天气"完整送给 LLM 更自然 |

## 架构

```
mic PCM (256ms chunks, 16kHz mono Int16)
        │
        ▼
AudioBuffer.feed(chunk)
        │
        ├── WakeWordDetector (新增，流式检测，内部切 80ms 帧)
        │       └── 命中 → 状态机进入 WOKEN + 回调 on_wake()
        │                  ├── Agent 忙(thinking/executing/speaking) → _do_interrupt()
        │                  └── Agent 空闲 → 无操作，等待语音
        │
        └── WebRTC VAD (现有，不变)
                │
                └── 语音段结束 → LISTENING状态? ──yes──▶ STT (完整音频含唤醒词) → Agent
                                        ──no───▶ 丢弃（门控关闭）
```

关键点：
- 唤醒词检测和 VAD 并行运行，同一股音频流两个消费者
- 唤醒词只做**门控开关**，不裁剪音频。STT 拿到的就是用户说的完整句子
- 唤醒词命中后，所在语音段如果在进行中会被 flush，后续语音段从新开始累积。但如果唤醒词所在段已经是完整段（静音结束），则直接走 STT

## 新增模块：WakeWordDetector

**文件：** `Module/input/wakeword.py`

**依赖：** `openwakeword` + `numpy`

### 接口

```python
class WakeWordDetector:
    def __init__(
        self,
        wake_words: list[str],
        on_wake: Callable[[], Coroutine],
    ):
        """
        wake_words: 唤醒词列表，来自 character_config
        on_wake: 命中回调（异步），由 AudioBuffer 注入
        """

    def feed(self, chunk: bytes) -> None:
        """喂入 256ms PCM chunk，内部切帧，逐帧推理"""

    def reset(self) -> None:
        """清空 buffer 和模型状态（状态机回退时调用）"""

    async def close(self) -> None:
        """释放 openWakeWord 模型资源"""
```

### 帧切分

| | AudioBuffer chunk | openWakeWord 要求 |
|---|---|---|
| 时长 | 256ms | 80ms |
| 采样数 @16kHz | 4096 | 1280 |
| 字节数 (Int16) | 8192 | 2560 |

`feed()` 内部逻辑：

1. chunk 追加到内部 ring buffer
2. 每凑够 2560 bytes (80ms) 提取一帧
3. `numpy.frombuffer(frame, dtype=np.int16)` 转为 int16 数组
4. 对每个唤醒词对应的 `openwakeword.Model` 调用 `model.predict(samples)`
5. 任一模型 score > 0.5 且距上次命中超过 1s（去抖）→ 调用 `on_wake()`

### openWakeWord 模型

- 使用预训练 `.tflite` 模型文件
- 每个唤醒词创建一个 `openwakeword.Model` 实例
- `openwakeword` 内置常见唤醒词模型（"alexa", "hey_jarvis", "ok_google" 等），但不覆盖用户自定义词
- 自定义唤醒词（如 "Elysia"）需要用户提供 `.tflite` 模型文件。可通过 openWakeWord 在线工具训练生成
- 构造函数接收 `model_paths: dict[str, str]` 映射唤醒词 → 模型文件路径
- 未提供模型的唤醒词会打印警告并跳过

### 可选依赖

`openwakeword` 作为可选依赖：安装时需要 `pip install openwakeword numpy`。如果未安装，`WakeWordDetector` 构造时抛出 `ImportError` 附带安装提示。服务端在 `wake_words` 非空但导入失败时记录警告并降级为无唤醒词模式（STT 直接通过，门控不生效）。

## 唤醒状态机

三个状态，放在 `AudioBuffer` 内部（不引入新类）：

```
           ┌──────────────────────────────────┐
           │                                  │
           ▼                                  │
       AWAITING ──(wake 命中)──▶ WOKEN        │
           ▲                       │          │
           │                  (5s 无语音)       │
           │                       ▼          │
           │                   AWAITING       │
           │                                  │
           │          (VAD 检测到语音)          │
           │                  │               │
           │                  ▼               │
           └──(STT 完成)── LISTENING ──────────┘
```

- **AWAITING**：等待唤醒词。VAD 分出的语音段直接丢弃（不送 STT）
- **WOKEN**：唤醒词已命中，等待用户说话。启动 5s 超时计时器。5s 内无语音 → AWAITING
- **LISTENING**：用户开始说话，VAD 正在累积语音块。静音 1.5s 后送 STT → 完成后回到 AWAITING

### WOKEN 期间唤醒词重复命中

WOKEN 状态下再次命中唤醒词：重置 5s 超时计时器，不做其他操作。

## AudioBuffer 改动

**文件：** `Module/input/audio_buffer.py`

### 构造函数变更

```python
def __init__(
    self,
    ...,
    wake_words: list[str] | None = None,
):
```

- `wake_words=None` → 不启用唤醒词，行为与现有完全一致
- `wake_words` 非空 → 创建 `WakeWordDetector`，初始化状态为 `AWAITING`

### `feed()` 变更

```python
def feed(self, chunk: bytes):
    if self._deafened:
        return

    # 新增：唤醒词流式检测（与 VAD 并行）
    if self._wake_detector:
        self._wake_detector.feed(chunk)

    # 现有 VAD + 语音段分割逻辑不变
    is_speech = self._is_speech(chunk)
    ...
```

### `_process_segment()` 门控

```python
def _process_segment(self):
    audio = b"".join(self._chunks)
    self._chunks.clear()

    if len(audio) < MIN_SEGMENT_BYTES:
        return

    # 门控：非 LISTENING 状态丢弃
    if self._wake_state != WakeState.LISTENING:
        return

    wav = pcm_to_wav(audio)
    asyncio.create_task(self._transcribe(wav))
```

### on_wake 回调

```python
async def _on_wake(self):
    if self._wake_state == WakeState.LISTENING:
        # 已在聆听中，忽略
        return
    self._wake_state = WakeState.WOKEN
    self._woken_at = time.monotonic()
    self._chunks.clear()  # 清除唤醒词之前的音频
    await self._on_interrupt()  # 触发打断（如果 Agent 在忙）
```

## 服务端集成

**文件：** `Module/input/server.py`

`_handle_ws()` 创建 AudioBuffer 时传入唤醒词：

```python
wake_words = agent.get_wake_words()  # 从 character_config 读取

audio_buf = AudioBuffer(
    stt_transcribe=stt_transcribe,
    on_interrupt=lambda: _do_interrupt(agent, skip_if={"thinking", "executing"}),
    on_transcription=lambda text: _do_transcription(agent, ws, text, audio_buf, stt_emotion_state),
    wake_words=wake_words,
)
```

**文件：** `core/character_config.py`

已有 `wake_words: list[str]` 字段，默认值 `["Elysia"]`。无需修改配置格式。

**文件：** `core/agent.py`

暴露方法使 server.py 可获取唤醒词配置：

```python
def get_wake_words(self) -> list[str]:
    return self._character_config.wake_words
```

## 前端改动（最小）

**文件：** `static/js/state.js`

```javascript
this.wakeEnabled = true;  // 唤醒词开关
```

**文件：** `static/js/main.js`

处理新增的 `wake` 消息类型，用于 UI 反馈（如麦克风按钮短暂高亮）：

```javascript
case 'wake':
    // 显示"检测到唤醒词"的短暂视觉反馈
    flashWakeIndicator();
    break;
```

**文件：** `static/js/mic.js`

`flashWakeIndicator()`：麦克风按钮闪烁或临时变色，1s 后恢复。

## 向后兼容

- `wake_words=None`（默认）时，行为与现有完全一致
- `--stt` 关闭时，唤醒词功能不可用（因为没有 STT 流水线）
- AudioBuffer 现有测试不受影响

## 验证计划

### 单元测试 (`Module/input/test_wakeword.py`)

- 静音 PCM → openWakeWord 不触发
- 合成/预录 "Elysia" 音频 → 触发检测
- 不包含唤醒词的语音 → 不触发
- 连续多次命中 → 去抖逻辑正确（1s 冷却）
- 超时回退验证
- 无 `wake_words` 时 feed() 为空操作

### 集成测试 (`Module/input/test_audio_buffer.py`)

- `wake_words=None` → 门控不生效，行为不变
- `wake_words=["Elysia"]` → AWAITING 状态丢弃语音，WOKEN→LISTENING 后正常 STT
- 5s 超时回退验证

### 端到端验证

1. 启动服务端（需 SenseVoice at localhost:20042）
2. 打开调试页面，确认麦克风正常
3. 说唤醒词 → 确认发送 `{"type": "wake"}` 到前端
4. 说 "Elysia, 你好" → 确认完整转录 "Elysia, 你好" 进入 LLM
5. 唤醒后 5s 不说话 → 确认回到 AWAITING（后续普通语音被丢弃）
6. Agent 正在输出时说唤醒词 → 确认打断

## 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `Module/input/wakeword.py` | 新建 | WakeWordDetector 类 |
| `Module/input/__init__.py` | 修改 | 导出 WakeWordDetector |
| `Module/input/audio_buffer.py` | 修改 | 状态机 + 门控 + 并行检测 |
| `Module/input/server.py` | 修改 | 传入 wake_words + on_wake 回调 |
| `core/agent.py` | 修改 | 暴露 get_wake_words() |
| `core/character_config.py` | 查看 | 确认 wake_words 字段可用 |
| `Module/input/test_wakeword.py` | 新建 | 唤醒词检测单元测试 |
| `Module/input/test_audio_buffer.py` | 修改 | 唤醒门控集成测试 |
| `static/js/state.js` | 修改 | 新增 wakeEnabled 状态 |
| `static/js/main.js` | 修改 | 处理 `wake` 消息类型 |
| `static/js/mic.js` | 修改 | 唤醒状态 UI 反馈 |
