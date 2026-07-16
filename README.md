# PromptReady MCP

Official [Model Context Protocol](https://modelcontextprotocol.io/) client for
[PromptReady](https://promptready.space) — convert PDF/CSV to Markdown from AI
agents (Grok, Claude Code, Cursor, and other MCP hosts).

**Same PromptReady account and credits as the web app.**

## Features

- Browser Google login (tokens stay on your machine)
- `get_credits`, `convert_pdf`, `get_status`, `wait_and_download`
- Saved convert defaults (engine, tables, images) — not on every call
- Factory default: **PaddleOCR-VL**, tables on, images off

## Install

```bash
git clone https://github.com/hydrojwh/promptready-mcp.git
cd promptready-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Login (once per machine)

```bash
promptready-mcp-login
```

Opens Google OAuth and saves credentials to
`~/.config/promptready/credentials.json` (file mode `0600`).

Supabase Auth must allow redirect:

`http://127.0.0.1:18765/callback`

## MCP host config

### Grok

```toml
[mcp_servers.promptready]
command = "/ABS/PATH/promptready-mcp/.venv/bin/python"
args = ["-m", "promptready_mcp.server"]
cwd = "/ABS/PATH/promptready-mcp"
enabled = true
tool_timeout_sec = 3600
```

### Claude Desktop / generic JSON

```json
{
  "mcpServers": {
    "promptready": {
      "command": "/ABS/PATH/promptready-mcp/.venv/bin/python",
      "args": ["-m", "promptready_mcp.server"]
    }
  }
}
```

No access token in config files required after login.

## Tools

| Tool | Purpose |
|------|---------|
| `login` / `logout` | Browser auth / clear local credentials |
| `get_credits` | Credit balance |
| `get_convert_settings` / `set_convert_settings` | Saved convert defaults |
| `convert_pdf` | Upload path → queue (optional `wait`) |
| `get_status` | Job status |
| `wait_and_download` | Poll + save `.md` |

Downloaded names follow the web app: `{name}_PaddleOCR-VL.md` (engine label).

## Security

- Tokens are **never** hardcoded in this repository.
- Do **not** commit `~/.config/promptready/*` or `.env`.
- Only use the official package linked from https://promptready.space
- Vulnerability reports: see [SECURITY.md](SECURITY.md)

## Service terms

Using the cloud API is subject to
[PromptReady Terms](https://promptready.space) and Privacy Policy.
This MIT-licensed client does not grant free unlimited conversion.

## Smoke test (no account)

```bash
./scripts/smoke_stdio.sh
```

## License

MIT — see [LICENSE](LICENSE).
