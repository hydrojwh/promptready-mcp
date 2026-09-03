"""PromptReady MCP server entrypoint (stdio transport).

P0: get_credits
P1: convert_pdf, get_status, wait_and_download
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from . import client
from .client import PromptReadyAPIError
from .config import load_settings

logger = logging.getLogger("promptready_mcp")

mcp = FastMCP("promptready")

DEFAULT_OUT_DIR = "promptready-out"

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
        tokens = await client.refresh_access_token(
            settings.base_url, settings.refresh_token
        )
        # Update process env so subsequent tools see the new token.
        os.environ["PROMPTREADY_ACCESS_TOKEN"] = tokens["access_token"]
        if tokens.get("refresh_token"):
            os.environ["PROMPTREADY_REFRESH_TOKEN"] = tokens["refresh_token"]
        settings = load_settings()
        return await _call(settings.access_token), settings


@mcp.tool()
async def login(timeout_sec: int = 300) -> str:
    """Open the browser Google login page and save tokens for this machine.

    Starts a local callback on http://127.0.0.1:18765/callback, opens PromptReady
    Google OAuth, stores credentials under ~/.config/promptready/credentials.json
    (mode 600), and applies them to this process.

    Users should run this once (or use CLI: promptready-mcp-login) instead of
    pasting tokens manually. Requires Supabase redirect allowlist for that URL.
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
                "If OAuth redirect failed, add http://127.0.0.1:18765/callback to "
                "Supabase Auth redirect URLs, or run: promptready-mcp-login --email you@x.com"
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

    if not wait:
        result["next"] = "Call get_status or convert_pdf(..., wait=true)"
        return _ok(**result)

    # wait path
    waited = await _poll_until_done(timeout_sec=timeout_sec)
    result["status"] = waited
    if waited.get("stage") == "completed":
        saved = await _save_result_markdown(waited, path, output_dir)
        if saved.get("ok"):
            result["markdown_path"] = saved.get("markdown_path")
            result["download"] = "ok"
        else:
            result["download_error"] = saved.get("error")
    return _ok(**result)


@mcp.tool()
async def get_status() -> str:
    """Get conversion status for the authenticated user's current session job.

    Uses GET /api/v1/convert/status (session = user_id when logged in).
    """
    try:
        data, _ = await _with_auth_retry(
            lambda base, token: client.get_conversion_status(base, token)
        )
        return _ok(**data)
    except PromptReadyAPIError as e:
        return _err(e.message, status_code=e.status_code, body=e.body)
    except httpx.HTTPError as e:
        return _err(f"Network error: {e}")


@mcp.tool()
async def wait_and_download(
    timeout_sec: int = 3600,
    poll_interval_sec: float = 3.0,
    output_dir: str = DEFAULT_OUT_DIR,
    source_path: str = "",
) -> str:
    """Poll get_status until completed/failed/cancelled, then download markdown if available.

    Args:
        timeout_sec: Max seconds to wait (OCR can take many minutes).
        poll_interval_sec: Sleep between polls (default 3).
        output_dir: Where to write the .md file.
        source_path: Optional original file path (used only for output filename).
    """
    try:
        status = await _poll_until_done(
            timeout_sec=timeout_sec, poll_interval_sec=poll_interval_sec
        )
    except PromptReadyAPIError as e:
        return _err(e.message, status_code=e.status_code, body=e.body)

    stage = status.get("stage") or status.get("status")
    if stage != "completed":
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


async def _poll_until_done(
    *, timeout_sec: int = 3600, poll_interval_sec: float = 3.0
) -> Dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + max(1, timeout_sec)
    last: Dict[str, Any] = {}
    terminal = {"completed", "failed", "cancelled"}

    while asyncio.get_event_loop().time() < deadline:
        data, _ = await _with_auth_retry(
            lambda base, token: client.get_conversion_status(base, token)
        )
        last = data
        stage = data.get("stage") or data.get("status")
        if stage in terminal:
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
