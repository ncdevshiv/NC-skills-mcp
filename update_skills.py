import os
import re
import yaml
from pathlib import Path
from datetime import datetime

SKILLS_DIR = Path("F:/dev/skills")
AUTHOR = "ncdevshiv"
DEFAULT_VERSION = "1.0"

CATEGORY_PATTERNS = {
    "ai": ["ai-", "ai_", "agent-", "agent_", "llm", "gpt", "claude", "rag-", "rag_"],
    "azure": ["azure-", "azure_"],
    "aws": ["aws-", "aws_"],
    "google-cloud": ["gcp-", "google-", "gcp_"],
    "web": ["web-", "react-", "nextjs", "vue-", "angular-", "frontend-", "html", "css", "javascript", "typescript"],
    "mobile": ["android-", "ios-", "flutter-", "react-native", "mobile-"],
    "backend": ["backend-", "api-", "server-", "node-", "express", "fastapi", "django", "spring"],
    "database": ["database-", "sql-", "mysql", "postgresql", "postgres", "mongodb", "redis", "dynamodb", "cosmos"],
    "devops": ["docker-", "kubernetes", "k8s-", "terraform", "ansible", "ci-", "cd-", "jenkins", "github-actions"],
    "security": ["security-", "penetration", "attack", "vulnerability", "exploit", "audit", "defense", "hack", "anti-"],
    "testing": ["test-", "testing-", "e2e-", "unit-test", "jest", "pytest", "playwright", "cypress"],
    "data": ["data-", "analytics", "etl", "pipeline", "spark", "airflow"],
    "cloud": ["cloud-"],
    "os": ["linux-", "windows-", "macos-", "bash-", "shell-", "powershell"],
    "office": ["excel-", "word-", "powerpoint", " libreoffice", "google-"],
    "architecture": ["architecture-", "architect-", "pattern-"],
    "documentation": ["doc-", "documentation-", "readme-"],
    "product": ["product-", "ux-", "ui-", "design-"],
}

SECURITY_SKILLS = [
    "active-directory-attacks",
    "api-security-best-practices",
    "api-security-testing",
    "api-fuzzing-bug-bounty",
    "attack-tree-construction",
    "aws-penetration-testing",
    "backend-security-coder",
    "cc-skill-security-review",
    "cloud-penetration-testing",
    "codebase-cleanup-deps-audit",
    "dependency-management-deps-audit",
    "ethical-hacking-methodology",
    "frontend-security-coder",
    "frontend-mobile-security-xss-scan",
    "k8s-security-policies",
    "laravel-security-audit",
    "mobile-security-coder",
    "production-code-audit",
    "security-scanning-security-dependencies",
    "security-requirement-extraction",
    "security-compliance-compliance-check",
    "security-bluebook-builder",
    "security-auditor",
    "security-audit",
    "security-scanning-security-sast",
    "security-scanning-security-hardening",
    "ssh-penetration-testing",
    "smtp-penetration-testing",
    "web-security-testing",
    "vulnerability-scanner",
    "wordpress-penetration-testing",
    "solidity-security",
    "anti-reversing-techniques",
    "accessibility-compliance-accessibility-audit",
    "seo-audit",
]

def get_category(skill_name):
    skill_lower = skill_name.lower()
    for category, patterns in CATEGORY_PATTERNS.items():
        for pattern in patterns:
            if pattern in skill_lower:
                return category
    return "other"

def is_security_skill(skill_name):
    return skill_name.lower() in [s.lower() for s in SECURITY_SKILLS]

def process_skill_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Parse YAML frontmatter
    if not content.startswith('---'):
        return False
    
    parts = content.split('---', 2)
    if len(parts) < 3:
        return False
    
    frontmatter = parts[1]
    body = parts[2]

    # Parse existing YAML
    try:
        meta = yaml.safe_load(frontmatter)
        if meta is None:
            meta = {}
    except:
        return False

    # Get skill name
    skill_name = meta.get('name', file_path.parent.name)

    # Determine category
    category = get_category(skill_name)

    # Check if it's a security skill
    is_security = is_security_skill(skill_name)

    # Build updated frontmatter
    new_frontmatter = []
    new_frontmatter.append(f"name: {skill_name}")
    
    # Handle description - keep existing or add based on body
    description = meta.get('description', '')
    if not description or len(description) < 10:
        # Try to extract from body
        first_line = body.strip().split('\n')[0] if body.strip() else ''
        if first_line.startswith('# '):
            description = first_line[2:].strip()
        if not description:
            description = f"{skill_name.replace('-', ' ').replace('_', ' ').title()} skill"
    
    new_frontmatter.append(f"description: \"{description}\"")

    # Build metadata section
    new_frontmatter.append("metadata:")
    new_frontmatter.append(f"  author: {AUTHOR}")
    
    # Keep existing version or use default
    existing_version = None
    if 'metadata' in meta and isinstance(meta['metadata'], dict):
        existing_version = meta['metadata'].get('version')
    elif 'version' in meta:
        existing_version = meta.get('version')
    
    version = existing_version if existing_version else DEFAULT_VERSION
    new_frontmatter.append(f"  version: \"{version}\"")
    new_frontmatter.append(f"  category: {category}")
    new_frontmatter.append(f"  updated: {datetime.now().strftime('%Y-%m-%d')}")

    # Keep risk if exists
    risk = meta.get('risk', 'safe' if not is_security else 'unknown')
    new_frontmatter.append(f"risk: {risk}")

    # Keep source if exists
    source = meta.get('source', 'community')
    new_frontmatter.append(f"source: {source}")

    # Add access control warning for security skills
    if is_security:
        new_frontmatter.append("access_control:")
        new_frontmatter.append("  requires_authorization: true")
        new_frontmatter.append("  authorized_only: true")
        new_frontmatter.append("  warning: \"This skill involves security testing. Ensure you have explicit written authorization before proceeding.\"")

    # Reconstruct file
    new_content = '---\n' + '\n'.join(new_frontmatter) + '\n---\n' + body

    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    return True

def main():
    count = 0
    security_count = 0
    
    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            skill_name = skill_dir.name
            is_sec = is_security_skill(skill_name)
            if process_skill_file(skill_file):
                count += 1
                if is_sec:
                    security_count += 1
                print(f"Processed: {skill_name} (security: {is_sec})")
    
    print(f"\nTotal processed: {count}")
    print(f"Security skills: {security_count}")

if __name__ == "__main__":
    main()
