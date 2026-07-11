"""C4: 记忆。"""
import time
from Module.system_prompt.protocol import Jinja2Section


class MemorySection(Jinja2Section):
    name = "Memory"
    version = "1.0.0"
    description = "她记得的过去"

    def __init__(self, template_path: str, long_term=None, session=None):
        super().__init__(template_path)
        self._long_term = long_term
        self._session = session
        self._user_message = ""

    def set_context(self, **kwargs) -> None:
        self._user_message = kwargs.get("user_message", "")

    async def render(self) -> str:
        if self._long_term is None or not self._user_message:
            return ""
        try:
            results = await self._long_term.search(query_text=self._user_message, top_k=5)
        except Exception:
            return ""
        if not results:
            return ""
        now = time.time()
        micro = []
        for entry in results:
            micro.append({
                "content": entry.text,
                "type": entry.memory_type,
                "timestamp": entry.timestamp if entry.timestamp else now,
                "poignancy": entry.poignancy,
            })
        return self._render_template(memory_ctx={"micro": micro, "macro": []})
