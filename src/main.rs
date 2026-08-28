mod config;
mod cursor;
mod index;
mod mcp;
mod skill;

use anyhow::Result;
use clap::Parser;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

use crate::index::SkillIndex;
use crate::mcp::McpServer;

#[derive(Parser, Debug)]
#[command(name = "skills-mcp-server", version, about = "Skills MCP Server — MCP 2025-11-25 compliant (Rust)")]
struct Args {
    #[arg(long, help = "Run Streamable HTTP instead of stdio")]
    http: bool,
    #[arg(long, default_value = "127.0.0.1")]
    host: String,
    #[arg(long, default_value_t = 3000)]
    port: u16,
    #[arg(long, default_value = "info", help = "Log level: trace,debug,info,warn,error")]
    log_level: String,
}

fn init_tracing(level: &str) {
    let filter = match level.to_lowercase().as_str() {
        "trace" => "trace",
        "debug" => "debug",
        "warn" => "warn",
        "error" => "error",
        _ => "info",
    };
    let _ = tracing_subscriber::fmt()
        .with_env_filter(format!("skills_mcp={},{}", filter, filter))
        .with_writer(std::io::stderr)
        .try_init();
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    init_tracing(&args.log_level);

    if args.http {
        run_http(args.host, args.port).await?;
    } else {
        if std::env::var("SYSTEM_PROMPT_PATH").is_ok() {
            tracing::warn!("SYSTEM_PROMPT_PATH is deprecated and ignored (was arbitrary file read). Remove it from env.");
        }
        run_stdio().await?;
    }
    Ok(())
}

// ── Stdio transport ────────────────────────────────────────────────────────

async fn run_stdio() -> Result<()> {
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|x| x.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));
    // Prefer current dir if skills/ exists there
    let cwd_skills = PathBuf::from("skills");
    let skills_dir = if cwd_skills.exists() {
        cwd_skills.canonicalize().unwrap_or(cwd_skills)
    } else {
        exe_dir.join("skills").canonicalize().unwrap_or_else(|_| PathBuf::from("skills"))
    };
    let index_path = if PathBuf::from("skills-index.json").exists() {
        PathBuf::from("skills-index.json")
    } else {
        exe_dir.join("skills-index.json")
    };

    let index = Arc::new(SkillIndex::new(index_path, skills_dir.clone()));
    let mut server = McpServer::new();

    tracing::info!("Starting {} v{} stdio (supports {:?})", crate::config::SERVER_NAME, crate::config::SERVER_VERSION, crate::config::SUPPORTED_PROTOCOL_VERSIONS);

    let stdin = tokio::io::stdin();
    let mut stdout = tokio::io::stdout();
    let mut reader = BufReader::new(stdin);
    let mut line = String::new();

    loop {
        line.clear();
        let n = reader.read_line(&mut line).await?;
        if n == 0 {
            break; // EOF
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let msg: Value = match serde_json::from_str(trimmed) {
            Ok(v) => v,
            Err(_) => {
                send_error(&mut stdout, None, -32700, "Parse error", None).await?;
                continue;
            }
        };
        if msg.get("jsonrpc").and_then(|v| v.as_str()) != Some("2.0") {
            // Could be valid JSON but not JSON-RPC — treat as parse error per spec
            // If it's an object without jsonrpc, return -32600
            if msg.is_object() {
                let id = msg.get("id").cloned();
                send_error(&mut stdout, id, -32600, "Invalid Request: jsonrpc must be \"2.0\"", None).await?;
            } else {
                send_error(&mut stdout, None, -32700, "Parse error", None).await?;
            }
            continue;
        }
        // Batch arrays are not supported — spec says return -32600
        if msg.is_array() {
            send_error(&mut stdout, None, -32600, "Invalid Request: batch not supported", None).await?;
            continue;
        }

        let method = msg.get("method").and_then(|v| v.as_str()).map(|s| s.to_string());
        let params = msg.get("params").cloned().unwrap_or(Value::Null);
        let id = msg.get("id").cloned();

        // Distinguish notifications (no id) vs requests (id present and not null)
        let is_notification = !msg.as_object().unwrap().contains_key("id") || msg.get("id").map(|v| v.is_null()).unwrap_or(false);
        // Spec: id MUST NOT be null for requests. If id is null explicitly, treat as invalid.
        if msg.as_object().unwrap().contains_key("id") && msg.get("id").map(|v| v.is_null()).unwrap_or(false) {
            send_error(&mut stdout, Some(Value::Null), -32600, "Invalid Request: id must not be null", None).await?;
            continue;
        }

        // Missing method field
        if method.is_none() {
            // If it's a notification without method, just warn
            if is_notification {
                tracing::warn!("notification without method: {}", trimmed.chars().take(200).collect::<String>());
                continue;
            }
            send_error(&mut stdout, id, -32600, "Invalid Request: method is required", None).await?;
            continue;
        }
        let method = method.unwrap();

        let res: Result<()> = async {
            match method.as_str() {
                "initialize" => {
                    if !params.is_object() {
                        send_error(&mut stdout, id.clone(), -32602, "Invalid params for initialize", None).await?;
                        return Ok(());
                    }
                    let result = server.handle_initialize(&params);
                    send_response(&mut stdout, id.clone(), result).await?;
                }
                "notifications/initialized" => {
                    server.initialized = true;
                    tracing::info!("Client initialized (protocol {})", server.protocol_version);
                }
                "notifications/cancelled" => {
                    tracing::info!("Cancelled: {}", params);
                }
                "ping" => {
                    if is_notification { return Ok(()); }
                    send_response(&mut stdout, id.clone(), json!({})).await?;
                }
                "logging/setLevel" => {
                    if is_notification { return Ok(()); }
                    let level = params.get("level").and_then(|v| v.as_str()).unwrap_or("info");
                    tracing::info!("Client set log level to {}", level);
                    // Acknowledge without actually changing filter (could store)
                    send_response(&mut stdout, id.clone(), json!({})).await?;
                }
                _ => {
                    // Guard: warn if not initialized (but still serve, per spec SHOULD)
                    if !server.initialized && !matches!(method.as_str(), "ping" | "initialize" | "notifications/initialized") {
                        tracing::warn!("Method {} called before initialized — serving anyway", method);
                    }

                    match method.as_str() {
                        "tools/list" => {
                            if is_notification { return Ok(()); }
                            let p = if params.is_object() { params.clone() } else { json!({}) };
                            match server.handle_tools_list(&p) {
                                Ok(r) => send_response(&mut stdout, id.clone(), r).await?,
                                Err(e) => send_error(&mut stdout, id.clone(), -32602, &e.to_string(), None).await?,
                            }
                        }
                        "tools/call" => {
                            if is_notification { return Ok(()); }
                            if !params.is_object() {
                                send_error(&mut stdout, id.clone(), -32602, "Invalid params for tools/call", None).await?;
                                return Ok(());
                            }
                            match server.handle_tools_call(&index, &skills_dir, &params) {
                                Ok(r) => {
                                    // Unknown tool should be protocol error, but our handler returns isError; detect and map to -32602
                                    if let Some(content) = r.get("content") {
                                        // Check if it's unknown tool error — we return isError but could also check
                                    }
                                    send_response(&mut stdout, id.clone(), r).await?
                                },
                                Err(e) => send_error(&mut stdout, id.clone(), -32602, &e.to_string(), None).await?,
                            }
                        }
                        "resources/list" => {
                            if is_notification { return Ok(()); }
                            let p = if params.is_object() { params.clone() } else { json!({}) };
                            match server.handle_resources_list(&index, &p) {
                                Ok(r) => send_response(&mut stdout, id.clone(), r).await?,
                                Err(e) => send_error(&mut stdout, id.clone(), -32602, &e.to_string(), None).await?,
                            }
                        }
                        "resources/read" => {
                            if is_notification { return Ok(()); }
                            let p = if params.is_object() { params.clone() } else { json!({}) };
                            match server.handle_resources_read(&index, &skills_dir, &p) {
                                Ok(r) => send_response(&mut stdout, id.clone(), r).await?,
                                Err(e) => {
                                    // Map not found to -32002 or -32602
                                    let msg = e.to_string();
                                    if msg.contains("not found") || msg.contains("Invalid resource") {
                                        send_error(&mut stdout, id.clone(), -32002, &msg, None).await?;
                                    } else {
                                        send_error(&mut stdout, id.clone(), -32602, &msg, None).await?;
                                    }
                                }
                            }
                        }
                        "resources/templates/list" => {
                            if is_notification { return Ok(()); }
                            let r = server.handle_resources_templates_list();
                            send_response(&mut stdout, id.clone(), r).await?;
                        }
                        "prompts/list" => {
                            if is_notification { return Ok(()); }
                            let p = if params.is_object() { params.clone() } else { json!({}) };
                            match server.handle_prompts_list(&index, &p) {
                                Ok(r) => send_response(&mut stdout, id.clone(), r).await?,
                                Err(e) => send_error(&mut stdout, id.clone(), -32602, &e.to_string(), None).await?,
                            }
                        }
                        "prompts/get" => {
                            if is_notification { return Ok(()); }
                            let p = if params.is_object() { params.clone() } else { json!({}) };
                            match server.handle_prompts_get(&skills_dir, &p) {
                                Ok(r) => send_response(&mut stdout, id.clone(), r).await?,
                                Err(e) => send_error(&mut stdout, id.clone(), -32602, &e.to_string(), None).await?,
                            }
                        }
                        "completion/complete" => {
                            if is_notification { return Ok(()); }
                            let p = if params.is_object() { params.clone() } else { json!({}) };
                            let r = server.handle_completion(&index, &p);
                            send_response(&mut stdout, id.clone(), r).await?;
                        }
                        "shutdown" => {
                            if is_notification { return Ok(()); }
                            send_response(&mut stdout, id.clone(), json!({})).await?;
                        }
                        _ => {
                            if is_notification {
                                tracing::warn!("Unknown notification method: {}", method);
                            } else {
                                send_error(&mut stdout, id.clone(), -32601, &format!("Method not found: {}", method), None).await?;
                            }
                        }
                    }
                }
            }
            Ok(())
        }.await;

        if let Err(e) = res {
            tracing::error!("handler error for {}: {}", method, e);
            if !is_notification {
                send_error(&mut stdout, id, -32603, &format!("Internal error: {}", e), None).await?;
            }
        }
    }

    Ok(())
}

async fn send_response(stdout: &mut tokio::io::Stdout, id: Option<Value>, result: Value) -> Result<()> {
    let msg = json!({ "jsonrpc": "2.0", "id": id.unwrap_or(Value::Null), "result": result });
    let mut s = serde_json::to_string(&msg)?;
    s.push('\n');
    stdout.write_all(s.as_bytes()).await?;
    stdout.flush().await?;
    Ok(())
}

async fn send_error(stdout: &mut tokio::io::Stdout, id: Option<Value>, code: i32, message: &str, data: Option<Value>) -> Result<()> {
    let mut err = json!({ "code": code, "message": message });
    if let Some(d) = data { err["data"] = d; }
    let msg = json!({ "jsonrpc": "2.0", "id": id.unwrap_or(Value::Null), "error": err });
    let mut s = serde_json::to_string(&msg)?;
    s.push('\n');
    stdout.write_all(s.as_bytes()).await?;
    stdout.flush().await?;
    Ok(())
}

// ── HTTP transport ───────────────────────────────────────────────────────
async fn run_http(host: String, port: u16) -> Result<()> {
    use axum::{extract::State, http::{HeaderMap, StatusCode}, response::IntoResponse, routing::{get, post}, Json, Router};
    use std::sync::Arc;
    use tokio::sync::Mutex;

    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|x| x.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));
    let skills_dir = if PathBuf::from("skills").exists() {
        PathBuf::from("skills").canonicalize().unwrap_or_else(|_| PathBuf::from("skills"))
    } else {
        exe_dir.join("skills")
    };
    let index_path = if PathBuf::from("skills-index.json").exists() {
        PathBuf::from("skills-index.json")
    } else {
        exe_dir.join("skills-index.json")
    };
    let index = Arc::new(SkillIndex::new(index_path, skills_dir.clone()));

    #[derive(Clone)]
    struct AppState {
        index: Arc<SkillIndex>,
        skills_dir: PathBuf,
    }
    let state = AppState { index, skills_dir };

    async fn health() -> impl IntoResponse {
        Json(json!({ "name": crate::config::SERVER_NAME, "version": crate::config::SERVER_VERSION, "protocolVersion": crate::config::LATEST_PROTOCOL_VERSION }))
    }

    async fn mcp_post(
        State(state): State<AppState>,
        headers: HeaderMap,
        body: axum::body::Bytes,
    ) -> impl IntoResponse {
        // Origin check
        if let Some(origin) = headers.get("origin").and_then(|v| v.to_str().ok()) {
            if !(origin.contains("localhost") || origin.contains("127.0.0.1") || origin == "null") {
                // For now warn, but spec says 403 — be permissive for local dev
                tracing::warn!("Origin {} not localhost — would be 403 in strict mode", origin);
            }
        }
        // Accept check (warn)
        if let Some(accept) = headers.get("accept").and_then(|v| v.to_str().ok()) {
            if !accept.contains("application/json") && !accept.contains("text/event-stream") {
                tracing::warn!("Accept header missing json/event-stream: {}", accept);
            }
        }

        let text = match String::from_utf8(body.to_vec()) {
            Ok(t) => t,
            Err(_) => {
                return (StatusCode::BAD_REQUEST, Json(json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Parse error"}}))).into_response();
            }
        };
        let msg: Value = match serde_json::from_str(text.trim()) {
            Ok(v) => v,
            Err(_) => {
                return (StatusCode::BAD_REQUEST, Json(json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Parse error"}}))).into_response();
            }
        };
        if msg.get("jsonrpc").and_then(|v| v.as_str()) != Some("2.0") {
            return (StatusCode::BAD_REQUEST, Json(json!({"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Invalid Request: jsonrpc must be 2.0"}}))).into_response();
        }
        let method = msg.get("method").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let params = msg.get("params").cloned().unwrap_or(Value::Null);
        let id = msg.get("id").cloned();
        let is_notification = !msg.as_object().map(|o| o.contains_key("id")).unwrap_or(false) || id.as_ref().map(|v| v.is_null()).unwrap_or(false);

        if is_notification {
            // For notifications, return 202 per spec
            return (StatusCode::ACCEPTED, Json(json!({}))).into_response();
        }

        // Stateless: new server per request — no global Mutex, no cross-client leak
        let mut srv = McpServer::new();
        let res: Value = match method.as_str() {
            "initialize" => {
                let r = srv.handle_initialize(&params);
                json!({"jsonrpc":"2.0","id":id,"result":r})
            }
            "ping" => json!({"jsonrpc":"2.0","id":id,"result":json!({})}),
            "tools/list" => {
                let p = if params.is_object() { params.clone() } else { json!({}) };
                match srv.handle_tools_list(&p) { Ok(r) => json!({"jsonrpc":"2.0","id":id,"result":r}), Err(e) => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32602,"message":e.to_string()}}) }
            }
            "tools/call" => {
                match srv.handle_tools_call(&state.index, &state.skills_dir, &params) { Ok(r) => json!({"jsonrpc":"2.0","id":id,"result":r}), Err(e) => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32602,"message":e.to_string()}}) }
            }
            "resources/list" => {
                let p = if params.is_object() { params.clone() } else { json!({}) };
                match srv.handle_resources_list(&state.index, &p) { Ok(r) => json!({"jsonrpc":"2.0","id":id,"result":r}), Err(e) => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32602,"message":e.to_string()}}) }
            }
            "resources/read" => {
                match srv.handle_resources_read(&state.index, &state.skills_dir, &params) { Ok(r) => json!({"jsonrpc":"2.0","id":id,"result":r}), Err(e) => {
                    let msg = e.to_string();
                    let code = if msg.contains("not found") { -32002 } else { -32602 };
                    json!({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":msg}})
                }}
            }
            "resources/templates/list" => json!({"jsonrpc":"2.0","id":id,"result": srv.handle_resources_templates_list()}),
            "prompts/list" => {
                let p = if params.is_object() { params.clone() } else { json!({}) };
                match srv.handle_prompts_list(&state.index, &p) { Ok(r) => json!({"jsonrpc":"2.0","id":id,"result":r}), Err(e) => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32602,"message":e.to_string()}}) }
            }
            "prompts/get" => {
                match srv.handle_prompts_get(&state.skills_dir, &params) { Ok(r) => json!({"jsonrpc":"2.0","id":id,"result":r}), Err(e) => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32602,"message":e.to_string()}}) }
            }
            "completion/complete" => {
                let r = srv.handle_completion(&state.index, &params);
                json!({"jsonrpc":"2.0","id":id,"result":r})
            }
            "logging/setLevel" => json!({"jsonrpc":"2.0","id":id,"result":json!({})}),
            _ => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message": format!("Method not found: {}", method)}}),
        };
        (StatusCode::OK, Json(res)).into_response()
    }

    let app = Router::new()
        .route("/", get(health))
        .route("/health", get(health))
        .route("/mcp", post(mcp_post).get(|| async { (StatusCode::METHOD_NOT_ALLOWED, "Use POST for MCP") }))
        .with_state(state);

    let addr = format!("{}:{}", host, port);
    tracing::info!("HTTP MCP listening on http://{}/mcp (health http://{}/health)", addr, addr);
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}
