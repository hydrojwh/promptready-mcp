"""Per-user convert defaults (local, not per-call).

Stored at ~/.config/promptready/settings.json (mode 0o600).
convert_pdf always reads this file — users change settings rarely via
get_convert_settings / set_convert_settings, not on every convert.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

# Product default = single preset agreed 2026-07-16
DEFAULT_CONVERT_SETTINGS: Dict[str, Any] = {
    "engine": "paddle",
    "include_tables": True,
    "include_images": False,
    "include_visuals": False,
    "remove_references": True,
}

# 백엔드 app/services/pdf_utils.py:normalize_engine 이 받는 3종과 같아야 한다.
# 은퇴한 `paddle` 은 아래 _normalize 에서 `paddle` 로 흡수된다.
ALLOWED_ENGINES = frozenset({"paddle", "deepseek_ocr", "glm_ocr"})


def default_settings_path() -> Path:
    override = os.environ.get("PROMPTREADY_SETTINGS_PATH")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return (base / "promptready" / "settings.json").resolve()


def _normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(DEFAULT_CONVERT_SETTINGS)
    if not isinstance(raw, dict):
        return out

    engine = raw.get("engine", out["engine"])
    if isinstance(engine, str):
        e = engine.strip().lower().replace("-", "_").replace(" ", "_")
        if e in {"glmocr"}:
            e = "glm_ocr"
        if e in {"deepseek", "deepseekocr", "deepseek_ocr_3b"}:
            e = "deepseek_ocr"
        # 은퇴한 `paddle` 슬롯과 1.5/1.6 표기 흔들림을 전부 흡수한다.
        if e in {
            "paddle", "paddleocr_vl", "paddleocrvl", "vl15",
            "paddleocr_vl15", "paddle_vl_1_5", "paddleocr_vl_1_5", "paddleocr_vl_1.5",
            "paddle_vl16", "paddle_vl_1_6", "paddle_vl_1.6",
            "paddleocr_vl16", "paddleocr_vl_1_6", "paddleocr_vl_1.6",
        }:
            e = "paddle"
        if e in ALLOWED_ENGINES:
            out["engine"] = e

    for key in ("include_tables", "include_images", "include_visuals", "remove_references"):
        if key in raw:
            out[key] = bool(raw[key])

    # Images off ⇒ visuals off (keep consistent unless user set visuals explicitly true with images)
    if not out["include_images"]:
        out["include_visuals"] = False
    elif "include_visuals" not in raw:
        out["include_visuals"] = True

    return out


def load_convert_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or default_settings_path()
    if not p.is_file():
        return deepcopy(DEFAULT_CONVERT_SETTINGS)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return deepcopy(DEFAULT_CONVERT_SETTINGS)
    convert = data.get("convert") if isinstance(data, dict) else None
    if isinstance(convert, dict):
        return _normalize(convert)
    if isinstance(data, dict) and "engine" in data:
        return _normalize(data)
    return deepcopy(DEFAULT_CONVERT_SETTINGS)


def save_convert_settings(
    updates: Dict[str, Any],
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Merge updates into current settings and write file. Returns full convert settings."""
    current = load_convert_settings(path)
    merged = {**current, **{k: v for k, v in updates.items() if v is not None}}
    normalized = _normalize(merged)
    p = path or default_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"convert": normalized}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return normalized


def settings_to_api_params(settings: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Query params for POST /convert (string booleans)."""
    s = settings or load_convert_settings()
    return {
        "engine": str(s["engine"]),
        "remove_references": str(bool(s["remove_references"])).lower(),
        "include_images": str(bool(s["include_images"])).lower(),
        "include_visuals": str(bool(s["include_visuals"])).lower(),
        "include_tables": str(bool(s["include_tables"])).lower(),
    }
