# mcp-agent-playwright

Playwright browser automation for AI agents, as a local **MCP server**.
Navigate, read accessibility snapshots, click, type, screenshot, and evaluate
JavaScript in a real browser.

Build to be called by any MCP-aware agent harness (opencode, Claude, LM Studio
agents, etc.) over stdio. Works in two modes:

- **Own browser (default)** — lazily launches a fresh Playwright browser on
  first use and keeps it alive between tool calls.
- **CDP attach** — set `MCP_PLAYWRIGHT_CDP_ENDPOINT` to connect over Chrome
  DevTools Protocol to an already-running browser (Brave, Chrome, …). The
  server adopts the existing tabs and detaches on close — it never shuts the
  user's browser down.

## Tool surface

| Domain | Tools |
|---|---|
| Navigation | `browser_navigate`, `browser_go_back`, `browser_go_forward` |
| Page state | `browser_status`, `browser_snapshot`, `browser_console_messages` |
| Interaction | `browser_click`, `browser_type`, `browser_press_key`, `browser_hover`, `browser_select_option` |
| Tabs | `browser_tabs`, `browser_tab_new`, `browser_tab_switch`, `browser_tab_close` |
| Waiting | `browser_wait_for_text`, `browser_wait_for_timeout` |
| Capture | `browser_screenshot` |
| Escape hatch | `browser_evaluate` |
| Lifecycle | `browser_close` |

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Install

```powershell
git clone <repo-url> mcp_agent_playwright
cd mcp_agent_playwright
uv sync
```

Install a Playwright browser binary:

```powershell
uv run playwright install chromium
```

## Usage

### Run as an MCP server (stdio — what harnesses expect)

```powershell
uv run mcp-agent-playwright
```

### Register in an MCP client

```json
{
  "mcpServers": {
    "playwright": {
      "command": "uv",
      "args": ["--project", "/path/to/mcp_agent_playwright", "run", "mcp-agent-playwright"]
    }
  }
}
```

### Attach to a running browser (CDP mode)

Launch your browser with the debugging flag (example: Brave):

```powershell
Start-Process -FilePath "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" `
  -ArgumentList '--remote-debugging-port=9222','--remote-allow-origins=*'
```

Replace the `-FilePath` with your browser's actual path (Brave, Chrome, Edge,
or any Chromium-based browser).

Then set `MCP_PLAYWRIGHT_CDP_ENDPOINT` when starting the server (or in the
MCP client config):

```json
{
  "mcpServers": {
    "playwright": {
      "command": "uv",
      "args": ["--project", "/path/to/mcp_agent_playwright", "run", "mcp-agent-playwright"],
      "env": { "MCP_PLAYWRIGHT_CDP_ENDPOINT": "http://127.0.0.1:9222" }
    }
  }
}
```

`browser_status` reports which mode is active (`mode: cdp` vs
`mode: own-browser`). If the CDP endpoint is configured but unreachable, the
server falls back to launching its own browser and says so in the status note.

## Workflow

Start with `browser_navigate(url)`, then `browser_snapshot` to see the page as
a numbered accessibility tree. Interact using a `[ref]` number or a locator
string (`role=button,name=Submit`, `css=#id`, `text=Hello`). Always snapshot
again after actions.

## Configuration reference

| Var | Default | Purpose |
|---|---|---|
| `MCP_PLAYWRIGHT_HEADLESS` | `1` | `0` for a visible browser window (own-browser mode) |
| `MCP_PLAYWRIGHT_BROWSER` | `chromium` | `firefox` or `webkit` (own-browser mode only) |
| `MCP_PLAYWRIGHT_VIEWPORT` | `1280,900` | Browser viewport |
| `MCP_PLAYWRIGHT_CDP_ENDPOINT` | *(blank)* | CDP URL to attach to instead of launching a browser |
| `MCP_PLAYWRIGHT_SCREENSHOT_DIR` | `./screenshots` | Where `browser_screenshot` saves files |

## Development

```powershell
uv run ruff check .
uv run pytest -q
```

## License

MIT © 2026 Christof Milius