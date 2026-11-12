"""The centre client speaks the HTTP surface and nothing more."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from misaka.core.messaging.client import CenterClient, CenterError
from misaka.core.messaging.sync import SubscriptionSync


class FakeCentre:
    def __init__(self):
        self.requests = []
        self.subscriptions = set()
        centre = self

        class Handler(BaseHTTPRequestHandler):
            def _read(self):
                length = int(self.headers.get("content-length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def _reply(self, status, body=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                if data:
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def do_POST(self):
                body = self._read()
                centre.requests.append((
                    "POST", self.path, body, self.headers.get("authorization"),
                    self.headers.get("x-notifs-to"), self.headers.get("idempotency-key")))
                if body.get("type") == "refused":
                    self._reply(422, {"error": "no subscription"})
                else:
                    self._reply(200, {"id": self.headers.get("idempotency-key"),
                                      "deduplicated": False, "recipients": []})

            def do_PUT(self):
                body = self._read()
                centre.requests.append(("PUT", self.path, body, self.headers.get("authorization")))
                if self.path == "/api/subscriptions":
                    centre.subscriptions.add(body["name"])
                self._reply(200, {})

            def do_DELETE(self):
                centre.requests.append(("DELETE", self.path, None, self.headers.get("authorization")))
                name = self.path.rsplit("/", 1)[-1]
                if name not in centre.subscriptions:
                    self._reply(404, {"error": "missing"})
                    return
                centre.subscriptions.discard(name)
                self._reply(204)

            def do_GET(self):
                centre.requests.append(("GET", self.path, None, self.headers.get("authorization")))
                self._reply(200, {"ok": True})

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def centre():
    fake = FakeCentre()
    try:
        yield fake
    finally:
        fake.stop()


def test_a_message_is_published_with_the_token(centre):
    client = CenterClient(centre.url, "secret")
    client.send("test.failed", {"card": "t_a00001"}, message_id="m-1", to=["worker"])
    method, path, body, auth, to, message_id = centre.requests[-1]
    assert (method, path) == ("POST", "/api/messages")
    assert body == {"type": "test.failed", "content": {"card": "t_a00001"}}
    assert (to, message_id) == ("worker", "m-1")
    assert auth == "Bearer secret"


def test_a_refusal_becomes_an_error_with_its_status(centre):
    client = CenterClient(centre.url, "secret")
    with pytest.raises(CenterError) as error:
        client.send("refused", "x")
    assert error.value.status == 422


def test_an_endpoint_is_declared_as_a_webhook(centre):
    CenterClient(centre.url, "secret").put_endpoint(
        "worker-1", "worker", "http://127.0.0.1:9000/messages",
        max_wait_ms=250, min_interval_ms=1500)
    method, path, body, _ = centre.requests[-1]
    assert (method, path) == ("PUT", "/api/endpoints")
    assert body["delivery"] == {"kind": "webhook",
                                "config": {"url": "http://127.0.0.1:9000/messages"}}
    assert (body["max_wait_ms"], body["min_interval_ms"]) == (250, 1500)


def test_subscriptions_follow_the_card(centre):
    sync = SubscriptionSync(CenterClient(centre.url, "secret"), "worker-1")
    names = sync.apply_card("t_a00001", [{"source": "tests", "type": "test.failed"},
                                         {"source": "ci", "type": "build.failed"}])
    assert names == ["card:t_a00001:0", "card:t_a00001:1"]
    assert centre.subscriptions == set(names)
    assert sync.clear(names) == []
    assert centre.subscriptions == set()


def test_dropping_a_missing_subscription_is_not_a_failure(centre):
    sync = SubscriptionSync(CenterClient(centre.url, "secret"), "worker-1")
    assert sync.clear(["card:t_a00001:9"]) == []
