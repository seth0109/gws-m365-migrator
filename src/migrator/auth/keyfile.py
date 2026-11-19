"""The private-key file behind `certificate_path`.

Certificate auth signs a client assertion with the app's RSA *private key*; the
certificate itself (the public half) is what gets uploaded to the app
registration. The config field is named `certificate_path`, so the file people
point it at is very often the certificate, or the .pfx it was exported in. MSAL
does not look at the key until the first token request, where every such
mistake surfaces as PyJWT's `InvalidKeyError: Could not parse the provided
public key.` — which names neither the file nor the problem.

`read_private_key` parses the file up front (no network) and says what it is.
"""
from __future__ import annotations

import re
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

_PEM_LABEL = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----")
_NEED = "certificate_path must be the PEM private key (BEGIN PRIVATE KEY)"
_FROM_PFX = "openssl pkcs12 -in <file.pfx> -nocerts -nodes -out key.pem"


class KeyFileError(ValueError):
    """`certificate_path` does not hold a usable PEM RSA private key."""


def _describe(data: bytes) -> str:
    """Why `data` is not a PEM private key, and what to do about it."""
    if not data.strip():
        return "is empty"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"-\x00-\x00-\x00" in data[:64]:
        return "is UTF-16 text (PowerShell `>` / Out-File writes that); re-save it as UTF-8"
    labels = [m.decode("ascii") for m in _PEM_LABEL.findall(data)]
    if not labels:
        return (
            "is not PEM text; it looks like a binary .pfx/.p12 or DER file. "
            f"Extract the key from a PFX with: {_FROM_PFX}"
        )
    if "OPENSSH PRIVATE KEY" in labels:
        return (
            "is an OpenSSH-format key; convert a copy to PEM with: "
            "ssh-keygen -p -m PEM -f <copy>"
        )
    if any(label.endswith("PRIVATE KEY") for label in labels):
        return "has a private-key block that does not parse (truncated or altered when copied?)"
    if "CERTIFICATE" in labels:
        return (
            "holds only the certificate, which is the public half uploaded to the app "
            "registration. Point certificate_path at the private key created with it: "
            f"the -keyout file from openssl, or from a PFX: {_FROM_PFX}"
        )
    return f"holds {', '.join(labels)} but no private key"


def read_private_key(path: Path) -> bytes:
    """Return the PEM bytes of the RSA private key at `path`.

    Raises KeyFileError naming the file and what it actually contains. A PEM
    that carries the certificate alongside the key is fine. A missing file is
    left to raise its own FileNotFoundError."""
    data = path.read_bytes()
    try:
        key = load_pem_private_key(data, password=None)
    except TypeError as exc:  # "Password was not given but private key is encrypted"
        raise KeyFileError(
            f"certificate_path '{path}' is a passphrase-protected private key; write an "
            "unencrypted copy with: openssl pkey -in <file> -out key.pem"
        ) from exc
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise KeyFileError(f"certificate_path '{path}' {_describe(data)}. {_NEED}.") from exc
    if not isinstance(key, RSAPrivateKey):
        raise KeyFileError(
            f"certificate_path '{path}' holds a non-RSA key ({type(key).__name__}); "
            "Microsoft Entra certificate auth needs an RSA key."
        )
    return data
