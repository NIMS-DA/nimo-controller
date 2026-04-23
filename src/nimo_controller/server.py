# server.py — NIMO Controller backend
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fastmcp import Client
from agents import (
    Agent, Runner, function_tool,
    OpenAIChatCompletionsModel, AsyncOpenAI, set_tracing_disabled,
)
from agents.items import (
    ItemHelpers, MessageOutputItem, ToolCallItem, ToolCallOutputItem,
)
from agents.mcp import MCPServerManager, MCPServerStdio, MCPServerStreamableHttp

from .paths import (
    CANDIDATES_FILE,
    NIMO_SERVER_SCRIPT,
    RESULTS_DIR,
    STATIC_DIR,
    TEMPLATES_DIR,
    ensure_user_dirs,
    resolve_config_path,
)

# ---------------------------------------------------------------------------
# Config — loaded from config.yaml (falls back to bundled default)
# ---------------------------------------------------------------------------
ensure_user_dirs()
_CONFIG_PATH: Path = resolve_config_path()


def _load_config(path: Path = _CONFIG_PATH) -> dict:
    """Read the YAML config file. Returns an empty dict on failure."""
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


_cfg = _load_config()

# LLM model (shared across all agents)
LLM_MODEL: str = _cfg.get("model", "gpt-4o-mini")

# Optional OpenAI-compatible endpoint (e.g. Ollama: http://host:11434/v1).
# When set, all agents and the inner generate_blocks call route through it.
OPENAI_BASE_URL: Optional[str] = _cfg.get("openai_base_url") or None
OPENAI_API_KEY: Optional[str] = _cfg.get("openai_api_key") or None

if OPENAI_API_KEY and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY

def _build_openai_client() -> Optional[AsyncOpenAI]:
    """Build a custom AsyncOpenAI client if a base_url is configured."""
    if not OPENAI_BASE_URL:
        return None
    return AsyncOpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY or "ollama",
    )


_OPENAI_CLIENT: Optional[AsyncOpenAI] = _build_openai_client()

if _OPENAI_CLIENT is not None:
    # Tracing posts to api.openai.com which fails for local endpoints.
    set_tracing_disabled(True)


def _agent_model():
    """Return the model object (or model name) to pass to Agent(...)."""
    if _OPENAI_CLIENT is not None:
        return OpenAIChatCompletionsModel(
            model=LLM_MODEL,
            openai_client=_OPENAI_CLIENT,
        )
    return LLM_MODEL

# NIMO MCP
NIMO_MCP_NAME: str = "nimo"
NIMO_SCRIPT: str = str(NIMO_SERVER_SCRIPT)
NIMO_CLIENT_ONLY: bool = False

# Additional MCP servers
_mcp_raw = _cfg.get("mcp_servers") or {}
MCP_SERVERS: Dict[str, str] = {str(k): str(v) for k, v in _mcp_raw.items()} if isinstance(_mcp_raw, dict) else {}

# Server port
SERVER_PORT: int = int(_cfg.get("port", 8888))

# Internal constants (not user-configurable)
NIMO_VAR_KEY = "__nimo_var__"
LAST_FLOAT_KEY = "__last_float__"
LOOP_COUNTER_KEY = "__loop_counter__"

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class JobEvent:
    """Single event emitted during workflow execution."""
    id: int
    event: str
    data: Any
    ts: float = field(default_factory=time.time)


@dataclass
class Job:
    """In-memory state for a running Blockly workflow."""
    id: str
    status: str = "queued"
    events: List[JobEvent] = field(default_factory=list)
    next_event_id: int = 1
    task: Optional[asyncio.Task] = None
    error: Optional[str] = None
    workspace_xml: Optional[str] = None
    workflow_ast: Optional[dict] = None
    history: List[dict] = field(default_factory=list)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def push(self, event: str, data: Any) -> None:
        """Append an event to the job's event log."""
        self.events.append(JobEvent(self.next_event_id, event, data))
        self.next_event_id += 1


@dataclass
class ChatSession:
    """Tracks an agent-mode chat turn with pending tool-approval interruptions.

    Used only in agent mode where MCP tools require user approval.
    """
    id: str
    agent: Agent
    state: Any
    interruptions: List[Any]
    created_at: float = field(default_factory=time.time)
    seen_call_ids: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def now_s() -> float:
    """Return the current UNIX timestamp in seconds."""
    return time.time()


def sse(ev_id: int, event: str, data: Any) -> str:
    """Format a single Server-Sent Events frame."""
    return f"id: {ev_id}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _cleanup_sessions(app: FastAPI, max_age: float = 3600.0) -> None:
    """Remove agent chat sessions older than *max_age* seconds."""
    cutoff = now_s() - max_age
    sessions: Dict[str, ChatSession] = app.state.agent_sessions
    for sid in [s for s, v in sessions.items() if v.created_at < cutoff]:
        sessions.pop(sid, None)


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    """Read *key* from *obj* whether it is a dict or an object with attributes."""
    if obj is None:
        return default
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def normalize_args(args: Any) -> Any:
    """Ensure tool-call arguments are returned as a dict."""
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return {}
        try:
            return json.loads(s)
        except Exception:
            return {"_raw": s}
    return args if args is not None else {}


def normalize_tool_output(output: Any) -> Any:
    """Normalise a tool output value into a JSON-friendly Python object."""
    if isinstance(output, str):
        s = output.strip()
        if s.startswith(("{", "[")):
            try:
                return json.loads(s)
            except Exception:
                pass
        return output
    if isinstance(output, dict):
        if output.get("type") == "text" and isinstance(output.get("text"), str):
            s = output["text"].strip()
            if s.startswith(("{", "[")):
                try:
                    return json.loads(s)
                except Exception:
                    pass
            return output
    if isinstance(output, list):
        texts = [_attr(item, "text", "") for item in output if _attr(item, "type") == "text"]
        if texts:
            combined = "\n".join(texts).strip()
            if combined.startswith(("{", "[")):
                try:
                    return json.loads(combined)
                except Exception:
                    pass
            return combined
    return output


def extract_images(source: Any) -> list[dict]:
    """Extract base64 image items from an MCP result or Agents SDK raw_item."""
    images: list[dict] = []
    content = None
    for attr in ("content", "output"):
        content = _attr(source, attr)
        if isinstance(content, (list, tuple)):
            break
        content = None
    if content is None:
        return images
    for item in content:
        if _attr(item, "type") == "image":
            data = _attr(item, "data", "")
            if data:
                images.append({"data": data, "mimeType": _attr(item, "mimeType", "image/png")})
    return images


# ---------------------------------------------------------------------------
# generate_blocks — two-stage LLM tool (Blockly mode only)
# ---------------------------------------------------------------------------

_INNER_SYSTEM_PROMPT = """\
You are a Blockly workflow AST generator.
Given a user request and a tool catalog, output ONLY a valid JSON object
with a "body" array. No explanation, no markdown fences.

AST schema:
{
  "body": [
    {
      "kind": "tool",
      "server_id": "<server>",
      "tool": "<tool_name>",
      "args": { "<key>": <value> }
    },
    {
      "kind": "repeat",
      "times": 5,
      "counter_var": "i",
      "body": [ ... ]
    },
    {
      "kind": "if",
      "counter_var": "i",
      "op": "==",
      "value": 1,
      "then": [ ... ],
      "else": [ ... ]
    }
  ]
}

NIMO shortcuts:
- selection: {"kind":"tool","server_id":"nimo","tool":"selection","args":{"method":"PHYSBO"}}
- update:    {"kind":"tool","server_id":"nimo","tool":"update","args":{"objs":{"__last_float__":true}}}

Rules:
- Use ONLY tools from the catalog below.
- Match server_id, tool name, and arg schema exactly.
- Output raw JSON only.
"""

_BLOCKLY_AGENT_INSTRUCTIONS = """\
You are a helpful lab assistant.

You can also build Blockly workflows.
When the user asks you to build, create, or generate a workflow (blocks),
call the `generate_blocks` tool with a short natural-language description
of what the user wants. Example:
  generate_blocks(description="repeat 5 times: selection with PHYSBO, run measure tool on lab server, then update")

Do NOT put JSON in the description — just summarise the user's intent in plain text.
If the user is NOT asking to build a workflow, answer normally without calling generate_blocks.
"""

_AGENT_MODE_INSTRUCTIONS = """\
You are a helpful lab assistant with access to MCP tools.
Use the available tools to help the user with their tasks.
"""

_app_ref: Optional[FastAPI] = None


async def _get_tool_catalog(app_state: Any) -> str:
    """Build a human-readable catalog of every MCP tool currently registered."""
    lines: list[str] = []
    for sid, client in [("nimo", app_state.client_nimo), *app_state.mcp_clients.items()]:
        try:
            for t in await client.list_tools():
                schema = json.dumps(t.inputSchema or {}, ensure_ascii=False)
                lines.append(f'- server_id: "{sid}", tool: "{t.name}", '
                             f'description: "{t.description or ""}", args: {schema}')
        except Exception:
            pass
    return "\n".join(lines) or "(no tools available)"


@function_tool
async def generate_blocks(description: str) -> str:
    """Generate a Blockly workflow AST from a natural-language description.

    Called by the Blockly-mode chat agent when the user asks to create
    a workflow. Internally calls a cheaper LLM with the full tool catalog.

    Args:
        description: Plain-text summary of the desired workflow.

    Returns:
        JSON string ``{"ok": true, "ast": {...}}`` on success.
    """
    import openai

    if _app_ref is None:
        return json.dumps({"ok": False, "error": "Server not initialised"})

    try:
        catalog = await _get_tool_catalog(_app_ref.state)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"Failed to build tool catalog: {e}"})

    system = _INNER_SYSTEM_PROMPT + f"\n\nAvailable tools:\n{catalog}"
    raw = ""

    try:
        client = _OPENAI_CLIENT if _OPENAI_CLIENT is not None else openai.AsyncOpenAI()
        resp = await client.chat.completions.create(
            model=LLM_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": description},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

        ast_obj = json.loads(raw)
        if not isinstance(ast_obj, dict) or "body" not in ast_obj:
            return json.dumps({"ok": False, "error": "AST must have a 'body' key", "raw": raw})
        return json.dumps({"ok": True, "ast": ast_obj})
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "error": "Model returned invalid JSON", "raw": raw})
    except Exception as e:
        return json.dumps({"ok": False, "error": f"Inner LLM call failed: {e}"})


# ---------------------------------------------------------------------------
# Agent lifecycle — dual mode
# ---------------------------------------------------------------------------

def _build_mcp_servers(
    *, nimo_script: str, other_servers: dict[str, str], include_nimo: bool,
) -> list:
    """Construct MCP server instances for the Agents SDK."""
    out: list = []
    if include_nimo:
        # Launch as a module so intra-package relative imports resolve.
        out.append(MCPServerStdio(
            name=NIMO_MCP_NAME,
            params={
                "command": sys.executable,
                "args": ["-m", "nimo_controller.nimo_mcp.nimo_server"],
            },
            cache_tools_list=True, require_approval=True,
        ))
    for sid, url in other_servers.items():
        out.append(MCPServerStreamableHttp(
            name=sid, params={"url": url},
            cache_tools_list=True, require_approval=True,
            client_session_timeout_seconds=600,
        ))
    return out


async def _rebuild_agents(app: FastAPI) -> None:
    """Recreate both the Blockly-mode and agent-mode chat agents.

    The Blockly agent has only the generate_blocks tool (no MCP).
    The agent-mode agent connects to all MCP servers with approval flow.
    """
    global _app_ref
    _app_ref = app

    # Blockly mode agent (no MCP)
    app.state.blockly_agent = Agent(
        name="Assistant",
        instructions=_BLOCKLY_AGENT_INSTRUCTIONS,
        model=_agent_model(),
        tools=[generate_blocks],
    )

    # Agent mode agent (with MCP)
    old_mgr = getattr(app.state, "agent_mcp_manager", None)
    if old_mgr:
        try:
            await old_mgr.__aexit__(None, None, None)
        except Exception:
            pass

    servers = _build_mcp_servers(
        nimo_script=NIMO_SCRIPT,
        other_servers=app.state.dynamic_servers,
        include_nimo=(not NIMO_CLIENT_ONLY),
    )
    manager = MCPServerManager(servers)
    await manager.__aenter__()
    app.state.agent_mcp_manager = manager

    app.state.agent_agent = Agent(
        name="Assistant",
        instructions=_AGENT_MODE_INSTRUCTIONS,
        model=_agent_model(),
        mcp_servers=manager.active_servers,
    )
    app.state.agent_sessions = {}


# ---------------------------------------------------------------------------
# Workflow execution
# ---------------------------------------------------------------------------

def _get_client(app: FastAPI, server_id: str) -> Client:
    """Return the FastMCP Client for *server_id*."""
    if server_id == "nimo":
        return app.state.client_nimo
    clients: Dict[str, Client] = app.state.mcp_clients
    if server_id in clients:
        return clients[server_id]
    raise KeyError(f"Unknown server_id: {server_id}")


async def _resolve_nimo_vars(app: FastAPI, obj: Any) -> Any:
    """Recursively replace ``{"__nimo_var__": "<n>"}`` placeholders."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {NIMO_VAR_KEY} and isinstance(obj[NIMO_VAR_KEY], str):
            name = obj[NIMO_VAR_KEY]
            r = await app.state.client_nimo.call_tool("get_proposal", {})
            proposal = r.data
            if not isinstance(proposal, dict):
                raise RuntimeError(f"get_proposal returned non-dict: {type(proposal)}")
            if name not in proposal:
                raise KeyError(f"NIMO proposal has no key: {name}")
            return proposal[name]
        return {k: await _resolve_nimo_vars(app, v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [await _resolve_nimo_vars(app, v) for v in obj]
    return obj


def _replace_last_float(obj: Any, last_float: Optional[float]) -> Any:
    """Recursively replace ``{"__last_float__": true}`` with *last_float*."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {LAST_FLOAT_KEY} and obj.get(LAST_FLOAT_KEY) is True:
            if last_float is None:
                raise RuntimeError("This step needs last float, but none exists yet.")
            return last_float
        return {k: _replace_last_float(v, last_float) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_last_float(v, last_float) for v in obj]
    return obj


def _replace_loop_counters(obj: Any, counters: dict[str, int]) -> Any:
    """Recursively replace ``{"__loop_counter__": "<var>"}`` with current value."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {LOOP_COUNTER_KEY} and isinstance(obj.get(LOOP_COUNTER_KEY), str):
            name = obj[LOOP_COUNTER_KEY]
            if name not in counters:
                raise KeyError(f"Loop counter variable not found: {name}")
            return counters[name]
        return {k: _replace_loop_counters(v, counters) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_loop_counters(v, counters) for v in obj]
    return obj


def _ast_to_pseudocode(ast: Optional[dict], indent: int = 0) -> str:
    """Convert a workflow AST into a human-readable pseudocode string."""
    if not ast:
        return "(empty workflow)"
    body = ast.get("body", [])
    if not isinstance(body, list):
        return "(invalid workflow)"
    return "\n".join(_node_lines(body, indent))


def _node_lines(nodes: list, indent: int) -> list[str]:
    """Recursively convert AST nodes to indented pseudocode lines."""
    pad = "    " * indent
    lines: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = node.get("kind")

        if kind == "tool":
            sid = node.get("server_id", "")
            tool = node.get("tool", "")
            args = node.get("args", {})
            args_str = ", ".join(
                f"{k}={_arg_display(v)}" for k, v in args.items()
            ) if isinstance(args, dict) else ""
            lines.append(f"{pad}[{sid}] {tool}({args_str})")

        elif kind == "repeat":
            times = node.get("times", 1)
            counter = node.get("counter_var")
            label = f"repeat {times} times ({counter}):" if counter else f"repeat {times} times:"
            lines.append(f"{pad}{label}")
            lines.extend(_node_lines(node.get("body", []), indent + 1))

        elif kind == "if":
            var = node.get("counter_var", "?")
            op = node.get("op", "==")
            val = node.get("value", 0)
            lines.append(f"{pad}if {var} {op} {val}:")
            lines.extend(_node_lines(node.get("then", []), indent + 1))
            if node.get("else"):
                lines.append(f"{pad}else:")
                lines.extend(_node_lines(node.get("else", []), indent + 1))
    return lines


def _arg_display(v: Any) -> str:
    """Format a single argument value for pseudocode display."""
    if isinstance(v, dict):
        if NIMO_VAR_KEY in v:
            return f"proposal.{v[NIMO_VAR_KEY]}"
        if v.get(LAST_FLOAT_KEY) is True:
            return "last_result"
        if LOOP_COUNTER_KEY in v:
            return str(v[LOOP_COUNTER_KEY])
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


async def _exec_node(app: FastAPI, job: Job, node: dict, ctx: dict) -> None:
    """Execute a single workflow AST node (tool call, repeat, or if block)."""
    if not isinstance(node, dict):
        raise RuntimeError(f"node must be dict, got {type(node)}")

    kind = node.get("kind")

    if kind == "repeat":
        times = int(node.get("times", 1))
        if times < 0:
            raise RuntimeError("repeat.times must be >= 0")
        counter_var = node.get("counter_var")
        bid = node.get("block_id")
        if isinstance(bid, str) and bid:
            job.push("active", {"block_id": bid})
        job.push("log", {"kind": "text", "text": f"▶ Repeat x{times}"})
        body = node.get("body", [])
        if not isinstance(body, list):
            raise RuntimeError("repeat.body must be a list")
        counters = ctx.setdefault("loop_counters", {})
        for r in range(times):
            if isinstance(counter_var, str) and counter_var:
                counters[counter_var] = r
            job.push("log", {"kind": "text", "text": f"-- Round {r+1}/{times} --"})
            for child in body:
                await _exec_node(app, job, child, ctx)
        if isinstance(counter_var, str) and counter_var:
            counters.pop(counter_var, None)
        return

    if kind == "if":
        counter_var = node.get("counter_var")
        op = node.get("op", "==")
        value = node.get("value", 0)
        bid = node.get("block_id")
        if not isinstance(counter_var, str) or not counter_var:
            raise RuntimeError("if node requires counter_var")
        if op not in ("==", "<", ">", "<=", ">="):
            raise RuntimeError(f"if node: unsupported operator '{op}'")
        counters = ctx.get("loop_counters", {})
        if counter_var not in counters:
            raise RuntimeError(f"if node: counter '{counter_var}' not in scope")
        actual = counters[counter_var]
        target = int(value)
        cond = (
            (op == "==" and actual == target) or
            (op == "<"  and actual <  target) or
            (op == ">"  and actual >  target) or
            (op == "<=" and actual <= target) or
            (op == ">=" and actual >= target)
        )
        if isinstance(bid, str) and bid:
            job.push("active", {"block_id": bid})
        job.push("log", {"kind": "text",
                          "text": f"▶ If {counter_var} {op} {target} → {cond}",
                          "counter_var": counter_var, "counter_value": actual,
                          "op": op, "target": target, "result": cond})
        branch = node.get("then", []) if cond else node.get("else", [])
        if not isinstance(branch, list):
            raise RuntimeError("if then/else must be a list")
        for child in branch:
            await _exec_node(app, job, child, ctx)
        return

    if kind != "tool":
        raise RuntimeError(f"Unknown node kind: {kind}")

    server_id, tool = node.get("server_id"), node.get("tool")
    args, block_id = node.get("args", {}), node.get("block_id")
    if not server_id or not tool:
        raise RuntimeError("tool node requires server_id and tool")
    if not isinstance(args, dict):
        raise RuntimeError("tool.args must be dict")
    if isinstance(block_id, str) and block_id:
        job.push("active", {"block_id": block_id})

    resolved = await _resolve_nimo_vars(app, args)
    resolved = _replace_last_float(resolved, ctx.get("last_float"))
    resolved = _replace_loop_counters(resolved, ctx.get("loop_counters", {}))

    t_start = time.time()
    job.push("log", {"kind": "tool_call", "server_id": server_id, "tool": tool,
                      "args": resolved, "text": f"▶ Run: [{server_id}] {tool}"})

    client = _get_client(app, server_id)
    use_task = _supports_tasks(client) and await _tool_supports_task(client, tool)

    if use_task:
        r = await _call_tool_as_task(client, tool, resolved)
    else:
        r = await client.call_tool(tool, resolved)

    t_end = time.time()
    result = r.data
    images = extract_images(r)

    # If the tool returned only images (no text data), record image metadata
    if result is None and images:
        result = [{"type": "image", "mimeType": img.get("mimeType", "image/png"),
                   "size": len(img.get("data", ""))} for img in images]

    output: dict[str, Any] = {"data": result}
    if images:
        output["images"] = images
    job.push("log", {"kind": "tool_output", "server_id": server_id,
                      "tool": tool, "output": output, "text": "Result"})
    job.history.append({
        "kind": "tool", "server_id": server_id, "tool": tool,
        "args": resolved, "result": result,
        "started_at": t_start, "finished_at": t_end,
        "duration_s": round(t_end - t_start, 3),
    })
    if isinstance(result, (int, float)):
        ctx["last_float"] = float(result)


def _supports_tasks(client: Client) -> bool:
    """Check if a connected MCP server supports task-augmented tool calls."""
    init = client.initialize_result
    if init is None:
        return False
    caps = getattr(init, "capabilities", None)
    if caps is None:
        return False
    tasks = getattr(caps, "tasks", None) or _attr(caps, "tasks")
    if not tasks:
        return False
    requests = _attr(tasks, "requests")
    if not requests:
        return False
    tools = _attr(requests, "tools")
    if not tools:
        return False
    return bool(_attr(tools, "call"))


def _supports_task_cancel(client: Client) -> bool:
    """Check if the server supports tasks/cancel."""
    init = client.initialize_result
    if init is None:
        return False
    caps = getattr(init, "capabilities", None)
    if caps is None:
        return False
    tasks = getattr(caps, "tasks", None) or _attr(caps, "tasks")
    if not tasks:
        return False
    return bool(_attr(tasks, "cancel"))


async def _tool_supports_task(client: Client, tool_name: str) -> bool:
    """Check if a specific tool declares taskSupport as 'optional' or 'required'."""
    try:
        tools = await client.list_tools()
    except Exception:
        return False
    for t in tools:
        if t.name == tool_name:
            execution = _attr(t, "execution")
            if not execution:
                return False
            support = _attr(execution, "taskSupport", "forbidden")
            return support in ("optional", "required")
    return False


async def _call_tool_as_task(client: Client, tool: str, args: dict) -> Any:
    """Call a tool as a background task with cancellation support."""
    task = await client.call_tool(tool, args, task=True)

    if not hasattr(task, "wait"):
        return task

    try:
        return await task.result()
    except asyncio.CancelledError:
        if _supports_task_cancel(client):
            try:
                task.cancel()
            except Exception:
                pass
        raise


# ---------------------------------------------------------------------------
# Chat / agent log extraction
# ---------------------------------------------------------------------------

def _extract_run_logs(
    result: Any,
    seen_call_ids: Optional[set[str]] = None,
    hide_call_ids: Optional[set[str]] = None,
) -> list[dict]:
    """Convert Agents SDK run-result items into a flat list of UI log dicts."""
    logs: list[dict] = []
    for item in getattr(result, "new_items", None) or []:
        agent_name = getattr(getattr(item, "agent", None), "name", "Assistant")
        raw = getattr(item, "raw_item", None)

        if isinstance(item, ToolCallItem):
            call_id = _attr(raw, "call_id")
            if hide_call_ids and call_id and call_id in hide_call_ids:
                continue
            if call_id and seen_call_ids is not None:
                cid = str(call_id)
                if cid in seen_call_ids:
                    continue
                seen_call_ids.add(cid)
            logs.append({
                "type": "tool_call", "agent": agent_name,
                "tool": _attr(raw, "name", "") or "",
                "call_id": str(call_id) if call_id is not None else None,
                "arguments": normalize_args(_attr(raw, "arguments", {})),
            })
        elif isinstance(item, ToolCallOutputItem):
            call_id = _attr(raw, "call_id")
            entry: dict[str, Any] = {
                "type": "tool_output", "agent": agent_name,
                "call_id": str(call_id) if call_id is not None else None,
                "output": normalize_tool_output(getattr(item, "output", None)),
            }
            imgs = extract_images(raw)
            if imgs:
                entry["images"] = imgs
            logs.append(entry)
        elif isinstance(item, MessageOutputItem):
            text = ItemHelpers.text_message_output(item)
            if text:
                logs.append({"type": "message", "agent": agent_name, "text": text})
    return logs


def _interruption_to_ui(it: Any) -> dict:
    """Convert an Agents SDK interruption into a JSON-serialisable dict."""
    args = getattr(it, "arguments", None)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            pass
    return {
        "agent": getattr(getattr(it, "agent", None), "name", "Assistant"),
        "tool": getattr(it, "name", ""),
        "arguments": args,
        "call_id": str(getattr(it, "call_id", None) or ""),
    }


# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _app_ref
    _app_ref = app

    app.state.current_workflow = None
    app.state.workflow_lock = asyncio.Lock()
    app.state.server_lock = asyncio.Lock()

    import logging
    log = logging.getLogger("nimo")

    # NIMO client (in-process)
    from .nimo_mcp.nimo_server import mcp as nimo_mcp_server
    nimo = Client(nimo_mcp_server)
    await nimo.__aenter__()
    app.state.client_nimo = nimo

    # Additional MCP clients (fail-safe)
    clients: Dict[str, Client] = {}
    connected_servers: Dict[str, str] = {}
    for sid, url in MCP_SERVERS.items():
        c = Client(url)
        try:
            await c.__aenter__()
            clients[sid] = c
            connected_servers[sid] = url
            log.info("Connected to MCP server '%s': %s", sid, url)
        except Exception as e:
            log.error("Failed to connect to MCP server '%s' (%s): %s", sid, url, e)
            try:
                await c.__aexit__(None, None, None)
            except Exception:
                pass
    app.state.mcp_clients = clients
    app.state.dynamic_servers: Dict[str, str] = dict(connected_servers)

    await _rebuild_agents(app)

    try:
        yield
    finally:
        job: Optional[Job] = app.state.current_workflow
        if job and job.task and not job.task.done():
            job.task.cancel()
        mgr = getattr(app.state, "agent_mcp_manager", None)
        if mgr:
            try:
                await mgr.__aexit__(None, None, None)
            except Exception:
                pass
        for c in clients.values():
            try:
                await c.__aexit__(None, None, None)
            except Exception:
                pass
        await nimo.__aexit__(None, None, None)


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main SPA page."""
    return (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/config")
async def get_config():
    """Return the current runtime configuration (read-only)."""
    return {
        "ok": True,
        "model": LLM_MODEL,
        "port": SERVER_PORT,
        "config_path": str(_CONFIG_PATH),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@app.get("/tools")
async def list_tools():
    out = []
    for sid, client in [("nimo", app.state.client_nimo), *app.state.mcp_clients.items()]:
        try:
            for t in await client.list_tools():
                out.append({"server_id": sid, "name": t.name,
                             "description": t.description, "input_schema": t.inputSchema})
        except Exception:
            pass
    return out


@app.get("/nimo/status")
async def nimo_status():
    """Check if NIMO is ready (candidates file loaded)."""
    try:
        r = await app.state.client_nimo.call_tool("get_parameter_names", {})
        params = r.data
        ready = isinstance(params, list) and len(params) > 0
        return {"ok": True, "ready": ready}
    except Exception as e:
        return {"ok": False, "ready": False, "error": str(e)}


@app.get("/nimo/parameters")
async def nimo_parameters():
    r = await app.state.client_nimo.call_tool("get_parameter_names", {})
    params = r.data
    if not isinstance(params, list) or not all(isinstance(x, str) for x in params):
        return {"ok": False, "error": f"Unexpected return: {type(params)}"}
    return {"ok": True, "parameters": params}


@app.post("/nimo/candidates")
async def upload_candidates(file: UploadFile):
    """Upload a new candidates.csv and reinitialize NIMO."""
    if not file.filename or not file.filename.endswith(".csv"):
        return {"ok": False, "error": "Only .csv files are accepted"}

    content = await file.read()

    # Validate CSV has at least 2 columns (parameters + objective)
    import csv as csv_mod
    try:
        reader = csv_mod.DictReader(content.decode("utf-8").splitlines())
        cols = reader.fieldnames or []
        if len(cols) < 2:
            return {"ok": False, "error": "CSV must have at least 2 columns (parameters + objective)"}
    except Exception as e:
        return {"ok": False, "error": f"Invalid CSV: {e}"}

    # Write to <user-data-dir>/candidates.csv
    candidates_path = str(CANDIDATES_FILE)
    os.makedirs(os.path.dirname(candidates_path), exist_ok=True)
    with open(candidates_path, "wb") as f:
        f.write(content)

    # Reinitialize NIMO wrapper via MCP tool
    try:
        r = await app.state.client_nimo.call_tool("reinitialize", {})
        msg = r.data or "Reinitialized"
    except Exception as e:
        return {"ok": False, "error": f"File saved but reinitialize failed: {e}"}

    return {"ok": True, "message": msg}


# ---------------------------------------------------------------------------
# Settings (dynamic MCP server management)
# ---------------------------------------------------------------------------

class AddServerRequest(BaseModel):
    name: str
    url: str


@app.get("/settings/servers")
async def list_servers():
    out = [{"name": NIMO_MCP_NAME, "builtin": True}]
    for name, url in app.state.dynamic_servers.items():
        out.append({"name": name, "url": url, "builtin": False})
    return {"ok": True, "servers": out}


@app.post("/settings/servers")
async def add_server(req: AddServerRequest):
    name, url = req.name.strip(), req.url.strip()
    if not name or not url:
        return {"ok": False, "error": "name and url are required"}
    if name == NIMO_MCP_NAME:
        return {"ok": False, "error": f"'{NIMO_MCP_NAME}' is reserved"}

    async with app.state.server_lock:
        clients: Dict[str, Client] = app.state.mcp_clients
        if name in clients:
            try:
                await clients[name].__aexit__(None, None, None)
            except Exception:
                pass
            del clients[name]

        c = Client(url)
        try:
            await c.__aenter__()
            await c.list_tools()
        except Exception as e:
            try:
                await c.__aexit__(None, None, None)
            except Exception:
                pass
            return {"ok": False, "error": f"Failed to connect: {e}"}

        clients[name] = c
        app.state.dynamic_servers[name] = url
        try:
            await _rebuild_agents(app)
        except Exception as e:
            return {"ok": False, "error": f"Agent rebuild failed: {e}"}
    return {"ok": True, "name": name, "url": url}


@app.delete("/settings/servers/{name}")
async def remove_server(name: str):
    if name == NIMO_MCP_NAME:
        return {"ok": False, "error": f"Cannot remove built-in '{NIMO_MCP_NAME}'"}

    async with app.state.server_lock:
        if name not in app.state.dynamic_servers:
            return {"ok": False, "error": f"Server '{name}' not found"}
        clients: Dict[str, Client] = app.state.mcp_clients
        if name in clients:
            try:
                await clients[name].__aexit__(None, None, None)
            except Exception:
                pass
            del clients[name]
        del app.state.dynamic_servers[name]
        try:
            await _rebuild_agents(app)
        except Exception as e:
            return {"ok": False, "error": f"Agent rebuild failed: {e}"}
    return {"ok": True, "name": name}


# ---------------------------------------------------------------------------
# Workflow API (Blockly mode)
# ---------------------------------------------------------------------------

class WorkflowStartRequest(BaseModel):
    workflow: dict
    workspace_xml: str


@app.post("/workflow/start")
async def workflow_start(req: WorkflowStartRequest):
    async with app.state.workflow_lock:
        cur: Optional[Job] = app.state.current_workflow
        if cur and cur.status in ("queued", "running"):
            return {"ok": False, "error": "A workflow is already running", "workflow_id": cur.id}
        job = Job(id=uuid.uuid4().hex, workspace_xml=req.workspace_xml,
                  workflow_ast=req.workflow)
        app.state.current_workflow = job
        job.push("status", {"status": "queued"})

    async def runner():
        job.status = "running"
        job.started_at = time.time()
        job.push("status", {"status": "running"})
        try:
            # Initialize a new NIMO run directory for this workflow
            await app.state.client_nimo.call_tool("reinitialize", {})

            ctx: dict[str, Any] = {"last_float": None}
            body = (req.workflow or {}).get("body", [])
            if not isinstance(body, list):
                raise RuntimeError("workflow.body must be a list")
            for node in body:
                await _exec_node(app, job, node, ctx)
            job.status = "done"
            job.finished_at = time.time()
            job.push("status", {"status": "done"})
        except asyncio.CancelledError:
            job.status = "canceled"
            job.finished_at = time.time()
            job.push("status", {"status": "canceled"})
            raise
        except Exception as e:
            job.error = str(e)
            job.status = "error"
            job.finished_at = time.time()
            job.push("error", {"error": job.error})
            job.push("status", {"status": "error"})

    job.task = asyncio.create_task(runner())
    return {"ok": True, "workflow_id": job.id}


@app.get("/workflow/current")
async def workflow_current():
    job: Optional[Job] = app.state.current_workflow
    if not job:
        return {"ok": True, "exists": False}
    return {"ok": True, "exists": True, "workflow_id": job.id,
            "status": job.status, "workspace_xml": job.workspace_xml}


@app.get("/workflow/{workflow_id}/events")
async def workflow_events(workflow_id: str, request: Request):
    job: Optional[Job] = app.state.current_workflow
    if not job or job.id != workflow_id:
        async def once():
            yield sse(1, "error", {"error": "workflow not found"})
        return StreamingResponse(once(), media_type="text/event-stream")
    try:
        last_id = int(request.headers.get("last-event-id") or 0)
    except Exception:
        last_id = 0

    async def gen():
        sent = last_id
        ping_at = now_s()
        while True:
            if await request.is_disconnected():
                break
            for e in job.events:
                if e.id > sent:
                    yield sse(e.id, e.event, e.data)
                    sent = e.id
            t = now_s()
            if t - ping_at >= 15:
                yield ": ping\n\n"
                ping_at = t
            if job.status in ("done", "error", "canceled") and sent >= job.next_event_id - 1:
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/workflow/{workflow_id}/cancel")
async def workflow_cancel(workflow_id: str):
    async with app.state.workflow_lock:
        job: Optional[Job] = app.state.current_workflow
        if not job or job.id != workflow_id:
            return {"ok": False, "error": "workflow not found"}
        if job.task and not job.task.done():
            job.task.cancel()
            return {"ok": True, "status": "cancel_requested"}
        return {"ok": True, "status": job.status}


@app.get("/workflow/{workflow_id}/log")
async def workflow_log(workflow_id: str):
    """Return the execution log of a workflow as JSON."""
    job: Optional[Job] = app.state.current_workflow
    if not job or job.id != workflow_id:
        return {"ok": False, "error": "workflow not found"}
    if job.status in ("queued", "running"):
        return {"ok": False, "error": "workflow is still running"}
    return {
        "ok": True,
        "workflow_id": job.id,
        "status": job.status,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "duration_s": round(job.finished_at - job.started_at, 3) if job.started_at and job.finished_at else None,
        "steps": job.history,
    }


@app.get("/workflow/{workflow_id}/log.md")
async def workflow_log_md(workflow_id: str):
    """Download the execution log as a Markdown file."""
    job: Optional[Job] = app.state.current_workflow
    if not job or job.id != workflow_id:
        return {"ok": False, "error": "workflow not found"}
    if job.status in ("queued", "running"):
        return {"ok": False, "error": "workflow is still running"}

    from datetime import datetime, timezone

    _local_tz = datetime.now(timezone.utc).astimezone().tzinfo
    _tz_name = datetime.now(_local_tz).strftime("UTC%z")  # e.g. "UTC+0900"

    def _fmt_ts(ts: Optional[float]) -> str:
        if ts is None:
            return ""
        return datetime.fromtimestamp(ts, tz=_local_tz).strftime("%Y-%m-%d %H:%M:%S.") \
            + f"{int(ts * 1000) % 1000:03d}"

    lines: list[str] = ["# Workflow Log\n"]
    lines.append("## Workflow\n")
    lines.append("```")
    lines.append(_ast_to_pseudocode(job.workflow_ast))
    lines.append("```\n")
    lines.append(f"- Status: {job.status}")
    lines.append(f"- Timezone: {_tz_name}")
    lines.append(f"- Started: {_fmt_ts(job.started_at)}")
    lines.append(f"- Finished: {_fmt_ts(job.finished_at)}")
    if job.started_at and job.finished_at:
        lines.append(f"- Duration: {round(job.finished_at - job.started_at, 3)}s")
    if job.error:
        lines.append(f"- Error: {job.error}")
    lines.append("")

    for i, h in enumerate(job.history, 1):
        tool_label = f"[{h.get('server_id', '')}] {h.get('tool', '')}"
        lines.append(f"## Step {i}: {tool_label}\n")
        lines.append(f"- Started: {_fmt_ts(h.get('started_at'))}")
        lines.append(f"- Finished: {_fmt_ts(h.get('finished_at'))}")
        lines.append(f"- Duration: {h.get('duration_s', '')}s")
        lines.append("")
        lines.append("### Input\n")
        lines.append("```json")
        lines.append(json.dumps(h.get("args", {}), indent=2, ensure_ascii=False))
        lines.append("```\n")
        lines.append("### Output\n")
        lines.append("```json")
        lines.append(json.dumps(h.get("result"), indent=2, ensure_ascii=False))
        lines.append("```\n")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter(["\n".join(lines)]),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="workflow_log_{ts}.md"'},
    )


# ---------------------------------------------------------------------------
# Chat API — Blockly mode (block generation only)
# ---------------------------------------------------------------------------

class ChatRunRequest(BaseModel):
    message: str


@app.post("/chat/run")
async def chat_run(req: ChatRunRequest):
    """Blockly-mode chat: generate_blocks tool only, no MCP."""
    text = (req.message or "").strip()
    if not text:
        return {"ok": True, "reply": "", "logs": []}

    result = await Runner.run(app.state.blockly_agent, text)
    logs = _extract_run_logs(result)

    has_msg = any(x.get("type") == "message" and x.get("text") for x in logs)
    reply = "" if has_msg else str(result.final_output)
    return {"ok": True, "reply": reply, "logs": logs}


# ---------------------------------------------------------------------------
# Agent API — Agent mode (MCP tools with approval)
# ---------------------------------------------------------------------------

class AgentDecisionRequest(BaseModel):
    session_id: str
    decision: str  # approve | reject
    interruption_index: int


@app.post("/agent/run")
async def agent_run(req: ChatRunRequest):
    """Agent-mode chat: full MCP tool access with approval flow."""
    _cleanup_sessions(app)
    text = (req.message or "").strip()
    if not text:
        return {"ok": True, "mode": "final", "reply": "", "logs": []}

    agent: Agent = app.state.agent_agent
    result = await Runner.run(agent, text)
    pending = {str(getattr(i, "call_id", ""))
               for i in (getattr(result, "interruptions", None) or [])}
    logs = _extract_run_logs(result, None, hide_call_ids=pending)

    if getattr(result, "interruptions", None):
        sid = uuid.uuid4().hex
        s = ChatSession(id=sid, agent=agent,
                        state=result.to_state(),
                        interruptions=list(result.interruptions))
        app.state.agent_sessions[sid] = s
        return {
            "ok": True, "mode": "approval",
            "session_id": sid, "interruption_index": 0,
            "pending_count": len(s.interruptions),
            "call": _interruption_to_ui(s.interruptions[0]),
            "logs": logs,
        }

    has_msg = any(x.get("type") == "message" and x.get("text") for x in logs)
    reply = "" if has_msg else str(result.final_output)
    return {"ok": True, "mode": "final", "reply": reply, "logs": logs}


@app.post("/agent/decision")
async def agent_decision(req: AgentDecisionRequest):
    """Approve or reject a pending MCP tool call in agent mode."""
    _cleanup_sessions(app)
    sessions: Dict[str, ChatSession] = app.state.agent_sessions
    s = sessions.get(req.session_id)
    if not s:
        return {"ok": False, "error": "Unknown or expired session_id"}
    if not (0 <= req.interruption_index < len(s.interruptions)):
        return {"ok": False, "error": "Invalid interruption_index"}
    decision = (req.decision or "").lower().strip()
    if decision not in ("approve", "reject"):
        return {"ok": False, "error": "decision must be 'approve' or 'reject'"}

    it = s.interruptions.pop(req.interruption_index)
    (s.state.approve if decision == "approve" else s.state.reject)(it)

    result = await Runner.run(s.agent, s.state)
    pending = {str(getattr(i, "call_id", ""))
               for i in (getattr(result, "interruptions", None) or [])}
    logs = _extract_run_logs(result, s.seen_call_ids, hide_call_ids=pending)

    if getattr(result, "interruptions", None):
        s.state = result.to_state()
        s.interruptions = list(result.interruptions)
        return {
            "ok": True, "mode": "approval", "session_id": s.id,
            "interruption_index": 0,
            "pending_count": len(s.interruptions),
            "call": _interruption_to_ui(s.interruptions[0]),
            "logs": logs,
        }
    sessions.pop(s.id, None)
    has_msg = any(x.get("type") == "message" and x.get("text") for x in logs)
    reply = "" if has_msg else str(result.final_output)
    return {"ok": True, "mode": "final", "reply": reply, "logs": logs}


# Use the ``nimo-controller`` console script (see nimo_controller.cli) to run.