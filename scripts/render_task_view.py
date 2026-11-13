#!/usr/bin/env python3
"""Render the real task-view cells to a PNG, for visual review without a terminal.

Example:
  scripts/render_task_view.py --home ~/.local/share/misaka-personal \
      --project ~/p-test --request perm-test --cols 140 --rows 44 \
      --out /tmp/task-view.png
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLORS = {
    "normal": (205, 214, 244),
    "dim": (127, 132, 156),
    "accent": (203, 166, 247),
    "active": (166, 227, 161),
    "wait": (249, 226, 175),
    "good": (166, 227, 161),
    "bad": (243, 139, 168),
}
BG = (30, 30, 46)
CELL_W, CELL_H = 12, 26
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--request")
    parser.add_argument("--select")
    parser.add_argument("--out", required=True)
    parser.add_argument("--cols", type=int, default=140)
    parser.add_argument("--rows", type=int, default=44)
    args = parser.parse_args()

    os.environ["MISAKA_HOME"] = str(Path(args.home).expanduser().resolve())
    from misaka.core.platform.task_snapshot import SnapshotReader
    from misaka.ui.task_view.canvas import Canvas

    reader = SnapshotReader(
        [args.project], os.path.join(os.environ["MISAKA_HOME"], "state", "board.db"),
        request_id=args.request,
    )
    screen = Canvas(reader.read(), select_card=args.select).render(args.cols, args.rows)
    font = ImageFont.truetype(FONT, 18, index=7)
    image = Image.new("RGB", (args.cols * CELL_W, args.rows * CELL_H), BG)
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(screen.cells):
        for x, (char, color) in enumerate(row):
            if char:
                draw.text((x * CELL_W, y * CELL_H), char, font=font,
                          fill=COLORS.get(color, COLORS["normal"]))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    image.save(args.out)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
