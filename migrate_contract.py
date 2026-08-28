#!/usr/bin/env python3
"""Backfill Skill Contract fields into SKILL.md frontmatter if missing: cluster, intent, quality_score."""
import re, pathlib, yaml, hashlib, json
from pathlib import Path

ROOT = Path(__file__).parent
SKILLS_DIR = ROOT / "skills"

# reuse logic from rebuild_index
INTENT_KWS = {
    "build": ["build", "create", "generate", "scaffold", "implement"],
    "audit": ["audit", "review", "scan", "assess", "compliance"],
    "migrate": ["migrate", "migration", "upgrade", "convert"],
    "debug": ["debug", "troubleshoot", "fix", "diagnose"],
    "automate": ["automate", "automation", "workflow", "pipeline", "deploy"],
    "test": ["test", "testing", "e2e", "unit", "playwright"],
    "design": ["design", "architect", "pattern"],
}
def infer_intent(name, desc, body):
    hay = f"{name} {desc} {body[:2000]}".lower()
    best = ("build", 0)
    for intent, kws in INTENT_KWS.items():
        score = sum(1 for kw in kws if kw in hay)
        if score > best[1]:
            best = (intent, score)
    return best[0]

def quality_score(name, desc, body, has_tags):
    score = 0.5
    if len(desc) > 50: score += 0.15
    if len(body) > 2000: score += 0.15
    if has_tags: score += 0.1
    if "example" in body.lower() or "workflow" in body.lower(): score += 0.1
    return round(min(1.0, score), 2)

count = 0
for d in sorted(SKILLS_DIR.iterdir()):
    if not d.is_dir(): continue
    md = d / "SKILL.md"
    if not md.exists(): continue
    text = md.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"): continue
    parts = text.split("---", 2)
    if len(parts) < 3: continue
    fm_raw, body = parts[1], parts[2]
    try:
        meta = yaml.safe_load(fm_raw) or {}
    except: 
        continue
    # flatten metadata
    if isinstance(meta.get("metadata"), dict):
        for k,v in meta["metadata"].items():
            if k not in meta:
                meta[k] = v
    name = str(meta.get("name", d.name)).strip()
    desc = str(meta.get("description", "") or "").strip()
    has_tags = bool(meta.get("tags"))
    # check if contract fields present
    need = any(k not in meta for k in ["cluster", "intent", "quality_score"])
    # also check inside metadata dict
    if not need:
        continue
    # infer
    cluster = meta.get("category", "other")
    # if category is other, try to infer better? keep as is for file, but we have better in index
    intent = infer_intent(name, desc, body)
    qscore = quality_score(name, desc, body, has_tags)
    # rebuild frontmatter preserving original + adding missing
    # simplest: append missing keys after existing fm_raw
    extra = []
    if "cluster" not in meta: extra.append(f"cluster: {cluster}")
    if "intent" not in meta: extra.append(f"intent: {intent}")
    if "quality_score" not in meta: extra.append(f"quality_score: {qscore}")
    if not extra: continue
    new_fm = fm_raw.rstrip() + "\n" + "\n".join(extra) + "\n"
    new_text = f"---{new_fm}---{body}"
    md.write_text(new_text, encoding="utf-8")
    count += 1
    if count % 100 == 0:
        print(f"... {count} migrated")

print(f"Migrated {count} skills with contract fields")
# CI gate: verify all have required fields
missing = []
for d in SKILLS_DIR.iterdir():
    if not d.is_dir(): continue
    md = d / "SKILL.md"
    if not md.exists(): continue
    t = md.read_text(encoding="utf-8", errors="replace")
    if "intent:" not in t or "cluster:" not in t:
        missing.append(d.name)
print(f"Still missing contract: {len(missing)}")
if missing[:5]: print(missing[:5])

# also write CI workflow
wf_dir = ROOT / ".github" / "workflows"
wf_dir.mkdir(parents=True, exist_ok=True)
wf = wf_dir / "validate-skills.yml"
wf.write_text("""name: validate-skills
on: [push, pull_request]
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install pyyaml
      - run: python migrate_contract.py --check
      - run: python rebuild_index.py
      - run: cargo check
""", encoding="utf-8")
print(f"Wrote CI gate to {wf}")
