"""Declare an instance at the centre: one endpoint and the subscription that carries
direct messages to it."""
from __future__ import annotations

import os

from misaka.core.messaging.client import CenterClient


def identity() -> str:
    """The centre identity this install speaks as; the endpoint is registered under it."""
    return os.environ.get("MISAKA_CENTER_IDENTITY", "").strip() or "misaka"


def ensure_endpoint(client: CenterClient, instance_id: str, inbox_url: str) -> None:
    client.put_endpoint(instance_id, identity(), inbox_url)
    client.put_subscription(f"direct:{instance_id}", instance_id, "*", "direct")
