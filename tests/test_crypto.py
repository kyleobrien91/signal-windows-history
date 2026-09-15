import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import crypto
from crypto.attachment import decrypt_attachment, inspect_attachment, stream_attachment_range
from crypto.key import _CACHE_MAX, _cache, _cache_lock, _get_cached, get_signal_key


def make_encrypted_attachment(plaintext: bytes, key_b64: str) -> bytes:
    raw_key = base64.b64decode(key_b64)
    aes_key, mac_key = raw_key[:32], raw_key[32:]
    iv = os.urandom(16)
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len]) * pad_len

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()

    tag = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    return iv + ct + tag


class TestCryptoPackage(unittest.TestCase):

    def setUp(self):
        with _cache_lock:
            _cache.clear()
        self.key_b64 = base64.b64encode(os.urandom(64)).decode("utf-8")

    def tearDown(self):
        with _cache_lock:
            _cache.clear()

    def test_decrypt_attachment_roundtrip(self):
        plaintext = b"Signal Crypto Test Data payload " * 10
        enc_data = make_encrypted_attachment(plaintext, self.key_b64)

        decrypted = crypto.decrypt_attachment(enc_data, self.key_b64)
        self.assertEqual(decrypted, plaintext)

    def test_decrypt_attachment_with_size_truncation(self):
        plaintext = b"0123456789ABCDEF"
        enc_data = make_encrypted_attachment(plaintext, self.key_b64)

        decrypted = crypto.decrypt_attachment(enc_data, self.key_b64, size=10)
        self.assertEqual(decrypted, b"0123456789")

    def test_decrypt_attachment_hmac_failure(self):
        plaintext = b"Top Secret"
        enc_data = make_encrypted_attachment(plaintext, self.key_b64)
        corrupted = enc_data[:-1] + b"\x00"

        with self.assertRaises(ValueError) as ctx:
            crypto.decrypt_attachment(corrupted, self.key_b64)
        self.assertIn("HMAC verification failed", str(ctx.exception))

    def test_cache_hit_and_miss_behavior(self):
        plaintext = b"Cached attachment bytes"
        enc_data = make_encrypted_attachment(plaintext, self.key_b64)

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(enc_data)
            enc_path = f.name

        try:
            # 1. First call -> Cache Miss, reads file
            res1 = _get_cached("msg_cache_1", enc_path, self.key_b64, len(plaintext))
            self.assertEqual(res1, plaintext)
            self.assertIn("msg_cache_1", _cache)

            # Delete file to prove second call hits RAM cache
            os.unlink(enc_path)

            # 2. Second call -> Cache Hit
            res2 = _get_cached("msg_cache_1", enc_path, self.key_b64, len(plaintext))
            self.assertEqual(res2, plaintext)
        finally:
            if os.path.exists(enc_path):
                os.unlink(enc_path)

    def test_cache_lru_eviction(self):
        # Fill cache up to _CACHE_MAX
        for i in range(_CACHE_MAX):
            msg_id = f"msg_{i}"
            with _cache_lock:
                _cache[msg_id] = f"data_{i}".encode("utf-8")

        self.assertEqual(len(_cache), _CACHE_MAX)
        self.assertIn("msg_0", _cache)

        # Add one more item
        plaintext = b"Extra data"
        enc_data = make_encrypted_attachment(plaintext, self.key_b64)

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(enc_data)
            enc_path = f.name

        try:
            _get_cached("msg_new", enc_path, self.key_b64, len(plaintext))
            self.assertEqual(len(_cache), _CACHE_MAX)
            self.assertNotIn("msg_0", _cache)
            self.assertIn("msg_new", _cache)
        finally:
            os.unlink(enc_path)

    @patch("crypto.key.dpapi_decrypt")
    def test_get_signal_key_encrypted_key_path(self, mock_dpapi):
        expected_key = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        master_key = os.urandom(32)
        mock_dpapi.return_value = master_key

        local_state = {
            "os_crypt": {
                "encrypted_key": base64.b64encode(b"DPAPI" + b"dummy_encrypted_master_key").decode("utf-8")
            }
        }

        nonce = os.urandom(12)
        encrypted = AESGCM(master_key).encrypt(nonce, expected_key.encode("utf-8"), None)
        encrypted_key = "v10" + (nonce + encrypted).hex()
        config_json = {"encryptedKey": encrypted_key}

        with tempfile.TemporaryDirectory() as tmpdir:
            sig_dir = os.path.join(tmpdir, "Signal")
            os.makedirs(sig_dir)

            with open(os.path.join(sig_dir, "Local State"), "w", encoding="utf-8") as f:
                json.dump(local_state, f)
            with open(os.path.join(sig_dir, "config.json"), "w", encoding="utf-8") as f:
                json.dump(config_json, f)

            with patch.dict(os.environ, {"APPDATA": tmpdir}):
                key = get_signal_key()

        self.assertEqual(key, expected_key)
        mock_dpapi.assert_called_once_with(b"dummy_encrypted_master_key")

    def test_inspect_and_stream_attachment_range(self):
        plaintext = b"Chunk 1 payload " * 100  # 1600 bytes
        enc_data = make_encrypted_attachment(plaintext, self.key_b64)

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(enc_data)
            enc_path = f.name

        try:
            effective_len = inspect_attachment(enc_path, self.key_b64)
            self.assertEqual(effective_len, len(plaintext))

            chunks = list(stream_attachment_range(enc_path, self.key_b64, 0, effective_len - 1))
            self.assertTrue(len(chunks) >= 1)
            self.assertEqual(b"".join(chunks), plaintext)
        finally:
            os.unlink(enc_path)


if __name__ == "__main__":
    unittest.main()
