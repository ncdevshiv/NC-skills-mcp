#!/usr/bin/env python3
"""Edge-case harness — validates Rust (& Python fallback) MCP server against 2025-11-25 spec."""
import json, subprocess, sys, time, os, pathlib

BIN_RUST = pathlib.Path("target/debug/skills-mcp-server.exe")
BIN_PY = [sys.executable, "skills_mcp.py"]
BIN = [str(BIN_RUST)] if BIN_RUST.exists() else BIN_PY

def spawn(bin_cmd):
    return subprocess.Popen(bin_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, encoding="utf-8", errors="replace")

def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n"); proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line)

def assert_eq(a,b,msg):
    if a!=b:
        print(f"FAIL {msg}: {a!r} != {b!r}"); return False
    print(f"PASS {msg}"); return True

def run():
    ok = 0; fail = 0
    def check(cond, msg):
        nonlocal ok, fail
        if cond: ok+=1; print(f"PASS {msg}")
        else: fail+=1; print(f"FAIL {msg}")

    proc = spawn(BIN)
    time.sleep(0.4)
    # initialize
    r = send(proc, {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"harness","version":"1"}}})
    check(r and r.get("result",{}).get("protocolVersion")=="2025-11-25", "initialize negotiates 2025-11-25")
    proc.stdin.write(json.dumps({"jsonrpc":"2.0","method":"notifications/initialized"})+"\n"); proc.stdin.flush(); time.sleep(0.1)

    # tools/list should include 5 tools including get_skill_summary
    r = send(proc, {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
    tools = [t["name"] for t in r["result"]["tools"]] if r and "result" in r else []
    check("find_skills" in tools, "tools/list has find_skills")
    check("get_skill_summary" in tools, "tools/list has get_skill_summary (progressive disclosure)")
    check("get_skill" in tools, "tools/list has get_skill")

    # find_skills empty -> 0
    r = send(proc, {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"find_skills","arguments":{"query":"","limit":5}}})
    d = json.loads(r["result"]["content"][0]["text"]) if r else {}
    check(d.get("count")==0, "find_skills empty -> 0")

    # find_skills normal
    r = send(proc, {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"find_skills","arguments":{"query":"FastAPI","limit":3}}})
    d = json.loads(r["result"]["content"][0]["text"]) if r else {}
    check(d.get("count",0)>0, "find_skills FastAPI returns >0")

    # get_skill safe
    r = send(proc, {"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"get_skill","arguments":{"name":"api-documentation"}}})
    d = json.loads(r["result"]["content"][0]["text"]) if r and "result" in r else {}
    check(d.get("name")=="api-documentation", "get_skill safe")

    # security without flag blocked
    r = send(proc, {"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"get_skill","arguments":{"name":"active-directory-attacks"}}})
    check(r["result"].get("isError")==True, "security without flag blocked")

    # string "false" must NOT bypass (old bool("false")==True bug)
    r = send(proc, {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"get_skill","arguments":{"name":"active-directory-attacks","allow_security":"false"}}})
    check(r["result"].get("isError")==True, "string false does not bypass gate")

    # string "true" allows
    r = send(proc, {"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"get_skill","arguments":{"name":"active-directory-attacks","allow_security":"true"}}})
    check("active-directory" in json.loads(r["result"]["content"][0]["text"]).get("name","") if r and "result" in r else False, "string true allows")

    # traversal blocked
    r = send(proc, {"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"get_skill","arguments":{"name":"../../mcp_config"}}})
    check(r["result"].get("isError")==True, "traversal blocked")

    # pagination
    r = send(proc, {"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"list_skills","arguments":{"limit":2}}})
    d = json.loads(r["result"]["content"][0]["text"]) if r else {}
    check(d.get("count")==2 and "nextCursor" in d, "list_skills pagination nextCursor")

    # get_skill_summary progressive disclosure
    r = send(proc, {"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"get_skill_summary","arguments":{"name":"api-documentation"}}})
    d = json.loads(r["result"]["content"][0]["text"]) if r else {}
    check("summary" in d and "sections" in d, "get_skill_summary has summary+sections")

    # section extraction
    r = send(proc, {"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"get_skill","arguments":{"name":"api-documentation","section":"Workflow"}}})
    d = json.loads(r["result"]["content"][0]["text"]) if r and "result" in r else {}
    check(len(d.get("content",""))>0 and len(d.get("content","")) < 8000, "get_skill section extraction")

    # ping
    r = send(proc, {"jsonrpc":"2.0","id":13,"method":"ping","params":{}})
    check(r.get("result")=={}, "ping")

    # invalid cursor
    r = send(proc, {"jsonrpc":"2.0","id":14,"method":"tools/call","params":{"name":"list_skills","arguments":{"cursor":"!!!","limit":2}}})
    check(r.get("error") or r["result"].get("isError"), "invalid cursor returns error")

    # resources/list
    r = send(proc, {"jsonrpc":"2.0","id":15,"method":"resources/list","params":{"limit":2}})
    check(len(r["result"]["resources"])==2, "resources/list paginated")

    # prompts/list
    r = send(proc, {"jsonrpc":"2.0","id":16,"method":"prompts/list","params":{"limit":2}})
    check(len(r["result"]["prompts"])==2, "prompts/list paginated")

    # completion
    r = send(proc, {"jsonrpc":"2.0","id":17,"method":"completion/complete","params":{"ref":{"type":"ref/prompt","name":"get_skill"},"argument":{"name":"name","value":"api-"}}})
    check(len(r["result"]["completion"]["values"])>0, "completion/complete api-")

    # id:null should be -32600
    proc.stdin.write(json.dumps({"jsonrpc":"2.0","id":None,"method":"ping"})+"\n"); proc.stdin.flush()
    r_null = json.loads(proc.stdout.readline())
    check(r_null.get("error",{}).get("code")==-32600, "id:null -> -32600")
    # next ping still works
    r = send(proc, {"jsonrpc":"2.0","id":18,"method":"ping","params":{}})
    check(r.get("result")=={}, "ping after id:null still works")

    proc.terminate()
    try: proc.wait(timeout=2)
    except: proc.kill()
    print(f"\n=== HARNESS DONE: {ok} pass, {fail} fail ===")
    return 0 if fail==0 else 1

if __name__ == "__main__":
    sys.exit(run())
