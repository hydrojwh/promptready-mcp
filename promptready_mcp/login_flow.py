"""Interactive login for PromptReady MCP users.

Flows:
  1) browser (default): open Google OAuth, local callback captures tokens
  2) email: POST /api/v1/auth/signin

Tokens are stored via token_store (chmod 600) and applied to the environment.
"""
from __future__ import annotations

import asyncio
import errno
import json
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.request import Request, urlopen

from .config import DEFAULT_BASE_URL, load_settings
from .token_store import Credentials, apply_credentials_to_environ, save_credentials

# Fixed port so the local callback address is stable and documented.
# Loopback redirects need no provider-side registration (RFC 8252 §7.3),
# so --port NNNNN works too when this one is busy.
DEFAULT_CALLBACK_PORT = 18765
CALLBACK_PATH = "/callback"

# Cloudflare/WAF often blocks the default Python-urllib User-Agent (HTTP 403).
_HTTP_HEADERS = {
    "User-Agent": "PromptReady-MCP/0.3 (login; compatible)",
    "Accept": "application/json, text/html, */*",
}


def _api(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _http_json(url: str, *, data: Optional[bytes] = None, method: str = "GET") -> Dict[str, Any]:
    headers = dict(_HTTP_HEADERS)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def login_with_email(
    email: str,
    password: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> Credentials:
    """Email/password sign-in (same as web test path)."""
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    data = _http_json(
        _api(base_url, "/api/v1/auth/signin"),
        data=body,
        method="POST",
    )
    if not data.get("success") or not data.get("access_token"):
        raise RuntimeError(data.get("message") or "signin failed")
    user = data.get("user") or {}
    creds = Credentials(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        email=user.get("email") or email,
        user_id=user.get("id"),
        base_url=base_url,
    )
    save_credentials(creds)
    apply_credentials_to_environ(creds)
    return creds


def _fetch_google_auth_url(base_url: str, redirect_to: str) -> str:
    q = urllib.parse.urlencode({"redirect_to": redirect_to})
    url = _api(base_url, f"/api/v1/auth/google/url?{q}")
    data = _http_json(url)
    if not data.get("success") or not data.get("url"):
        raise RuntimeError(data.get("message") or "failed to get Google auth URL")
    return data["url"]


def _exchange_code(base_url: str, code: str) -> Credentials:
    q = urllib.parse.urlencode({"code": code})
    url = _api(base_url, f"/api/v1/auth/callback?{q}")
    data = _http_json(url)
    if not data.get("success") or not data.get("access_token"):
        raise RuntimeError(data.get("message") or "code exchange failed")
    user = data.get("user") or {}
    return Credentials(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        email=user.get("email"),
        user_id=user.get("id"),
        base_url=base_url,
    )


def _enrich_email_from_credits(creds: Credentials, *, timeout: float = 8.0) -> bool:
    """Best-effort: fill creds.email via GET /api/v1/users/credits.

    Reuses the async fetch_credits from .client (httpx is already a hard
    dependency); the callback handler thread has no running event loop, so
    asyncio.run is safe here. Never raises and never logs: a failed or slow
    lookup must not fail the login, and swallowed errors keep tokens out of
    logs and exception text. Returns True when an email was found.
    """
    if creds.email:
        return True
    try:
        from .client import fetch_credits

        data = asyncio.run(
            fetch_credits(
                creds.base_url or DEFAULT_BASE_URL,
                creds.access_token,
                timeout=timeout,
            )
        )
        email = str(data.get("email") or "").strip()
        if email:
            creds.email = email
            return True
    except Exception:
        pass
    return False


_CAPTURE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>PromptReady MCP Login</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 36rem; margin: 3rem auto; padding: 0 1rem; }
    .ok { color: #0a0; } .err { color: #a00; } code { background: #f4f4f4; padding: 0.1em 0.3em; }
  </style>
</head>
<body>
  <h1>PromptReady MCP</h1>
  <p id="msg">Completing login…</p>
  <script>
  (async function () {
    const msg = document.getElementById('msg');
    try {
      const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
      const query = new URLSearchParams(location.search);
      const payload = {};
      if (hash.get('access_token')) {
        payload.access_token = hash.get('access_token');
        payload.refresh_token = hash.get('refresh_token') || '';
      } else if (query.get('code')) {
        payload.code = query.get('code');
      } else if (query.get('error')) {
        throw new Error(query.get('error_description') || query.get('error'));
      } else {
        throw new Error('No access_token or code in callback URL. Please retry the login.');
      }
      const r = await fetch('/capture', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || 'capture failed');
      msg.className = 'ok';
      msg.innerHTML = 'Login successful' + (data.email ? ' as <code>' + data.email + '</code>' : '') +
        '. You can close this tab and return to your terminal or MCP client.';
      // Clear tokens from the address bar
      history.replaceState(null, '', '/done');
    } catch (e) {
      msg.className = 'err';
      msg.textContent = 'Login failed: ' + (e && e.message ? e.message : e);
    }
  })();
  </script>
</body>
</html>
"""

_DONE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Done</title></head>
<body style="font-family:system-ui;max-width:36rem;margin:3rem auto">
<h1>Logged in</h1><p>You can close this window.</p></body></html>
"""


def login_with_browser(
    *,
    base_url: str = DEFAULT_BASE_URL,
    port: int = DEFAULT_CALLBACK_PORT,
    open_browser: bool = True,
    timeout_sec: float = 300.0,
) -> Credentials:
    """Start local callback server, open Google login, wait for tokens."""
    redirect_to = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    auth_url = _fetch_google_auth_url(base_url, redirect_to)

    result: Dict[str, Any] = {"creds": None, "error": None}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return  # quiet

        def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in (CALLBACK_PATH, "/"):
                # If provider put code in query (no hash), still serve capture page
                self._send(200, _CAPTURE_HTML.encode("utf-8"))
                return
            if parsed.path == "/done":
                self._send(200, _DONE_HTML.encode("utf-8"))
                return
            self._send(404, b"not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/capture":
                self._send(404, b'{"ok":false}', "application/json")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
                if payload.get("access_token"):
                    creds = Credentials(
                        access_token=payload["access_token"],
                        refresh_token=payload.get("refresh_token") or None,
                        base_url=base_url,
                    )
                elif payload.get("code"):
                    creds = _exchange_code(base_url, payload["code"])
                else:
                    raise RuntimeError("missing access_token or code")
                # Best-effort email enrichment (see _enrich_email_from_credits):
                # creds are already saved, and a lookup failure must not fail
                # the login. Re-save only when an email was found.
                path = save_credentials(creds)
                if _enrich_email_from_credits(creds):
                    save_credentials(creds)
                apply_credentials_to_environ(creds)
                result["creds"] = creds
                result["path"] = str(path)
                body = json.dumps(
                    {"ok": True, "email": creds.email or ""},
                ).encode("utf-8")
                self._send(200, body, "application/json")
                # Allow response to flush before shutdown
                threading.Thread(target=lambda: (time.sleep(0.3), done.set()), daemon=True).start()
            except Exception as e:
                result["error"] = str(e)
                body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                self._send(400, body, "application/json")
                done.set()

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        # CPython maps WSAEADDRINUSE on Windows to errno.EADDRINUSE too.
        if e.errno == errno.EADDRINUSE:
            raise RuntimeError(
                f"Login callback port {port} is already in use — another "
                "login may still be running. Close it and try again, or "
                f"pick a different port: promptready-mcp-login --port {port + 1}"
            ) from None
        raise
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Diagnostics must go to stderr: under the MCP stdio transport, stdout is
    # reserved for JSON-RPC frames and any plain-text line corrupts the
    # protocol stream. stderr still reaches the terminal in CLI use, so the
    # login URL stays visible for `promptready-mcp-login`.
    print(f"Listening for OAuth callback on {redirect_to}", file=sys.stderr, flush=True)
    print(f"Open this URL if the browser does not open:\n{auth_url}\n", file=sys.stderr, flush=True)
    if open_browser:
        webbrowser.open(auth_url)

    finished = done.wait(timeout=timeout_sec)
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()

    if not finished:
        raise TimeoutError(
            f"Login timed out after {timeout_sec:.0f}s. Check the browser "
            "for the Google sign-in page (its URL was printed above). If "
            "browser login keeps failing, use email instead: "
            "promptready-mcp-login --email you@x.com. Note: if you complete "
            "the login in the browser afterwards, access and refresh tokens "
            "can be exposed in the browser address bar — invalidate that "
            "session (see SECURITY.md)."
        )
    if result.get("error"):
        raise RuntimeError(result["error"])
    creds = result.get("creds")
    if not creds:
        raise RuntimeError("login finished without credentials")
    return creds


def run_login_cli(argv: Optional[list] = None) -> int:
    """CLI entry: promptready-mcp-login [--email E --password P] [--port N]"""
    import argparse
    import getpass

    parser = argparse.ArgumentParser(description="Log in to PromptReady for MCP")
    parser.add_argument(
        "--base-url",
        default=None,
        help="API base URL (default: PROMPTREADY_BASE_URL env, then saved "
        "credentials, then built-in — same priority as the MCP server)",
    )
    parser.add_argument("--email", help="Email/password login instead of browser")
    parser.add_argument("--password", help="Password (or prompt)")
    parser.add_argument("--port", type=int, default=DEFAULT_CALLBACK_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    base_url = args.base_url or load_settings().base_url

    try:
        if args.email:
            password = args.password or getpass.getpass("Password: ")
            creds = login_with_email(args.email, password, base_url=base_url)
        else:
            creds = login_with_browser(
                base_url=base_url,
                port=args.port,
                open_browser=not args.no_browser,
                timeout_sec=args.timeout,
            )
        print("Login OK.", flush=True)
        if creds.email:
            print(f"  email: {creds.email}", flush=True)
        print("  credentials saved (chmod 600).", flush=True)
        print("  MCP/server will load them automatically.", flush=True)
        return 0
    except Exception as e:
        print(f"Login failed: {e}", flush=True)
        return 1
