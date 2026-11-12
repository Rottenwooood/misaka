"""A candidate is a skill proposal that nothing loads until a person approves it.

Approval moves the directory into the shared skill root, which is the same shape the loader
already reads (a directory holding SKILL.md). A rejected candidate is deleted.
"""
from __future__ import annotations

from pathlib import Path

from misaka.config import home
from misaka.utils import atomic


def _candidates() -> Path:
    path = home.path("skill_candidates")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _frontmatter(name: str, description: str, status: str, version: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description!r}\n"
        f"version: {version}\n"
        f"status: {status}\n"
        "---\n\n"
    )


class SkillCandidate:
    def propose(self, name: str, body: str, *, description: str = "", version: str = "0.1.0") -> dict:
        if not name.strip() or "/" in name or "\\" in name:
            raise ValueError("A skill name is one path component")
        if not body.strip():
            raise ValueError("A skill needs a body")
        target = _candidates() / name
        target.mkdir(parents=True, exist_ok=True)
        atomic.write_text(target / "SKILL.md", _frontmatter(
            name, description or name, "proposed", version) + body.strip() + "\n")
        return {"name": name, "path": str(target), "status": "proposed"}

    def list(self) -> list[dict]:
        found = []
        for directory in sorted(_candidates().iterdir()):
            if not directory.is_dir():
                continue
            text = (directory / "SKILL.md").read_text(encoding="utf-8-sig") if (
                directory / "SKILL.md").is_file() else ""
            found.append({"name": directory.name,
                          "description": _field(text, "description"),
                          "status": _field(text, "status")})
        return found

    def approve(self, name: str) -> dict:
        source = _candidates() / name
        if not (source / "SKILL.md").is_file():
            raise ValueError(f"Unknown skill candidate: {name}")
        target = home.path("shared_skills") / name
        if target.exists():
            raise ValueError(f"A skill named {name} is already published")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        atomic.write_text(target / "SKILL.md", (target / "SKILL.md").read_text(
            encoding="utf-8-sig").replace("status: proposed", "status: published"))
        return {"name": name, "path": str(target), "status": "published"}

    def reject(self, name: str) -> dict:
        source = _candidates() / name
        if not source.is_dir():
            raise ValueError(f"Unknown skill candidate: {name}")
        import shutil
        shutil.rmtree(source)
        return {"name": name, "status": "rejected"}


def _field(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""
