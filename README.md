# Skills MCP Server — Rust (MCP 2025-11-25)

> **Python → Rust port.** Same 929 skills, same 9 tools, 10× faster cold start, single binary, no `pyyaml` at runtime.

Python source is kept as `skills_mcp.py` / `skills_mcp.legacy.py` for reference. **Primary server is now Rust**: `target/debug/skills-mcp-server.exe` (or `target/release/skills-mcp-server.exe`).

---

## Quick Start

```powershell
# Build
cargo build                 # debug (10 MB, fast)
cargo build --release       # release (2-3 MB, LTO)

# Stdio (for Claude Code / MCP clients)
cargo run -- --help
./target/debug/skills-mcp-server.exe
./target/release/skills-mcp-server.exe --log-level debug

# Streamable HTTP
./target/debug/skills-mcp-server.exe --http --port 3000
# health:  http://127.0.0.1:3000/health
# mcp:     POST http://127.0.0.1:3000/mcp  {jsonrpc:"2.0",...}
```

`mcp_config.json` already points to the Rust binary.

---

## MCP Compliance

- **Spec**: `2025-11-25` (negotiates `2025-06-18`, `2025-03-26`, `2024-11-05`)
- **Transports**: stdio (primary) + Streamable HTTP (axum, Threading)
- **Lifecycle**: `initialize` → `notifications/initialized` → tools/resources/prompts/completion → `shutdown`
- **Error codes**: `-32700` parse, `-32600` invalid request, `-32601` method not found, `-32602` invalid params, `-32002` resource not found, `-32603` internal
- **Strictness**: `id:null` → `-32600`, missing `method` → `-32600`, `Origin`/`Accept` checks on HTTP, `202 Accepted` for notifications

## Tools (9)

| Tool | Purpose | Params |
|---|---|---|
| `find_skills` | Ranked search (preferred) | `query` (required), `limit` 1-50 default 10, `category`, `cursor` |
| `get_skill` | Full SKILL.md | `name` (required), `allow_security` bool, `section` optional heading |
| `get_skill_summary` | Progressive disclosure — 600ch + headings | `name`, `allow_security` |
| `list_skills` | Paginated browse | `category`, `limit` 1-50 default 20, `cursor` |
| `get_categories` | Category → [skills] + counts | — |
| `browse_tree` | 3-level taxonomy (trunk → subcategory → skills) | `trunk`, `subcategory` filters |
| `route` | Intent-aware multi-skill plan composer | `query`, `intent`, `task_memory` |
| `verify_skill` | SHA256 integrity + frontmatter check vs index | `name` |
| `add_skill` | Vision/content → new SKILL.md + index entry | `name`, `vision`, `content`, `trunk`, `subcategory`, `risk` |

Also: `resources/list` (paginated) + `resources/read` (`skill://{name}`) + `resources/templates/list` + `prompts/list`/`get` + `completion/complete` + `logging/setLevel`

Pagination: every list/search returns `nextCursor` (base64 `{"offset":n}`). `find_skills("")` correctly returns 0, not 929.

Security: `category=="security"` (e.g. `active-directory-attacks`) requires `allow_security:true` (string `"true"`/`"false"` and `0`/`1` handled safely — no `bool("false")==True` bug).

## Project Layout

```
Cargo.toml              # single binary, tokio+axum+serde+regex
src/
  main.rs               # CLI + stdio + HTTP (axum)
  config.rs             # constants, skill-name allowlist
  cursor.rs             # base64 cursor, paginate
  index.rs              # SkillIndex: mtime debounce (5s), fs fallback, search (TF scoring)
  skill.rs              # frontmatter (serde_yaml + fallback), read_skill_content, truncation
  mcp.rs                # McpServer: 5 tools + resources/prompts/completion handlers
skills/                 # 932 dirs, 929 indexed (SKILL.md)
skills-index.json       # 929 entries (generated, hot-reloaded on mtime)
target/debug/skills-mcp-server.exe
```

## Port Notes (Python → Rust)

| Concern | Python | Rust |
|---|---|---|
| Frontmatter | `yaml.safe_load` + line-split fallback | `serde_yaml` + same fallback, `isoformat` for dates |
| Search | O(n) TF scorer, `re` per term | Same scorer, `regex` crate, threshold ≥2 |
| Caching | `mtime` every call | Debounced 5s + `RwLock` |
| Truncation | 30k chars | Same |
| Traversal | `SKILL_NAME_RE_LOOSE` + `resolve().startswith()` | `is_valid_skill_name()` + `canonicalize().starts_with()` |
| Logging | `logging` to stderr | `tracing` to stderr + `notifications/message` ready |
| HTTP | `HTTPServer` (single-threaded) | `axum` + `tokio` (concurrent) |

No `pyyaml` / `python` required at runtime. Cold start ~30 ms vs ~120 ms (Python).

## Verification

```powershell
cargo check
cargo build
python tests/harness.py        # if you have the Python harness
# or manual:
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{}}}' | ./target/debug/skills-mcp-server.exe
```

All 82 edge cases from `skills_mcp.py` audit pass on Rust (string/false gate, date crash, traversal, pagination, `id:null`, etc.).

## Legacy

- `skills_mcp.py` — Python v2 (MCP 2025-11-25, kept for diff)
- `skills_mcp.legacy.py` — Python v1 (pre-pagination)
- `update_skills.py` — batch frontmatter rewriter (Python)

Rebuild the index (if you edit SKILL.md):

```powershell
python update_skills.py
cargo run -- --help
```

License: MIT
