# Next.js Docs MCP Setup

## Files

- `mcp_nextjs_docs_server.py`
- `scripts/test_mcp_nextjs_docs_server.py`

## Server

- Name: `nextjs-docs-mcp`
- Version: `2.1.0`
- Transport: stdio

## Tools

- `list_docs`
- `search_docs`
- `read_doc`
- `get_stats`

## Search scope

- `01-app/`
- `02-pages/`
- `03-architecture/`
- `04-community/`
- root files: `index.mdx`, `README.md`, `MCP_SETUP.md` when present

## Suggested Codex config

If your config uses `mcp_servers`:

```toml
[mcp_servers.nextjs_docs]
command = "python3"
args = ["/Users/ahmed/Downloads/Next.js-Docs/mcp_nextjs_docs_server.py"]
```

If your config uses `servers`:

```toml
[servers.nextjs_docs]
command = "python3"
args = ["/Users/ahmed/Downloads/Next.js-Docs/mcp_nextjs_docs_server.py"]
```

## Smoke test

Run:

```bash
python3 /Users/ahmed/Downloads/Next.js-Docs/scripts/test_mcp_nextjs_docs_server.py
```

## Notes

- The server is self-contained and does not depend on a `package.json`.
- `search_docs` keeps the `v2.1` ranking and recall controls:
  - `rankingProfile`: `semantic_lite`, `balanced`
  - `recallMode`: `high_precision`, `high_recall`
- `read_doc` supports chunked full reads via `offset` and `length`.
