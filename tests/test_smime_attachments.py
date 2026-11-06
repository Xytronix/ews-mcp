"""read_attachment on S/MIME and CMS files.

Two defects are pinned here:

1. read_attachment handed the parent message's rendered body to OpenSSL as
   detached content for every .p7s/.p7m. That broke attached signatures
   (smime.p7m, CAdES contract.pdf.p7m), which carry their own content, and a
   detached signature never verifies against a rendered body, only against
   the exact signed MIME part.
2. An encrypted (EnvelopedData) .p7m was reported as "No certificates found"
   instead of as encrypted.

Fixtures are built at test time with ``cryptography`` and the openssl CLI.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
from exchangelib import FileAttachment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mail_backends.accounts import MailAccount  # noqa: E402
from src.mail_backends.imap_backend import IMAPBackend  # noqa: E402
from src.mail_backends.registry import MailboxRegistry  # noqa: E402
from src.tools import attachment_tools  # noqa: E402
from src.tools.mailbox_tools import ReadAttachmentTool  # noqa: E402

BODY = b"Quarterly figures attached."
# The first part of the multipart/signed entity: what a detached signature
# signs, headers included, in CRLF canonical form.
SIGNED_PART = b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + BODY + b"\r\n"
BOUNDARY = b"sig-boundary-1"

VALID = "valid: the signed content matches the signature"
UNVERIFIABLE = "not verified: detached signature"
ENVELOPED_OID = "1.2.840.113549.1.7.3"
AUTH_ENVELOPED_OID = "1.2.840.113549.1.9.16.1.23"
# ContentInfo { authEnvelopedData, [0] { SEQUENCE { INTEGER 0 } } }: the
# cryptography package has no AuthEnvelopedData builder.
AUTH_ENVELOPED = bytes.fromhex("3014060b2a864886f70d0109100117a0053003020100")


@pytest.fixture(scope="module")
def signer(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Test Signer"),
        x509.NameAttribute(NameOID.EMAIL_ADDRESS, "signer@example.com"),
    ])
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(start)
        .not_valid_after(start + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    folder = tmp_path_factory.mktemp("signer")
    (folder / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    return SimpleNamespace(key=key, cert=cert, folder=folder)


def sign(signer, data: bytes, *options) -> bytes:
    return (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(data)
        .add_signer(signer.cert, signer.key, hashes.SHA256())
        .sign(serialization.Encoding.DER, list(options))
    )


def encrypt(signer, data: bytes) -> bytes:
    return (
        pkcs7.PKCS7EnvelopeBuilder()
        .set_data(data)
        .add_recipient(signer.cert)
        .encrypt(serialization.Encoding.DER, [])
    )


def streamed_signature(signer, data: bytes, *, attached: bool) -> bytes:
    """Indefinite-length BER, as Outlook and other S/MIME clients emit."""
    source = signer.folder / "data"
    source.write_bytes(data)
    cmd = [
        "openssl", "cms", "-sign", "-binary", "-stream", "-outform", "DER",
        "-in", str(source),
        "-signer", str(signer.folder / "cert.pem"),
        "-inkey", str(signer.folder / "key.pem"),
    ]
    if attached:
        cmd.append("-nodetach")
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    assert out[:2] == b"\x30\x80"  # really indefinite length
    return out


def multipart_signed(signature: bytes, signed_part: bytes = SIGNED_PART) -> bytes:
    """A multipart/signed entity: the signed part, then the signature."""
    encoded = base64.encodebytes(signature).replace(b"\n", b"\r\n")
    return (
        b'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; '
        b'micalg=sha-256; boundary="' + BOUNDARY + b'"\r\n'
        b"\r\n"
        b"This is a cryptographically signed message in MIME format.\r\n"
        b"\r\n"
        b"--" + BOUNDARY + b"\r\n"
        + signed_part + b"\r\n"
        b"--" + BOUNDARY + b"\r\n"
        b'Content-Type: application/pkcs7-signature; name="smime.p7s"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="smime.p7s"\r\n'
        b"\r\n"
        + encoded + b"\r\n"
        b"--" + BOUNDARY + b"--\r\n"
    )


def message_headers(subject: bytes) -> bytes:
    return (
        b"From: sender@example.com\r\n"
        b"To: reader@example.com\r\n"
        b"Subject: " + subject + b"\r\n"
        b"MIME-Version: 1.0\r\n"
    )


def signed_message(signature: bytes, signed_part: bytes = SIGNED_PART) -> bytes:
    return message_headers(b"Signed report") + multipart_signed(signature, signed_part)


def pkcs7_mime_message(cms: bytes, smime_type: bytes) -> bytes:
    """A whole message whose body is one smime.p7m (opaque-signed or encrypted)."""
    return (
        message_headers(b"S/MIME report")
        + b"Content-Type: application/pkcs7-mime; smime-type=" + smime_type
        + b'; name="smime.p7m"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="smime.p7m"\r\n'
        b"\r\n"
        + base64.encodebytes(cms).replace(b"\n", b"\r\n")
    )


def minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref,
    )
    return bytes(out)


# ----------------------------------------------------------------- Exchange
class _Client:
    """Enough of EWSClient for the Exchange read_attachment path."""

    def __init__(self):
        self.config = SimpleNamespace(ews_email="reader@example.com")
        self.mailbox_registry = SimpleNamespace(find_account=lambda address: None)
        self.sqlite_cache = None

    def get_account(self, target_mailbox=None):
        return SimpleNamespace()


def read_exchange(monkeypatch, name: str, content: bytes, *, mime_content=None) -> str:
    """read_attachment over EWS for a message whose rendered body is BODY."""
    message = SimpleNamespace(
        attachments=[FileAttachment(name=name, content=content)],
        text_body=BODY.decode(),
        body=f"<p>{BODY.decode()}</p>",
        subject="Signed report",
        mime_content=mime_content,
    )
    monkeypatch.setattr(
        attachment_tools, "find_message_for_account", lambda account, message_id: message
    )
    result = asyncio.run(
        ReadAttachmentTool(_Client()).execute(message_id="MID", attachment_name=name)
    )
    return result["content"]


def test_attached_signature_verifies_and_shows_its_content(monkeypatch, signer):
    text = read_exchange(monkeypatch, "smime.p7m", sign(signer, SIGNED_PART))
    assert VALID in text
    assert "### Signed content (embedded)" in text
    assert BODY.decode() in text
    assert "CN=Test Signer" in text


def test_attached_cades_document_is_extracted(monkeypatch, signer):
    signature = sign(signer, minimal_pdf("Contract terms apply"), pkcs7.PKCS7Options.Binary)
    text = read_exchange(monkeypatch, "contract.pdf.p7m", signature)
    assert VALID in text
    assert "Contract terms apply" in text
    assert "%PDF" not in text


def test_detached_signature_verifies_against_the_signed_mime_part(monkeypatch, signer):
    signature = sign(signer, SIGNED_PART, pkcs7.PKCS7Options.DetachedSignature)
    text = read_exchange(
        monkeypatch, "smime.p7s", signature, mime_content=signed_message(signature)
    )
    assert VALID in text
    assert "### Signed content (detached)" in text
    assert BODY.decode() in text


def test_detached_signature_without_content_is_unverifiable_not_failed(monkeypatch, signer):
    signature = sign(signer, SIGNED_PART, pkcs7.PKCS7Options.DetachedSignature)
    text = read_exchange(monkeypatch, "smime.p7s", signature, mime_content=None)
    assert UNVERIFIABLE in text
    assert "failed" not in text
    assert VALID not in text


def test_detached_signature_over_altered_content_fails(monkeypatch, signer):
    """Guard: the exact-part lookup must not turn a bad signature into a valid one."""
    signature = sign(signer, SIGNED_PART, pkcs7.PKCS7Options.DetachedSignature)
    altered = signed_message(signature, SIGNED_PART.replace(b"Quarterly", b"Annual"))
    text = read_exchange(monkeypatch, "smime.p7s", signature, mime_content=altered)
    assert "failed" in text
    assert VALID not in text


def test_exchange_clear_signed_entity_verifies(monkeypatch, signer):
    """Exchange stores clear-signed mail as smime.p7m holding the multipart/signed entity."""
    signature = sign(signer, SIGNED_PART, pkcs7.PKCS7Options.DetachedSignature)
    text = read_exchange(monkeypatch, "smime.p7m", multipart_signed(signature))
    assert VALID in text
    assert "### Signed content (detached)" in text
    assert BODY.decode() in text


@pytest.mark.parametrize("attached", [True, False], ids=["attached", "detached"])
def test_streamed_ber_signature_verifies(monkeypatch, signer, attached):
    signature = streamed_signature(signer, SIGNED_PART, attached=attached)
    text = read_exchange(
        monkeypatch,
        "smime.p7m" if attached else "smime.p7s",
        signature,
        mime_content=None if attached else signed_message(signature),
    )
    assert VALID in text
    assert BODY.decode() in text


def test_signed_eml_attachment_verifies_its_signature(monkeypatch, signer):
    signature = sign(signer, SIGNED_PART, pkcs7.PKCS7Options.DetachedSignature)
    text = read_exchange(monkeypatch, "forwarded.eml", signed_message(signature))
    assert "Subject: Signed report" in text
    assert VALID in text
    assert "### Signed content (detached)" in text


@pytest.mark.parametrize(
    ("make", "oid"),
    [
        (lambda signer: encrypt(signer, b"Salary review"), ENVELOPED_OID),
        (lambda signer: AUTH_ENVELOPED, AUTH_ENVELOPED_OID),
    ],
    ids=["envelopedData", "authEnvelopedData"],
)
def test_encrypted_p7m_is_reported_as_encrypted(monkeypatch, signer, make, oid):
    text = read_exchange(monkeypatch, "smime.p7m", make(signer))
    assert "encrypted" in text
    assert "private key" in text
    assert oid in text


# ---------------------------------------------------------------------- IMAP
def imap_client(monkeypatch, raw: bytes):
    backend = IMAPBackend(MailAccount(email="reader@icloud.com", provider="icloud", settings={}))
    mime_fetches = []

    def fetch(message_id, folder, writable=False):
        return "7", "42", {"raw": raw, "size": len(raw), "flags": []}

    def get_mime(message_id, folder="inbox"):
        mime_fetches.append(message_id)
        return raw

    monkeypatch.setattr(backend, "_fetch_full", fetch)
    monkeypatch.setattr(backend, "get_mime", get_mime)
    monkeypatch.setattr("src.mail_backends.registry.accounts_from_env", lambda: [])
    client = SimpleNamespace(config=SimpleNamespace(
        ews_email="",
        mail_account_rows=[
            {"email": backend.email, "provider": "icloud", "password": "test-only"},
        ],
    ))
    client.mailbox_registry = MailboxRegistry(client.config, client)
    client.mailbox_registry._backends[backend.email] = backend
    return client, mime_fetches


def read_imap(client, name: str) -> str:
    result = asyncio.run(ReadAttachmentTool(client).execute(
        target_mailbox="reader@icloud.com", message_id="7:42", attachment_name=name,
    ))
    return result["content"]


def test_imap_detached_signature_verifies_against_the_raw_message(monkeypatch, signer):
    signature = sign(signer, SIGNED_PART, pkcs7.PKCS7Options.DetachedSignature)
    client, mime_fetches = imap_client(monkeypatch, signed_message(signature))
    text = read_imap(client, "smime.p7s")
    assert VALID in text
    assert "### Signed content (detached)" in text
    assert BODY.decode() in text
    assert mime_fetches == ["7:42"]


@pytest.mark.parametrize(
    ("smime_type", "expected"),
    [(b"signed-data", BODY.decode()), (b"enveloped-data", "encrypted")],
    ids=["signed-data", "enveloped-data"],
)
def test_imap_attached_and_encrypted_read_without_fetching_mime(
    monkeypatch, signer, smime_type, expected
):
    cms = sign(signer, SIGNED_PART) if smime_type == b"signed-data" else encrypt(signer, b"x")
    client, mime_fetches = imap_client(monkeypatch, pkcs7_mime_message(cms, smime_type))
    text = read_imap(client, "smime.p7m")
    assert expected in text
    assert mime_fetches == []
