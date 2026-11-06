"""SmarterMail private REST wire.

The EWS surface cannot express a large part of what the mail server actually
does (scheduled send, signatures, notes, file storage, virtual folders, domain
administration, ...). SmarterMail's own JSON API — the one its webmail talks
to — covers all of it, so these tools ride that wire instead.

The API is private and undocumented: paths and payload shapes were read off the
webmail bundle and verified against the live server. It is version-coupled to
the SmarterMail build and only serves SmarterMail mailboxes, never the
IMAP/iCloud or Graph/Outlook backends.
"""

from .client import (
    SmarterMailClient,
    SmarterMailError,
    SmarterMailRegistry,
    SCHEDULED_FOLDER,
)

__all__ = [
    "SmarterMailClient",
    "SmarterMailError",
    "SmarterMailRegistry",
    "SCHEDULED_FOLDER",
]
