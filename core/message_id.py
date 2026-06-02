"""Message ID generation."""
from __future__ import annotations

import uuid


def new_message_id() -> str:
    """Return 'msg_' + 26 hex chars from uuid4."""
    return f"msg_{uuid.uuid4().hex[:26]}"
