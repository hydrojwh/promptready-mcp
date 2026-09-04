# Security Policy

## Supported versions

Security fixes are applied to the latest release of this MCP client on a
best-effort basis.

## What this project is

This repository is an **open-source API client** (Model Context Protocol server
over stdio). It talks to the PromptReady cloud API. It does **not** contain
server-side secrets, GPU workers, or payment backend code.

## Credentials on your machine

After `promptready-mcp-login`, tokens are stored only on **your** computer:

- `~/.config/promptready/credentials.json` (mode `0600`)
- Optional settings: `~/.config/promptready/settings.json`

**Never commit** these files, paste tokens into issues, or share them in chat logs.

## Tokens in the address bar after an interrupted login

The `login` tool and `promptready-mcp-login` wait for the OAuth redirect on a
local callback (`http://127.0.0.1:18765/callback`) and clear the address bar
from the callback page itself. If that wait **times out or the process is
stopped first** and you then complete the login in the browser, Supabase
redirects to a callback address that no longer exists and the **access and
refresh tokens stay in the browser address bar and history** — the page that
scrubs them never runs.

If this happens:

- Treat the session as compromised: **invalidate it** (sign out everywhere /
  revoke the session for this account), then log in again.
- Clear the affected browser history entry if it contains a `#access_token=`
  fragment.

The timeout is a deadline for the *whole* login, so keep it generous (default
300 s) or complete the login before it expires. Do not lower it to work around
a slow browser.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Email: **security@promptready.space** (or the contact listed on https://promptready.space)

Include:

- Description of the issue
- Steps to reproduce
- Affected version / commit
- Whether tokens or user data could be exposed

We will acknowledge receipt when possible and coordinate a fix before public disclosure.

## Scope

**In scope (this client):**

- Token handling and local storage
- Local OAuth callback on `127.0.0.1`
- Dependency / supply-chain issues in published packages

**Out of scope:**

- PromptReady cloud infrastructure (report via the website contact / ToS channels)
- Social engineering against end users
- Issues that require physical access to an unlocked machine

## Official distribution

Only install from the **official** GitHub repository and/or PyPI package linked
from https://promptready.space. Third-party mirrors may be malicious.
