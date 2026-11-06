"""Provider-agnostic mailbox backends (EWS, IMAP/SMTP, Microsoft Graph)."""

from .accounts import AccountConfigError, MailAccount
from .base import (
    BODY_FORMATS,
    WELL_KNOWN_FOLDERS,
    MailAttachmentInfo,
    MailBackend,
    MailBackendError,
    MailFolder,
    MailMessage,
    OutgoingMessage,
    normalize_folder,
)
from .registry import MailboxRegistry

__all__ = [
    "AccountConfigError",
    "BODY_FORMATS",
    "MailAccount",
    "MailAttachmentInfo",
    "MailBackend",
    "MailBackendError",
    "MailFolder",
    "MailMessage",
    "MailboxRegistry",
    "OutgoingMessage",
    "WELL_KNOWN_FOLDERS",
    "normalize_folder",
]
