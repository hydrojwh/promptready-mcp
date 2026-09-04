"""Persist PromptReady auth tokens for the MCP CLI / server.

Default path: ~/.config/promptready/credentials.json (mode 0o600).
Override with PROMPTREADY_CREDENTIALS_PATH.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
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
        # One token-free stderr line so "why did my login disappear" is
        # answerable; the path is safe to show, file contents are not.
        print(
            f"promptready-mcp: credentials file at {p} is unreadable or "
            "corrupt; acting as not logged in. Remove the file and log in "
            "again (login tool or promptready-mcp-login) if this recurs.",
            file=sys.stderr,
            flush=True,
        )
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
    payload = json.dumps(creds.to_dict(), indent=2) + "\n"
    # Per-call temp file in the target's own directory (same filesystem, so
    # the final replace stays atomic). mkstemp's O_CREAT|O_EXCL means two
    # processes never share a tmp file — the fixed name let one process
    # replace the file while another was mid-write into the same tmp,
    # crashing that writer or landing torn content in credentials.json.
    # mkstemp also creates the file 0600 from the first instant, whatever
    # the umask, so the token is never briefly group/world-readable.
    fd, tmp_name = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, p)
    except BaseException:
        # Never leave a token-bearing tmp behind on a failed save.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def clear_credentials(path: Optional[Path] = None) -> bool:
    p = path or default_credentials_path()
    removed = False
    if p.is_file():
        p.unlink()
        removed = True
    # A crash between mkstemp and replace leaves a token-bearing temp file;
    # logout must sweep those too. The names are random (mkstemp) but the
    # frame is deterministic: "<target-name>.*.tmp" in the same directory.
    # The pre-0.3.5 code used the fixed name "<stem>.tmp" — sweep legacy
    # leftovers with the same shape as well.
    stale = list(p.parent.glob(p.name + ".*.tmp"))
    legacy = p.with_suffix(".tmp")
    if legacy.is_file():
        stale.append(legacy)
    for t in stale:
        try:
            t.unlink()
        except OSError:
            pass
    return removed


def apply_credentials_to_environ(creds: Credentials) -> None:
    """Export tokens into process env (used by MCP tools / scripts)."""
    os.environ["PROMPTREADY_ACCESS_TOKEN"] = creds.access_token
    if creds.refresh_token:
        os.environ["PROMPTREADY_REFRESH_TOKEN"] = creds.refresh_token
    if creds.base_url:
        os.environ.setdefault("PROMPTREADY_BASE_URL", creds.base_url)
