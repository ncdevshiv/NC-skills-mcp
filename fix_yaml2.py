#!/usr/bin/env python3
import pathlib, re, yaml
ROOT = pathlib.Path("skills")
fixed = 0
for d in sorted(ROOT.iterdir()):
    if not d.is_dir(): continue
    p = d / "SKILL.md"
    if not p.exists(): continue
    t = p.read_text(encoding="utf-8", errors="replace")
    if not t.startswith("---"): continue
    parts = t.split("---", 2)
    if len(parts) < 3: continue
    fm, body = parts[1], parts[2]
    try:
        yaml.safe_load(fm)
        continue
    except:
        pass
    lines = fm.split("\n")
    # find description start
    desc_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("description:"):
            desc_idx = i
            break
    if desc_idx is None:
        continue
    # find next key after description (next line that looks like "key:" and not inside description value)
    # keys we expect: metadata, author, category, risk, etc. — but we can detect by regex for next top-level key
    next_idx = None
    for j in range(desc_idx+1, len(lines)):
        # a top-level key is like "metadata:", "risk:", "source:", "cluster:", etc. — no leading spaces or 0-2 spaces, and contains colon, and not starting with quote
        stripped = lines[j].strip()
        if re.match(r'^[a-z_]+:\s*', stripped) and not stripped.startswith('"') and not stripped.startswith("'"):
            # ensure it's a known key, not a continuation of description that happens to contain colon
            # description continuation lines typically start with '"' or are quoted fragments
            # So if line starts with '"' or is like '"something",', skip
            if stripped.startswith('"') or stripped.startswith("'"):
                continue
            # also check if it's one of expected keys
            if stripped.split(":")[0] in ["metadata","author","category","risk","source","cluster","intent","quality_score","updated","version","access_control","requires_authorization","authorized_only","warning","tags","name"]:
                next_idx = j
                break
            # fallback: any key with colon and not quoted
            if ":" in stripped and not stripped.startswith('"'):
                # check if next line after is also key-like, to avoid false positive
                # for now treat as next key
                next_idx = j
                break
    if next_idx is None:
        # fallback: find metadata:
        for j in range(desc_idx+1, len(lines)):
            if lines[j].strip().startswith("metadata:"):
                next_idx = j
                break
    if next_idx is None:
        print(f"no next key for {d.name}, skipping")
        continue
    # collect raw description block
    raw_block = "\n".join(lines[desc_idx:next_idx])
    # extract value after "description:"
    m = re.match(r'\s*description:\s*(.*)', raw_block, re.S)
    if not m:
        continue
    raw_val = m.group(1)
    # Clean: remove outer quotes, inner escaped quotes, commas, newlines, extra spaces
    # raw_val is like '"This skill should be ....\nmetrics", "SaaS metrics", ...\n"'
    # Replace newlines, remove leading/trailing quotes, split by commas/quotes
    # Simplest: extract all double-quoted fragments and join with space
    # Find all "..." fragments
    fragments = re.findall(r'"([^"]*)"', raw_val)
    if fragments:
        # join fragments with space, clean
        cleaned = " ".join(f.strip() for f in fragments if f.strip())
        # also capture unquoted tail like " or requests guidance..."
        # The last fragment may be followed by unquoted text before final "
        # For now, cleaned is good
        # If cleaned is too short, fallback to stripping quotes and commas
        if len(cleaned) < 20:
            cleaned = re.sub(r'["\n,]+', ' ', raw_val).strip()
            cleaned = re.sub(r'\s+', ' ', cleaned)
    else:
        cleaned = re.sub(r'["\n]+', ' ', raw_val).strip()
        cleaned = re.sub(r'\s+', ' ', cleaned)
    # limit to 300 chars and escape single quotes for YAML single-quoted
    cleaned = cleaned[:500].strip()
    cleaned_esc = cleaned.replace("'", "''")
    new_desc_line = f"description: '{cleaned_esc}'"
    # reconstruct fm
    new_lines = lines[:desc_idx] + [new_desc_line] + lines[next_idx:]
    new_fm = "\n".join(new_lines)
    try:
        yaml.safe_load(new_fm)
    except Exception as e:
        print(f"still fail {d.name}: {e}")
        # debug
        print(new_fm[:500])
        continue
    new_text = f"---{new_fm}---{body}"
    p.write_text(new_text, encoding="utf-8")
    fixed += 1
    if fixed % 20 == 0:
        print(f"... {fixed} fixed")
print(f"Fixed {fixed} more")
# final verify
bad = []
for d in sorted(ROOT.iterdir()):
    if not d.is_dir(): continue
    p = d / "SKILL.md"
    if not p.exists(): continue
    t = p.read_text(encoding="utf-8")
    if not t.startswith("---"): continue
    fm = t.split("---",2)[1]
    try:
        yaml.safe_load(fm)
    except:
        bad.append(d.name)
print(f"Remaining bad: {len(bad)}")
if bad[:10]: print(bad[:10])
