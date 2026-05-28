"""Sentence buffer — splits streaming tokens into complete sentences for TTS.

Accumulates LLM token output and yields complete sentences as they form,
ensuring GPT-SoVITS receives natural prosodic units rather than single chars.
"""

from __future__ import annotations

import re
_HARD_DELIMITERS = "。！？!?\n～…〜"
_DELIMITER_RUN = set(_HARD_DELIMITERS + ".")


def _is_dot_boundary(text: str, dot_pos: int) -> bool:
    """False if . is part of a number (3.14), abbreviation (Dr.), or URL
    (example.com).  True if it's a genuine sentence boundary."""
    # Digit before → number dot
    if dot_pos > 0 and text[dot_pos - 1].isdigit():
        return False
    # Known abbreviations: single uppercase + 1-2 lowercase before dot
    prefix = text[max(0, dot_pos - 3):dot_pos]
    if prefix in {"Dr", "Mr", "Ms", "Mrs", "Prof", "Sr", "Jr", "St", "Mt",
                  "Capt", "Col", "Gen", "Sgt", "Lt", "Rev", "Hon", "Rep", "Sen"}:
        return False
    # All-uppercase abbreviation: U.S., D.C., etc.
    if re.fullmatch(r"[A-Z]{1,2}", prefix):
        return False
    # Alpha on both sides → URL or word boundary
    before = text[dot_pos - 1] if dot_pos > 0 else ""
    after = text[dot_pos + 1] if dot_pos + 1 < len(text) else ""
    if before.isalpha() and after.isalpha():
        return False
    return True


class SentenceBuffer:
    """Accumulates streaming text and yields full sentences."""

    DEFAULT_DELIMITERS = _HARD_DELIMITERS + "."

    def __init__(
        self,
        *,
        delimiters: str | None = None,
        min_chunk_length: int = 5,
    ):
        if delimiters is None:
            delimiters = self.DEFAULT_DELIMITERS
        self._delimiters = delimiters
        self._min_chunk = min_chunk_length
        self._split_pat = re.compile(
            rf"([{re.escape(delimiters.replace('.', ''))}]|[.]?)"
        ) if delimiters else re.compile(r"()")
        self._buffer: str = ""

    # ---- Public API ----

    def feed(self, delta: str) -> str | None:
        """Feed a text delta. Returns a complete sentence if one is ready, else None."""
        self._buffer += delta
        return self._extract()

    def flush(self) -> str | None:
        """Flush remaining buffer content. Returns whatever is left, or None if empty."""
        if self._buffer.strip():
            remaining = self._buffer
            self._buffer = ""
            return remaining
        return None

    def reset(self) -> None:
        self._buffer = ""

    # ---- Internal ----

    def _extract(self) -> str | None:
        """Try to extract a complete sentence from the buffer."""
        text = self._buffer

        # Find the first sentence boundary
        idx = self._find_split(text)
        if idx < 0:
            return None

        # Split into [before delim, delim, rest]
        delim = text[idx]
        if delim == "." and not _is_dot_boundary(text, idx):
            # False positive — abbreviation/number dot, skip this one
            # Advance past this dot and try again
            return self._skip_dot_and_retry(text, idx)

        before = text[:idx]
        after = text[idx + 1:]

        # Merge consecutive delimiters into the same sentence.
        # "啊？" + "！" should be "啊？！", not two short sentences.
        # Allow a single space between delimiters in the run.
        run = delim
        while after and after[0] in _DELIMITER_RUN:
            run += after[0]
            after = after[1:]
        if after and after[0] == " ":
            # Check if space is followed by another delimiter
            rest = after[1:]
            if rest and rest[0] in _DELIMITER_RUN:
                run += " "
                after = rest
                while after and after[0] in _DELIMITER_RUN:
                    run += after[0]
                    after = after[1:]

        complete = before + run

        # If there's nothing before the delimiter, skip this empty extraction
        if not before.strip():
            self._buffer = after
            return self._extract() if self._buffer else None

        complete_stripped = complete.strip()

        # Hold only when consecutive delimiters follow (already merged, after is empty
        # or starts with a delimiter). Holding with text after would block the buffer
        # permanently — _find_split keeps hitting the same delimiter.
        if len(complete_stripped) < self._min_chunk:
            if not after.strip():
                return None  # incomplete — wait for more
            if after and after[0] in _DELIMITER_RUN:
                return None  # consecutive delimiter — merge

        self._buffer = after
        return complete.rstrip(" ")  # trailing spaces only, preserve \n

    def _find_split(self, text: str) -> int:
        """Find the index of the first sentence delimiter in text. Returns -1 if none."""
        best = -1
        # Check each delimiter character
        for ch in self._delimiters:
            pos = text.find(ch)
            if pos >= 0 and (best < 0 or pos < best):
                # Extra check for English period
                if ch == "." and not _is_dot_boundary(text, pos):
                    continue
                best = pos
        return best

    def _skip_dot_and_retry(self, text: str, dot_pos: int) -> str | None:
        """Advance past a false-positive dot and try extracting again."""
        # We can't easily skip; just return None and wait for more context
        # The next char after the dot might clarify things
        return None

