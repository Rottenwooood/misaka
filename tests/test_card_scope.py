"""A card carries the paths it may touch, the commands it may not run, and its subscriptions."""
import json
import sqlite3
from contextlib import closing

from tests.work_fixture import BODY, Scenario


def rows(scenario, sql, args=()):
    with closing(sqlite3.connect(scenario.database)) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(sql, args)]


def card(scenario, card_id):
    return next(row for row in rows(
        scenario, "SELECT * FROM card_state WHERE id=?", (card_id,)))


def subscriptions(scenario, card_id):
    return json.loads(card(scenario, card_id)["subscriptions"] or "[]")


def seed(scenario):
    scenario.apply([
        {"op": "template.create", "id": "sister", "role": "sister",
         "config": {"system": "work", "tools": ["read", "write", "bash"]}},
        {"op": "instance.create", "id": "worker", "template_id": "sister",
         "template_revision": 1},
        {"op": "request.create", "id": "req", "title": "Scope", "owner_id": "lo",
         "workspace": str(scenario.project)},
    ])


def test_a_card_records_scope_commands_and_subscriptions():
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order",
             "config": {"system": "coordinate"}},
            {"op": "instance.create", "id": "lo", "template_id": "lo",
             "template_revision": 1},
        ])
        seed(s)
        s.apply([{"op": "plan.update", "request_id": "req", "expected_revision": 0, "cards": [{
            "id": "t_a00001", "title": "One", "instance_id": "worker", "body": BODY,
            "fs_scope": ["src", "/tmp/shared"],
            "forbidden_commands": ["rm -rf", "git push"],
            "subscriptions": [{"source": "tests", "type": "test.failed"},
                              {"source": "ci", "type": "build.failed", "target": "worker"}],
        }]}])
        stored = card(s, "t_a00001")
        assert json.loads(stored["fs_scope"]) == ["src", "/tmp/shared"]
        assert json.loads(stored["forbidden_commands"]) == ["rm -rf", "git push"]
        assert subscriptions(s, "t_a00001") == [
            {"source": "tests", "type": "test.failed", "target": None},
            {"source": "ci", "type": "build.failed", "target": "worker"},
        ]
    finally:
        s.close()


def test_saving_again_replaces_the_subscriptions():
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order",
             "config": {"system": "coordinate"}},
            {"op": "instance.create", "id": "lo", "template_id": "lo",
             "template_revision": 1},
        ])
        seed(s)
        s.apply([{"op": "plan.update", "request_id": "req", "expected_revision": 0, "cards": [{
            "id": "t_a00001", "title": "One", "instance_id": "worker", "body": BODY,
            "subscriptions": [{"source": "tests", "type": "test.failed"}],
        }]}])
        s.apply([{"op": "plan.update", "request_id": "req", "expected_revision": 1, "cards": [{
            "id": "t_a00001", "subscriptions": [{"source": "ci", "type": "build.failed"}],
        }]}])
        assert subscriptions(s, "t_a00001") == [
            {"source": "ci", "type": "build.failed", "target": None}]
    finally:
        s.close()


def test_cancelling_a_card_keeps_the_configuration_and_stops_it():
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order",
             "config": {"system": "coordinate"}},
            {"op": "instance.create", "id": "lo", "template_id": "lo",
             "template_revision": 1},
        ])
        seed(s)
        s.apply([{"op": "plan.update", "request_id": "req", "expected_revision": 0, "cards": [{
            "id": "t_a00001", "title": "One", "instance_id": "worker", "body": BODY,
            "subscriptions": [{"source": "tests", "type": "test.failed"}],
        }]}])
        s.apply([{"op": "plan.update", "request_id": "req", "expected_revision": 1, "cards": [{
            "id": "t_a00001", "cancel": True,
        }]}])
        assert subscriptions(s, "t_a00001") == [
            {"source": "tests", "type": "test.failed", "target": None}]
        assert card(s, "t_a00001")["status"] == "stopped"
    finally:
        s.close()


def test_a_bad_subscription_shape_is_refused():
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order",
             "config": {"system": "coordinate"}},
            {"op": "instance.create", "id": "lo", "template_id": "lo",
             "template_revision": 1},
        ])
        seed(s)
        s.apply([{"op": "plan.update", "request_id": "req", "expected_revision": 0, "cards": [{
            "id": "t_a00001", "title": "One", "instance_id": "worker", "body": BODY,
            "subscriptions": [{"source": "tests", "kind": "test.failed"}],
        }]}], code=1)
        assert rows(s, "SELECT * FROM tasks") == []
    finally:
        s.close()


def test_the_view_snapshot_carries_scope_commands_and_subscriptions():
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order", "config": {"system": "c"}},
            {"op": "instance.create", "id": "lo", "template_id": "lo", "template_revision": 1},
        ])
        seed(s)
        s.apply([{"op": "plan.update", "request_id": "req", "expected_revision": 0, "cards": [{
            "id": "t_a00001", "title": "One", "instance_id": "worker", "body": BODY,
            "fs_scope": ["src"], "forbidden_commands": ["git push"],
            "subscriptions": [{"source": "tests", "type": "test.failed"}],
        }]}])
        snapshot = s.snapshot("--project", str(s.project))
        node = next(n for p in snapshot["projects"] for n in p["nodes"] if n["id"] == "t_a00001")
        assert json.loads(node["fs_scope"]) == ["src"]
        assert json.loads(node["forbidden_commands"]) == ["git push"]
        assert json.loads(node["subscriptions"])[0]["type"] == "test.failed"
    finally:
        s.close()
