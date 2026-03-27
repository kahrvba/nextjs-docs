#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_nextjs_docs_server.py"


def send(proc: subprocess.Popen, req_id: int, method: str, params: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params

    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()

    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(f"no response for {method}")
    return json.loads(line)


def unpack_text_tool_response(response: dict) -> dict:
    if "error" in response:
        raise RuntimeError(response["error"]["message"])
    content = response["result"]["content"]
    return json.loads(content[0]["text"])


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        init = send(proc, 1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "local-smoke-test", "version": "1.0"}})
        assert init["result"]["serverInfo"]["name"] == "nextjs-docs-mcp"
        assert init["result"]["serverInfo"]["version"] == "2.1.0"

        tools = send(proc, 2, "tools/list")
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        assert {"list_docs", "search_docs", "read_doc", "get_stats"} <= tool_names

        stats = unpack_text_tool_response(send(proc, 3, "tools/call", {"name": "get_stats", "arguments": {}}))
        assert stats["filesDiscovered"] > 0
        assert str(ROOT) == stats["root"]

        listed = unpack_text_tool_response(send(proc, 4, "tools/call", {"name": "list_docs", "arguments": {"pattern": "01-app/**/*.mdx", "limit": 5}}))
        assert listed["total"] > 0
        assert listed["results"]

        search = unpack_text_tool_response(
            send(
                proc,
                5,
                "tools/call",
                {
                    "name": "search_docs",
                    "arguments": {
                        "query": "mcp ai agents",
                        "limit": 5,
                        "searchMode": "both",
                        "rankingProfile": "semantic_lite",
                        "recallMode": "high_precision",
                    },
                },
            )
        )
        assert search["total"] > 0
        assert any(result["path"].endswith("01-app/02-guides/mcp.mdx") for result in search["results"])

        read = unpack_text_tool_response(
            send(
                proc,
                6,
                "tools/call",
                {"name": "read_doc", "arguments": {"path": "01-app/02-guides/mcp.mdx", "length": 4000}},
            )
        )
        assert read["path"] == "01-app/02-guides/mcp.mdx"
        assert "mcp" in read["content"].lower()

        resources = send(proc, 7, "resources/list")
        assert resources["result"]["resources"]

        print("MCP smoke test passed")
        print(json.dumps({"stats": stats, "topSearchPaths": [item["path"] for item in search["results"][:3]]}, ensure_ascii=False, indent=2))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
