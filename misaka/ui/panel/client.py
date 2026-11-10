"""Thin client: connect to the daemon, starting it if it is not running (herdr-style auto-detect)."""
import json
import os
import subprocess
import sys
import threading
import time

from misaka.config import CFG, home
from misaka.utils import local_socket


def _sock_path():
    return os.path.expanduser(CFG["net_sock"])


def check_sock_path():
    """The socket's directory must be this user's alone before anything binds or connects."""
    home.private_dir(os.path.dirname(_sock_path()))


def request(method, params=None, *, timeout=10):
    """Send one request and return its result. Raises ConnectionError if the daemon is not running."""
    try:
        con = local_socket.connect(_sock_path(), timeout)
    except TimeoutError as error:
        raise TimeoutError(f"Panel request {method!r} timed out after {timeout}s; result unknown") from error
    try:
        con.sendall((json.dumps(
            {"id": "1", "method": method, "params": params or {}},
            ensure_ascii=False) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = con.recv(65536)
            if not chunk:
                # A daemon mid-shutdown accepts and then hangs up: that is "not
                # running", never a reply to parse.
                raise ConnectionError("the daemon closed the connection")
            buf += chunk
    except TimeoutError as error:
        # No blind retry: a mutating request may have executed before its reply was lost.
        raise TimeoutError(f"Panel request {method!r} timed out after {timeout}s; result unknown") from error
    finally:
        con.close()
    out = json.loads(buf)
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out["result"]


def _wait_for_dying_daemon(timeout):
    """A daemon that hung up mid-shutdown still LISTENS for a moment; spawning
    immediately makes the new daemon exit with "already running". Wait until its
    listener is gone -- refused / missing / timed out (herdr's stale classification);
    the file itself stays behind by design and the new daemon reclaims it."""
    deadline = time.monotonic() + timeout
    path = _sock_path()
    while time.monotonic() < deadline and os.path.exists(path):
        try:
            local_socket.connect(path, 0.5).close()
        except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
            return                       # herdr's stale classification: the old daemon is gone
        except OSError:
            pass
        time.sleep(0.05)


def _spawn_and_wait(timeout):
    log_path = _sock_path() + ".log"
    os.makedirs(os.path.dirname(log_path), mode=0o700, exist_ok=True)
    with os.fdopen(os.open(log_path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600), "a+b") as log:  # windows-footgun: ok - binary mode
        if hasattr(os, "fchmod"):
            os.fchmod(log.fileno(), 0o600)
        log_start = log.seek(0, os.SEEK_END)
        process = subprocess.Popen(
            # The daemon hosts PTYs, not model sessions. The CLI entry eagerly imports
            # every provider SDK before binding the socket; use its existing direct entry.
            [sys.executable, "-u", "-m", "misaka.ui.panel.daemon"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
            # Windows: not the panel's terminal. herdr detaches its one native binary
            # (detach_server_daemon_command); this interpreter starts through the venv's launcher,
            # and the python it spawns would get a console window of its own, left open on the
            # desktop. A console with no window (herdr's CREATE_NO_WINDOW for background commands)
            # is shared down that chain instead, and by everything the daemon runs.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Reap our child without tying its lifetime to this client or holding up a reply.
        threading.Thread(target=process.wait, daemon=True).start()
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                return request("ping", timeout=min(2, remaining))
            except (ConnectionError, FileNotFoundError, OSError):
                if process.poll() is not None:
                    break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        code = process.poll()
        state = f"exited with code {code}" if code is not None else f"is still running after {timeout:g}s"
        # Only this attempt's tail belongs in the error; keep the full log on disk.
        log.seek(max(log_start, log.seek(0, os.SEEK_END) - 8192))
        detail = log.read().decode("utf-8", errors="replace").strip()
    raise RuntimeError(
        f"Daemon process {process.pid} {state}, but its socket did not answer. "
        f"Log: {log_path}" + (f"\n{detail}" if detail else "")
    ) from None


def check_protocol(info):
    """Accept exactly this build. A mismatch never stops or replaces another daemon."""
    from misaka.ui.panel.daemon import PROTOCOL
    if info.get("proto") != PROTOCOL:
        raise RuntimeError(
            f"Daemon protocol mismatch: {info.get('proto')}, expected {PROTOCOL}. "
            "Use a separate MISAKA_HOME or explicitly stop that daemon before starting this build."
        )
    return info


def ensure(timeout=8.0):
    """Connect to a matching daemon, starting one only when none is listening."""
    check_sock_path()
    try:
        info = request("ping", timeout=2)
    except (ConnectionError, FileNotFoundError, OSError):
        _wait_for_dying_daemon(timeout=min(3.0, timeout))
        return check_protocol(_spawn_and_wait(timeout))
    return check_protocol(info)
