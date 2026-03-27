#!/usr/bin/env python3
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
SEARCH_DIRS = [
    ROOT / "01-app",
    ROOT / "02-pages",
    ROOT / "03-architecture",
    ROOT / "04-community",
]
ROOT_INCLUDE_FILES = {
    ROOT / "index.mdx",
    ROOT / "README.md",
    ROOT / "MCP_SETUP.md",
}

TEXT_EXTENSIONS = {
    ".md",
    ".mdx",
    ".txt",
    ".json",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".d.ts",
    ".yml",
    ".yaml",
}

IGNORED_DIR_NAMES = {
    ".git",
    ".github",
    ".vscode",
    ".idea",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
}

MAX_SEARCH_LIMIT = 200
DEFAULT_SEARCH_LIMIT = 25
MAX_READ_CHUNK = 200_000
DEFAULT_READ_CHUNK = 50_000
MAX_INDEXED_FILE_BYTES = 2_000_000
MAX_QUERY_LENGTH = 512
MAX_QUERY_TOKENS = 12


@dataclass
class IndexedDoc:
    path: Path
    rel: str
    uri: str
    mime_type: str
    raw_text: str
    text: str
    text_lower: str
    size_bytes: int
    mtime_ns: int
    digest: str
    source_rel: Optional[str]


_DOC_PATHS_CACHE: Optional[List[Path]] = None
_DOCS_INDEX: Dict[str, IndexedDoc] = {}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _send(message: Dict[str, Any]) -> None:
    sys.stdout.write(_json_dumps(message) + "\n")
    sys.stdout.flush()


def _error_response(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _ok_response(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _is_in_search_scope(path: Path) -> bool:
    resolved = path.resolve()
    root_files = {p.resolve() for p in ROOT_INCLUDE_FILES if p.exists()}
    if resolved in root_files:
        return True

    for base in SEARCH_DIRS:
        if base.exists() and _is_relative_to(resolved, base):
            return True
    return False


def _is_text_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return True
    if path.name.endswith(".d.ts"):
        return True
    return False


def _should_skip_path(path: Path) -> bool:
    return any(part in IGNORED_DIR_NAMES for part in path.parts)


def _matches_pattern(rel: str, pattern: str) -> bool:
    pattern = pattern.strip()
    if not pattern:
        return True
    if fnmatch(rel, pattern):
        return True

    normalized = pattern.rstrip("/")
    if not any(ch in normalized for ch in "*?[]"):
        return rel == normalized or rel.startswith(normalized + "/")

    if normalized.endswith("/**/*"):
        base = normalized[: -len("/**/*")]
        return rel.startswith(base + "/")

    if normalized.endswith("/**"):
        base = normalized[: -len("/**")]
        return rel.startswith(base + "/")

    return False


def _to_doc_uri(path: Path) -> str:
    return f"docs://{path.relative_to(ROOT).as_posix()}"


def _from_doc_uri(uri: str) -> Path:
    if not uri.startswith("docs://"):
        raise ValueError("URI must start with docs://")

    rel = uri[len("docs://") :]
    candidate = (ROOT / rel).resolve()
    if not _is_in_search_scope(candidate):
        raise ValueError("URI points outside searchable scope")
    return candidate


def _mime_for(path: Path) -> str:
    if path.suffix.lower() in {".md", ".mdx"}:
        return "text/markdown"
    if _is_text_file(path):
        return "text/plain"
    return "application/octet-stream"


def _all_knowledge_files() -> List[Path]:
    global _DOC_PATHS_CACHE
    if _DOC_PATHS_CACHE is not None:
        return _DOC_PATHS_CACHE

    files: List[Path] = []

    for path in sorted(ROOT_INCLUDE_FILES):
        if path.exists() and path.is_file() and _is_text_file(path):
            files.append(path)

    for base in SEARCH_DIRS:
        if not base.exists():
            continue

        for path in base.rglob("*"):
            if not path.is_file() or not _is_text_file(path):
                continue
            if _should_skip_path(path):
                continue
            files.append(path)

    dedup: Dict[str, Path] = {}
    for path in files:
        dedup[path.relative_to(ROOT).as_posix()] = path

    _DOC_PATHS_CACHE = [dedup[key] for key in sorted(dedup.keys())]
    return _DOC_PATHS_CACHE


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _compute_digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _strip_order_prefix(part: str) -> str:
    return re.sub(r"^\d+-", "", part)


def _route_key_for_rel(rel: str) -> str:
    path = Path(rel)
    parts = list(path.parts)
    if not parts:
        return ""

    normalized: List[str] = []
    for part in parts[:-1]:
        normalized.append(_strip_order_prefix(part))

    stem = path.stem
    if stem != "index":
        normalized.append(_strip_order_prefix(stem))

    return "/".join(normalized)


def _extract_frontmatter_source(text: str) -> Optional[str]:
    if not text.startswith("---\n"):
        return None

    end = text.find("\n---\n", 4)
    if end == -1:
        return None

    frontmatter = text[4:end]
    match = re.search(r"(?m)^source:\s*([^\n]+)\s*$", frontmatter)
    if not match:
        return None
    return match.group(1).strip().strip("'\"")


def _find_path_by_route_key(route_key: str) -> Optional[Path]:
    for path in _all_knowledge_files():
        rel = path.relative_to(ROOT).as_posix()
        if _route_key_for_rel(rel) == route_key:
            return path
    return None


def _resolve_doc_text(path: Path, raw_text: str, visited: Optional[set[str]] = None) -> Tuple[str, Optional[str]]:
    rel = path.relative_to(ROOT).as_posix()
    visited = visited or set()
    if rel in visited:
        return raw_text, None

    visited.add(rel)
    source_slug = _extract_frontmatter_source(raw_text)
    if not source_slug:
        return raw_text, None

    source_path = _find_path_by_route_key(source_slug)
    if source_path is None:
        return raw_text, None

    source_rel = source_path.relative_to(ROOT).as_posix()
    source_raw = _read_text(source_path)
    resolved_text, nested_source_rel = _resolve_doc_text(source_path, source_raw, visited)
    return resolved_text, nested_source_rel or source_rel


def _index_doc(path: Path) -> Optional[IndexedDoc]:
    try:
        stat = path.stat()
    except OSError:
        return None

    size_bytes = int(stat.st_size)
    if size_bytes > MAX_INDEXED_FILE_BYTES:
        return None

    rel = path.relative_to(ROOT).as_posix()
    cached = _DOCS_INDEX.get(rel)
    if cached and cached.mtime_ns == int(stat.st_mtime_ns) and cached.size_bytes == size_bytes:
        return cached

    raw_text = _read_text(path)
    text, source_rel = _resolve_doc_text(path, raw_text)
    doc = IndexedDoc(
        path=path,
        rel=rel,
        uri=_to_doc_uri(path),
        mime_type=_mime_for(path),
        raw_text=raw_text,
        text=text,
        text_lower=text.lower(),
        size_bytes=size_bytes,
        mtime_ns=int(stat.st_mtime_ns),
        digest=_compute_digest(text),
        source_rel=source_rel,
    )
    _DOCS_INDEX[rel] = doc
    return doc


def _refresh_index() -> None:
    valid_rels = set()
    for path in _all_knowledge_files():
        rel = path.relative_to(ROOT).as_posix()
        valid_rels.add(rel)
        _index_doc(path)

    for rel in [key for key in _DOCS_INDEX.keys() if key not in valid_rels]:
        _DOCS_INDEX.pop(rel, None)


def _safe_snippet(text: str, idx: int, qlen: int, radius: int = 120) -> str:
    if idx < 0:
        snippet = text[: min(2 * radius, len(text))]
    else:
        start = max(0, idx - radius)
        end = min(len(text), idx + qlen + radius)
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        while end < len(text) and not text[end - 1].isspace():
            end += 1
        snippet = text[start:end].strip()
    return re.sub(r"\s+", " ", snippet)


def _normalize_paging(arguments: Dict[str, Any]) -> Tuple[int, int]:
    raw_offset = arguments.get("offset", 0)
    raw_limit = arguments.get("limit", DEFAULT_SEARCH_LIMIT)

    try:
        offset = int(raw_offset)
    except (TypeError, ValueError):
        offset = 0

    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = DEFAULT_SEARCH_LIMIT

    return max(0, offset), max(1, min(MAX_SEARCH_LIMIT, limit))


def _validate_query(query: str) -> str:
    query = query.strip()
    if not query:
        raise ValueError("query is required")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query is too long (max {MAX_QUERY_LENGTH} characters)")
    return query


def _tokenize(text: str) -> List[str]:
    return [token for token in re.split(r"[^a-z0-9_./-]+", text.lower()) if token]


def _first_positions(text: str, terms: List[str], cap: int = 6) -> Dict[str, List[int]]:
    positions: Dict[str, List[int]] = {}
    for term in terms:
        start = 0
        hits: List[int] = []
        while len(hits) < cap:
            idx = text.find(term, start)
            if idx == -1:
                break
            hits.append(idx)
            start = idx + len(term)
        if hits:
            positions[term] = hits
    return positions


def _near_bonus(positions: Dict[str, List[int]]) -> float:
    keys = list(positions.keys())
    if len(keys) < 2:
        return 0.0

    best = None
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            for p1 in positions[keys[i]]:
                for p2 in positions[keys[j]]:
                    dist = abs(p1 - p2)
                    best = dist if best is None or dist < best else best

    if best is None:
        return 0.0
    if best <= 24:
        return 1.0
    if best <= 80:
        return 0.6
    if best <= 180:
        return 0.25
    return 0.0


def _score_match(rel: str, text: str, text_lower: str, query: str, query_lower: str) -> Tuple[float, int, int]:
    rel_lower = rel.lower()
    score = 0.0

    path_index = rel_lower.find(query_lower)
    content_index = text_lower.find(query_lower)
    phrase_count = text_lower.count(query_lower)
    query_tokens = _tokenize(query)[:MAX_QUERY_TOKENS]

    if path_index != -1:
        score += 120.0
        if rel_lower.endswith(query_lower):
            score += 20.0
        if path_index == 0:
            score += 10.0

    if content_index != -1:
        score += 80.0
        score += min(35.0, phrase_count * 3.5)

    if query_tokens:
        path_hits = 0
        content_hits = 0
        tf_weight = 0.0
        token_positions = _first_positions(text_lower, query_tokens)

        for token in query_tokens:
            in_path = token in rel_lower
            token_count = text_lower.count(token)
            in_content = token_count > 0

            if in_path:
                path_hits += 1
            if in_content:
                content_hits += 1
            if token_count > 0:
                tf_weight += min(6.0, 1.0 + math.log1p(token_count))

        coverage = (path_hits + content_hits) / (2.0 * len(query_tokens))
        score += coverage * 70.0
        score += path_hits * 14.0
        score += content_hits * 10.0
        score += tf_weight * 3.2
        score += _near_bonus(token_positions) * 22.0

        title = rel_lower.rsplit("/", 1)[-1]
        title_hits = sum(1 for token in query_tokens if token in title)
        score += title_hits * 7.0

    return score, path_index, content_index


def _list_resources() -> List[Dict[str, Any]]:
    resources: List[Dict[str, Any]] = []
    for path in _all_knowledge_files():
        rel = path.relative_to(ROOT).as_posix()
        resources.append(
            {
                "uri": _to_doc_uri(path),
                "name": rel,
                "description": f"Next.js docs knowledge file: {rel}",
                "mimeType": _mime_for(path),
            }
        )
    return resources


def _read_resource(uri: str) -> Dict[str, Any]:
    path = _from_doc_uri(uri)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Resource not found: {uri}")

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": _mime_for(path),
                "text": _read_text(path),
            }
        ]
    }


def _tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": "list_docs",
            "description": "List Next.js docs knowledge file paths with pagination. Supports optional glob patterns like 01-app/**/*.mdx or 02-pages/**/*.mdx",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Optional glob pattern against repo-relative paths."},
                    "offset": {"type": "integer", "minimum": 0, "description": "Start index (default 0)."},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT,
                        "description": f"Page size (default {DEFAULT_SEARCH_LIMIT}, max {MAX_SEARCH_LIMIT}).",
                    },
                },
            },
        },
        {
            "name": "search_docs",
            "description": "Search Next.js docs knowledge files by path and content with semantic-lite relevance scoring and pagination.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to find in file paths or file contents."},
                    "offset": {"type": "integer", "minimum": 0, "description": "Start index in matches (default 0)."},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT,
                        "description": f"Page size (default {DEFAULT_SEARCH_LIMIT}, max {MAX_SEARCH_LIMIT}).",
                    },
                    "searchMode": {
                        "type": "string",
                        "enum": ["both", "path", "content"],
                        "description": "Search scope: both (default), path only, or content only.",
                    },
                    "rankingProfile": {
                        "type": "string",
                        "enum": ["semantic_lite", "balanced"],
                        "description": "Ranking profile (default semantic_lite).",
                    },
                    "recallMode": {
                        "type": "string",
                        "enum": ["high_precision", "high_recall"],
                        "description": "Result breadth mode (default high_precision).",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "read_doc",
            "description": "Read one Next.js docs knowledge file by path with optional chunking via offset/length.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repo-relative path, such as 01-app/02-guides/mcp.mdx or 02-pages/02-guides/testing/playwright.mdx",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Character offset for chunked reads (default 0).",
                    },
                    "length": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_READ_CHUNK,
                        "description": f"Characters to return (default {DEFAULT_READ_CHUNK}, max {MAX_READ_CHUNK}).",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "get_stats",
            "description": "Get MCP index and corpus statistics for diagnostics.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _tool_list_docs(arguments: Dict[str, Any]) -> Dict[str, Any]:
    pattern = str(arguments.get("pattern", "")).strip()
    offset, limit = _normalize_paging(arguments)

    items: List[Dict[str, Any]] = []
    for path in _all_knowledge_files():
        rel = path.relative_to(ROOT).as_posix()
        if not _matches_pattern(rel, pattern):
            continue
        items.append({"path": rel, "uri": _to_doc_uri(path)})

    total = len(items)
    page = items[offset : offset + limit]
    payload = {
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": (offset + limit) < total,
        "nextOffset": offset + limit if (offset + limit) < total else None,
        "results": page,
    }
    return {"content": [{"type": "text", "text": _json_dumps(payload)}]}


def _tool_search_docs(arguments: Dict[str, Any]) -> Dict[str, Any]:
    query = _validate_query(str(arguments.get("query", "")))
    offset, limit = _normalize_paging(arguments)
    search_mode = str(arguments.get("searchMode", "both")).strip().lower() or "both"
    if search_mode not in {"both", "path", "content"}:
        raise ValueError("searchMode must be one of: both, path, content")
    ranking_profile = str(arguments.get("rankingProfile", "semantic_lite")).strip().lower() or "semantic_lite"
    if ranking_profile not in {"semantic_lite", "balanced"}:
        raise ValueError("rankingProfile must be one of: semantic_lite, balanced")
    recall_mode = str(arguments.get("recallMode", "high_precision")).strip().lower() or "high_precision"
    if recall_mode not in {"high_precision", "high_recall"}:
        raise ValueError("recallMode must be one of: high_precision, high_recall")

    query_lower = query.lower()
    query_tokens = _tokenize(query)[:MAX_QUERY_TOKENS]
    min_token_hits = 1 if recall_mode == "high_recall" or len(query_tokens) <= 2 else 2
    results = []

    _refresh_index()

    for rel, doc in _DOCS_INDEX.items():
        rel_lower = rel.lower()
        phrase_path_hit = query_lower in rel_lower
        phrase_content_hit = query_lower in doc.text_lower

        token_path_hits = sum(1 for token in query_tokens if token in rel_lower)
        token_content_hits = sum(1 for token in query_tokens if token in doc.text_lower)
        token_hit_total = token_path_hits + token_content_hits

        if not phrase_path_hit and not phrase_content_hit and token_hit_total < min_token_hits:
            continue

        path_hit = phrase_path_hit or token_path_hits > 0
        content_hit = phrase_content_hit or token_content_hits > 0

        if search_mode == "path" and not path_hit:
            continue
        if search_mode == "content" and not content_hit:
            continue
        if search_mode == "both" and not (path_hit or content_hit):
            continue

        score, path_idx, content_idx = _score_match(rel, doc.text, doc.text_lower, query, query_lower)
        if ranking_profile == "balanced":
            score *= 0.92 if path_hit and not content_hit else 1.0

        snippet = ""
        if content_hit:
            snippet_idx = content_idx
            snippet_len = len(query)
            if snippet_idx < 0 and query_tokens:
                token_positions = [doc.text_lower.find(token) for token in query_tokens]
                token_positions = [pos for pos in token_positions if pos >= 0]
                if token_positions:
                    snippet_idx = min(token_positions)
                    snippet_len = len(query_tokens[0])
            snippet = _safe_snippet(doc.text, snippet_idx, snippet_len)

        results.append(
            {
                "path": rel,
                "uri": doc.uri,
                "pathMatch": path_hit,
                "contentMatch": content_hit,
                "pathTokenHits": token_path_hits,
                "contentTokenHits": token_content_hits,
                "snippet": snippet,
                "score": score,
                "_pathIdx": path_idx,
            }
        )

    results.sort(key=lambda item: (-item["score"], item["_pathIdx"] if item["_pathIdx"] >= 0 else 10**9, item["path"]))

    total = len(results)
    page = results[offset : offset + limit]
    for item in page:
        item.pop("_pathIdx", None)

    payload = {
        "query": query,
        "searchMode": search_mode,
        "rankingProfile": ranking_profile,
        "recallMode": recall_mode,
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": (offset + limit) < total,
        "nextOffset": offset + limit if (offset + limit) < total else None,
        "results": page,
    }
    return {"content": [{"type": "text", "text": _json_dumps(payload)}]}


def _tool_read_doc(arguments: Dict[str, Any]) -> Dict[str, Any]:
    rel = str(arguments.get("path", "")).strip()
    if not rel:
        raise ValueError("path is required")

    candidate = (ROOT / rel).resolve()
    if not _is_in_search_scope(candidate):
        raise ValueError("path must be inside 01-app/, 02-pages/, 03-architecture/, 04-community/, or allowed root files")
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(f"File not found: {rel}")

    raw_offset = arguments.get("offset", 0)
    raw_length = arguments.get("length", DEFAULT_READ_CHUNK)

    try:
        offset = int(raw_offset)
    except (TypeError, ValueError):
        offset = 0

    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        length = DEFAULT_READ_CHUNK

    offset = max(0, offset)
    length = max(1, min(MAX_READ_CHUNK, length))

    raw_text = _read_text(candidate)
    text, source_rel = _resolve_doc_text(candidate, raw_text)
    total_chars = len(text)
    content = text[offset : offset + length]
    payload = {
        "path": candidate.relative_to(ROOT).as_posix(),
        "uri": _to_doc_uri(candidate),
        "resolvedFromSource": bool(source_rel),
        "sourcePath": source_rel,
        "totalChars": total_chars,
        "offset": offset,
        "length": length,
        "hasMore": (offset + length) < total_chars,
        "nextOffset": offset + length if (offset + length) < total_chars else None,
        "content": content,
    }
    return {"content": [{"type": "text", "text": _json_dumps(payload)}]}


def _tool_get_stats(_: Dict[str, Any]) -> Dict[str, Any]:
    _refresh_index()
    files_count = len(_all_knowledge_files())
    indexed_count = len(_DOCS_INDEX)
    total_indexed_bytes = sum(doc.size_bytes for doc in _DOCS_INDEX.values())

    payload = {
        "root": str(ROOT),
        "searchDirs": [str(path) for path in SEARCH_DIRS],
        "filesDiscovered": files_count,
        "filesIndexed": indexed_count,
        "filesSkippedForSize": max(0, files_count - indexed_count),
        "maxIndexedFileBytes": MAX_INDEXED_FILE_BYTES,
        "totalIndexedBytes": total_indexed_bytes,
    }
    return {"content": [{"type": "text", "text": _json_dumps(payload)}]}


def _handle_request(req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {})

    if method == "initialize":
        return _ok_response(
            req_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "nextjs-docs-mcp", "version": "2.1.0"},
            },
        )

    if method == "notifications/initialized":
        return _ok_response(req_id, {})

    if method == "resources/list":
        return _ok_response(req_id, {"resources": _list_resources()})

    if method == "resources/read":
        uri = params.get("uri")
        if not uri:
            return _error_response(req_id, -32602, "Missing required parameter: uri")
        try:
            return _ok_response(req_id, _read_resource(str(uri)))
        except Exception as exc:
            return _error_response(req_id, -32000, str(exc))

    if method == "tools/list":
        return _ok_response(req_id, {"tools": _tool_definitions()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error_response(req_id, -32602, "arguments must be an object")

        try:
            if name == "list_docs":
                return _ok_response(req_id, _tool_list_docs(arguments))
            if name == "search_docs":
                return _ok_response(req_id, _tool_search_docs(arguments))
            if name == "read_doc":
                return _ok_response(req_id, _tool_read_doc(arguments))
            if name == "get_stats":
                return _ok_response(req_id, _tool_get_stats(arguments))
            return _error_response(req_id, -32601, f"Unknown tool: {name}")
        except Exception as exc:
            return _error_response(req_id, -32000, str(exc))

    return _error_response(req_id, -32601, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "id" not in req:
            continue

        _send(_handle_request(req))


if __name__ == "__main__":
    main()
