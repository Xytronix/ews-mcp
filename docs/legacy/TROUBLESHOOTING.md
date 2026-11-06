> **Legacy 4.0 documentation.** This page describes the `src/` server as
> extended by this fork. Upstream's `ghcr.io/azizmazrou/ews-mcp:latest` image
> does not include the fork's additions, and the fork publishes no image (see
> the [README](../../README.md#quick-start)). For upstream's 4.5 line see
> [`v5/README.md`](../../v5/README.md) and [`v5/docs/API.md`](../../v5/docs/API.md).

# Troubleshooting Guide

Common issues and solutions for the EWS MCP Server.

> **Note:** Exchange `find_person(source="all")` searches GAL, contacts and email history. On Graph and iCloud/CardDAV, `all` explicitly reports contacts-only partial coverage; GAL/history/domain search and contact analytics are unsupported.

## Authentication Issues

### Problem: "Authentication failed" error

**Symptoms:**
```
AuthenticationError: Authentication setup failed
```

**Solutions:**

1. **Verify OAuth2 Credentials:**
   ```bash
   # Check all required environment variables are set
   echo $EWS_CLIENT_ID
   echo $EWS_TENANT_ID
   # Don't echo the secret for security
   ```

2. **Check Azure AD App Permissions:**
   - Ensure app has `full_access_as_app` or specific EWS permissions
   - Verify admin consent has been granted
   - Check app is not expired or disabled

3. **Test Basic Auth First:**
   ```bash
   # Try basic auth to isolate OAuth2 issues
   EWS_AUTH_TYPE=basic
   EWS_USERNAME=user@company.com
   EWS_PASSWORD=yourpassword
   ```

4. **Check Token Acquisition:**
   - Enable DEBUG logging to see token requests
   - Verify network can reach login.microsoftonline.com
   - Check firewall/proxy settings

### Problem: "Invalid client secret" error

**Solution:**
- Client secrets expire - check expiration in Azure Portal
- Generate new secret and update .env file
- Ensure you copied the secret VALUE, not the ID

## Connection Issues

### Problem: "Connection timeout" or "Cannot connect to Exchange"

**Symptoms:**
```
ConnectionError: Failed to connect to Exchange
```

**Solutions:**

1. **Verify Server URL:**
   ```bash
   # Test connectivity
   curl -v https://outlook.office365.com/EWS/Exchange.asmx

   # Or for on-premises
   curl -v https://mail.company.com/EWS/Exchange.asmx
   ```

2. **Check Network Connectivity:**
   - Verify firewall allows HTTPS (443) to Exchange server
   - Test from Docker container if using Docker
   - Check corporate proxy settings

3. **Try Autodiscovery:**
   ```bash
   EWS_AUTODISCOVER=true
   # Don't set EWS_SERVER_URL
   ```

4. **Increase Timeout:**
   ```bash
   REQUEST_TIMEOUT=60
   ```

### Problem: "Autodiscovery failed" or "Autodiscovery timeout"

**Symptoms:**
```
AutoDiscoverFailed: Autodiscovery failed with error
ConnectionError: Autodiscovery timed out
```

**Solutions:**

1. **Use Manual Configuration (Recommended):**
   ```bash
   EWS_AUTODISCOVER=false
   # Just provide hostname - the server constructs the full EWS URL automatically
   EWS_SERVER_URL=mail.company.com
   # This becomes: https://mail.company.com/EWS/Exchange.asmx
   ```

   You can provide any of these formats:
   | You Provide | Server Constructs |
   |-------------|-------------------|
   | `mail.company.com` | `https://mail.company.com/EWS/Exchange.asmx` |
   | `https://mail.company.com` | `https://mail.company.com/EWS/Exchange.asmx` |
   | `https://mail.company.com/EWS/Exchange.asmx` | (used as-is) |
   | `outlook.office365.com` | `https://outlook.office365.com/EWS/Exchange.asmx` |

2. **Check DNS (if autodiscovery is required):**
   ```bash
   # Should return autodiscover endpoint
   nslookup autodiscover.company.com
   ```

3. **Verify Email Domain:**
   - Ensure EWS_EMAIL matches your Exchange domain
   - Check for typos

4. **Network/Firewall Issues:**
   - Autodiscovery endpoints may be blocked by firewall
   - Using explicit EWS_SERVER_URL bypasses autodiscovery completely
   - This is the recommended approach for most deployments

## Docker Issues

### Problem: Container won't start

**Solutions:**

1. **Check Logs:**
   ```bash
   docker logs ews-mcp-server
   ```

2. **Verify .env File:**
   ```bash
   # Ensure file exists and is readable
   ls -la .env
   cat .env | grep -v PASSWORD
   ```

3. **Check Docker Resources:**
   - Ensure Docker has enough memory (min 512MB)
   - Check disk space
   - Restart Docker daemon

4. **Rebuild Image:**
   ```bash
   docker-compose down
   docker-compose build --no-cache
   docker-compose up
   ```

### Problem: "Permission denied" errors in container

**Solution:**
```bash
# Check file permissions
ls -la src/

# Fix permissions
chmod -R 755 src/

# Rebuild
docker-compose up --build
```

## Tool Execution Errors

### Problem: "Tool execution failed" errors

**Common Causes:**

1. **Invalid Item IDs:**
   - Keep each ID paired with its `target_mailbox` and `folder`; IMAP IDs are folder-scoped, not portable mailbox handles.
   - IMAP draft body/subject and attachment mutations rewrite the message. Use the returned fresh `id`/`message_id` and attachment part IDs, or re-list attachments before the next mutation.
   - If a rewrite cannot identify its replacement, re-list Drafts. Never assume the old ID identifies the new message or retry the mutation blindly.
   - Use search/list tools for current IDs rather than caching them indefinitely.

2. **Permissions Issues:**
   - Verify app has required permissions
   - Check mailbox delegation settings
   - Ensure user has access to requested folders

3. **Rate Limiting:**
   ```
   RateLimitError: Rate limit exceeded
   ```
   - Wait 60 seconds
   - Reduce request frequency
   - Increase RATE_LIMIT_REQUESTS_PER_MINUTE

### Problem: Email sending fails

**Solutions:**

1. **Check Send Permissions:**
   - Verify app can send emails
   - Check SMTP is enabled on mailbox
   - Test with a simple email first

2. **Attachment Issues:**
   - Use canonical `files`. Remote agents (e.g. behind an MCP gateway) share no filesystem with the server: send base64 `file_content` + `file_name`, a `file_url`, or call `stage_outgoing_file` and pass its returned `file_path`. A `File not found on the Email MCP server` error means a caller-side path was used; the response's `hint` lists the alternatives.
   - Smoke-test attachments with `dry_run=true, verify_files=true` on `send_email`, `reply_email` or `forward_email` — nothing is sent.
   - Legacy `attachments`/`inline_attachments` remain supported only on their existing Exchange paths and are deprecated. Graph/IMAP reject them.
   - URL and SmarterMail-storage fetches are capped at **25 MiB per file**. Graph send/reply/forward and draft creators reject more than **3 MiB total raw bytes** in newly supplied `files`; mail-server limits also apply. Large Graph upload sessions are not implemented.
   - `MAX_ATTACHMENT_SIZE` is not currently enforced by these outbound paths. Local-path/base64 sources do not use the remote-fetch cap.
   - Oversized files do not automatically become links. Explicitly put an externally hosted download link in the body; `attach_file_link` generates links only for SmarterMail storage.

3. **Recipient Validation:**
   - Use valid email addresses
   - Check recipient exists in organization
   - Verify no send restrictions

### Problem: Graph failed after creating a draft

A multi-step create/upload/send failure is not necessarily an empty operation.
Inspect the structured `success:false` result: `draft_id` when known, `mailbox`,
`folder`, `stage`, `sent:false`, cleanup information and submission uncertainty.
New drafts are retained by default. Inspect attachments before recovering from
a partial upload, and reuse the retained draft rather than creating duplicates.
Optional `cleanup_on_failure=true` only requests deletion of drafts created by
the current call; it must never delete an existing caller-supplied draft.
If submission is uncertain, reconcile with the provider before resending or
deleting the draft. A missing recovery ID can mean creation failed before an
ID was received; it is not proof that the provider created nothing.

For IMAP draft creation, a post-creation metadata/relisting failure can also
leave a draft. `cleanup_on_failure` defaults to false; true requests targeted
cleanup only when the newly created draft's ID is known. If its ID is unknown,
re-list Drafts rather than guessing. Existing caller drafts are never eligible
for this cleanup option.

### Problem: SMTP accepted mail, but sending reported warnings

Submission is not delivery: `accepted_by_mail_server` does not override
`delivery_confirmed:false`. Inspect `accepted_recipients`,
`refused_recipients` and `partial_failure`; do not retry all recipients after
partial acceptance. Failure to save a Sent copy or clean up a draft also does
not undo submission. A network timeout may leave acceptance unknown; neither
an error nor a remaining draft proves that resending is safe.

### Problem: A tool works for one mailbox but not another

Use `list_mailboxes` and the selected mailbox's capabilities, not merely the
server-wide tool list. Mail and draft tools are provider-unified; core calendar
and contact operations support Graph with separately consented scopes and
iCloud/DAV with CalDAV/CardDAV discovery or configured URLs. Graph free/busy and
meeting suggestions require an organization account, not a personal Microsoft
account. CalDAV/CardDAV supports event/contact CRUD, not organization free/busy
or contact analytics. Tasks/contact analytics remain Exchange-only.
Scheduling/undo require SmarterMail REST.
See [provider scope](API.md#conventions).

Graph event/contact HTTP 403 with a valid mail token can mean missing
`Calendars.ReadWrite` / `Contacts.ReadWrite` consent for the selected app/account.
Refreshing a token does not add permissions. Obtain user/admin consent and
re-authenticate. On refresh the backend requests the account's
`settings.graph_scope`, or by default `offline_access`, `User.Read`, the mail
scopes, `Calendars.ReadWrite` and `Contacts.ReadWrite`. A custom
`settings.graph_scope` must include the calendar/contact scopes too (see the
[scope configuration example](API.md#calendar)). App-only accounts request
`https://graph.microsoft.com/.default`, so the app's application permissions
apply. Never silently modify stored credentials or scopes.
Graph `find_person`
scans only default contacts and reports truncation at its scan limit. A partial
or truncated result is not proof that a person is absent from the directory.

### Problem: Scheduling failed or cancellation did not confirm

Native `schedule_email` supports `files` and saved EWS `draft_id` only for
SmarterMail-backed Exchange. With a draft ID, omit every compose override
(`to`, `cc`, `bcc`, `subject`, `body`, `body_format`); edit first with
`update_draft`. The native Scheduled queue consumes the source draft and
returns a numeric Scheduled UID for cancellation, not an EWS message ID.

After a mutation/submission failure, inspect `draft_id`, `uid`, `folder`,
`stage`, `submission_uncertain` and `recovery_hint`. Files may already have
appended to an existing draft; that source is never deleted as failure cleanup.
If native submission was attempted, check both Drafts and Scheduled before
retrying: the source may already have moved and the operation may have queued.
Do not infer non-delivery from an error or a missing Drafts item.

`cancel_scheduled_email` scans the whole queue and requests removal, not
guaranteed prevention. Observed removal reports `removed_from_queue:true`,
`cancelled:null`, `cancellation_confirmed:false`, `delivery_prevented:null`.
An item absent beforehand may already have sent. A requested soft move may
return no destination IDs, so `moved_to_deleted` remains unknown without
evidence. Native `undo_send` acknowledgment also cannot prove delivery stopped:
the provider may acknowledge even an absent UID. Neither operation is recall,
a portable Graph/IMAP scheduler or an exactly-once delivery guarantee.

### Problem: find_person returns 0 results for GAL search

On Exchange, `find_person` uses multi-strategy GAL search; check query scope,
authentication and impersonation rights when expected people are missing
([Authentication Issues](#authentication-issues) above). On Graph, use
`source="contacts"`: `all` reports contacts-only partial coverage, while an
explicit GAL/history/domain request is unsupported rather than a zero-match
search. A working mail login does not establish calendar/contact permissions.

**Exchange multi-strategy search:**
1. **Exact match** — direct EWS `ResolveNames` SOAP call
2. **Partial match** — wildcard / prefix search
3. **Domain search** — `@domain` → all users at that domain
4. **Fuzzy match** — handles typos and minor variations

**If you still see 0 results despite a person clearly existing in the GAL:**
The GAL search uses `account.protocol.resolve_names()` which returns tuples in the format `(mailbox, contact_info)`. If the code doesn't properly unpack these tuples, results appear empty.

**Verification:**
Check if direct Python code works:
```python
from exchangelib import Account, Credentials

credentials = Credentials(username="user@example.com", password="pass")
account = Account(primary_smtp_address="user@example.com",
                  credentials=credentials, autodiscover=True)

# This should work
results = account.protocol.resolve_names(
    names=["Smith"],
    return_full_contact_data=True
)

# Results are tuples: (mailbox, contact_info)
for result in results:
    mailbox, contact_info = result
    print(f"Found: {mailbox.name} - {mailbox.email_address}")
```

**Solution:**
The `_search_gal()` method in `contact_intelligence_tools.py` has been updated to properly handle tuple format:

```python
# Correct tuple unpacking
if isinstance(result, tuple):
    mailbox = result[0]
    contact_info = result[1] if len(result) > 1 else None

    contact = {
        'name': getattr(mailbox, 'name', ''),
        'email': getattr(mailbox, 'email_address', ''),
        'routing_type': getattr(mailbox, 'routing_type', 'SMTP')
    }

    # Extract additional details from contact_info
    if contact_info:
        if hasattr(contact_info, 'display_name'):
            contact['display_name'] = contact_info.display_name
        # ... more fields
```

**Testing:**
```python
# Test with generic names
find_person(query="Smith", search_scope="gal")
# Should return results from GAL only

# Test with Arabic text (UTF-8)
find_person(query="أحمد", search_scope="gal")
# Should handle Arabic names correctly

# Test email search
find_person(query="john@example.com", search_scope="gal")
# Should find by email address
```

**Verify Scope Isolation:**
```python
# GAL-only search should NOT include email_history
result = find_person(query="Smith", search_scope="gal")
for contact in result['unified_results']:
    assert 'gal' in contact['sources']
    assert 'email_history' not in contact['sources']
```

**Debugging:**
Enable detailed logging to see GAL search flow:
```bash
LOG_LEVEL=DEBUG
```

Look for these log messages:
```
=== GAL Search Start === Query: 'Smith'
GAL returned 5 raw result(s)
  GAL [1] John Smith <john.smith@company.com> @ Example Corp
  GAL [2] Jane Smith <jane.smith@company.com> @ Acme Inc
=== GAL Search Complete === Found 2 contact(s)
```

If you see:
```
GAL search returned 0 results
```

But direct Python code works, then there may be an issue with:
1. Account initialization in the MCP server
2. Permissions on the service account
3. Network connectivity to Exchange server

**Common Mistakes:**
```python
# Wrong - trying to access .mailbox.name on tuple
name = result.mailbox.name  # AttributeError!

# Correct - unpack tuple first
mailbox, contact_info = result
name = mailbox.name  # Works!

# Also correct - check type first
if isinstance(result, tuple):
    mailbox = result[0]
    name = mailbox.name
```

## Performance Issues

### Problem: Slow response times

**Solutions:**

1. **Enable Caching:**
   ```bash
   ENABLE_CACHE=true
   CACHE_TTL=300
   ```

2. **Reduce Result Sets:**
   - Use smaller max_results values
   - Implement pagination
   - Use specific date ranges

3. **Connection Pooling:**
   ```bash
   CONNECTION_POOL_SIZE=20
   ```

4. **Optimize Queries:**
   - Use search filters instead of fetching all items
   - Filter on server-side, not client-side

## Logging and Debugging

### Enable Debug Logging

```bash
LOG_LEVEL=DEBUG
```

### Check Server Logs

```bash
# Docker
docker-compose logs -f

# Local
tail -f logs/ews-mcp.log
```

### Enable Audit Logging

```bash
ENABLE_AUDIT_LOG=true
```

### Test Connection Manually

```python
from src.config import settings
from src.auth import AuthHandler
from src.ews_client import EWSClient

auth = AuthHandler(settings)
client = EWSClient(settings, auth)

# Test connection
print(client.test_connection())

# Test inbox access
print(client.account.inbox.total_count)
```

## Common Error Messages

### "Module 'mcp' not found"

**Solution:**
```bash
pip install mcp
# Or
pip install -r requirements.txt
```

### "exchangelib.errors.UnauthorizedError"

**Solution:**
- Check credentials are correct
- Verify account is not locked
- Check multi-factor authentication isn't blocking access
- Try different auth method

### "SSL Certificate Verification Failed"

**Solution:**
```bash
# For development only - DO NOT use in production
# Edit src/ews_client.py
# SSL verification is disabled by default via NoVerifyHTTPAdapter
```

### "Invalid folder name"

**Valid folder names:**
- inbox
- sent
- drafts
- deleted
- junk

## Docker Build Issues

### Problem: "pip install failed with exit code 1"

**Symptoms:**
```
ERROR: Cannot install pydantic-settings==2.1.0 and pydantic==2.5.3
because these package versions have conflicting dependencies.
```

**Solution:**
This error occurs with older versions of requirements.txt. Ensure you have the latest version:

```bash
# Pull latest changes
git pull origin main

# Verify requirements.txt has correct versions
grep "pydantic" requirements.txt
# Should show:
# pydantic>=2.8.0
# pydantic-settings>=2.5.2
```

**Root Cause:**
- `mcp>=1.0.0` requires `pydantic>=2.8.0` and `pydantic-settings>=2.5.2`
- Older requirements.txt had `pydantic==2.5.3` and `pydantic-settings==2.1.0`
- Fixed in commit `b286d92` and later

### Problem: "Docker build fails on Alpine Linux"

**Symptoms:**
```
ERROR: failed to build: exchangelib dependencies cannot be installed
```

**Solution:**
The Dockerfile has been updated to use Debian slim instead of Alpine:

```dockerfile
# Correct (current version)
FROM python:3.11-slim

# Incorrect (old version)
FROM python:3.11-alpine
```

**If you see this error:**
1. Pull latest code: `git pull origin main`
2. Rebuild without cache: `docker build --no-cache -t ews-mcp-server .`

### Problem: GitHub Actions workflows failing

**Symptoms:**
- "buildx failed with: ERROR: failed to build"
- "Process completed with exit code 1"
- Python tests being canceled

**Solutions:**

1. **For dependency conflicts:**
   - Ensure branch has latest commit with fixed requirements.txt
   - Check commit hash in workflow logs matches latest
   - Old commit: `f68e530` (broken)
   - Fixed commit: `b286d92` or later

2. **For attestation errors:**
   - Workflows have been updated to remove problematic attestation
   - Pull latest `.github/workflows/docker-publish.yml`

3. **For matrix cancellation:**
   - Workflows now have `fail-fast: false`
   - Tests run in non-blocking mode
   - Pull latest `.github/workflows/python-tests.yml`

## Getting Help

1. **Check Logs:** Always check logs first
2. **Enable Debug Mode:** Set LOG_LEVEL=DEBUG
3. **Test Connection:** Use test_connection() method
4. **Isolate Issue:** Test with minimal configuration
5. **Check Documentation:** Review relevant docs sections
6. **Verify Dependencies:** Check you have latest requirements.txt
7. **Check Commit:** Ensure using latest commit (not old PR branch)
8. **Create Issue:** If all else fails, create GitHub issue with:
   - Error message (redact credentials)
   - Configuration (redact secrets)
   - Steps to reproduce
   - Environment details (OS, Python version, Docker version)
   - Git commit hash

## Useful Commands

```bash
# Test Exchange connectivity
curl -v https://outlook.office365.com/EWS/Exchange.asmx

# Check Python version
python --version

# Check Docker version
docker --version
docker-compose --version

# View environment variables (be careful with secrets!)
env | grep EWS

# Test MSAL token acquisition
python -c "from msal import ConfidentialClientApplication; print('MSAL OK')"

# Validate .env file
python -c "from dotenv import load_dotenv; load_dotenv(); import os; print('Email:', os.getenv('EWS_EMAIL'))"
```
