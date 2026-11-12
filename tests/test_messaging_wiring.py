"""Endpoint declaration, direct messages and instance subscriptions."""
import json
import sqlite3
from contextlib import closing

from misaka.core.messaging import delivery, registry
from misaka.core.network import messages
from tests.work_fixture import BODY, Scenario


class StubClient:
    def __init__(self):
        self.endpoints = []
        self.subscriptions = []
        self.sent = []

    def put_endpoint(self, *args, **kwargs):
        self.endpoints.append((args, kwargs))

    def put_subscription(self, *args):
        self.subscriptions.append(args)

    def send(self, type, content, *, message_id=None, to=None):
        self.sent.append({"type": type, "content": content, "to": to})
        return {"id": "m-9"}


def test_an_instance_is_declared_with_one_endpoint_and_a_direct_subscription(monkeypatch):
    monkeypatch.delenv("MISAKA_CENTER_IDENTITY", raising=False)
    client = StubClient()
    registry.ensure_endpoint(client, "worker-1", "http://127.0.0.1:8710/messages")
    assert client.endpoints == [(("worker-1", "misaka", "http://127.0.0.1:8710/messages"), {})]
    assert client.subscriptions == [("direct:worker-1", "worker-1", "*", "direct")]


def test_a_direct_message_is_published_to_the_instance(monkeypatch):
    client = StubClient()
    monkeypatch.setattr(messages, "_target_instance", lambda target: "worker-1")
    from misaka.core.messaging import config
    monkeypatch.setattr(config, "center", lambda: client)
    message_id = messages._publish({"to_task": "t_a00001"}, "hello", summary="hi",
                                   sender="last-order")
    assert message_id == "m-9"
    assert client.sent == [{"type": "direct",
                            "content": {"text": "hello", "summary": "hi",
                                        "from": "last-order", "card": "t_a00001"},
                            "to": ["worker-1"]}]


def test_no_centre_means_no_publish(monkeypatch):
    from misaka.core.messaging import config
    monkeypatch.setattr(config, "center", lambda: None)
    assert messages._publish({"to_task": "t_a00001"}, "hello", summary="hi",
                             sender="last-order") is None


def test_a_delivery_without_a_card_reaches_the_instances_live_session(monkeypatch):
    from misaka.core import session_catalog
    seen = {}
    monkeypatch.setattr(session_catalog, "live_instance_session", lambda iid: {"id": "s-1"})
    monkeypatch.setattr(messages, "resolve_address",
                        lambda addr, **kw: seen.setdefault("addr", addr) or {"to_addr": addr})
    monkeypatch.setattr(messages, "post", lambda target, **kw: 1)
    settled = delivery.deliver([{"id": "m-1", "type": "direct", "endpoint": "worker-1",
                                 "content": {"text": "hi"}}])
    assert settled == ["m-1"]
    assert seen["addr"] == "s-1"


def test_instance_subscriptions_are_stored_with_the_instance(tmp_path):
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order",
             "config": {"system": "coordinate"}},
            {"op": "instance.create", "id": "lo", "template_id": "lo",
             "template_revision": 1},
        ])
        from misaka.core.platform import tasks, work
        with closing(tasks.connect(s.database)) as con:
            work.set_instance_subscriptions(
                con, "lo", [{"source": "calendar", "type": "reminder", "target": None}])
            row = con.execute("SELECT subscriptions FROM instances WHERE id='lo'").fetchone()
        assert json.loads(row[0]) == [{"source": "calendar", "type": "reminder",
                                       "target": None}]
    finally:
        s.close()
