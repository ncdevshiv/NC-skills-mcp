#!/usr/bin/env python3
import json, sqlite3, pathlib
idx = json.load(open("skills-index.json", encoding="utf-8"))
db = pathlib.Path("skills-index.db")
if db.exists(): db.unlink()
con = sqlite3.connect(str(db))
cur = con.cursor()
cur.execute("CREATE TABLE skills (name TEXT PRIMARY KEY, description TEXT, category TEXT, cluster TEXT, intent TEXT, tags TEXT, summary TEXT, hash TEXT, quality_score REAL, path TEXT)")
cur.execute("CREATE VIRTUAL TABLE skills_fts USING fts5(name, description, tags, cluster, content='skills', content_rowid='rowid')")
for s in idx["skills"]:
    cur.execute("INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?,?)", (s["name"], s["description"], s["category"], s.get("cluster",""), s.get("intent",""), ",".join(s.get("tags",[])), s.get("summary","")[:500], s.get("hash",""), s.get("quality_score",0.5), s["path"]))
    cur.execute("INSERT INTO skills_fts(rowid, name, description, tags, cluster) VALUES (last_insert_rowid(),?,?,?,?)", (s["name"], s["description"], ",".join(s.get("tags",[])), s.get("cluster","")))
con.commit()
# verify
cur.execute("SELECT count(*) FROM skills"); print("skills", cur.fetchone()[0])
cur.execute("SELECT count(*) FROM skills_fts"); print("fts", cur.fetchone()[0])
con.close()
print(f"Built {db} with FTS5 fallback ready")
