import pathlib
p = pathlib.Path("src/index.rs")
t = p.read_text(encoding="utf-8")
old = "        // Fallback: scan FS\n        let scanned = self.scan_fs()?;"
new = """        // SQLite FTS5 fallback for 10k scale (if JSON missing/corrupt and DB exists)
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

        // Fallback: scan FS
        let scanned = self.scan_fs()?;"""
if old in t:
    t = t.replace(old, new)
    p.write_text(t, encoding="utf-8")
    print("patched fallback")
else:
    print("not found")
    print(repr(t[2500:3500]))

# Now add try_load_sqlite method after scan_fs
# Find scan_fs end and insert before "    pub fn ensure_fresh"
scan_end = "        tracing::info!(\"Scanned {} skills from FS\", skills.len());\n        Ok(skills)\n    }"
if scan_end in t:
    # reload t
    t = p.read_text(encoding="utf-8")
    addition = """
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
                risk: "safe".into(),
                path: row.get(9)?,
            })
        })?;
        let mut skills = Vec::new();
        for r in rows { skills.push(r?); }
        if skills.is_empty() { anyhow::bail!("empty db"); }
        Ok(skills)
    }
"""
    t = t.replace(scan_end, scan_end + addition)
    p.write_text(t, encoding="utf-8")
    print("added try_load_sqlite")
else:
    print("scan_end not found")
