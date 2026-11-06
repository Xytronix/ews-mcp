"""With outbound tracking off the send log names no ``None`` tracking id."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exchangelib import Message  # noqa: E402

from src.tools import email_tools  # noqa: E402
from tests.support import offline_account  # noqa: E402


class _Client:
    mailbox_registry = None

    def __init__(self, account):
        self.account = account
        self.config = SimpleNamespace(ews_email=account.primary_smtp_address)

    def get_account(self, target_mailbox=None):
        return self.account


def test_send_log_reads_cleanly_without_a_tracking_id(monkeypatch, caplog):
    monkeypatch.setattr(email_tools, "new_outbound_tracking_id", lambda: None)
    monkeypatch.setattr(Message, "send", lambda self, *a, **k: None)
    caplog.set_level(logging.INFO)

    response = asyncio.run(
        email_tools.SendEmailTool(_Client(offline_account())).execute(
            to=["someone@example.com"], subject="probe", body="plain text"
        )
    )

    assert response["tracking_id"] is None
    accepted = [r.getMessage() for r in caplog.records if "accepted" in r.getMessage()]
    assert accepted == [
        "Mail server accepted message to someone@example.com with 0 attachment(s)"
    ]
