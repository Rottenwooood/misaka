"""Bind native session replacements and current-state handoff to an instance."""
import json

from misaka.core.platform import instances
from misaka.utils.redact import redact


class InstancePart:
    def __init__(self, spec):
        self.spec = spec
        self.tools, self.commands = [], []
        self.session = None

    def attach(self, session):
        self.session = session

    async def session_start(self, _event, ctx):
        manager = ctx.sessionManager
        if not manager.isPersisted():
            raise ValueError("An instance needs a persisted native session")
        instances.bind_current(self.spec.instance_id, manager.getSessionFile())
        config = instances.get(self.spec.instance_id)["config"]
        if "tools" in config:
            available = {t.name for t in self.session.getAllTools()}
            if missing := set(config["tools"]) - available:
                raise ValueError(f"Unknown configured tools: {sorted(missing)}")
            self.session.setActiveToolsByName(config["tools"])

    async def before_agent_start(self, event, _ctx):
        if self.spec.role == "last_order":
            state = redact(json.dumps(instances.current_state(self.spec.instance_id), ensure_ascii=False))
            return {"systemPrompt": event.get("systemPrompt", "") + "\n\n# Current instance state\n" + state}


def part(spec):
    return InstancePart(spec) if spec.instance_id else None
