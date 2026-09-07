"""Focused regressions for diagnostic findings F05/F06/F11/F14/F17/F18."""

import datetime
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import hermetic  # noqa: F401
from fastapi import HTTPException
from starlette.requests import Request

from app.middleware import auth as auth_middleware
from app.models.user import User
from app.routers import library
from app.routers import sync as sync_router
from app.services import embedding_service, personalization_history, url_safety


failures = []


def check(name, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def _call_without_raising(fn):
    try:
        fn()
        return None
    except Exception as exc:  # pragma: no cover - reported by the assertion
        return type(exc).__name__


def request_with_claims(claims):
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.firebase_claims = claims
    return request


print("\n=== F06 — verified identity boundary ===")
user = User(id="u", email="u@example.com")
try:
    auth_middleware.get_current_verified_user(request_with_claims({"email_verified": False}), user)
    password_blocked = False
except HTTPException as exc:
    password_blocked = exc.status_code == 403 and exc.detail.get("code") == "email_verification_required"
check("unverified password identity is rejected", password_blocked)
check("verified email identity passes", auth_middleware.get_current_verified_user(
    request_with_claims({"email_verified": True}), user,
) is user)
for provider in ("google.com", "apple.com"):
    accepted = auth_middleware.get_current_verified_user(
        request_with_claims({"firebase": {"sign_in_provider": provider}}), user,
    ) is user
    check(f"trusted federated identity passes ({provider})", accepted)


print("\n=== F11 — Firebase cold-start initialization is single-flight ===")
calls = []
fake_apps = {}


def fake_initialize(credential):
    calls.append(credential)
    fake_apps["[DEFAULT]"] = object()


auth_middleware._firebase_initialized = False
with mock.patch.object(auth_middleware.firebase_admin, "_apps", fake_apps), \
     mock.patch.object(auth_middleware.credentials, "Certificate", return_value="credential"), \
     mock.patch.object(auth_middleware.firebase_admin, "initialize_app", side_effect=fake_initialize):
    with ThreadPoolExecutor(max_workers=12) as pool:
        errors = list(pool.map(lambda _: _call_without_raising(auth_middleware.init_firebase), range(24)))
check("24 concurrent callers initialize exactly once", len(calls) == 1, len(calls))
check("no concurrent caller observes an initialization error", not any(errors), errors)


print("\n=== F05 — DNS answers are validated at the actual connection boundary ===")
public_answer = [(2, 1, 6, "", ("93.184.216.34", 80))]
private_answer = [(2, 1, 6, "", ("127.0.0.1", 80))]
pool_hosts = []


class FakeRaw:
    status = 200
    headers = {}

    def stream(self, size):
        return iter([b"ok"])

    def close(self):
        pass


class FakePool:
    def __init__(self, host, **kwargs):
        pool_hosts.append(host)

    def urlopen(self, *args, **kwargs):
        return FakeRaw()

    def close(self):
        pass


with mock.patch.object(url_safety.socket, "getaddrinfo", return_value=public_answer), \
     mock.patch.object(url_safety.urllib3, "HTTPConnectionPool", FakePool):
    response = url_safety.fetch_public_url("http://safe.example/article")
check("transport connects to the validated IP, never the hostname", pool_hosts == ["93.184.216.34"], pool_hosts)
check("pinned transport preserves a normal response", response.status_code == 200 and response.content == b"ok")

pool_hosts.clear()
with mock.patch.object(url_safety.socket, "getaddrinfo", side_effect=[public_answer, private_answer]), \
     mock.patch.object(url_safety.urllib3, "HTTPConnectionPool", FakePool):
    try:
        url_safety.fetch_public_url("http://rebind.example/article")
        rebound_blocked = False
    except url_safety.UnsafeUrlError:
        rebound_blocked = True
check("a public-check/private-connect DNS rebind is blocked", rebound_blocked and not pool_hosts)


print("\n=== F14 — original upload archival retries while bytes still exist ===")
attempts = []


class RetryS3:
    def upload_file(self, **kwargs):
        attempts.append(kwargs)
        if len(attempts) < 3:
            raise RuntimeError("temporary S3 failure")
        return kwargs["filename"]


# The helper imports time inside itself, so patching the stdlib module is the
# reliable seam regardless of library.py's imports.
with mock.patch.object(library, "S3Service", return_value=RetryS3()), \
     mock.patch("time.sleep", return_value=None):
    archived = library._archive_original_with_retry(b"pdf", "u/item.pdf", "application/pdf")
check("two transient failures are retried and the third succeeds", archived == "u/item.pdf" and len(attempts) == 3)


print("\n=== F13 — avatar deletion keeps a durable cleanup identity ===")


class FakeDb:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


avatar_user = types.SimpleNamespace(id="avatar-user", avatar_url="avatar-user/avatar.jpg")
avatar_db = FakeDb()
with mock.patch.object(library, "_cleanup_ledger_upsert_pending", return_value="inserted") as ledger_add, \
     mock.patch.object(library, "_cleanup_ledger_resolve", return_value=True) as ledger_resolve, \
     mock.patch.object(sync_router, "S3Service") as avatar_s3:
    avatar_s3.return_value.delete_file.return_value = False
    avatar_result = sync_router.delete_avatar(request_with_claims({}), avatar_user, avatar_db)
check("cleanup ledger is durable before S3 deletion is attempted", ledger_add.call_count == 1 and avatar_s3.return_value.delete_file.call_count == 1)
check("provider false return stays retryable after the visible avatar clears",
      avatar_result == {"ok": True, "cleanup_pending": True} and avatar_user.avatar_url is None)
check("failed provider outcome is written back to the ledger", ledger_resolve.call_args.args[3] is False)

unsafe_user = types.SimpleNamespace(id="unsafe-avatar", avatar_url="unsafe-avatar/avatar.jpg")
with mock.patch.object(library, "_cleanup_ledger_upsert_pending", return_value="failed"), \
     mock.patch.object(sync_router, "S3Service") as unused_s3:
    try:
        sync_router.delete_avatar(request_with_claims({}), unsafe_user, FakeDb())
        unsafe_blocked = False
    except HTTPException as exc:
        unsafe_blocked = exc.status_code == 503
check("avatar reference is retained if the cleanup identity cannot be persisted",
      unsafe_blocked and unsafe_user.avatar_url == "unsafe-avatar/avatar.jpg" and unused_s3.call_count == 0)


print("\n=== F17 — skipped questions expire without allowing paraphrase repeats ===")
old = datetime.datetime.utcnow() - datetime.timedelta(days=8)
history = [{
    "question": "Would you rather automate this routine or control every step yourself?",
    "options": [
        {"id": "a", "text": "Automate", "tag": "prefers_automation"},
        {"id": "b", "text": "Control it", "tag": "prefers_manual_control"},
    ],
    "tags": [], "status": "pending", "created_at": old,
}]
available = personalization_history.available_tags(history)
check("an unanswered dimension becomes available after seven days",
      "prefers_automation" in available and "prefers_manual_control" in available)
similar = {
    "question": "Would you rather automate this routine or control every step yourself?",
    "options": history[0]["options"],
}
check("the old wording still blocks the same question", not personalization_history.is_novel_question(similar, history))
check("taxonomy now supports substantially more than four dimensions", len(personalization_history.DIMENSIONS) >= 10)


print("\n=== F18 — Voyage owns one bounded transport attempt ===")
created = []


class FakeVoyageClient:
    def __init__(self, **kwargs):
        created.append(kwargs)


old_key = embedding_service.settings.voyage_api_key
old_client = embedding_service._voyage_client
embedding_service.settings.voyage_api_key = "test-key"
embedding_service._voyage_client = None
with mock.patch.dict(sys.modules, {"voyageai": types.SimpleNamespace(Client=FakeVoyageClient)}):
    embedding_service._get_voyage_client()
embedding_service.settings.voyage_api_key = old_key
embedding_service._voyage_client = old_client
check("SDK hidden retries are disabled", created and created[0].get("max_retries") == 0, created)
check("each SDK transport attempt is six seconds", created and created[0].get("timeout") == 6.0, created)


if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print("RESULT: ALL PASS")
