"""Base class for tools that ride the SmarterMail private JSON API."""

from typing import Any, Dict, Optional

from .base import BaseTool
from ..exceptions import ToolExecutionError, ValidationError
from ..smartermail import SmarterMailClient, SmarterMailError

#: Shared schema fragment: every SmarterMail tool addresses one mailbox.
MAILBOX_PROPERTY: Dict[str, Any] = {
    "target_mailbox": {
        "type": "string",
        "description": (
            "Mailbox to operate on. Default: the primary mailbox. Only "
            "SmarterMail mailboxes are supported (not iCloud/IMAP or "
            "Outlook/Graph accounts)."
        ),
    }
}


def smartermail_client(ews_client, target_mailbox: Optional[str] = None) -> SmarterMailClient:
    """SmarterMail client for ``target_mailbox``, or a clear refusal.

    Lives outside :class:`SmarterMailTool` because non-SmarterMail tools need
    the same wire — ``add_attachment(storage_file_id=...)`` pulls bytes from
    personal file storage without otherwise being a SmarterMail tool.
    """
    registry = ews_client.smartermail
    if not registry.enabled:
        raise ToolExecutionError(
            "SmarterMail REST tools are disabled. Set SMARTERMAIL_ENABLED=true "
            "and SMARTERMAIL_BASE_URL (or EWS_SERVER_URL) to use them."
        )
    try:
        return registry.get(target_mailbox)
    except SmarterMailError as exc:
        raise ToolExecutionError(str(exc)) from exc


class SmarterMailTool(BaseTool):
    """Mixin/base giving tools an authenticated SmarterMail client.

    The API is per-mailbox and password-authenticated, so a tool asks for the
    client of the mailbox it was pointed at. IMAP/Graph mailboxes are rejected
    up front: this wire only speaks to the SmarterMail server.
    """

    #: Set on subclasses that touch domain-level administration.
    requires_domain_admin: bool = False

    def client(self, target_mailbox: Optional[str] = None) -> SmarterMailClient:
        """Return the SmarterMail client for ``target_mailbox``."""
        self.require_exchange_mailbox(target_mailbox)
        client = smartermail_client(self.ews_client, target_mailbox)
        if self.requires_domain_admin:
            self.require_admin_tools()
        return client

    def require_admin_tools(self) -> None:
        """Refuse a domain-admin operation unless the operator opted in."""
        if not getattr(self.ews_client.config, "smartermail_admin_tools", False):
            raise ToolExecutionError(
                "Domain administration tools are disabled. Set "
                "SMARTERMAIL_ADMIN_TOOLS=true to enable them — they can "
                "create, rename, disable and delete mailboxes."
            )

    def call(self, client: SmarterMailClient, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """Run one API call, mapping API errors onto tool errors."""
        try:
            return client.request(method, path, **kwargs)
        except SmarterMailError as exc:
            raise ToolExecutionError(str(exc)) from exc

    def schema(
        self,
        name: str,
        description: str,
        properties: Dict[str, Any],
        required: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Build the MCP schema with the shared mailbox parameter attached."""
        props = dict(properties)
        props.update(MAILBOX_PROPERTY)
        schema: Dict[str, Any] = {
            "name": name,
            "description": description,
            "inputSchema": {"type": "object", "properties": props},
        }
        if required:
            schema["inputSchema"]["required"] = list(required)
        return schema

    @staticmethod
    def require(kwargs: Dict[str, Any], *names: str) -> None:
        """Validate that required arguments are present and non-empty."""
        missing = [n for n in names if kwargs.get(n) in (None, "", [], {})]
        if missing:
            raise ValidationError(f"{', '.join(missing)} required")
