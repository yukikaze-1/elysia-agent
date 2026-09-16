"""MessageBuilder 集成测试。"""
import pytest
from core.prompt.message_builder import MessageBuilder
from core.schema.message import UserMessage, AssistantMessage, ToolResultMessage, MessageId
from core.schema.part import (
    AudioPart,
    FilePart,
    ImagePart,
    StickerPart,
    TextPart,
    VideoPart,
)
from core.schema.tool import ToolCall, ToolCallFunction

pytestmark = pytest.mark.unit   # 纯 Pydantic 模型 + MessageBuilder 组装，无 DB/网络、无 mock


def _make_user_msg(content: str = "Hi", msg_id: str = "u1") -> UserMessage:
    return UserMessage(content=[TextPart(content=content)], message_id=MessageId(msg_id))


def _make_assistant_msg(
    content: str = "Hello",
    msg_id: str = "a1",
    inner_voice: str | None = None,
    tool_calls: list[ToolCall] | None = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=[TextPart(content=content)],
        message_id=MessageId(msg_id),
        inner_voice=inner_voice,
        tool_calls=tool_calls,
    )


def _make_user_msg_multimodal(parts: list) -> UserMessage:
    return UserMessage(content=parts, message_id=MessageId("um"))


class TestBuildMessages:
    def test_basic_assembly(self):
        """基本 messages 组装：system + history + user。"""
        builder = MessageBuilder()
        system = "You are a helpful assistant."
        history = [
            _make_user_msg("Hi"),
            _make_assistant_msg("Hello!", inner_voice="someone said hi"),
        ]
        messages = builder.build_messages(system, history, "How are you?")
        assert messages[0] == {"role": "system", "content": system}
        assert len(messages) >= 3
        assistant_has_iv = any(
            "<inner_voice>" in m.get("content", "") for m in messages if m["role"] == "assistant"
        )
        assert assistant_has_iv

    def test_tool_message_skipped(self):
        """工具结果消息被跳过。"""
        builder = MessageBuilder()
        history = [
            _make_assistant_msg(
                "let me check",
                tool_calls=[ToolCall(
                    id="c1",
                    function=ToolCallFunction(name="web_search", arguments='{"query":"test"}'),
                )],
            ),
            ToolResultMessage(
                content=[TextPart(content="search results here")],
                tool_call_id="c1", tool_name="web_search",
                message_id=MessageId("msg_test_t1"),
            ),
            _make_assistant_msg("found it"),
        ]
        messages = builder.build_messages("system", history, "thanks")
        roles = [m["role"] for m in messages]
        assert "tool" not in roles

    def test_multimodal_content_list_supported(self):
        """content 为 list（image 类型）且 supports_multimodal=True 时正常传递。"""
        builder = MessageBuilder()
        content = [
            {"type": "text", "content": "look at this"},
            {"type": "image", "src": "data:image/png;base64,xxx"},
        ]
        messages = builder.build_messages("system", [], content, supports_multimodal=True)
        user_msg = [m for m in messages if m["role"] == "user"][0]
        assert isinstance(user_msg["content"], list)

    def test_multimodal_unsupported_image_fallback(self):
        """content 为 list + 有 image 类型 + supports_multimodal=False → describe_image 提示。"""
        builder = MessageBuilder()
        content = [
            {"type": "text", "content": "check this"},
            {"type": "image", "src": "http://a.com/1.png"},
        ]
        messages = builder.build_messages("system", [], content, supports_multimodal=False)
        user_content = messages[-1]["content"]
        assert "describe_image" in user_content

    def test_tool_summary_injected(self):
        """tool_calls → _format_tool_summary 注入到 assistant 消息。"""
        builder = MessageBuilder()
        history = [
            _make_assistant_msg(
                "let me search",
                tool_calls=[ToolCall(
                    id="c1",
                    function=ToolCallFunction(name="search", arguments='{"q":"test"}'),
                )],
            ),
            ToolResultMessage(
                content=[TextPart(content="result: found")],
                tool_call_id="c1", tool_name="search",
                message_id=MessageId("msg_test_t2"),
            ),
        ]
        messages = builder.build_messages("system", history, "ok")
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        has_tool_summary = any("search" in m.get("content", "") for m in assistant_msgs)
        assert has_tool_summary

    def test_inner_voice_with_str_content(self):
        """inner_voice 注入：字符串 content + iv → 包含 <inner_voice>。"""
        builder = MessageBuilder()
        history = [
            _make_assistant_msg("Hello", inner_voice="thinking about greeting"),
        ]
        messages = builder.build_messages("system", history, "bye")
        assistant = [m for m in messages if m["role"] == "assistant"][0]
        assert "<inner_voice>thinking about greeting</inner_voice>" in assistant["content"]

    def test_format_tool_summary_json_parse_fails(self):
        """_format_tool_summary JSON 解析失败时不崩，返回 name 基本字符串。"""
        builder = MessageBuilder()
        history = [
            _make_assistant_msg(
                "",
                tool_calls=[ToolCall(
                    id="c1",
                    function=ToolCallFunction(name="test", arguments="not-json"),
                )],
            ),
            ToolResultMessage(
                content=[TextPart(content="ok")],
                tool_call_id="c1", tool_name="test",
                message_id=MessageId("msg_test_t3"),
            ),
        ]
        messages = builder.build_messages("system", history, "done")
        assistant = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant) > 0
        # JSON 解析失败时只输出工具名，不含参数
        assert "[已调用: test]" in assistant[0]["content"]

    def test_single_image_unsupported(self):
        """单张图片 + 不支持多模态 → describe_image 提示。"""
        builder = MessageBuilder()
        content = [
            {"type": "text", "content": "hello"},
            {"type": "image", "src": "http://a.com/1.png"},
        ]
        messages = builder.build_messages("system", [], content, supports_multimodal=False)
        assert "describe_image" in messages[-1]["content"]

    def test_no_user_content(self):
        """user_content 为 "" → 不添加 user message。"""
        builder = MessageBuilder()
        messages = builder.build_messages("system", [_make_user_msg("hi")], "")
        assert len(messages) == 2

    def test_history_image_count_multiple(self):
        """历史消息中有多张图片 + 不支持多模态 → 数量提示。"""
        builder = MessageBuilder()
        history = [
            _make_user_msg_multimodal([
                ImagePart(src="http://a.com/1.png"),
                ImagePart(src="http://a.com/2.png"),
            ]),
            _make_assistant_msg("nice pics"),
        ]
        messages = builder.build_messages("system", history, "thanks", supports_multimodal=False)
        user_msgs = [m for m in messages if m["role"] == "user"]
        two_image_msg = [m for m in user_msgs if "2张" in m.get("content", "")]
        assert len(two_image_msg) > 0


# ============================================================================
# S2-H8：贴图/附件-only 消息的占位文本（当前轮 + 历史侧）
# ============================================================================

class TestMediaOnlyPlaceholder:
    """转换器只发射 Text/Image；sticker/file/audio/video 必须留占位（不得静默消失）。

    缺陷形状：契约允许"仅贴图/仅附件"消息（content 为空），而它们此前变成
    `{"role":"user","content":[]}` ——模型像"用户什么都没说"一样作答；历史侧
    `_history_message_content` 还返回 None 把它们**整条删掉**（实测 8 条存量消息
    只发出 5 条）。
    """

    def test_sticker_only_current_turn_has_placeholder(self):
        """贴图-only 当前消息（走生产同形的 model_dump）→ 有占位、不是空 content。"""
        builder = MessageBuilder()
        content = [StickerPart(sticker_id="elysia_happy",
                               src="/static/stickers/elysia_happy.png").model_dump()]
        messages = builder.build_messages("system", [], content, supports_multimodal=False)

        user_msg = messages[-1]
        assert user_msg["role"] == "user"
        assert "elysia_happy" in str(user_msg["content"]), (
            f"贴图必须留下可识别的占位：{user_msg['content']!r}")
        assert user_msg["content"] != []

    def test_file_only_current_turn_has_placeholder_multimodal(self):
        """多模态 provider 上 file-only 同样不得变成空 content（带文件名与体积）。"""
        builder = MessageBuilder()
        content = [FilePart(src="/api/attachments/att_1", filename="report.pdf",
                            mime="application/pdf", size_bytes=2048,
                            attachment_id="att_1").model_dump()]
        messages = builder.build_messages("system", [], content, supports_multimodal=True)

        user_msg = messages[-1]
        assert isinstance(user_msg["content"], list)
        joined = " ".join(b.get("text", "") for b in user_msg["content"])
        assert "report.pdf" in joined
        assert "2.0KB" in joined
        assert user_msg["content"] != []

    def test_audio_and_video_placeholders(self):
        builder = MessageBuilder()
        content = [
            AudioPart(src="/api/attachments/att_a", mime="audio/wav",
                      duration_ms=2500).model_dump(),
            VideoPart(src="/api/attachments/att_v", mime="video/mp4",
                      duration_ms=1000).model_dump(),
        ]
        messages = builder.build_messages("system", [], content, supports_multimodal=True)
        joined = " ".join(b.get("text", "") for b in messages[-1]["content"])
        assert "音频" in joined and "2.5s" in joined
        assert "视频" in joined and "1.0s" in joined

    def test_file_without_filename_falls_back_to_attachment_id(self):
        """没有任何可读标识时用 attachment_id 兜底（否则模型只知道"有个东西"）。"""
        builder = MessageBuilder()
        content = [FilePart(src="/api/attachments/att_9",
                            mime="application/octet-stream",
                            attachment_id="att_9").model_dump()]
        messages = builder.build_messages("system", [], content, supports_multimodal=True)
        joined = " ".join(b.get("text", "") for b in messages[-1]["content"])
        assert "att_9" in joined

    def test_media_plus_text_keeps_both(self):
        """有文本时占位与文本都要在（不能二选一）。"""
        builder = MessageBuilder()
        content = [TextPart(content="看看这个").model_dump(),
                   StickerPart(sticker_id="s1",
                               src="/static/stickers/s1.png").model_dump()]
        messages = builder.build_messages("system", [], content, supports_multimodal=False)
        joined = str(messages[-1]["content"])
        assert "看看这个" in joined and "s1" in joined

    def test_history_media_only_message_not_dropped(self):
        """历史侧：media-only 的 user 消息不得被整条丢弃。"""
        builder = MessageBuilder()
        history = [
            _make_user_msg_multimodal([StickerPart(sticker_id="s1", src="/static/stickers/s1.png")]),
            _make_assistant_msg("哈哈"),
        ]
        messages = builder.build_messages("system", history, "嗯", supports_multimodal=False)

        user_entries = [m for m in messages if m["role"] == "user"]
        assert len(user_entries) == 2, f"media-only 历史不得消失：{messages!r}"
        assert "s1" in str(user_entries[0]["content"])

    def test_history_file_placeholder_multimodal(self):
        """历史侧多模态：图片走 image_url，file 走占位（两者都要在）。"""
        builder = MessageBuilder()
        history = [
            _make_user_msg_multimodal([
                ImagePart(src="http://a.com/1.png"),
                FilePart(src="/api/attachments/att_z", filename="a.zip",
                         mime="application/zip", size_bytes=10),
            ]),
        ]
        messages = builder.build_messages("system", history, None, supports_multimodal=True)

        blocks = messages[1]["content"]
        assert any(b.get("type") == "image_url" for b in blocks)
        joined = " ".join(b.get("text", "") for b in blocks)
        assert "a.zip" in joined

    def test_plain_text_message_gets_no_placeholder(self):
        """反空转：纯文本消息不得被塞进任何占位文案（否则每轮都在污染 prompt）。"""
        builder = MessageBuilder()
        messages = builder.build_messages(
            "system", [_make_user_msg("普通文本")], "也普通", supports_multimodal=False)

        assert messages[-1]["content"] == "也普通"
        assert "[" not in str(messages[1]["content"])
