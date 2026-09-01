# core/prompt/tests/scenarios.py
"""Elysia 场景集——角色无关的输入上下文参数化列表。

注意：build_system_prompt() 不接受 mood 参数（mood 从 engine.state.mood 读取），
故本表不含 mood 字段。泄漏词是静态模板文本，mood 不影响泄漏断言。
"""

SCENARIOS: list[tuple[str, str, str, list[str], str]] = [
    # (id, user_message, stt_emotion, stt_events, tts_provider)
    ("日常", "今天天气不错。", "", [], "minimax"),
    ("严肃", "我想认真跟你聊一件重要的事。", "", [], "minimax"),
    ("悲伤", "我最好的朋友离开我了，我很难过。", "SAD", [], "minimax"),
    ("拒绝", "我需要一点空间，现在不想聊天。", "", [], "minimax"),
    ("调情", "总觉得你说话让人心跳加速呢。", "", [], "minimax"),
    ("工具", "帮我查一下明天的天气。", "", [], "minimax"),
    ("记忆", "你还记得我喜欢吃什么吗？", "", [], "minimax"),
]
