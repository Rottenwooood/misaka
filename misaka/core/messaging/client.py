"""The notification centre as a dependency.

The centre owns delivery, identity and rate limiting. This module only speaks its HTTP
surface: publish a message, declare an endpoint, declare or drop a subscription. It holds
no policy of its own and never inspects what a message means.
"""
from __future__ import annotations

import httpx


class CenterError(RuntimeError):
    """A refusal from the centre, kept as a status and a short body."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"centre refused the request ({status}): {message}")
        self.status = status
        self.message = message


class CenterClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        response = httpx.request(
            method,
            f"{self.base_url}{path}",
            json=payload,
            headers={"authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise CenterError(response.status_code, response.text[:400])
        return response.json() if response.content else {}

    def send(self, type: str, content, *, message_id: str | None = None,
             to: list[str] | None = None) -> dict:
        body: dict = {"type": type, "content": content}
        if message_id is not None:
            body["id"] = message_id
        if to:
            body["to"] = list(to)
        return self.request("POST", "/api/messages", body)

    def put_endpoint(self, name: str, owner: str, url: str, *,
                     headers: dict[str, str] | None = None,
                     max_wait_ms: int = 0, min_interval_ms: int = 2000) -> dict:
        delivery: dict = {"kind": "webhook", "config": {"url": url}}
        if headers:
            delivery["config"]["headers"] = headers
        return self.request("PUT", "/api/endpoints", {
            "name": name,
            "owner": owner,
            "delivery": delivery,
            "max_wait_ms": max_wait_ms,
            "min_interval_ms": min_interval_ms,
        })

    def put_subscription(self, name: str, endpoint: str, source: str, type: str) -> dict:
        return self.request("PUT", "/api/subscriptions", {
            "name": name, "endpoint": endpoint, "source": source, "type": type,
        })

    def drop_subscription(self, name: str) -> None:
        self.request("DELETE", f"/api/subscriptions/{name}")

