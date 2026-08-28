use anyhow::Result;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::config::{is_valid_skill_name, MAX_GET_SKILL_CHARS, TRUNCATE_NOTICE};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SkillRecord {
    pub name: String,
    pub description: String,
    pub category: String,
    pub author: String,
    pub version: String,
    pub risk: String,
    pub path: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub summary: Option<String>,
    #[serde(default)]
    pub hash: Option<String>,
    #[serde(default)]
    pub cluster: String,
    #[serde(default)]
    pub intent: Option<String>,
    #[serde(default)]
    pub quality_score: Option<f64>,
    #[serde(default)]
    pub trunk: Option<String>,
    #[serde(default)]
    pub subcategory: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SkillContent {
    pub name: String,
    pub metadata: HashMap<String, String>,
    pub content: String,
    pub readme: Option<String>,
    pub truncated: bool,
    pub risk: String,
}

fn parse_frontmatter(path: &Path) -> HashMap<String, String> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(_) => return HashMap::new(),
    };
    if !text.starts_with("---") {
        return HashMap::new();
    }
    // Find second ---
    let rest = &text[3..];
    let end = match rest.find("---") {
        Some(i) => i,
        None => return HashMap::new(),
    };
    let fm = rest[..end].trim();

    // Try serde_yaml first
    if let Ok(yaml) = serde_yaml::from_str::<serde_yaml::Value>(fm) {
        if let Some(map) = yaml.as_mapping() {
            let mut out = HashMap::new();
            // Flatten metadata.* if present
            let mut meta_extra: HashMap<String, String> = HashMap::new();
            if let Some(meta) = map.get(&serde_yaml::Value::String("metadata".into())) {
                if let Some(m) = meta.as_mapping() {
                    for (k, v) in m {
                        if let Some(ks) = k.as_str() {
                            let vs = yaml_value_to_string(v);
                            meta_extra.insert(ks.to_string(), vs);
                        }
                    }
                }
            }
            for (k, v) in map {
                if let Some(ks) = k.as_str() {
                    if ks == "metadata" {
                        continue;
                    }
                    let vs = yaml_value_to_string(&v);
                    // Handle tags as comma-separated
                    if ks == "tags" && v.is_sequence() {
                        // already handled via vs but keep
                    }
                    out.insert(ks.to_string(), vs);
                }
            }
            for (k, v) in meta_extra {
                out.entry(k).or_insert(v);
            }
            // Normalize tags if list
            if let Some(tags) = out.get("tags") {
                // keep as is, index will split
                let _ = tags;
            }
            return out;
        }
    }

    // Fallback: line split
    let mut out = HashMap::new();
    for line in fm.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(idx) = line.find(':') {
            let k = line[..idx].trim().to_string();
            let v = line[idx + 1..].trim().trim_matches(|c| c == '"' || c == '\'').to_string();
            out.insert(k, v);
        }
    }
    out
}

fn yaml_value_to_string(v: &serde_yaml::Value) -> String {
    match v {
        serde_yaml::Value::Null => String::new(),
        serde_yaml::Value::Bool(b) => b.to_string(),
        serde_yaml::Value::Number(n) => n.to_string(),
        serde_yaml::Value::String(s) => s.clone(),
        serde_yaml::Value::Sequence(seq) => seq
            .iter()
            .map(yaml_value_to_string)
            .collect::<Vec<_>>()
            .join(", "),
        serde_yaml::Value::Mapping(m) => {
            // For nested, just debug
            let mut s = String::new();
            for (k, v) in m {
                s.push_str(&format!("{}:{} ", yaml_value_to_string(k), yaml_value_to_string(v)));
            }
            s.trim().to_string()
        }
        serde_yaml::Value::Tagged(t) => yaml_value_to_string(&t.value),
    }
}

pub fn read_skill_content(skills_dir: &Path, skill_name: &str) -> Option<SkillContent> {
    if !is_valid_skill_name(skill_name) {
        return None;
    }
    let skill_path = skills_dir.join(skill_name);
    // Canonicalize and ensure inside skills_dir
    let canonical_skills = skills_dir.canonicalize().ok()?;
    let canonical_skill = skill_path.canonicalize().ok()?;
    if !canonical_skill.starts_with(&canonical_skills) {
        return None;
    }
    if !skill_path.is_dir() {
        return None;
    }
    let skill_file = skill_path.join("SKILL.md");
    if !skill_file.exists() {
        return None;
    }
    let content_raw = std::fs::read_to_string(&skill_file).ok()?;
    let meta = parse_frontmatter(&skill_file);

    let truncated;
    let content = if content_raw.len() > MAX_GET_SKILL_CHARS {
        truncated = true;
        let notice = TRUNCATE_NOTICE.replace("{limit}", &MAX_GET_SKILL_CHARS.to_string());
        format!("{}{}", &content_raw[..MAX_GET_SKILL_CHARS], notice)
    } else {
        truncated = false;
        content_raw
    };

    let readme = {
        let p = skill_path.join("README.md");
        if p.exists() {
            std::fs::read_to_string(&p).ok().map(|mut s| {
                if s.len() > 5000 {
                    s.truncate(5000);
                    s.push_str("\n---[README truncated]---");
                }
                s
            })
        } else {
            None
        }
    };

    // Risk from meta or unknown
    let risk = meta.get("risk").cloned().unwrap_or_else(|| "unknown".into());

    Some(SkillContent {
        name: skill_name.to_string(),
        metadata: meta,
        content,
        readme,
        truncated,
        risk,
    })
}

pub fn get_skill_metadata(skills_dir: &Path, skill_name: &str) -> HashMap<String, String> {
    let p = skills_dir.join(skill_name).join("SKILL.md");
    parse_frontmatter(&p)
}

// Compute SHA256 of a file, return first 16 hex chars (matches Python index)
pub fn file_hash(path: &Path) -> Option<String> {
    let data = std::fs::read(path).ok()?;
    let mut hasher = Sha256::new();
    hasher.update(&data);
    let digest = hasher.finalize();
    Some(format!("{:x}", digest).chars().take(16).collect::<String>())
}

// Expose parse for index
pub fn parse_frontmatter_public(path: &Path) -> HashMap<String, String> {
    parse_frontmatter(path)
}
