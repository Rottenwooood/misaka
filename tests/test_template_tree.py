"""Template versions are nodes in a tree; every instance pins one node."""
import json
import sqlite3
from contextlib import closing

from tests.work_fixture import Scenario


def rows(scenario, sql, args=()):
    with closing(sqlite3.connect(scenario.database)) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(sql, args)]


def nodes(scenario):
    found = {}
    for row in rows(scenario, "SELECT id,revision,parent_revision,config_json FROM templates"):
        found[(row["id"], row["revision"])] = {
            **row,
            "config": json.loads(row["config_json"]),
        }
    return found


def instances(scenario):
    return {row["id"]: row for row in rows(
        scenario, "SELECT id,template_id,template_revision FROM instances")}


def test_three_ways_to_create_an_instance():
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "role", "role": "sister",
             "config": {"system": "base"}},
            {"op": "instance.create", "id": "pinned", "template_id": "role",
             "template_revision": 1},
            {"op": "instance.create", "id": "patched", "template_id": "role",
             "template_revision": 1, "config": {"model": "x/y"}},
            {"op": "instance.create", "id": "fresh", "template_id": "fresh-role",
             "role": "sister", "config": {"system": "new"}},
        ])
        tree = nodes(s)
        assert set(tree) == {("role", 1), ("role", 2), ("fresh-role", 1)}
        assert tree[("role", 1)]["parent_revision"] is None
        assert tree[("role", 2)]["parent_revision"] == 1
        assert tree[("role", 2)]["config"] == {"system": "base", "model": "x/y"}
        assert tree[("role", 1)]["config"] == {"system": "base"}
        assert tree[("fresh-role", 1)]["parent_revision"] is None
        assert tree[("fresh-role", 1)]["config"] == {"system": "new"}

        pinned = instances(s)
        assert pinned["pinned"]["template_revision"] == 1
        assert pinned["patched"]["template_revision"] == 2
        assert pinned["fresh"]["template_id"] == "fresh-role"
        assert "overrides_json" not in [row["name"] for row in rows(
            s, "PRAGMA table_info(instances)")]
    finally:
        s.close()


def test_a_revision_may_branch_from_an_older_node():
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "r", "role": "sister",
             "config": {"system": "a"}},
            {"op": "template.revise", "id": "r", "expected_revision": 1,
             "config": {"system": "b"}},
            {"op": "template.revise", "id": "r", "expected_revision": 2,
             "config": {"system": "c"}, "parent_revision": 1},
        ])
        tree = nodes(s)
        assert tree[("r", 2)]["parent_revision"] == 1
        assert tree[("r", 3)]["parent_revision"] == 1
        assert tree[("r", 3)]["config"] == {"system": "c"}
    finally:
        s.close()


def test_a_new_template_needs_role_and_configuration():
    s = Scenario()
    try:
        s.apply([
            {"op": "instance.create", "id": "lo", "template_id": "new-role",
             "template_revision": 1},
        ], code=1)
    finally:
        s.close()


def test_patching_an_unknown_revision_is_refused():
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "r", "role": "sister",
             "config": {"system": "a"}},
            {"op": "instance.create", "id": "i", "template_id": "r",
             "template_revision": 9, "config": {"system": "b"}},
        ], code=1)
        assert nodes(s) == {}
        assert instances(s) == {}
    finally:
        s.close()
