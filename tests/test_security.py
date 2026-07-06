import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.security import decrypt_payload, encrypt_payload, redact, validate_ssh_keypair


def test_encrypted_payload_roundtrip_and_not_plaintext():
    encrypted = encrypt_payload({"api_token": "very-secret"})
    assert "very-secret" not in encrypted
    assert decrypt_payload(encrypted) == {"api_token": "very-secret"}


def test_log_redaction():
    output = redact("token=abc password: xyz\n-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----", ["abc"])
    assert "abc" not in output
    assert "xyz" not in output
    assert "BEGIN OPENSSH" not in output


def test_ssh_keypair_validation():
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    validate_ssh_keypair(private, public)
    with pytest.raises(ValueError, match="gehören nicht zusammen"):
        validate_ssh_keypair(private, "ssh-ed25519 AAAAinvalid")
