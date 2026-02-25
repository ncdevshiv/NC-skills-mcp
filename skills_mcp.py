#!/usr/bin/env python3
"""
Skills MCP Server - Exposes skills to autonomous agents
Implements the MCP stdio protocol for communication with the host
"""

import json
import sys
from pathlib import Path
from typing import Any, Optional

SKILLS_DIR = Path(__file__).parent / "skills"


def get_skill_metadata(skill_path: Path) -> Optional[dict]:
    """Extract YAML frontmatter from SKILL.md"""
    skill_file = skill_path / "SKILL.md"
    if not skill_file.exists():
        return None
    
    content = skill_file.read_text(encoding="utf-8")
    
    if not content.startswith("---"):
        return None
    
    try:
        second_dash = content.index("---", 3)
        yaml_content = content[3:second_dash].strip()
        
        metadata = {}
        for line in yaml_content.split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip().strip('"')
        
        return metadata
    except Exception:
        return None


def get_all_skills() -> list[dict]:
    """Get all available skills with metadata"""
    skills = []
    
    if not SKILLS_DIR.exists():
        return skills
    
    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        
        metadata = get_skill_metadata(skill_dir)
        if metadata:
            skills.append({
                "name": skill_dir.name,
                "description": metadata.get("description", ""),
                "version": metadata.get("version", "unknown"),
                "author": metadata.get("author", "unknown"),
                "category": metadata.get("category", "general"),
                "tags": metadata.get("tags", "").split(", ") if metadata.get("tags") else [],
            })
    
    return sorted(skills, key=lambda x: x.get("name", ""))


def get_skill_content(skill_name: str) -> Optional[dict]:
    """Get full skill content"""
    skill_path = SKILLS_DIR / skill_name
    
    if not skill_path.exists():
        return None
    
    skill_file = skill_path / "SKILL.md"
    if not skill_file.exists():
        return None
    
    content = skill_file.read_text(encoding="utf-8")
    
    metadata = get_skill_metadata(skill_path)
    
    readme_path = skill_path / "README.md"
    readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else None
    
    return {
        "name": skill_name,
        "metadata": metadata,
        "content": content,
        "readme": readme,
    }


def find_relevant_skills(query: str) -> list[dict]:
    """Find skills relevant to a query"""
    all_skills = get_all_skills()
    query_lower = query.lower()
    
    relevant = []
    for skill in all_skills:
        score = 0
        name = skill.get("name", "").lower()
        desc = skill.get("description", "").lower()
        category = skill.get("category", "").lower()
        tags = " ".join(skill.get("tags", [])).lower()
        
        if query_lower in name:
            score += 10
        if query_lower in desc:
            score += 5
        if query_lower in category:
            score += 3
        if query_lower in tags:
            score += 3
        
        if score > 0:
            skill["relevance_score"] = score
            relevant.append(skill)
    
    return sorted(relevant, key=lambda x: x.get("relevance_score", 0), reverse=True)


def get_skill_categories() -> dict:
    """Get skills grouped by category"""
    skills = get_all_skills()
    categories = {}
    
    for skill in skills:
        cat = skill.get("category", "general")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(skill["name"])
    
    return categories


class MCPServer:
    """MCP Server implementation"""
    
    def __init__(self):
        self.capabilities = {
            "tools": {},
            "resources": {}
        }
        self.server_info = {
            "name": "skills-mcp-server",
            "version": "1.0.0"
        }
        self._initialized = False
    
    def handle_request(self, method: str, params: dict, request_id: Any = None) -> dict:
        """Handle incoming MCP requests"""
        if method == "initialize":
            return self.initialize(params)
        elif method == "tools/list":
            return self.list_tools()
        elif method == "tools/call":
            return self.call_tool(params.get("name"), params.get("arguments", {}))
        elif method == "resources/list":
            return self.list_resources()
        elif method == "resources/read":
            return self.read_resource(params.get("uri"))
        elif method == "ping":
            return {}
        elif method == "shutdown":
            return {}
        return None
    
    def initialize(self, params: dict) -> dict:
        """Handle initialize request"""
        self._initialized = True
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": self.capabilities,
            "serverInfo": self.server_info
        }
    
    def list_tools(self) -> dict:
        return {
            "tools": [
                {
                    "name": "list_skills",
                    "description": "List all available skills with their descriptions",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "Filter by category"
                            }
                        }
                    }
                },
                {
                    "name": "get_skill",
                    "description": "Get full skill content including metadata and instructions",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Skill name to retrieve"
                            }
                        },
                        "required": ["name"]
                    }
                },
                {
                    "name": "find_skills",
                    "description": "Find relevant skills for a given task or query - use this to discover which skills to use",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Task or topic to find relevant skills for"
                            }
                        },
                        "required": ["query"]
                    }
                },
                {
                    "name": "get_categories",
                    "description": "Get all skill categories and their skills",
                    "inputSchema": {
                        "type": "object",
                        "properties": {}
                    }
                }
            ]
        }
    
    def call_tool(self, name: str, args: dict) -> Any:
        """Call a tool by name"""
        if name == "list_skills":
            skills = get_all_skills()
            if "category" in args and args["category"]:
                skills = [s for s in skills if s.get("category") == args["category"]]
            return {"content": [{"type": "text", "text": json.dumps({"skills": skills, "count": len(skills)})}]}
        elif name == "get_skill":
            skill_name = args.get("name", "")
            skill = get_skill_content(str(skill_name))
            if skill:
                return {"content": [{"type": "text", "text": json.dumps(skill)}]}
            return {"content": [{"type": "text", "text": json.dumps({"error": f"Skill '{skill_name}' not found"})}], "isError": True}
        elif name == "find_skills":
            result = find_relevant_skills(args.get("query", ""))
            return {"content": [{"type": "text", "text": json.dumps({"skills": result})}]}
        elif name == "get_categories":
            return {"content": [{"type": "text", "text": json.dumps({"categories": get_skill_categories()})}]}
        return {"content": [{"type": "text", "text": json.dumps({"error": f"Unknown tool: {name}"})}], "isError": True}
    
    def list_resources(self) -> dict:
        skills = get_all_skills()
        return {
            "resources": [
                {
                    "uri": f"skill://{s['name']}",
                    "name": s["name"],
                    "description": s.get("description", "")[:100],
                    "mimeType": "text/markdown"
                }
                for s in skills
            ]
        }
    
    def read_resource(self, uri: str) -> Any:
        """Read a resource by URI"""
        if uri and uri.startswith("skill://"):
            skill_name = uri[8:]
            skill = get_skill_content(str(skill_name))
            if skill:
                return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": json.dumps(skill)}]}
            return {"contents": [], "error": f"Skill '{skill_name}' not found"}
        return {"contents": [], "error": f"Invalid resource URI: {uri}"}


def send_response(request_id: Any, result: Any):
    """Send a JSON-RPC response to stdout"""
    # Ensure request_id is not null - use a default if needed
    response_id = request_id if request_id is not None else "notification"
    response = {
        "jsonrpc": "2.0",
        "id": response_id,
        "result": result
    }
    print(json.dumps(response), flush=True)


def send_error(request_id: Any, code: int, message: str, data: Any = None):
    """Send a JSON-RPC error response to stdout"""
    # Ensure request_id is not null
    response_id = request_id if request_id is not None else "error"
    error_obj = {
        "code": code,
        "message": message
    }
    if data:
        error_obj["data"] = data
    response = {
        "jsonrpc": "2.0",
        "id": response_id,
        "error": error_obj
    }
    print(json.dumps(response), flush=True)


def parse_jsonrpc_message(line: str) -> Optional[dict]:
    """Parse a JSON-RPC message with better error handling"""
    try:
        msg = json.loads(line.strip())
        # Validate basic structure
        if not isinstance(msg, dict):
            return None
        # Check for required fields
        if "jsonrpc" not in msg:
            return None
        if msg.get("jsonrpc") != "2.0":
            return None
        return msg
    except (json.JSONDecodeError, TypeError):
        return None


def main():
    """Main MCP stdio server loop"""
    server = MCPServer()
    
    # Read JSON-RPC requests from stdin
    for line in sys.stdin:
        # Skip empty lines
        if not line.strip():
            continue
            
        request = parse_jsonrpc_message(line)
        
        if request is None:
            # Send error for malformed JSON
            send_error(None, -32700, "Parse error - invalid JSON")
            continue
        
        # Extract required fields
        method = request.get("method")
        if not method:
            send_error(request.get("id"), -32600, "Invalid Request - method is required")
            continue
        
        params = request.get("params", {})
        request_id = request.get("id")
        
        try:
            # Handle the request
            if method == "initialize":
                result = server.handle_request(method, params, request_id)
                send_response(request_id, result)
            elif method in ["tools/list", "tools/call", "resources/list", "resources/read", "ping", "shutdown"]:
                result = server.handle_request(method, params, request_id)
                if result is not None:
                    send_response(request_id, result)
                else:
                    send_error(request_id, -32601, f"Method not found: {method}")
            else:
                send_error(request_id, -32601, f"Method not found: {method}")
                
        except Exception as e:
            # Handle other errors with proper error code
            send_error(request_id, -32603, f"Internal error: {str(e)}")


if __name__ == "__main__":
    main()
