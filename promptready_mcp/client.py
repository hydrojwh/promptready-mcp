"""Thin async HTTP client for the PromptReady backend API.

Source of truth:
  - app/api/endpoints/users.py  GET /api/v1/users/credits
  - app/api/endpoints/convert.py  POST /api/v1/convert, GET /api/v1/convert/status
  - app/api/endpoints/auth.py  POST /api/v1/auth/refresh
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import httpx

API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT = 60.0
CONVERT_TIMEOUT = 300.0  # upload + queue only; OCR runs async


class PromptReadyAPIError(Exception):
    """API or transport failure with a user-facing message."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{API_PREFIX}{path}"


def _auth_headers(access_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def fetch_credits(
    base_url: str, access_token: str, *, timeout: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """GET /users/credits → {credit_balance, email}."""
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.get(
            _url(base_url, "/users/credits"),
            headers=_auth_headers(access_token),
        )
        if resp.status_code >= 400:
            raise PromptReadyAPIError(
                f"credits failed HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )
        return resp.json()


async def refresh_access_token(
    base_url: str, refresh_token: str, *, timeout: float = DEFAULT_TIMEOUT
) -> Dict[str, str]:
    """POST /auth/refresh → {access_token, refresh_token}."""
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.post(
            _url(base_url, "/auth/refresh"),
            json={"refresh_token": refresh_token},
        )
        if resp.status_code >= 400:
            raise PromptReadyAPIError(
                f"token refresh failed HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )
        data = resp.json()
        if not data.get("success") or not data.get("access_token"):
            raise PromptReadyAPIError(
                data.get("message") or "token refresh returned no access_token",
                body=str(data)[:500],
            )
        out: Dict[str, str] = {"access_token": data["access_token"]}
        if data.get("refresh_token"):
            out["refresh_token"] = data["refresh_token"]
        return out


async def convert_pdf(
    base_url: str,
    access_token: str,
    file_path: Path,
    *,
    timeout: float = CONVERT_TIMEOUT,
    params: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """POST /convert multipart — queues PDF and returns ConvertResponse JSON.

    Query params come from user_settings (saved defaults), not per-call UI.
    """
    from .user_settings import settings_to_api_params

    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise PromptReadyAPIError(f"file not found: {path}")
    if path.suffix.lower() not in (".pdf", ".csv"):
        raise PromptReadyAPIError("only .pdf or .csv files are supported")

    query = params if params is not None else settings_to_api_params()
    data = path.read_bytes()
    if not data:
        raise PromptReadyAPIError("file is empty")

    files = {
        "file": (path.name, data, "application/pdf" if path.suffix.lower() == ".pdf" else "text/csv"),
    }
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.post(
            _url(base_url, "/convert"),
            headers=_auth_headers(access_token),
            params=query,
            files=files,
        )
        if resp.status_code >= 400:
            raise PromptReadyAPIError(
                f"convert failed HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )
        return resp.json()


async def get_conversion_status(
    base_url: str, access_token: str, *, timeout: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """GET /convert/status for the authenticated user's session."""
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.get(
            _url(base_url, "/convert/status"),
            headers=_auth_headers(access_token),
        )
        if resp.status_code >= 400:
            raise PromptReadyAPIError(
                f"status failed HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )
        return resp.json()


async def download_url_to_path(
    url: str, dest: Path, *, timeout: float = CONVERT_TIMEOUT
) -> Path:
    """Download markdown_url (presigned) to dest path."""
    dest = Path(dest).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        resp = await http.get(url)
        if resp.status_code >= 400:
            raise PromptReadyAPIError(
                f"download failed HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:300],
            )
        dest.write_bytes(resp.content)
    return dest


async def list_usage(
    base_url: str,
    access_token: str,
    *,
    limit: int = 20,
    offset: int = 0,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """GET /users/usage → {total_count, logs: [usage row, ...]}.

    DB-backed (survives server restarts), newest first. Each row carries
    id / file_name / page_count / credits_deducted / status / created_at /
    markdown_object_key / expires_at / engine_used — the durable source
    for looking up a conversion by its log_id (usage row id).
    """
    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.get(
            _url(base_url, "/users/usage"),
            headers=_auth_headers(access_token),
            params={"limit": limit, "offset": offset},
        )
        if resp.status_code >= 400:
            raise PromptReadyAPIError(
                f"usage list failed HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )
        return resp.json()


async def download_usage_markdown(
    base_url: str,
    access_token: str,
    log_id: str,
    dest: Path,
    *,
    timeout: float = CONVERT_TIMEOUT,
) -> Path:
    """GET /convert/download/{log_id}?proxy=true → markdown bytes at dest.

    The endpoint has two modes (app/api/endpoints/convert.py
    download_markdown_file): proxy=true streams the file bytes back on
    this request (text/markdown), proxy=false returns a JSON envelope
    with a presigned URL that would need a second download hop. The
    proxy mode is chosen: one request, no redirect, and the presigned
    URL buys nothing for a server-side client. 410 = the 3-hour
    retention window expired; 404 = key missing/file already cleaned.
    """
    dest = Path(dest).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
        resp = await http.get(
            _url(base_url, f"/convert/download/{log_id}"),
            headers=_auth_headers(access_token),
            params={"proxy": "true"},
        )
        if resp.status_code >= 400:
            raise PromptReadyAPIError(
                f"download failed HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )
        dest.write_bytes(resp.content)
    return dest
