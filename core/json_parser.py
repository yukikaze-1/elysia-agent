"""TagParser — streaming XML-tag parser for dual-track LLM output.

Parses:
  <public_reply>visible text</public_reply>
  <inner_voice>hidden thoughts</inner_voice>
  <mood>emotional state</mood>

Yields public_reply content immediately for the text-delta / TTS pipeline.
inner_voice is accumulated for debug logging. mood is captured at the end.
"""

from __future__ import annotations

from enum import Enum, auto
import re


class Field(Enum):
    PUBLIC_REPLY = auto()
    INNER_VOICE = auto()
    MOOD = auto()


# Tag patterns — opening and closing.
_TAGS = {
    Field.PUBLIC_REPLY: ("<public_reply>", "</public_reply>"),
    Field.INNER_VOICE:  ("<inner_voice>",  "</inner_voice>"),
    Field.MOOD:          ("<mood>",         "</mood>"),
}

_OPEN_RE = re.compile(r"<(public_reply|inner_voice|mood)>")
_CLOSE_RE = re.compile(r"</(public_reply|inner_voice|mood)>")


class TagParser:
    """Streaming parser for XML-tag-delimited dual-track output.

    The LLM is prompted to output in this format:
      <public_reply>user-visible text</public_reply>
      <inner_voice>private thoughts</inner_voice>
      <mood>current emotion</mood>

    The parser emits public_reply content as soon as it's seen (minus
    a small hold-back window to avoid emitting closing tags prematurely).
    inner_voice is accumulated silently. mood is available via finish().
    """

    # Characters to hold back when yielding public_reply, to avoid
    # emitting the "</public_reply>" closing tag as text.
    _HOLD = len("</public_reply>")  # 16

    def __init__(self):
        self._field: Field | None = None     # current parsing context
        self._buf: str = ""                   # pending input
        self._public_reply: str = ""
        self._inner_voice: str = ""
        self._mood: str = ""
        self._done: bool = False

    def feed(self, chunk: str) -> dict:
        """Feed a text chunk. Returns {"field": Field, "content": "..."} or {"field": None}."""
        if self._done or not chunk:
            return {"field": None, "content": None}

        self._buf += chunk

        # If we haven't entered any tag yet, look for the first opening tag
        if self._field is None:
            return self._scan_opening()

        # Inside a tag context — accumulate until closing tag
        close_tag = _TAGS[self._field][1]
        end = self._buf.find(close_tag)

        if end == -1:
            # Closing tag not yet seen — yield safe content for public_reply
            if self._field == Field.PUBLIC_REPLY:
                return self._yield_safe_public()
            return {"field": None, "content": None}

        # Found closing tag — extract content before it
        content = self._buf[:end]
        after = self._buf[end + len(close_tag):]

        if self._field == Field.PUBLIC_REPLY:
            self._public_reply += content
        elif self._field == Field.INNER_VOICE:
            self._inner_voice += content
        elif self._field == Field.MOOD:
            self._mood += content.strip()

        result = {
            "field": self._field,
            "content": content,
        } if content else {"field": None, "content": None}

        self._buf = after
        self._field = None  # ready for next tag

        # Scan for another opening tag immediately in the remaining buffer
        if result["field"] is None and after:
            return self._scan_opening()

        return result

    def _scan_opening(self) -> dict:
        """Look for the next opening tag in the buffer."""
        m = _OPEN_RE.search(self._buf)
        if not m:
            return {"field": None, "content": None}

        tag_name = m.group(1)
        # Discard everything before the opening tag (text outside tags is ignored)
        self._buf = self._buf[m.end():]

        self._field = {
            "public_reply": Field.PUBLIC_REPLY,
            "inner_voice": Field.INNER_VOICE,
            "mood": Field.MOOD,
        }[tag_name]

        # Recurse to immediately process content after the opening tag
        return self._scan_opening() if not self._buf else self.feed("")

    def _yield_safe_public(self) -> dict:
        """Yield public_reply content, holding back a safety margin."""
        if len(self._buf) <= self._HOLD:
            return {"field": None, "content": None}

        safe = self._buf[:-self._HOLD]
        self._buf = self._buf[-self._HOLD:]
        self._public_reply += safe
        return {"field": Field.PUBLIC_REPLY, "content": safe}

    def finish(self) -> dict:
        """Call after the stream ends. Returns final extracted fields.

        If no tags were found at all, the entire buffer is treated as
        public_reply (graceful fallback for non-tag output).
        """
        if self._done:
            return {
                "public_reply": self._public_reply,
                "inner_voice": self._inner_voice,
                "mood": self._mood,
            }

        if self._field is None and self._buf and not self._public_reply:
            # No tags found at all — treat whole response as public_reply
            self._public_reply = self._buf.strip()

        # Flush remaining buffer for current field
        if self._field == Field.PUBLIC_REPLY and self._buf:
            clean = _CLOSE_RE.sub("", self._buf).strip()
            self._public_reply += clean
        elif self._field == Field.INNER_VOICE and self._buf:
            clean = _CLOSE_RE.sub("", self._buf).strip()
            self._inner_voice += clean
        elif self._field == Field.MOOD and self._buf:
            clean = _CLOSE_RE.sub("", self._buf).strip()
            self._mood += clean

        self._done = True
        return {
            "public_reply": self._public_reply,
            "inner_voice": self._inner_voice,
            "mood": self._mood,
        }
