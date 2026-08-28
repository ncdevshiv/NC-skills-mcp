use anyhow::Result;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::Arc;

use crate::config::*;
use crate::cursor::{decode_cursor, encode_cursor, paginate};
use crate::index::SkillIndex;
use crate::skill::{file_hash, parse_frontmatter_public, read_skill_content, SkillContent, SkillRecord};

#[derive(Debug, Clone)]
pub struct McpServer {
    pub server_info: Value,
    pub capabilities: Value,
    pub protocol_version: String,
    pub initialized: bool,
    pub client_info: Option<Value>,
    pub task_memory: Vec<String>,
}

impl McpServer {
    pub fn new() -> Self {
        Self {
            server_info: json!({ "name": SERVER_NAME, "version": SERVER_VERSION }),
            capabilities: json!({
                "tools": { "listChanged": false },
                "resources": { "subscribe": false, "listChanged": false },
                "prompts": { "listChanged": false },
                "logging": {},
                "completions": {}
            }),
            protocol_version: LATEST_PROTOCOL_VERSION.to_string(),
            initialized: false,
            client_info: None,
            task_memory: Vec::new(),
        }
    }

    fn negotiate(&mut self, requested: Option<&str>) -> String {
        if let Some(v) = requested {
            if SUPPORTED_PROTOCOL_VERSIONS.contains(&v) {
                self.protocol_version = v.to_string();
                return v.to_string();
            }
            tracing::info!("unsupported protocolVersion {} -> {}", v, LATEST_PROTOCOL_VERSION);
        }
        self.protocol_version = LATEST_PROTOCOL_VERSION.to_string();
        LATEST_PROTOCOL_VERSION.to_string()
    }

    pub fn handle_initialize(&mut self, params: &Value) -> Value {
        let req_ver = params.get("protocolVersion").and_then(|v| v.as_str());
        let ver = self.negotiate(req_ver);
        self.client_info = params.get("clientInfo").cloned();
        let caps = params.get("capabilities").cloned().unwrap_or(json!({}));
        tracing::info!("initialize from {:?} caps={} -> {}", self.client_info, caps, ver);
        json!({
            "protocolVersion": ver,
            "capabilities": self.capabilities,
            "serverInfo": self.server_info,
            "instructions": "Skills MCP — discover via find_skills(query) before get_skill(name). Do NOT call list_skills unpaginated; use cursor/limit. Security skills (category==security) require allow_security=true."
        })
    }

    /// Check if a skill is security-gated. Returns error text if gated and not allowed.
    fn check_security_gate(
        &self,
        index: &SkillIndex,
        skill_name: &str,
        allow_security: bool,
    ) -> Result<(), String> {
        let rec = index.get(skill_name);
        let live_meta = index.cached_frontmatter(skill_name).unwrap_or_default();
        let live_cat = live_meta.get("category").map(|s| s.to_lowercase()).unwrap_or_default();
        let idx_cat = rec.as_ref().map(|r| r.category.to_lowercase()).unwrap_or_default();
        let eff_cat = if !live_cat.is_empty() { live_cat } else { idx_cat };
        let eff_risk = live_meta
            .get("risk")
            .cloned()
            .or_else(|| rec.as_ref().map(|r| r.risk.clone()))
            .unwrap_or_else(|| "unknown".into());
        if eff_cat == "security" && !allow_security {
            return Err(format!(
                "Skill '{}' is security-gated (risk={}, category={}). Pass allow_security=true only if you have explicit user authorization.",
                skill_name, eff_risk, eff_cat
            ));
        }
        Ok(())
    }

    pub fn handle_tools_list(&self, _params: &Value) -> Result<Value> {
        // Only 9 tools total — no pagination needed. Returning all at once.
        let tools = vec![
            json!({
                "name": "find_skills",
                "title": "Find relevant skills",
                "description": "Search skills by natural-language task. Returns top-k ranked. Call BEFORE get_skill. Supports trunk/subcategory filters for tree navigation. Empty query returns 0.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "Task or topic, e.g. 'FastAPI REST API'" },
                        "limit": { "type": "integer", "description": "Max results 1-50 default 10", "minimum": 1, "maximum": 50, "default": 10 },
                        "category": { "type": "string", "description": "Optional category filter" },
                        "trunk": { "type": "string", "description": "Optional trunk filter (ai-ml, web, backend, security, devops, data, cloud, mobile, database, testing)" },
                        "subcategory": { "type": "string", "description": "Optional subcategory filter" },
                        "cursor": { "type": "string", "description": "Pagination cursor" }
                    },
                    "required": ["query"]
                },
                "annotations": { "title": "Find skills", "readOnlyHint": true, "openWorldHint": false }
            }),
            json!({
                "name": "get_skill",
                "title": "Get skill content",
                "description": "Load full SKILL.md by exact name. Security-gated skills need allow_security=true.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": { "type": "string", "description": "Exact skill name" },
                        "allow_security": { "type": "boolean", "description": "Confirm authorization for security-gated skills", "default": false },
                        "section": { "type": "string", "description": "Optional markdown section heading to extract (e.g. 'Workflow')" }
                    },
                    "required": ["name"]
                },
                "annotations": { "title": "Get skill", "readOnlyHint": true, "openWorldHint": false }
            }),
            json!({
                "name": "get_skill_summary",
                "title": "Get skill summary",
                "description": "Lightweight summary: metadata + first 600 chars + section headings. Use for progressive disclosure.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": { "type": "string", "description": "Exact skill name" },
                        "allow_security": { "type": "boolean", "default": false }
                    },
                    "required": ["name"]
                },
                "annotations": { "title": "Get skill summary", "readOnlyHint": true, "openWorldHint": false }
            }),
            json!({
                "name": "list_skills",
                "title": "List skills (paginated)",
                "description": "Paginated list. Prefer find_skills. Use trunk/subcategory+cursor/limit to avoid full dump.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "category": { "type": "string", "description": "Filter by category" },
                        "trunk": { "type": "string", "description": "Filter by trunk" },
                        "subcategory": { "type": "string", "description": "Filter by subcategory" },
                        "cursor": { "type": "string", "description": "Pagination cursor" },
                        "limit": { "type": "integer", "description": "Page size 1-50 default 20", "minimum": 1, "maximum": 50, "default": 20 }
                    }
                },
                "annotations": { "title": "List skills", "readOnlyHint": true, "openWorldHint": false }
            }),
            json!({
                "name": "get_categories",
                "title": "Get categories",
                "description": "List categories, trunks, subcategories, and skill counts. Use browse_tree for the full hierarchy.",
                "inputSchema": { "type": "object", "properties": {}, "additionalProperties": false },
                "annotations": { "title": "Get categories", "readOnlyHint": true, "openWorldHint": false }
            }),
            json!({
                "name": "browse_tree",
                "title": "Browse skill taxonomy tree",
                "description": "Explore the hierarchical skill taxonomy: trunks -> subcategories -> skills. Use to discover available skills without dumping the entire index.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "trunk": { "type": "string", "description": "Filter to a specific trunk (ai-ml, web, backend, security, devops, data, cloud, mobile, database, testing)" },
                        "subcategory": { "type": "string", "description": "Filter to a specific subcategory within the trunk" },
                        "limit": { "type": "integer", "description": "Max skills per subcategory (1-50)", "minimum": 1, "maximum": 50, "default": 15 }
                    }
                },
                "annotations": { "title": "Browse tree", "readOnlyHint": true, "openWorldHint": false }
            }),
            json!({
                "name": "route",
                "title": "Route task to skills",
                "description": "Intent-aware router: given task intent + query, returns composed plan of 2-3 skills. Task memory is passed explicitly for context.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "intent": { "type": "string", "description": "Task intent: build, audit, migrate, debug, automate, test, design", "enum": ["build","audit","migrate","debug","automate","test","design"] },
                        "query": { "type": "string", "description": "Natural language task" },
                        "constraints": { "type": "object", "description": "Constraints like {lang: python, platform: azure}", "additionalProperties": true },
                        "task_memory": { "type": "array", "items": { "type": "string" }, "description": "Recent search queries for context boosting (pass explicitly, not server-stored)" }
                    },
                    "required": ["query"]
                },
                "annotations": { "title": "Route", "readOnlyHint": true, "openWorldHint": false }
            }),
            json!({
                "name": "verify_skill",
                "title": "Verify skill integrity",
                "description": "Run verification hook: checks skill exists, frontmatter valid, quality_score, SHA256 hash integrity, and taxonomy assignment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": { "type": "string", "description": "Exact skill name" }
                    },
                    "required": ["name"]
                },
                "annotations": { "title": "Verify skill", "readOnlyHint": true, "openWorldHint": false }
            }),
            json!({
                "name": "add_skill",
                "title": "Add a new skill",
                "description": "Create and register a new skill in the inventory. Accepts skill content as markdown (with or without YAML frontmatter). The agent can paste a skill description, raw .md content, or a vision in any language and the server will create the skill directory, SKILL.md, and update the index.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": { "type": "string", "description": "Skill name (kebab-case). If omitted, inferred from content or vision." },
                        "content": { "type": "string", "description": "Full skill content as markdown. Can be raw .md, full SKILL.md with frontmatter, or a freeform vision/description." },
                        "vision": { "type": "string", "description": "Freeform description of what the skill should do, in any language. Used when content is not provided." },
                        "trunk": { "type": "string", "description": "Optional trunk assignment (ai-ml, web, backend, security, devops, data, cloud, mobile, database, testing). If omitted, auto-classified." },
                        "subcategory": { "type": "string", "description": "Optional subcategory assignment. If omitted, auto-classified." },
                        "risk": { "type": "string", "description": "Risk level: safe (default) or high. Security skills auto-set to high." },
                        "copy_from": { "type": "array", "items": { "type": "string" }, "description": "Names of existing skills to use as reference templates for structure." }
                    },
                    "required": []
                },
                "annotations": { "title": "Add skill", "readOnlyHint": false, "openWorldHint": true }
            }),
        ];
        Ok(json!({ "tools": tools }))
    }

    pub fn handle_tools_call(
        &mut self,
        index: &SkillIndex,
        skills_dir: &PathBuf,
        params: &Value,
    ) -> Result<Value> {
        let name = params.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let args = params.get("arguments").cloned().unwrap_or(json!({}));
        if !args.is_object() {
            anyhow::bail!("arguments must be an object");
        }
        let args_obj = args.as_object().unwrap();

        tracing::info!("tools/call {} args={}", name, serde_json::to_string(&args).unwrap_or_default().chars().take(300).collect::<String>());

        match name {
            "find_skills" => {
                let query = args_obj.get("query").and_then(|v| v.as_str()).unwrap_or("").to_string();
                // Task memory: keep last 3 queries for intent boosting
                if !query.trim().is_empty() {
                    self.task_memory.push(query.clone());
                    if self.task_memory.len() > 3 { self.task_memory.remove(0); }
                }
                if query.trim().is_empty() {
                    return Ok(json!({
                        "content": [{ "type": "text", "text": serde_json::to_string(&json!({"skills": [], "count": 0, "warning": "Empty query — provide a specific task description."}))? }]
                    }));
                }
                let limit = args_obj.get("limit").and_then(|v| v.as_u64()).unwrap_or(10) as usize;
                let limit = limit.clamp(1, MAX_PAGE_SIZE);
                let category = args_obj.get("category").and_then(|v| v.as_str());
                let cursor = args_obj.get("cursor").and_then(|v| v.as_str());
                // Search with enough headroom for pagination
                let results = index.search(&query, MAX_PAGE_SIZE * 4, category);
                let (page, next) = paginate(&results, cursor, Some(limit))?;
                let mut payload = json!({ "skills": page, "count": page.len(), "totalMatches": results.len() });
                if let Some(n) = next { payload["nextCursor"] = json!(n); }
                Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&payload)? }] }))
            }
            "get_skill" => {
                let skill_name = args_obj.get("name").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                if skill_name.is_empty() {
                    return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error":"Missing required argument: name"}))? }], "isError": true }));
                }
                if !is_valid_skill_name(&skill_name) {
                    return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error": format!("Invalid skill name: {}", skill_name)}))? }], "isError": true }));
                }
                // Security gate: only category==security is gated
                let rec = index.get(&skill_name);
                let live_meta = index.cached_frontmatter(&skill_name).unwrap_or_default();
                let live_cat = live_meta.get("category").map(|s| s.to_lowercase()).unwrap_or_default();
                let idx_cat = rec.as_ref().map(|r| r.category.to_lowercase()).unwrap_or_default();
                let eff_cat = if !live_cat.is_empty() { live_cat } else { idx_cat };
                let eff_risk = live_meta.get("risk").cloned().or_else(|| rec.as_ref().map(|r| r.risk.clone())).unwrap_or_else(|| "unknown".into());
                let raw_allow = args_obj.get("allow_security");
                let allow_security = match raw_allow {
                    Some(Value::String(s)) => matches!(s.to_lowercase().as_str(), "true" | "1" | "yes"),
                    Some(Value::Number(n)) => n.as_u64() == Some(1),
                    Some(Value::Bool(b)) => *b,
                    _ => false,
                };
                let is_gated = eff_cat == "security";
                if is_gated && !allow_security {
                    return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({
                        "error": format!("Skill '{}' is security-gated (risk={}, category={}). Pass allow_security=true only if you have explicit user authorization.", skill_name, eff_risk, eff_cat),
                        "risk": eff_risk, "category": eff_cat, "requires_authorization": true
                    }))? }], "isError": true }));
                }
                let mut data = match read_skill_content(skills_dir, &skill_name) {
                    Some(d) => d,
                    None => return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error": format!("Skill '{}' not found", skill_name)}))? }], "isError": true })),
                };
                // Optional section extraction
                if let Some(section) = args_obj.get("section").and_then(|v| v.as_str()) {
                    if !section.trim().is_empty() {
                        let extracted = extract_section(&data.content, section);
                        if let Some(sec) = extracted {
                            data.content = sec;
                        } else {
                            return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error": format!("Section '{}' not found in skill '{}'", section, skill_name)}))? }], "isError": true }));
                        }
                    }
                }
                let text = serde_json::to_string(&data)?;
                Ok(json!({ "content": [{ "type": "text", "text": text }] }))
            }
            "get_skill_summary" => {
                let skill_name = args_obj.get("name").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                if skill_name.is_empty() {
                    return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error":"Missing required argument: name"}))? }], "isError": true }));
                }
                if !is_valid_skill_name(&skill_name) {
                    return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error": format!("Invalid skill name: {}", skill_name)}))? }], "isError": true }));
                }
                let rec = index.get(&skill_name);
                let live_meta = index.cached_frontmatter(&skill_name).unwrap_or_default();
                let live_cat = live_meta.get("category").map(|s| s.to_lowercase()).unwrap_or_default();
                let idx_cat = rec.as_ref().map(|r| r.category.to_lowercase()).unwrap_or_default();
                let eff_cat = if !live_cat.is_empty() { live_cat } else { idx_cat };
                let raw_allow = args_obj.get("allow_security");
                let allow_security = match raw_allow {
                    Some(Value::String(s)) => matches!(s.to_lowercase().as_str(), "true" | "1" | "yes"),
                    Some(Value::Number(n)) => n.as_u64() == Some(1),
                    Some(Value::Bool(b)) => *b,
                    _ => false,
                };
                if eff_cat == "security" && !allow_security {
                    return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error": format!("Skill '{}' is security-gated. Pass allow_security=true", skill_name), "requires_authorization": true}))? }], "isError": true }));
                }
                let data = match read_skill_content(skills_dir, &skill_name) {
                    Some(d) => d,
                    None => return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error": format!("Skill '{}' not found", skill_name)}))? }], "isError": true })),
                };
                let summary = data.content.chars().take(600).collect::<String>();
                let sections = extract_headings(&data.content);
                let payload = json!({
                    "name": data.name,
                    "metadata": data.metadata,
                    "summary": summary,
                    "sections": sections,
                    "truncated": data.truncated,
                    "risk": data.risk,
                    "category": rec.as_ref().map(|r| r.category.clone()).unwrap_or_default()
                });
                Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&payload)? }] }))
            }
            "list_skills" => {
                let category = args_obj.get("category").and_then(|v| v.as_str());
                let cursor = args_obj.get("cursor").and_then(|v| v.as_str());
                let limit = args_obj.get("limit").and_then(|v| v.as_u64()).map(|n| n as usize).unwrap_or(DEFAULT_PAGE_SIZE);
                let all = index.all_skills();
                let items: Vec<Value> = all
                    .iter()
                    .filter(|r| category.map(|c| r.category == c).unwrap_or(true))
                    .map(|r| json!({
                        "name": r.name, "description": r.description, "category": r.category,
                        "version": r.version, "author": r.author, "risk": r.risk, "tags": r.tags
                    }))
                    .collect();
                let (page, next) = paginate(&items, cursor, Some(limit))?;
                let mut payload = json!({ "skills": page, "count": page.len(), "total": items.len() });
                if let Some(n) = next { payload["nextCursor"] = json!(n); }
                if category.is_none() && cursor.is_none() && items.len() > 100 {
                    payload["warning"] = json!("Unfiltered list_skills returns ~928 items. Prefer find_skills(query) or filter by category and paginate via cursor/limit.");
                }
                Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&payload)? }] }))
            }
            "get_categories" => {
                let cats = index.categories();
                let counts: std::collections::HashMap<String, usize> = cats.iter().map(|(k, v)| (k.clone(), v.len())).collect();
                let total: usize = cats.values().map(|v| v.len()).sum();
                let payload = json!({ "categories": cats, "counts": counts, "totalSkills": total });
                Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&payload)? }] }))
            }
            "route" => {
                let query = args_obj.get("query").and_then(|v| v.as_str()).unwrap_or("").to_string();
                if query.trim().is_empty() {
                    return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error":"route requires query"}))? }], "isError": true }));
                }
                let intent = args_obj.get("intent").and_then(|v| v.as_str()).map(|s| s.to_lowercase());
                // Task memory is passed explicitly by client, NOT server-stored
                let mem: Vec<String> = args_obj
                    .get("task_memory")
                    .and_then(|v| v.as_array())
                    .map(|a| a.iter().filter_map(|v| v.as_str()).map(|s| s.to_string()).collect())
                    .unwrap_or_default();
                let boosted_query = if !mem.is_empty() {
                    format!("{} {}", mem.join(" "), query)
                } else {
                    query.clone()
                };
                let results = index.search(&boosted_query, 10, None);
                let mut scored: Vec<(f64, serde_json::Value)> = results.into_iter().map(|v| {
                    let base = v.get("relevance_score").and_then(|x| x.as_f64()).unwrap_or(0.0);
                    let mut score = base;
                    if let Some(ref it) = intent {
                        if let Some(skill_intent) = v.get("intent").and_then(|x| x.as_str()) {
                            if skill_intent.to_lowercase() == *it { score += 5.0; }
                        }
                    }
                    (score, v)
                }).collect();
                scored.sort_by(|a,b| b.0.partial_cmp(&a.0).unwrap());
                let plan: Vec<serde_json::Value> = scored.iter().take(3).map(|(_,v)| v.clone()).collect();
                let clusters: std::collections::HashSet<String> = plan.iter().filter_map(|v| v.get("cluster").and_then(|x| x.as_str()).map(|s| s.to_string())).collect();
                let payload = json!({
                    "query": query,
                    "intent": intent,
                    "plan": plan,
                    "clusters": clusters.into_iter().collect::<Vec<_>>(),
                    "task_memory": mem,
                    "total_candidates": scored.len()
                });
                Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&payload)? }] }))
            }
            "verify_skill" => {
                let skill_name = args_obj.get("name").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                if skill_name.is_empty() || !crate::config::is_valid_skill_name(&skill_name) {
                    return Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error": format!("Invalid skill name: {}", skill_name)}))? }], "isError": true }));
                }
                let rec = index.get(&skill_name);
                let meta = index.cached_frontmatter(&skill_name);
                let exists = rec.is_some() || meta.is_some();
                let quality = rec.as_ref().and_then(|r| r.quality_score).unwrap_or(0.5);
                let indexed_hash = rec.as_ref().and_then(|r| r.hash.clone()).unwrap_or_default();
                let cluster = rec.as_ref().map(|r| r.cluster.clone()).unwrap_or_default();
                let trunk = rec.as_ref().and_then(|r| r.trunk.clone());
                let subcategory = rec.as_ref().and_then(|r| r.subcategory.clone());
                // Hash integrity: compare SHA256 of actual file against indexed hash
                let actual_hash = rec.as_ref().and_then(|r| {
                    let p = PathBuf::from(r.path.clone());
                    file_hash(&p)
                });
                let actual_hash_str = actual_hash.clone();
                let hash_match = match actual_hash {
                    Some(ah) => indexed_hash == ah,
                    None => indexed_hash.is_empty(),
                };
                let verified = exists && hash_match;
                let payload = json!({
                    "name": skill_name,
                    "exists": exists,
                    "verified": verified,
                    "quality_score": quality,
                    "hash_indexed": indexed_hash,
                    "hash_actual": actual_hash_str,
                    "hash_match": hash_match,
                    "cluster": cluster,
                    "trunk": trunk,
                    "subcategory": subcategory,
                    "frontmatter_valid": meta.is_some(),
                    "path": rec.as_ref().map(|r| r.path.clone()).unwrap_or_default()
                });
                Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&payload)? }] }))
            }
            "browse_tree" => {
                let trunk_filter = args_obj.get("trunk").and_then(|v| v.as_str());
                let subcategory_filter = args_obj.get("subcategory").and_then(|v| v.as_str());
                let limit = args_obj.get("limit").and_then(|v| v.as_u64()).map(|n| n as usize).unwrap_or(15).clamp(1, MAX_PAGE_SIZE);
                let all = index.all_skills();
                // Build tree from available skills + known taxonomy
                let mut tree: std::collections::BTreeMap<String, serde_json::Value> = std::collections::BTreeMap::new();

                let trunks = if let Some(tf) = trunk_filter {
                    vec![tf.to_string()]
                } else {
                    VALID_TRUNKS.iter().map(|t| t.to_string()).collect()
                };

                for trunk in trunks {
                    let trunk_l = trunk.to_lowercase();
                    if !is_valid_trunk(&trunk_l) { continue; }
                    // Collect all subcategories for this trunk
                    let mut subs_map: std::collections::BTreeMap<String, Vec<String>> = std::collections::BTreeMap::new();
                    for (t, sub) in TAXONOMY.iter() {
                        if *t == trunk_l {
                            subs_map.entry(sub.to_string()).or_default();
                        }
                    }
                    // Group skills by subcategory
                    for r in all.iter() {
                        let r_trunk = r.trunk.as_ref().map(|t| t.to_lowercase()).unwrap_or_default();
                        if r_trunk != trunk_l { continue; }
                        let sub_name = if let Some(ref sf) = subcategory_filter {
                            sf.to_string()
                        } else {
                            r.subcategory.clone().unwrap_or_else(|| {
                                // Auto-classify: pick first matching subcategory
                                find_matching_subcategory(r, &trunk_l)
                            })
                        };
                        if subs_map.contains_key(&sub_name) {
                            subs_map.get_mut(&sub_name).unwrap().push(r.name.clone());
                        } else {
                            subs_map.entry(sub_name).or_default().push(r.name.clone());
                        }
                    }
                    // Dedup and limit
                    let subcategories: Vec<serde_json::Value> = subs_map
                        .into_iter()
                        .filter(|(sub, _)| subcategory_filter.map(|sf| sub.to_lowercase() == sf.to_lowercase()).unwrap_or(true))
                        .map(|(sub, mut names)| {
                            names.sort();
                            names.dedup();
                            names.truncate(limit);
                            json!({
                                "subcategory": sub,
                                "skill_count": names.len(),
                                "skills": names
                            })
                        })
                        .collect();
                    let trunk_key = trunk.clone();
                    let trunk_display = trunk.clone();
                    tree.insert(trunk_key, json!({
                        "trunk": trunk_display,
                        "subcategories": subcategories,
                        "total_skills": subcategories.iter().map(|v| v["skill_count"].as_u64().unwrap_or(0)).sum::<u64>() as usize
                    }));
                }
                let payload = json!({ "tree": tree });
                Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&payload)? }] }))
            }
            "add_skill" => {
                // Create a new skill in the inventory
                let name = args_obj
                    .get("name")
                    .and_then(|v| v.as_str())
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty());
                let content = args_obj.get("content").and_then(|v| v.as_str()).map(|s| s.to_string());
                let vision = args_obj.get("vision").and_then(|v| v.as_str()).map(|s| s.to_string());
                let trunk_arg = args_obj.get("trunk").and_then(|v| v.as_str()).map(|s| s.to_lowercase());
                let subcategory_arg = args_obj.get("subcategory").and_then(|v| v.as_str()).map(|s| s.to_lowercase());
                let risk_arg = args_obj.get("risk").and_then(|v| v.as_str()).map(|s| s.to_lowercase());
                let copy_from: Vec<String> = args_obj
                    .get("copy_from")
                    .and_then(|v| v.as_array())
                    .map(|a| a.iter().filter_map(|v| v.as_str()).map(|s| s.to_string()).collect())
                    .unwrap_or_default();

                // Validate: need either content or vision
                if content.is_none() && vision.is_none() {
                    return Ok(json!({
                        "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error": "add_skill requires either 'content' (markdown) or 'vision' (freeform description)."}))? }]
                    }));
                }

                // Load reference templates from copy_from
                let mut refs: Vec<SkillContent> = Vec::new();
                for ref_name in &copy_from {
                    if is_valid_skill_name(ref_name) {
                        if let Some(ref_data) = read_skill_content(skills_dir, ref_name) {
                            refs.push(ref_data);
                        }
                    }
                }

                // Delegate skill creation to the create_skill helper
                let create_args = CreateSkillArgs {
                    skills_dir: skills_dir.clone(),
                    name,
                    content,
                    vision,
                    trunk: trunk_arg,
                    subcategory: subcategory_arg,
                    risk: risk_arg,
                    refs,
                };
                match create_skill(create_args) {
                    Ok(result) => {
                        let payload = json!({
                            "created": result.name,
                            "path": result.path,
                            "trunk": result.trunk,
                            "subcategory": result.subcategory,
                            "risk": result.risk,
                            "message": result.message
                        });
                        Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&payload)? }] }))
                    }
                    Err(e) => {
                        let err_text = serde_json::to_string(&json!({"error": format!("Failed to create skill: {}", e)})).unwrap_or_else(|_| "{}".to_string());
                        Ok(json!({
                            "content": [{ "type": "text", "text": err_text }],
                            "isError": true
                        }))
                    }
                }
            }
            _ => Ok(json!({ "content": [{ "type": "text", "text": serde_json::to_string(&json!({"error": format!("Unknown tool: {}", name)}))? }], "isError": true })),
        }
    }

    pub fn handle_resources_list(&self, index: &SkillIndex, params: &Value) -> Result<Value> {
        let cursor = params.get("cursor").and_then(|v| v.as_str());
        let limit = params.get("limit").and_then(|v| v.as_u64()).map(|n| n as usize);
        let recs = index.all_skills();
        let resources: Vec<Value> = recs
            .iter()
            .map(|r| json!({
                "uri": format!("skill://{}", r.name),
                "name": r.name,
                "title": r.name.replace('-', " ").split_whitespace().map(|w| {
                    let mut c = w.chars();
                    match c.next() {
                        None => String::new(),
                        Some(f) => f.to_uppercase().collect::<String>() + c.as_str()
                    }
                }).collect::<Vec<_>>().join(" "),
                "description": r.description.chars().take(120).collect::<String>(),
                "mimeType": "text/markdown"
            }))
            .collect();
        let (page, next) = paginate(&resources, cursor, limit)?;
        let mut out = json!({ "resources": page });
        if let Some(n) = next { out["nextCursor"] = json!(n); }
        Ok(out)
    }

    pub fn handle_resources_read(
        &self,
        index: &SkillIndex,
        skills_dir: &PathBuf,
        params: &Value,
    ) -> Result<Value> {
        let uri = params.get("uri").and_then(|v| v.as_str()).ok_or_else(|| anyhow::anyhow!("Missing uri"))?;
        if !uri.starts_with("skill://") {
            anyhow::bail!("Invalid resource URI (expected skill://<name>): {}", uri);
        }
        let skill_name = uri[8..].trim();
        if !is_valid_skill_name(skill_name) {
            anyhow::bail!("Invalid skill name in URI: {}", uri);
        }
        // Security gate for resources/read (was missing — Bug #1)
        let rec = index.get(skill_name);
        let live_meta = index.cached_frontmatter(skill_name).unwrap_or_default();
        let live_cat = live_meta.get("category").map(|s| s.to_lowercase()).unwrap_or_default();
        let idx_cat = rec.as_ref().map(|r| r.category.to_lowercase()).unwrap_or_default();
        let eff_cat = if !live_cat.is_empty() { live_cat } else { idx_cat };
        if eff_cat == "security" {
            anyhow::bail!(
                "Skill '{}' is security-gated. Use tools/call get_skill with allow_security=true.",
                skill_name
            );
        }
        let data = read_skill_content(skills_dir, skill_name)
            .ok_or_else(|| anyhow::anyhow!("Skill '{}' not found", skill_name))?;
        Ok(json!({ "contents": [{ "uri": uri, "mimeType": "text/markdown", "text": serde_json::to_string(&data)? }] }))
    }

    pub fn handle_resources_templates_list(&self) -> Value {
        json!({
            "resourceTemplates": [{
                "uriTemplate": "skill://{name}",
                "name": "Skill by name",
                "description": "Load any skill by name as a resource",
                "mimeType": "text/markdown"
            }]
        })
    }

    pub fn handle_prompts_list(&self, index: &SkillIndex, params: &Value) -> Result<Value> {
        let cursor = params.get("cursor").and_then(|v| v.as_str());
        let limit = params.get("limit").and_then(|v| v.as_u64()).map(|n| n as usize);
        let recs = index.all_skills();
        let prompts: Vec<Value> = recs
            .iter()
            .map(|r| json!({
                "name": r.name,
                "title": r.name.replace('-', " ").split_whitespace().map(|w| {
                    let mut c = w.chars();
                    match c.next() { None => String::new(), Some(f) => f.to_uppercase().collect::<String>() + c.as_str() }
                }).collect::<Vec<_>>().join(" "),
                "description": r.description.chars().take(150).collect::<String>(),
                "arguments": []
            }))
            .collect();
        let (page, next) = paginate(&prompts, cursor, limit)?;
        let mut out = json!({ "prompts": page });
        if let Some(n) = next { out["nextCursor"] = json!(n); }
        Ok(out)
    }

    pub fn handle_prompts_get(
        &self,
        skills_dir: &PathBuf,
        params: &Value,
    ) -> Result<Value> {
        let name = params.get("name").and_then(|v| v.as_str()).unwrap_or("");
        if name.is_empty() || !is_valid_skill_name(name) {
            anyhow::bail!("Unknown prompt: {}", name);
        }
        let data = read_skill_content(skills_dir, name)
            .ok_or_else(|| anyhow::anyhow!("Prompt/skill '{}' not found", name))?;
        // Bug #1 fix: gate security skills via metadata check
        let cat = data.metadata.get("category").map(|s| s.to_lowercase()).unwrap_or_default();
        if cat == "security" {
            anyhow::bail!(
                "Skill '{}' is security-gated. Use tools/call get_skill with allow_security=true.",
                name
            );
        }
        let desc = data.metadata.get("description").cloned().unwrap_or_default();
        Ok(json!({
            "description": desc,
            "messages": [{ "role": "user", "content": { "type": "text", "text": data.content.chars().take(8000).collect::<String>() } }]
        }))
    }

    pub fn handle_completion(&self, index: &SkillIndex, params: &Value) -> Value {
        let arg = params.get("argument").cloned().unwrap_or(json!({}));
        let arg_name = arg.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let value = arg.get("value").and_then(|v| v.as_str()).unwrap_or("");
        if matches!(arg_name, "name" | "skill" | "prompt") {
            let recs = index.all_skills();
            let vals: Vec<String> = recs
                .iter()
                .filter(|r| r.name.to_lowercase().contains(&value.to_lowercase()))
                .take(20)
                .map(|r| r.name.clone())
                .collect();
            let total = vals.len();
            return json!({ "completion": { "values": vals, "total": total, "hasMore": false } });
        }
        json!({ "completion": { "values": [], "total": 0, "hasMore": false } })
    }
}

fn extract_headings(md: &str) -> Vec<String> {
    md.lines()
        .filter(|l| l.starts_with("## "))
        .map(|l| l.trim_start_matches("## ").trim().to_string())
        .take(20)
        .collect()
}

fn extract_section(md: &str, heading: &str) -> Option<String> {
    let target = heading.to_lowercase();
    let lines: Vec<&str> = md.lines().collect();
    let mut start: Option<usize> = None;
    let mut end = lines.len();
    for (i, line) in lines.iter().enumerate() {
        let t = line.trim();
        // Match any heading level (#, ##, ###, etc.)
        if t.starts_with('#') && t.find(' ').map(|sp| t[..=sp].trim_end().ends_with('#')).unwrap_or(false) {
            let h = t.trim_start_matches('#').trim().to_lowercase();
            if start.is_some() {
                end = i;
                break;
            }
            if h.contains(&target) || target.contains(&h) {
                start = Some(i);
            }
        }
    }
    start.map(|s| lines[s..end].join("\n"))
}

// ── Subcategory auto-classification helpers ────────────────────────────────

/// Find the best matching subcategory for a skill within a given trunk
fn find_matching_subcategory(r: &SkillRecord, trunk: &str) -> String {
    // Try to match by name/description keywords against known subcategories
    let hay = format!(
        "{} {} {} {} {}",
        r.name, r.description, r.category, r.tags.join(" "), r.intent.as_deref().unwrap_or("")
    )
    .to_lowercase();

    // Subcategory keyword patterns (all arrays normalized to 5 elements)
    let patterns = vec![
        ("agent-development", &["agent", "crewai", "langgraph", "autonomous", "orchestration"]),
        ("llm-application", &["llm", "gpt", "claude", "rag", "langchain"]),
        ("prompt-engineering", &["prompt", "jailbreak", "cot", "few-shot", "template"]),
        ("computer-vision", &["vision", "image", "cv", "detection", "recognition"]),
        ("voice-audio-ai", &["voice", "audio", "speech", "tts", "stt"]),
        ("ml-ops-engineering", &["mlops", "mlflow", "pipeline", "training", "deployment"]),
        ("frontend-frameworks", &["react", "vue", "angular", "svelte", "tailwind"]),
        ("fullstack-development", &["fullstack", "nextjs", "nuxt", "remix", "full-stack"]),
        ("web-automation", &["browser", "playwright", "puppeteer", "selenium", "scraping"]),
        ("web3-blockchain", &["web3", "blockchain", "solidity", "defi", "smart-contract"]),
        ("design-ux", &["design", "figma", "ux", "ui-design", "accessibility"]),
        ("api-design", &["api", "rest", "graphql", "grpc", "openapi"]),
        ("backend-frameworks", &["backend", "fastapi", "django", "nestjs", "express"]),
        ("serverless-functions", &["serverless", "lambda", "cloud-function", "azure-function", "faaS"]),
        ("ci-cd-pipelines", &["ci", "cd", "pipeline", "jenkins", "github-actions"]),
        ("containers-orchestration", &["docker", "kubernetes", "k8s", "container", "helm"]),
        ("infrastructure-iaas", &["terraform", "ansible", "cloudformation", "pulumi", "infracost"]),
        ("monitoring-observability", &["monitoring", "prometheus", "grafana", "datadog", "sentry"]),
        ("data-engineering-pipelines", &["data", "etl", "airflow", "dbt", "spark"]),
        ("analytics-visualization", &["analytics", "dashboard", "amplitude", "mixpanel", "posthog"]),
        ("ai-ml-engineering", &["ml", "mlops", "training", "inference", "model"]),
        ("aws", &["aws", "s3", "ec2", "lambda", "iam"]),
        ("azure", &["azure", "cosmos", "keyvault", "blob", "service-bus"]),
        ("gcp-google-cloud", &["gcp", "google-cloud", "cloud-run", "bigquery", "gcs"]),
        ("android", &["android", "jetpack", "kotlin", "compose", "androidx"]),
        ("ios", &["ios", "swift", "swiftui", "xcode", "apple"]),
        ("cross-platform", &["flutter", "react-native", "mobile", "cross-platform", "rn"]),
        ("sql-relational", &["sql", "postgres", "mysql", "sqlite", "mariadb"]),
        ("nosql", &["nosql", "mongodb", "redis", "dynamodb", "neo4j"]),
        ("vector-embedding", &["vector", "embedding", "pinecone", "weaviate", "qdrant"]),
        ("orm-odm", &["prisma", "orm", "sqlalchemy", "typeorm", "mongoose"]),
        ("application-security", &["xss", "sast", "code-review", "vulnerability", "owasp"]),
        ("penetration-testing", &["pentest", "penetration", "attack", "exploit", "metasploit"]),
        ("cloud-security", &["cloud-security", "cloud-hardening", "cloud-audit", "cloud-scan", "cloud-policy"]),
        ("auth-authorization", &["auth", "oauth", "jwt", "authorization", "rbac"]),
        ("compliance-forensics", &["compliance", "gdpr", "soc2", "pci", "hipaa"]),
        ("reverse-engineering", &["reverse", "decompil", "binary", "firmware", "malware"]),
        ("unit-integration", &["unit", "integration", "jest", "pytest", "tdd"]),
        ("e2e-acceptance", &["e2e", "end-to-end", "cypress", "playwright", "acceptance"]),
        ("performance-load", &["performance", "load-testing", "stress", "benchmark", "k6"]),
        ("security-testing", &["security-testing", "pentest", "pentesting", "security-scan", "sast"]),
    ];

    let mut best = ("other", 0usize);
    for (sub, kws) in patterns {
        let score = kws.iter().filter(|&&kw| hay.contains(kw)).count();
        if score > best.1 {
            best = (sub, score);
        }
    }
    best.0.to_string()
}

// ── Skill creation helpers ────────────────────────────────────────────────

struct CreateSkillArgs {
    skills_dir: PathBuf,
    name: Option<String>,
    content: Option<String>,
    vision: Option<String>,
    trunk: Option<String>,
    subcategory: Option<String>,
    risk: Option<String>,
    refs: Vec<SkillContent>,
}

struct CreateSkillResult {
    name: String,
    path: String,
    trunk: String,
    subcategory: String,
    risk: String,
    message: String,
}

/// Classify a skill into trunk + subcategory from content/vision
fn classify_skill_content(vision: &str, existing_content: &str) -> (String, String, String) {
    let hay = format!("{} {}", vision, existing_content).to_lowercase();

    // Determine trunk
    let trunk_kw = vec![
        ("ai-ml", &["agent", "llm", "gpt", "claude", "rag", "prompt", "computer-vision", "voice", "audio", "mlops", "ml-ops", "embedding", "vector"]),
        ("web", &["web", "frontend", "react", "vue", "angular", "nextjs", "tailwind", "javascript", "typescript", "css", "html", "3d", "web3"]),
        ("backend", &["backend", "api", "rest", "graphql", "grpc", "serverless", "fastapi", "django", "nestjs", "express", "python", "node", "ruby"]),
        ("security", &["security", "pentest", "pentesting", "hack", "vulnerability", "attack", "exploit", "auth", "oauth", "jwt", "sqli", "xss", "cwe"]),
        ("devops", &["devops", "docker", "kubernetes", "terraform", "ci-cd", "pipeline", "deploy", "ansible", "helm", "gitops", "cicd", "jenkins", "k8s"]),
        ("data", &["data", "analytics", "etl", "pipeline", "spark", "airflow", "dbt", "dashboard", "kafka", "clickhouse", "warehouse", "lake", "analytics"]),
        ("cloud", &["cloud", "aws", "azure", "gcp", "google-cloud", "serverless", "iaas", "paas", "saas", "cloud-run", "cloudfront", "s3", "ec2"]),
        ("mobile", &["mobile", "android", "ios", "flutter", "react-native", "swift", "kotlin", "compose", "swiftui", "expo", "app", "mobile", "cross-platform"]),
        ("database", &["database", "sql", "postgres", "mysql", "mongodb", "redis", "prisma", "orm", "nosql", "vector-db", "caching", "db", "db-admin"]),
        ("testing", &["test", "testing", "playwright", "cypress", "e2e", "unit", "tdd", "integration", "pytest", "jest", "load-test", "bench", "qa"]),
    ];
    let mut best_trunk = ("other", 0usize);
    for (trunk, kws) in trunk_kw {
        let score = kws.iter().filter(|&&kw| hay.contains(kw)).count();
        if score > best_trunk.1 {
            best_trunk = (trunk, score);
        }
    }

    let trunk = if best_trunk.0 == "other" {
        // Check if user provided an explicit trunk
        if let Some(t) = &TRUNK_CATEGORIES.iter().find(|(t, _)| hay.contains(t)) {
            t.0.to_string()
        } else {
            "ai-ml".to_string() // default safe fallback
        }
    } else {
        best_trunk.0.to_string()
    };

    let subcategory = find_matching_subcategory(
        &SkillRecord {
            name: String::new(),
            description: vision.to_string(),
            category: trunk.clone(),
            author: String::new(),
            version: String::new(),
            risk: String::new(),
            path: String::new(),
            tags: vec![],
            summary: Some(existing_content.to_string()),
            hash: None,
            cluster: trunk.clone(),
            intent: None,
            quality_score: None,
            trunk: Some(trunk.clone()),
            subcategory: None,
        },
        &trunk,
    );

    // Determine risk
    let risk_kw = &["pentest", "pentesting", "hack", "vulnerability", "exploit", "attack", "security-audit", "forensic", "reverse-engineer"];
    let risk = if risk_kw.iter().any(|kw| hay.contains(kw)) {
        "high".to_string()
    } else {
        "safe".to_string()
    };

    (trunk, subcategory, risk)
}

/// Generate a kebab-case name from vision text
fn infer_name_from_vision(vision: &str, content: &str) -> String {
    let hay = format!("{} {}", vision, content).to_lowercase();
    // Try to extract first heading as name
    if let Some(heading) = hay.lines().find(|l| l.starts_with('#')) {
        let name = heading.trim_start_matches('#').trim();
        if !name.is_empty() {
            return slugify(name);
        }
    }
    // Fallback: use first 5 words
    let words: Vec<&str> = hay.split_whitespace().take(5).collect();
    slugify(&words.join("-"))
}

fn slugify(s: &str) -> String {
    s.to_lowercase()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect::<String>()
        .split('-')
        .filter(|p| !p.is_empty())
        .take(6)
        .collect::<Vec<_>>()
        .join("-")
        .chars()
        .take(64)
        .collect()
}

fn create_skill(args: CreateSkillArgs) -> Result<CreateSkillResult, String> {
    let content = args.content.unwrap_or_default();
    let vision = args.vision.unwrap_or_default();

    // Determine name
    let name = args
        .name
        .or_else(|| if !vision.is_empty() || !content.is_empty() {
            Some(infer_name_from_vision(&vision, &content))
        } else {
            None
        })
        .ok_or("Could not determine skill name. Provide name or content/vision.".to_string())?;

    if !is_valid_skill_name(&name) {
        return Err(format!("Invalid skill name '{}'. Must be lowercase kebab-case, 1-64 chars.", name));
    }

    // Check for duplicate
    let skill_dir = args.skills_dir.join(&name);
    if skill_dir.exists() {
        return Err(format!("Skill '{}' already exists at {}.", name, skill_dir.display()));
    }

    // Classify if not provided
    let (trunk, subcategory, risk) = if let Some(ref t) = args.trunk {
        (t.clone(), args.subcategory.clone().unwrap_or("other".to_string()), args.risk.clone().unwrap_or("safe".to_string()))
    } else {
        let (t, s, r) = classify_skill_content(&vision, &content);
        (t, s, args.risk.unwrap_or(r))
    };

    // Build SKILL.md content from references, content, or vision
    let skill_md = build_skill_md(&name, &vision, &content, &trunk, &subcategory, &risk, &args.refs);

    // Write SKILL.md
    std::fs::create_dir_all(&skill_dir).map_err(|e| format!("Failed to create directory: {}", e))?;
    let skill_file = skill_dir.join("SKILL.md");
    std::fs::write(&skill_file, skill_md).map_err(|e| format!("Failed to write SKILL.md: {}", e))?;

    let path = format!("skills/{}/SKILL.md", name);
    let message = format!("Skill '{}' created at {}. Trunk: {}, Subcategory: {}.", name, path, trunk, subcategory);
    tracing::info!("{}", message);

    Ok(CreateSkillResult {
        name,
        path,
        trunk,
        subcategory,
        risk,
        message,
    })
}

/// Build a SKILL.md with frontmatter + body
fn build_skill_md(
    name: &str,
    vision: &str,
    content: &str,
    trunk: &str,
    subcategory: &str,
    risk: &str,
    refs: &[SkillContent],
) -> String {
    // If content has frontmatter already, use it as-is
    if content.starts_with("---") {
        return content.to_string();
    }

    // Determine description
    let description = if !vision.is_empty() {
        vision.lines().next().unwrap_or(vision).chars().take(300).collect::<String>()
    } else if let Some(ref r) = refs.first() {
        r.metadata.get("description").cloned().unwrap_or_default()
    } else {
        format!("{} skill", name.replace('-', " ").to_lowercase())
    };

    // Determine author
    let author = refs
        .first()
        .and_then(|r| r.metadata.get("author"))
        .cloned()
        .unwrap_or_else(|| "ncdevshiv".to_string());

    let mut md = String::new();
    md.push_str("---\n");
    md.push_str(&format!("name: {}\n", name));
    md.push_str(&format!("description: \"{}\"\n", description));
    md.push_str("metadata:\n");
    md.push_str(&format!("  author: {}\n", author));
    md.push_str(&format!("  version: \"1.0\"\n"));
    md.push_str(&format!("  category: {}\n", trunk));
    md.push_str(&format!("  updated: \"{}\"\n", chrono::Local::now().format("%Y-%m-%d")));
    md.push_str("tags:\n");
    // Generate tags from name + trunk + subcategory
    let tags: Vec<&str> = vec![name, trunk, subcategory]
        .iter()
        .flat_map(|s| s.split('-'))
        .filter(|t| t.len() >= 3)
        .take(5)
        .collect();
    for tag in &tags {
        md.push_str(&format!("  - {}\n", tag));
    }
    md.push_str(&format!("risk: {}\n", risk));
    md.push_str(&format!("trunk: {}\n", trunk));
    md.push_str(&format!("subcategory: {}\n", subcategory));
    md.push_str("---\n\n");

    // Body: use content if provided, else derive from vision or reference
    if !content.is_empty() {
        md.push_str(&content);
    } else if !vision.is_empty() {
        md.push_str("# ");
        md.push_str(&name.replace('-', " ").to_string());
        md.push('\n');
        md.push_str("\n");
        md.push_str(&vision);
    } else if let Some(ref r) = refs.first() {
        // Use first reference as template
        let template_body = r
            .content
            .split("---")
            .nth(2)
            .unwrap_or("")
            .trim()
            .to_string();
        md.push_str(&template_body);
    } else {
        md.push_str("TODO: Complete the skill content.\n");
    }

    md
}

/// chrono is needed for date formatting; if not available, fallback
use chrono;
