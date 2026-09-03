# PromptReady MCP

<!-- mcp-name: io.github.hydrojwh/promptready-mcp -->

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
pip install promptready-mcp
```

Or run it without installing:

```bash
uvx promptready-mcp
```

<details>
<summary>From source</summary>

```bash
git clone https://github.com/hydrojwh/promptready-mcp.git
cd promptready-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

</details>

## Login (once per machine)

```bash
promptready-mcp-login
```

If you installed with `uvx`, the login command lives in the same package:

```bash
uvx --from promptready-mcp promptready-mcp-login
```

Either opens Google OAuth and saves credentials to
`~/.config/promptready/credentials.json` (file mode `0600`). That path is in your
home directory, so it survives `uvx` cache resets. You can also log in from inside
an MCP host by calling the `login` tool.

Supabase Auth must allow redirect:

`http://127.0.0.1:18765/callback`

<details>
<summary>Email login fallback</summary>

If browser-based Google login is not an option, the same command accepts email
and password:

```bash
promptready-mcp-login --email you@x.com
```

Leave out `--password` and you will be prompted for it instead — this keeps the
password out of your shell history.

</details>

Already have an access token? Set `PROMPTREADY_ACCESS_TOKEN` in the environment
(MCP host or shell). Environment variables take precedence over the saved
credentials file.

## MCP host config

### Grok

```toml
[mcp_servers.promptready]
command = "promptready-mcp"
enabled = true
tool_timeout_sec = 3600
```

### Claude Desktop / generic JSON

```json
{
  "mcpServers": {
    "promptready": {
      "command": "promptready-mcp"
    }
  }
}
```

No access token in config files required after login.

<details>
<summary>Host cannot find <code>promptready-mcp</code></summary>

GUI hosts start servers with a narrow `PATH`, so a console script installed by
`pip install --user` is often invisible to them — the host reports a spawn
failure or "server disconnected" rather than a missing command.

Two reliable fixes:

```json
{ "mcpServers": { "promptready": {
  "command": "uvx", "args": ["promptready-mcp"] } } }
```

or point at the absolute path of the script:
`/ABS/PATH/.venv/bin/promptready-mcp`.

</details>

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

Credits are deducted by the server when a job is queued, exactly as on the web
app. `convert_pdf(wait=True)` can run for a long time, so give the host a high
tool timeout.

## Security

- Tokens are **never** hardcoded in this repository.
- Do **not** commit `~/.config/promptready/*` or `.env`.
- Only use the official package linked from https://promptready.space
- Vulnerability reports: see [SECURITY.md](SECURITY.md)

## Service terms

Using the cloud API is subject to the
[Terms of Service](https://promptready.space/legal/terms-of-service.en.md) and
[Privacy Policy](https://promptready.space/legal/privacy-policy.en.md).
This MIT-licensed client does not grant free unlimited conversion.

## Smoke test (no account)

```bash
./scripts/smoke_stdio.sh
```

## License

MIT — see [LICENSE](LICENSE).
