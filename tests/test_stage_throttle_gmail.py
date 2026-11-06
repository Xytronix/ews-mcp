"""Stage upload endpoint, EWS throttling backoff, Gmail account preset."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import main as main_mod  # noqa: E402
from src.mail_backends.accounts import accounts_from_env  # noqa: E402
from src.tools import email_tools as et  # noqa: E402


# ------------------------------------------------------------- /stage
def _call_stage(monkeypatch, tmp_path, *, token="tok", auth="Bearer tok",
                body=b"%PDF-1.4 data", name="a.pdf", method="POST"):
    monkeypatch.setenv("EWS_STAGING_DIR", str(tmp_path))
    monkeypatch.setattr("src.attachment_staging.staging_root", lambda: tmp_path)
    if token is None:
        monkeypatch.delenv("STAGE_UPLOAD_TOKEN", raising=False)
    else:
        monkeypatch.setenv("STAGE_UPLOAD_TOKEN", token)
    sent = []

    async def send_json(_send, status, payload):
        sent.append((status, json.loads(payload)))

    msgs = [{"type": "http.request", "body": body[:5], "more_body": True},
            {"type": "http.request", "body": body[5:], "more_body": False}]

    async def receive():
        return msgs.pop(0)

    headers = [(b"x-file-name", name.encode()), (b"content-type", b"application/pdf")]
    if auth:
        headers.append((b"authorization", auth.encode()))
    scope = {"method": method, "headers": headers, "query_string": b"ttl_seconds=600"}
    asyncio.run(main_mod._handle_stage_upload(scope, receive, None, send_json))
    return sent[0]


def test_stage_upload_ok(monkeypatch, tmp_path):
    status, data = _call_stage(monkeypatch, tmp_path)
    assert status == 200 and data["success"]
    assert data["size_bytes"] == len(b"%PDF-1.4 data")
    assert Path(data["file_path"]).read_bytes() == b"%PDF-1.4 data"


def test_stage_upload_auth_and_disabled(monkeypatch, tmp_path):
    assert _call_stage(monkeypatch, tmp_path, auth="Bearer nope")[0] == 401
    assert _call_stage(monkeypatch, tmp_path, auth=None)[0] == 401
    assert _call_stage(monkeypatch, tmp_path, token=None)[0] == 404
    assert _call_stage(monkeypatch, tmp_path, method="GET")[0] == 405
    assert _call_stage(monkeypatch, tmp_path, name="../x")[0] == 400


# ---------------------------------------------------------- throttling
class ErrorServerBusy(Exception):
    pass


def test_throttled_exception_retried_with_backoff():
    calls, sleeps = [], []

    async def factory():
        calls.append(1)
        if len(calls) < 3:
            raise ErrorServerBusy("The server cannot service this request right now")
        return {"success": True}

    async def fake_sleep(d):
        sleeps.append(d)
    et._SEARCH_LOCK = None
    res = asyncio.run(et.run_throttled(factory, sleep=fake_sleep))
    assert res == {"success": True} and len(calls) == 3
    assert sleeps == [et.THROTTLE_BASE_DELAY, et.THROTTLE_BASE_DELAY * 2]


def test_throttled_meta_retried_and_429_classified():
    results = [{"meta": {"error_code": "THROTTLED"}},
               {"meta": {"per_folder_errors": [{"error_code": "THROTTLED"}]}},
               {"success": True, "items": []}]

    async def factory():
        return results.pop(0)

    async def fake_sleep(_d):
        pass
    et._SEARCH_LOCK = None
    assert asyncio.run(et.run_throttled(factory, sleep=fake_sleep))["success"]
    assert et._classify_ews_error(Exception("HTTP 429 Too Many Requests")) == "THROTTLED"


def test_non_throttle_error_not_retried_and_gives_up():
    async def boom():
        raise ValueError("bad")
    et._SEARCH_LOCK = None
    try:
        asyncio.run(et.run_throttled(boom))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    n = []

    async def busy():
        n.append(1)
        raise ErrorServerBusy("busy")

    async def fake_sleep(_d):
        pass
    try:
        asyncio.run(et.run_throttled(busy, sleep=fake_sleep, max_retries=2))
    except ErrorServerBusy:
        pass
    assert len(n) == 3


def test_searches_are_serialized():
    active, peak = [0], [0]

    async def factory():
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.01)
        active[0] -= 1
        return {"success": True}

    async def main():
        et._SEARCH_LOCK = None
        await asyncio.gather(*[et.run_throttled(factory) for _ in range(5)])
    asyncio.run(main())
    assert peak[0] == 1


# --------------------------------------------------------------- Gmail
def test_gmail_env_account():
    accs = accounts_from_env({"GMAIL_EMAIL": "x@gmail.com",
                              "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"})
    (acc,) = accs
    assert acc.kind == "imap" and acc.provider == "gmail"
    assert acc.get("imap_host") == "imap.gmail.com"
    assert acc.get("smtp_host") == "smtp.gmail.com"
    assert accounts_from_env({"GMAIL_EMAIL": "x@gmail.com"}) == []


# ------------------------------------------------- tool-call rate limiter
def test_rate_limiter_waits_instead_of_failing():
    from src.middleware.rate_limiter import RateLimiter
    from src.exceptions import RateLimitError
    import src.middleware.rate_limiter as rl
    clock = [1000.0]
    rl.time = lambda: clock[0]
    try:
        lim = RateLimiter(3)
        slept = []

        async def fake_sleep(d):
            slept.append(d)
            clock[0] += d

        async def burst(n, max_wait=120):
            for _ in range(n):
                await lim.acquire(max_wait=max_wait, sleep=fake_sleep)
        asyncio.run(burst(5))
        assert len(slept) == 2 and abs(sum(slept) - 60) < 1
        try:
            asyncio.run(burst(4, max_wait=1))
            raise AssertionError("expected RateLimitError")
        except RateLimitError:
            pass
    finally:
        import time as _t
        rl.time = _t.time
