"""Task-view process identity and the single launch contract used by CLI and Panel."""

import os
import sys
from pathlib import Path

from misaka.config import home


def is_task_view(argv):
    words = list(argv or ())
    return any(
        words[i : i + 4] == ["-m", "misaka", "view", "tasks"]
        for i in range(len(words) - 3)
    )


def command(projects):
    argv = [sys.executable, "-m", "misaka", "view", "tasks"]
    for project in projects:
        argv.extend(["--project", project])
    # A source checkout and its child panes must use the same code.
    source = str(Path(__file__).resolve().parents[3])
    env = {
        "MISAKA_HOME": str(home.home()),
        "PYTHONPATH": os.pathsep.join(
            filter(None, [source, os.environ.get("PYTHONPATH")])
        ),
    }
    return argv, env
