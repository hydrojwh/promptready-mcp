"""PromptReady MCP server entrypoint (stdio transport).

P0: get_credits
P1: convert_pdf, get_status, wait_and_download
P2 (0.3.7): slash prompts for humans — see prompts.py
P3 (0.4.0): durable results — log_id-keyed status/download via the DB-backed
    /users/usage + /convert/download/{log_id} endpoints, so a backend restart
    or a second back-to-back convert can no longer orphan a finished job.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from . import client
from .client import PromptReadyAPIError
from .config import load_settings
from .prompts import register_prompts

logger = logging.getLogger("promptready_mcp")

mcp = FastMCP("promptready")

# Human entry points: /promptready:<name> in MCP hosts (0.3.7).
register_prompts(mcp)

DEFAULT_OUT_DIR = "promptready-out"

# The backend can flip stage to "completed" a few seconds before the result
# markdown_url is populated (measured <=5s after completion, 2026-09-04,
# 4/4 files). convert_pdf(wait=true) keeps re-checking status for up to
# this long before reporting a pending download. 30s = 6x the measured
# worst case, bounded so a wait call cannot hang. Status reads are GETs
# and never deduct credits (credits go on POST /convert only).
RESULT_URL_WAIT_SEC = 30.0

# Durable path (0.4.0): /users/usage has no id filter, so finding a log_id
# walks newest-first pages. 50 x 8 pages = the 400 most recent rows — well
# past a heavy day of conversions; older than that gets a clear error
# instead of an unbounded scan.
USAGE_SCAN_PAGE_SIZE = 50
USAGE_SCAN_MAX_ROWS = 400

# usage_logs statuses (app/core/deps.py deduct/refund + queue_manager):
# pending while queued/running; the rest are terminal.
# "refunded" is a refund bookkeeping row: terminal, never downloadable. Treating it as
# terminal keeps a directly-passed refund id from polling for the full timeout.
USAGE_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "refunded")

# Match static/js/workspace-utils.js ENGINE_DISPLAY_NAMES + buildMarkdownDownloadFilename.
# 별칭 집합은 백엔드 app/services/pdf_utils.py:normalize_engine 과 같아야 한다.
ENGINE_DISPLAY_NAMES = {
    "paddle": "PaddleOCR-VL-1.6",
    "deepseek_ocr": "DeepSeek-OCR",
    "glm_ocr": "GLM-OCR",
}


_ENGINE_ALIASES = {
    "glm_ocr": "glm_ocr", "glmocr": "glm_ocr",
    "deepseek_ocr": "deepseek_ocr", "deepseek": "deepseek_ocr",
    "deepseekocr": "deepseek_ocr", "deepseek_ocr_3b": "deepseek_ocr",
    # 내부 키는 `paddle` 를 유지한다 — 1.5 시절 이름이지만 usage_logs·환경변수에 퍼져 있다.
    "paddle": "paddle",
    "paddle_vl16": "paddle", "paddle_vl_1_6": "paddle", "paddle_vl_1.6": "paddle",
    "paddleocr_vl16": "paddle", "paddleocr_vl_1_6": "paddle", "paddleocr_vl_1.6": "paddle",
    "paddle_vl_1_5": "paddle", "paddleocr_vl15": "paddle",
    "paddleocr_vl_1_5": "paddle", "paddleocr_vl_1.5": "paddle", "vl15": "paddle",
    # 은퇴한 구형 슬롯 — 후속 버전으로 흡수한다.
    "paddle": "paddle", "paddleocr_vl": "paddle", "paddleocrvl": "paddle",
}


def resolve_engine_alias(raw: Optional[str]) -> Optional[str]:
    """별칭을 정식 키로. 아는 이름이 아니면 None — 오타를 조용히 삼키지 않는다."""
    if not raw or not isinstance(raw, str):
        return None
    return _ENGINE_ALIASES.get(raw.strip().lower().replace("-", "_").replace(" ", "_"))


def _normalize_engine(raw: Optional[str]) -> str:
    """표시·파일명용. 모르는 값이면 기본 엔진으로 떨어진다."""
    return resolve_engine_alias(raw) or "paddle"


def build_markdown_download_filename(
    filename: Optional[str],
    engine_used: Optional[str] = None,
) -> str:
    """Same naming as web: `{base}_{PaddleOCR-VL}.md` (CSV → `_converted.md`)."""
    safe = filename or "document.pdf"
    lower = safe.lower()
    base = Path(safe).stem
    if lower.endswith(".csv"):
        return f"{base}_converted.md"
    engine = _normalize_engine(engine_used)
    label = ENGINE_DISPLAY_NAMES.get(engine, ENGINE_DISPLAY_NAMES["paddle"])
    return f"{base}_{label}.md"


def _err(
    message: str,
    *,
    status_code: Optional[int] = None,
    body: Optional[str] = None,
    **extra: Any,
) -> str:
    payload: Dict[str, Any] = {"ok": False, "error": message}
    if status_code is not None:
        payload["status_code"] = status_code
    if body:
        payload["body"] = body
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def _persist_refreshed_tokens(tokens: Dict[str, str]) -> None:
    """Best-effort write of refreshed tokens back to the credentials file.

    Supabase rotates refresh tokens (the old one is revoked on each use), so
    keeping the new pair only in os.environ leaves a dead token on disk.
    MCP hosts restart this server on every reconnect, which turned that into
    a re-login loop. Only the token fields are swapped: email / user_id /
    base_url are preserved from the existing file because Settings does not
    carry them and save_credentials overwrites the whole file.

    Never raises: a read-only filesystem must not fail the tool call, which
    already holds a working token in memory. Emits one token-free stderr
    line instead of failing silently. If no credentials file exists (env-only
    setup), nothing is written — we do not create a file the user never had.
    """
    try:
        from .token_store import load_credentials, save_credentials

        creds = load_credentials()
        if creds is None:
            return
        creds.access_token = tokens["access_token"]
        if tokens.get("refresh_token"):
            creds.refresh_token = tokens["refresh_token"]
        save_credentials(creds)
    except Exception:
        print(
            "promptready-mcp: could not save refreshed credentials to disk; "
            "a fresh login may be needed after this process exits",
            file=sys.stderr,
            flush=True,
        )


async def _with_auth_retry(coro_factory):
    """Run an async API call; on 401 try refresh once if refresh_token is set."""
    settings = load_settings()
    if not settings.access_token:
        raise PromptReadyAPIError(
            "Not logged in. Call the login tool or run: promptready-mcp-login"
        )

    async def _call(token: str):
        return await coro_factory(settings.base_url, token)

    try:
        return await _call(settings.access_token), settings
    except PromptReadyAPIError as e:
        if e.status_code not in (401, 403) or not settings.refresh_token:
            raise
        try:
            tokens = await client.refresh_access_token(
                settings.base_url, settings.refresh_token
            )
        except PromptReadyAPIError:
            # `from None` cuts __cause__ but leaves the original exception in
            # __context__; a dumper walking the chain could still reach the
            # HTTP body (tokens included). Raise, catch, unlink the context,
            # bare-re-raise — a bare raise does not re-chain (verified
            # empirically; pre-clearing before `raise err` does not stick).
            err = PromptReadyAPIError(
                "Session expired: token refresh failed. Log in again — "
                "call the login tool or run: promptready-mcp-login"
            )
            try:
                raise err from None
            except PromptReadyAPIError:
                err.__context__ = None
                raise
        # Update process env so subsequent tools see the new token, and the
        # credentials file so a server restart does not resurrect the now
        # revoked refresh token (Supabase rotates them).
        os.environ["PROMPTREADY_ACCESS_TOKEN"] = tokens["access_token"]
        if tokens.get("refresh_token"):
            os.environ["PROMPTREADY_REFRESH_TOKEN"] = tokens["refresh_token"]
        _persist_refreshed_tokens(tokens)
        settings = load_settings()
        return await _call(settings.access_token), settings


def _valid_log_id(raw: Any) -> Optional[str]:
    """Canonical-UUID check for log_id before it goes into a request path.

    Same defense posture as prompts._validate_arg (0.3.8): programmatic
    callers can send anything, and a stray "/" in log_id would otherwise
    become a path segment. Accepts only the dashed hex canonical form
    (case-insensitive); everything else → None.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        parsed = uuid.UUID(candidate)
    except ValueError:
        return None
    if str(parsed) != candidate.lower():
        return None
    return str(parsed)


def _parse_usage_time(value: Any) -> Optional[datetime]:
    """Parse a usage-row timestamp (created_at/expires_at) → aware UTC."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _usage_row_downloadable(row: Dict[str, Any]) -> bool:
    """completed ∧ result key present ∧ retention window not over."""
    if row.get("status") != "completed" or not row.get("markdown_object_key"):
        return False
    expires = _parse_usage_time(row.get("expires_at"))
    if expires is None:
        # Key present but no parsable expiry — let the download attempt decide.
        return True
    return expires > datetime.now(timezone.utc)


async def _find_usage_row(log_id: str) -> Dict[str, Any]:
    """Find the /users/usage row with id == log_id (newest-first paging).

    Raises PromptReadyAPIError when the row is not within the scan limit
    — the error names the limit so the caller knows it is a bound, not
    a "does not exist".
    """
    scanned = 0
    while scanned < USAGE_SCAN_MAX_ROWS:
        data, _ = await _with_auth_retry(
            lambda base, token, _off=scanned: client.list_usage(
                base, token, limit=USAGE_SCAN_PAGE_SIZE, offset=_off
            )
        )
        for row in data.get("logs") or []:
            if str(row.get("id")) == log_id:
                return row
        total = data.get("total_count") or 0
        scanned += USAGE_SCAN_PAGE_SIZE
        if scanned >= total:
            break
    raise PromptReadyAPIError(
        f"log_id {log_id} not found within the {USAGE_SCAN_MAX_ROWS} most "
        "recent usage rows — wrong id, another account, or an old conversion"
    )


def _is_conversion_row(row: Dict[str, Any]) -> bool:
    """False for refund bookkeeping rows in /users/usage.

    refund_credits without a log_id inserts a separate usage row
    (status 'refunded', file_name 'REFUND: <reason>', 0 pages, negative
    credits). It is not a conversion: picking it as "the latest" or listing it
    next to real files is wrong, and it can never be downloaded.
    """
    if str(row.get("status") or "") == "refunded":
        return False
    return not str(row.get("file_name") or "").startswith("REFUND:")


async def _latest_usage_row() -> Optional[Dict[str, Any]]:
    """Most recent *conversion* row of /users/usage (the fallback pick), or None."""
    data, _ = await _with_auth_retry(
        lambda base, token: client.list_usage(base, token, limit=50, offset=0)
    )
    for row in data.get("logs") or []:
        if _is_conversion_row(row):
            return row
    return None


async def _wait_usage_row(
    log_id: str, *, timeout_sec: int, poll_interval_sec: float
) -> Dict[str, Any]:
    """Poll the usage row for log_id until terminal status or timeout.

    Returns the row; on timeout the row dict gets timeout=True plus a
    message naming the log_id for the retry call.
    """
    deadline = asyncio.get_event_loop().time() + max(1, timeout_sec)
    key_wait_started: Optional[float] = None
    while True:
        row = await _find_usage_row(log_id)
        status = str(row.get("status") or "")
        now = asyncio.get_event_loop().time()
        if status == "completed" and not row.get("markdown_object_key"):
            # The backend flips usage status to completed a few seconds BEFORE it
            # records the result key (queue_manager: update_usage_log_status, then
            # update_usage_log_markdown). Downloading in that gap 404s with "file
            # key missing", which must never read as "expired — reconvert". Same
            # window the memory path covers with RESULT_URL_WAIT_SEC (0.3.7).
            if key_wait_started is None:
                key_wait_started = now
            if now - key_wait_started < RESULT_URL_WAIT_SEC and now < deadline:
                await asyncio.sleep(max(0.5, min(poll_interval_sec, 2.0)))
                continue
        elif status in USAGE_TERMINAL_STATUSES:
            return row
        if status in USAGE_TERMINAL_STATUSES:
            return row  # completed, key still missing after the wait: caller decides
        if now >= deadline:
            out = dict(row)
            out["timeout"] = True
            out["message"] = (
                f"still '{status}' after {timeout_sec}s — call "
                f"wait_and_download with log_id={log_id} to keep waiting"
            )
            return out
        await asyncio.sleep(max(0.5, poll_interval_sec))


async def _download_by_log_id(
    row: Dict[str, Any], output_dir: str
) -> Dict[str, Any]:
    """Save the completed result of `row` via /convert/download/{log_id}.

    Error mapping keeps the two contracts apart: 410/404 mean the stored
    result is gone for good (re-convert is the only way out — say so);
    anything else is a transient delivery failure where the conversion
    succeeded and credits were spent, so the answer is a retry with the
    same log_id, never a fresh convert.
    """
    log_id = str(row.get("id"))
    out_dir = Path(output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = row.get("file_name") or "document.pdf"
    dest = out_dir / build_markdown_download_filename(name, row.get("engine_used"))
    try:
        await _with_auth_retry(
            lambda base, token: client.download_usage_markdown(
                base, token, log_id, dest
            )
        )
        return {"ok": True, "markdown_path": str(dest.resolve())}
    except PromptReadyAPIError as e:
        if e.status_code == 410:
            return {
                "ok": False,
                "expired": True,
                "error": (
                    "The result expired: downloads are kept for 3 hours "
                    "(HTTP 410). The stored markdown is gone — converting "
                    "the file again is the only way to get it, and that "
                    "spends fresh credits."
                ),
            }
        if e.status_code == 404 and not row.get("markdown_object_key"):
            # The row never showed a result key: the result was never recorded (or
            # not yet) — the conversion itself is complete and paid for. Retry,
            # do not reconvert.
            return {
                "ok": False,
                "expired": False,
                "error": (
                    "The conversion completed but its result key is not recorded "
                    "yet (HTTP 404: file key missing)."
                ),
                "hint": (
                    "The conversion succeeded and the credits were already spent — "
                    "do NOT convert this file again. Retry wait_and_download with "
                    f"log_id={log_id} in a few seconds; check list_conversions if it "
                    "keeps failing."
                ),
            }
        if e.status_code == 404:
            return {
                "ok": False,
                "expired": True,
                "error": (
                    "The result file is no longer on the server (HTTP 404: "
                    "missing or cleaned after the 3-hour window). Converting "
                    "the file again is the only way to get it (fresh credits)."
                ),
            }
        return {
            "ok": False,
            "expired": False,
            "error": e.message,
            "hint": (
                "The conversion succeeded and the credits were already "
                "spent — do NOT convert this file again. Retry "
                f"wait_and_download with log_id={log_id}; the server still "
                "holds the result."
            ),
        }


def _usage_row_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """Common DB-row fields shared by the durable-path responses."""
    return {
        "log_id": str(row.get("id")) if row.get("id") is not None else None,
        "file_name": row.get("file_name"),
        "page_count": row.get("page_count"),
        "credits_deducted": row.get("credits_deducted"),
        "engine_used": row.get("engine_used"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
    }


async def _durable_wait_by_log_id(
    log_id: str,
    *,
    timeout_sec: int,
    poll_interval_sec: float,
    output_dir: str,
    source_path: str = "",
    fallback: bool = False,
) -> tuple:
    """Wait for + download one conversion by log_id on the DB path.

    Returns (ok, payload). payload carries source/log_id/file_name/...
    plus stage + finished; ok=False payloads carry error (+hint for
    transient delivery failures, +expired when the stored result is gone
    for good). payload["row"] keeps the raw usage row for callers that
    merge extra fields (convert_pdf wait path).
    """
    try:
        row = await _wait_usage_row(
            log_id, timeout_sec=timeout_sec, poll_interval_sec=poll_interval_sec
        )
    except PromptReadyAPIError as e:
        return False, {
            "error": e.message,
            "status_code": e.status_code,
            "body": e.body,
            "log_id": log_id,
        }
    except httpx.HTTPError as e:
        return False, {"error": f"Network error: {e}", "log_id": log_id}

    payload: Dict[str, Any] = {"row": row}
    if fallback:
        payload["source"] = "usage_db_fallback"
        payload["fallback_note"] = (
            "session job not found on the server (restart or new instance?) "
            "— resolved via the most recent conversion for this account"
        )
    else:
        payload["source"] = "usage_db"
    payload.update(_usage_row_fields(row))
    status = str(row.get("status") or "")

    if row.get("timeout"):
        payload.update(
            finished=False, stage=status, status=status,
            timeout=True, message=row.get("message"),
        )
        return True, payload
    if status != "completed":
        payload.update(
            finished=True, stage=status, status=status,
            message=f"conversion ended as '{status}'",
        )
        return True, payload

    saved = await _download_by_log_id(row, output_dir)
    if not saved.get("ok"):
        payload.update(
            finished=False, stage="completed", status="completed",
            error=saved["error"], expired=saved.get("expired", False),
        )
        if saved.get("hint"):
            payload["hint"] = saved["hint"]
        return False, payload
    payload.update(
        finished=True, stage="completed", status="completed",
        markdown_path=saved.get("markdown_path"),
        message="conversion completed; markdown saved",
    )
    return True, payload


@mcp.tool()
async def login(timeout_sec: int = 300) -> str:
    """Open the browser Google login page and save tokens for this machine.

    Starts a local callback on http://127.0.0.1:18765/callback, opens PromptReady
    Google OAuth, stores credentials under ~/.config/promptready/credentials.json
    (mode 600), and applies them to this process.

    Users should run this once (or use CLI: promptready-mcp-login) instead of
    pasting tokens manually. After sign-in the browser returns to the local
    callback URL; no Supabase configuration is required.
    """
    try:
        # Browser + local HTTP server is blocking; run in a worker thread.
        from .login_flow import login_with_browser
        from .token_store import default_credentials_path

        settings = load_settings()
        creds = await asyncio.to_thread(
            login_with_browser,
            base_url=settings.base_url,
            timeout_sec=float(timeout_sec),
            open_browser=True,
        )
        return _ok(
            email=creds.email,
            credentials_path=str(default_credentials_path()),
            message="Login successful. Tokens saved for MCP tools.",
        )
    except Exception as e:
        return _err(
            str(e),
            hint=(
                "If the browser flow keeps failing, log in from a terminal "
                "instead: promptready-mcp-login --email you@x.com"
            ),
        )


@mcp.tool()
async def logout() -> str:
    """Remove saved MCP credentials from this machine and clear env tokens."""
    from .token_store import clear_credentials

    cleared = clear_credentials()
    os.environ.pop("PROMPTREADY_ACCESS_TOKEN", None)
    os.environ.pop("PROMPTREADY_REFRESH_TOKEN", None)
    return _ok(cleared=cleared, message="Logged out (credentials file removed if present).")


@mcp.tool()
async def get_credits() -> str:
    """Get the calling user's PromptReady credit balance.

    Returns JSON: ok + credit_balance + email, or ok:false + error.
    Requires prior login (promptready-mcp-login / login tool) or env token.
    """
    try:
        data, _ = await _with_auth_retry(
            lambda base, token: client.fetch_credits(base, token)
        )
        return _ok(**data)
    except PromptReadyAPIError as e:
        return _err(e.message, status_code=e.status_code, body=e.body)
    except httpx.HTTPError as e:
        return _err(f"Network error: {e}")


@mcp.tool()
async def get_convert_settings() -> str:
    """Show saved convert defaults for this machine (not per-file options).

    Defaults live in ~/.config/promptready/settings.json. convert_pdf always
    uses these until you change them with set_convert_settings.
    """
    from .user_settings import default_settings_path, load_convert_settings

    s = load_convert_settings()
    return _ok(settings=s, path=str(default_settings_path()))


@mcp.tool()
async def set_convert_settings(
    engine: Optional[str] = None,
    include_tables: Optional[bool] = None,
    include_images: Optional[bool] = None,
    remove_references: Optional[bool] = None,
) -> str:
    """Update saved convert defaults (persists for future convert_pdf calls).

    Only pass fields you want to change. Example: set engine to paddle once,
    then every convert uses that until changed again.

    Args:
        engine: paddle (PaddleOCR-VL-1.6) | deepseek_ocr (DeepSeek-OCR) | glm_ocr (GLM-OCR)
        include_tables: keep markdown tables
        include_images: include image/visual extraction (heavier)
        remove_references: strip references section when supported
    """
    from .user_settings import (
        ALLOWED_ENGINES,
        default_settings_path,
        save_convert_settings,
    )

    updates: Dict[str, Any] = {}
    if engine is not None:
        # 별칭(`deepseek`, 은퇴한 `paddle` 등)은 받아주고, 아예 모르는 이름만 거부한다.
        e = resolve_engine_alias(engine)
        if e is None or e not in ALLOWED_ENGINES:
            return _err(
                f"invalid engine {engine!r}",
                allowed=sorted(ALLOWED_ENGINES),
            )
        updates["engine"] = e
    if include_tables is not None:
        updates["include_tables"] = include_tables
    if include_images is not None:
        updates["include_images"] = include_images
        updates["include_visuals"] = include_images
    if remove_references is not None:
        updates["remove_references"] = remove_references

    if not updates:
        return _err("no settings provided; pass engine and/or include_* fields")

    saved = save_convert_settings(updates)
    return _ok(
        settings=saved,
        path=str(default_settings_path()),
        message="Saved. Future convert_pdf calls will use these defaults.",
    )


@mcp.tool()
async def convert_pdf(
    path: str,
    wait: bool = False,
    timeout_sec: int = 3600,
    output_dir: str = DEFAULT_OUT_DIR,
) -> str:
    """Upload a local PDF/CSV and queue conversion (same credits as the web app).

    Uses **saved user settings** (get_convert_settings / set_convert_settings),
    not per-call engine flags. Factory default: paddle, tables on, images off.

    Args:
        path: Absolute or home-relative path to a .pdf or .csv file.
        wait: If true, poll until done and download markdown (can take many minutes).
        timeout_sec: Max wait when wait=true (default 3600).
        output_dir: Directory for downloaded .md when wait=true.

    Returns JSON with job info; if wait=true and completed, includes local md path.
    The download can lag completion by a few seconds; if the file still cannot
    be saved, the response says download="pending" — the conversion succeeded
    and credits were already spent, so call wait_and_download, not convert again.
    """
    from .user_settings import load_convert_settings, settings_to_api_params

    preset = load_convert_settings()
    api_params = settings_to_api_params(preset)
    try:
        def _convert(base: str, token: str):
            return client.convert_pdf(base, token, Path(path), params=api_params)

        data, _ = await _with_auth_retry(_convert)
    except PromptReadyAPIError as e:
        return _err(e.message, status_code=e.status_code, body=e.body)
    except Exception as e:
        return _err(f"convert error: {e}")

    # CSV may return markdown inline immediately.
    if data.get("markdown") and not data.get("options", {}).get("async"):
        out = Path(output_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        engine_used = data.get("engine_used") or preset.get("engine")
        dest = out / build_markdown_download_filename(
            data.get("filename") or Path(path).name, engine_used
        )
        dest.write_text(data["markdown"], encoding="utf-8")
        return _ok(
            mode="inline",
            filename=data.get("filename"),
            page_count=data.get("page_count"),
            engine_used=engine_used,
            settings=preset,
            markdown_path=str(dest.resolve()),
            message=data.get("message"),
        )

    job_id = (data.get("options") or {}).get("job_id")
    log_id = (data.get("options") or {}).get("log_id")
    result: Dict[str, Any] = {
        "mode": "async",
        "filename": data.get("filename"),
        "page_count": data.get("page_count"),
        "engine_used": data.get("engine_used") or preset.get("engine"),
        "settings": preset,
        "job_id": job_id,
        "message": data.get("message"),
        "credits_note": "Credits were deducted by the server on queue (same as web).",
    }
    if log_id:
        # Durable handle (0.4.0): the DB row id. Keep it — it finds the
        # result even after a backend restart or another convert.
        result["log_id"] = log_id

    if not wait:
        result["next"] = (
            "Call get_status / convert_pdf(..., wait=true), or "
            f"wait_and_download(log_id={log_id}) — the log_id works for 3 "
            "hours even if the server restarts"
            if log_id
            else "Call get_status or convert_pdf(..., wait=true)"
        )
        return _ok(**result)

    # wait path — durable route when the server handed us a log_id
    if log_id:
        outcome_ok, outcome = await _durable_wait_by_log_id(
            log_id, timeout_sec=timeout_sec, poll_interval_sec=3.0,
            output_dir=output_dir,
        )
        result["source"] = outcome.get("source")
        row = outcome.get("row") or {}
        if outcome_ok and outcome.get("stage") == "completed":
            result["status"] = {"stage": "completed", "status": "completed"}
            result["markdown_path"] = outcome.get("markdown_path")
            result["download"] = "ok"
        elif outcome_ok:
            # failed / cancelled, or still pending at timeout — terminal-ish
            result["status"] = {
                "stage": outcome.get("stage"),
                "status": outcome.get("stage"),
                "message": outcome.get("message"),
            }
            result["download"] = "not_attempted" if outcome.get("finished") else "pending"
            if outcome.get("timeout"):
                result["next"] = outcome.get("message")
        elif outcome.get("expired"):
            result["status"] = {"stage": "completed", "status": "completed"}
            result["download"] = "expired"
            result["download_error"] = outcome.get("error")
            result["next"] = outcome.get("error")
        else:
            result["status"] = {"stage": "completed", "status": "completed"}
            result["download"] = "pending"
            result["download_error"] = outcome.get("error")
            result["next"] = outcome.get(
                "hint"
            ) or (
                "Conversion succeeded and credits were already spent — do "
                f"NOT convert this file again. Call wait_and_download with "
                f"log_id={log_id} to fetch the markdown."
            )
        if row:
            # Row fields from the DB (page_count, created_at, ...). Skipped
            # on transport errors (empty row) so the upload-time log_id and
            # counts survive in the response.
            result.update(_usage_row_fields(row))
        return _ok(**result)

    waited = await _poll_until_done(
        timeout_sec=timeout_sec, url_wait_sec=RESULT_URL_WAIT_SEC
    )
    result["status"] = waited
    if waited.get("stage") == "completed":
        saved = await _save_result_markdown(waited, path, output_dir)
        if saved.get("ok"):
            result["markdown_path"] = saved.get("markdown_path")
            result["download"] = "ok"
        else:
            # The conversion itself succeeded and credits were spent at
            # queue time; only the file delivery failed. Say so explicitly
            # so the caller fetches the result instead of converting again
            # (a re-convert would spend fresh credits for the same file).
            result["download"] = "pending"
            result["download_error"] = saved.get("error")
            result["next"] = (
                "Conversion succeeded and credits were already spent — do "
                "NOT convert this file again. The server still has the "
                "result; call wait_and_download to fetch the markdown."
            )
    return _ok(**result)


@mcp.tool()
async def get_status(log_id: str = "") -> str:
    """Get conversion status — durable by log_id, session memory otherwise.

    With log_id (the UUID from convert_pdf's response): reads the
    DB-backed usage row (GET /users/usage) — survives backend restarts,
    redeploys and back-to-back converts. Status is the DB value
    (pending / completed / failed / cancelled).

    Without log_id: reads the session status (GET /convert/status); if
    the server no longer knows the session job ("not_found" — restart,
    scale-to-zero), falls back to the most recent usage row and says
    which file it picked.
    """
    if log_id:
        norm = _valid_log_id(log_id)
        if norm is None:
            return _err(
                f"invalid log_id {log_id!r}: expected a UUID "
                "(the log_id field convert_pdf returned)"
            )
        try:
            row = await _find_usage_row(norm)
        except PromptReadyAPIError as e:
            return _err(e.message, status_code=e.status_code, body=e.body)
        except httpx.HTTPError as e:
            return _err(f"Network error: {e}")
        return _ok(
            source="usage_db",
            stage=row.get("status"),
            status=row.get("status"),
            **_usage_row_fields(row),
        )

    try:
        data, _ = await _with_auth_retry(
            lambda base, token: client.get_conversion_status(base, token)
        )
    except PromptReadyAPIError as e:
        return _err(e.message, status_code=e.status_code, body=e.body)
    except httpx.HTTPError as e:
        return _err(f"Network error: {e}")

    stage = data.get("stage") or data.get("status")
    if stage != "not_found":
        return _ok(**data)

    # Session memory lost the job — fall back to the newest usage row.
    try:
        row = await _latest_usage_row()
    except PromptReadyAPIError as e:
        return _err(e.message, status_code=e.status_code, body=e.body)
    except httpx.HTTPError as e:
        return _err(f"Network error: {e}")
    if row is None:
        return _ok(**data)  # nothing converted on this account either

    return _ok(
        source="usage_db_fallback",
        stage=row.get("status"),
        status=row.get("status"),
        fallback_note=(
            "session job not found on the server (restart or new instance?) "
            "— showing the most recent conversion for this account"
        ),
        **_usage_row_fields(row),
    )


@mcp.tool()
async def list_conversions(limit: int = 10) -> str:
    """List this account's recent conversions (DB-backed, restart-proof).

    Each entry: log_id, file_name, page_count, credits, status
    (pending/completed/failed/cancelled), created_at, expires_at,
    engine_used, and downloadable (completed ∧ result retained — false
    once the 3-hour window passed). Use a log_id with wait_and_download
    to fetch any downloadable result, even after a server restart.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        return _err(f"invalid limit {limit!r}: must be an integer in 1..200")
    try:
        data, _ = await _with_auth_retry(
            # Over-fetch a little: refund bookkeeping rows are skipped below and
            # would otherwise shrink the page below the requested limit.
            lambda base, token: client.list_usage(
                base, token, limit=min(limit + 10, 200), offset=0
            )
        )
    except PromptReadyAPIError as e:
        return _err(e.message, status_code=e.status_code, body=e.body)
    except httpx.HTTPError as e:
        return _err(f"Network error: {e}")

    rows = [r for r in (data.get("logs") or []) if _is_conversion_row(r)][:limit]
    conversions = [
        {
            "log_id": row.get("id"),
            "file_name": row.get("file_name"),
            "page_count": row.get("page_count"),
            "credits": row.get("credits_deducted"),
            "status": row.get("status"),
            "created_at": row.get("created_at"),
            "expires_at": row.get("expires_at"),
            "engine_used": row.get("engine_used"),
            "downloadable": _usage_row_downloadable(row),
        }
        for row in rows
    ]
    return _ok(
        total_count=data.get("total_count"),
        count=len(conversions),
        conversions=conversions,
    )


@mcp.tool()
async def wait_and_download(
    timeout_sec: int = 3600,
    poll_interval_sec: float = 3.0,
    output_dir: str = DEFAULT_OUT_DIR,
    source_path: str = "",
    log_id: str = "",
) -> str:
    """Wait for a conversion to finish, then save its markdown.

    Durable path (0.4.0): pass log_id (UUID from convert_pdf's response
    or list_conversions) — status comes from the DB usage row and the
    file from GET /convert/download/{log_id}, so the result survives
    backend restarts and later converts. 410/404 there mean the 3-hour
    retention window closed: the stored result is gone and converting
    again (fresh credits) is the only way to get it.

    Without log_id: polls the session status as before (free reads); if
    the server reports not_found, falls back to the most recent usage
    row and says which file was picked (plus a warning when it does not
    match source_path).

    Args:
        timeout_sec: Max seconds to wait (OCR can take many minutes).
        poll_interval_sec: Sleep between polls (default 3).
        output_dir: Where to write the .md file.
        source_path: Optional original file path (fallback filename check).
        log_id: Optional conversion id (UUID) — preferred, restart-proof.
    """
    if log_id:
        norm = _valid_log_id(log_id)
        if norm is None:
            return _err(
                f"invalid log_id {log_id!r}: expected a UUID "
                "(the log_id field convert_pdf returned)"
            )
        ok, payload = await _durable_wait_by_log_id(
            norm,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
            output_dir=output_dir,
        )
        payload.pop("row", None)
        if ok:
            return _ok(**payload)
        extra = {k: v for k, v in payload.items() if k != "error"}
        return _err(payload.get("error") or "download failed", **extra)

    try:
        status = await _poll_until_done(
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
            url_wait_sec=RESULT_URL_WAIT_SEC,
        )
    except PromptReadyAPIError as e:
        return _err(e.message, status_code=e.status_code, body=e.body)

    stage = status.get("stage") or status.get("status")
    if stage != "completed":
        if status.get("status") == "not_found" or stage == "not_found":
            return await _fallback_wait_and_download(
                timeout_sec=timeout_sec,
                poll_interval_sec=poll_interval_sec,
                output_dir=output_dir,
                source_path=source_path,
            )
        return _ok(
            finished=False,
            stage=stage,
            status=status,
            message=status.get("message") or f"stopped at stage={stage}",
        )

    saved = await _save_result_markdown(status, source_path or "output.pdf", output_dir)
    if not saved.get("ok"):
        return _err(saved.get("error") or "download failed", status=status)
    return _ok(
        finished=True,
        stage="completed",
        markdown_path=saved.get("markdown_path"),
        page_count=status.get("page_count"),
        filename=status.get("filename"),
        message=status.get("message"),
    )


async def _fallback_wait_and_download(
    *,
    timeout_sec: int,
    poll_interval_sec: float,
    output_dir: str,
    source_path: str,
) -> str:
    """Session job not_found → newest usage row: wait on it, disclose the pick.

    The pick is disclosed (file name, log_id, created_at) and a warning
    is attached when its file name differs from source_path, so the
    fallback never silently hands back a different document.
    """
    try:
        picked = await _latest_usage_row()
    except PromptReadyAPIError as e:
        return _err(e.message, status_code=e.status_code, body=e.body)
    except httpx.HTTPError as e:
        return _err(f"Network error: {e}")
    if picked is None:
        return _err(
            "no session job found and no usage history for this account — "
            "convert a file first (convert_pdf)"
        )

    picked_log_id = str(picked.get("id"))
    warnings: list = []
    if (
        source_path
        and picked.get("file_name")
        and Path(source_path).name != picked.get("file_name")
    ):
        warnings.append(
            f"picked the most recent conversion {picked.get('file_name')!r} "
            f"(log_id={picked_log_id}, created_at={picked.get('created_at')}), "
            f"which does NOT match source_path {Path(source_path).name!r} — "
            "pass log_id to target a specific conversion"
        )

    ok, payload = await _durable_wait_by_log_id(
        picked_log_id,
        timeout_sec=timeout_sec,
        poll_interval_sec=poll_interval_sec,
        output_dir=output_dir,
        fallback=True,
    )
    payload.pop("row", None)
    if warnings:
        payload["warnings"] = warnings
    if ok:
        return _ok(**payload)
    extra = {k: v for k, v in payload.items() if k != "error"}
    if warnings:
        extra.setdefault("warnings", warnings)
    return _err(payload.get("error") or "download failed", **extra)


def _status_has_markdown(status: Dict[str, Any]) -> bool:
    """True if the status payload already carries the result markdown."""
    result = status.get("result") or {}
    return bool(
        status.get("markdown")
        or status.get("markdown_url")
        or result.get("markdown")
        or result.get("markdown_url")
    )


async def _poll_until_done(
    *,
    timeout_sec: int = 3600,
    poll_interval_sec: float = 3.0,
    url_wait_sec: float = 0.0,
) -> Dict[str, Any]:
    """Poll GET /convert/status until a terminal stage is reached.

    With url_wait_sec > 0, a "completed" stage whose payload has no
    markdown_url/markdown yet is not treated as final: the backend can
    flip the stage a few seconds before the result URL appears, so keep
    re-checking for up to url_wait_sec (status reads are free — no
    credits). Callers that pass 0 keep the pre-0.3.6 behavior.
    """
    deadline = asyncio.get_event_loop().time() + max(1, timeout_sec)
    last: Dict[str, Any] = {}
    terminal = {"completed", "failed", "cancelled"}
    url_wait_started: Optional[float] = None

    while asyncio.get_event_loop().time() < deadline:
        data, _ = await _with_auth_retry(
            lambda base, token: client.get_conversion_status(base, token)
        )
        last = data
        stage = data.get("stage") or data.get("status")
        if stage in terminal:
            if (
                stage == "completed"
                and url_wait_sec > 0
                and not _status_has_markdown(data)
            ):
                now = asyncio.get_event_loop().time()
                if url_wait_started is None:
                    url_wait_started = now
                if now - url_wait_started < url_wait_sec and now < deadline:
                    await asyncio.sleep(max(0.5, poll_interval_sec))
                    continue
            return data
        if stage == "not_found" and last.get("status") == "not_found":
            # No job yet or lost — keep brief wait then return.
            await asyncio.sleep(poll_interval_sec)
            data2, _ = await _with_auth_retry(
                lambda base, token: client.get_conversion_status(base, token)
            )
            stage2 = data2.get("stage") or data2.get("status")
            if stage2 in terminal:
                return data2
            if stage2 == "not_found":
                return data2
            last = data2
            continue
        await asyncio.sleep(max(0.5, poll_interval_sec))

    last["timeout"] = True
    last["message"] = last.get("message") or f"Timed out after {timeout_sec}s"
    return last


async def _save_result_markdown(
    status: Dict[str, Any], source_path: str, output_dir: str
) -> Dict[str, Any]:
    out_dir = Path(output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = status.get("filename") or Path(source_path).name or "output.pdf"
    result = status.get("result") or {}
    engine_used = (
        status.get("engine_used")
        or result.get("engine_used")
        or status.get("engine")
        or (result.get("options") or {}).get("engine")
    )
    dest = out_dir / build_markdown_download_filename(name, engine_used)

    # Prefer inline markdown if ever present
    if status.get("markdown"):
        dest.write_text(str(status["markdown"]), encoding="utf-8")
        return {"ok": True, "markdown_path": str(dest.resolve())}

    url = status.get("markdown_url")
    if not url:
        url = result.get("markdown_url")
        if result.get("markdown"):
            dest.write_text(str(result["markdown"]), encoding="utf-8")
            return {"ok": True, "markdown_path": str(dest.resolve())}

    if not url:
        return {
            "ok": False,
            "error": "completed but no markdown_url/markdown in status payload",
        }

    try:
        await client.download_url_to_path(url, dest)
        return {"ok": True, "markdown_path": str(dest.resolve())}
    except PromptReadyAPIError as e:
        return {"ok": False, "error": e.message}


def main() -> None:
    """Run the MCP server over stdio."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
