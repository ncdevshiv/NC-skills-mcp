pub const SERVER_NAME: &str = "skills-mcp-server";
pub const SERVER_VERSION: &str = "2.1.0";

pub const SUPPORTED_PROTOCOL_VERSIONS: &[&str] = &[
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
];
pub const LATEST_PROTOCOL_VERSION: &str = "2025-11-25";

pub const DEFAULT_PAGE_SIZE: usize = 20;
pub const MAX_PAGE_SIZE: usize = 50;
pub const MAX_GET_SKILL_CHARS: usize = 30_000;
pub const TRUNCATE_NOTICE: &str =
    "\n\n---[TRUNCATED: skill body exceeds {limit} chars. Use get_skill with excerpt or read via resources/read with skill://{name}]---";

/// Valid risk taxonomy (standardized)
pub const VALID_RISKS: &[&str] = &["safe", "medium", "high", "unknown"];
/// Valid trunk roots in the taxonomy tree
pub const VALID_TRUNKS: &[&str] = &[
    "ai-ml",
    "web",
    "backend",
    "security",
    "devops",
    "data",
    "cloud",
    "mobile",
    "database",
    "testing",
];
/// Canonical trunk -> subcategory mapping (exhaustive)
pub const TAXONOMY: &[(/* trunk */ &str, /* sub */ &str)] = &[
    ("ai-ml", "agent-development"),
    ("ai-ml", "llm-application"),
    ("ai-ml", "prompt-engineering"),
    ("ai-ml", "computer-vision"),
    ("ai-ml", "voice-audio-ai"),
    ("ai-ml", "ml-ops-engineering"),
    ("web", "frontend-frameworks"),
    ("web", "fullstack-development"),
    ("web", "web-automation"),
    ("web", "web3-blockchain"),
    ("web", "design-ux"),
    ("backend", "api-design"),
    ("backend", "backend-frameworks"),
    ("backend", "serverless-functions"),
    ("devops", "ci-cd-pipelines"),
    ("devops", "containers-orchestration"),
    ("devops", "infrastructure-iaas"),
    ("devops", "monitoring-observability"),
    ("data", "data-engineering-pipelines"),
    ("data", "analytics-visualization"),
    ("data", "ai-ml-engineering"),
    ("cloud", "aws"),
    ("cloud", "azure"),
    ("cloud", "gcp-google-cloud"),
    ("mobile", "android"),
    ("mobile", "ios"),
    ("mobile", "cross-platform"),
    ("database", "sql-relational"),
    ("database", "nosql"),
    ("database", "vector-embedding"),
    ("database", "orm-odm"),
    ("security", "application-security"),
    ("security", "penetration-testing"),
    ("security", "cloud-security"),
    ("security", "auth-authorization"),
    ("security", "compliance-forensics"),
    ("security", "reverse-engineering"),
    ("testing", "unit-integration"),
    ("testing", "e2e-acceptance"),
    ("testing", "performance-load"),
    ("testing", "security-testing"),
];
/// Canonical trunk -> category mapping
pub const TRUNK_CATEGORIES: &[(/* trunk */ &str, /* cat */ &str)] = &[
    ("ai-ml", "ai"),
    ("web", "web"),
    ("backend", "backend"),
    ("devops", "devops"),
    ("data", "data"),
    ("cloud", "cloud"),
    ("mobile", "mobile"),
    ("database", "database"),
    ("security", "security"),
    ("testing", "testing"),
];

/// Standardize a risk value to the known taxonomy
pub fn normalize_risk(raw: &str) -> &str {
    let r = raw.trim().to_lowercase();
    match r.as_str() {
        "safe" | "official" => "safe",
        "medium" | "moderate" => "medium",
        "high" | "critical" => "high",
        "unknown" | "none" | "" => "unknown",
        _ => "unknown",
    }
}

/// Validate a trunk name
pub fn is_valid_trunk(t: &str) -> bool {
    VALID_TRUNKS.contains(&t.to_lowercase().as_str())
}
/// Validate a subcategory name
pub fn is_valid_subcategory(s: &str) -> bool {
    TAXONOMY.iter().any(|(_, sub)| sub == &s)
}

/// Allowlist for skill names — mirrors Python SKILL_NAME_RE_LOOSE
/// `^[a-z0-9][a-z0-9\-_\.]{1,80}$` case-insensitive
pub fn is_valid_skill_name(name: &str) -> bool {
    if name.is_empty() || name.len() > 80 {
        return false;
    }
    let mut chars = name.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphanumeric() => {},
        _ => return false,
    }
    for c in chars {
        if !(c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.') {
            return false;
        }
    }
    true
}
