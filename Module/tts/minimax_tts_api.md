# MiniMax 语音合成 API 文档

> 整理自 [MiniMax 开放平台文档](https://platform.minimaxi.com/docs/api-reference/speech-t2a-http)
> 整理日期：2026-07-06

---

## 目录

1. [同步语音合成 HTTP](#1-同步语音合成-http)
2. [同步语音合成 WebSocket](#2-同步语音合成-websocket)
3. [异步长文本语音合成 - 创建任务](#3-异步长文本语音合成---创建任务)
4. [异步长文本语音合成 - 查询状态](#4-异步长文本语音合成---查询状态)
5. [音色快速复刻 - 上传复刻音频](#5-音色快速复刻---上传复刻音频)
6. [音色快速复刻 - 上传示例音频](#6-音色快速复刻---上传示例音频)
7. [音色快速复刻 - 执行复刻](#7-音色快速复刻---执行复刻)
8. [音色设计](#8-音色设计)
9. [声音管理 - 查询可用音色](#9-声音管理---查询可用音色)
10. [声音管理 - 删除音色](#10-声音管理---删除音色)

---

## 通用信息

### 授权方式

所有接口均使用 **Bearer Token** 认证：

```
Authorization: Bearer <你的API_KEY>
```

API Key 在 [账户管理 > 接口密钥](https://platform.minimaxi.com/user-center/basic-information/interface-key) 中获取。

### 基础域名

| 域名 | 说明 |
|------|------|
| `https://api.minimaxi.com` | 主域名 |
| `https://api-bj.minimaxi.com` | 备用域名（北京） |

### 通用响应结构

```json
{
  "base_resp": {
    "status_code": 0,
    "status_msg": "success"
  }
}
```

- `status_code`: `0` 表示成功，非 0 表示错误
- `status_msg`: 状态描述

### 可用的语音模型

| 模型 | 说明 |
|------|------|
| `speech-2.8-hd` | 最新高清版（支持语气词标签） |
| `speech-2.8-turbo` | 最新快速版（支持语气词标签） |
| `speech-2.6-hd` | 高清版 |
| `speech-2.6-turbo` | 快速版 |
| `speech-02-hd` | 高清版（不支持 Persian/Filipino/Tamil） |
| `speech-02-turbo` | 快速版（不支持 Persian/Filipino/Tamil） |
| `speech-01-hd` | 高清版（不支持 Persian/Filipino/Tamil） |
| `speech-01-turbo` | 快速版（不支持 Persian/Filipino/Tamil） |

### 语气词标签（仅 speech-2.8-hd / speech-2.8-turbo）

可在文本中插入以下标签实现语气效果：

`(laughs)` `(chuckle)` `(coughs)` `(clear-throat)` `(groans)` `(breath)` `(pant)` `(inhale)` `(exhale)` `(gasps)` `(sniffs)` `(sighs)` `(snorts)` `(burps)` `(lip-smacking)` `(humming)` `(hissing)` `(emm)` `(whistles)` `(sneezes)` `(crying)` `(applause)`

### 文本高级功能

- **段落切换**：使用换行符
- **停顿控制**：`<#x#>`，`x` 为秒数（0.01~99.99），不能连续使用
- **行内发音替换**：`(拼音/音标)` 包裹，例如 `"处理(chu3li3)"`、`"The word live is pronounced (lɪv)"`

---

## 1. 同步语音合成 HTTP

**POST** `https://api.minimaxi.com/v1/t2a_v2`

使用 HTTP 协议进行同步语音合成，发送文本即可获得合成后的音频数据。

### 请求头

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `Content-Type` | enum | 是 | 固定为 `application/json` |

### 请求体参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `model` | enum | **是** | - | 模型版本（见上方模型列表） |
| `text` | string | **是** | - | 待合成文本，**< 10000 字符**，超过 3000 字符建议流式 |
| `stream` | boolean | 否 | `false` | 是否流式输出 |
| `stream_options` | object | 否 | - | 流式相关选项 |
| `voice_setting` | object | 否 | - | 音色设置 |
| `audio_setting` | object | 否 | - | 音频输出设置 |
| `pronunciation_dict` | object | 否 | - | 发音字典 |
| `timbre_weights` | object[] | 否 | - | 音色混合权重 |
| `language_boost` | enum | 否 | `null` | 增强小语种/方言识别，可选 `auto` 或具体语种 |
| `voice_modify` | object | 否 | - | 声音效果器 |
| `subtitle_enable` | boolean | 否 | `false` | 是否开启字幕（speech-2.8/2.6/02/01 系列支持） |
| `subtitle_type` | enum | 否 | `sentence` | 字幕粒度：`sentence` / `word` / `word_streaming`（仅流式） |
| `output_format` | enum | 否 | `hex` | 输出形式：`hex`（默认）或 `url`（仅非流式，URL 有效期 24h） |
| `aigc_watermark` | boolean | 否 | `false` | 末尾添加音频节奏标识（仅非流式） |

#### voice_setting

| 参数 | 类型 | 说明 |
|------|------|------|
| `voice_id` | string | 音色 ID |
| `speed` | number | 语速，默认 1 |
| `vol` | number | 音量，默认 1 |
| `pitch` | number | 音调，默认 0 |
| `emotion` | string | 情感，如 `"happy"` |

#### audio_setting

| 参数 | 类型 | 说明 |
|------|------|------|
| `sample_rate` | integer | 采样率，如 32000 |
| `bitrate` | integer | 比特率，如 128000 |
| `format` | string | 格式：`mp3` / `wav` / `flac`（流式仅支持 `mp3`） |
| `channel` | integer | 声道数：1（单声道）或 2（立体声） |

### 请求示例

```bash
curl --request POST \
  --url https://api.minimaxi.com/v1/t2a_v2 \
  --header 'Authorization: Bearer <你的API_KEY>' \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "speech-2.8-hd",
    "text": "今天是不是很开心呀(laughs)，当然了！",
    "stream": false,
    "voice_setting": {
      "voice_id": "male-qn-qingse",
      "speed": 1,
      "vol": 1,
      "pitch": 0,
      "emotion": "happy"
    },
    "pronunciation_dict": {
      "tone": [
        "处理/(chu3)(li3)",
        "危险/dangerous"
      ]
    },
    "audio_setting": {
      "sample_rate": 32000,
      "bitrate": 128000,
      "format": "mp3",
      "channel": 1
    },
    "subtitle_enable": false
  }'
```

### 响应参数

| 字段 | 类型 | 说明 |
|------|------|------|
| `data` | object/null | 合成数据，可能为 null |
| `data.audio` | string | hex 编码的音频数据（`output_format=hex` 时） |
| `data.status` | integer | 状态码 |
| `trace_id` | string | 会话 ID，用于问题定位 |
| `extra_info` | object | 额外信息 |
| `extra_info.audio_length` | integer | 音频时长（毫秒） |
| `extra_info.audio_sample_rate` | integer | 采样率 |
| `extra_info.audio_size` | integer | 文件大小（字节） |
| `extra_info.bitrate` | integer | 比特率 |
| `extra_info.word_count` | integer | 字数 |
| `extra_info.usage_characters` | integer | 计费字符数 |
| `extra_info.audio_format` | string | 音频格式 |
| `extra_info.audio_channel` | integer | 声道数 |
| `base_resp` | object | 基础响应 |

### 响应示例

```json
{
  "data": {
    "audio": "<hex 编码的音频数据>",
    "status": 2
  },
  "extra_info": {
    "audio_length": 9900,
    "audio_sample_rate": 32000,
    "audio_size": 160323,
    "bitrate": 128000,
    "word_count": 52,
    "invisible_character_ratio": 0,
    "usage_characters": 26,
    "audio_format": "mp3",
    "audio_channel": 1
  },
  "trace_id": "01b8bf9bb7433cc75c18eee6cfa8fe21",
  "base_resp": {
    "status_code": 0,
    "status_msg": "success"
  }
}
```

### 注意事项

- 流式输出时 `output_format` 固定为 `hex`，不能用 `url`
- `language_boost` 支持 40+ 语种列表

---

## 2. 同步语音合成 WebSocket

**WSS** `wss://api.minimaxi.com/v1/t2a_v2`

使用 WebSocket 协议进行同步语音合成，适合需要低延迟流式传输的场景。

### 客户端发送事件

#### task_start — 任务开始

```json
{
  "event": "task_start",
  "model": "speech-2.8-turbo",
  "language_boost": "Chinese",
  "voice_setting": {
    "voice_id": "male-qn-qingse",
    "speed": 1,
    "vol": 1,
    "pitch": 0
  },
  "pronunciation_dict": {
    "tone": [
      "处理/(chu3)(li3)",
      "危险/dangerous"
    ]
  },
  "audio_setting": {
    "sample_rate": 32000,
    "bitrate": 128000,
    "format": "mp3",
    "channel": 1
  }
}
```

发送 `task_start` 正式开始合成任务，需等服务端返回 `task_started` 后才能继续发送。

#### task_continue — 任务继续

```json
{
  "event": "task_continue",
  "text": "真正的危险不是计算机开始像人一样思考(sighs)，而是人开始像计算机一样思考。"
}
```

收到 `task_started` 后可发送，**支持顺序发送多个**。最后一次收到服务端返回结果后超过 **120s** 无新事件则自动断开。

#### task_finish — 任务结束

```json
{
  "event": "task_finish"
}
```

服务端收到后会等待当前队列全部完成后关闭连接。

### 服务端返回事件

| 事件 | 说明 |
|------|------|
| `connected_success` | WebSocket 建连成功 |
| `task_started` | 任务已成功开始 |
| `task_continued` | 音频数据返回（含 `data.audio` 和 `extra_info`） |
| `task_finished` | 任务成功结束 |
| `task_failed` | 任务失败，需关闭连接并处理错误 |

### connected_success

```json
{
  "session_id": "xxxx",
  "event": "connected_success",
  "trace_id": "0303a2882bf18235ae7a809ae0f3cca7",
  "base_resp": { "status_code": 0, "status_msg": "success" }
}
```

### 音频数据返回（task_continued）

```json
{
  "data": { "audio": "xxx" },
  "extra_info": {
    "audio_channel": 1,
    "audio_format": "mp3",
    "audio_length": 9914,
    "audio_sample_rate": 32000,
    "audio_size": 157869,
    "bitrate": 128000,
    "invisible_character_ratio": 0,
    "usage_characters": 158,
    "word_count": 158
  },
  "is_final": true,
  "session_id": "301871346491491",
  "trace_id": "04ee3794e2c9e4a6d5f99e77742f06fd",
  "base_resp": { "status_code": 0, "status_msg": "success" }
}
```

### task_failed

```json
{
  "session_id": "xxxx",
  "event": "task_failed",
  "trace_id": "0303a2882bf18235ae7a809ae0f3cca7",
  "base_resp": { "status_code": 1004, "status_msg": "XXXXXXX" }
}
```

---

## 3. 异步长文本语音合成 — 创建任务

**POST** `https://api.minimaxi.com/v1/t2a_async_v2`

创建异步长文本语音合成任务，适用于大量文本。返回 `task_id` 后通过查询接口获取结果。

### 请求体参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | enum | **是** | 模型版本 |
| `text` | string | 二选一必填 | 待合成文本，**最长 5 万字符** |
| `text_file_id` | integer | 二选一必填 | 文本文件 ID，单文件 < 100 万字符（txt/zip） |
| `voice_setting` | object | **是** | 音色设置（voice_id, speed, vol, pitch） |
| `audio_setting` | object | **是** | 音频设置（见下方） |
| `pronunciation_dict` | object | 否 | 发音字典 |
| `language_boost` | enum | 否 | 增强语种识别 |
| `voice_modify` | object | 否 | 声音效果器 |
| `aigc_watermark` | boolean | 否 | 默认为 false |

#### text_file_id 说明

- **txt 文件**：< 1,000,000 字符，支持 `<#x#>` 停顿标记
- **zip 文件**：内含 txt 或 json 文件。json 特殊格式支持 `title`、`content`、`extra` 三个字段，每个非空字段独立生成一组音频/字幕/附加信息文件

#### audio_setting（异步）

| 参数 | 说明 |
|------|------|
| `audio_sample_rate` | 采样率，如 32000 |
| `bitrate` | 比特率，如 128000 |
| `format` | **仅支持** `mp3` / `wav` / `flac` |
| `channel` | 声道数（1 或 2） |

#### voice_modify（声音效果器）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `pitch` | 0 | 变调 |
| `intensity` | 0 | 强度 |
| `timbre` | 0 | 音色修改 |
| `sound_effects` | - | 音效，如 `"spacious_echo"` |

### 请求示例

```bash
curl --request POST \
  --url https://api.minimaxi.com/v1/t2a_async_v2 \
  --header 'Authorization: Bearer <API_KEY>' \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "speech-2.8-hd",
    "text": "真正的危险不是计算机开始像人一样思考(sighs)，而是人开始像计算机一样思考。",
    "language_boost": "auto",
    "voice_setting": {
      "voice_id": "audiobook_male_1",
      "speed": 1,
      "vol": 1,
      "pitch": 1
    },
    "pronunciation_dict": {
      "tone": ["危险/dangerous"]
    },
    "audio_setting": {
      "audio_sample_rate": 32000,
      "bitrate": 128000,
      "format": "mp3",
      "channel": 2
    },
    "voice_modify": {
      "pitch": 0,
      "intensity": 0,
      "timbre": 0,
      "sound_effects": "spacious_echo"
    }
  }'
```

### 响应参数

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_id` | string | 任务 ID，用于查询状态 |
| `task_token` | string | 任务令牌 |
| `file_id` | integer | 音频文件 ID |
| `usage_characters` | integer | 消耗字符数 |
| `base_resp` | object | 基础响应 |

### 响应示例

```json
{
  "task_id": 95157322514444,
  "task_token": "eyJhbGciOiJSUz...",
  "file_id": 95157322514444,
  "usage_characters": 101,
  "base_resp": {
    "status_code": 0,
    "status_msg": "success"
  }
}
```

### 返回文件

任务完成后通过 `file_id` 调用文件检索接口下载：

- 音频文件（mp3/wav/flac）
- 字幕文件（精确到句）
- 额外信息 JSON 文件

> ⚠️ **下载 URL 有效期 9 小时（32,400 秒）**，请及时下载！

---

## 4. 异步长文本语音合成 — 查询状态

**GET** `https://api.minimaxi.com/v1/query/t2a_async_query_v2?task_id={task_id}`

> ⚠️ **速率限制：每秒最多查询 10 次**

### 查询参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `task_id` | integer | **是** | 任务 ID |

### 请求示例

```bash
curl --request GET \
  --url 'https://api.minimaxi.com/v1/query/t2a_async_query_v2?task_id=95157322514444' \
  --header 'Authorization: Bearer <API_KEY>'
```

### 响应示例

```json
{
  "task_id": 95157322514444,
  "status": "Processing",
  "file_id": 95157322514496,
  "base_resp": {
    "status_code": 0,
    "status_msg": "success"
  }
}
```

### 任务状态说明

| status | 说明 |
|--------|------|
| `Processing` | 任务处理中 |
| `Success` | 任务完成 |
| `Failed` | 任务失败 |
| `Expired` | 任务已过期 |

---

## 5. 音色快速复刻 — 上传复刻音频

**POST** `https://api.minimaxi.com/v1/files/upload`

上传用于复刻的原始音频文件。

### 请求头

| 参数 | 值 |
|------|-----|
| `Content-Type` | `multipart/form-data` |

### 表单参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `purpose` | enum | **是** | 固定为 `voice_clone` |
| `file` | file | **是** | 音频文件 |

### 文件要求

- 格式：**mp3 / m4a / wav**
- 时长：**10 秒 ~ 5 分钟**
- 大小：**≤ 20 MB**

### 请求示例

```bash
curl --request POST \
  --url https://api.minimaxi.com/v1/files/upload \
  --header 'Authorization: Bearer <API_KEY>' \
  --header 'Content-Type: multipart/form-data' \
  --form purpose=voice_clone \
  --form file='@复刻音频.mp3'
```

### 响应示例

```json
{
  "file": {
    "file_id": 123456789,
    "bytes": 5896337,
    "created_at": 1700469398,
    "filename": "复刻音频.mp3",
    "purpose": "voice_clone"
  },
  "base_resp": {
    "status_code": 0,
    "status_msg": "success"
  }
}
```

---

## 6. 音色快速复刻 — 上传示例音频

**POST** `https://api.minimaxi.com/v1/files/upload`

上传示例音频（prompt audio），用于增强合成音色的相似度和稳定性。

### 表单参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `purpose` | enum | **是** | 固定为 `prompt_audio` |
| `file` | file | **是** | 示例音频文件 |

### 文件要求

- 格式：**mp3 / m4a / wav**
- 时长：**< 8 秒**
- 大小：**≤ 20 MB**

### 请求示例

```bash
curl --request POST \
  --url https://api.minimaxi.com/v1/files/upload \
  --header 'Authorization: Bearer <API_KEY>' \
  --header 'Content-Type: multipart/form-data' \
  --form purpose=prompt_audio \
  --form file='@sample.mp3'
```

### 响应示例

```json
{
  "file": {
    "file_id": 987654321,
    "bytes": 128000,
    "created_at": 1700469398,
    "filename": "sample.mp3",
    "purpose": "prompt_audio"
  },
  "base_resp": {
    "status_code": 0,
    "status_msg": "success"
  }
}
```

---

## 7. 音色快速复刻 — 执行复刻

**POST** `https://api.minimaxi.com/v1/voice_clone`

使用上传的音频进行音色快速复刻，可试听合成效果。

> ⚠️ 复刻音色若 **7 天内未正式调用**，系统会自动删除。
> ⚠️ 调用前需要完成**个人或企业认证**。

### 请求体参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file_id` | integer | **是** | 复刻音频的 file_id（上传时返回） |
| `voice_id` | string | **是** | 自定义音色 ID，8~256 字符，首字符英文字母，可含数字/字母/`-`/`_`，末位不能是 `-`/`_`，不可重复 |
| `clone_prompt` | object | 否 | 示例音频，含 `prompt_audio`（file_id）和 `prompt_text`（对应文本） |
| `text` | string | 否 | 试听文本，≤1000 字符。提供后会合成试听音频并收费 |
| `model` | enum | 条件必填 | 若提供 `text` 则必传 |
| `language_boost` | enum | 否 | 增强语种识别 |
| `text_validation` | string | 否 | 预期文本，≤200 字符。用于 ASR 校验 |
| `accuracy` | number | 否 | ASR 相似度阈值 [0,1]，默认 0.7 |
| `need_noise_reduction` | boolean | 否 | 降噪，默认 false |
| `need_volume_normalization` | boolean | 否 | 音量归一化，默认 false |
| `aigc_watermark` | boolean | 否 | 水印，默认 false |

### 请求示例

```bash
curl --request POST \
  --url https://api.minimaxi.com/v1/voice_clone \
  --header 'Authorization: Bearer <API_KEY>' \
  --header 'Content-Type: application/json' \
  --data '{
    "file_id": 123456789,
    "voice_id": "myVoiceClone01",
    "clone_prompt": {
      "prompt_audio": 987654321,
      "prompt_text": "This voice sounds natural and pleasant."
    },
    "text": "A gentle breeze sweeps across the soft grass(breath).",
    "model": "speech-2.8-hd",
    "text_validation": "This voice sounds natural and pleasant.",
    "accuracy": 0.7,
    "need_noise_reduction": false,
    "need_volume_normalization": false,
    "aigc_watermark": false
  }'
```

### 响应示例

```json
{
  "input_sensitive": false,
  "input_sensitive_type": 0,
  "demo_audio": "<试听音频链接>",
  "extra_info": {
    "audio_length": 11124,
    "audio_sample_rate": 32000,
    "audio_size": 179926,
    "bitrate": 128000,
    "word_count": 18,
    "usage_characters": 18
  },
  "base_resp": {
    "status_code": 0,
    "status_msg": "success"
  }
}
```

### 响应参数

| 字段 | 说明 |
|------|------|
| `input_sensitive` | 输入是否含敏感内容 |
| `input_sensitive_type` | 敏感内容类型代码 |
| `demo_audio` | 试听音频链接（仅当请求中提供了 text 和 model） |
| `extra_info` | 试听音频元数据 |
| `base_resp` | 基础响应 |

---

## 8. 音色设计

**POST** `https://api.minimaxi.com/v1/voice_design`

通过文本描述生成新音色，无需上传音频。

### 请求体参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `prompt` | string | **是** | 音色描述文本 |
| `preview_text` | string | **是** | 试听文本，≤500 字符。试听合成收费 **2 元/万字符** |
| `voice_id` | string | 否 | 自定义 voice_id，不传则自动生成 |
| `aigc_watermark` | boolean | 否 | 水印，默认 false |

### 请求示例

```bash
curl --request POST \
  --url https://api.minimaxi.com/v1/voice_design \
  --header 'Authorization: Bearer <API_KEY>' \
  --header 'Content-Type: application/json' \
  --data '{
    "prompt": "讲述悬疑故事的播音员，声音低沉富有磁性，语速时快时慢，营造紧张神秘的氛围。",
    "preview_text": "夜深了，古屋里只有他一人。窗外传来若有若无的脚步声，他屏住呼吸，慢慢地，慢慢地，走向那扇吱呀作响的门……"
  }'
```

### 响应示例

```json
{
  "trial_audio": "<hex 编码音频>",
  "voice_id": "ttv-voice-2025060717322425-xxxxxxxx",
  "base_resp": {
    "status_code": 0,
    "status_msg": "success"
  }
}
```

### 响应参数

| 字段 | 说明 |
|------|------|
| `voice_id` | 生成的音色 ID，可用于语音合成 |
| `trial_audio` | hex 编码的试听音频 |

---

## 9. 声音管理 — 查询可用音色

**POST** `https://api.minimaxi.com/v1/get_voice`

查询当前账号下所有可用音色 ID，包括系统音色、克隆音色、AI 生成音色。

> 快速复刻/音色生成的音色需**正式调用过一次**才会出现在列表中。

### 请求体参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `voice_type` | enum | **是** | `system` / `voice_cloning` / `voice_generation` / `all` |

### 请求示例

```bash
curl --request POST \
  --url https://api.minimaxi.com/v1/get_voice \
  --header 'Authorization: Bearer <API_KEY>' \
  --header 'Content-Type: application/json' \
  --data '{ "voice_type": "all" }'
```

### 响应示例

```json
{
  "system_voice": [
    {
      "voice_id": "Chinese (Mandarin)_Reliable_Executive",
      "description": ["一位沉稳可靠的中年男性高管声音，标准普通话。"],
      "voice_name": "沉稳高管",
      "created_time": "1970-01-01"
    },
    {
      "voice_id": "Chinese (Mandarin)_News_Anchor",
      "description": ["一位专业、播音腔的中年女性新闻主播，标准普通话。"],
      "voice_name": "新闻女声",
      "created_time": "1970-01-01"
    }
  ],
  "voice_cloning": [
    { "voice_id": "test12345", "description": [], "created_time": "2025-08-20" }
  ],
  "voice_generation": [
    { "voice_id": "ttv-voice-2025082011321125-2uEN0X1S", "description": [], "created_time": "2025-08-20" }
  ],
  "base_resp": { "status_code": 0, "status_msg": "success" }
}
```

### 响应字段

| 字段 | 说明 |
|------|------|
| `system_voice` | 系统预置音色列表 |
| `voice_cloning` | 快速复刻的音色列表 |
| `voice_generation` | 音色设计生成的音色列表 |

每个音色对象包含 `voice_id`、`description`、`voice_name`、`created_time`。

---

## 10. 声音管理 — 删除音色

**POST** `https://api.minimaxi.com/v1/delete_voice`

永久删除指定的音色。仅支持 Voice Cloning 或 Voice Generation 创建的音色。

> ⚠️ **删除后不可恢复，voice_id 无法再次使用！**

### 请求体参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `voice_type` | enum | **是** | `voice_cloning` 或 `voice_generation` |
| `voice_id` | string | **是** | 要删除的音色 ID |

### 请求示例

```bash
curl --request POST \
  --url https://api.minimaxi.com/v1/delete_voice \
  --header 'Authorization: Bearer <API_KEY>' \
  --header 'Content-Type: application/json' \
  --data '{
    "voice_type": "voice_cloning",
    "voice_id": "yanshang11123"
  }'
```

### Python 示例

```python
import requests

url = "https://api.minimaxi.com/v1/delete_voice"
payload = {
    "voice_type": "voice_cloning",
    "voice_id": "yanshang11123"
}
headers = {
    "Content-Type": "application/json",
    "Authorization": "Bearer <API_KEY>"
}

response = requests.post(url, json=payload, headers=headers)
print(response.text)
```

### 响应示例

```json
{
  "voice_id": "yanshang11123",
  "created_time": "1728962464",
  "base_resp": {
    "status_code": 0,
    "status_msg": "success"
  }
}
```

---

## 附录：language_boost 支持语种

`Chinese`, `Chinese,Yue`, `English`, `Arabic`, `Russian`, `Spanish`, `French`, `Portuguese`, `German`, `Turkish`, `Dutch`, `Ukrainian`, `Vietnamese`, `Indonesian`, `Japanese`, `Italian`, `Korean`, `Thai`, `Polish`, `Romanian`, `Greek`, `Czech`, `Finnish`, `Hindi`, `Bulgarian`, `Danish`, `Hebrew`, `Malay`, `Persian`, `Slovak`, `Swedish`, `Croatian`, `Filipino`, `Hungarian`, `Norwegian`, `Slovenian`, `Catalan`, `Nynorsk`, `Tamil`, `Afrikaans`, `auto`

> ⚠️ speech-01 / speech-02 系列不支持 Persian、Filipino、Tamil。

---

## 接口速查表

| # | 接口 | 方法 | 路径 |
|---|------|------|------|
| 1 | 同步语音合成 | POST | `/v1/t2a_v2` |
| 2 | 同步语音合成 (WS) | WSS | `/v1/t2a_v2` |
| 3 | 创建异步任务 | POST | `/v1/t2a_async_v2` |
| 4 | 查询任务状态 | GET | `/v1/query/t2a_async_query_v2` |
| 5 | 上传复刻音频 | POST | `/v1/files/upload` (purpose=voice_clone) |
| 6 | 上传示例音频 | POST | `/v1/files/upload` (purpose=prompt_audio) |
| 7 | 执行复刻 | POST | `/v1/voice_clone` |
| 8 | 音色设计 | POST | `/v1/voice_design` |
| 9 | 查询可用音色 | POST | `/v1/get_voice` |
| 10 | 删除音色 | POST | `/v1/delete_voice` |
