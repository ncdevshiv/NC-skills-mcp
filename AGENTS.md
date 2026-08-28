# Autonomous Skill System — MCP 2.1

## Overview
929 skills, 10 trunks × 40 subcategories (tree taxonomy). MCP 2025-11-25 compliant. Pagination + security gates + hash integrity verification enabled.

## How It Works

### 1. Skill Discovery (REQUIRED — prevents 70k-token overload)
Before starting ANY task:
1. Call `find_skills(query="specific task description", limit=10)` — ranked search, never dumps all 929.
2. Review top results (relevance_score, risk, trunk, subcategory).
3. Call `get_skill(name="exact-name")` for the best 1-2 hits.
   - Security-gated skills (category==security) require `allow_security=true` + explicit user authorization.
4. Follow the loaded SKILL.md guidance.

### 2. Anti-Patterns (DO NOT)
- ❌ `list_skills` without `category` filter or without `limit`/`cursor` — returns 280KB / ~70k tokens, will blow your context.
- ❌ `find_skills(query="")` — empty query now correctly returns 0 results.
- ❌ `get_skill` with path traversal or guessed names — names are allowlisted `^[a-z0-9][a-z0-9-_]{1,64}$`.
- ❌ Bypassing `browse_tree` for discovery — use the tree first, then drill into `find_skills`.

### 3. Pagination
All list/search/resource/prompt endpoints support:
- `limit` (1-50, default 20 for list_skills/resources/prompts, 10 for find_skills)
- `cursor` (opaque base64 offset, returned as `nextCursor`)
- `total` / `totalMatches` fields for progress.

Example:
```
find_skills(query="FastAPI REST API", limit=5) -> {skills:[...], nextCursor:"eyJv..."}
find_skills(query="FastAPI REST API", limit=5, cursor="eyJv...") -> next page
list_skills(category="ai", limit=20, cursor="...")
resources/list with cursor/limit
prompts/list with cursor/limit
```

### 4. Tree Taxonomy (browse_tree)

The skill inventory is organized as a 3-level tree: **trunk → subcategory → skills**.

```
├── ai-ml
│   ├── agent-development
│   ├── llm-application
│   ├── prompt-engineering
│   ├── computer-vision
│   ├── voice-audio-ai
│   └── ml-ops-engineering
├── web
│   ├── frontend-frameworks
│   ├── fullstack-development
│   ├── web-automation
│   ├── web3-blockchain
│   └── design-ux
├── backend
│   ├── api-design
│   ├── backend-frameworks
│   └── serverless-functions
├── devops
│   ├── ci-cd-pipelines
│   ├── containers-orchestration
│   ├── infrastructure-iaas
│   └── monitoring-observability
├── data
│   ├── data-engineering-pipelines
│   ├── analytics-visualization
│   └── ai-ml-engineering
├── cloud
│   ├── aws
│   ├── azure
│   └── gcp-google-cloud
├── mobile
│   ├── android
│   ├── ios
│   └── cross-platform
├── database
│   ├── sql-relational
│   ├── nosql
│   ├── vector-embedding
│   └── orm-odm
├── security
│   ├── application-security
│   ├── penetration-testing
│   ├── cloud-security
│   ├── auth-authorization
│   ├── compliance-forensics
│   └── reverse-engineering
└── testing
    ├── unit-integration
    ├── e2e-acceptance
    ├── performance-load
    └── security-testing
```

Use `browse_tree` to explore. Filter by `trunk` (e.g. `browse_tree({trunk:"testing"})`) to see only that branch.
A task may need skills from multiple trunks — the `route` tool composes multi-skill plans.

### 5. Tool Reference

| Tool | Purpose | Key Params |
|------|---------|------------|
| `find_skills` | Ranked search (preferred) | `query` (required), `limit` (1-50), `category`, `cursor` |
| `get_skill` | Load full SKILL.md | `name` (required), `allow_security` (bool), `section` (optional heading) |
| `get_skill_summary` | Progressive disclosure — metadata + section headings | `name` (required), `allow_security` (bool) |
| `list_skills` | Paginated browse (fallback) | `category`, `limit`, `cursor` |
| `get_categories` | Category map + counts | — |
| `browse_tree` | Hierarchical taxonomy tree | `trunk` (optional filter), `subcategory` (optional filter) |
| `route` | Intent-aware router — composes multi-skill plan | `query` (required), `intent` (build/audit/migrate/debug/automate/test/design), `task_memory` (required context) |
| `verify_skill` | Hash integrity check — compares file vs index | `name` (required) |
| `add_skill` | Create new skill from vision or markdown content | `name`, `vision`, `content`, `trunk`, `subcategory`, `risk` |

### 6. add_skill Workflow

To add a new skill to the inventory, call `add_skill` with:

- `name` (required): kebab-case, e.g. `"my-new-skill"`
- `vision` (optional): freeform description in natural language
- `content` (optional): full SKILL.md markdown content
- `trunk` (optional): auto-classified if omitted
- `subcategory` (optional): auto-classified if omitted
- `risk` (optional): `safe` | `medium` | `high` | `unknown` (default `safe`)

The tool auto-generates SKILL.md with YAML frontmatter, slugifies the name, and places it in `skills/{name}/SKILL.md`. After adding, run `python rebuild_index.py` to sync the index.

### 7. Security Gating

- `get_skill` blocks security skills unless `allow_security=true` is passed (bool, not string).
- `resources/read` with `skill://` URI blocks security skills.
- `prompts/get` blocks security skills.
- `verify_skill` always works (non-invasive).

### 8. Hash Integrity

`verify_skill` computes SHA256 of the live SKILL.md file and compares it to the indexed hash. Returns `hash_match` (bool), `hash_indexed`, `hash_actual`, `verified`, `trunk`, `subcategory`. Use this to detect tampering or stale index entries. Run `python rebuild_index.py` after any bulk modifications to sync hashes.

### 9. Example Flow (Progressive Disclosure)
User: "Build a FastAPI REST API"
1. `browse_tree` → see `backend > api-design` has relevant skills
2. `find_skills(query="FastAPI REST API", limit=5)` → `fastapi-pro` (score 23.28)
3. `get_skill_summary(name="fastapi-pro")` → metadata + section headings (~400 tokens)
4. `get_skill(name="fastapi-pro", section="Workflow")` → only that section (~800 tokens)

## Important
- Always `find_skills` first — never `list_skills` unfiltered.
- Security skills (`category==security`) need `allow_security=true`.
- Use pagination; handle `nextCursor` when exploring broadly.
- Use progressive disclosure: `browse_tree → find → summary → section` saves ~80% tokens vs full dump.
- Skills are cached from `skills-index.json` (≈350KB, 929 entries, tags/summary/hash, hot reload debounced 5s).
- Tree taxonomy: 10 trunks, 40 subcategories. `browse_tree` exposes the full hierarchy.

## MCP Transport (Rust — primary)
- stdio: `./target/debug/skills-mcp-server.exe` or `cargo run` (Python fallback: `python skills_mcp.py`)
- Streamable HTTP: `./target/debug/skills-mcp-server.exe --http --port 3000` → POST /mcp (concurrent, axum+tower)
- Protocol versions: 2025-11-25 (latest), 2025-06-18, 2025-03-26, 2024-11-05 (negotiated)
- Primary binary built via `cargo build` — no `pyyaml` at runtime

## Version: 2.1.0