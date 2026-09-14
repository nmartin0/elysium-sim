"""
rest.py  (a JSON API on localhost, the shape small-business SaaS has)

THE FOURTH WAY A SMALL BUSINESS'S DATA IS REACHED, after a database on
a port, a database in a file, and a folder of CSV. Square, Stripe,
Shopify, QuickBooks Online, ServiceTitan, Jobber: none of them hand
anyone a database connection. They expose a paginated JSON API behind
a bearer token, and that is the only way in. Foundry connects to these
through its REST API source type.

WHAT MAKES THIS AUTHENTIC IS NOT "IT RETURNS JSON". It is the four
things a consumer of such an API has to survive, none of which any
fixture reproduces:

  - PAGINATION. A collection does not arrive in one response. It
    arrives in pages with an opaque cursor, and a consumer that reads
    the first page and stops silently loses everything after it. That
    is the single most common integration bug against APIs of this
    shape, and it is silent -- the data looks fine, there is just less
    of it.
  - AUTHENTICATION. A bearer token, and a 401 without it. A consumer
    that forgets the header gets a well-formed JSON error rather than
    a connection failure, which is a different thing to handle.
  - RATE LIMITING. 429 with a Retry-After header. Real APIs do this
    and a consumer that ignores it makes the problem worse.
  - TIMESTAMPS AS STRINGS. ISO 8601 with an offset, not epoch
    integers, because that is what these APIs emit -- and parsing them
    is a real source of off-by-one-day errors.

THE CURSOR IS OPAQUE ON PURPOSE. It encodes an offset, but it is
base64 and a consumer has no business decoding it -- that is exactly
how real cursors behave, and a consumer that cracks one open and does
arithmetic on it is writing a bug this silo should not make easy.

NOT A FRAMEWORK. http.server from the standard library, in a thread.
The simulator has two runtime dependencies and this is not worth a
third: the surface is four verbs on a handful of collections, and a
web framework would bring routing, validation and middleware that
nothing here needs.
"""

import base64
import binascii
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from simulator.silo import ConnectionDescriptor, Silo, SiloError

#: Square's default and a common one across these APIs. Small enough
#: that any realistic collection spans several pages, which is the
#: point: a consumer that ignores the cursor must visibly lose data.
DEFAULT_PAGE_SIZE = 100

#: The prefix these APIs conventionally version under.
API_PREFIX = "/v1"

#: Seconds a 429 asks the caller to wait. Real values vary; what
#: matters is that the header is there to be honoured or ignored.
RETRY_AFTER_SECONDS = 2

_SHUTDOWN_TIMEOUT = 10


def _encode_cursor(offset: int) -> str:
    """An opaque cursor. Base64 so it is obviously not for reading."""
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode()


def _decode_cursor(cursor: str) -> int:
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
        prefix, _, value = decoded.partition(":")
        if prefix != "offset":
            raise ValueError(decoded)
        return int(value)
    except (ValueError, UnicodeDecodeError, binascii.Error) as error:
        raise ValueError(f"malformed cursor: {cursor!r}") from error


class _Handler(BaseHTTPRequestHandler):
    """Routing for one silo. Held on the server, not on the class."""

    #: Set by the silo when the server is created.
    silo: "RestSilo"

    def log_message(self, format: str, *args: object) -> None:
        """Silence. http.server logs every request to stderr by
        default, which would bury a test run's real output."""

    def do_GET(self) -> None:  # noqa: N802 -- http.server's naming
        silo = self.server.silo  # type: ignore[attr-defined]

        if not silo._rate_limit_allows():
            self._respond(429, {"errors": [{"code": "RATE_LIMITED",
                                            "detail": "Too many requests"}]},
                          extra_headers={"Retry-After": str(RETRY_AFTER_SECONDS)})
            return

        if not self._authorised(silo.token):
            # A well-formed JSON error, not a connection failure --
            # which is a different thing for a consumer to handle, and
            # the thing that really happens.
            self._respond(401, {"errors": [{"code": "UNAUTHORIZED",
                                            "detail": "Missing or invalid bearer token"}]})
            return

        path, _, query = self.path.partition("?")
        if not path.startswith(f"{API_PREFIX}/"):
            self._respond(404, {"errors": [{"code": "NOT_FOUND", "detail": path}]})
            return

        collection = path[len(API_PREFIX) + 1:].strip("/")
        if collection not in silo.collections:
            self._respond(404, {"errors": [{"code": "NOT_FOUND",
                                            "detail": f"No such collection: {collection}"}]})
            return

        parameters = dict(
            pair.split("=", 1) for pair in query.split("&") if "=" in pair
        )
        try:
            offset = _decode_cursor(parameters["cursor"]) if "cursor" in parameters else 0
        except ValueError as error:
            self._respond(400, {"errors": [{"code": "INVALID_CURSOR", "detail": str(error)}]})
            return

        records = silo.collections[collection]
        page = records[offset: offset + silo.page_size]
        next_offset = offset + len(page)
        body: dict[str, object] = {"data": page}
        if next_offset < len(records):
            body["cursor"] = _encode_cursor(next_offset)
        self._respond(200, body)

    def _authorised(self, token: str | None) -> bool:
        if token is None:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {token}"

    def _respond(self, status: int, body: dict, *, extra_headers: dict | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)


class RestSilo(Silo):
    """One silo exposed as a paginated JSON API on localhost."""

    kind: ClassVar[str] = "rest"
    requires_port: ClassVar[bool] = True

    def __init__(self, name: str, data_dir: Path, port: int, *,
                 token: str | None = "sim-token", page_size: int = DEFAULT_PAGE_SIZE,
                 rate_limit: int | None = None) -> None:
        super().__init__(name, data_dir)
        self.port = port
        #: None disables authentication entirely. Some small-business
        #: APIs really are open on a private network, and a pack should
        #: be able to say so rather than have a token forced on it.
        self.token = token
        self.page_size = page_size
        #: Requests allowed before 429s begin. None means no limit,
        #: which is the sane default -- a limiter on by default would
        #: make every consumer's first run fail for a reason that is
        #: not about the data.
        self.rate_limit = rate_limit
        self.collections: dict[str, list[dict]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._request_count = 0
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- lifecycle ---------------------------------------------------

    def create(self) -> None:
        """Nothing on disk. The collections live in memory.

        Not a stub: this silo's state is whatever the simulation has
        published into it, and persisting that to disk would be a
        second copy to keep in step with no reader for it.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        if self._server is not None:
            return
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        except OSError as error:
            raise SiloError(f"{self.name}: cannot listen on port {self.port}: {error}") from error
        # Loopback only, by the bind address above. A simulated
        # business answering on a LAN interface would be a genuinely
        # bad thing to leave running.
        server.silo = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True,
                                        name=f"rest-silo-{self.name}")
        self._thread.start()

    def stop(self) -> None:
        """Stop serving. Quiet if it never was.

        Promises the outcome rather than the mechanism, the same as
        every other silo: a server already torn down by terminate()
        must not turn a teardown into an error.
        """
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is None:
            return
        try:
            server.shutdown()
        except OSError:
            # terminate() closed the socket underneath it. The outcome
            # -- not serving -- is already achieved.
            pass
        server.server_close()
        if thread is not None:
            thread.join(timeout=_SHUTDOWN_TIMEOUT)

    def is_reachable(self) -> bool:
        """Whether the API answers at all.

        Any HTTP status counts, including 401 and 429. Those mean the
        service is up and is refusing this particular request, which is
        a different condition from the service being down -- and
        conflating them is how a health check reports an outage during
        a rate-limit window.
        """
        try:
            urllib.request.urlopen(f"{self.base_url}{API_PREFIX}/", timeout=2)
            return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            return False

    def connection(self) -> ConnectionDescriptor:
        details: dict[str, object] = {"base_url": self.base_url, "format": "json"}
        if self.token is not None:
            details["auth"] = "bearer"
            details["token"] = self.token
        return ConnectionDescriptor(kind=self.kind, details=details)

    def terminate(self) -> None:
        """Drop the listening socket without a graceful shutdown.

        The API equivalent of a process being killed: connections stop
        being accepted immediately, mid-flight requests are cut, and a
        consumer sees a connection error rather than a clean response.
        """
        server = self._server
        if server is None:
            return
        try:
            server.socket.close()
        except OSError:
            pass
        self._server, self._thread = None, None

    # -- publishing --------------------------------------------------

    def publish(self, collection: str, records: Sequence[dict]) -> None:
        """Replace a collection's contents."""
        with self._lock:
            self.collections[collection] = [dict(record) for record in records]

    def append(self, collection: str, record: dict) -> None:
        """Add one record, creating the collection if needed."""
        with self._lock:
            self.collections.setdefault(collection, []).append(dict(record))

    # -- rate limiting -----------------------------------------------

    def _rate_limit_allows(self) -> bool:
        if self.rate_limit is None:
            return True
        with self._lock:
            self._request_count += 1
            return self._request_count <= self.rate_limit

    def reset_rate_limit(self) -> None:
        """Begin a fresh window.

        Real APIs reset on a rolling clock. This is deliberately manual
        so a pack or a scenario decides when the window turns, rather
        than a consumer's behaviour depending on how long a test took.
        """
        with self._lock:
            self._request_count = 0


# =============================================================================
# AI-ONLY NOTES -- not user-facing. Context for a future AI session (or me,
# later) that lacks this conversation's history. Update this section
# whenever something genuinely open, deferred, or rejected comes up here.
# =============================================================================
#
# RESOLVED (kept for history): cursors are base64 rather than a plain integer
# offset, and are decoded only here. Real cursors are opaque, and a consumer
# that cracks one open and does arithmetic on it is writing a bug -- one this
# silo should not make easy by handing out a readable number.
#
# RESOLVED: rate limiting is OFF by default and its window resets manually. A
# limiter on by default would make a consumer's first run fail for a reason
# that has nothing to do with the data, and a wall-clock window would make
# behaviour depend on how long a test took to run.
#
# RESOLVED: is_reachable() counts 401 and 429 as reachable. Those mean the
# service is up and refusing this request, which is a different condition from
# the service being down; conflating them is how a health check reports an
# outage during a rate-limit window.
#
# RESOLVED: ConnectionDescriptor carries a token for this kind, which
# contradicts an earlier claim in simulator/silo.py that no silo has
# credentials. That claim was true when every silo trusted loopback and is not
# now -- the docstring there has been corrected rather than left to rot.
#
# DEFERRED (known, intentional, not yet built): GET only. These APIs also
# accept writes, and a consumer with writeback enabled would POST. Adding it
# means deciding what a write does to a simulation that owns the data, which is
# a real design question and not a handler method.
#
# DEFERRED: no webhooks. Square, Stripe and QuickBooks Online all push events
# as well as serving polls, and a consumer built on webhooks behaves very
# differently from one that polls. Worth simulating; needs somewhere to push
# TO, which is a consumer-side fact the simulator does not have.
#
# DEFERRED: collections live in memory, so a restart empties them. Fine while a
# run is one process; the moment a world is resumed across processes this needs
# the same treatment the other silos get from their own storage.
