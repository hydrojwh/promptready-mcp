"""User-facing slash prompts (0.3.7).

Tools are invoked by agents; prompts are the human entry point — MCP
hosts surface them as slash commands (Claude Code: `/promptready:<name>`).
A prompt never runs code itself: it injects one short instruction that
makes the agent call exactly one tool, so the wording is the contract.

Hosts pass slash-command arguments positionally (whitespace-split, no
quoting), so every prompt argument must be optional — a prompt invoked
with no arguments must still render a useful instruction that tells the
agent to ask the user for the missing values in chat.
"""
from __future__ import annotations

from typing import Any

from .config import load_settings

# The six names are fixed by product decision (0.3.7): do not rename —
# users type these as /promptready:<name>.
PROMPT_NAMES = ("login", "logout", "credits", "convert", "site", "settings")


def login() -> str:
    """Log in to PromptReady (opens the browser Google sign-in page)."""
    return """Log the user into PromptReady.

Call the `login` tool now. It opens the browser Google sign-in page and
saves the credentials on this machine — tokens never enter the
conversation. When it returns, report the signed-in email.

If the user arrived here after a "Session expired: token refresh
failed" message, this login is the fix: once it succeeds, retry the
action that failed. If the browser flow itself keeps failing, suggest
the terminal fallback: `promptready-mcp-login --email you@x.com`.
"""


def logout() -> str:
    """Log out of PromptReady on this machine."""
    return """Log the user out of PromptReady on this machine.

Call the `logout` tool now. It removes the saved credentials file (if
present) and clears the session tokens used by this MCP server. Report
what it returned — no confirmation question is needed, the user just
asked for it.
"""


def credits() -> str:
    """Show your PromptReady credit balance."""
    return """Show the user's PromptReady credit balance.

Call the `get_credits` tool now and report the balance together with
the account email it returns. It requires a prior login (once per
machine); if it says "Not logged in", tell the user to run the `login`
prompt (/promptready:login) instead of pasting tokens.
"""


def convert(input_path: str = "", output_path: str = "") -> str:
    """Convert a PDF/CSV to Markdown. Arguments: input_path output_path.

    Both arguments are optional positional paths (no spaces — give such
    paths in chat instead). Omitted values are asked for in chat.
    """
    if input_path:
        path_line = f"- path: `{input_path}`"
    else:
        path_line = (
            "- path: not given — ask the user for the input PDF/CSV path "
            "first (never guess a path)"
        )
    if output_path:
        out_line = f"- output_dir: `{output_path}`"
    else:
        out_line = "- output_dir: `promptready-out` (default)"

    return f"""Convert a local PDF or CSV to Markdown with PromptReady.

Call the `convert_pdf` tool with `wait=true` (OCR can take minutes) and:
{path_line}
{out_line}

When it finishes, report the local markdown path to the user.

If the response reports `download="pending"`: the conversion itself
already succeeded and the credits were already spent — call
`wait_and_download` with the same output_dir to fetch the file. NEVER
call `convert_pdf` again for the same file: that queues a fresh
conversion and spends fresh credits.
"""


def site() -> str:
    """Show the PromptReady web app URL for the configured server."""
    base_url = load_settings().base_url
    return f"""Point the user at the PromptReady web app.

The configured PromptReady server is: {base_url}

Show that URL to the user as a markdown link. The web app shares the
same account and credits as this MCP server. No tool call is needed
for this.
"""


def settings() -> str:
    """Show — and if the user asked, change — saved convert defaults."""
    return """Show the user's saved PromptReady convert defaults.

Call the `get_convert_settings` tool and show the current defaults
(engine, tables, images) as a short list.

Only if the user also asked to change a default, call
`set_convert_settings` with exactly the fields to change (engine:
paddle | deepseek_ocr | glm_ocr). Otherwise stop after showing — do
not change anything on your own.
"""


def register_prompts(mcp: Any) -> None:
    """Register the six slash prompts on the FastMCP instance.

    Called from server.py so the prompts travel with the same server
    (name `promptready`) as the tools.
    """
    for fn in (login, logout, credits, convert, site, settings):
        mcp.prompt()(fn)
