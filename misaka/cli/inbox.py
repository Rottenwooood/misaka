"""Run the local callback the centre delivers to."""
from __future__ import annotations

import time

from misaka.core.messaging import delivery
from misaka.core.messaging.inbox import CallbackServer, Inbox


def serve(args) -> int:
    server = CallbackServer(Inbox(), delivery.deliver, host=args.host, port=args.port)
    url = server.start()
    print(f"inbox listening on {url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        server.stop()
