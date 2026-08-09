"""End-to-end coverage for deploy/install.sh probe_health.

probe_health decides whether the port really holds our agent, and each of its
four verdicts is a PR #1 regression that was verified by hand and nothing else:

- healthy vs unhealthy: `curl -f` conflated the health check's own 503 with a
  refused connection (2f29104), so a slow first poll looked dead.
- foreign: dropping -f to tell those apart then accepted any 200, so a different
  HTTP service squatting on the port passed for burnrate (305ce32). The body is
  now checked for the "poller_healthy" field only our endpoint emits.
- down: a truly closed port must read as down, not as one of the above.

install.sh grew a `main` guard so `source`-ing it defines probe_health/url_for
without performing an install; the tests drive the *real* function by sourcing
it and pointing PROBE_URL at a local http.server that returns a chosen response.
Nothing here touches launchd.
"""

import http.server
import pathlib
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager

SCRIPT = pathlib.Path(__file__).parent.parent / "deploy" / "install.sh"

# The exact status/body shapes GET /api/healthz returns (src/burnrate/app.py):
# 200 + poller_healthy true when healthy, 503 + poller_healthy false otherwise.
HEALTHY_BODY = b'{"ok": true, "poller_healthy": true}'
UNHEALTHY_BODY = b'{"ok": true, "poller_healthy": false}'
# A different service on the port: a 200 whose body lacks our field.
FOREIGN_BODY = b"<html><body>nginx welcome</body></html>"


@contextmanager
def _health_server(status: int, body: bytes) -> Iterator[str]:
    """Serve `status`/`body` on an ephemeral loopback port; yield its base URL.

    Every path is answered identically -- probe_health only ever hits
    /api/healthz -- so the handler ignores the request line.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:  # silence the test log
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _closed_port_url() -> str:
    """A loopback URL whose port has nothing listening (bind, read, release)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def _classify(probe_url: str) -> str:
    """Source install.sh and run the real probe_health against `probe_url`.

    The guard makes sourcing a no-op install, so this reaches the function
    without launchctl/plutil/config ever running. probe_health prints its
    verdict with no trailing newline, so stdout is the bare classification.
    """
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; PROBE_URL="$2"; probe_health', "bash", str(SCRIPT), probe_url],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_200_with_our_field_is_healthy():
    with _health_server(200, HEALTHY_BODY) as url:
        assert _classify(url) == "healthy"


def test_a_503_with_our_field_is_unhealthy():
    """The regression 2f29104 fixed: 503 is the health check reporting itself,
    not a dead port. It must be distinguishable from `down`, which is why the
    body still carries poller_healthy on a 503."""
    with _health_server(503, UNHEALTHY_BODY) as url:
        assert _classify(url) == "unhealthy"


def test_a_200_without_our_field_is_foreign():
    """The regression 305ce32 fixed: a foreign HTTP service answering 200 on the
    port must not pass for burnrate. The missing poller_healthy field is what
    gives it away, before the status is ever consulted."""
    with _health_server(200, FOREIGN_BODY) as url:
        assert _classify(url) == "foreign"


def test_our_field_with_an_unexpected_status_is_foreign():
    """Body says burnrate but the status is neither 200 nor 503: the status
    case's default arm still calls it foreign rather than trusting the body
    alone."""
    with _health_server(404, HEALTHY_BODY) as url:
        assert _classify(url) == "foreign"


def test_a_closed_port_is_down():
    """Nothing listening: curl fails and the verdict is `down`, kept separate
    from unhealthy so the installer's diagnostics point the right way."""
    assert _classify(_closed_port_url()) == "down"
