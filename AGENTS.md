# Autonomous Skill System

## Overview
This agent has access to a skill library with 500+ skills covering development, security, cloud, and more.

## How It Works

### 1. Skill Discovery (Automatic)
Before starting ANY task, the agent MUST:
1. Call `find_skills` with a query describing the task
2. Review top matching skills with their relevance scores
3. Select the most relevant skill(s) for the task

### 2. Skill Loading
Once relevant skills are identified:
1. Call `get_skill` to retrieve the full skill content
2. Follow the skill's instructions exactly
3. Apply the skill's patterns and best practices

### 3. Autonomous Behavior
- **DO NOT** ask the user which skill to use
- **DO NOT** wait for user confirmation to use skills
- **DO** automatically discover and apply relevant skills
- **DO** chain multiple skills if needed for complex tasks

## Example Flow

User: "Build a FastAPI REST API"

Agent thought: The user wants to build a FastAPI API. Let me find relevant skills.

1. Call `find_skills` with query: "FastAPI REST API development"
2. Results show: `python-fastapi-development`, `api-design-principles`, `python-patterns`
3. Call `get_skill` for `python-fastapi-development`
4. Follow the skill's guidance to build the API

## Tools Available

| Tool | Purpose |
|------|---------|
| `find_skills(query)` | Discover relevant skills for a task |
| `get_skill(name)` | Get full skill content and instructions |
| `list_skills()` | List all skills (optional category filter) |
| `get_categories()` | Get skills grouped by category |

## Important
- Always use `find_skills` first when starting a new task
- Skills are your knowledge base - use them proactively
- Never say "I need to use a skill" - just use it
