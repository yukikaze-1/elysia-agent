"""C5: 感官输入。"""
from datetime import datetime
from Module.system_prompt.protocol import Jinja2Section

# STT 情绪 → emoji 映射（内联自 core.schema.mood，消除跨层依赖）
_STT_EMOTION_EMOJI: dict[str, str] = {
    "HAPPY": "\U0001f60a", "SAD": "\U0001f614", "ANGRY": "\U0001f621",
    "NEUTRAL": "", "FEARFUL": "\U0001f630", "DISGUSTED": "\U0001f922",
    "SURPRISED": "\U0001f62e",
}


def _stt_emotion_emoji(tag: str) -> str:
    return _STT_EMOTION_EMOJI.get(tag, "")


# STT 事件类型 → emoji 映射（内联自 core.schema.mood，消除跨层依赖）
_STT_EVENT_EMOJI: dict[str, str] = {
    "Speech": "", "BGM": "\U0001f3bc", "Applause": "\U0001f44f",
    "Laughter": "\U0001f600", "Cry": "\U0001f62d",
    "Cough": "\U0001f637", "Sneeze": "\U0001f927",
    "Breath": "", "Sing": "", "Speech_Noise": "", "GBG": "",
    "Event_UNK": "",
}


def _stt_event_emoji(event: str) -> str:
    return _STT_EVENT_EMOJI.get(event, "")


class SensorySection(Jinja2Section):
    name = "Sensory"
    version = "1.2.0"
    description = "此刻感知：环境事实 + 输入来源 + 语音情绪（用户消息由消息列表传递）"

    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._stt_emotion = ""
        self._stt_events: list[str] = []

    def set_context(self, **kwargs) -> None:
        self._stt_emotion = kwargs.get("stt_emotion", "")
        self._stt_events = kwargs.get("stt_events", [])

    async def render(self) -> str:
        now = datetime.now()
        # 推导输入来源：STT emotion 非空 → 语音，空 → 文字
        input_source = "语音" if self._stt_emotion else "文字"
        sensory_ctx = {
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "time_of_day": self._time_of_day(now.hour),
            "day_of_week": self._day_name(now.weekday()),
            "season": self._season(now.month),
            "input_source": input_source,
            "voice": self._build_voice(),
        }
        return self._render_template(sensory_ctx=sensory_ctx)

    def _build_voice(self) -> str:
        """构建 <voice> 内容。无 STT 数据时返回空串。"""
        parts = []
        if self._stt_emotion and self._stt_emotion != "NEUTRAL":
            emoji = self._emotion_emoji()
            parts.append(f"语调: {self._stt_emotion} {emoji}".rstrip())
        events = self._format_events()
        if events:
            parts.append(f"事件: {events}")
        return " | ".join(parts) if parts else ""

    def _emotion_emoji(self) -> str:
        return _stt_emotion_emoji(self._stt_emotion) if self._stt_emotion else ""

    def _format_events(self) -> str:
        if not self._stt_events:
            return ""
        parts = []
        for evt in self._stt_events:
            emoji = _stt_event_emoji(evt)
            parts.append(f"{evt} {emoji}" if emoji else evt)
        return ", ".join(parts)

    @staticmethod
    def _day_name(weekday: int) -> str:
        return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][weekday]

    @staticmethod
    def _time_of_day(hour: int) -> str:
        if 5 <= hour < 12:
            return "早晨"
        elif 12 <= hour < 14:
            return "中午"
        elif 14 <= hour < 18:
            return "下午"
        elif 18 <= hour < 22:
            return "傍晚"
        else:
            return "夜晚"

    @staticmethod
    def _season(month: int) -> str:
        if 3 <= month <= 5:
            return "春"
        elif 6 <= month <= 8:
            return "夏"
        elif 9 <= month <= 11:
            return "秋"
        else:
            return "冬"
