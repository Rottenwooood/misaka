"""Read-only views bypass agent bootstrap, credential checks and project initialization."""

import json
import os
import sys
from pathlib import Path

from misaka.config import home
from misaka.core.platform.task_snapshot import SnapshotReader


def run(args):
    if args.snapshot and any(size < 1 for size in args.snapshot):
        print("--snapshot 的列数和行数必须为正整数", file=sys.stderr)
        return 2
    projects = [
        str(Path(p).expanduser().resolve()) for p in (args.project or [os.getcwd()])
    ]
    if args.pane:
        from misaka.ui.panel import client

        argv = [sys.executable, "-m", "misaka", "view", "tasks"]
        for project in projects:
            argv.extend(["--project", project])
        # Use the caller's code, even when a pre-existing daemon came from another install.
        source = str(Path(__file__).resolve().parents[2])
        env = {
            "MISAKA_HOME": str(home.home()),
            "PYTHONPATH": os.pathsep.join(
                filter(None, [source, os.environ.get("PYTHONPATH")])
            ),
        }
        try:
            result = client.request(
                "pane.create",
                {"argv": argv, "cwd": projects[0], "title": "Tasks · 只读", "env": env},
            )
        except (OSError, RuntimeError, ConnectionError) as error:
            print(f"无法打开画布面板，请先运行 misaka panel：{error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    reader = SnapshotReader(projects, home.path("db"))
    if args.json:
        snapshot = reader.read()
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return int(
            snapshot["runtime_state"] == "error"
            or any(
                p["errors"]
                or any(n["errors"] or n["missing_needs"] for n in p["nodes"])
                or any(g["cycle_or_downstream"] for g in p["groups"])
                for p in snapshot["projects"]
            )
        )
    from misaka.ui.task_view.canvas import Canvas
    from misaka.ui.task_view.canvas import run as run_canvas

    if args.snapshot:
        print(Canvas(reader.read()).render(*args.snapshot).plain())
        return 0
    return run_canvas(reader)
