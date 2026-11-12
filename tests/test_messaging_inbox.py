"""A delivery is taken once, and only acknowledged after it has been stored."""
import json
import sqlite3

import httpx
import pytest

from misaka.core.messaging.inbox import BadDelivery, CallbackServer, Inbox


def connect(path):
    def open_db():
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE IF NOT EXISTS inbox_messages("
                    "id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, source TEXT NOT NULL, "
                    "received_at INTEGER NOT NULL)")
        return con
    return open_db


def batch(message_id="m-1"):
    return {"id": "b-1", "endpoint": "worker-1", "messages": [
        {"id": message_id, "type": "test.failed", "content": {"card": "t_a00001"},
         "source": "tests"}]}


def test_a_delivery_is_taken_once(tmp_path):
    inbox = Inbox(connect(tmp_path / "inbox.db"))
    assert [m["id"] for m in inbox.accept(batch())] == ["m-1"]
    assert inbox.accept(batch()) == []


def test_a_malformed_delivery_is_refused(tmp_path):
    inbox = Inbox(connect(tmp_path / "inbox.db"))
    with pytest.raises(BadDelivery):
        inbox.accept({"messages": [{"type": "x"}]})
    with pytest.raises(BadDelivery):
        inbox.accept({"messages": "no"})


def test_the_callback_acknowledges_only_after_the_work(tmp_path):
    inbox = Inbox(connect(tmp_path / "inbox.db"))
    seen = []
    server = CallbackServer(inbox, seen.extend)
    url = server.start()
    try:
        first = httpx.post(url, json=batch(), timeout=5)
        assert first.status_code == 200
        assert [m["id"] for m in seen] == ["m-1"]
        again = httpx.post(url, json=batch(), timeout=5)
        assert again.status_code == 200
        assert [m["id"] for m in seen] == ["m-1"]
    finally:
        server.stop()


def test_a_failed_hand_over_is_not_acknowledged(tmp_path):
    inbox = Inbox(connect(tmp_path / "inbox.db"))
    server = CallbackServer(inbox, lambda _batch: (_ for _ in ()).throw(RuntimeError("busy")))
    url = server.start()
    try:
        assert httpx.post(url, json=batch("m-2"), timeout=5).status_code == 500
    finally:
        server.stop()


def test_a_bad_body_is_a_client_error(tmp_path):
    inbox = Inbox(connect(tmp_path / "inbox.db"))
    server = CallbackServer(inbox, lambda _batch: None)
    url = server.start()
    try:
        assert httpx.post(url, content=b"not json", timeout=5).status_code == 400
    finally:
        server.stop()
