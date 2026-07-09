# MiniMax TTS 云端接入设计

> 日期：2026-07-09 | 状态：已实现

## 1. 背景

当前 TTS 模块有两个 provider：

| provider | 部署 | 协议 |
|----------|------|------|
| `gpt-sovits` | 本地 | 专有 HTTP API |
| `custom` | 云端 | OpenAI 兼容 (`/v1/audio/speech`) |

需要接入 MiniMax 云端 TTS，其使用自有 HTTP + SSE 协议，与现有两者均不兼容，需新建 backend。

## 2. MiniMax API 要点

| 维度 | 详情 |
|------|------|
| 端点 | `POST https://api.minimaxi.com/v1/t2a_v2` |
| 认证 | `Authorization: Bearer <api_key>` |
| 非流式 | `stream: false` → JSON 中 `data.audio` 为 hex 编码音频 |
| 流式 | `stream: true` → SSE (`text/event-stream`)，逐行 `data: {...}` |
| 音频格式 | 非流式可选 wav/mp3/flac；流式仅 mp3 |
| 音色 | `voice_setting.voice_id` + 可选 `speed/vol/pitch/emotion` |
| 文本标签 | `(laughs)` 语气词、`<#x#>` 停顿控制、`(拼音)` 发音标注 |

## 3. 设计

### 3.1 新增文件：`Module/tts/minimax_tts.py`

```
class MiniMaxTTSBackend(TTSBackend):
```

- 使用共享 `httpx.AsyncClient`（实例生命周期内复用），参考 `openai_tts.py` 模式
- 构造函数参数：`model`（默认 `speech-2.8-hd`）、`api_key`（必填）、`base_url`、`voice_id`（默认音色）、`voice_map`（可选 `dict[str, str]`）、`speed`、`vol`、`pitch`、`sample_rate`、`audio_format`、`logger`
- **character → voice_id 映射**：synthesize 时按 `voice_map.get(character)` 查找，未命中则 fallback 到构造参数的 `voice_id`。v1 不引入 YAML 映射文件，调用方传 dict 即可；后续可按需补 YAML 加载逻辑
- `synthesize()`：POST `stream: false`，JSON 响应 `bytes.fromhex(data.audio)`，返回 bytes
- `synthesize_stream()`：POST `stream: true`，SSE 解析 + `asyncio.Queue` + `create_task` 桥接（参考 `gpt_sovits.py` 的 `_call_api` 模式），hex decode 后逐 chunk yield
- `test()`：`POST /v1/get_voice` 验证连通性，使用 `make_test_method()` 工厂
- `close()`：`await self._client.aclose()`
- 超时：connect=5s, read=120s

### 3.2 修改：`Module/tts/registry.py`

```python
_BACKENDS["minimax"] = {
    "base_url": "https://api.minimaxi.com",
    "env_key": "MINIMAX_API_KEY",       # 供文档/调试引用；实际解析用独立的 env var
}
```

`TTSFactory.create()` 添加 `minimax` 分支，**使用两个独立环境变量**（与 `custom` 共用同一个 `env_key` 不同——`custom` 有 base_url 必须显式传入的保护，MiniMax 有默认 base_url 无法区分）：

```python
api_key = resolve_api_key("MINIMAX_API_KEY", cfg, kwargs.pop("api_key", None))
base_url = resolve_base_url("MINIMAX_TTS_URL", cfg, kwargs.pop("base_url", None))
```

- `MINIMAX_API_KEY` — API 密钥，必填
- `MINIMAX_TTS_URL` — 覆盖 base_url（默认 `https://api.minimaxi.com`），可选
- `inject_logger()` 注入 logger
- 实例化 `MiniMaxTTSBackend`

### 3.3 修改：`Module/tts/text_preprocess.py`

新增 `strip_for_minimax(text: str) -> str`：

| 标记 | 动作 | 原因 |
|------|------|------|
| `[动作]` | 删除 | GPT-SoVits 残留，MiniMax 不认 |
| `<<心情>>` | 删除 | 同上 |
| `()` `（）` | **保留** | MiniMax 语气词 + 发音标注 |
| `<>` | **保留** | MiniMax `<#x#>` 停顿控制 |
| emoji | 删除 | MiniMax 不可合成 |

**实现注意**：该函数不使用现有 `strip_all_markup` 中的 `<.*?>` 正则（该正则会误匹配并删除 `<#x#>` 停顿标签）。`strip_for_minimax` 独立实现，只组合 `[...]`、`<<...>>`、emoji 三条删除规则，**不引入任何 `<>` 匹配逻辑**。

### 3.4 参数映射

| TTSBackend | MiniMax API |
|------------|-------------|
| `character` | `voice_setting.voice_id` |
| `emotion` | `voice_setting.emotion` |
| `model` | `model` |
| `speed`（构造参数） | `voice_setting.speed` |
| `vol`（构造参数） | `voice_setting.vol` |
| `pitch`（构造参数） | `voice_setting.pitch` |

### 3.5 不修改的部分

- `Module/tts/__init__.py` — 只导出 ABC、Factory、Config，无需改动
- `Module/tts/config.py` — `TTSConfig` 已有 `model/base_url/api_key`，够用
- `core/tts_stream.py` — 预处理硬编码问题，本次不碰，后续单独讨论

## 4. 已知问题（TODO，后续讨论）

1. `core/tts_stream.py` 的 `TTSStreamManager` 硬编码调用了 `strip_all_markup()`，MiniMax 的 `(laughs)` / `<#x#>` 标签会在流式管线里被提前删除
2. 预处理函数应由 TTS backend 提供（`backend.get_strip_function()`），而非全局硬编码
3. LLM 输出格式 / prompt 模板是否需要根据 TTS backend 调整标记体系

## 5. 测试

- 单元测试（mock HTTP）：构造、非流式、流式、错误处理、取消
- 不依赖真实 MiniMax API
- `python -m pytest Module/tts/test_tts.py -v -k "MiniMax"` — 26 tests

## 6. 验证

1. 单元测试全绿（61/61）
2. 冒烟测试：设置 `MINIMAX_API_KEY` 环境变量后，`TTSFactory.create("minimax/speech-2.8-hd").test()` 返回 `{"ok": true}`
3. 在调用方用 `synthesize("你好(sighs)")` 验证语气标签生效

## 7. 实施变更记录

以下设计在实现时根据 MiniMax API 实际行为调整：

| 原设计 | 变更 | 原因 |
|--------|------|------|
| SSE 流式解析 | `synthesize_stream()` 内部用非流式 wav + 分块 yield（最终版本） | MiniMax 流式仅 mp3；SSE 每个 event 是独立可播 mp3（非增量片段），拼接导致重复；一度尝试 SSE 后回退 |
| `speed/vol/pitch` 传 float | `int()` 强转 | MiniMax JSON 校验拒绝 `1.0`（number），要求 `1`（int64） |
| `audio_setting` 无 `bitrate` | 新增 `bitrate: 128000` | MiniMax API 要求此字段 |
| `voice_id` 默认空 | 默认 `"elysia_test_001"`，空值抛 `ValueError` | MiniMax 拒绝空 voice_id（`empty field`） |
| `_BACKENDS["minimax"].env_key` 共用 | `MINIMAX_API_KEY`（api_key）+ `MINIMAX_TTS_URL`（base_url）独立 | 避免 API key 值被 `resolve_base_url` 当成 URL |
| `resolve_base_url` 复用顶层值 | minimax 分支保存 `explicit_base_url` 独立传入 | 顶层用 `cfg["env_key"]=MINIMAX_API_KEY` 解析会污染 base_url |
| 无语音映射配置 | 新增 `minimax_voices.yaml` + `_load_voice_map()` | 支持多音色；`character` → `voice_id` 映射 |
| 无前端 format 元数据 | 音频管线透传 `format`（wav/mp3） | `audio.js` 根据 format 创建正确 MIME 的 Blob |
| 无情绪参数对接 | `EmotionEngine` mood → `TTSStreamManager.set_emotion()` | MiniMax 支持 `voice_setting.emotion` |
| Prompt 无 TTS 感知 | `f2_output_format.j2` 按 `tts_provider` 输出标记指令 | MiniMax：`(laughs)` `<#x#>`；GPT-SoVits：纯文本 |
| `TTSStreamManager` 硬编码 `strip_all_markup` | 从 backend 获取 `get_preprocess()` | 解除 MiniMax 标签被误删 |
| 前端显示 TTS 标签 | `<#x#>` 停顿标签过滤；`(中文舞台指示)` 过滤 | 停顿和舞台指示不显示；英文语气标签保留显示 |
| `/api/providers` 缺 MiniMax | 注册表新增 `"minimax"` | 前端设置下拉可选 |
