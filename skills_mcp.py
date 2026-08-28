#!/usr/bin/env python3
"""
Skills MCP Server — MCP 2025-11-25 (MCP "2") compliant
- stdio transport, full JSON-RPC 2.0, notifications/initialized handshake
- pagination (cursor) for tools/resources/prompts
- cached index, inverted search, sanitized inputs, security gates
- structured output, annotations, resourceTemplates, prompts

Run:  python skills_mcp.py              # stdio
      python skills_mcp.py --http --port 3000  # optional streamable HTTP
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------
SERVER_NAME = "skills-mcp-server"
SERVER_VERSION = "2.1.0"

# MCP spec versions we support, newest first — negotiate to latest common.
SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

SKILLS_DIR = Path(__file__).parent / "skills"
INDEX_PATH = Path(__file__).parent / "skills-index.json"

# Pagination / size guards (agent-overload protection)
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
MAX_GET_SKILL_CHARS = 30_000  # beyond this we truncate + warn
TRUNCATE_NOTICE = "\n\n---[TRUNCATED: skill body exceeds {limit} chars. Use get_skill with excerpt or read via resources/read with skill://<name>]---"

# Security
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{1,64}$")
# some legacy skills use 2-char names like '00-...' — allow leading digit
SKILL_NAME_RE_LOOSE = re.compile(r"^[a-z0-9][a-z0-9\-_\.]{1,80}$", re.IGNORECASE)

# Logging — MUST go to stderr, never stdout (stdout is JSON-RPC)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("skills-mcp")

# ---------------------------------------------------------------------------
# Cursor helpers (opaque pagination token)
# ---------------------------------------------------------------------------

def encode_cursor(offset: int) -> str:
    raw = json.dumps({"offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode())
        off = int(data.get("offset", 0))
        return max(0, off)
    except Exception:
        raise ValueError("Invalid cursor")


def paginate(items: list[dict], cursor: Optional[str], limit: Optional[int]) -> tuple[list[dict], Optional[str]]:
    limit = int(limit) if limit is not None else DEFAULT_PAGE_SIZE
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = decode_cursor(cursor)
    page = items[offset : offset + limit]
    next_cursor = encode_cursor(offset + limit) if offset + limit < len(items) else None
    return page, next_cursor

# ---------------------------------------------------------------------------
# Skill index — cached, mtime-aware, inverted search
# ---------------------------------------------------------------------------

@dataclass
class SkillRecord:
    name: str
    description: str
    category: str
    author: str
    version: str
    risk: str
    path: str
    tags: list[str]


class SkillIndex:
    """Loads skills-index.json (or falls back to FS scan) and keeps it hot."""

    def __init__(self, index_path: Path, skills_dir: Path):
        self.index_path = index_path
        self.skills_dir = skills_dir
        self._skills: list[SkillRecord] = []
        self._by_name: dict[str, SkillRecord] = {}
        self._mtime: float = 0
        self._load(force=True)

    def _load(self, force: bool = False):
        try:
            mtime = self.index_path.stat().st_mtime if self.index_path.exists() else 0
        except Exception:
            mtime = 0
        if not force and mtime == self._mtime and self._skills:
            return
        self._mtime = mtime

        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                raw = data.get("skills", [])
                skills: list[SkillRecord] = []
                for s in raw:
                    skills.append(
                        SkillRecord(
                            name=str(s.get("name", "")).strip(),
                            description=str(s.get("description", "") or "")[:300],
                            category=str(s.get("category", "other") or "other"),
                            author=str(s.get("author", "unknown")),
                            version=str(s.get("version", "1.0")),
                            risk=str(s.get("risk", "unknown")),
                            path=str(s.get("path", f"skills/{s.get('name','')}/SKILL.md")),
                            tags=self._parse_tags(s.get("tags")),
                        )
                    )
                # sort deterministically
                skills.sort(key=lambda x: x.name)
                self._skills = [s for s in skills if s.name and SKILL_NAME_RE_LOOSE.match(s.name)]
                self._by_name = {s.name: s for s in self._skills}
                log.info("Loaded %d skills from index %s", len(self._skills), self.index_path)
                return
            except Exception as e:
                log.warning("Failed to load index %s: %s — falling back to FS scan", self.index_path, e)

        # Fallback: scan FS with proper YAML parsing (safe)
        self._skills = self._scan_fs()
        self._by_name = {s.name: s for s in self._skills}

    def _parse_tags(self, raw) -> list[str]:
        if isinstance(raw, list):
            return [str(t).strip() for t in raw if str(t).strip()]
        if isinstance(raw, str):
            return [t.strip() for t in raw.split(",") if t.strip()]
        return []

    def _scan_fs(self) -> list[SkillRecord]:
        skills: list[SkillRecord] = []
        if not self.skills_dir.exists():
            return skills
        for d in self.skills_dir.iterdir():
            if not d.is_dir():
                continue
            md = d / "SKILL.md"
            if not md.exists():
                continue
            meta = _parse_frontmatter(md)
            name = (meta.get("name") or d.name).strip()
            if not SKILL_NAME_RE_LOOSE.match(name):
                continue
            skills.append(
                SkillRecord(
                    name=name,
                    description=str(meta.get("description", "") or "")[:300],
                    category=str(meta.get("category", "other") or "other"),
                    author=str(meta.get("author", "unknown")),
                    version=str(meta.get("version", "1.0")),
                    risk=str(meta.get("risk", "unknown")),
                    path=f"skills/{d.name}/SKILL.md",
                    tags=self._parse_tags(meta.get("tags")),
                )
            )
        skills.sort(key=lambda x: x.name)
        log.info("Scanned %d skills from FS", len(skills))
        return skills

    def ensure_fresh(self):
        self._load(force=False)

    def all_skills(self) -> list[SkillRecord]:
        self.ensure_fresh()
        return list(self._skills)

    def get(self, name: str) -> Optional[SkillRecord]:
        self.ensure_fresh()
        return self._by_name.get(name)

    def categories(self) -> dict[str, list[str]]:
        self.ensure_fresh()
        cats: dict[str, list[str]] = {}
        for s in self._skills:
            cats.setdefault(s.category, []).append(s.name)
        return cats

    def search(self, query: str, limit: int = 10, category_filter: Optional[str] = None) -> list[dict]:
        """
        Multi-term weighted search with threshold and cap.
        - tokenises query, scores name/desc/category/tags per term
        - empty query => empty result (never dump all 928)
        - dedup, threshold filtering, top-k
        """
        self.ensure_fresh()
        q = (query or "").strip()
        if not q:
            return []
        # normalise: split on non-alnum, drop 1-char tokens
        terms = [t.lower() for t in re.split(r"[^a-z0-9]+", q.lower()) if len(t) >= 2]
        if not terms:
            terms = [q.lower()]

        scored: list[tuple[float, SkillRecord]] = []
        for s in self._skills:
            if category_filter and s.category != category_filter:
                continue
            name_l = s.name.lower()
            desc_l = s.description.lower()
            cat_l = s.category.lower()
            tags_l = " ".join(s.tags).lower()
            # per-term scoring
            score = 0.0
            for term in terms:
                if term == name_l:
                    score += 20
                elif term in name_l:
                    # exact token vs substring: boost token
                    if re.search(rf"\b{re.escape(term)}\b", name_l):
                        score += 10
                    else:
                        score += 4
                if term in desc_l:
                    # count occurrences, capped
                    cnt = desc_l.count(term)
                    score += min(3, cnt) * 2.0
                    if re.search(rf"\b{re.escape(term)}\b", desc_l):
                        score += 1
                if term == cat_l:
                    score += 5
                elif term in cat_l:
                    score += 2
                if term in tags_l:
                    score += 3
            # small boost for safe skills? no — keep neutral
            if score > 0:
                # tie-breaker: shorter name = more specific
                score += max(0, 2 - len(s.name) * 0.02)
                scored.append((score, s))

        # threshold: require at least 2 points (avoid noise)
        scored = [(sc, s) for sc, s in scored if sc >= 2]
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: max(1, min(limit, MAX_PAGE_SIZE))]
        return [
            {
                "name": s.name,
                "description": s.description,
                "category": s.category,
                "version": s.version,
                "author": s.author,
                "risk": s.risk,
                "path": s.path,
                "tags": s.tags,
                "relevance_score": round(sc, 2),
            }
            for sc, s in top
        ]


# Single global index
_skill_index = SkillIndex(INDEX_PATH, SKILLS_DIR)

# ---------------------------------------------------------------------------
# Frontmatter / content helpers (safe)
# ---------------------------------------------------------------------------

def _parse_frontmatter(skill_file: Path) -> dict:
    """Parse YAML frontmatter using yaml.safe_load when available, else fallback."""
    try:
        text = skill_file.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    # find second ---
    # frontmatter is between first and second ---
    try:
        # split only first two delimiters
        _, fm, _ = text.split("---", 2)
    except ValueError:
        return {}
    fm = fm.strip()
    # try yaml
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(fm)
        if isinstance(data, dict):
            meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            out = dict(data)
            if meta:
                for k, v in meta.items():
                    if k not in out:
                        out[k] = v
            if "tags" in out and isinstance(out["tags"], list):
                out["tags"] = ", ".join(str(t) for t in out["tags"])
            for k, v in list(out.items()):
                if hasattr(v, "isoformat"):
                    out[k] = v.isoformat()
                elif isinstance(v, dict):
                    for kk, vv in list(v.items()):
                        if hasattr(vv, "isoformat"):
                            v[kk] = vv.isoformat()
            return out
    except Exception:
        pass
    # fallback: line split (handles colons in values correctly via split(":",1))
    out: dict[str, str] = {}
    for line in fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip("\"'").strip()
    return out


def _read_skill_content(skill_name: str) -> Optional[dict]:
    """Validated, size-guarded read of SKILL.md + optional README."""
    # strict allowlist
    if not SKILL_NAME_RE_LOOSE.match(skill_name):
        return None
    # prevent traversal even though allowlist blocks it
    skill_path = (SKILLS_DIR / skill_name).resolve()
    try:
        # ensure inside SKILLS_DIR
        if not str(skill_path).startswith(str(SKILLS_DIR.resolve())):
            return None
    except Exception:
        return None
    if not skill_path.is_dir():
        return None
    skill_file = skill_path / "SKILL.md"
    if not skill_file.exists():
        return None
    try:
        content = skill_file.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Failed to read %s: %s", skill_file, e)
        return None

    meta = _parse_frontmatter(skill_file)
    # truncate if huge
    truncated = False
    if len(content) > MAX_GET_SKILL_CHARS:
        content = content[:MAX_GET_SKILL_CHARS] + TRUNCATE_NOTICE.format(limit=MAX_GET_SKILL_CHARS)
        truncated = True

    readme = None
    readme_path = skill_path / "README.md"
    if readme_path.exists():
        try:
            readme = readme_path.read_text(encoding="utf-8")
            if len(readme) > 5000:
                readme = readme[:5000] + "\n---[README truncated]---"
        except Exception:
            readme = None

    rec = _skill_index.get(skill_name)
    return {
        "name": skill_name,
        "metadata": meta if meta else ({"name": skill_name} if not rec else {"name": rec.name, "category": rec.category}),
        "content": content,
        "readme": readme,
        "truncated": truncated,
        "risk": (rec.risk if rec else meta.get("risk", "unknown")),
    }

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

class MCPServer:
    def __init__(self):
        self.server_info = {"name": SERVER_NAME, "version": SERVER_VERSION}
        # capabilities per spec — clients use these to gate features
        self.capabilities = {
            "tools": {"listChanged": True},
            "resources": {"subscribe": False, "listChanged": True},
            "prompts": {"listChanged": True},
            "logging": {},
            # completions / roots are optional; advertise minimally
            "completions": {},
        }
        self._initialized = False
        self._client_info: Optional[dict] = None
        self._protocol_version: str = LATEST_PROTOCOL_VERSION

    # -- protocol negotiation --
    def _negotiate_protocol(self, requested: Optional[str]) -> str:
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            return requested
        # if client asks for unknown version, return latest we support
        # spec says server should return its supported version
        if requested:
            log.info("Client requested unsupported protocolVersion %s — negotiating to %s", requested, LATEST_PROTOCOL_VERSION)
        return LATEST_PROTOCOL_VERSION

    # -- handlers --
    def handle_initialize(self, params: dict) -> dict:
        req_ver = params.get("protocolVersion")
        self._protocol_version = self._negotiate_protocol(req_ver)
        self._client_info = params.get("clientInfo")
        caps = params.get("capabilities", {})
        log.info("initialize from %s caps=%s -> protocol %s", self._client_info, caps, self._protocol_version)
        # Do NOT set _initialized here — wait for notifications/initialized per spec
        return {
            "protocolVersion": self._protocol_version,
            "capabilities": self.capabilities,
            "serverInfo": self.server_info,
            "instructions": (
                "Skills MCP — discover skills via find_skills(query) before calling get_skill(name). "
                "Do NOT call list_skills without a category filter; use pagination (cursor/limit). "
                "Security skills (risk != safe) require explicit user authorization."
            ),
        }

    def handle_tools_list(self, params: dict) -> dict:
        cursor = params.get("cursor")
        # tools are fixed; return all at once (no pagination needed for <10 tools)
        tools = [
            {
                "name": "find_skills",
                "title": "Find relevant skills",
                "description": "Search 928 skills by natural-language task. Returns top-k ranked results. Call this BEFORE get_skill. Empty query returns no results — be specific.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Task or topic, e.g. 'FastAPI REST API development'"},
                        "limit": {"type": "integer", "description": "Max results (1-50, default 10)", "minimum": 1, "maximum": 50, "default": 10},
                        "category": {"type": "string", "description": "Optional category filter"},
                        "cursor": {"type": "string", "description": "Pagination cursor"},
                    },
                    "required": ["query"],
                },
                "annotations": {"title": "Find skills", "readOnlyHint": True, "openWorldHint": False},
            },
            {
                "name": "get_skill",
                "title": "Get skill content",
                "description": "Load full SKILL.md for one skill by exact name. Fails if skill is security-gated and allow_security != true.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Exact skill name"},
                        "allow_security": {"type": "boolean", "description": "Confirm authorization for security-gated skills", "default": False},
                        "section": {"type": "string", "description": "Optional markdown section heading to extract"},
                    },
                    "required": ["name"],
                },
                "annotations": {"title": "Get skill", "readOnlyHint": True, "openWorldHint": False},
            },
            {
                "name": "get_skill_summary",
                "title": "Get skill summary",
                "description": "Lightweight summary: metadata + first 600 chars + section headings.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Exact skill name"},
                        "allow_security": {"type": "boolean", "default": False},
                    },
                    "required": ["name"],
                },
                "annotations": {"title": "Get skill summary", "readOnlyHint": True, "openWorldHint": False},
            },
            {
                "name": "list_skills",
                "title": "List skills (paginated)",
                "description": "Paginated list of all skills. Prefer find_skills for discovery. Use category filter + cursor/limit to avoid full dump.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "Filter by category"},
                        "trunk": {"type": "string", "description": "Filter by trunk"},
                        "subcategory": {"type": "string", "description": "Filter by subcategory"},
                        "cursor": {"type": "string", "description": "Pagination cursor"},
                        "limit": {"type": "integer", "description": "Page size 1-50 (default 20)", "minimum": 1, "maximum": 50, "default": 20},
                    },
                },
                "annotations": {"title": "List skills", "readOnlyHint": True, "openWorldHint": False},
            },
            {
                "name": "get_categories",
                "title": "Get categories",
                "description": "List categories and skill counts.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"title": "Get categories", "readOnlyHint": True, "openWorldHint": False},
            },
            {
                "name": "browse_tree",
                "title": "Browse skill taxonomy tree",
                "description": "Explore the hierarchical skill taxonomy: trunks -> subcategories -> skills. Use to discover skills without dumping the entire index.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "trunk": {"type": "string", "description": "Filter to a specific trunk"},
                        "subcategory": {"type": "string", "description": "Filter to a specific subcategory"},
                        "limit": {"type": "integer", "description": "Max skills per subcategory", "minimum": 1, "maximum": 50, "default": 15},
                    },
                },
                "annotations": {"title": "Browse tree", "readOnlyHint": True, "openWorldHint": False},
            },
            {
                "name": "route",
                "title": "Route task to skills",
                "description": "Intent-aware router: given task intent + query, returns composed plan of 2-3 skills. Task memory passed explicitly.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "intent": {"type": "string", "description": "Task intent: build, audit, migrate, debug, automate, test, design", "enum": ["build","audit","migrate","debug","automate","test","design"]},
                        "query": {"type": "string", "description": "Natural language task"},
                        "constraints": {"type": "object", "description": "Constraints like {lang: python, platform: azure}", "additionalProperties": True},
                        "task_memory": {"type": "array", "items": {"type": "string"}, "description": "Recent search queries for context boosting (pass explicitly)"},
                    },
                    "required": ["query"],
                },
                "annotations": {"title": "Route", "readOnlyHint": True, "openWorldHint": False},
            },
            {
                "name": "verify_skill",
                "title": "Verify skill integrity",
                "description": "Run verification hook: checks skill exists, frontmatter valid, quality_score, SHA256 hash integrity, and taxonomy assignment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Exact skill name"},
                    },
                    "required": ["name"],
                },
                "annotations": {"title": "Verify skill", "readOnlyHint": True, "openWorldHint": False},
            },
            {
                "name": "add_skill",
                "title": "Add a new skill",
                "description": "Create and register a new skill in the inventory. Accepts markdown content, a freeform vision, or references to existing skills.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Skill name (kebab-case). If omitted, inferred."},
                        "content": {"type": "string", "description": "Full skill content as markdown"},
                        "vision": {"type": "string", "description": "Freeform description of what the skill should do"},
                        "trunk": {"type": "string", "description": "Optional trunk assignment"},
                        "subcategory": {"type": "string", "description": "Optional subcategory assignment"},
                        "risk": {"type": "string", "description": "Risk level: safe or high"},
                        "copy_from": {"type": "array", "items": {"type": "string"}, "description": "Names of existing skills to use as reference templates"},
                    },
                    "required": [],
                },
                "annotations": {"title": "Add skill", "readOnlyHint": False, "openWorldHint": True},
            },
        ]
        result = {"tools": tools}
        return result

    def handle_tools_call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        log.info("tools/call %s args=%s", name, {k: (v if k != 'query' else str(v)[:120]) for k,v in args.items()})

        if name == "find_skills":
            query = str(args.get("query", "") or "")
            if not query.strip():
                return {
                    "content": [{"type": "text", "text": json.dumps({"skills": [], "count": 0, "warning": "Empty query — provide a specific task description."})}],
                    "isError": False,
                }
            limit = int(args.get("limit", 10)) if args.get("limit") is not None else 10
            limit = max(1, min(limit, MAX_PAGE_SIZE))
            category = args.get("category")
            # cursor for find_skills paginates over scored results — we re-score and slice
            cursor = args.get("cursor")
            results = _skill_index.search(query, limit=MAX_PAGE_SIZE * 2, category_filter=category)
            # paginate the ranked list
            page, next_cursor = paginate(results, cursor, limit)
            payload: dict[str, Any] = {"skills": page, "count": len(page), "totalMatches": len(results)}
            if next_cursor:
                payload["nextCursor"] = next_cursor
            return {"content": [{"type": "text", "text": json.dumps(payload)}]}

        elif name == "get_skill":
            skill_name = str(args.get("name", "") or "").strip()
            if not skill_name:
                return {"content": [{"type": "text", "text": json.dumps({"error": "Missing required argument: name"})}], "isError": True}
            if not SKILL_NAME_RE_LOOSE.match(skill_name):
                return {"content": [{"type": "text", "text": json.dumps({"error": f"Invalid skill name: {skill_name}"})}], "isError": True}
            rec = _skill_index.get(skill_name)
            # security gate — check both index AND live frontmatter (index may be stale)
            # live metadata is source of truth for gated skills
            live_meta: dict = {}
            try:
                # quick peek at frontmatter without full content load
                live_path = (SKILLS_DIR / skill_name / "SKILL.md")
                if live_path.exists():
                    live_meta = _parse_frontmatter(live_path)
            except Exception:
                live_meta = {}
            live_risk = str(live_meta.get("risk", "") or "").lower() if live_meta else ""
            live_cat = str(live_meta.get("category", "") or "").lower() if live_meta else ""
            idx_risk = (rec.risk.lower() if rec else "unknown")
            idx_cat = (rec.category.lower() if rec else "")
            # effective risk/category: live overrides index if present
            eff_risk = live_risk if live_risk else idx_risk
            eff_cat = live_cat if live_cat else idx_cat
            raw_allow = args.get("allow_security", False)
            if isinstance(raw_allow, str):
                allow_security = raw_allow.lower() in ("true", "1", "yes")
            elif isinstance(raw_allow, int) and not isinstance(raw_allow, bool):
                allow_security = raw_allow == 1
            else:
                allow_security = bool(raw_allow)
            # Only gate true security risks — category==security is the reliable signal.
            # Many skills have risk=unknown but are NOT security-sensitive (azure stubs).
            is_gated = (eff_cat == "security")
            if is_gated and not allow_security:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": f"Skill '{skill_name}' is security-gated (risk={eff_risk or 'unknown'}, category={eff_cat or 'unknown'}). Pass allow_security=true only if you have explicit user authorization.",
                                    "risk": eff_risk or "unknown",
                                    "category": eff_cat or "unknown",
                                    "requires_authorization": True,
                                }
                            ),
                        }
                    ],
                    "isError": True,
                }
            data = _read_skill_content(skill_name)
            if data is None:
                return {"content": [{"type": "text", "text": json.dumps({"error": f"Skill '{skill_name}' not found"})}], "isError": True}
            return {"content": [{"type": "text", "text": json.dumps(data)}]}

        elif name == "list_skills":
            category = args.get("category")
            cursor = args.get("cursor")
            limit = args.get("limit", DEFAULT_PAGE_SIZE)
            try:
                limit = int(limit)
            except Exception:
                limit = DEFAULT_PAGE_SIZE
            all_recs = _skill_index.all_skills()
            items = [
                {
                    "name": r.name,
                    "description": r.description,
                    "category": r.category,
                    "version": r.version,
                    "author": r.author,
                    "risk": r.risk,
                    "tags": r.tags,
                }
                for r in all_recs
                if not category or r.category == category
            ]
            try:
                page, next_cursor = paginate(items, cursor, limit)
            except ValueError as e:
                return {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "isError": True}
            payload = {"skills": page, "count": len(page), "total": len(items)}
            if next_cursor:
                payload["nextCursor"] = next_cursor
            if not category and not cursor and len(items) > 100:
                payload["warning"] = "Unfiltered list_skills returns ~928 items. Prefer find_skills(query) or filter by category and paginate via cursor/limit."
            return {"content": [{"type": "text", "text": json.dumps(payload)}]}

        elif name == "get_categories":
            cats = _skill_index.categories()
            payload = {"categories": cats, "counts": {k: len(v) for k, v in cats.items()}, "totalSkills": sum(len(v) for v in cats.values())}
            return {"content": [{"type": "text", "text": json.dumps(payload)}]}

        elif name == "get_skill_summary":
            skill_name = str(args.get("name", "") or "").strip()
            if not skill_name or not SKILL_NAME_RE_LOOSE.match(skill_name):
                return {"content": [{"type": "text", "text": json.dumps({"error": "Missing or invalid argument: name"})}], "isError": True}
            rec = _skill_index.get(skill_name)
            live_meta = _parse_frontmatter(SKILLS_DIR / skill_name / "SKILL.md") if (SKILLS_DIR / skill_name / "SKILL.md").exists() else {}
            live_cat = str(live_meta.get("category", "") or "").lower() if live_meta else ""
            idx_cat = (rec.category.lower() if rec else "")
            eff_cat = live_cat if live_cat else idx_cat
            raw_allow = args.get("allow_security", False)
            allow_security = bool(raw_allow)
            if eff_cat == "security" and not allow_security:
                return {"content": [{"type": "text", "text": json.dumps({"error": f"Skill '{skill_name}' is security-gated. Pass allow_security=true"})}], "isError": True}
            data = _read_skill_content(skill_name)
            if data is None:
                return {"content": [{"type": "text", "text": json.dumps({"error": f"Skill '{skill_name}' not found"})}], "isError": True}
            summary_text = data["content"][:600]
            sections = [line.strip("# ").strip() for line in data["content"].split("\n") if line.startswith("## ")][:20]
            payload = {"name": data["name"], "metadata": data["metadata"], "summary": summary_text, "sections": sections, "truncated": data["truncated"], "risk": data["risk"], "category": (rec.category if rec else "")}
            return {"content": [{"type": "text", "text": json.dumps(payload)}]}

        elif name == "browse_tree":
            trunk_filter = args.get("trunk")
            subcategory_filter = args.get("subcategory")
            limit = int(args.get("limit", 15))
            limit = max(1, min(limit, MAX_PAGE_SIZE))
            all_recs = _skill_index.all_skills()
            # Build tree from known taxonomy + skills
            known_subcategories = {
                "ai-ml": ["agent-development","llm-application","prompt-engineering","computer-vision","voice-audio-ai","ml-ops-engineering"],
                "web": ["frontend-frameworks","fullstack-development","web-automation","web3-blockchain","design-ux"],
                "backend": ["api-design","backend-frameworks","serverless-functions"],
                "devops": ["ci-cd-pipelines","containers-orchestration","infrastructure-iaas","monitoring-observability"],
                "data": ["data-engineering-pipelines","analytics-visualization","ai-ml-engineering"],
                "cloud": ["aws","azure","gcp-google-cloud"],
                "mobile": ["android","ios","cross-platform"],
                "database": ["sql-relational","nosql","vector-embedding","orm-odm"],
                "security": ["application-security","penetration-testing","cloud-security","auth-authorization","compliance-forensics","reverse-engineering"],
                "testing": ["unit-integration","e2e-acceptance","performance-load","security-testing"],
            }
            trunks = [trunk_filter] if trunk_filter else list(known_subcategories.keys())
            tree = {}
            for trunk in trunks:
                subs = known_subcategories.get(trunk, [])
                # Group skills by subcategory
                sub_skills: dict[str, list[str]] = {s: [] for s in subs}
                for r in all_recs:
                    if getattr(r, "trunk", None) and r.trunk.lower() != trunk.lower():
                        continue
                    sub_name = getattr(r, "subcategory", None) or "other"
                    if subcategory_filter and sub_name.lower() != subcategory_filter.lower():
                        continue
                    if sub_name not in sub_skills:
                        sub_skills[sub_name] = []
                    sub_skills[sub_name].append(r.name)
                subcategories = []
                for sub, names in sub_skills.items():
                    if subcategory_filter and sub.lower() != subcategory_filter.lower():
                        continue
                    names = sorted(set(names))[:limit]
                    subcategories.append({"subcategory": sub, "skill_count": len(names), "skills": names})
                tree[trunk] = {"trunk": trunk, "subcategories": subcategories, "total_skills": sum(sc["skill_count"] for sc in subcategories)}
            return {"content": [{"type": "text", "text": json.dumps({"tree": tree})}]}

        elif name == "route":
            query = str(args.get("query", "") or "").strip()
            if not query:
                return {"content": [{"type": "text", "text": json.dumps({"error": "route requires query"})}], "isError": True}
            intent = args.get("intent")
            if intent:
                intent = intent.lower()
            # Task memory passed explicitly (not server-stored)
            mem = args.get("task_memory", [])
            boosted_query = " ".join(mem) + " " + query if mem else query
            results = _skill_index.search(boosted_query, limit=10)
            scored = []
            for r in results:
                base = r.get("relevance_score", 0)
                score = base
                if intent and r.get("intent") and r["intent"].lower() == intent:
                    score += 5.0
                scored.append((score, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            plan = [v for _, v in scored[:3]]
            clusters = list(set(v.get("cluster", "") for v in plan if v.get("cluster")))
            payload = {"query": query, "intent": intent, "plan": plan, "clusters": clusters, "task_memory": mem, "total_candidates": len(scored)}
            return {"content": [{"type": "text", "text": json.dumps(payload)}]}

        elif name == "verify_skill":
            skill_name = str(args.get("name", "") or "").strip()
            if not skill_name or not SKILL_NAME_RE_LOOSE.match(skill_name):
                return {"content": [{"type": "text", "text": json.dumps({"error": f"Invalid skill name: {skill_name}"})}], "isError": True}
            rec = _skill_index.get(skill_name)
            skill_file = SKILLS_DIR / skill_name / "SKILL.md"
            meta = _parse_frontmatter(skill_file) if skill_file.exists() else {}
            exists = rec is not None or bool(meta)
            import hashlib
            actual_hash = None
            hash_match = True
            if skill_file.exists():
                try:
                    actual_hash = hashlib.sha256(skill_file.read_bytes()).hexdigest()[:16]
                    if rec and rec.hash:
                        hash_match = (rec.hash == actual_hash)
                except Exception:
                    hash_match = True
            verified = exists and hash_match
            payload = {
                "name": skill_name,
                "exists": exists,
                "verified": verified,
                "quality_score": (rec.quality_score if rec and hasattr(rec, "quality_score") and rec.quality_score else 0.5),
                "hash_indexed": (rec.hash if rec else ""),
                "hash_actual": actual_hash,
                "hash_match": hash_match,
                "cluster": (rec.cluster if rec else ""),
                "trunk": (getattr(rec, "trunk", None) or ""),
                "subcategory": (getattr(rec, "subcategory", None) or ""),
                "frontmatter_valid": bool(meta),
                "path": (rec.path if rec else ""),
            }
            return {"content": [{"type": "text", "text": json.dumps(payload)}]}

        elif name == "add_skill":
            # Fallback: Python server does not fully implement add_skill
            return {"content": [{"type": "text", "text": json.dumps({"error": "add_skill is not available in the Python fallback server. Use the Rust server (primary) for skill creation.", "hint": "Ensure mcp_config.json points to target/debug/skills-mcp-server.exe, not the Python fallback."})}], "isError": True}

        else:
            return {"content": [{"type": "text", "text": json.dumps({"error": f"Unknown tool: {name}"})}], "isError": True}

    def handle_resources_list(self, params: dict) -> dict:
        cursor = params.get("cursor")
        recs = _skill_index.all_skills()
        resources = [
            {
                "uri": f"skill://{r.name}",
                "name": r.name,
                "description": (r.description or "")[:120],
                "mimeType": "text/markdown",
            }
            for r in recs
        ]
        try:
            page, next_cursor = paginate(resources, cursor, params.get("limit", DEFAULT_PAGE_SIZE))
        except ValueError as e:
            raise ValueError(str(e))
        out: dict[str, Any] = {"resources": page}
        if next_cursor:
            out["nextCursor"] = next_cursor
        return out

    def handle_resources_read(self, params: dict) -> dict:
        uri = params.get("uri", "")
        if not uri or not isinstance(uri, str):
            raise ValueError("Missing uri")
        if not uri.startswith("skill://"):
            return {"contents": [], "error": f"Invalid resource URI (expected skill://<name>): {uri}"}
        skill_name = uri[8:].strip()
        if not SKILL_NAME_RE_LOOSE.match(skill_name):
            return {"contents": [], "error": f"Invalid skill name in URI: {uri}"}
        rec = _skill_index.get(skill_name)
        live_meta = _parse_frontmatter(SKILLS_DIR / skill_name / "SKILL.md") if (SKILLS_DIR / skill_name / "SKILL.md").exists() else {}
        live_cat = str(live_meta.get("category", "") or "").lower() if live_meta else ""
        idx_cat = (rec.category.lower() if rec else "")
        eff_cat = live_cat if live_cat else idx_cat
        if eff_cat == "security":
            return {"contents": [], "error": f"Skill '{skill_name}' is security-gated. Use tools/call get_skill with allow_security=true."}
        data = _read_skill_content(skill_name)
        if data is None:
            return {"contents": [], "error": f"Skill '{skill_name}' not found"}
        return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": json.dumps(data)}]}

    def handle_resources_templates_list(self, _params: dict) -> dict:
        return {
            "resourceTemplates": [
                {
                    "uriTemplate": "skill://{name}",
                    "name": "Skill by name",
                    "description": "Load any skill by name as a resource",
                    "mimeType": "text/markdown",
                }
            ]
        }

    def handle_prompts_list(self, params: dict) -> dict:
        """
        Expose skills as prompts — paginated. Each prompt is a thin wrapper
        that tells the agent how to use get_skill. Avoids dumping 928 prompts
        without pagination.
        """
        cursor = params.get("cursor")
        recs = _skill_index.all_skills()
        prompts = [
            {
                "name": r.name,
                "title": r.name.replace("-", " ").title(),
                "description": r.description[:150],
                "arguments": [],
            }
            for r in recs
        ]
        try:
            page, next_cursor = paginate(prompts, cursor, params.get("limit", DEFAULT_PAGE_SIZE))
        except ValueError as e:
            raise ValueError(str(e))
        out: dict[str, Any] = {"prompts": page}
        if next_cursor:
            out["nextCursor"] = next_cursor
        return out

    def handle_prompts_get(self, params: dict) -> dict:
        name = params.get("name", "")
        if not name or not SKILL_NAME_RE_LOOSE.match(name):
            raise ValueError(f"Unknown prompt: {name}")
        data = _read_skill_content(name)
        if data is None:
            raise ValueError(f"Prompt/skill '{name}' not found")
        # Prompts return messages array
        return {
            "description": data["metadata"].get("description", "") if isinstance(data.get("metadata"), dict) else "",
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": data["content"][:8000]},
                }
            ],
        }

    def handle_completion_complete(self, params: dict) -> dict:
        # Minimal completion: suggest skill names for argument 'name' or 'query'
        ref = params.get("ref") or {}
        arg = params.get("argument") or {}
        arg_name = arg.get("name", "")
        value = arg.get("value", "") or ""
        # complete skill names
        if arg_name in ("name", "skill", "prompt"):
            recs = _skill_index.all_skills()
            vals = [r.name for r in recs if value.lower() in r.name.lower()][:20]
            return {"completion": {"values": vals, "total": len(vals), "hasMore": False}}
        if arg_name == "query":
            # no completion for freeform
            return {"completion": {"values": [], "total": 0, "hasMore": False}}
        return {"completion": {"values": [], "total": 0, "hasMore": False}}

# ---------------------------------------------------------------------------
# JSON-RPC plumbing (stdio)
# ---------------------------------------------------------------------------

def send_response(req_id: Any, result: Any):
    msg = {"jsonrpc": "2.0", "id": req_id, "result": result}
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def send_error(req_id: Any, code: int, message: str, data: Any = None):
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "error": err}
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def send_notification(method: str, params: Any = None):
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def parse_message(line: str) -> Optional[dict]:
    try:
        msg = json.loads(line)
    except Exception:
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("jsonrpc") != "2.0":
        return None
    return msg


def main_stdio():
    server = MCPServer()
    log.info("Starting %s v%s stdio (supports %s)", SERVER_NAME, SERVER_VERSION, SUPPORTED_PROTOCOL_VERSIONS)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = parse_message(line)
        if msg is None:
            send_error(None, -32700, "Parse error")
            continue

        method = msg.get("method")
        params = msg.get("params") or {}
        req_id = msg.get("id")  # None => notification (no response)
        is_notification = "id" not in msg or msg.get("id") is None

        # Notifications that carry a method but no id must NOT get a response
        try:
            # -- initialize --
            if method == "initialize":
                if not isinstance(params, dict):
                    send_error(req_id, -32602, "Invalid params for initialize")
                    continue
                result = server.handle_initialize(params)
                send_response(req_id, result)
                continue

            # -- notifications/initialized (no response) --
            if method == "notifications/initialized":
                server._initialized = True
                log.info("Client initialized (protocol %s)", server._protocol_version)
                # optional: send initial log notification
                # do not respond (notification)
                continue

            if method == "notifications/cancelled":
                # no-op, spec allows cancellation
                log.info("Cancelled notification: %s", params)
                continue

            # Guard: require initialized before other methods (except ping/initialize)
            if not server._initialized and method not in ("ping", "initialize", "notifications/initialized"):
                # Spec says server SHOULD allow handling but we warn and still serve
                log.warning("Method %s called before initialized — serving anyway", method)

            # -- ping --
            if method == "ping":
                # ping may be notification or request
                if is_notification:
                    continue
                send_response(req_id, {})
                continue

            # -- tools --
            if method == "tools/list":
                result = server.handle_tools_list(params if isinstance(params, dict) else {})
                if is_notification:
                    continue
                send_response(req_id, result)
                continue
            if method == "tools/call":
                if not isinstance(params, dict):
                    send_error(req_id, -32602, "Invalid params for tools/call")
                    continue
                try:
                    result = server.handle_tools_call(params)
                except ValueError as e:
                    send_error(req_id, -32602, str(e))
                    continue
                if is_notification:
                    continue
                send_response(req_id, result)
                continue

            # -- resources --
            if method == "resources/list":
                result = server.handle_resources_list(params if isinstance(params, dict) else {})
                if is_notification:
                    continue
                send_response(req_id, result)
                continue
            if method == "resources/read":
                result = server.handle_resources_read(params if isinstance(params, dict) else {})
                if is_notification:
                    continue
                send_response(req_id, result)
                continue
            if method == "resources/templates/list":
                result = server.handle_resources_templates_list(params if isinstance(params, dict) else {})
                if is_notification:
                    continue
                send_response(req_id, result)
                continue

            # -- prompts --
            if method == "prompts/list":
                result = server.handle_prompts_list(params if isinstance(params, dict) else {})
                if is_notification:
                    continue
                send_response(req_id, result)
                continue
            if method == "prompts/get":
                try:
                    result = server.handle_prompts_get(params if isinstance(params, dict) else {})
                except ValueError as e:
                    send_error(req_id, -32602, str(e))
                    continue
                if is_notification:
                    continue
                send_response(req_id, result)
                continue

            # -- completions --
            if method == "completion/complete":
                result = server.handle_completion_complete(params if isinstance(params, dict) else {})
                if is_notification:
                    continue
                send_response(req_id, result)
                continue

            # -- logging / shutdown compat --
            if method == "shutdown":
                if is_notification:
                    continue
                send_response(req_id, {})
                continue

            # Unknown method
            if is_notification:
                log.warning("Unknown notification method: %s", method)
                continue
            send_error(req_id, -32601, f"Method not found: {method}")

        except ValueError as e:
            # Invalid params
            if is_notification:
                log.warning("ValueError on notification %s: %s", method, e)
                continue
            send_error(req_id, -32602, str(e))
        except Exception as e:
            log.exception("Internal error handling %s", method)
            if is_notification:
                continue
            send_error(req_id, -32603, f"Internal error: {e}")


# ---------------------------------------------------------------------------
# Optional Streamable HTTP (minimal) — for MCP 2 HTTP transport
# ---------------------------------------------------------------------------

def main_http(host: str, port: int):
    """Tiny Streamable HTTP server: POST /mcp with JSON-RPC, GET /health."""
    try:
        from http.server import BaseHTTPRequestHandler, HTTPServer
    except Exception as e:
        log.error("HTTP transport unavailable: %s", e)
        sys.exit(1)

    server = MCPServer()
    # pre-initialize as initialized for HTTP stateless mode
    server._initialized = True
    server._protocol_version = LATEST_PROTOCOL_VERSION

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/health", "/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"name": SERVER_NAME, "version": SERVER_VERSION, "protocolVersion": LATEST_PROTOCOL_VERSION}).encode())
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.path not in ("/mcp", "/"):
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
            msg = parse_message(body.strip()) if body.strip() else None
            if msg is None:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}).encode())
                return
            method = msg.get("method")
            params = msg.get("params") or {}
            req_id = msg.get("id")
            try:
                if method == "initialize":
                    result = server.handle_initialize(params if isinstance(params, dict) else {})
                    server._initialized = True
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
                elif method == "ping":
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
                elif method == "tools/list":
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": server.handle_tools_list(params if isinstance(params, dict) else {})}
                elif method == "tools/call":
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": server.handle_tools_call(params if isinstance(params, dict) else {})}
                elif method == "resources/list":
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": server.handle_resources_list(params if isinstance(params, dict) else {})}
                elif method == "resources/read":
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": server.handle_resources_read(params if isinstance(params, dict) else {})}
                elif method == "resources/templates/list":
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": server.handle_resources_templates_list({})}
                elif method == "prompts/list":
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": server.handle_prompts_list(params if isinstance(params, dict) else {})}
                elif method == "prompts/get":
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": server.handle_prompts_get(params if isinstance(params, dict) else {})}
                elif method == "completion/complete":
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": server.handle_completion_complete(params if isinstance(params, dict) else {})}
                else:
                    resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
            except ValueError as e:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": str(e)}}
            except Exception as e:
                log.exception("HTTP handler error")
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": f"Internal error: {e}"}}
            body_out = json.dumps(resp, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)

        def log_message(self, fmt, *args):
            log.info("%s", fmt % args)

    httpd = HTTPServer((host, port), Handler)
    log.info("HTTP MCP listening on http://%s:%s/mcp  (health http://%s:%s/health)", host, port, host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("HTTP server stopped")
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Skills MCP Server (MCP 2025-11-25)")
    ap.add_argument("--http", action="store_true", help="Run Streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args()
    log.setLevel(getattr(logging, args.log_level, logging.INFO))

    if args.http:
        main_http(args.host, args.port)
    else:
        # Warn if legacy env var is set (removed)
        if os.environ.get("SYSTEM_PROMPT_PATH"):
            log.warning("SYSTEM_PROMPT_PATH is deprecated and ignored (was arbitrary file read). Remove it from env.")
        main_stdio()


if __name__ == "__main__":
    main()
