"""Explicit formal entities for focused execution/research unit tests.

These builders prepare valid state before calling product APIs. They are not
installed by conftest and do not change or monkeypatch application behavior.
"""

import hashlib
import os
import secrets

from misaka.core.platform import cards, tasks, work


def owner(con):
    if con.execute("SELECT 1 FROM instances WHERE id='test-lo'").fetchone() is None:
        work.apply(
            con,
            [
                {
                    "op": "template.create",
                    "id": "test-lo-template",
                    "role": "last-order",
                    "config": {},
                },
                {
                    "op": "instance.create",
                    "id": "test-lo",
                    "template_id": "test-lo-template",
                    "template_revision": 1,
                },
            ],
        )
    return "test-lo"


def identity(con, profile, workspace):
    profile = profile or "10032"
    if con.execute("SELECT 1 FROM templates WHERE id=?", (profile,)).fetchone() is None:
        work.apply(
            con,
            [{"op": "template.create", "id": profile, "role": "sister", "config": {}}],
        )
    iid = "i_" + secrets.token_hex(6)
    work.apply(
        con,
        [
            {
                "op": "instance.create",
                "id": iid,
                "template_id": profile,
                "template_revision": 1,
            }
        ],
    )
    rid = (
        "test_"
        + hashlib.sha256(os.fsencode(tasks.canonical_workspace(workspace))).hexdigest()[
            :12
        ]
    )
    if con.execute("SELECT 1 FROM work_requests WHERE id=?", (rid,)).fetchone() is None:
        work.apply(
            con,
            [
                {
                    "op": "request.create",
                    "id": rid,
                    "title": "Test request",
                    "owner_id": owner(con),
                    "workspace": str(workspace),
                }
            ],
        )
    return rid, iid


def execution_card(con, title, body="", assignee="", **kwargs):
    workspace = kwargs.get("workspace") or os.getcwd()
    rid, iid = identity(con, assignee, workspace)
    return tasks.create_task(
        con, title, body=body, request_id=rid, instance_id=iid, **kwargs
    )


def contract_card(con, workspace, title, body, assignee, **kwargs):
    rid, iid = identity(con, assignee, workspace)
    if "## acceptance criteria" not in body.lower():
        body += "\n## acceptance criteria\n- Fixture output exists.\n"
    return cards.create(con, workspace, title, body, iid, request_id=rid, **kwargs)


def research_run(con, **kwargs):
    from misaka.core.research import runs

    kwargs.setdefault("owner_id", owner(con))
    # Research tests use these ordinary profiles as their planner's catalog.
    for profile in ("10032", "10033", "10077"):
        if (
            con.execute("SELECT 1 FROM templates WHERE id=?", (profile,)).fetchone()
            is None
        ):
            work.apply(
                con,
                [
                    {
                        "op": "template.create",
                        "id": profile,
                        "role": "sister",
                        "config": {},
                    }
                ],
            )
    return runs.create(con, **kwargs)
