"""Set a stretch of an instance's history aside, without deleting it.

Isolation is a new session: the engine already keeps a fresh window free of the old turns,
and the old transcript stays on disk. The record names what was set aside and why, so a
person can find it again.
"""
from __future__ import annotations

import time

from misaka.config import home
from misaka.core.platform import instances, work
from misaka.core.platform import tasks as board


class HistoryIsolation:
    def __init__(self, connect=None):
        self._connect = connect or (lambda: board.connect(str(home.path("db"))))

    def isolate(self, instance_id: str, label: str | None = None) -> dict:
        con = self._connect()
        try:
            item = work.instance(con, instance_id)
            previous = item["session_file"]
            name = (label or f"session {item['session_epoch']}").strip()
            after = instances.new_session(con, instance_id, item["session_epoch"])
            with board.write_txn(con):
                con.execute(
                    "INSERT INTO history_isolations(instance_id,epoch,label,session_file,created_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(instance_id,epoch) DO UPDATE SET label=excluded.label",
                    (instance_id, after["session_epoch"] - 1, name, previous, int(time.time())),
                )
            return {"instance_id": instance_id, "label": name, "session_epoch": after["session_epoch"]}
        finally:
            con.close()

    def list(self, instance_id: str) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT instance_id,epoch,label,session_file,created_at FROM history_isolations "
                "WHERE instance_id=? ORDER BY epoch", (instance_id,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            con.close()
