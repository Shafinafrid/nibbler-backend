"""
Import this FIRST, before anything from `app`, to cut a test off from the real
world: no `.env`, no inherited credentials, no outbound network.

Three separate leaks, each of which alone is enough to run a "test" against
production:

1. **`.env` is a RELATIVE path.** `app/config.py` declares `env_file = ".env"`,
   which pydantic resolves against the working directory, so a suite run from
   the repo root loads production AWS, Pinecone, Voyage and Firebase keys.
   Fixed by moving the process to an empty temp directory first.

2. **Exported environment variables outrank everything.** The first version of
   this file used `setdefault`, which does nothing when the variable is already
   set — so a developer or CI runner with `DATABASE_URL` or
   `AWS_ACCESS_KEY_ID` exported handed those straight to the tests. Fixed by
   OVERWRITING, not defaulting.

3. **Empty credentials do not prevent network calls.** boto3 with a blank key
   still resolves DNS, opens TLS and gets rejected by AWS — an outbound request
   to a real endpoint, which a test that accepts a 502 will happily pass over.
   Fixed by a socket guard that blocks non-loopback connections and makes the
   whole run exit non-zero if one was attempted.

Point 3 is the one that matters most for reading results: without it, "all
suites exited 0" and "no suite touched the network" are different claims, and
only the first one was ever being checked.

    import hermetic  # noqa: F401  — must precede any `app.` import

Every value below is a placeholder. Nothing here is a real credential.
"""

import atexit
import os
import socket
import sys
import tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANDBOX = tempfile.mkdtemp(prefix="nibbler-hermetic-")

# ── 1 + 2: environment ──────────────────────────────────────────────────────

# OVERWRITTEN, never defaulted. An inherited production value is exactly what
# this is defending against, so "only set it if absent" is the wrong verb.
_FORCED_BLANK = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "PINECONE_API_KEY", "VOYAGE_API_KEY", "MIXPANEL_TOKEN", "RESEND_API_KEY",
    "EXPO_ACCESS_TOKEN", "REVENUECAT_SECRET_API_KEY", "REVENUECAT_WEBHOOK_SECRET",
    "OPENAI_API_KEY", "OPENAI_LLM_API_KEY", "ANTHROPIC_LLM_API_KEY",
    "CLAUDE_API_KEY", "QWEN_API_KEY", "QWEN_BASE_URL",
    "FIREBASE_PRIVATE_KEY", "FIREBASE_PRIVATE_KEY_ID", "FIREBASE_CLIENT_EMAIL",
    "FIREBASE_CLIENT_ID", "BUG_DRIVE_FOLDER_ID",
)
for _name in _FORCED_BLANK:
    os.environ[_name] = ""

os.environ["FIREBASE_PROJECT_ID"] = "test-project"
os.environ["SECRET_KEY"] = "test-secret-key-not-a-real-one-000000"
os.environ["APP_ENV"] = "test"

# A suite that already chose its own throwaway SQLite file keeps it — several
# do, and they assert against that file. Anything else (an inherited Postgres
# URL, most of all) is replaced.
_db = os.environ.get("DATABASE_URL", "")
if not _db.startswith("sqlite://"):
    os.environ["DATABASE_URL"] = "sqlite:///%s/test.db" % SANDBOX

# THE important line for `.env`: with the CWD elsewhere, it cannot be found.
os.chdir(SANDBOX)
sys.path.insert(0, BACKEND)


# ── 3: network ──────────────────────────────────────────────────────────────

blocked_attempts = []

_real_socket_connect = socket.socket.connect
_real_create_connection = socket.create_connection


def _is_loopback(address) -> bool:
    """Loopback stays open: it is not the outside world, and blocking it would
    break anything that talks to a local test fixture."""
    try:
        host = address[0]
    except (TypeError, IndexError):
        return False
    return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "")


class NetworkBlocked(RuntimeError):
    """Raised in place of an outbound connection during tests."""


def _guarded_connect(self, address, *args, **kwargs):
    if isinstance(address, tuple) and not _is_loopback(address):
        blocked_attempts.append(address)
        raise NetworkBlocked("NETWORK_ATTEMPT_BLOCKED %r" % (address,))
    return _real_socket_connect(self, address, *args, **kwargs)


def _guarded_create_connection(address, *args, **kwargs):
    if not _is_loopback(address):
        blocked_attempts.append(address)
        raise NetworkBlocked("NETWORK_ATTEMPT_BLOCKED %r" % (address,))
    return _real_create_connection(address, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.create_connection = _guarded_create_connection


@atexit.register
def _fail_on_network_use():
    """Make an outbound attempt fail the RUN, not just the request.

    Without this the guard is invisible: boto3 swallows the error, the endpoint
    returns 502, the test asserts "502 is acceptable" and the suite exits 0 —
    with a real connection attempt to AWS hidden inside a green result. Exiting
    non-zero here is the difference between a suite that is isolated and a
    suite that merely looks isolated.
    """
    if not blocked_attempts:
        return
    sys.stdout.flush()
    sys.stderr.write(
        "\nHERMETIC FAILURE: %d outbound connection attempt(s) during this run:\n"
        % len(blocked_attempts)
    )
    for addr in blocked_attempts[:10]:
        sys.stderr.write("  NETWORK_ATTEMPT_BLOCKED %r\n" % (addr,))
    sys.stderr.write("Tests must not contact external services. Stub the client.\n")
    sys.stderr.flush()
    os._exit(1)


def env_file_is_unreachable() -> bool:
    """True when no `.env` is visible from the current working directory."""
    return not os.path.exists(".env")


def production_env_is_purged() -> bool:
    """True when nothing that could be a real credential survived import."""
    return all(os.environ.get(n, "") == "" for n in _FORCED_BLANK)
