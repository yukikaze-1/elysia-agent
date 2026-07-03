"""C5: 感官输入。"""
from datetime import datetime
from Module.prompt.protocol import Jinja2Section


class SensorySection(Jinja2Section):
    name = "Sensory"
    version = "1.0.0"
    description = "此刻感知：环境/用户行为/语音情绪"

    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._user_message = ""
        self._user_name = ""
        self._stt_emotion = ""
        self._stt_events: list[str] = []

    def set_context(self, **kwargs) -> None:
        self._user_message = kwargs.get("user_message", "")
        self._user_name = kwargs.get("user_name", "")
        self._stt_emotion = kwargs.get("stt_emotion", "")
        self._stt_events = kwargs.get("stt_events", [])

    async def render(self) -> str:
        now = datetime.now()
        sensory_ctx = {
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "time_of_day": self._time_of_day(now.hour),
            "day_of_week": self._day_name(now.weekday()),
            "season": self._season(now.month),
            "perception": self._build_perception(),
            "stt_emotion": self._stt_emotion,
            "stt_emotion_emoji": self._emotion_emoji(),
            "stt_events": self._format_events(),
        }
        return self._render_template(sensory_ctx=sensory_ctx)

    def _build_perception(self) -> str:
        parts = [f"{self._user_name}说：{self._user_message}"]
        if self._stt_emotion and self._stt_emotion != "NEUTRAL":
            parts.append(f"[语调: {self._stt_emotion}]")
        return "\n".join(parts)

    def _emotion_emoji(self) -> str:
        try:
            from core.emotion_engine import stt_emotion_emoji
            return stt_emotion_emoji(self._stt_emotion) if self._stt_emotion else ""
        except ImportError:
            return ""

    def _format_events(self) -> str:
        if not self._stt_events:
            return ""
        try:
            from core.emotion_engine import stt_event_emoji
            parts = []
            for evt in self._stt_events:
                emoji = stt_event_emoji(evt)
                parts.append(f"{evt} {emoji}" if emoji else evt)
            return ", ".join(parts)
        except ImportError:
            return ", ".join(self._stt_events)

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
