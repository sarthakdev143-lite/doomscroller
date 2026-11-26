# Connecting an AI assistant (MCP)

The clipper is available as a remote **MCP server** at `https://<your-domain>/mcp` (local runs:
`http://127.0.0.1:8700/mcp`). It speaks the MCP *streamable HTTP* transport (stateless, JSON
responses) and authenticates with the same API keys as the REST API.

1. Get a key: **Settings → Developers → Create key** (Studio plan). Copy the `dsk_live_...` value; it is
   shown once. Pick scopes: `jobs:read`, `jobs:write`, `clips:read`, `clips:write`, `billing:read`
   (all by default). A read-only assistant only needs `jobs:read` + `clips:read`.
2. Add the server to your client with the header `Authorization: Bearer dsk_live_...` (snippets below).
3. Ask: *"Check how many credits it costs to clip https://youtu.be/XXXX, then make 3 Shorts and give me
   the download links when they're done."*

## Tools

| tool | what it does | scope |
|---|---|---|
| `get_balance` | credits left (1 credit = 1 minute of source video) and plan | `billing:read` |
| `estimate_video(url)` | title, duration, credit cost, or `ok:false` + reason; free | `jobs:read` |
| `create_clips(url, clips=3, language, layout, captions, broll, memes, sfx, music, max_clip_seconds)` | starts a job, reserves credits | `jobs:write` |
| `get_job(job_id)` | status, progress, credits charged, clips with signed download URLs | `jobs:read` |
| `list_jobs(status?, limit?)` | recent jobs | `jobs:read` |
| `list_clips(job_id?, limit?)` | finished clips with titles, descriptions, hashtags, links | `clips:read` |
| `get_clip_links(clip_id)` | fresh signed video / download / thumbnail URLs (~1 hour) | `clips:read` |
| `get_publish_package(clip_id, timezone?)` | per-platform copy (YouTube Shorts, Reels, TikTok) + suggested posting times + zip link | `clips:read` |
| `cancel_job(job_id)` | cancels a queued/running job; reserved credits return in full | `jobs:write` |

Jobs usually take 2-15 minutes; assistants should poll `get_job` every 30-60 seconds. We never post to
social platforms for you: download the MP4 (or the package zip) and post it yourself.

Errors come back as tool errors with a JSON body, e.g.
`{"error":"insufficient_credits","message":"This video needs 12 credits; you have 5."}`. Every call is
rate-limited per key (`SAAS_API_RATE_PER_MINUTE`, shared with `/api/v1`); a revoked key, a suspended
account or a plan without MCP access fails the very next request.

## Claude Code

```bash
claude mcp add --transport http clipper https://<your-domain>/mcp \
  --header "Authorization: Bearer dsk_live_YOUR_KEY"
```

Check it with `claude mcp list`, or `/mcp` inside a session.

## Claude Desktop

Settings → Connectors → **Add custom connector** → URL `https://<your-domain>/mcp`. If your Claude
Desktop build can't send a custom header to a custom connector, use the `mcp-remote` bridge in
`claude_desktop_config.json` (needs Node.js):

```json
{
  "mcpServers": {
    "clipper": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<your-domain>/mcp",
               "--header", "Authorization:Bearer ${CLIPPER_API_KEY}"],
      "env": { "CLIPPER_API_KEY": "dsk_live_YOUR_KEY" }
    }
  }
}
```

(The header is written `Authorization:Bearer ...` without a space after the colon because some
platforms split arguments on spaces; `mcp-remote` accepts it.)

## Cursor

`~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{
  "mcpServers": {
    "clipper": {
      "url": "https://<your-domain>/mcp",
      "headers": { "Authorization": "Bearer dsk_live_YOUR_KEY" }
    }
  }
}
```

## VS Code (GitHub Copilot agent mode)

`.vscode/mcp.json`:

```json
{
  "inputs": [{ "type": "promptString", "id": "clipper-key", "description": "Clipper API key", "password": true }],
  "servers": {
    "clipper": {
      "type": "http",
      "url": "https://<your-domain>/mcp",
      "headers": { "Authorization": "Bearer ${input:clipper-key}" }
    }
  }
}
```

## ChatGPT

ChatGPT's custom connectors (Settings → Connectors → Advanced → Developer mode → **Create**) take a
remote MCP URL. Use `https://<your-domain>/mcp`. ChatGPT connectors authenticate with OAuth or no
auth, not a static Bearer header, so until we ship OAuth for MCP, connect ChatGPT through a bridge that
adds the header (for example a small proxy you run that forwards to `/mcp` with
`Authorization: Bearer dsk_live_...`), or use the REST API from a GPT Action with API-key auth
(`Authorization: Bearer`, base URL `https://<your-domain>/api/v1`).

## Testing by hand

```bash
curl -s https://<your-domain>/mcp \
  -H "Authorization: Bearer dsk_live_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-protocol-version: 2025-06-18" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_balance","arguments":{}}}'
```

or point the MCP Inspector at it: `npx @modelcontextprotocol/inspector` → Transport "Streamable HTTP",
URL `.../mcp`, header `Authorization: Bearer dsk_live_...`.
