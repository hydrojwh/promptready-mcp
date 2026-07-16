"""Runtime configuration: env vars, then saved credentials file.

Priority:
  1. PROMPTREADY_* environment variables (MCP host / shell)
  2. ~/.config/promptready/credentials.json (from `promptready-mcp-login`)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

DEFAULT_BASE_URL = "https://promptready.space"


@dataclass(frozen=True)
class Settings:
    """Resolved server settings."""

    base_url: str
    access_token: Optional[str]
    refresh_token: Optional[str]


def load_settings() -> Settings:
    """Resolve base URL and tokens (env overrides credentials file)."""
    base_url = os.environ.get("PROMPTREADY_BASE_URL", "").strip()
    access_token = os.environ.get("PROMPTREADY_ACCESS_TOKEN")
    access_token = access_token.strip() if access_token else None
    refresh_token = os.environ.get("PROMPTREADY_REFRESH_TOKEN")
    refresh_token = refresh_token.strip() if refresh_token else None

    if not access_token or not base_url:
        try:
            from .token_store import load_credentials

            creds = load_credentials()
        except Exception:
            creds = None
        if creds:
            if not access_token:
                access_token = creds.access_token
            if not refresh_token and creds.refresh_token:
                refresh_token = creds.refresh_token
            if not base_url and creds.base_url:
                base_url = creds.base_url

    if not base_url:
        base_url = DEFAULT_BASE_URL

    return Settings(
        base_url=base_url,
        access_token=access_token,
        refresh_token=refresh_token,
    )
