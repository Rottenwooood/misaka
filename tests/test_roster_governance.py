"""Instance session state, subscriptions, stop and bulk deletion."""
import asyncio
import json
from contextlib import closing

from tests.work_fixture import BODY, Scenario

CARD = "t_a00001"


def _payload(result):
    text = result["content"][0]["text"]
    return json.loads(text.split(">>>\n", 1)[1].split("\n<<<END-", 1)[0])


def _seed(s):
    s.apply([
        {"op": "template.create", "id": "lo", "role": "last-order", "config": {}},
        {"op": "instance.create", "id": "lo", "template_id": "lo", "template_revision": 1},
        {"op": "template.create", "id": "sister", "role": "sister", "config": {}},
        {"op": "instance.create", "id": "worker", "template_id": "sister", "template_revision": 1},
        {"op": "request.create", "id": "req", "title": "R", "owner_id": "lo",
         "workspace": str(s.project)},
    ])


def test_instance_status_distinguishes_live_idle_and_none():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            with tasks.write_txn(con):
                con.execute("UPDATE instances SET session_file=? WHERE id='worker'",
                            (str(s.project / "session.json"),))
            status = {row["id"]: row for row in work.instance_status(con)}
            assert status["worker"]["session"] == "idle"
            assert status["lo"]["session"] == "none"
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            with tasks.write_txn(con):
                con.execute("UPDATE tasks SET status='running' WHERE id=?", (CARD,))
            assert work.instance_status(con, "worker")[0]["session"] == "live"
            assert work.instance_status(con, "worker")[0]["running_card"] == CARD
    finally:
        s.close()


def test_instance_subscriptions_are_added_listed_paused_and_removed():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            added = work.instance_subscriptions(
                con, "worker", "add", [{"source": "tests", "type": "failed"}])
            assert added["subscriptions"] == [
                {"source": "tests", "type": "failed", "enabled": True}]
            same = work.instance_subscriptions(
                con, "worker", "add", [{"source": "tests", "type": "failed"}])
            assert same["subscriptions"] == added["subscriptions"]
            paused = work.instance_subscriptions(
                con, "worker", "set_enabled",
                [{"source": "tests", "type": "failed", "enabled": False}])
            assert paused["subscriptions"][0]["enabled"] is False
            missing = work.instance_subscriptions(
                con, "worker", "set_enabled",
                [{"source": "other", "type": "x", "enabled": True}])
            assert missing["failed"] == [
                {"source": "other", "type": "x", "reason": "not_found"}]
            removed = work.instance_subscriptions(
                con, "worker", "remove", [{"source": "tests", "type": "failed"}])
            assert removed["subscriptions"] == []
    finally:
        s.close()


def test_bulk_delete_retires_a_used_instance_and_refuses_a_pinned_template():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            result = work.bulk_delete(con, "instance", ["worker"])
            assert result["retired"] == ["worker"] and result["failed"] == []
            assert con.execute("SELECT retired FROM instances WHERE id='worker'").fetchone()[0] == 1
            templates = work.bulk_delete(con, "template", ["sister"])
            assert templates["failed"] == [{"id": "sister", "reason": "in_use"}]
            s.apply([{"op": "template.create", "id": "spare", "role": "sister", "config": {}}])
            removed = work.bulk_delete(con, "template", ["spare"])
            assert removed["deleted"] == ["spare"]
    finally:
        s.close()


def test_the_roster_tool_returns_session_state_and_manages_subscriptions():
    from misaka.core.network.wiring import coordination
    from misaka.core.platform import tasks

    s = Scenario()
    tools = {}

    class Harness:
        def registerTool(self, definition):
            tools[definition.name] = definition

    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            with tasks.write_txn(con):
                con.execute("UPDATE instances SET session_file=? WHERE id='worker'",
                            (str(s.project / "session.json"),))
            coordination.install(Harness(), object(), lambda: con, "lo")
            shown = _payload(asyncio.run(tools["misaka_roster"].execute(
                "call", {"command": {"action": "show", "kind": "instance", "id": "worker"}},
                None, None, None)))
            assert shown["instances"][0]["session"] == "idle"
            assert "pid" not in shown["instances"][0]
            assert shown["instances"][0]["running_card"] is None
            assert shown["instances"][0]["subscriptions"] == []
            added = _payload(asyncio.run(tools["misaka_roster"].execute(
                "call", {"command": {"action": "subscriptions", "id": "worker",
                                     "op": "add",
                                     "items": [{"source": "tests", "type": "failed"}]}},
                None, None, None)))
            assert added["subscriptions"][0]["source"] == "tests"
            assert added["synced"] is False
    finally:
        s.close()


def test_the_roster_tool_hibernates_and_kills_a_running_card():
    from misaka.core.network.wiring import coordination
    from misaka.core.platform import tasks, work

    s = Scenario()
    tools = {}

    class Harness:
        def registerTool(self, definition):
            tools[definition.name] = definition

    try:
        _seed(s)
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": CARD, "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            with tasks.write_txn(con):
                con.execute("UPDATE tasks SET status='running' WHERE id=?", (CARD,))
            coordination.install(Harness(), object(), lambda: con, "lo")
            slept = _payload(asyncio.run(tools["misaka_roster"].execute(
                "call", {"command": {"action": "stop_instance", "id": "worker",
                                     "mode": "hibernate"}},
                None, None, None)))
            assert slept["session"] in {"idle", "none"}
            assert tasks.get(con, CARD)["status"] == "ready"
            with tasks.write_txn(con):
                con.execute("UPDATE tasks SET status='running' WHERE id=?", (CARD,))
            killed = _payload(asyncio.run(tools["misaka_roster"].execute(
                "call", {"command": {"action": "stop_instance", "id": "worker",
                                     "mode": "kill"}},
                None, None, None)))
            assert killed["session"] == "none"
            assert tasks.get(con, CARD)["status"] == "blocked"
            assert tasks.get(con, CARD)["block_reason"] == "instance_killed"
    finally:
        s.close()


def test_subscription_changes_reach_the_centre(monkeypatch):
    from misaka.core.messaging import config as center_config
    from misaka.core.messaging import registry
    from misaka.core.messaging.sync import SubscriptionSync
    from misaka.core.network.wiring import coordination
    from misaka.core.platform import tasks

    s = Scenario()
    tools = {}
    calls = []

    class Harness:
        def registerTool(self, definition):
            tools[definition.name] = definition

    def apply(self, owner, specs, *, prefix="card"):
        calls.append(("apply", owner, [item["source"] for item in specs]))
        return []

    def clear(self, names):
        calls.append(("clear", list(names)))
        return []

    try:
        _seed(s)
        monkeypatch.setattr(center_config, "center", lambda: object())
        monkeypatch.setattr(center_config, "inbox_url", lambda: "http://centre.invalid")
        monkeypatch.setattr(registry, "ensure_endpoint", lambda *args: None)
        monkeypatch.setattr(SubscriptionSync, "apply", apply)
        monkeypatch.setattr(SubscriptionSync, "clear", clear)
        with closing(tasks.connect(s.database)) as con:
            coordination.install(Harness(), object(), lambda: con, "lo")
            asyncio.run(tools["misaka_roster"].execute(
                "call", {"command": {"action": "subscriptions", "id": "worker",
                                     "op": "add",
                                     "items": [{"source": "tests", "type": "failed"}]}},
                None, None, None))
            asyncio.run(tools["misaka_roster"].execute(
                "call", {"command": {"action": "subscriptions", "id": "worker",
                                     "op": "remove",
                                     "items": [{"source": "tests", "type": "failed"}]}},
                None, None, None))
        assert ("apply", "worker", ["tests"]) in calls
        assert ("clear", ["instance:worker:0"]) in calls
    finally:
        s.close()


def test_roster_config_is_opt_in():
    from misaka.core.network.wiring.coordination import _roster_view
    from misaka.core.platform import tasks

    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "sister", "role": "sister",
             "config": {"system": "BASE"}},
            {"op": "instance.create", "id": "worker", "template_id": "sister",
             "template_revision": 1},
        ])
        with closing(tasks.connect(s.database)) as con:
            assert "config" not in _roster_view(con, "template")["templates"][0]
            assert _roster_view(con, "template", detail=True)["templates"][0]["config"]["system"] == "BASE"
            instance = _roster_view(con, "instance", "worker", detail=True)["instances"][0]
            assert instance["config"]["system"] == "BASE"
    finally:
        s.close()
