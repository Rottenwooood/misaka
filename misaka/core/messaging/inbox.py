"""Receive a delivery from the centre, once, and hand the new messages on.

The centre delivers at least once: the same batch can arrive twice after a retry or a
restart. This module is where "once" is decided, by message ID, before anything reaches a
session. A batch is acknowledged only after the new messages have been stored.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from misaka.core.network import messages


class BadDelivery(ValueError):
    pass


class Inbox:
    def __init__(self, connect: Callable | None = None) -> None:
        self._connect = connect or messages.connect

    def pending(self) -> list[dict]:
        """Rows taken but never handed over, oldest first; retried on the next delivery."""
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, endpoint, source FROM inbox_messages "
                "WHERE delivered_at IS NULL ORDER BY received_at, id").fetchall()
        finally:
            con.close()
        return [{"id": row[0], "endpoint": row[1], "source": row[2]} for row in rows]

    def mark_delivered(self, ids: list[str]) -> None:
        if not ids:
            return
        con = self._connect()
        try:
            with con:
                con.executemany(
                    "UPDATE inbox_messages SET delivered_at=strftime('%s','now') WHERE id=?",
                    [(value,) for value in ids])
        finally:
            con.close()

    def accept(self, payload) -> list[dict]:
        """Store unseen messages by ID and return only the ones this call added."""
        if not isinstance(payload, dict):
            raise BadDelivery("delivery must be an object")
        batch = payload.get("messages")
        if not isinstance(batch, list):
            raise BadDelivery("delivery.messages must be a list")
        fresh = []
        con = self._connect()
        try:
            with con:
                for item in batch:
                    if not isinstance(item, dict):
                        raise BadDelivery("each message must be an object")
                    message_id = item.get("id")
                    if not isinstance(message_id, str) or not message_id.strip():
                        raise BadDelivery("each message needs an id")
                    if not isinstance(item.get("type"), str) or not item["type"].strip():
                        raise BadDelivery("each message needs a type")
                    cursor = con.execute(
                        "INSERT OR IGNORE INTO inbox_messages(id,endpoint,source,received_at) "
                        "VALUES(?,?,?,strftime('%s','now'))",
                        (message_id, str(payload.get("endpoint") or ""),
                         str(item.get("source") or "")),
                    )
                    if cursor.rowcount == 1:
                        fresh.append(item)
        finally:
            con.close()
        return fresh


class CallbackServer:
    """A local HTTP door the centre posts to; the only listener this process opens."""

    def __init__(self, inbox: Inbox, on_batch: Callable[[list[dict]], list[str]],
                 host: str = "127.0.0.1", port: int = 0) -> None:
        self.inbox = inbox
        self.on_batch = on_batch
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        inbox, on_batch = self.inbox, self.on_batch

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server's spelling
                if self.path.split("?")[0] != "/messages":
                    self.send_error(404)
                    return
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    fresh = inbox.accept(json.loads(raw or b"{}"))
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "invalid delivery")
                    return
                try:
                    batch = {row["id"]: row for row in inbox.pending()}
                    batch.update({item["id"]: item for item in fresh})
                    if batch:
                        inbox.mark_delivered(on_batch(list(batch.values())))
                except Exception:
                    # Acknowledging before the work is done would lose the batch.
                    self.send_error(500, "not taken over")
                    return
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer((self.host, self.port), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        self.port = server.server_address[1]
        return f"http://{self.host}:{self.port}/messages"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
