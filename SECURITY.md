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
