#!/usr/bin/env python3
"""player.media - Neutral shared media streaming and HTTP Range handling.

Provides shared Range header parsing and serve_encrypted_media() implementation
for player/server.py and signal_player.py without importing HTTP server modules or creating circular dependencies.
"""

import os
import sys
from typing import Optional, Tuple, Union

from crypto.attachment import inspect_attachment, stream_attachment_range


def parse_range_header(range_header: Optional[str], total_length: int) -> Tuple[Union[int, str], Optional[Tuple[int, int]]]:
    """Parses single-byte HTTP Range header according to spec.

    Args:
        range_header: Raw 'Range' header value or None.
        total_length: Validated total plaintext byte length.

    Returns:
        Tuple of (status, bounds):
          - (200, None): Full response requested (no Range header).
          - (206, (start, end)): Valid single range spec with inclusive bounds [start, end].
          - (400, None): Malformed Range header or syntax error.
          - (416, None): Range Not Satisfiable (start >= total_length or invalid offset).
    """
    if not range_header:
        return 200, None

    range_header = range_header.strip()
    if not range_header.startswith("bytes="):
        return 400, None

    spec = range_header[6:].strip()
    if not spec or "," in spec:  # Multiple ranges not supported
        return 400, None

    parts = spec.split("-")
    if len(parts) != 2:
        return 400, None

    s_str, e_str = parts[0].strip(), parts[1].strip()

    if total_length == 0:
        return 416, None

    if s_str == "" and e_str != "":
        # Suffix range: -length
        try:
            length = int(e_str)
            if length <= 0:
                return 400, None
            start = max(0, total_length - length)
            end = total_length - 1
            return 206, (start, end)
        except ValueError:
            return 400, None

    elif s_str != "" and e_str == "":
        # Open-ended range: start-
        try:
            start = int(s_str)
            if start < 0:
                return 400, None
            if start >= total_length:
                return 416, None
            end = total_length - 1
            return 206, (start, end)
        except ValueError:
            return 400, None

    elif s_str != "" and e_str != "":
        # Range: start-end
        try:
            start = int(s_str)
            end = int(e_str)
            if start < 0 or end < 0 or start > end:
                return 400, None
            if start >= total_length:
                return 416, None
            end = min(end, total_length - 1)
            return 206, (start, end)
        except ValueError:
            return 400, None

    return 400, None


def serve_encrypted_media(
    handler,
    enc_path: str,
    local_key_b64: str,
    declared_size: Optional[int],
    content_type: str,
):
    """Handles encrypted media request with Pass 1 authentication before headers and streaming Pass 2.

    Args:
        handler: BaseHTTPRequestHandler instance.
        enc_path: Path to encrypted file on disk.
        local_key_b64: Base64-encoded local key.
        declared_size: Optional declared attachment size.
        content_type: Content-Type header value.
    """
    file_id = os.path.basename(enc_path) or "unknown_attachment"

    if not os.path.exists(enc_path):
        sys.stderr.write(f"[Media Stream Error] Attachment file missing: {file_id}\n")
        handler.send_error(404, "Encrypted file missing from attachments")
        return

    # Pass 1: O(1) MAC verification & structure check BEFORE sending success headers
    try:
        total_length = inspect_attachment(enc_path, local_key_b64, declared_size)
    except ValueError as e:
        sys.stderr.write(f"[Media Stream Error] Decryption/validation error for {file_id}: {e}\n")
        handler.send_error(400, f"Decryption/validation error: {e}")
        return
    except Exception as e:
        sys.stderr.write(f"[Media Stream Error] Server error inspecting {file_id}: {type(e).__name__}\n")
        handler.send_error(500, f"Internal server error: {e}")
        return

    range_header = handler.headers.get("Range")
    status, bounds = parse_range_header(range_header, total_length)

    if status == 400:
        handler.send_error(400, "Bad Range header")
        return
    elif status == 416:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{total_length}")
        handler.send_header("Content-Length", "0")
        handler.send_header("Accept-Ranges", "bytes")
        handler.end_headers()
        return

    if status == 200:
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(total_length))
        handler.send_header("Accept-Ranges", "bytes")
        handler.end_headers()

        if total_length > 0:
            try:
                for chunk in stream_attachment_range(enc_path, local_key_b64, 0, total_length - 1, declared_size):
                    handler.wfile.write(chunk)
            except Exception as e:
                handler.close_connection = True
                sys.stderr.write(f"[Media Stream Error] Full stream interrupted for {file_id}: {type(e).__name__}\n")
                raise

    elif status == 206:
        start, end = bounds
        range_length = max(0, end - start + 1)

        handler.send_response(206)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Range", f"bytes {start}-{end}/{total_length}")
        handler.send_header("Content-Length", str(range_length))
        handler.send_header("Accept-Ranges", "bytes")
        handler.end_headers()

        if range_length > 0:
            try:
                for chunk in stream_attachment_range(enc_path, local_key_b64, start, end, declared_size):
                    handler.wfile.write(chunk)
            except Exception as e:
                handler.close_connection = True
                sys.stderr.write(f"[Media Stream Error] Range stream interrupted for {file_id}: {type(e).__name__}\n")
                raise
