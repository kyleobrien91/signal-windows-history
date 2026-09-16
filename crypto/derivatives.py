#!/usr/bin/env python3
"""
crypto/derivatives.py - In-memory frame extraction & derived media generation.

Generates poster thumbnails and multi-frame hover-preview sprite sheets completely
in RAM without writing temporary plaintext files to disk.
"""

import io
from typing import List, Tuple
from PIL import Image

try:
    import av
except ImportError:
    av = None


def extract_video_frames_in_memory(
    video_bytes: bytes,
    num_frames: int = 5,
    target_size: Tuple[int, int] = (320, 180),
) -> List[Image.Image]:
    """Extracts uniformly spaced keyframes from video_bytes in memory.

    Args:
        video_bytes: Decrypted video payload bytes.
        num_frames: Number of frames to extract.
        target_size: (width, height) tuple for frame resizing.

    Returns:
        List of PIL RGB Image objects.
    """
    w, h = target_size
    if not video_bytes:
        raise ValueError("Empty video payload bytes")

    if av is None:
        raise RuntimeError("PyAV ('av') library is required for frame extraction")

    try:
        container = av.open(io.BytesIO(video_bytes))
        video_stream = next((s for s in container.streams if s.type == 'video'), None)

        if not video_stream:
            container.close()
            raise ValueError("No video stream found in container")

        duration_sec = 0.0
        if video_stream.duration and video_stream.time_base:
            duration_sec = float(video_stream.duration * video_stream.time_base)

        frames: List[Image.Image] = []

        if duration_sec > 0.5:
            # Seek to uniformly spaced timestamp fractions
            time_stamps = [(i * duration_sec / (num_frames + 1)) for i in range(1, num_frames + 1)]
            for ts in time_stamps:
                seek_pts = int(ts / video_stream.time_base)
                try:
                    container.seek(seek_pts, stream=video_stream)
                    for frame in container.decode(video_stream):
                        img = frame.to_image().convert('RGB').resize((w, h), Image.Resampling.LANCZOS)
                        frames.append(img)
                        break
                except Exception:
                    pass
        else:
            # Fallback sequential read
            for frame in container.decode(video_stream):
                img = frame.to_image().convert('RGB').resize((w, h), Image.Resampling.LANCZOS)
                frames.append(img)
                if len(frames) >= num_frames:
                    break

        container.close()

        if not frames:
            raise ValueError("Failed to decode any valid frames from video stream")

        # Duplicate last frame if needed to reach num_frames
        while len(frames) < num_frames:
            frames.append(frames[-1].copy())

        return frames[:num_frames]

    except Exception as e:
        raise ValueError(f"Video frame extraction failed: {e}")


def generate_poster_bytes(video_bytes: bytes, params: dict) -> bytes:
    """Generates a single poster thumbnail image byte payload in RAM.

    Params:
        width: int (default 320)
        height: int (default 180)
        format: str (default 'webp')
        quality: int (default 80)
    """
    w = int(params.get('width', 320))
    h = int(params.get('height', 180))
    fmt = str(params.get('format', 'webp')).upper()
    quality = int(params.get('quality', 80))

    if fmt not in ('WEBP', 'JPEG', 'PNG'):
        fmt = 'WEBP'

    frames = extract_video_frames_in_memory(video_bytes, num_frames=1, target_size=(w, h))
    poster = frames[0]

    buf = io.BytesIO()
    if fmt == 'JPEG':
        poster.save(buf, format='JPEG', quality=quality)
    else:
        poster.save(buf, format=fmt, quality=quality)
    return buf.getvalue()


def generate_preview_sprite_bytes(video_bytes: bytes, params: dict) -> bytes:
    """Generates a composite horizontal sprite sheet image payload in RAM.

    Params:
        width: int (default 320)
        height: int (default 180)
        frames: int (default 5)
        format: str (default 'webp')
        quality: int (default 80)
    """
    w = int(params.get('width', 320))
    h = int(params.get('height', 180))
    num_frames = max(1, min(20, int(params.get('frames', 5))))
    fmt = str(params.get('format', 'webp')).upper()
    quality = int(params.get('quality', 80))

    if fmt not in ('WEBP', 'JPEG', 'PNG'):
        fmt = 'WEBP'

    extracted = extract_video_frames_in_memory(video_bytes, num_frames=num_frames, target_size=(w, h))

    # Create composite horizontal sprite image (width = w * num_frames, height = h)
    sprite = Image.new('RGB', (w * num_frames, h))
    for i, img in enumerate(extracted):
        sprite.paste(img, (i * w, 0))

    buf = io.BytesIO()
    if fmt == 'JPEG':
        sprite.save(buf, format='JPEG', quality=quality)
    else:
        sprite.save(buf, format=fmt, quality=quality)
    return buf.getvalue()
