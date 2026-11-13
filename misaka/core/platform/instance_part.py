"""Bind native session replacements and current-state handoff to an instance."""
import json
import logging

from misaka.core.platform import instances

logger = logging.getLogger(__name__)


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
        item = instances.get(self.spec.instance_id)
        config = item["config"]
        available = {t.name for t in self.session.getAllTools()}
        instances.touch(self.spec.instance_id, force=True)
        if "tools" in config:
            if missing := set(config["tools"]) - available:
                raise ValueError(f"Unknown configured tools: {sorted(missing)}")
            self.session.setActiveToolsByName(config["tools"])
        else:
            defaults = list(instances.default_tools(item["role"]) or ())
            if missing := set(defaults) - available:
                if self.spec.kind in ("foreground", "dm"):
                    raise ValueError(
                        f"Default tools are not registered: {sorted(missing)}"
                    )
                defaults = [name for name in defaults if name in available]
            if defaults:
                self.session.setActiveToolsByName(defaults)
        self._sync_centre(item)

    async def tool_call(self, _event, _ctx=None):
        """A tool boundary is the instance's most recent sign of life."""
        instances.touch(self.spec.instance_id)

    def _sync_centre(self, item):
        """Declare this instance and its own subscriptions; a centre outage is not fatal."""
        from misaka.core.messaging import config, registry
        from misaka.core.messaging.sync import SubscriptionSync

        client = config.center()
        url = config.inbox_url()
        if client is None or url is None:
            return
        try:
            registry.ensure_endpoint(client, item["id"], url)
            specs = [entry for entry in json.loads(item["subscriptions"] or "[]")
                     if entry.get("enabled", True)]
            if specs:
                SubscriptionSync(client, item["id"]).apply(item["id"], specs, prefix="instance")
        except Exception:
            logger.warning("Could not declare instance %s at the centre", item["id"], exc_info=True)

def part(spec):
    return InstancePart(spec) if spec.instance_id else None
