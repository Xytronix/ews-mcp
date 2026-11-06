"""Exchange Web Services client wrapper."""

from exchangelib import Account, Configuration, Credentials, BASIC, NTLM, OAUTH2, DELEGATE, IMPERSONATION, FaultTolerance, Version, EWSTimeZone
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_not_exception_type
import logging
import pytz
from typing import Optional, Dict

from .config import Settings, parse_ews_version
from .auth import AuthHandler
from .exceptions import EWSConnectionError, AuthenticationError


# exchangelib auth scheme per configured EWS_AUTH_TYPE. SmarterMail needs the
# scheme pinned (its EWS endpoint 401s exchangelib's auth probing), but the pin
# has to follow the configuration so OAuth2 / NTLM deployments keep their own
# scheme instead of being downgraded to Basic.
EWS_AUTH_TYPES = {"basic": BASIC, "ntlm": NTLM, "oauth2": OAUTH2}


class EWSClient:
    """Exchange Web Services client wrapper with connection management."""

    def __init__(self, config: Settings, auth_handler: AuthHandler):
        self.config = config
        self.auth_handler = auth_handler
        self.logger = logging.getLogger(__name__)
        self._account: Optional[Account] = None
        self._impersonated_accounts: Dict[str, Account] = {}
        # EWS_VERSION pins the schema version. With "auto" the first account
        # on EWS_SERVER_URL detects it and later mailboxes reuse the answer.
        self._configured_version = parse_ews_version(getattr(config, "ews_version", None))
        self._detected_version: Optional[Version] = None

        self._sqlite_cache = None
        self._mailbox_registry = None
        self._smartermail = None

        if getattr(self.config, "ews_insecure_skip_verify", False):
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
            self.logger.warning(
                "EWS_INSECURE_SKIP_VERIFY=true: TLS certificate verification "
                "DISABLED for all Exchange traffic. This is unsafe on untrusted "
                "networks. Prefer installing your internal CA bundle."
            )

    @property
    def account(self) -> Account:
        """Lazy load account connection."""
        if self._account is None:
            self._account = self._create_account()
        return self._account

    @property
    def sqlite_cache(self):
        """Lazy unified SQLite cache (body_format + attachment_text + embedding)."""
        if self._sqlite_cache is None:
            from pathlib import Path
            from .cache.sqlite_cache import SQLiteCache
            data_dir = Path(getattr(self.config, "data_dir", "data"))
            mailbox_slug = (
                self.config.ews_email.replace("@", "_at_").replace(".", "_")
            )
            db_path = data_dir / f"ews_mcp_{mailbox_slug}.sqlite"
            self._sqlite_cache = SQLiteCache(db_path)
            legacy = data_dir / "embeddings" / "embeddings.json"
            if legacy.exists():
                legacy_model = (
                    getattr(self.config, "ai_embedding_model", None)
                    or "nomic-embed-text"
                )
                stats = self._sqlite_cache.import_legacy_embeddings_json(
                    legacy, legacy_model
                )
                self.logger.info(
                    "Embedding cache migrated to SQLite: %s", stats
                )
        return self._sqlite_cache

    @property
    def mailbox_registry(self):
        """Lazy provider-agnostic mailbox registry (EWS + IMAP + Graph).

        Owned here because the registry needs both the settings and this
        client (the EWS backend delegates to :meth:`get_account`). Building it
        opens no connections.
        """
        if self._mailbox_registry is None:
            from .mail_backends.registry import MailboxRegistry
            self._mailbox_registry = MailboxRegistry(self.config, self)
        return self._mailbox_registry

    @property
    def smartermail(self):
        """Lazy SmarterMail REST registry (private JSON API, one client per
        mailbox). Building it opens no connections.
        """
        if self._smartermail is None:
            from .smartermail import SmarterMailRegistry
            self._smartermail = SmarterMailRegistry(self.config)
        return self._smartermail

    def _auth_type(self):
        """exchangelib ``auth_type`` for the configured EWS_AUTH_TYPE.

        Returns None for an unrecognised value so exchangelib falls back to
        its own detection instead of forcing the wrong scheme.
        """
        return EWS_AUTH_TYPES.get(self.config.ews_auth_type)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_not_exception_type(AuthenticationError)
    )
    def _create_account(self) -> Account:
        """Create Exchange account with retry logic."""
        try:
            self.logger.info(f"Connecting to Exchange for {self.config.ews_email}")
            self.logger.info(f"Using timezone: {self.config.timezone}")

            credentials = self.auth_handler.get_credentials()

            try:
                tz = EWSTimeZone(self.config.timezone)
                self.logger.info(f"Successfully loaded timezone: {self.config.timezone}")
            except Exception as e:
                self.logger.warning(f"Failed to load timezone {self.config.timezone}, falling back to UTC: {e}")
                tz = EWSTimeZone('UTC')

            use_manual_config = bool(self.config.ews_server_url)

            if not use_manual_config and self.config.ews_autodiscover:
                self.logger.info("Using autodiscovery (no EWS_SERVER_URL provided)")

                BaseProtocol.TIMEOUT = self.config.request_timeout

                account = Account(
                    primary_smtp_address=self.config.ews_email,
                    credentials=credentials,
                    autodiscover=True,
                    access_type=DELEGATE,
                    default_timezone=tz
                )
            elif use_manual_config:
                self.logger.info(f"Using manual configuration: {self.config.ews_server_url}")

                ews_input = self.config.ews_server_url.strip()

                if ews_input.endswith('/EWS/Exchange.asmx'):
                    ews_url = ews_input
                elif '/EWS/' in ews_input:
                    if not ews_input.endswith('.asmx'):
                        ews_url = ews_input.rstrip('/') + '/Exchange.asmx'
                    else:
                        ews_url = ews_input
                else:
                    server = ews_input.replace('https://', '').replace('http://', '').rstrip('/')
                    ews_url = f"https://{server}/EWS/Exchange.asmx"

                self.logger.info(f"Using EWS endpoint: {ews_url}")

                config = Configuration(
                    service_endpoint=ews_url,
                    credentials=credentials,
                    auth_type=self._auth_type(),
                    version=self._ews_version(),
                    retry_policy=FaultTolerance(max_wait=120),
                    max_connections=self.config.connection_pool_size
                )

                BaseProtocol.TIMEOUT = self.config.request_timeout

                account = Account(
                    primary_smtp_address=self.config.ews_email,
                    config=config,
                    autodiscover=False,
                    access_type=DELEGATE,
                    default_timezone=tz
                )
                self._remember_version(account)
            else:
                raise EWSConnectionError(
                    "No EWS_SERVER_URL provided and EWS_AUTODISCOVER is disabled. "
                    "Either provide EWS_SERVER_URL or enable EWS_AUTODISCOVER."
                )

            return account

        except AuthenticationError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to create account: {e}")
            raise EWSConnectionError(f"Failed to connect to Exchange: {e}")

    def _ews_version(self) -> Optional[Version]:
        """Version for a Configuration on EWS_SERVER_URL; None lets exchangelib detect it.

        Detection tries API versions until the server answers, a burst of
        requests that a throttling server (hosted SmarterMail) can refuse.
        Reusing the first answer makes N mailboxes cost one detection.
        """
        if self._configured_version is not None:
            return self._configured_version
        return self._detected_version

    def _remember_version(self, account: Account) -> None:
        """Keep the version the first EWS_SERVER_URL account detected."""
        if self._configured_version is None and self._detected_version is None:
            self._detected_version = getattr(account, "version", None)

    def test_connection(self) -> bool:
        """Test EWS connection."""
        try:
            _ = self.account.inbox.total_count
            self.logger.info("Connection test successful")
            return True
        except Exception as e:
            self.logger.error(f"Connection test failed: {e}")
            return False

    def close(self) -> None:
        """Close all connections and cleanup."""
        self.clear_impersonation_cache()

        if self._mailbox_registry is not None:
            self._mailbox_registry.close()

        if self._smartermail is not None:
            self._smartermail.close()

        if self._account:
            self.logger.info("Closing EWS connection")
            self._account.protocol.close()
            self._account = None

    def get_account(self, target_mailbox: Optional[str] = None) -> Account:
        """Get Exchange account, optionally for a different mailbox."""
        if not target_mailbox:
            return self.account

        if target_mailbox.lower() == self.config.ews_email.lower():
            return self.account

        cache_key = target_mailbox.lower()
        if cache_key in self._impersonated_accounts:
            self.logger.debug(f"Using cached account for {target_mailbox}")
            return self._impersonated_accounts[cache_key]

        try:
            tz = EWSTimeZone(self.config.timezone)
        except Exception:
            tz = EWSTimeZone('UTC')

        mailbox_password = None
        for _email, _pw in self.config.ews_accounts.items():
            if _email.lower() == cache_key:
                mailbox_password = _pw
                break

        if mailbox_password is not None:
            if not self.config.ews_server_url:
                raise EWSConnectionError(
                    "EWS_SERVER_URL is required for multi-account mode."
                )
            self.logger.info(f"Building account for {target_mailbox} (own credentials)")
            config = Configuration(
                service_endpoint=self._get_ews_url(),
                credentials=Credentials(username=target_mailbox, password=mailbox_password),
                auth_type=self._auth_type(),
                version=self._ews_version(),
                retry_policy=FaultTolerance(max_wait=120),
                max_connections=self.config.connection_pool_size,
            )
            account = Account(
                primary_smtp_address=target_mailbox,
                config=config,
                autodiscover=False,
                access_type=DELEGATE,
                default_timezone=tz,
            )
            self._remember_version(account)
            self._impersonated_accounts[cache_key] = account
            return account

        if not self.config.ews_impersonation_enabled:
            raise EWSConnectionError(
                f"No credentials configured for mailbox {target_mailbox}. "
                f"Add it to the ews_accounts table (preferred), or enable EWS_IMPERSONATION_ENABLED."
            )

        try:
            self.logger.info(f"Creating impersonated account for {target_mailbox}")
            access_type = (
                IMPERSONATION
                if self.config.ews_impersonation_type == "impersonation"
                else DELEGATE
            )
            credentials = self.auth_handler.get_credentials()
            if self.config.ews_server_url:
                config = Configuration(
                    service_endpoint=self._get_ews_url(),
                    credentials=credentials,
                    auth_type=self._auth_type(),
                    version=self._ews_version(),
                    retry_policy=FaultTolerance(max_wait=120),
                    max_connections=self.config.connection_pool_size,
                )
                impersonated_account = Account(
                    primary_smtp_address=target_mailbox,
                    config=config,
                    autodiscover=False,
                    access_type=access_type,
                    default_timezone=tz,
                )
                self._remember_version(impersonated_account)
            elif self.config.ews_autodiscover:
                impersonated_account = Account(
                    primary_smtp_address=target_mailbox,
                    credentials=credentials,
                    autodiscover=True,
                    access_type=access_type,
                    default_timezone=tz,
                )
            else:
                raise EWSConnectionError(
                    "No EWS_SERVER_URL provided and EWS_AUTODISCOVER is disabled."
                )
            self._impersonated_accounts[cache_key] = impersonated_account
            return impersonated_account
        except Exception as e:
            self.logger.error(f"Failed to access mailbox {target_mailbox}: {e}")
            raise EWSConnectionError(
                f"Failed to access mailbox {target_mailbox}: {e}."
            )

    def _get_ews_url(self) -> str:
        """Get normalized EWS endpoint URL."""
        ews_input = self.config.ews_server_url.strip()
        if ews_input.endswith('/EWS/Exchange.asmx'):
            return ews_input
        elif '/EWS/' in ews_input:
            if not ews_input.endswith('.asmx'):
                return ews_input.rstrip('/') + '/Exchange.asmx'
            return ews_input
        else:
            server = ews_input.replace('https://', '').replace('http://', '').rstrip('/')
            return f"https://{server}/EWS/Exchange.asmx"

    def clear_impersonation_cache(self) -> None:
        """Clear cached impersonated accounts."""
        for email, account in self._impersonated_accounts.items():
            try:
                account.protocol.close()
                self.logger.debug(f"Closed impersonated connection for {email}")
            except Exception:
                pass
        self._impersonated_accounts.clear()
        self.logger.info("Impersonation cache cleared")