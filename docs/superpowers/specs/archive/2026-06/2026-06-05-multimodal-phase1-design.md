# 多模态图片支持 — Phase 1: 用户→AI

打通用户发送图片给 AI 的完整链路，支持 MIMO-v2.5 图片理解。

## 核心设计

**统一 `content` 为 multimodal parts 数组**：定义 Part dataclass（`TextPart`、`ImagePart`、`FilePart`、`AudioPart`、`VideoPart`），`ChatMessageV2.content` 为 `list[Part]`。直接对齐 OpenAI multimodal content 格式。

```
纯文本:  [{"type": "text", "content": "你好"}]
带图:    [{"type": "image", "source": "...", "mime": "image/png"}, {"type": "text", "content": "..."}]
```

## 改动清单

| 文件 | 改动 |
|------|------|
| `Module/schema/message_format.py` (新增) | 5 种 Part dataclass + `ChatMessageV2` + `content_to_openai()` 转换 + `_resolve_image_source()` base64 转换 |
| `Module/schema/message.py` | 删除（废弃，由 message_format.py 替代） |
| `Module/schema/__init__.py` | 修复已删除 message.py 的 import |
| `core/memory/session.py` | `add_message()` 兼容 content: list[dict] 和旧 str 格式；`messages_for_client()` 适配新格式 |
| `core/prompt_assembler.py` | `build_messages()` 支持 content 参数，`content_to_openai()` 转 OpenAI 格式 |
| `core/cognitive_loop.py` | `process()` / `_think_loop()` 支持 content 参数 |
| `core/agent.py` | `process_message()` 支持 content 参数 |
| `core/input/server.py` | `POST /api/images`（图片上传）、`POST /api/chat`（curl 测试）、WS 多模态消息处理 |
| `core/app_config.py` | `ServerConfig` 加 `image_max_mb`、`image_allowed_types` |
| `Module/config/validator.py` | 修复 `list[str]` 参数化泛型 `isinstance` 兼容 |
| `config.yaml` | `server.image_max_mb`、`server.image_allowed_types` |
| `todo.md` | 多模态 Phase 2 项 |

## 关键设计决策

1. **MIMO image_url 块在前**：MIMO 要求 content 数组中 image_url 先于 text，`content_to_openai()` 做了排序
2. **本地图片转 base64**：`_resolve_image_source()` 检测 localhost URL 后自动从 `data/images/` 或 `static/` 读取文件转 `data:image/png;base64,...`
3. **向后兼容**：`ChatMessageV2.from_dict()` 接收旧 str content → 包装为 `[TextPart]`；`SessionMemory.add_message()` str → `[{"type": "text", "content": "..."}]`

## 验证

```bash
# 上传图片
curl -F "image=@test.png" http://localhost:8765/api/images
# → {"url": "/static/images/xxx.png"}

# 发带图消息
curl -X POST http://localhost:8765/api/chat \
  -H "Content-Type: application/json" \
  -d '{"content": [{"type": "text", "content": "图片里有什么？"}, {"type": "image", "source": "http://localhost:8765/static/images/xxx.png", "mime": "image/png"}]}'

# 测试
python -m pytest Module/ -v --asyncio-mode=auto
python -m pytest core/tests/ -v --asyncio-mode=auto
```

## Phase 2（todo）

- AI → 用户图片输出（工具返回图片 / LLM 生成图片）
- 前端图片交互（粘贴/拖拽/文件选择上传 + 图片气泡渲染）
- 文件支持（`type: "file"` content part）
- 音频/视频 Part 全链路
- assistant 消息 content_type 迁入 content array
