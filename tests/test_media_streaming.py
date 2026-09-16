#!/usr/bin/env python3
import base64
import hashlib
import hmac
import io
import os
import tempfile
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from crypto.attachment import inspect_attachment, stream_attachment_range, decrypt_attachment
import crypto
from player.media import parse_range_header, serve_encrypted_media
import player.server as player_server


def make_encrypted_attachment(plaintext: bytes, key_b64: str) -> bytes:
    """Helper to create a valid Signal encrypted attachment payload."""
    raw_key = base64.b64decode(key_b64)
    aes_key = raw_key[:32]
    mac_key = raw_key[32:]

    iv = os.urandom(16)
    # PKCS#7 padding
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len]) * pad_len

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()

    mac_input = iv + ct
    tag = hmac.new(mac_key, mac_input, hashlib.sha256).digest()

    return iv + ct + tag


class MockHTTPHandler:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.response_code = None
        self.response_headers = {}
        self.wfile = io.BytesIO()
        self.error_code = None
        self.error_message = None
        self.close_connection = False

    def send_response(self, code, message=None):
        self.response_code = code

    def send_header(self, keyword, value):
        self.response_headers[keyword] = str(value)

    def end_headers(self):
        pass

    def send_error(self, code, message=None, explain=None):
        self.error_code = code
        self.error_message = message


@pytest.fixture
def test_key():
    return base64.b64encode(os.urandom(64)).decode("utf-8")


def test_streaming_parity_with_decrypt_attachment(test_key):
    plaintext = b"Hello, World! " * 500  # 7000 bytes
    enc_data = make_encrypted_attachment(plaintext, test_key)

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(enc_data)
        enc_path = f.name

    try:
        validated_len = inspect_attachment(enc_path, test_key)
        assert validated_len == len(plaintext)

        streamed = b"".join(stream_attachment_range(enc_path, test_key, 0, validated_len - 1))
        legacy_decrypted = decrypt_attachment(enc_data, test_key)

        assert streamed == plaintext
        assert streamed == legacy_decrypted
    finally:
        os.unlink(enc_path)


def test_hmac_and_structure_validation_failures(test_key):
    plaintext = b"Secret media content"
    enc_data = make_encrypted_attachment(plaintext, test_key)

    # Corrupt MAC
    corrupt_mac = enc_data[:-1] + b"\x00"
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(corrupt_mac)
        enc_path = f.name

    try:
        with pytest.raises(ValueError, match="HMAC verification failed"):
            inspect_attachment(enc_path, test_key)
    finally:
        os.unlink(enc_path)

    # Short file (< 64 bytes)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(enc_data[:30])
        enc_path = f.name

    try:
        with pytest.raises(ValueError, match="Invalid attachment file structure"):
            inspect_attachment(enc_path, test_key)
    finally:
        os.unlink(enc_path)

    # Bad padding
    raw_key = base64.b64decode(test_key)
    aes_key, mac_key = raw_key[:32], raw_key[32:]
    iv = os.urandom(16)
    bad_padded = plaintext + b"\x99" * 12  # 20 bytes total = 32 bytes (2 blocks) with invalid padding byte \x99
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(bad_padded) + encryptor.finalize()
    tag = hmac.new(mac_key, iv + ct, hashlib.sha256).digest()
    bad_pad_enc = iv + ct + tag

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(bad_pad_enc)
        enc_path = f.name

    try:
        with pytest.raises(ValueError, match="Invalid PKCS#7 padding"):
            inspect_attachment(enc_path, test_key)
    finally:
        os.unlink(enc_path)


def test_declared_size_validation(test_key):
    plaintext = b"0123456789ABCDEF"  # 16 bytes
    enc_data = make_encrypted_attachment(plaintext, test_key)

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(enc_data)
        enc_path = f.name

    try:
        # declared_size == plaintext length
        assert inspect_attachment(enc_path, test_key, declared_size=16) == 16
        # declared_size < plaintext length
        assert inspect_attachment(enc_path, test_key, declared_size=10) == 10
        # declared_size > plaintext length -> error
        with pytest.raises(ValueError, match="declared_size exceeds decrypted plaintext length"):
            inspect_attachment(enc_path, test_key, declared_size=20)
        # negative declared_size -> error
        with pytest.raises(ValueError, match="Invalid declared_size"):
            inspect_attachment(enc_path, test_key, declared_size=-5)
    finally:
        os.unlink(enc_path)


def test_cbc_unaligned_random_access_ranges(test_key):
    plaintext = bytes([i % 256 for i in range(1000)])  # 1000 bytes
    enc_data = make_encrypted_attachment(plaintext, test_key)

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(enc_data)
        enc_path = f.name

    try:
        inspect_attachment(enc_path, test_key)

        # Unaligned start and unaligned end across multiple block boundaries
        start, end = 17, 853
        range_bytes = b"".join(stream_attachment_range(enc_path, test_key, start, end))
        assert len(range_bytes) == (end - start + 1)
        assert range_bytes == plaintext[start : end + 1]

        # Single byte request
        start, end = 50, 50
        range_bytes = b"".join(stream_attachment_range(enc_path, test_key, start, end))
        assert range_bytes == plaintext[50:51]
    finally:
        os.unlink(enc_path)


def test_parse_range_header():
    total = 1000

    # No range
    assert parse_range_header(None, total) == (200, None)

    # Valid start-end
    assert parse_range_header("bytes=0-499", total) == (206, (0, 499))
    assert parse_range_header("bytes=500-999", total) == (206, (500, 999))

    # Open-ended start-
    assert parse_range_header("bytes=500-", total) == (206, (500, 999))

    # Suffix -length
    assert parse_range_header("bytes=-100", total) == (206, (900, 999))

    # Out of bounds
    assert parse_range_header("bytes=1000-1500", total) == (416, None)

    # Suffix -0 (length 0) is malformed/unsatisfiable
    assert parse_range_header("bytes=-0", total) == (400, None)

    # Malformed syntax / inverted bounds
    assert parse_range_header("bytes=500-400", total) == (400, None)
    assert parse_range_header("bytes=abc-def", total) == (400, None)
    assert parse_range_header("bytes=0-100,200-300", total) == (400, None)

    # Zero-length total
    assert parse_range_header("bytes=0-10", 0) == (416, None)


def test_serve_encrypted_media_http_responses(test_key):
    plaintext = b"0123456789" * 100  # 1000 bytes
    enc_data = make_encrypted_attachment(plaintext, test_key)

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(enc_data)
        enc_path = f.name

    try:
        # 1. 200 OK Full Response
        handler = MockHTTPHandler()
        serve_encrypted_media(handler, enc_path, test_key, None, "video/mp4")
        assert handler.response_code == 200
        assert handler.response_headers["Content-Length"] == "1000"
        assert handler.response_headers["Content-Type"] == "video/mp4"
        assert handler.wfile.getvalue() == plaintext

        # 2. 206 Partial Content
        handler = MockHTTPHandler({"Range": "bytes=100-199"})
        serve_encrypted_media(handler, enc_path, test_key, None, "video/mp4")
        assert handler.response_code == 206
        assert handler.response_headers["Content-Range"] == "bytes 100-199/1000"
        assert handler.response_headers["Content-Length"] == "100"
        assert handler.wfile.getvalue() == plaintext[100:200]

        # 3. 416 Range Not Satisfiable
        handler = MockHTTPHandler({"Range": "bytes=2000-3000"})
        serve_encrypted_media(handler, enc_path, test_key, None, "video/mp4")
        assert handler.response_code == 416
        assert handler.response_headers["Content-Range"] == "bytes */1000"

        # 4. 400 Bad Request (Malformed Range)
        handler = MockHTTPHandler({"Range": "bytes=500-100"})
        serve_encrypted_media(handler, enc_path, test_key, None, "video/mp4")
        assert handler.error_code == 400

        # 5. 404 Missing File
        handler = MockHTTPHandler()
        serve_encrypted_media(handler, enc_path + ".nonexistent", test_key, None, "video/mp4")
        assert handler.error_code == 404
    finally:
        os.unlink(enc_path)


def test_cache_non_insertion(test_key):
    plaintext = b"Cache test content"
    enc_data = make_encrypted_attachment(plaintext, test_key)

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(enc_data)
        enc_path = f.name

    try:
        initial_cache_len = len(crypto._cache)
        handler = MockHTTPHandler()
        serve_encrypted_media(handler, enc_path, test_key, None, "video/mp4")
        assert handler.response_code == 200

        # Cache must remain unchanged (streaming bypasses _cache)
        assert len(crypto._cache) == initial_cache_len
    finally:
        os.unlink(enc_path)


def test_player_server_handler(test_key):
    plaintext = b"Media for player test"
    enc_data = make_encrypted_attachment(plaintext, test_key)

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(enc_data)
        enc_path = f.name

    try:
        msg_id = "test_msg_123"
        media_entry = (enc_path, test_key, len(plaintext), "video/mp4")

        # Set up lookup map in player.server
        with player_server._lookup_lock:
            player_server._media_lookup[msg_id] = media_entry

        # Invoke player_server handler _stream
        srv_handler = MockHTTPHandler()

        player_server._attach_root = ""
        player_server._Handler._stream(srv_handler, msg_id)
        assert srv_handler.response_code == 200
        assert srv_handler.wfile.getvalue() == plaintext
    finally:
        os.unlink(enc_path)


def make_test_video_bytes() -> bytes:
    import av
    buf = io.BytesIO()
    container = av.open(buf, mode='w', format='mp4')
    stream = container.add_stream('h264', rate=30)
    stream.width = 320
    stream.height = 180
    stream.pix_fmt = 'yuv420p'
    for i in range(5):
        frame = av.VideoFrame(320, 180, 'yuv420p')
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return buf.getvalue()


def test_derivative_endpoint_security_and_headers(test_key):
    plaintext = make_test_video_bytes()
    enc_data = make_encrypted_attachment(plaintext, test_key)

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(enc_data)
        enc_path = f.name

    try:
        msg_id = "test_deriv_msg_456"
        media_entry = (enc_path, test_key, len(plaintext), "video/mp4")

        with player_server._lookup_lock:
            player_server._media_lookup[msg_id] = media_entry

        player_server._attach_root = ""

        # Initialize cache
        from crypto.cache import DerivedMediaCache
        tmp_cache_dir = tempfile.mkdtemp()
        cache = DerivedMediaCache(cache_dir=tmp_cache_dir)
        player_server._Handler._global_cache = cache

        poster_params = {'width': 320, 'height': 180, 'format': 'webp', 'quality': 80}
        valid_key = cache.derive_cache_key(msg_id, 'poster', 1, poster_params)

        # 1. Valid derivative request
        qs = {
            'id': [msg_id],
            'type': ['poster'],
            'w': ['320'],
            'h': ['180'],
            'v': ['1'],
            'fmt': ['webp'],
            'q': ['80'],
        }
        handler = MockHTTPHandler()
        player_server._Handler._serve_derivative(handler, valid_key, qs)

        assert handler.response_code == 200
        assert handler.response_headers["Content-Type"] == "image/webp"
        assert handler.response_headers["Cache-Control"] == "private, no-store, no-cache, must-revalidate"
        assert len(handler.wfile.getvalue()) > 0

        # 2. Key mismatch request -> 400 Bad Request
        bad_qs = dict(qs, w=['640'])  # Width mismatch
        handler = MockHTTPHandler()
        player_server._Handler._serve_derivative(handler, valid_key, bad_qs)
        assert handler.error_code == 400

        # 3. Missing/Unauthorized media_id -> 404 Not Found
        unauth_qs = dict(qs, id=['unauthorized_msg'])
        handler = MockHTTPHandler()
        player_server._Handler._serve_derivative(handler, valid_key, unauth_qs)
        assert handler.error_code == 404

        # 4. Out-of-bounds parameter formatting -> HTTP 400
        bad_format_qs = dict(qs, fmt=['invalid_format'])
        handler = MockHTTPHandler()
        player_server._Handler._serve_derivative(handler, valid_key, bad_format_qs)
        assert handler.error_code == 400
    finally:
        os.unlink(enc_path)


def test_server_media_lookup_lru_bounds():
    # Insert 1500 lookup entries into player_server
    player_server._media_lookup.clear()
    lookup_dict = {f"item_{i}": (f"path_{i}", "key", 100, "video/mp4") for i in range(1500)}
    player_server._update_media_lookup(lookup_dict)

    # Size must be strictly bounded by _LOOKUP_MAX (1000)
    assert len(player_server._media_lookup) == player_server._LOOKUP_MAX
    # Oldest entries (item_0 ... item_499) must have been evicted
    assert "item_0" not in player_server._media_lookup
    assert "item_1499" in player_server._media_lookup
