import base64
import json

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config.settings import get_settings

settings = get_settings()


def _get_key() -> bytes:
    raw = settings.ENCRYPTION_KEY
    decoded = base64.urlsafe_b64decode(raw + "==")
    return decoded[:32]


def encrypt_json(data: dict) -> str:
    import os
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, json.dumps(data).encode(), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_json(encrypted: str) -> dict:
    key = _get_key()
    aesgcm = AESGCM(key)
    raw = base64.urlsafe_b64decode(encrypted)
    nonce, ciphertext = raw[:12], raw[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode())


def encrypt_str(value: str) -> str:
    return encrypt_json({"v": value})


def decrypt_str(encrypted: str) -> str:
    return decrypt_json(encrypted)["v"]
