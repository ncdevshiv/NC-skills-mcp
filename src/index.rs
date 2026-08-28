use anyhow::Result;
use moka::sync::Cache;
use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::RwLock;
use std::time::{Duration, Instant};

use crate::config::{is_valid_skill_name, normalize_risk, MAX_PAGE_SIZE};
use crate::skill::{parse_frontmatter_public, SkillRecord};
use rusqlite::Connection;

#[derive(Debug, Serialize, Deserialize, Clone)]
struct IndexEntry {
    name: String,
    description: String,
    category: String,
    author: String,
    version: String,
    risk: String,
    path: String,
    #[serde(default)]
    tags: serde_yaml::Value,
    #[serde(default)]
    summary: Option<String>,
    #[serde(default)]
    hash: Option<String>,
    #[serde(default)]
    cluster: Option<String>,
    #[serde(default)]
    intent: Option<String>,
    #[serde(default)]
    quality_score: Option<f64>,
    #[serde(default)]
    trunk: Option<String>,
    #[serde(default)]
    subcategory: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct IndexFile {
    version: String,
    generated: String,
    skills: Vec<IndexEntry>,
}

pub struct SkillIndex {
    index_path: PathBuf,
    skills_dir: PathBuf,
    inner: RwLock<Inner>,
    last_check: RwLock<Instant>,
    frontmatter_cache: Cache<String, HashMap<String, String>>,
}

struct Inner {
    skills: Vec<SkillRecord>,
    by_name: HashMap<String, SkillRecord>,
    inverted: HashMap<String, Vec<usize>>, // term -> posting list
    clusters: HashMap<String, Vec<String>>, // cluster -> [skill names]
    mtime: Option<std::time::SystemTime>,
}

impl SkillIndex {
    pub fn new(index_path: PathBuf, skills_dir: PathBuf) -> Self {
        let s = Self {
            index_path,
            skills_dir,
            inner: RwLock::new(Inner {
                skills: Vec::new(),
                by_name: HashMap::new(),
                inverted: HashMap::new(),
                clusters: HashMap::new(),
                mtime: None,
            }),
            last_check: RwLock::new(Instant::now() - Duration::from_secs(10)),
            frontmatter_cache: Cache::builder().max_capacity(500).time_to_live(Duration::from_secs(600)).build(),
        };
        s.load_force();
        s
    }

    fn load_force(&self) {
        if let Err(e) = self.load(true) {
            tracing::warn!("failed to load index: {}", e);
        }
    }

    fn load(&self, force: bool) -> Result<()> {
        if !force {
            let last = *self.last_check.read().unwrap();
            if last.elapsed() < Duration::from_secs(5) {
                return Ok(());
            }
        }
        *self.last_check.write().unwrap() = Instant::now();

        let mtime = std::fs::metadata(&self.index_path).and_then(|m| m.modified()).ok();
        {
            let inner = self.inner.read().unwrap();
            if !force && inner.mtime == mtime && !inner.skills.is_empty() {
                return Ok(());
            }
        }

        if self.index_path.exists() {
            match std::fs::read_to_string(&self.index_path) {
                Ok(text) => match serde_json::from_str::<IndexFile>(&text) {
                    Ok(file) => {
                        let mut skills = Vec::new();
                        for e in file.skills {
                            let tags = parse_tags_value(&e.tags);
                            if e.name.is_empty() || !is_valid_skill_name(&e.name) {
                                continue;
                            }
                            // cluster defaults to category if not set
                            let cluster = e.cluster.unwrap_or_else(|| e.category.clone());
                            skills.push(SkillRecord {
                                name: e.name,
                                description: e.description.chars().take(300).collect(),
                                category: if e.category.is_empty() { "other".into() } else { e.category },
                                author: e.author,
                                version: e.version,
                                risk: normalize_risk(&e.risk).to_string(),
                                path: e.path,
                                tags,
                                summary: e.summary,
                                hash: e.hash,
                                cluster,
                                intent: e.intent,
                                quality_score: e.quality_score,
                                trunk: e.trunk,
                                subcategory: e.subcategory,
                            });
                        }
                        skills.sort_by(|a, b| a.name.cmp(&b.name));
                        let by_name = skills.iter().map(|s| (s.name.clone(), s.clone())).collect();
                        let (inverted, clusters) = build_inverted_and_clusters(&skills);
                        let mut inner = self.inner.write().unwrap();
                        inner.skills = skills;
                        inner.by_name = by_name;
                        inner.inverted = inverted;
                        inner.clusters = clusters;
                        inner.mtime = mtime;
                        tracing::info!("Loaded {} skills from index {} (inverted {} terms, {} clusters)", inner.skills.len(), self.index_path.display(), inner.inverted.len(), inner.clusters.len());
                        self.frontmatter_cache.invalidate_all();
                        return Ok(());
                    }
                    Err(e) => tracing::warn!("failed to parse index {}: {} — fallback", self.index_path.display(), e),
                },
                Err(e) => tracing::warn!("failed to read index {}: {} — fallback", self.index_path.display(), e),
            }
        }

        // SQLite FTS5 fallback for 10k scale (if JSON missing/corrupt and DB exists)
        if let Ok(sqlite_skills) = self.try_load_sqlite() {
            if !sqlite_skills.is_empty() {
                let (inverted, clusters) = build_inverted_and_clusters(&sqlite_skills);
                let mut inner = self.inner.write().unwrap();
                inner.by_name = sqlite_skills.iter().map(|s| (s.name.clone(), s.clone())).collect();
                inner.skills = sqlite_skills;
                inner.inverted = inverted;
                inner.clusters = clusters;
                inner.mtime = mtime;
                self.frontmatter_cache.invalidate_all();
                tracing::info!("Loaded {} skills from SQLite fallback", inner.skills.len());
                return Ok(());
            }
        }

        let scanned = self.scan_fs()?;
        let (inverted, clusters) = build_inverted_and_clusters(&scanned);
        let mut inner = self.inner.write().unwrap();
        inner.by_name = scanned.iter().map(|s| (s.name.clone(), s.clone())).collect();
        inner.skills = scanned;
        inner.inverted = inverted;
        inner.clusters = clusters;
        inner.mtime = mtime;
        self.frontmatter_cache.invalidate_all();
        Ok(())
    }

    fn scan_fs(&self) -> Result<Vec<SkillRecord>> {
        let mut skills = Vec::new();
        if !self.skills_dir.exists() {
            return Ok(skills);
        }
        for entry in std::fs::read_dir(&self.skills_dir)? {
            let entry = entry?;
            if !entry.file_type()?.is_dir() { continue; }
            let dir = entry.path();
            let md = dir.join("SKILL.md");
            if !md.exists() { continue; }
            let meta = parse_frontmatter_public(&md);
            let name = meta.get("name").cloned().unwrap_or_else(|| dir.file_name().unwrap().to_string_lossy().to_string()).trim().to_string();
            if !is_valid_skill_name(&name) { continue; }
            let desc = meta.get("description").cloned().unwrap_or_default().chars().take(300).collect();
            let cat = meta.get("category").cloned().unwrap_or_else(|| "other".into());
            let cluster = cat.clone();
            skills.push(SkillRecord {
                name: name.clone(),
                description: desc,
                category: cat.clone(),
                author: meta.get("author").cloned().unwrap_or_else(|| "unknown".into()),
                version: meta.get("version").cloned().unwrap_or_else(|| "1.0".into()),
                risk: normalize_risk(&meta.get("risk").cloned().unwrap_or_else(|| "unknown".into())).to_string(),
                path: format!("skills/{}/SKILL.md", dir.file_name().unwrap().to_string_lossy()),
                tags: meta.get("tags").map(|t| split_tags(t)).unwrap_or_default(),
                summary: None,
                hash: None,
                cluster,
                intent: None,
                quality_score: None,
                trunk: meta.get("trunk").cloned(),
                subcategory: meta.get("subcategory").cloned(),
            });
        }
        skills.sort_by(|a,b| a.name.cmp(&b.name));
        tracing::info!("Scanned {} skills from FS", skills.len());
        Ok(skills)
    }
    fn try_load_sqlite(&self) -> anyhow::Result<Vec<SkillRecord>> {
        let db_path = self.index_path.with_file_name("skills-index.db");
        if !db_path.exists() { anyhow::bail!("no db"); }
        let conn = Connection::open(&db_path)?;
        let mut stmt = conn.prepare("SELECT name, description, category, cluster, intent, tags, summary, hash, quality_score, path FROM skills")?;
        let rows = stmt.query_map([], |row| {
            Ok(SkillRecord {
                name: row.get(0)?,
                description: row.get(1)?,
                category: row.get(2)?,
                cluster: row.get::<_, Option<String>>(3)?.unwrap_or_else(|| row.get::<_, String>(2).unwrap_or_else(|_| "other".into())),
                intent: row.get(4)?,
                tags: row.get::<_, String>(5).map(|s| s.split(',').map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect()).unwrap_or_default(),
                summary: row.get(6)?,
                hash: row.get(7)?,
                quality_score: row.get(8)?,
                author: "ncdevshiv".into(),
                version: "1.0".into(),
                risk: "unknown".into(), // SQLite fallback: can't determine risk from DB alone
                path: row.get(9)?,
                trunk: None,
                subcategory: None,
            })
        })?;
        let mut skills = Vec::new();
        for r in rows { skills.push(r?); }
        if skills.is_empty() { anyhow::bail!("empty db"); }
        Ok(skills)
    }


    pub fn ensure_fresh(&self) { let _ = self.load(false); }

    pub fn all_skills(&self) -> Vec<SkillRecord> {
        self.ensure_fresh();
        self.inner.read().unwrap().skills.clone()
    }
    pub fn get(&self, name: &str) -> Option<SkillRecord> {
        self.ensure_fresh();
        self.inner.read().unwrap().by_name.get(name).cloned()
    }
    pub fn categories(&self) -> HashMap<String, Vec<String>> {
        self.ensure_fresh();
        let inner = self.inner.read().unwrap();
        let mut cats: HashMap<String, Vec<String>> = HashMap::new();
        for s in &inner.skills { cats.entry(s.category.clone()).or_default().push(s.name.clone()); }
        cats
    }
    pub fn clusters(&self) -> HashMap<String, Vec<String>> {
        self.ensure_fresh();
        self.inner.read().unwrap().clusters.clone()
    }

    pub fn search(&self, query: &str, limit: usize, category_filter: Option<&str>) -> Vec<serde_json::Value> {
        self.ensure_fresh();
        let q = query.trim();
        if q.is_empty() { return Vec::new(); }
        let terms = tokenize(q);
        let inner = self.inner.read().unwrap();

        // Candidate set via inverted index: union of posting lists
        let mut candidate_ids: HashSet<usize> = HashSet::new();
        let mut term_found = false;
        for term in &terms {
            if let Some(posting) = inner.inverted.get(term) {
                term_found = true;
                for &id in posting { candidate_ids.insert(id); }
            }
        }
        // If no term in inverted (e.g. rare), fallback to full scan for that query
        let candidates: Vec<(usize, SkillRecord)> = if term_found && !candidate_ids.is_empty() {
            candidate_ids.into_iter().filter_map(|id| inner.skills.get(id).map(|s| (id, s.clone()))).collect()
        } else {
            // fallback: need category filter still
            inner.skills.iter().enumerate().map(|(i,s)| (i, s.clone())).collect()
        };

        let mut scored: Vec<(f64, SkillRecord)> = Vec::new();
        for (_, s) in candidates {
            if let Some(cat) = category_filter { if s.category != cat { continue; } }
            let score = score_skill(&s, &terms);
            if score >= 2.0 {
                let mut boosted = score + (2.0 - s.name.len() as f64 * 0.02).max(0.0);
                // quality & staleness boost if present
                if let Some(qs) = s.quality_score { boosted += qs * 2.0; }
                scored.push((boosted, s));
            }
        }
        scored.sort_by(|a,b| b.0.partial_cmp(&a.0).unwrap());
        let lim = limit.clamp(1, MAX_PAGE_SIZE);
        scored.truncate(lim);
        scored.into_iter().map(|(sc,s)| serde_json::json!({
            "name": s.name, "description": s.description, "category": s.category,
            "version": s.version, "author": s.author, "risk": s.risk, "path": s.path,
            "tags": s.tags, "cluster": s.cluster, "intent": s.intent,
            "relevance_score": (sc*100.0).round()/100.0
        })).collect()
    }

    pub fn cached_frontmatter(&self, skill_name: &str) -> Option<HashMap<String, String>> {
        if let Some(v) = self.frontmatter_cache.get(skill_name) { return Some(v); }
        let path = self.skills_dir.join(skill_name).join("SKILL.md");
        if !path.exists() { return None; }
        let meta = parse_frontmatter_public(&path);
        self.frontmatter_cache.insert(skill_name.to_string(), meta.clone());
        Some(meta)
    }
}

fn tokenize(q: &str) -> Vec<String> {
    let re = Regex::new(r"[^a-z0-9]+").unwrap();
    let mut terms: Vec<String> = re.split(&q.to_lowercase()).filter(|t| t.len()>=2).map(|s| s.to_string()).collect();
    if terms.is_empty() { terms.push(q.to_lowercase()); }
    terms
}

fn score_skill(s: &SkillRecord, terms: &[String]) -> f64 {
    let name_l = s.name.to_lowercase();
    let desc_l = s.description.to_lowercase();
    let cat_l = s.category.to_lowercase();
    let tags_l = s.tags.join(" ").to_lowercase();
    let cluster_l = s.cluster.to_lowercase();
    let mut score = 0.0;
    for term in terms {
        if term == &name_l { score += 20.0; }
        else if name_l.contains(term) {
            let pat = format!(r"\b{}\b", regex::escape(term));
            if Regex::new(&pat).map(|re| re.is_match(&name_l)).unwrap_or(false) { score += 10.0; } else { score += 4.0; }
        }
        if desc_l.contains(term) {
            let cnt = desc_l.matches(term).count();
            score += (cnt.min(3) as f64)*2.0;
            let pat = format!(r"\b{}\b", regex::escape(term));
            if Regex::new(&pat).map(|re| re.is_match(&desc_l)).unwrap_or(false) { score += 1.0; }
        }
        if term == &cat_l { score += 5.0; } else if cat_l.contains(term) { score += 2.0; }
        if tags_l.contains(term) { score += 3.0; }
        if cluster_l.contains(term) { score += 2.0; }
        // summary boost if present
        if let Some(summary) = &s.summary { if summary.to_lowercase().contains(term) { score += 1.5; } }
    }
    score
}

fn build_inverted_and_clusters(skills: &[SkillRecord]) -> (HashMap<String, Vec<usize>>, HashMap<String, Vec<String>>) {
    let mut inv: HashMap<String, Vec<usize>> = HashMap::new();
    let mut clusters: HashMap<String, Vec<String>> = HashMap::new();
    for (idx, s) in skills.iter().enumerate() {
        // index name, description, category, tags, cluster
        let text = format!("{} {} {} {} {}", s.name, s.description, s.category, s.tags.join(" "), s.cluster);
        for term in tokenize(&text) {
            inv.entry(term).or_default().push(idx);
        }
        clusters.entry(s.cluster.clone()).or_default().push(s.name.clone());
    }
    // dedup postings
    for v in inv.values_mut() { v.sort_unstable(); v.dedup(); }
    (inv, clusters)
}

fn parse_tags_value(v: &serde_yaml::Value) -> Vec<String> {
    match v {
        serde_yaml::Value::Sequence(seq) => seq.iter().filter_map(|x| x.as_str().map(|s| s.trim().to_string())).filter(|s| !s.is_empty()).collect(),
        serde_yaml::Value::String(s) => split_tags(s),
        _ => Vec::new(),
    }
}
fn split_tags(s: &str) -> Vec<String> { s.split(',').map(|t| t.trim().to_string()).filter(|t| !t.is_empty()).collect() }
