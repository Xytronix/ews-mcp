"""Shared test helpers."""
from __future__ import annotations

from exchangelib import (
    Account,
    Build,
    Configuration,
    Credentials,
    DELEGATE,
    Version,
)

ADDRESS = "user@example.com"


def offline_account(address: str = ADDRESS) -> Account:
    """A real exchangelib ``Account`` that never touches the network.

    exchangelib rejects a stub object in ``Message(account=...)``, and item
    serialisation needs the account's protocol version, so tests that inspect
    an outbound payload need the real class — just not a real server.
    """
    config = Configuration(
        credentials=Credentials(address, "secret"),
        service_endpoint="https://mail.example.com/EWS/Exchange.asmx",
        auth_type="basic",
        version=Version(Build(15, 1, 0, 0)),
    )
    return Account(address, config=config, autodiscover=False, access_type=DELEGATE)
