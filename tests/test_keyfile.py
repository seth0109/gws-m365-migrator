"""`certificate_path` must hold the app's PEM RSA private key.

The field name invites pointing it at the certificate (the public half) or at
the .pfx it was exported in. MSAL accepts any bytes at construction and only
fails at the first token request, with PyJWT's `InvalidKeyError: Could not
parse the provided public key.` — so the key file is parsed up front and the
error says which file it is and what it actually contains.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from migrator.auth import ms_auth
from migrator.auth.keyfile import KeyFileError, read_private_key
from migrator.auth.ms_auth import MSTokenProvider
from migrator.config import Config
from migrator.configgen import ConfigGenError, CredentialSet, check_key_files

_TP = "AABBCCDDEEFF00112233445566778899AABBCCDD"
_KEY = rsa.generate_private_key(65537, 2048)  # throwaway, generated once per run
_NO_PW = ser.NoEncryption()
_PW = ser.BestAvailableEncryption(b"pw")


def _self_signed() -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "migrator-test")])
    now = datetime.datetime.now(datetime.UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(_KEY.public_key())
        .serial_number(1).not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(_KEY, hashes.SHA256())
    )


_CERT = _self_signed()
_PKCS8 = _KEY.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, _NO_PW)
_CERT_PEM = _CERT.public_bytes(ser.Encoding.PEM)

_GOOD: dict[str, bytes] = {
    "pkcs8 (openssl -keyout)": _PKCS8,
    "pkcs1 RSA PRIVATE KEY": _KEY.private_bytes(
        ser.Encoding.PEM, ser.PrivateFormat.TraditionalOpenSSL, _NO_PW
    ),
    "key then certificate": _PKCS8 + _CERT_PEM,
    "certificate then key": _CERT_PEM + _PKCS8,
    "crlf line endings": _PKCS8.replace(b"\n", b"\r\n"),
    "utf-8 bom": b"\xef\xbb\xbf" + _PKCS8,
}

# What ends up in certificate_path instead of the key → the phrase that names it.
_BAD: dict[str, tuple[bytes, str]] = {
    "certificate only": (_CERT_PEM, "holds only the certificate"),
    "certificate, DER (.cer)": (_CERT.public_bytes(ser.Encoding.DER), "is not PEM text"),
    "pfx": (
        pkcs12.serialize_key_and_certificates(b"migrator", _KEY, _CERT, None, _PW),
        "openssl pkcs12 -in",
    ),
    "passphrase-protected key": (
        _KEY.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, _PW),
        "passphrase-protected",
    ),
    "openssh-format key": (
        _KEY.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, _NO_PW),
        "OpenSSH-format",
    ),
    "key, DER": (
        _KEY.private_bytes(ser.Encoding.DER, ser.PrivateFormat.PKCS8, _NO_PW),
        "is not PEM text",
    ),
    "utf-16 text": (_PKCS8.decode("ascii").encode("utf-16"), "UTF-16"),
    "truncated key": (_PKCS8[: len(_PKCS8) // 2], "does not parse"),
    "public key": (
        _KEY.public_key().public_bytes(
            ser.Encoding.PEM, ser.PublicFormat.SubjectPublicKeyInfo
        ),
        "holds PUBLIC KEY but no private key",
    ),
    "ec key": (
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            ser.Encoding.PEM, ser.PrivateFormat.PKCS8, _NO_PW
        ),
        "needs an RSA key",
    ),
    "empty file": (b"", "is empty"),
}


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ── read_private_key ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", list(_GOOD))
def test_usable_key_files_are_returned_unchanged(tmp_path: Path, case: str) -> None:
    path = _write(tmp_path, "key.pem", _GOOD[case])
    data = read_private_key(path)
    assert data == _GOOD[case]
    # The consumer is PyJWT's RS256 signer (via MSAL): whatever passes must sign.
    assert jwt.encode({"iss": "app"}, data, algorithm="RS256")


@pytest.mark.parametrize("case", list(_BAD))
def test_unusable_key_file_is_named_and_described(tmp_path: Path, case: str) -> None:
    data, phrase = _BAD[case]
    path = _write(tmp_path, "ms-cert.pem", data)
    with pytest.raises(KeyFileError) as err:
        read_private_key(path)
    message = str(err.value)
    assert message.startswith(f"certificate_path '{path}'")
    assert phrase in message


def test_missing_key_file_says_where_it_looked_and_what_is_there(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    creds.mkdir()
    _write(creds, "ms-key-src.pem", _PKCS8)
    _write(creds, "ms-key-dst.pem", _GOOD["pkcs1 RSA PRIVATE KEY"])
    _write(creds, "ms-cert-src.crt", _CERT_PEM)          # not a key: must not be offered
    _write(creds, "id_rsa", _BAD["openssh-format key"][0])  # unusable format: likewise
    missing = creds / "ms-cert-src.pem"

    with pytest.raises(KeyFileError) as err:
        read_private_key(missing)
    message = str(err.value)
    assert message.startswith(f"certificate_path '{missing}' does not exist")
    assert str(missing.resolve()) in message  # the absolute path that was tried
    assert message.endswith(f"PEM private keys in '{creds}': ms-key-dst.pem, ms-key-src.pem.")


def test_missing_key_file_in_a_missing_folder_points_at_the_folder(tmp_path: Path) -> None:
    # The usual cause: migrator was run from a directory other than the one the
    # config's relative paths were written for.
    with pytest.raises(KeyFileError, match="does not exist or cannot be read"):
        read_private_key(tmp_path / "credentials" / "ms-key.pem")


def test_certificate_error_names_the_key_sitting_next_to_it(tmp_path: Path) -> None:
    cert = _write(tmp_path, "ms-cert-src.pem", _CERT_PEM)
    _write(tmp_path, "ms-key-src.pem", _PKCS8)
    with pytest.raises(KeyFileError) as err:
        read_private_key(cert)
    assert "holds only the certificate" in str(err.value)
    assert str(err.value).endswith(f"PEM private keys in '{tmp_path}': ms-key-src.pem.")


def test_folder_without_any_key_says_so(tmp_path: Path) -> None:
    cert = _write(tmp_path, "ms-cert-src.pem", _CERT_PEM)
    with pytest.raises(KeyFileError, match="There is no PEM private key in"):
        read_private_key(cert)


# ── token provider: fail before MSAL, say which tenant block ─────────────────


def _two_tenant_config(tmp_path: Path, source_key: Path, dest_key: Path) -> Config:
    def block(key: Path, cache: str, **extra: Any) -> dict[str, Any]:
        return {
            "tenant_id": "t", "client_id": "c", "certificate_path": str(key),
            "certificate_thumbprint": _TP, "token_cache_file": str(tmp_path / cache), **extra,
        }

    return Config.model_validate({
        "source": block(source_key, "src-cache.json", type="microsoft365"),
        "destination": block(dest_key, "dst-cache.json", cloud="gcc_high"),
        "users": [{"source_id": "a@src", "dest_id": "a@dst"}],
    })


@pytest.fixture
def msal_apps(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    built: list[dict[str, Any]] = []

    def _fake_app(client_id: str, **kwargs: Any) -> object:
        built.append(kwargs)
        return object()

    monkeypatch.setattr(ms_auth.msal, "ConfidentialClientApplication", _fake_app)
    return built


def test_certificate_in_place_of_the_source_key_fails_before_msal(
    tmp_path: Path, msal_apps: list[dict[str, Any]]
) -> None:
    cert = _write(tmp_path, "source-cert.pem", _CERT_PEM)
    key = _write(tmp_path, "dest-key.pem", _PKCS8)
    cfg = _two_tenant_config(tmp_path, source_key=cert, dest_key=key)
    assert cfg.source.type == "microsoft365"

    with pytest.raises(KeyFileError) as err:
        MSTokenProvider.from_config(cfg.source)
    message = str(err.value)
    assert message.startswith(f"source.certificate_path '{cert}'")
    assert "holds only the certificate" in message
    assert msal_apps == []  # no MSAL app, so no authority discovery over the network

    MSTokenProvider.from_config(cfg.destination)  # the good key still builds
    assert msal_apps[0]["client_credential"] == {"thumbprint": _TP, "private_key": _PKCS8}
    assert msal_apps[0]["authority"] == "https://login.microsoftonline.us/t"


def test_bad_destination_key_names_the_destination_block(
    tmp_path: Path, msal_apps: list[dict[str, Any]]
) -> None:
    key = _write(tmp_path, "source-key.pem", _PKCS8)
    pfx = _write(tmp_path, "dest.pfx", _BAD["pfx"][0])
    cfg = _two_tenant_config(tmp_path, source_key=key, dest_key=pfx)
    with pytest.raises(KeyFileError) as err:
        MSTokenProvider.from_config(cfg.destination)
    assert str(err.value).startswith(f"destination.certificate_path '{pfx}'")
    assert msal_apps == []


# ── init-config: catch it while scaffolding ──────────────────────────────────


def test_check_key_files_accepts_real_keys(tmp_path: Path) -> None:
    key = _write(tmp_path, "dest-key.pem", _PKCS8)
    src = _write(tmp_path, "source-key.pem", _GOOD["key then certificate"])
    check_key_files(CredentialSet(dest_cert=key, source_cert=src))
    check_key_files(CredentialSet(dest_cert=key))  # non-M365 source: no second key


@pytest.mark.parametrize(
    ("bad_role", "flag"), [("dest_cert", "--cert"), ("source_cert", "--source-cert")]
)
def test_check_key_files_rejects_a_certificate_naming_the_flag(
    tmp_path: Path, bad_role: str, flag: str
) -> None:
    paths = {
        "dest_cert": _write(tmp_path, "dest-key.pem", _PKCS8),
        "source_cert": _write(tmp_path, "source-key.pem", _PKCS8),
    }
    paths[bad_role] = _write(tmp_path, "the-cert.pem", _CERT_PEM)
    with pytest.raises(ConfigGenError) as err:
        check_key_files(CredentialSet(**paths))
    message = str(err.value)
    assert "the-cert.pem" in message and "holds only the certificate" in message
    assert message.endswith(f"(pass {flag} to choose another file)")


def test_check_key_files_reports_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigGenError, match="does not exist"):
        check_key_files(CredentialSet(dest_cert=tmp_path / "missing.pem"))


# ── smoke-test: a key-file mistake is one readable line, not a traceback ─────


def test_smoke_test_prints_the_key_problem_without_a_traceback(tmp_path: Path) -> None:
    import yaml
    from typer.testing import CliRunner

    from migrator.cli import app

    creds = tmp_path / "credentials"
    creds.mkdir()
    _write(creds, "ms-key-src.pem", _PKCS8)
    block = {"tenant_id": "t", "client_id": "c", "certificate_thumbprint": _TP}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "state_db": str(tmp_path / "state.db"),
        "log_file": None,
        "source": {
            **block, "type": "microsoft365",
            "certificate_path": str(creds / "ms-cert-src.pem"),  # no such file
            "token_cache_file": str(tmp_path / "src-cache.json"),
        },
        "destination": {
            **block, "cloud": "gcc_high",
            "certificate_path": str(creds / "ms-key-src.pem"),
            "token_cache_file": str(tmp_path / "dst-cache.json"),
        },
        "users": [{"source_id": "a@src", "dest_id": "a@dst"}],
    }), encoding="utf-8")

    result = CliRunner().invoke(app, ["smoke-test", "--config", str(config)])
    assert result.exit_code == 1
    text = " ".join(result.output.split())  # undo the console's line wrapping
    assert "source.certificate_path" in text and "does not exist" in text
    assert "ms-key-src.pem." in text  # the key that is there is offered
    assert "Traceback" not in text and "Smoke test passed" not in text
