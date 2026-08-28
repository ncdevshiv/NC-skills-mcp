#!/usr/bin/env python3
"""Rebuild skills-index.json with tags, summary, hash and improved taxonomy."""
import hashlib, json, re, pathlib
from pathlib import Path
import yaml

ROOT = Path(__file__).parent
SKILLS_DIR = ROOT / "skills"
INDEX_PATH = ROOT / "skills-index.json"

# Enhanced taxonomy: check name, description, and body keywords
CATEGORY_KEYWORDS = {
    "ai": ["ai-", "ai_", "agent-", "agent_", "llm", "gpt", "claude", "rag-", "crewai", "langchain", "langgraph", "embedding", "vector", "prompt", "hugging"],
    "azure": ["azure-", "azure_"],
    "aws": ["aws-", "aws_"],
    "web": ["web-", "react-", "nextjs", "vue-", "angular-", "frontend-", "html", "css", "javascript", "typescript", "tailwind", "browser", "chrome-extension"],
    "mobile": ["android-", "ios-", "flutter-", "react-native", "mobile-"],
    "backend": ["backend-", "api-", "server-", "node-", "express", "fastapi", "django", "spring", "nestjs", "grpc"],
    "database": ["database-", "sql-", "mysql", "postgresql", "postgres", "mongodb", "redis", "dynamodb", "cosmos", "neon", "prisma"],
    "devops": ["docker-", "kubernetes", "k8s-", "terraform", "ansible", "ci-", "cd-", "jenkins", "github-actions", "gitlab", "helm", "istio"],
    "security": ["security-", "penetration", "attack", "vulnerability", "exploit", "audit", "defense", "hack", "anti-", "pentest", "xss", "sqli", "privilege", "reversing"],
    "testing": ["test-", "testing-", "e2e-", "unit-test", "jest", "pytest", "playwright", "cypress", "bats-"],
    "data": ["data-", "analytics", "etl", "pipeline", "spark", "airflow", "data-"],
    "cloud": ["cloud-", "gcp-", "google-"],
    "os": ["linux-", "windows-", "macos", "bash-", "shell-", "powershell", "posix"],
    "office": ["excel-", "word", "powerpoint", "libreoffice"],
    "architecture": ["architecture-", "architect-", "pattern-", "c4-"],
    "documentation": ["doc-", "documentation-", "readme-", "wiki-"],
    "product": ["product-", "ux-", "ui-", "design-"],
}

def classify(name, description, body):
    hay = f"{name} {description} {body[:2000]}".lower()
    best = ("other", 0)
    for cat, kws in CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in kws if kw.lower() in hay)
        if score > best[1]:
            best = (cat, score)
    # also fallback to original prefix logic but with lower weight
    if best[1] == 0:
        return "other"
    return best[0]

INTENT_KEYWORDS = {
    "build": ["build", "create", "generate", "scaffold", "implement"],
    "audit": ["audit", "review", "scan", "assess", "compliance"],
    "migrate": ["migrate", "migration", "upgrade", "convert"],
    "debug": ["debug", "troubleshoot", "fix", "diagnose"],
    "automate": ["automate", "automation", "workflow", "pipeline", "deploy"],
    "test": ["test", "testing", "e2e", "unit", "playwright"],
    "design": ["design", "architect", "pattern"],
}

def infer_intent(name, description, body):
    hay = f"{name} {description} {body[:2000]}".lower()
    best = ("build", 0)
    for intent, kws in INTENT_KEYWORDS.items():
        score = sum(1 for kw in kws if kw in hay)
        if score > best[1]:
            best = (intent, score)
    return best[0]

def classify_trunk_subcategory(name, description, body):
    """Classify a skill into trunk + subcategory using keyword scoring."""
    hay = f"{name} {description} {body[:2000]}".lower()

    # Trunk classification
    trunk_kw = [
        ("ai-ml", ["agent", "llm", "gpt", "claude", "rag", "prompt", "computer-vision", "voice", "audio", "mlops", "ml-ops", "embedding", "vector", "crewai", "langgraph", "langchain"]),
        ("web", ["web", "frontend", "react", "vue", "angular", "nextjs", "tailwind", "javascript", "typescript", "css", "html", "3d", "web3", "solidity"]),
        ("backend", ["backend", "api", "rest", "graphql", "grpc", "serverless", "fastapi", "django", "nestjs", "express", "python", "node", "ruby"]),
        ("security", ["security", "pentest", "pentesting", "hack", "vulnerability", "attack", "exploit", "auth", "oauth", "jwt", "sqli", "xss", "cwe", "forensic", "reversing"]),
        ("devops", ["devops", "docker", "kubernetes", "k8s", "terraform", "ci-cd", "pipeline", "deploy", "ansible", "helm", "gitops", "jenkins"]),
        ("data", ["data", "analytics", "etl", "pipeline", "spark", "airflow", "dbt", "dashboard", "kafka", "clickhouse", "warehouse", "lake", "analytics"]),
        ("cloud", ["cloud", "aws", "azure", "gcp", "google-cloud", "serverless", "iaas", "paas", "saas", "cloud-run", "s3", "ec2", "lambda"]),
        ("mobile", ["mobile", "android", "ios", "flutter", "react-native", "swift", "kotlin", "compose", "swiftui", "expo", "cross-platform"]),
        ("database", ["database", "sql", "postgres", "mysql", "mongodb", "redis", "prisma", "orm", "nosql", "vector-db", "caching", "db"]),
        ("testing", ["test", "testing", "playwright", "cypress", "e2e", "unit", "tdd", "integration", "pytest", "jest", "load-test", "bench"]),
    ]
    best_trunk = ("other", 0)
    for trunk, kws in trunk_kw:
        score = sum(1 for kw in kws if kw in hay)
        if score > best_trunk[1]:
            best_trunk = (trunk, score)

    # If trunk is 'other', try to match by existing category
    if best_trunk[0] == "other":
        cat_map = {"ai": "ai-ml", "web": "web", "backend": "backend", "security": "security",
                    "devops": "devops", "data": "data", "cloud": "cloud", "mobile": "mobile",
                    "database": "database", "testing": "testing", "azure": "cloud", "aws": "cloud",
                    "google-cloud": "cloud", "os": "devops", "office": "web", "architecture": "ai-ml",
                    "documentation": "web", "product": "web"}
        best_trunk = (cat_map.get(best_trunk[0], "ai-ml"), best_trunk[1])

    # Subcategory classification
    sub_kw = [
        ("agent-development", ["agent", "crewai", "langgraph", "autonomous", "orchestration"]),
        ("llm-application", ["llm", "gpt", "claude", "rag", "langchain", "embedding", "vector"]),
        ("prompt-engineering", ["prompt", "jailbreak", "cot", "few-shot", "template"]),
        ("computer-vision", ["vision", "image", "cv", "detection", "recognition"]),
        ("voice-audio-ai", ["voice", "audio", "speech", "tts", "stt", "podcast", "transcription"]),
        ("ml-ops-engineering", ["mlops", "mlflow", "pipeline", "training", "deployment"]),
        ("frontend-frameworks", ["react", "vue", "angular", "svelte", "tailwind", "css", "html", "typescript", "javascript", "ui", "ux"]),
        ("fullstack-development", ["fullstack", "nextjs", "nuxt", "remix", "full-stack"]),
        ("web-automation", ["browser", "playwright", "puppeteer", "selenium", "scraping"]),
        ("web3-blockchain", ["web3", "blockchain", "solidity", "defi", "smart contract"]),
        ("design-ux", ["design", "figma", "ux", "ui-design", "accessibility", "a11y"]),
        ("api-design", ["api", "rest", "graphql", "grpc", "openapi", "swagger"]),
        ("backend-frameworks", ["backend", "fastapi", "django", "flask", "spring", "nestjs", "express", "ruby", "laravel"]),
        ("serverless-functions", ["serverless", "lambda", "cloud function", "azure function"]),
        ("ci-cd-pipelines", ["ci", "cd", "pipeline", "jenkins", "github-actions", "gitlab-ci", "circleci", "cicd", "deploy"]),
        ("containers-orchestration", ["docker", "kubernetes", "k8s", "container", "helm", "istio", "service-mesh"]),
        ("infrastructure-iaas", ["terraform", "ansible", "cloudformation", "pulumi", "infracost"]),
        ("monitoring-observability", ["monitoring", "prometheus", "grafana", "datadog", "sentry", "observability", "tracing", "slo"]),
        ("data-engineering-pipelines", ["data", "etl", "airflow", "dbt", "spark", "pipeline", "kafka", "clickhouse"]),
        ("analytics-visualization", ["analytics", "dashboard", "amplitude", "mixpanel", "posthog", "grafana", "visualization"]),
        ("ai-ml-engineering", ["ml", "machine-learning", "ai-engineering", "mlops"]),
        ("aws", ["aws", "s3", "ec2", "lambda", "iam"]),
        ("azure", ["azure", "cosmos", "keyvault", "blob", "service-bus"]),
        ("gcp-google-cloud", ["gcp", "google-cloud", "cloud-run", "bigquery", "gcs"]),
        ("android", ["android", "jetpack", "kotlin", "compose", "androidx"]),
        ("ios", ["ios", "swift", "swiftui", "xcode", "apple"]),
        ("cross-platform", ["flutter", "react-native", "mobile", "cross-platform"]),
        ("sql-relational", ["sql", "postgres", "mysql", "sqlite", "mariadb"]),
        ("nosql", ["nosql", "mongodb", "redis", "dynamodb", "neo4j"]),
        ("vector-embedding", ["vector", "embedding", "pinecone", "weaviate", "qdrant"]),
        ("orm-odm", ["prisma", "orm", "sqlalchemy", "typeorm", "mongoose"]),
        ("application-security", ["xss", "sast", "code-review", "vulnerability", "owasp"]),
        ("penetration-testing", ["pentest", "penetration", "attack", "exploit", "metasploit"]),
        ("cloud-security", ["cloud-security", "cloud-hardening", "cloud-audit"]),
        ("auth-authorization", ["auth", "oauth", "jwt", "authorization", "rbac"]),
        ("compliance-forensics", ["compliance", "gdpr", "soc2", "pci", "hipaa", "forensic", "audit"]),
        ("reverse-engineering", ["reverse", "decompil", "binary", "firmware", "malware", "reversing"]),
        ("unit-integration", ["unit", "integration", "jest", "pytest", "tdd"]),
        ("e2e-acceptance", ["e2e", "end-to-end", "cypress", "playwright", "acceptance"]),
        ("performance-load", ["performance", "load-testing", "stress", "benchmark", "k6"]),
        ("security-testing", ["security-testing", "pentest", "pentesting"]),
    ]
    best_sub = ("other", 0)
    for sub, kws in sub_kw:
        score = sum(1 for kw in kws if kw in hay)
        if score > best_sub[1]:
            best_sub = (sub, score)
    return best_trunk[0], best_sub[0]

def quality_score_for(name, description, body, has_tags):
    """0.0-1.0 based on completeness, freshness, length"""
    score = 0.5
    if len(description) > 50: score += 0.15
    if len(body) > 2000: score += 0.15
    if has_tags: score += 0.1
    if "example" in body.lower() or "workflow" in body.lower(): score += 0.1
    return round(min(1.0, score), 2)

def normalize_risk(raw):
    """Normalize non-standard risk values to the known taxonomy."""
    r = raw.strip().lower()
    if r in ("safe", "official"):
        return "safe"
    if r in ("medium", "moderate"):
        return "medium"
    if r in ("high", "critical"):
        return "high"
    return "unknown"

def extract_tags(name, description, category):
    # tokens from name + category
    tokens = re.split(r"[^a-z0-9]+", f"{name} {category}".lower())
    tags = [t for t in tokens if len(t) >= 3][:5]
    # add category as tag if not already
    if category not in tags and category != "other":
        tags.insert(0, category)
    return tags[:5]

for p in [SKILLS_DIR, ROOT]:
    print(p, p.exists())

skills = []
for d in sorted(SKILLS_DIR.iterdir()):
    if not d.is_dir(): continue
    md = d / "SKILL.md"
    if not md.exists(): continue
    text = md.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        continue
    parts = text.split("---", 2)
    if len(parts) < 3:
        continue
    fm_raw, body = parts[1], parts[2]
    try:
        meta = yaml.safe_load(fm_raw) or {}
    except:
        meta = {}
    # flatten metadata.*
    if isinstance(meta.get("metadata"), dict):
        for k,v in meta["metadata"].items():
            if k not in meta:
                meta[k] = v
    name = str(meta.get("name", d.name)).strip()
    desc = str(meta.get("description", "") or "").strip()[:300]
    if not desc:
        # fallback to first heading
        m = re.search(r"^#\s+(.+)", body, re.M)
        desc = m.group(1).strip()[:300] if m else f"{name.replace('-',' ').title()} skill"
    # improved category: use existing if not other and plausible, else reclassify
    existing_cat = str(meta.get("category", "other") or "other").strip().lower()
    if existing_cat == "other":
        category = classify(name, desc, body)
    else:
        category = existing_cat
    author = str(meta.get("author", "ncdevshiv") or "ncdevshiv")
    version = str(meta.get("version", "1.0") or "1.0")
    # risk is normalized later in the append block
    # tags
    raw_tags = meta.get("tags", "")
    if isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    elif isinstance(raw_tags, str) and raw_tags.strip():
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    else:
        tags = extract_tags(name, desc, category)
    # summary
    summary = re.sub(r"\s+", " ", body.strip())[:500]
    # hash on raw bytes (matches what Rust reads via read_to_string)
    raw_bytes = md.read_bytes()
    h = hashlib.sha256(raw_bytes).hexdigest()[:16]
    # intent & cluster & quality
    intent = infer_intent(name, desc, body)
    cluster = category  # v2: 1:1 with category, next: k-means 50
    qscore = quality_score_for(name, desc, body, bool(tags))
    # Tree taxonomy
    trunk, subcategory = classify_trunk_subcategory(name, desc, body)
    risk = normalize_risk(str(meta.get("risk", "safe") or "safe"))
    skills.append({
        "name": name,
        "description": desc,
        "category": category,
        "author": author,
        "version": version,
        "risk": risk,
        "path": f"skills/{d.name}/SKILL.md",
        "tags": tags,
        "summary": summary,
        "hash": h,
        "cluster": cluster,
        "intent": intent,
        "quality_score": qscore,
        "trunk": trunk,
        "subcategory": subcategory,
    })

# sort
skills.sort(key=lambda x: x["name"])
from datetime import datetime, timezone
out = {"version": "1.0", "generated": datetime.now(timezone.utc).isoformat(), "skills": skills}
# atomic write
tmp = INDEX_PATH.with_suffix(".tmp")
tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
tmp.replace(INDEX_PATH)
print(f"Rebuilt {len(skills)} skills -> {INDEX_PATH}")
from collections import Counter
c = Counter(s["category"] for s in skills)
print("categories:", dict(c))
print("with tags:", sum(1 for s in skills if s["tags"]))
print("other %:", round(c["other"]/len(skills)*100,1) if "other" in c else 0)
