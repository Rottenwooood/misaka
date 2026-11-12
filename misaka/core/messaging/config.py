"""Where the centre is, and the token this process may speak with.

Both come from the environment so a token never enters a project directory, a card or a
log. No centre configured means no centre: local work continues and cards that expect
deliveries refuse to start rather than run half-connected.
"""
from __future__ import annotations

import os

from misaka.core.messaging.client import CenterClient


def center() -> CenterClient | None:
    url = os.environ.get("MISAKA_CENTER_URL", "").strip()
    token = os.environ.get("MISAKA_CENTER_TOKEN", "").strip()
    if not url or not token:
        return None
    return CenterClient(url, token)
