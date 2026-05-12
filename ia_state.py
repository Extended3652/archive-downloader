"""JSON persistence helpers for favorites, sessions, and pending downloads."""
import json
import os
import time
from typing import Any, Dict, List, Optional


def default_favs() -> Dict[str, Any]:
    return {
        "items": [],
        "files": [],
        "folders": {"TV": [], "Movies": [], "Music": [], "Other": []},
    }


def load_favs(path: str) -> Dict[str, Any]:
    base = default_favs()
    try:
        if not os.path.exists(path):
            return base
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return base

        base["items"] = data.get("items", []) if isinstance(data.get("items", []), list) else []
        base["files"] = data.get("files", []) if isinstance(data.get("files", []), list) else []
        folders = data.get("folders", {})
        if isinstance(folders, dict):
            for k in ("TV", "Movies", "Music", "Other"):
                v = folders.get(k, [])
                base["folders"][k] = v if isinstance(v, list) else []
    except Exception:
        pass
    return base


def atomic_write_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def save_favs(path: str, favs: Dict[str, Any]) -> bool:
    try:
        atomic_write_json(path, favs)
        return True
    except Exception:
        return False


def load_session(path: str) -> Dict[str, Any]:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_session(path: str, data: Dict[str, Any]) -> bool:
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def pending_payload(
    identifier: str,
    item_title: str,
    files: List[Any],
    preview_prefix: str,
    glob_pat: str,
    completed_names: List[str],
) -> Dict[str, Any]:
    return {
        "identifier": identifier,
        "item_title": item_title,
        "files": [{"name": f.name, "size": int(f.size or 0), "fmt": f.fmt} for f in files],
        "preview_prefix": preview_prefix,
        "glob_pat": glob_pat,
        "completed_names": completed_names,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_pending(path: str, data: Dict[str, Any]) -> bool:
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def load_pending(path: str) -> Optional[Dict[str, Any]]:
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not data.get("identifier"):
            return None
        return data
    except Exception:
        return None


def clear_pending(path: str) -> bool:
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False
