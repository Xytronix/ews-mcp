"""EWS_VERSION: pinned, product names, and detect-once in "auto" mode.

The server version used to be hard-coded to SmarterMail's Exchange 2016
build for every EWS_SERVER_URL deployment, so Exchange 2013 or 2019 servers
were spoken to with the wrong schema. Detection is now the default, done once
per process and reused for every mailbox on the endpoint.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchangelib import Build, Credentials, Version  # noqa: E402
from exchangelib.protocol import CachingProtocol  # noqa: E402

from src import ews_client  # noqa: E402
from src.config import Settings, parse_ews_version  # noqa: E402

ENDPOINT = "https://mail.example.com/EWS/Exchange.asmx"


@pytest.fixture(autouse=True)
def fresh_protocols():
    """exchangelib caches one Protocol per endpoint and credentials process-wide."""
    CachingProtocol.clear_cache()
    yield
    CachingProtocol.clear_cache()


@pytest.mark.parametrize(("value", "api_version", "build"), [
    ("2016", "Exchange2016", Build(15, 1)),
    ("Exchange2019", "Exchange2019", Build(15, 2)),
    ("exchange_2013_sp1", "Exchange2013_SP1", Build(15, 0, 847)),
    ("2010 SP2", "Exchange2010_SP2", Build(14, 2)),
    ("15.1.1779.4", "Exchange2016", Build(15, 1, 1779, 4)),
    ("15.0", "Exchange2013", Build(15, 0)),
])
def test_named_and_numbered_versions(value, api_version, build):
    version = parse_ews_version(value)
    assert version.api_version == api_version
    assert version.build == build


@pytest.mark.parametrize("value", [None, "", "auto", " AUTO "])
def test_auto_means_detect(value):
    assert parse_ews_version(value) is None


def test_unknown_version_fails_at_startup_naming_the_choices():
    with pytest.raises(ValueError, match="2016") as exc:
        Settings(
            ews_email="user@example.com", ews_auth_type="basic", ews_username="user",
            ews_password="x", ews_server_url=ENDPOINT, ews_version="Exchange2099",
            _env_file=None,
        )
    assert "15.1.1779.4" in str(exc.value)


def _client(monkeypatch, version, accounts=("other@example.com",)):
    config = SimpleNamespace(
        ews_email="user@example.com", ews_server_url=ENDPOINT, ews_autodiscover=False,
        ews_version=version, ews_auth_type="basic", timezone="UTC",
        connection_pool_size=2, request_timeout=30, ews_insecure_skip_verify=False,
        ews_accounts={address: "secret" for address in accounts},
        ews_impersonation_enabled=False,
    )
    auth = SimpleNamespace(get_credentials=lambda: Credentials("user@example.com", "secret"))
    return ews_client.EWSClient(config, auth)


@pytest.fixture
def detections(monkeypatch):
    """Count server version detections; each answers Exchange 2019."""
    calls = []

    def guess(protocol, api_version_hint=None):
        calls.append(protocol.service_endpoint)
        return Version(build=Build(15, 2, 1544, 4))

    monkeypatch.setattr(ews_client.Version, "guess", classmethod(lambda cls, p, api_version_hint=None: guess(p)))
    return calls


def test_auto_detects_once_and_reuses_it_for_every_mailbox(monkeypatch, detections):
    client = _client(monkeypatch, "auto", accounts=("a@example.com", "b@example.com"))

    primary = client.account
    other_a = client.get_account("a@example.com")
    other_b = client.get_account("b@example.com")

    assert detections == [ENDPOINT]
    for account in (primary, other_a, other_b):
        assert account.version.build == Build(15, 2, 1544, 4)


def test_a_pinned_version_never_detects(monkeypatch, detections):
    client = _client(monkeypatch, "2013_SP1")

    primary = client.account
    other = client.get_account("other@example.com")

    assert detections == []
    assert primary.version.api_version == other.version.api_version == "Exchange2013_SP1"
