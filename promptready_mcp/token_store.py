"""Persist PromptReady auth tokens for the MCP CLI / server.

Default path: ~/.config/promptready/credentials.json (mode 0o600).
Override with PROMPTREADY_CREDENTIALS_PATH.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


def default_credentials_path() -> Path:
    override = os.environ.get("PROMPTREADY_CREDENTIALS_PATH")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".config"
    return (base / "promptready" / "credentials.json").resolve()


@dataclass
class Credentials:
    access_token: str
    refresh_token: Optional[str] = None
    email: Optional[str] = None
    user_id: Optional[str] = None
    base_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


def load_credentials(path: Optional[Path] = None) -> Optional[Credentials]:
    p = path or default_credentials_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    access = data.get("access_token")
    if not access:
        return None
    return Credentials(
        access_token=access,
        refresh_token=data.get("refresh_token"),
        email=data.get("email"),
        user_id=data.get("user_id"),
        base_url=data.get("base_url"),
    )


def save_credentials(creds: Credentials, path: Optional[Path] = None) -> Path:
    p = path or default_credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write privately
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def clear_credentials(path: Optional[Path] = None) -> bool:
    p = path or default_credentials_path()
    if p.is_file():
        p.unlink()
        return True
    return False


def apply_credentials_to_environ(creds: Credentials) -> None:
    """Export tokens into process env (used by MCP tools / scripts)."""
    os.environ["PROMPTREADY_ACCESS_TOKEN"] = creds.access_token
    if creds.refresh_token:
        os.environ["PROMPTREADY_REFRESH_TOKEN"] = creds.refresh_token
    if creds.base_url:
        os.environ.setdefault("PROMPTREADY_BASE_URL", creds.base_url)
