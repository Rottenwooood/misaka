"""Turn a delivery into a row the addressed card will read.

The centre knows endpoints and types, not cards. The sender puts the card ID in the content;
this module reads it and posts to that card's session. Without a card ID the endpoint decides:
the instance's live session is the recipient. A delivery that names neither is recorded and
dropped, because a retry would only repeat the same mistake.
"""
from __future__ import annotations

import json
import logging
import re

from misaka.core import session_catalog
from misaka.core.network import messages

logger = logging.getLogger(__name__)

CARD = re.compile(r"t_[0-9a-f]{6}")


def card_id(item: dict) -> str | None:
    content = item.get("content")
    if isinstance(content, dict):
        for key in ("card", "card_id", "task", "task_id"):
            value = content.get(key)
            if isinstance(value, str) and CARD.fullmatch(value):
                return value
    if isinstance(content, str):
        found = CARD.search(content)
        if found:
            return found.group(0)
    return None


def sender_of(item: dict) -> str:
    return f"centre:{item.get('source') or 'unknown'}"


def body_of(item: dict) -> str:
    content = item.get("content")
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return f"[{item.get('type')}] from {item.get('source') or 'unknown'}\n{text}"


def summary_of(item: dict) -> str:
    return f"{item.get('type')} from {item.get('source') or 'unknown'}"[:200]


def deliver(items: list[dict]) -> list[str]:
    """Post each delivery to its card; returns the IDs that need no retry.

    A message naming no card, or one the card cannot take, is acknowledged rather than
    retried: the queue would otherwise repeat it forever.
    """
    settled = []
    for item in items:
        message_id = str(item.get("id") or "")
        address = card_id(item)
        if not address:
            endpoint = item.get("endpoint")
            session = session_catalog.live_instance_session(endpoint)
            if session is not None:
                address = session["id"]
        if not address:
            logger.warning("delivery %s names no card and no live instance; dropped", message_id)
            settled.append(message_id)
            continue
        try:
            target = messages.resolve_address(address, sender=sender_of(item), space=None)
            messages.post(target, body=body_of(item), summary=summary_of(item),
                          sender=sender_of(item))
            settled.append(message_id)
        except (ValueError, OSError) as error:
            logger.warning("delivery %s not taken by %s: %s", message_id, address, error)
            settled.append(message_id)
    return settled
