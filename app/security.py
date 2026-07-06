import base64
import hashlib
import json
import re
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization

from .config import get_settings


password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _fernet() -> Fernet:
    raw = get_settings().master_key.get_secret_value().encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def encrypt_payload(payload: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(payload).encode()).decode()


def decrypt_payload(value: str) -> dict[str, Any]:
    try:
        return json.loads(_fernet().decrypt(value.encode()))
    except (InvalidToken, json.JSONDecodeError) as exc:
        raise ValueError("Credential konnte nicht entschlüsselt werden") from exc


SECRET_PATTERNS = [
    re.compile(r"(?i)(token|password|secret|certificate-key)(\s*[:=]\s*)([^\s]+)"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)(kubeadm join\s+.*?--token\s+)(\S+)"),
]


def redact(text: str, known_secrets: list[str] | None = None) -> str:
    result = text
    for secret in known_secrets or []:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = SECRET_PATTERNS[0].sub(r"\1\2[REDACTED]", result)
    result = SECRET_PATTERNS[1].sub("[REDACTED PRIVATE KEY]", result)
    result = SECRET_PATTERNS[2].sub(r"\1[REDACTED]", result)
    return result


def validate_ssh_keypair(private_key: str, public_key: str) -> None:
    raw = private_key.encode()
    try:
        key = serialization.load_ssh_private_key(raw, password=None)
    except (ValueError, TypeError):
        try:
            key = serialization.load_pem_private_key(raw, password=None)
        except (ValueError, TypeError) as exc:
            raise ValueError("Privater SSH-Schlüssel ist ungültig oder passwortgeschützt") from exc
    actual = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    expected = " ".join(public_key.strip().split()[:2])
    if actual != expected:
        raise ValueError("Public und Private SSH Key gehören nicht zusammen")
