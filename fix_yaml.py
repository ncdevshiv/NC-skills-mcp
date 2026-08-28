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
        continue  # ok
    except:
        pass
    # fix description line with unescaped quotes
    # find line starting with description:
    lines = fm.split("\n")
    new_lines = []
    for line in lines:
        if line.strip().startswith("description:"):
            # extract after colon
            m = re.match(r'(\s*description:\s*)(.*)', line)
            if m:
                prefix, val = m.groups()
                val = val.strip()
                # val is quoted with " but contains inner "attack...": need to re-quote with single outer
                # strip outer quotes if present
                if val.startswith('"') and val.endswith('"') and len(val) >=2:
                    inner = val[1:-1]
                    # escape single quotes for YAML single-quoted string: '' 
                    inner_esc = inner.replace("'", "''")
                    val = f"'{inner_esc}'"
                    line = prefix + val
                elif val.startswith('"'):
                    # malformed, try to single-quote whole
                    inner = val.strip('"')
                    inner_esc = inner.replace("'", "''")
                    val = f"'{inner_esc}'"
                    line = prefix + val
        new_lines.append(line)
    new_fm = "\n".join(new_lines)
    # verify
    try:
        yaml.safe_load(new_fm)
    except Exception as e:
        print(f"still fail {d.name}: {e}")
        continue
    new_text = f"---{new_fm}---{body}"
    p.write_text(new_text, encoding="utf-8")
    fixed += 1
    if fixed % 20 == 0:
        print(f"... {fixed} fixed")
print(f"Fixed {fixed} YAML files")
# verify all
bad = []
for d in sorted(ROOT.iterdir()):
    if not d.is_dir(): continue
    p = d / "SKILL.md"
    if not p.exists(): continue
    t = p.read_text(encoding="utf-8")
    if not t.startswith("---"): continue
    fm = t.split("---", 2)[1]
    try:
        yaml.safe_load(fm)
    except:
        bad.append(d.name)
print(f"Remaining bad: {len(bad)}")
if bad[:5]: print(bad[:5])
