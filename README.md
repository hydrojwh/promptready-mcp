# PromptReady MCP

<!-- mcp-name: io.github.hydrojwh/promptready-mcp -->

Official [Model Context Protocol](https://modelcontextprotocol.io/) client for
[PromptReady](https://promptready.space) — convert PDF/CSV to Markdown from AI
agents (Grok, Claude Code, Cursor, and other MCP hosts).

**Same PromptReady account and credits as the web app.**

> **Data flow**: your local PDF/CSV files are uploaded to the PromptReady cloud
> (`promptready.space`) for OCR processing. Converted Markdown is downloaded back
> to your machine. Files are auto-deleted from the server 3 hours after your
> batch finishes. No long-term storage.

## Features

- Browser Google login (tokens stay on your machine)
- `get_credits`, `convert_pdf`, `get_status`, `wait_and_download`
- Slash commands for humans: `/promptready:convert` and five more (below)
- Saved convert defaults (engine, tables, images) — not on every call
- Factory default: **PaddleOCR-VL**, tables on, images off

## Install

### As a Claude Code plugin (recommended)

Once listed in the [Claude Code plugin directory](https://claude.ai/directory):
```
/plugin install promptready
```

### As a standalone MCP server
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

After you sign in, the browser returns to `http://127.0.0.1:18765/callback` — a
local page started by the login command. No Supabase configuration is needed.

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

### Fastest: let your AI agent install it

If you are already in an MCP-capable agent, skip the JSON editing and just
ask:

> Install the PromptReady MCP server for me. The PyPI package is
> `promptready-mcp` (stdio command `promptready-mcp`). Add it to your MCP
> config, then I will run the `login` tool.

In Claude Code the agent can use the built-in CLI:

```bash
claude mcp add promptready -- promptready-mcp
```

### Reconnect after changing config

Hosts do not pick up MCP config changes mid-session. After changing the
config, restart the host or reconnect the server — in Claude Code, open the
`/mcp` panel and reconnect.

The `/mcp` panel shows server status and lists the connected servers'
tools, but it does **not** run them: picking a tool in that list will
not invoke it. Tools are invoked through normal conversation — ask the
agent to convert a file and it calls `convert_pdf` for you. If a call
fails, reconnect from the panel first.

### Claude Code

```bash
claude mcp add promptready -- promptready-mcp
```

The default scope is `local` (this project only). Use `--scope user` to
register it for all your projects, or `--scope project` to share the
registration through a committed `.mcp.json`.

### Cursor

Add to `~/.cursor/mcp.json` (or `.cursor/mcp.json` for a single project):

```json
{
  "mcpServers": {
    "promptready": {
      "command": "promptready-mcp"
    }
  }
}
```

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

## Slash commands

MCP *prompts* are the human entry point: instead of describing what you
want, you pick a command. Hosts that surface prompts as slash commands
(Claude Code does) show them as `/promptready:<name>`:

| Slash command | Purpose |
|---------------|---------|
| `/promptready:login` | Log in (opens the browser Google sign-in) |
| `/promptready:logout` | Log out on this machine |
| `/promptready:credits` | Show your credit balance |
| `/promptready:convert` | Convert a PDF/CSV to Markdown |
| `/promptready:site` | Show the PromptReady web app URL |
| `/promptready:settings` | Show — and optionally change — convert defaults |

Each command expands to a short instruction; the agent then calls the
matching tool (`login`, `get_credits`, `convert_pdf`, …) for you.

`/promptready:convert` optionally takes two positional arguments, input
path then output directory. Pick the command from the slash menu (hosts
may list it as `promptready:convert (MCP)`) and append the arguments:

```text
/promptready:convert (MCP) report.pdf markdown-out
```

Arguments are split on whitespace and cannot be quoted, so paths with
spaces do not fit on the command line — run the command bare and give
the paths in chat instead. Line breaks and control characters in
arguments are rejected outright (0.3.8): an argument is interpolated
into the instruction the command expands to, and it must never be able
to start a line of its own. With no arguments the command asks you for
them. The conversion itself always goes through the `convert_pdf` tool:
never re-run it for the same file while a download is pending — that
queues a fresh conversion and spends fresh credits; the expanded command
tells the agent to call `wait_and_download` instead.

Hosts that do not map prompts to slash commands simply ignore this
section; the tools keep working as before.

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
