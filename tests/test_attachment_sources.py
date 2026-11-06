"""Remote content sources for add_attachment: file_url and storage_file_id.

No network and no mail server: the URL fetch runs on an ``httpx.MockTransport``
and DNS is stubbed, so the SSRF guard, the size cap, redirect re-validation and
the filename derivation are all exercised offline. The SmarterMail download
rides a fake client that answers the two real endpoints.
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import attachment_sources as sources  # noqa: E402
from src.exceptions import ToolExecutionError, ValidationError  # noqa: E402
from src.smartermail import SmarterMailError  # noqa: E402


#: Captured before the autouse fixture stubs DNS out.
_RESOLVE_HOST = sources._resolve_host
PDF = b"%PDF-1.4 tiny"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every hostname resolves to one public address unless a test says otherwise."""
    monkeypatch.setattr(sources, "_resolve_host", lambda host: ["93.184.216.34"])


def serve(handler, monkeypatch):
    """Route the module's URL fetches through a mock transport."""
    monkeypatch.setattr(
        sources,
        "_build_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ),
    )


def load(kwargs, **extra):
    return asyncio.run(sources.load_attachment_content(kwargs, **extra))


# ------------------------------------------------------------------ selection
def test_no_source_is_rejected():
    with pytest.raises(ValidationError, match="one content source is required"):
        sources.selected_source({"message_id": "MID"})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"file_url": "https://x/y", "file_content": "eA=="},
        {"file_url": "https://x/y", "storage_file_id": "guid"},
        {"file_path": "/tmp/x", "storage_file_id": "guid"},
    ],
)
def test_two_sources_are_rejected(kwargs):
    with pytest.raises(ValidationError, match="exactly one content source"):
        sources.selected_source(kwargs)


def test_content_type_precedence():
    # caller wins, then the file name, then whatever the source reported
    assert sources.resolve_content_type("a.pdf", "text/plain", "image/png") == "text/plain"
    assert sources.resolve_content_type("a.pdf", None, "image/png") == "application/pdf"
    assert sources.resolve_content_type("blob", None, "image/png") == "image/png"
    assert sources.resolve_content_type("blob", None, None) == "application/octet-stream"


# -------------------------------------------------------------------- ssrf
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.pdf",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/x.pdf",
        "http://[::1]/x.pdf",
        "http://192.168.0.9:8000/x.pdf",
    ],
)
def test_literal_private_hosts_are_refused(url):
    with pytest.raises(ValidationError, match="file_url refused"):
        sources._require_fetchable_url(url)


def test_dns_name_pointing_at_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(sources, "_resolve_host", lambda host: ["127.0.0.1"])
    with pytest.raises(ValidationError, match="loopback"):
        sources._require_fetchable_url("https://evil.example/x.pdf")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/1", "/relative"])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(ValidationError, match="http\\(s\\) URL"):
        sources._require_fetchable_url(url)


def test_redirect_into_a_private_host_is_refused(monkeypatch):
    hosts = {"cdn.example": ["93.184.216.34"], "localhost": ["127.0.0.1"]}
    monkeypatch.setattr(sources, "_resolve_host", lambda host: hosts[host])

    def handler(request):
        return httpx.Response(302, headers={"location": "http://localhost/x.pdf"})

    serve(handler, monkeypatch)
    with pytest.raises(ValidationError, match="file_url refused"):
        sources.fetch_url_bytes("https://cdn.example/x.pdf")


def test_redirect_chain_is_capped(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"location": "/next"})

    serve(handler, monkeypatch)
    with pytest.raises(ToolExecutionError, match="more than 3 redirects"):
        sources.fetch_url_bytes("https://cdn.example/x.pdf")


# ------------------------------------------------------------------- fetching
def test_url_fetch_follows_a_redirect_and_uses_content_disposition(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/dl":
            return httpx.Response(302, headers={"location": "/final?x=1"})
        return httpx.Response(
            200,
            content=PDF,
            headers={
                "content-type": "application/pdf; charset=binary",
                "content-disposition": "attachment; filename=\"report final.pdf\"",
            },
        )

    serve(handler, monkeypatch)
    blob = sources.fetch_url_bytes("https://cdn.example/dl")
    assert seen == ["https://cdn.example/dl", "https://cdn.example/final?x=1"]
    assert blob.content == PDF
    assert blob.file_name == "report final.pdf"
    assert blob.content_type == "application/pdf"


def test_url_fetch_prefers_rfc2231_filename(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            content=PDF,
            headers={
                "content-disposition": "attachment; filename*=utf-8''Rechnung%20M%C3%A4rz.pdf"
            },
        )

    serve(handler, monkeypatch)
    assert sources.fetch_url_bytes("https://cdn.example/x").file_name == "Rechnung März.pdf"


def test_url_fetch_falls_back_to_the_url_path(monkeypatch):
    serve(lambda request: httpx.Response(200, content=PDF), monkeypatch)
    blob = sources.fetch_url_bytes("https://cdn.example/docs/my%20file.pdf?v=2")
    assert blob.file_name == "my file.pdf"


def test_url_fetch_strips_a_path_from_content_disposition(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            content=PDF,
            headers={"content-disposition": "attachment; filename=\"../../etc/passwd\""},
        )

    serve(handler, monkeypatch)
    assert sources.fetch_url_bytes("https://cdn.example/x").file_name == "passwd"


def test_url_fetch_rejects_a_declared_oversize_body(monkeypatch):
    def handler(request):
        return httpx.Response(
            200, content=PDF, headers={"content-length": str(50 * 1024 * 1024)}
        )

    serve(handler, monkeypatch)
    with pytest.raises(ToolExecutionError, match="exceeds the"):
        sources.fetch_url_bytes("https://cdn.example/big.iso")


def test_url_fetch_aborts_an_undeclared_oversize_body(monkeypatch):
    def handler(request):
        # An iterator body is streamed with no Content-Length, so only the
        # per-chunk accounting can stop it.
        return httpx.Response(200, content=iter([b"A" * 2000, b"A" * 3000]))

    serve(handler, monkeypatch)
    with pytest.raises(ToolExecutionError, match="body exceeds the 1024 byte limit"):
        sources.fetch_url_bytes("https://cdn.example/big.bin", max_bytes=1024)


def test_url_fetch_reports_http_status(monkeypatch):
    serve(lambda request: httpx.Response(404), monkeypatch)
    with pytest.raises(ToolExecutionError, match="HTTP 404"):
        sources.fetch_url_bytes("https://cdn.example/gone.pdf")


def test_url_fetch_reports_transport_failure(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    serve(handler, monkeypatch)
    with pytest.raises(ToolExecutionError, match="fetch failed"):
        sources.fetch_url_bytes("https://cdn.example/x.pdf")


def test_url_fetch_rejects_an_empty_body(monkeypatch):
    serve(lambda request: httpx.Response(200, content=b""), monkeypatch)
    with pytest.raises(ToolExecutionError, match="empty body"):
        sources.fetch_url_bytes("https://cdn.example/x.pdf")


def test_unresolvable_host_is_reported(monkeypatch):
    import socket

    def boom(host, *a, **kw):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(sources.socket, "getaddrinfo", boom)
    with pytest.raises(ToolExecutionError, match="cannot resolve host"):
        _RESOLVE_HOST("nope.invalid")


# ------------------------------------------------------------ storage_file_id
class FakeStorageClient:
    """Answers the two file-storage endpoints add_attachment needs."""

    def __init__(self, *, blob=PDF, name="quote.pdf", download_error=None):
        self.blob = blob
        self.name = name
        self.download_error = download_error
        self.calls = []

    def download(self, method, path, **kw):
        self.calls.append((method, path))
        if self.download_error:
            raise self.download_error
        return self.blob

    def request(self, method, path, **kw):
        self.calls.append((method, path))
        return {
            "folder": {
                "id": 1,
                "path": "/",
                "files": [{"id": "other", "fileName": "nope.txt"}],
                "subFolders": [
                    {
                        "id": 2,
                        "path": "/Invoices",
                        "files": [{"id": "GUID-1", "fileName": self.name}],
                    }
                ],
            }
        }


def test_storage_download_uses_the_stored_name():
    client = FakeStorageClient()
    blob = sources.fetch_storage_bytes(lambda: client, "GUID-1")
    assert blob.content == PDF
    assert blob.file_name == "quote.pdf"
    assert ("GET", "/api/v1/filestorage/GUID-1/download") in client.calls


def test_storage_download_survives_missing_metadata():
    client = FakeStorageClient(name="quote.pdf")
    client.request = lambda *a, **kw: {"folder": {"id": 1, "files": []}}
    blob = sources.fetch_storage_bytes(lambda: client, "GUID-1")
    assert blob.content == PDF and blob.file_name is None


def test_storage_download_maps_api_errors():
    client = FakeStorageClient(download_error=SmarterMailError("HTTP 404 not found"))
    with pytest.raises(ToolExecutionError, match="storage_file_id GUID-1: HTTP 404"):
        sources.fetch_storage_bytes(lambda: client, "GUID-1")


def test_storage_download_rejects_oversize():
    client = FakeStorageClient(blob=b"A" * 4096)
    with pytest.raises(ToolExecutionError, match="exceeds the 1024 byte limit"):
        sources.fetch_storage_bytes(lambda: client, "GUID-1", max_bytes=1024)


def test_storage_download_rejects_empty_file():
    with pytest.raises(ToolExecutionError, match="is empty"):
        sources.fetch_storage_bytes(lambda: FakeStorageClient(blob=b""), "GUID-1")


# -------------------------------------------------------- load_attachment_content
def test_load_url_names_the_file_and_keeps_the_reported_type(monkeypatch):
    def handler(request):
        return httpx.Response(
            200, content=PDF, headers={"content-type": "application/pdf"}
        )

    serve(handler, monkeypatch)
    loaded = load({"file_url": "https://cdn.example/a/b.pdf"})
    assert loaded.content == PDF
    assert loaded.file_name == "b.pdf"
    assert loaded.source == "file_url"
    assert loaded.source_detail == "https://cdn.example/a/b.pdf"
    assert loaded.remote_content_type == "application/pdf"


def test_load_url_honours_an_explicit_file_name(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            content=PDF,
            headers={"content-disposition": "attachment; filename=server.pdf"},
        )

    serve(handler, monkeypatch)
    assert load({"file_url": "https://cdn.example/x", "file_name": "mine.pdf"}).file_name == "mine.pdf"


def test_load_url_without_any_name_asks_for_one(monkeypatch):
    serve(lambda request: httpx.Response(200, content=PDF), monkeypatch)
    with pytest.raises(ValidationError, match="pass file_name"):
        load({"file_url": "https://cdn.example/"})


def test_load_storage_uses_the_stored_name():
    client = FakeStorageClient()
    loaded = load({"storage_file_id": "GUID-1"}, storage_client=lambda: client)
    assert (loaded.content, loaded.file_name, loaded.source) == (PDF, "quote.pdf", "storage_file_id")
    assert loaded.source_detail == "GUID-1"


def test_load_storage_without_a_smartermail_mailbox_points_elsewhere():
    with pytest.raises(ValidationError, match="file_url or file_content"):
        load(
            {"storage_file_id": "GUID-1"},
            storage_unavailable="user@icloud.com (icloud over IMAP) has no "
            "SmarterMail file storage. Use file_url or file_content.",
        )


def test_load_file_content_still_requires_a_name():
    with pytest.raises(ValidationError, match="file_name is required"):
        load({"file_content": base64.b64encode(PDF).decode()})


def test_load_file_path_reads_the_server_filesystem(tmp_path):
    target = tmp_path / "local.pdf"
    target.write_bytes(PDF)
    loaded = load({"file_path": str(target)})
    assert (loaded.content, loaded.file_name, loaded.source) == (PDF, "local.pdf", "file_path")


def test_load_file_path_missing_file():
    with pytest.raises(ValidationError, match="File not found"):
        load({"file_path": "/nonexistent/nope.pdf"})
