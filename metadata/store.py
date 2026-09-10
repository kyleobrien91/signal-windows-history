#!/usr/bin/env python3
"""
signal_windows_history/metadata/store.py - Metadata Management Store

Tracks user annotations, favourites, custom labels, and seen status
for Signal videos. Persisted locally to 'signal_player_meta.json'.
"""

import json
import os
import threading
import time

_META_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signal_player_meta.json")
_meta_data = {
    "last_session_timestamp": 0,
    "seen_message_ids": [],
    "annotations": {}
}
_meta_lock = threading.Lock()


def _load_metadata():
    """Loads annotations and seen list from signal_player_meta.json."""
    global _meta_data
    if os.path.exists(_META_PATH):
        try:
            with open(_META_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and "annotations" in raw:
                _meta_data = {
                    "last_session_timestamp": raw.get("last_session_timestamp", 0),
                    "seen_message_ids": list(raw.get("seen_message_ids", [])),
                    "annotations": raw.get("annotations", {}),
                }
            elif isinstance(raw, dict):
                # Backwards compatible migration from { msg_id: { favourite, labels } }
                _meta_data = {
                    "last_session_timestamp": 0,
                    "seen_message_ids": list(raw.keys()),
                    "annotations": raw,
                }
            print(f"[Signal Player] [OK] Metadata loaded ({len(_meta_data['annotations'])} annotations, {len(_meta_data['seen_message_ids'])} seen)")
        except Exception as e:
            print(f"[Signal Player] Warning: could not load metadata: {e}")
            _meta_data = {"last_session_timestamp": 0, "seen_message_ids": [], "annotations": {}}
    else:
        _meta_data = {"last_session_timestamp": 0, "seen_message_ids": [], "annotations": {}}


def _save_metadata():
    """Saves metadata to disk safely under lock."""
    with _meta_lock:
        with open(_META_PATH, "w", encoding="utf-8") as f:
            json.dump(_meta_data, f, ensure_ascii=False, indent=2)


def _get_meta(item_id: str, msg_id: str = "", sent_at_ms: int = 0) -> dict:
    """Retrieves annotation dict, favourite flag, labels, and is_new calculation."""
    with _meta_lock:
        ann = _meta_data["annotations"].get(item_id) or _meta_data["annotations"].get(msg_id, {"favourite": False, "labels": []})
        last_ts = _meta_data.get("last_session_timestamp", 0)
        seen_set = set(_meta_data.get("seen_message_ids", []))
        is_seen = (item_id in seen_set) or (msg_id in seen_set)
        is_new = bool(last_ts > 0 and sent_at_ms > last_ts and not is_seen)
        return {
            "favourite": bool(ann.get("favourite", False)),
            "labels": list(ann.get("labels", [])),
            "is_new": is_new,
        }


def _set_meta(msg_id: str, favourite: bool = None, labels: list = None):
    """Updates favourite or labels for a given message/attachment ID."""
    with _meta_lock:
        entry = _meta_data["annotations"].setdefault(msg_id, {"favourite": False, "labels": []})
        if favourite is not None:
            entry["favourite"] = bool(favourite)
        if labels is not None:
            seen = []
            for lbl in labels:
                lbl = str(lbl).strip()[:30]
                if lbl and lbl not in seen:
                    seen.append(lbl)
            entry["labels"] = seen[:20]
        _save_metadata()
        return dict(entry)


def _mark_seen(msg_ids: list):
    """Marks a list of message/attachment IDs as viewed."""
    with _meta_lock:
        seen_list = _meta_data.setdefault("seen_message_ids", [])
        seen_set = set(seen_list)
        changed = False
        for mid in msg_ids:
            if mid and mid not in seen_set:
                seen_set.add(mid)
                seen_list.append(mid)
                changed = True
        if changed:
            _save_metadata()


def _all_labels() -> list:
    """Returns all unique labels currently in use, sorted alphabetically."""
    with _meta_lock:
        seen = set()
        for v in _meta_data["annotations"].values():
            seen.update(v.get("labels", []))
    return sorted(seen)