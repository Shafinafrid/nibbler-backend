"""F12 regression: submit and durably poll Mixpanel GDPR deletion jobs."""

import asyncio
from unittest import mock

import hermetic  # noqa: F401

from app.services import mixpanel_service


failures = []


def check(name, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Client:
    responses = []
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


settings = mixpanel_service.get_settings()
old_token = settings.mixpanel_token
old_bearer = settings.mixpanel_gdpr_bearer_token
settings.mixpanel_token = "project-token"
settings.mixpanel_gdpr_bearer_token = "oauth-token"
Client.calls = []
Client.responses = [Response({
    "status": "ok",
    "results": [{"status": "PENDING", "tracking_id": "job-123"}],
})]
with mock.patch.object(mixpanel_service.httpx, "AsyncClient", Client):
    submitted = asyncio.run(mixpanel_service.delete_historical_events("firebase-uid"))
check("submission records Mixpanel's nested tracking id", submitted == (False, "job-123", "pending"), submitted)
method, url, kwargs = Client.calls[0]
check("submission uses the documented v3 endpoint", method == "POST" and url.endswith("/data-deletions/v3.0/"), url)
check("OAuth credential is a Bearer header", kwargs["headers"].get("Authorization") == "Bearer oauth-token")
check("only the requested distinct id is submitted", kwargs["json"].get("distinct_ids") == ["firebase-uid"])

Client.calls = []
Client.responses = [Response({"status": "ok", "results": {"status": "SUCCESS"}})]
with mock.patch.object(mixpanel_service.httpx, "AsyncClient", Client):
    completed = asyncio.run(mixpanel_service.delete_historical_events("firebase-uid", "job-123"))
check("a SUCCESS poll is complete", completed == (True, "job-123", "success"), completed)
check("poll addresses only the persisted task", Client.calls[0][0] == "GET" and Client.calls[0][1].endswith("/job-123"))

Client.responses = [Response({"status": "ok", "results": {"status": "FAILURE"}})]
with mock.patch.object(mixpanel_service.httpx, "AsyncClient", Client):
    failed = asyncio.run(mixpanel_service.delete_historical_events("firebase-uid", "job-123"))
check("a failed provider job clears its id so durable retry can resubmit", failed == (False, "", "failure"), failed)

settings.mixpanel_token = old_token
settings.mixpanel_gdpr_bearer_token = old_bearer

if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    raise SystemExit(1)
print("RESULT: ALL PASS")
