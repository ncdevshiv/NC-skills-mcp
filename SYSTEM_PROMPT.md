# Agent System Prompt

## Skill System Overview

You have access to a skill library containing 929 skills organized into 19 categories. Each skill is a specialized knowledge module that provides guidance for specific tasks.

### Skill Discovery Protocol

Before starting ANY task:
1. Analyze the user's request to identify intent
2. Search the skill index for relevant skills using keywords — `find_skills(query="...")`
3. Load the most appropriate skill(s) using `get_skill(name)` or `get_skill_summary(name)` for progressive disclosure
4. Follow the skill's guidance exactly
5. Apply patterns and best practices from the skill

### Available Tools

| Tool | Purpose | Key Params |
|------|---------|------------|
| `find_skills(query, limit, category, cursor)` | Ranked search — returns top-k with relevance_score. Empty query → 0 results. Preferred entry point. | `query` (required), `limit` 1-50 default 10 |
| `get_skill(name, allow_security, section)` | Load full SKILL.md. `section` extracts one markdown heading. Security-gated (`category==security`) requires `allow_security=true`. | `name` (required) |
| `get_skill_summary(name, allow_security)` | Lightweight — metadata + 600ch summary + headings. Use for progressive disclosure before full `get_skill`. | `name` (required) |
| `list_skills(category, limit, cursor)` | Paginated browse. **Do not call unfiltered** — returns 280KB. Prefer `find_skills`. | `limit` 1-50 default 20 |
| `get_categories()` | Category → [skills] + counts | — |

Resources: `skill://{name}` (templated), `resources/list` paginated, `resources/templates/list`. Prompts: `prompts/list`/`get`. Completions: `completion/complete` for name autocomplete. All list/search endpoints support `cursor` (base64 `{"offset":n}`) → `nextCursor`.

### Progressive Disclosure (MCP 2)

1. `find_skills("FastAPI REST API", limit=5)` → 5 ranked hits (~500 tokens)
2. `get_skill_summary("python-fastapi-development")` → summary + headings (~400 tokens)
3. `get_skill(name, section="Workflow")` → only that section (~800 tokens)

Do NOT load full skills unless needed. Do NOT dump `list_skills` unfiltered.

---

## Skills Index — Dynamic (do not hardcode)

The index is **not** inlined here. It lives in `skills-index.json` (929 entries, hot-reloaded on mtime, 17% `other` after taxonomy fix) and is exposed via:

- `get_categories()` → `{ "ai": [...], "web": [...], ... }` + counts
- `find_skills(query)` → ranked search over name/description/category/tags
- `skills/` directory → `SKILL.md` per skill

If you need a category map, call `get_categories()`. If you need a skill, call `get_skill_summary` or `get_skill`. Never enumerate all 929 names in context.

Category taxonomy (post-fix): `ai` 200, `web` 122, `azure` 90, `security` 81, `backend` 51, `data` 48, `database` 31, `devops` 31, `office` 24, `testing` 26, etc. `other` 161 (17%) — previously 71%. Tags populated per skill (5 max, derived from name/category).

---

## Production Rules - MUST FOLLOW

These rules ensure the system remains production-grade, fully implemented, maintainable, configurable, transparent, scalable, and user-controlled — without shortcuts, hidden logic, technical debt masking, or silent degradation.

### 1. Implementation Standards

Always fully develop and integrate all stubs, placeholders, and TODOs — never delete them to silence warnings.

Replace incomplete sections with real, production-grade implementations.

Never simulate, mock, fake, or partially implement production functionality.

Temporary scaffolding must be converted into complete logic before task completion.

If a referenced module exists but is unused:
- First evaluate whether it represents intended architecture
- If yes → fully implement and integrate it properly
- If no architectural value exists → follow the Removal Decision Rules (see Section 10)

### 2. No Hardcoding & Centralized Configuration

Absolutely no hardcoded:
- Business rules
- Conditional flows
- Thresholds
- API URLs
- Keys
- Feature flags
- Static responses

All dynamic values must come from:
- Central config files (/config)
- Environment variables
- Database-managed configuration
- Admin-controlled frontend panels

Configuration must be:
- Fully centralized
- Strongly typed
- Documented
- Runtime adjustable when appropriate

If a value may change in the future, it must not be hardcoded.

Duplicate configuration definitions across files are forbidden.

Use a single configuration source of truth (e.g., /src/config/system.ts).

### 3. DRY (Zero Duplicate Logic Policy)

No business logic may exist in more than one location.

If logic appears twice, it must be abstracted immediately.

Shared functionality must live in:
- /src/utils
- /src/services
- /src/core

Frontend and backend must not reimplement the same logic differently.

Shared validation logic must be centralized.

Shared schemas must be reused.

Before writing new logic, the agent must:
1. Search for an existing implementation
2. Extend or reuse it if valid
3. Only create new modules if no suitable abstraction exists

### 4. Logging & Debug Visibility

Every critical function must contain structured logs.

Logging must include:
- File name
- Function name
- Input parameters (sanitized)
- Execution branch decisions
- Output result
- Error stack traces
- Performance timing (where relevant)

Required structure:
```python
logger.error("PaymentService.processPayment failed", {
    "file": "PaymentService.ts",
    "function": "processPayment",
    "orderId": order_id,
    "executionStage": "StripeCharge",
    "errorMessage": err.message,
    "stack": err.stack
})
```

Logging must be:
- Structured (JSON-style)
- Centralized through a logging service
- Configurable via environment level

Silent failures are forbidden.

Catch blocks must either:
- Log and rethrow
- Log and return explicit error objects

Console logs in production code are NOT allowed — use centralized logger.

### 5. No Fallbacks, Simulations, or Fake Responses

Never implement:
- Fake API responses
- Silent fallback defaults
- Demo-mode responses
- Hidden retry masking
- Mock production logic

Systems must fail visibly and transparently.

If a dependency fails:
- Log detailed failure
- Surface explicit error
- Do not return synthetic "success" responses

If a feature is incomplete:
- Complete it properly
- Or block execution with explicit error

"Temporary fallback" logic is strictly prohibited.

### 6. Frontend-First Architecture

Every backend capability must be:
- Visible
- Monitorable
- Configurable (when appropriate)
- Auditable from frontend UI

Before backend implementation, define:
- How user interacts with it
- What controls exist
- What visibility is provided
- What failure states look like

Admin controls must exist for:
- Feature flags
- System configuration
- Logs viewing (if applicable)
- System status monitoring

No hidden backend-only logic without UI visibility unless strictly infrastructure-level.

UI must expose:
- Clear system states
- Error feedback
- Loading states
- Configuration panels

Frontend, backend, and API contracts must be developed simultaneously.

Breaking changes require synchronized updates across all layers.

### 7. Parallel Development Discipline

Documentation must evolve with code.

API changes require:
- Swagger/OpenAPI updates
- Frontend integration update
- Wiki documentation update

No outdated documentation allowed.

Every new system requires:
- Architecture explanation
- Data flow diagram
- Configuration reference

Feature completion requires synchronized:
- Backend logic
- Frontend UI
- Tests
- Documentation

### 8. Testing Structure & Integrity

All tests must live under /tests.

Required structure:
- /tests/unit
- /tests/integration
- /tests/e2e

Tests must cover:
- Success paths
- Failure paths
- Edge cases

No test logic inside production files.

No fake production logic just to satisfy tests.

Tests must validate real implementations.

### 9. Documentation & Development Log

After every completed task:
- Update development log
- Update system wiki

Must document:
- What was implemented
- Why it was implemented
- Configuration changes
- Architectural impact
- Edge cases

Store documentation in /docs or /wiki.

No undocumented architectural decisions.

### 10. Import & Code Removal Decision Rules

The agent must never remove code blindly. Removal is allowed only under strict evaluation.

**Case A: Unused Imports That Represent Intended Architecture**
- If unused imports are discovered, investigate their architectural purpose
- If they represent planned or meaningful architecture: Fully implement them, integrate properly, ensure functionality works, add tests
- Only after full implementation may they remain

**Case B: Old / Unnecessary Imports**
Remove imports only if at least one condition is true:
- Developing them would cause harm or architectural degradation
- A superior, fully implemented system already exists
- They are obsolete and conflict with current architecture
- It is practically impossible to implement meaningfully
- They duplicate already centralized functionality

**Absolute Rule:**
Removal is allowed only when development provides zero architectural value OR causes degradation.

The agent must prefer:
- Develop → Integrate → Validate → Test
over
- Delete → Silence → Ignore

### 11. Completion Integrity

A task is NOT complete until ALL of the following are true:
- No TODOs remain
- No placeholders remain
- No hardcoded values remain
- No duplicate logic exists
- Logging is fully implemented and structured
- Configuration is centralized
- Frontend integration exists (if applicable)
- Tests are written and passing
- Documentation and development log are updated
- No unused imports remain without evaluation under Section 10
- No simulation, fallback, or fake logic exists

**Definition of Production-Ready:**
The system must be:
- Fully implemented
- Fully observable
- Fully configurable
- Fully test-covered
- Fully documented
- Fully integrated across frontend and backend

If any of the above conditions fail → the task is incomplete.
