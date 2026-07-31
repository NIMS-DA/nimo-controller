# server.py — NIMO Controller backend
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import yaml

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fastmcp import Client

from html import escape as html_escape

from . import report
from .paths import (
    AGENT_STATE_FILE,
    CANDIDATES_FILE,
    RESULTS_DIR,
    STATIC_DIR,
    TEMPLATES_DIR,
    USER_DATA_DIR,
    ensure_user_dirs,
    resolve_config_path,
)

# ---------------------------------------------------------------------------
# Config — loaded from config.yaml (falls back to bundled default)
# ---------------------------------------------------------------------------
ensure_user_dirs()
_CONFIG_PATH: Path = resolve_config_path()

# Module level: the report builder and the lifespan both log, and a name bound
# only inside lifespan() would be a NameError everywhere else.
log = logging.getLogger("nimo")


def _load_config(path: Path = _CONFIG_PATH) -> dict:
    """Read the YAML config file. Returns an empty dict on failure."""
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


_cfg = _load_config()

# NIMO runs in-process via direct function calls (see nimo_tools).
NIMO_MCP_NAME: str = "nimo"

# Additional MCP servers
_mcp_raw = _cfg.get("mcp_servers") or {}
MCP_SERVERS: Dict[str, str] = {str(k): str(v) for k, v in _mcp_raw.items()} if isinstance(_mcp_raw, dict) else {}

# Server port
SERVER_PORT: int = int(_cfg.get("port", 8888))

# Agent mode (experimental) — shows a prompt box under the execution log
ENABLE_AGENT: bool = bool(_cfg.get("enable_agent", False))

# Agent settings. Kept as a raw dict so this module never has to import
# .agent (and therefore pydantic_ai, which is slow) when the agent is off.
_agent_raw = _cfg.get("agent") or {}
AGENT_CONFIG: Dict[str, Any] = _agent_raw if isinstance(_agent_raw, dict) else {}

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
    run_dir: Optional[str] = None
    log_path: Optional[str] = None
    # candidates.csv as it stood when this run began, for the log and XML.
    candidates_snapshot: Optional[str] = None
    # Rendered at start, so it reflects the inputs rather than the end state.
    workflow_xml: Optional[str] = None
    # In-flight MCP task handle + its client, so /cancel can notify the server.
    current_task: Any = None
    current_client: Any = None
    # Set once the run has reached a terminal status *and* its session entry has
    # been written. An Event rather than something waiters could poll for: status
    # flips several statements before the entry lands, so a waiter woken by the
    # status would read the previous run's record. It sits next to finished_at
    # but answers a different question: that one is a timestamp, this is the
    # signal that the record is safe to read.
    completed: asyncio.Event = field(default_factory=asyncio.Event)

    def push(self, event: str, data: Any) -> None:
        """Append an event to the job's event log."""
        self.events.append(JobEvent(self.next_event_id, event, data))
        self.next_event_id += 1


@dataclass
class Session:
    """One optimization campaign: a results folder plus everything done in it.

    Workflow runs and agent tool calls all land in ``entries`` in the order they
    happened, so the folder holds a single readable record instead of one log
    per run.
    """
    started_at: float
    run_dir: str
    entries: List[dict] = field(default_factory=list)
    # What the session began from — the working candidates.csv drifts from this.
    candidates_snapshot: Optional[str] = None
    candidates_sha256: Optional[str] = None
    # The language the user writes in, kept here rather than read back from the
    # conversation: an interrupted turn throws that away, and the report should
    # not change language because somebody pressed Stop or reloaded the page.
    language: Optional[str] = None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def now_s() -> float:
    """Return the current UNIX timestamp in seconds."""
    return time.time()


def sse(ev_id: int, event: str, data: Any) -> str:
    """Format a single Server-Sent Events frame.

    ``default=str`` is a backstop, not the plan: tool results are converted to
    plain JSON by _jsonable() long before they get here. But a stream that died
    mid-run because one value could not be serialized would take the whole log
    with it, so anything that slips through is stringified instead.
    """
    return (f"id: {ev_id}\nevent: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n")


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    """Read *key* from *obj* whether it is a dict or an object with attributes."""
    if obj is None:
        return default
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


# --- Tool results ----------------------------------------------------------
#
# What a tool returns is whatever its author's type annotation implies: a
# number, a dict, a pydantic model, a dataclass. Everything downstream of here
# is JSON — the SSE frames, the session log, the report, the agent's context —
# so the conversion happens once, at this edge.
#
# The shape that arrives also depends on which library built the server. MCP
# output schemas must be objects, so a tool returning a bare number has its
# value wrapped as {"result": ...}. Servers built with the fastmcp library mark
# that wrapping and our client unwraps it automatically; servers built with the
# official SDK's FastMCP wrap without the marker, and the value arrives as a
# generated "<tool>Output" model instead of the number. Both are handled below.

_JSONABLE_DEPTH = 12
"""Recursion cap for _jsonable. Deep enough for any real tool result, and it
ends a reference cycle rather than a RecursionError inside a workflow run."""


def _jsonable(value: Any, _depth: int = 0) -> Any:
    """Convert *value* into something json.dumps can write. Never raises."""
    if value is None or isinstance(value, (bool, int, float, str)):
        # NaN and the infinities are not valid JSON for a strict reader.
        if isinstance(value, float) and (value != value or value in (
                float("inf"), float("-inf"))):
            return str(value)
        return value
    if _depth >= _JSONABLE_DEPTH:
        return str(value)

    d = _depth + 1
    if isinstance(value, dict):
        return {str(k): _jsonable(v, d) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v, d) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")

    # pydantic models — the common case for a typed MCP tool result.
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _jsonable(dump(mode="json"), d)
        except Exception:
            try:
                return _jsonable(dump(), d)
            except Exception:
                pass
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return _jsonable(dataclasses.asdict(value), d)
        except Exception:
            pass
    # numpy scalars answer .item(); arrays answer .tolist().
    for attr in ("tolist", "item"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return _jsonable(fn(), d)
            except Exception:
                pass
    return str(value)


def _schema_wraps_result(schema: Any) -> bool:
    """Whether *schema* describes a result wrapped as ``{"result": ...}``.

    fastmcp's own servers say so with ``x-fastmcp-wrap-result``; the official
    SDK wraps without saying, so the shape has to be recognised as well.
    """
    if not isinstance(schema, dict):
        return False
    if schema.get("x-fastmcp-wrap-result"):
        return True
    props = schema.get("properties")
    return (schema.get("type") == "object" and isinstance(props, dict)
            and set(props) == {"result"})


def _content_value(result: Any) -> Any:
    """Fall back to the text blocks when there is no structured content.

    The official SDK emits none for an untyped ``dict``/``list`` return, so
    without this the value would be lost entirely.
    """
    texts = []
    for item in (_attr(result, "content") or []):
        if _attr(item, "type") == "text":
            texts.append(_attr(item, "text", ""))
    if not texts:
        return None

    def parse(t: str) -> Any:
        try:
            return json.loads(t)
        except Exception:
            return t

    return parse(texts[0]) if len(texts) == 1 else [parse(t) for t in texts]


def _tool_value(result: Any, output_schema: Any = None) -> Any:
    """The value a tool returned, as plain JSON data.

    Prefers ``structured_content`` — it is the wire form, so it is JSON by
    construction — and unwraps the ``{"result": ...}`` envelope described by
    *output_schema*. Falls back to ``.data`` (what the in-process NIMO client
    provides) and then to the text blocks.
    """
    structured = _attr(result, "structured_content")
    if isinstance(structured, dict):
        if _schema_wraps_result(output_schema):
            return _jsonable(structured.get("result"))
        # No schema to consult: the envelope is still recognisable on its own,
        # and a tool that genuinely returns a one-key {"result": ...} object
        # loses only that wrapper.
        if output_schema is None and set(structured) == {"result"}:
            return _jsonable(structured["result"])
        return _jsonable(structured)
    if structured is not None:
        return _jsonable(structured)

    data = _attr(result, "data")
    if data is not None:
        return _jsonable(data)
    return _jsonable(_content_value(result))


def _as_number(value: Any) -> Optional[float]:
    """The single number *value* stands for, or None if it is not one number.

    An update block needs one objective value, but a tool that reports it may
    return ``42.0``, ``{"result": 42.0}`` or ``{"voltage": 42.0}`` depending on
    how its author typed it. Anything ambiguous — several numbers, or none —
    returns None so the caller can say so rather than optimize on a guess.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    if isinstance(value, (list, tuple)) and len(value) == 1:
        return _as_number(value[0])
    if isinstance(value, dict):
        found = [n for n in (_as_number(v) for v in value.values()) if n is not None]
        return found[0] if len(found) == 1 else None
    return None


def extract_images(source: Any) -> list[dict]:
    """Extract base64 image items from an MCP tool result."""
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
# Workflow execution
# ---------------------------------------------------------------------------

def _start_session(app: FastAPI) -> "Session":
    """Begin a new session and make it the current one.

    Wraps NimoWrapper.start_session(), which is what creates the folder and
    copies the master candidates.csv into it.
    """
    from .nimo_tools import INITIAL_CANDIDATES

    wrapper = app.state.nimo_wrapper
    wrapper.start_session()
    previous: Optional[Session] = getattr(app.state, "session", None)
    app.state.session = Session(
        started_at=now_s(),
        run_dir=wrapper.run_dir,
        candidates_snapshot=INITIAL_CANDIDATES,
        candidates_sha256=_file_sha256(os.path.join(wrapper.run_dir, INITIAL_CANDIDATES)),
        # Carried over: which language someone writes in is a fact about them,
        # not about the campaign, and "New session" clears the conversation it
        # would otherwise have to be worked out from again.
        language=getattr(previous, "language", None),
    )
    return app.state.session


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


async def _exec_node(app: FastAPI, job: Job, node: dict, ctx: dict) -> None:
    """Execute a single workflow AST node (tool call, repeat, if, or update block)."""
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

    if kind == "update":
        bid = node.get("block_id")
        if isinstance(bid, str) and bid:
            job.push("active", {"block_id": bid})
        body = node.get("body", [])
        if not isinstance(body, list):
            raise RuntimeError("update.body must be a list")
        if len(body) != 1:
            raise RuntimeError("an update block takes exactly one inner block")
        # Run the inner block; a numeric tool result updates ctx["last_float"].
        # Reset first so we only pick up a value produced inside this update block.
        ctx["last_float"] = None
        # No marker line here: the inner tool's card and the nimo update card
        # follow immediately and say the same thing, in the same shape as every
        # other entry in the log.
        for child in body:
            await _exec_node(app, job, child, ctx)
        value = ctx.get("last_float")
        if value is None:
            raise RuntimeError("update block: the block(s) inside did not return a float/int")
        # Delegate the actual NIMO update call through the normal tool path so it
        # reuses the same logging, task handling, and history recording.
        synthetic = {"kind": "tool", "server_id": "nimo", "tool": "update",
                     "args": {"objs": value}, "block_id": bid}
        await _exec_node(app, job, synthetic, ctx)
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
    meta = await _find_tool(client, tool)
    output_schema = _attr(meta, "outputSchema")
    use_task = _supports_tasks(client) and _meta_supports_task(meta)

    if use_task:
        r = await _call_tool_as_task(client, tool, resolved, _job_handle_sink(job))
    else:
        r = await client.call_tool(tool, resolved)

    t_end = time.time()
    result = _tool_value(r, output_schema)
    images = extract_images(r)

    # If the tool returned only images (no text data), record image metadata
    if result is None and images:
        result = [{"type": "image", "mimeType": img.get("mimeType", "image/png"),
                   "size": len(img.get("data", ""))} for img in images]

    # A substituted algorithm must never pass unnoticed. It rides along with the
    # output so it lands inside this call's card rather than as a loose line
    # below it, and it stays in the history that the session markdown is built from.
    notes = getattr(r, "notes", None) or []
    output: dict[str, Any] = {"data": result}
    if images:
        output["images"] = images
    if notes:
        output["notes"] = notes
    job.push("log", {"kind": "tool_output", "server_id": server_id,
                      "tool": tool, "output": output, "text": "Result"})
    job.history.append({
        "kind": "tool", "server_id": server_id, "tool": tool,
        "args": resolved, "result": result, "notes": notes,
        "started_at": t_start, "finished_at": t_end,
        "duration_s": round(t_end - t_start, 3),
    })
    # What an update block feeds to nimo. A structured result counts when
    # exactly one number can be read out of it — see _as_number.
    number = _as_number(result)
    if number is not None:
        ctx["last_float"] = number


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


async def _find_tool(client: Client, tool_name: str) -> Any:
    """The named tool's metadata, or None if it cannot be read.

    One listing answers both questions the call path has — can this tool run as
    a cancellable task, and how is its result shaped — so they do not cost two
    round trips.
    """
    try:
        for t in await client.list_tools():
            if t.name == tool_name:
                return t
    except Exception:
        pass
    return None


def _meta_supports_task(meta: Any) -> bool:
    """Whether tool metadata declares taskSupport as 'optional' or 'required'."""
    execution = _attr(meta, "execution")
    if not execution:
        return False
    return _attr(execution, "taskSupport", "forbidden") in ("optional", "required")


async def _tool_supports_task(client: Client, tool_name: str) -> bool:
    """Check if a specific tool declares taskSupport as 'optional' or 'required'."""
    return _meta_supports_task(await _find_tool(client, tool_name))


def _detach_task_cancel(task: Any) -> None:
    """Fire ``task.cancel()`` (a coroutine) without awaiting the current context.

    Used as a fallback when a cancellation is not driven by the /cancel endpoint
    (which awaits the notification itself). ``task.cancel()`` must be awaited to
    actually send ``tasks/cancel`` to the server, so schedule it detached.
    """
    async def _do() -> None:
        try:
            await task.cancel()
        except Exception:
            pass
    try:
        asyncio.ensure_future(_do())
    except Exception:
        pass


async def _call_tool_as_task(client: Client, tool: str, args: dict,
                             on_handle: Callable[[Any, Any], None]) -> Any:
    """Call a tool as a background task with cancellation support.

    *on_handle* is given ``(task, client)`` while the call is in flight and
    ``(None, None)`` once it is over, so a cancel endpoint can reach the handle
    and ``await task.cancel()`` (sending ``tasks/cancel`` to the MCP server).
    Workflows park it on the Job; the agent keys it by tool call id.
    """
    task = await client.call_tool(tool, args, task=True)

    if not hasattr(task, "wait"):
        return task

    on_handle(task, client)
    try:
        return await task.result()
    except asyncio.CancelledError:
        # Fallback for cancellations not initiated via /cancel: task.cancel() is
        # a coroutine, so schedule it detached (it can't be awaited here while we
        # are being cancelled).
        if _supports_task_cancel(client):
            _detach_task_cancel(task)
        raise
    finally:
        on_handle(None, None)


def _job_handle_sink(job: "Job") -> Callable[[Any, Any], None]:
    """Park an in-flight task handle on the job, for /workflow/{id}/cancel."""
    def sink(task: Any, client: Any) -> None:
        job.current_task = task
        job.current_client = client
    return sink


# ---------------------------------------------------------------------------
# Chat / agent log extraction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.current_workflow = None
    app.state.workflow_lock = asyncio.Lock()
    app.state.server_lock = asyncio.Lock()
    # Serializes report builds: two of them would race over the same two files,
    # and each spends a model call getting there.
    app.state.report_lock = asyncio.Lock()

    # NIMO runs in-process: call NimoWrapper directly (no MCP server/subprocess).
    # Tool schemas are snapshotted once so /tools keeps its previous output.
    from .nimo_tools import wrapper as nimo_wrapper, LocalNimoClient, snapshot_nimo_tools

    log.info("Using in-process NIMO (direct function calls)")
    nimo = LocalNimoClient(nimo_wrapper, await snapshot_nimo_tools())
    await nimo.__aenter__()
    app.state.client_nimo = nimo
    app.state.nimo_wrapper = nimo_wrapper  # for reading the current run_dir

    # One session folder for the whole app run. Without a candidates file there
    # is nothing to copy yet, so the first upload starts the session instead —
    # a fresh install must not fail to boot.
    app.state.session = None
    if os.path.isfile(str(CANDIDATES_FILE)):
        try:
            _start_session(app)
            log.info("Session folder: %s", app.state.session.run_dir)
        except Exception as e:
            log.error("Could not start a session: %s", e)
    else:
        log.info("No candidates file yet — a session starts on first upload")

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

    # Chat agent (fail-safe). Imported here, not at module scope, because
    # importing pydantic_ai is slow — skip it entirely when the agent is off.
    app.state.agent = None
    app.state.agent_error = None
    if ENABLE_AGENT:
        # These moved into the UI; say so rather than ignoring them silently.
        stale = [k for k in ("model", "thinking") if k in AGENT_CONFIG]
        if stale:
            log.warning("config.yaml: agent.%s is no longer used — the model and "
                        "reasoning effort are chosen in the UI",
                        " / agent.".join(stale))
        try:
            from .agent import AgentRuntime

            app.state.agent = AgentRuntime(
                AGENT_CONFIG,
                list_tools=_collect_tools,
                call_tool=_agent_call_tool,
                list_parameters=_nimo_parameters,
                activity=_activity_digest,
                make_report=_make_report,
                await_workflow=_await_workflow,
            )
            log.info("Chat agent ready: %s / %s",
                     app.state.agent.provider, app.state.agent.model)
        except Exception as e:
            app.state.agent_error = f"{type(e).__name__}: {e}"
            log.error("Chat agent unavailable: %s", app.state.agent_error)

    try:
        yield
    finally:
        job: Optional[Job] = app.state.current_workflow
        if job and job.task and not job.task.done():
            job.task.cancel()
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
        "port": SERVER_PORT,
        "config_path": str(_CONFIG_PATH),
        "data_dir": str(USER_DATA_DIR),
        "candidates_file": str(CANDIDATES_FILE),
        "results_dir": str(RESULTS_DIR),
        "enable_agent": ENABLE_AGENT,
        "agent": {
            "provider": str(AGENT_CONFIG.get("provider") or "openai"),
        } if ENABLE_AGENT else None,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def _collect_tools() -> List[dict]:
    """Gather tool schemas from NIMO and every connected MCP server.

    A server that fails to answer contributes nothing rather than breaking the
    whole listing.
    """
    out: List[dict] = []
    for sid, client in [("nimo", app.state.client_nimo), *app.state.mcp_clients.items()]:
        try:
            for t in await client.list_tools():
                out.append({"server_id": sid, "name": t.name,
                             "description": t.description, "input_schema": t.inputSchema,
                             "output_schema": getattr(t, "outputSchema", None)})
        except Exception:
            pass
    return out


@app.get("/tools")
async def list_tools():
    return await _collect_tools()


# --- Gateway handed to the chat agent ---------------------------------------
# Passed in as plain callables so agent.py never has to import this module.

# In-flight task handles for agent tool calls, keyed by tool call id. A dict,
# not the Job's single slot, because the model may run tools in parallel.
_agent_tool_handles: Dict[str, tuple] = {}


def _agent_handle_sink(call_id: str) -> Callable[[Any, Any], None]:
    def sink(task: Any, client: Any) -> None:
        if task is None:
            _agent_tool_handles.pop(call_id, None)
        else:
            _agent_tool_handles[call_id] = (task, client)
    return sink


async def _agent_cancel_tools() -> None:
    """Tell every in-flight agent tool call to stop, where the server allows it.

    Best-effort: servers that do not advertise tasks/cancel simply keep going,
    and one failure must not stop the rest from being notified.
    """
    for call_id, (task, client) in list(_agent_tool_handles.items()):
        try:
            if _supports_task_cancel(client):
                await task.cancel()
        except Exception:
            pass
        _agent_tool_handles.pop(call_id, None)


async def _agent_call_tool(server_id: str, tool: str, args: dict,
                           call_id: str = "") -> dict:
    """Run one tool on behalf of the agent and return ``{data, images}``.

    Errors come back as ``{"error": ...}`` instead of raising: unlike the
    workflow engine — which aborts the whole run — the agent can read a failure
    and decide what to do next.
    """
    job: Optional[Job] = app.state.current_workflow
    if job and job.status in ("queued", "running"):
        return {"error": "A Blockly workflow is running. Tools are unavailable "
                         "until it finishes, to avoid clashing over NIMO's run state."}
    started = time.time()
    output_schema = None
    try:
        client = _get_client(app, server_id)
        meta = await _find_tool(client, tool)
        output_schema = _attr(meta, "outputSchema")
        # Prefer the task-augmented path so a stop can reach the server; fall
        # back to a plain call for servers or tools that do not offer it.
        if call_id and _supports_tasks(client) and _meta_supports_task(meta):
            r = await _call_tool_as_task(client, tool, args, _agent_handle_sink(call_id))
        else:
            r = await client.call_tool(tool, args)
    except Exception as e:
        failure = f"{type(e).__name__}: {e}"
        _record_agent_tool(server_id, tool, args, {"error": failure}, started)
        return {"error": failure}

    result = _tool_value(r, output_schema)
    images = extract_images(r)
    if result is None and images:
        result = [{"type": "image", "mimeType": img.get("mimeType", "image/png"),
                   "size": len(img.get("data", ""))} for img in images]
    out: dict = {"data": result}
    if images:
        out["images"] = images
    # Notes go to the model as well: it should know when the algorithm it asked
    # for was substituted, or its next answer will misdescribe what ran.
    notes = getattr(r, "notes", None) or []
    if notes:
        out["notes"] = notes
    # Chat-driven calls belong in the session record too — the folder should
    # show everything that touched the instruments, not just workflow runs.
    _record_agent_tool(server_id, tool, args, result, started, notes)
    return out


def _record_agent_tool(server_id: str, tool: str, args: dict, result: Any,
                       started: float, notes: Optional[List[str]] = None) -> None:
    """Append an agent tool call to the session log. Never raises."""
    try:
        session: Optional[Session] = getattr(app.state, "session", None)
        if session is None:
            return
        finished = time.time()
        session.entries.append({
            "kind": "agent_tool",
            "server_id": server_id, "tool": tool, "args": args, "result": result,
            "notes": notes or [],
            "started_at": started, "finished_at": finished,
            "duration_s": round(finished - started, 3),
        })
        _write_session_log(app)
    except Exception:
        pass


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


async def _nimo_parameters() -> List[str]:
    """Candidate parameter names, or an empty list before a session exists.

    Also handed to the chat agent, which needs them to know what
    ``proposal.<name>`` may refer to when it writes a workflow.
    """
    r = await app.state.client_nimo.call_tool("get_parameter_names", {})
    params = r.data
    if not isinstance(params, list) or not all(isinstance(x, str) for x in params):
        raise RuntimeError(f"Unexpected return: {type(params)}")
    return params


@app.get("/nimo/parameters")
async def nimo_parameters():
    try:
        return {"ok": True, "parameters": await _nimo_parameters()}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


@app.get("/nimo/candidates")
async def get_candidates():
    """Return the current candidates.csv content (for the upload dialog preview)."""
    p = CANDIDATES_FILE
    if not p.is_file():
        return {"ok": True, "exists": False}
    try:
        content = p.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "exists": True, "content": content}


@app.post("/nimo/candidates")
async def upload_candidates(file: UploadFile):
    """Upload a new candidates.csv, which starts a new session."""
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

    # New candidates mean a new campaign, so this starts a fresh session.
    try:
        session = _start_session(app)
    except Exception as e:
        return {"ok": False, "error": f"File saved but the session failed to start: {e}"}

    return {"ok": True, "message": f"{file.filename} uploaded — new session started",
            "run_dir": session.run_dir}


def _open_in_file_manager(path: str) -> None:
    """Open *path* in the OS file manager (Explorer / Finder / xdg-open)."""
    import subprocess
    import sys

    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # Windows only
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


@app.post("/nimo/open-data-dir")
async def open_data_dir():
    """Open the data directory in the OS file manager.

    Convenience for the locally-run app: opens the fixed, server-controlled
    ``USER_DATA_DIR`` (never a client-supplied path) so the user can browse to
    candidates.csv / results in Explorer / Finder / their file manager.
    """
    path = str(USER_DATA_DIR)
    try:
        os.makedirs(path, exist_ok=True)
        _open_in_file_manager(path)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Settings (dynamic MCP server management)
# ---------------------------------------------------------------------------

class AddServerRequest(BaseModel):
    name: str
    url: str


@app.get("/settings/servers")
async def list_servers():
    # nimo runs in-process (direct function calls), not as an MCP server, so it
    # is not listed here — only the user-added MCP servers are shown.
    out = []
    for name, url in app.state.dynamic_servers.items():
        out.append({"name": name, "url": url, "builtin": False, "reconnectable": True})
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
    return {"ok": True, "name": name}


@app.post("/settings/servers/{name}/reconnect")
async def reconnect_server(name: str):
    """Re-establish the connection to an already-registered MCP server.

    Use this after the target server has been restarted: the stored URL is
    reused so there is no need to remove and re-add the server. The new client
    is connected and verified *before* the old one is dropped, so a failed
    reconnect (e.g. the server is still down) leaves the previous connection
    untouched.
    """
    async with app.state.server_lock:
        # Built-in NIMO runs in-process (direct calls) — nothing to reconnect.
        if name == NIMO_MCP_NAME:
            return {"ok": False,
                    "error": "NIMO runs in-process and has no separate server to reconnect to"}

        # Dynamic servers (swaps the entry in app.state.mcp_clients).
        url = app.state.dynamic_servers.get(name)
        if url is None:
            return {"ok": False, "error": f"Server '{name}' not found"}

        clients: Dict[str, Client] = app.state.mcp_clients
        old = clients.get(name)
        new = Client(url)
        try:
            await new.__aenter__()
            await new.list_tools()
        except Exception as e:
            try:
                await new.__aexit__(None, None, None)
            except Exception:
                pass
            return {"ok": False, "error": f"Failed to reconnect: {e}"}

        clients[name] = new
        app.state.dynamic_servers[name] = url
        if old is not None:
            try:
                await old.__aexit__(None, None, None)
            except Exception:
                pass
    return {"ok": True, "name": name, "url": url}


# ---------------------------------------------------------------------------
# Chat agent API (experimental)
# ---------------------------------------------------------------------------

class AgentChatRequest(BaseModel):
    prompt: str
    auto_approve: bool = True


class AgentApproveRequest(BaseModel):
    call_id: str
    approved: bool


def _agent_unavailable() -> Optional[str]:
    """Return why the agent cannot be used, or None if it is ready."""
    if not ENABLE_AGENT:
        return "Agent mode is disabled. Set enable_agent: true in config.yaml."
    if getattr(app.state, "agent", None) is None:
        return getattr(app.state, "agent_error", None) or "Agent is not initialized."
    return None


def _note_user_language(text: str) -> None:
    """Remember which language the user writes in, for the report to follow.

    Taken from the prompt on its way in rather than from the conversation on
    the way out: an interrupted turn throws the conversation away, and the
    report should not change language because of that. An inconclusive prompt
    ("ok", "yes") leaves the previous answer standing.
    """
    session: Optional[Session] = getattr(app.state, "session", None)
    lang = report.detect_language(text or "")
    if session is not None and lang is not None:
        session.language = lang


@app.post("/agent/chat")
async def agent_chat(req: AgentChatRequest):
    """Stream one chat turn back as Server-Sent Events.

    Unlike workflows there is no job/event buffer: a turn is short-lived and
    never needs to be reattached after a reload, so the reply is streamed
    straight out of this request.
    """
    # Before the turn, not inside gen(): this has to survive the turn being
    # interrupted, which is exactly when the conversation is thrown away.
    _note_user_language(req.prompt)

    async def gen():
        n = 0
        problem = _agent_unavailable()
        if problem:
            yield sse(1, "error", {"error": problem})
            return
        runtime = app.state.agent
        # An interrupted turn cannot be resumed — pydantic-ai gives no access to
        # its partial history — so the conversation is dropped rather than left
        # out of step with what actually ran.
        completed = False
        try:
            async with runtime.lock:
                runtime.auto_approve = req.auto_approve
                async for event, payload in runtime.stream_reply(req.prompt):
                    n += 1
                    yield sse(n, event, payload)
                    if event in ("done", "canceled"):
                        completed = event == "done"
        except (asyncio.CancelledError, GeneratorExit):
            # Client went away: a disconnect cancels the task, while a closed
            # generator arrives as GeneratorExit.
            raise
        except Exception as e:
            # An error leaves the previous turns intact, so keep the history.
            completed = True
            yield sse(n + 1, "error", {"error": f"{type(e).__name__}: {e}"})
        finally:
            if not completed:
                runtime.reset()

    return StreamingResponse(gen(), media_type="text/event-stream")


def _load_agent_state() -> dict:
    """What the UI chose last time, or an empty dict. Never raises."""
    try:
        with open(AGENT_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except Exception:
        # Missing on a first run, and unreadable state is not worth failing for.
        return {}


def _save_agent_state(model: str, thinking: Any) -> None:
    """Remember the current pick for the next launch. Never raises."""
    try:
        with open(AGENT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"provider": app.state.agent.provider,
                       "model": model, "thinking": thinking}, f, indent=2)
    except Exception as e:
        log.warning("Could not save the agent settings: %s", e)


@app.get("/agent/models")
async def agent_models():
    """List the models the configured endpoint offers, plus the current settings.

    Also where last launch's pick is restored. It happens here rather than at
    startup because this is the one place the endpoint's real list is in hand:
    a remembered model that the backend no longer offers is dropped instead of
    being set and failing on the first message. Doing it at startup would also
    mean a network call before the app could serve anything.

    A listing failure is not fatal: some OpenAI-compatible servers do not
    implement /models, so the current values are still returned and the UI
    falls back to a plain label.
    """
    problem = _agent_unavailable()
    if problem:
        return {"ok": False, "error": problem}
    runtime = app.state.agent
    try:
        models = await runtime.list_models()
        error = None
    except Exception as e:
        models, error = [], f"{type(e).__name__}: {e}"

    # Only when nothing has been chosen yet this run — never overrides a live
    # setting, so reloading the page mid-session cannot move the model.
    if not runtime.model:
        saved = _load_agent_state()
        if saved.get("provider") == runtime.provider and saved.get("model") in models:
            try:
                runtime.apply_settings(saved["model"], saved.get("thinking"))
            except Exception as e:
                log.warning("Could not restore the last agent settings: %s", e)

    return {"ok": True, "models": models, "error": error,
            "provider": runtime.provider, "model": runtime.model,
            "thinking": runtime.thinking}


class AgentSettingsRequest(BaseModel):
    model: str
    thinking: Optional[Union[bool, str]] = None


@app.post("/agent/settings")
async def agent_settings(req: AgentSettingsRequest):
    """Switch the model / reasoning effort without restarting the server."""
    problem = _agent_unavailable()
    if problem:
        return {"ok": False, "error": problem}
    runtime = app.state.agent
    # Wait for any in-flight reply so the model cannot change mid-stream.
    async with runtime.lock:
        try:
            runtime.apply_settings(req.model, req.thinking)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # Saved only once the change actually took, so a rejected model is not
        # what the next launch comes back to.
        _save_agent_state(runtime.model, runtime.thinking)
        return {"ok": True, "model": runtime.model, "thinking": runtime.thinking}


@app.post("/agent/approve")
async def agent_approve(req: AgentApproveRequest):
    """Let a tool call waiting on confirmation proceed, or deny it.

    Deliberately outside the runtime lock: /agent/chat holds that lock for the
    whole turn, and the call being approved is what it is waiting for.
    """
    runtime = getattr(app.state, "agent", None)
    if runtime is None:
        return {"ok": False, "error": "Agent is not initialized."}
    if not runtime.approve(req.call_id, req.approved):
        return {"ok": False, "error": "No tool call is waiting for that decision."}
    return {"ok": True}


class AgentCancelRequest(BaseModel):
    # Only the Stop button sends this. The pagehide beacon posts an empty body,
    # so a reload does not take a running campaign down with it — the run
    # survives and reattachIfRunning picks its log back up.
    stop_workflow: bool = False


@app.post("/agent/cancel")
async def agent_cancel(req: Optional[AgentCancelRequest] = None):
    """Stop the turn in flight, and optionally the run it started.

    Outside the runtime lock for the same reason as /agent/approve: the chat
    request holds it for the whole turn, which is exactly what we are stopping.

    In Auto mode plan_workflow blocks until its run is over, so stopping the
    turn alone would leave the instruments working with nobody waiting on them.
    Only the run *this turn* launched is taken down: a workflow the user started
    by hand is theirs, and the Cancel button above the workspace stops that one.
    """
    runtime = getattr(app.state, "agent", None)
    if runtime is None:
        return {"ok": False, "error": "Agent is not initialized."}
    runtime.request_abort()
    await _agent_cancel_tools()
    workflow_id = runtime.take_active_workflow() if (req and req.stop_workflow) else None
    if workflow_id:
        job: Optional[Job] = app.state.current_workflow
        if job and job.id == workflow_id and job.status in ("queued", "running"):
            await workflow_cancel(workflow_id)
        else:
            workflow_id = None   # already over, or superseded by another run
    return {"ok": True, "canceled_workflow": bool(workflow_id)}


@app.get("/agent/history")
async def agent_history():
    """The conversation so far, so a reload can redraw the chat log.

    The log lives only in the page, but the conversation lives on the server —
    without this the screen comes back empty while the agent still remembers.
    """
    runtime = getattr(app.state, "agent", None)
    if runtime is None:
        return {"ok": True, "items": []}
    return {"ok": True, "items": runtime.history()}


@app.post("/agent/reset")
async def agent_reset():
    """Forget the conversation so the next message starts a fresh context."""
    runtime = getattr(app.state, "agent", None)
    if runtime is not None:
        runtime.reset()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Workflow API (Blockly mode)
# ---------------------------------------------------------------------------

class WorkflowStartRequest(BaseModel):
    workflow: dict
    workspace_xml: str


class WorkflowXmlRequest(BaseModel):
    workflow: dict


@app.post("/workflow/xml")
async def workflow_xml_preview(req: WorkflowXmlRequest):
    """Render a workspace AST as NIMO workflow XML, without running it.

    The page owns the AST — the workspace is the executable form — so a preview
    of what the agent just designed has to come back through here rather than
    being built when the blocks were placed. There is no run behind it, so the
    <candidates> block carries columns but no snapshot.
    """
    return {"ok": True, "xml": _workflow_xml(app, req.workflow)}


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
            # No new folder here: runs share the session's, building on the same
            # candidates.csv. start_workflow() only snapshots the file and
            # restarts the per-run history.
            if app.state.session is None:
                raise RuntimeError(
                    "No session yet — upload a candidates file to start one.")
            job.candidates_snapshot = app.state.nimo_wrapper.start_workflow()
            # Rendered now, not at the end: it describes the run's inputs, and
            # the snapshot hash it carries must be the one taken just above.
            # The session log reuses this exact string.
            job.workflow_xml = _nimo_workflow_xml(app, job)
            job.push("log", {"kind": "workflow_xml", "xml": job.workflow_xml})

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
        finally:
            # Record this run in the session and rewrite its log, for
            # done / error / canceled alike (best-effort, all sync so it is
            # safe to run while a CancelledError is propagating).
            try:
                session: Optional[Session] = app.state.session
                if session is not None:
                    session.entries.append({
                        "kind": "workflow",
                        # Falls back only if the run died before it was rendered.
                        "workflow_xml": job.workflow_xml or _nimo_workflow_xml(app, job),
                        "candidates_snapshot": job.candidates_snapshot,
                        "status": job.status,
                        "started_at": job.started_at,
                        "finished_at": job.finished_at,
                        "error": job.error,
                        "history": list(job.history),
                    })
                    log_path = _write_session_log(app)
                    if log_path:
                        job.run_dir = session.run_dir
                        job.log_path = log_path
                        job.push("log", {"kind": "text", "text": f"Log saved: {log_path}"})
            except Exception as e:
                job.push("log", {"kind": "text", "text": f"Failed to save log: {e}"})
            # Last, and outside the try above: a waiter must be released even
            # when writing the log failed, and only once the entry it may go on
            # to read has been appended.
            job.completed.set()

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
            # Reliably notify the MCP server first: task.cancel() is a coroutine
            # that sends tasks/cancel. Awaiting it here (a context that is NOT
            # being cancelled) guarantees the server is told to stop before we
            # tear down the runner. Then cancel the runner task.
            task, client = job.current_task, job.current_client
            if task is not None and client is not None and _supports_task_cancel(client):
                try:
                    await task.cancel()
                except Exception:
                    pass
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


# ---------------------------------------------------------------------------
# Workflow log — compact, well-formed XML (see data/nimo-workflow-0.0.2.xsd)
# ---------------------------------------------------------------------------

_WORKFLOW_XML_VERSION = "0.0.2"


def _xa(s: Any) -> str:
    """Escape a value for use inside an XML attribute."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _servers_xml(app: FastAPI, indent: str) -> str:
    """Build the <servers> block from the connected MCP clients.

    NIMO is not listed: it runs in-process as direct Python calls, so it is not
    a server this run connected to. Its tools still appear in the procedure as
    server="nimo".
    """
    lines = [f"{indent}<servers>"]
    dynamic = getattr(app.state, "dynamic_servers", {}) or {}
    clients = getattr(app.state, "mcp_clients", {}) or {}
    for sid, client in clients.items():
        url = dynamic.get(sid, "")
        init = getattr(client, "initialize_result", None)
        info = getattr(init, "serverInfo", None) if init is not None else None
        ver = getattr(info, "version", None) if info is not None else None
        # No type attribute: every entry here is an MCP server, since NIMO is
        # excluded above.
        attrs = f' name="{_xa(sid)}" url="{_xa(url)}"'
        if ver:
            # Full serverInfo.version. A git commit is carried inside this single
            # version string as semver build metadata (e.g. "1.2.3+abc1234").
            attrs += f' version="{_xa(ver)}"'
        lines.append(f"{indent}  <server{attrs}/>")
    lines.append(f"{indent}</servers>")
    return "\n".join(lines)


def _file_sha256(path: str) -> Optional[str]:
    """SHA-256 of a file, or None if it cannot be read."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def _candidates_xml(app: FastAPI, job: Optional["Job"], indent: str) -> str:
    """Build the <candidates> block: the run's snapshot plus its columns.

    ``snapshot`` and ``sha256`` describe the same frozen file — the copy taken
    when this run started, not the working candidates.csv, which has been
    rewritten by the time the log is rendered. Without a snapshot neither is
    emitted, since a hash whose file is unknown says nothing.
    """
    wrapper = getattr(app.state, "nimo_wrapper", None)
    params = getattr(wrapper, "parameter_names", None) or []
    objectives = getattr(wrapper, "objective_names", None) or []

    attrs = ""
    snapshot = getattr(job, "candidates_snapshot", None) if job else None
    if snapshot:
        run_dir = getattr(wrapper, "run_dir", "") or ""
        sha = _file_sha256(os.path.join(run_dir, snapshot)) if run_dir else None
        attrs = f' snapshot="{_xa(snapshot)}"'
        if sha:
            attrs += f' sha256="{_xa(sha)}"'

    if not params and not objectives:
        return f"{indent}<candidates{attrs}/>"
    lines = [f"{indent}<candidates{attrs}>"]
    for p in params:
        lines.append(f'{indent}  <parameter name="{_xa(p)}"/>')
    for o in objectives:
        lines.append(f'{indent}  <objective name="{_xa(o)}"/>')
    lines.append(f"{indent}</candidates>")
    return "\n".join(lines)


def _arg_attr(k: str, v: Any) -> str:
    """Render one tool argument as an XML attribute (or '' to skip, e.g. last_result)."""
    if isinstance(v, dict):
        if NIMO_VAR_KEY in v:
            return f' {_xa(k)}="proposal.{_xa(v[NIMO_VAR_KEY])}"'
        if v.get(LAST_FLOAT_KEY) is True:
            return ""  # last_result is not user-visible — omit
        if LOOP_COUNTER_KEY in v:
            return f' {_xa(k)}="counter.{_xa(v[LOOP_COUNTER_KEY])}"'
    if isinstance(v, bool):
        val = "true" if v else "false"
    elif isinstance(v, (int, float)):
        val = str(v)
    elif isinstance(v, str):
        val = v
    else:
        val = json.dumps(v, ensure_ascii=False)
    return f' {_xa(k)}="{_xa(val)}"'


def _tool_xml(node: dict, indent: str) -> str:
    attrs = f' server="{_xa(node.get("server_id", ""))}" name="{_xa(node.get("tool", ""))}"'
    args = node.get("args", {})
    if isinstance(args, dict):
        for k, v in args.items():
            attrs += _arg_attr(k, v)
    return f"{indent}<tool{attrs}/>"


def _blocks_xml(nodes: Any, indent: str) -> list[str]:
    out: list[str] = []
    if not isinstance(nodes, list):
        return out
    for node in nodes:
        if isinstance(node, dict):
            out.append(_block_xml(node, indent))
    return out


def _branch_xml(name: str, body: Any, indent: str) -> str:
    """Render a <then>/<else> branch; inline when it is a single tool."""
    nodes = [n for n in (body or []) if isinstance(n, dict)] if isinstance(body, list) else []
    if not nodes:
        return f"{indent}<{name}/>"
    if len(nodes) == 1 and nodes[0].get("kind") == "tool":
        return f"{indent}<{name}>{_tool_xml(nodes[0], '')}</{name}>"
    inner = "\n".join(_blocks_xml(nodes, indent + "  "))
    return f"{indent}<{name}>\n{inner}\n{indent}</{name}>"


def _block_xml(node: dict, indent: str) -> str:
    kind = node.get("kind")
    if kind == "tool":
        return _tool_xml(node, indent)
    if kind == "repeat":
        cv = node.get("counter_var")
        times = int(node.get("times", 0) or 0)
        attrs = (f' counter="{_xa(cv)}"' if cv else "") + f' times="{times}"'
        body = _blocks_xml(node.get("body", []), indent + "  ")
        inner = ("\n" + "\n".join(body) + f"\n{indent}") if body else ""
        return f"{indent}<repeat{attrs}>{inner}</repeat>"
    if kind == "if":
        cv = node.get("counter_var", "")
        op = node.get("op", "==")
        val = int(node.get("value", 0) or 0)
        lines = [f'{indent}<if counter="{_xa(cv)}" op="{_xa(op)}" value="{val}">']
        lines.append(_branch_xml("then", node.get("then", []), indent + "  "))
        else_nodes = [n for n in (node.get("else") or []) if isinstance(n, dict)]
        if else_nodes:
            lines.append(_branch_xml("else", else_nodes, indent + "  "))
        lines.append(f"{indent}</if>")
        return "\n".join(lines)
    if kind == "update":
        body = _blocks_xml(node.get("body", []), indent + "  ")
        inner = ("\n" + "\n".join(body) + f"\n{indent}") if body else ""
        return f"{indent}<update>{inner}</update>"
    return f"{indent}<!-- unknown block: {_xa(kind)} -->"


def _workflow_xml(app: FastAPI, ast: dict, job: Optional[Job] = None) -> str:
    """Render the workflow (servers + candidates + procedure) as compact, strict XML.

    The AST is passed in rather than read off a Job, so the same renderer serves
    a run and a preview of a workspace that has not been run.
    """
    body = ast.get("body", []) if isinstance(ast, dict) else []
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<nimo-workflow version="{_WORKFLOW_XML_VERSION}">']
    lines.append(_servers_xml(app, "  "))
    lines.append(_candidates_xml(app, job, "  "))
    proc = _blocks_xml(body, "    ")
    if proc:
        lines.append("  <workflow>")
        lines.extend(proc)
        lines.append("  </workflow>")
    else:
        lines.append("  <workflow/>")
    lines.append("</nimo-workflow>")
    return "\n".join(lines)


def _nimo_workflow_xml(app: FastAPI, job: Job) -> str:
    """Render a run's workflow XML, including its candidates snapshot."""
    ast = job.workflow_ast if isinstance(job.workflow_ast, dict) else {}
    return _workflow_xml(app, ast, job)


def _local_tz():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().tzinfo


def _tz_name() -> str:
    from datetime import datetime
    return datetime.now(_local_tz()).strftime("UTC%z")  # e.g. "UTC+0900"


def _fmt_ts(ts: Optional[float]) -> str:
    """Local time down to milliseconds, matching the workflow log format."""
    if ts is None:
        return ""
    from datetime import datetime
    return datetime.fromtimestamp(ts, tz=_local_tz()).strftime("%Y-%m-%d %H:%M:%S.") \
        + f"{int(ts * 1000) % 1000:03d}"


def _step_markdown(step: dict, heading: str) -> List[str]:
    """Render one tool call — shared by workflow steps and agent tool calls."""
    lines = [f"{heading}\n"]
    lines.append(f"- Started: {_fmt_ts(step.get('started_at'))}")
    lines.append(f"- Finished: {_fmt_ts(step.get('finished_at'))}")
    lines.append(f"- Duration: {step.get('duration_s', '')}s")
    for note in step.get("notes") or []:
        lines.append(f"- Note: {note}")
    lines.append("")
    # default=str: tool results are converted by _jsonable() on the way in, but
    # the session log must never fail to write over how one value prints.
    lines.append("### Input\n")
    lines.append("```json")
    lines.append(json.dumps(step.get("args", {}), indent=2, ensure_ascii=False,
                            default=str))
    lines.append("```\n")
    lines.append("### Output\n")
    lines.append("```json")
    lines.append(json.dumps(step.get("result"), indent=2, ensure_ascii=False,
                            default=str))
    lines.append("```\n")
    return lines


def _build_session_markdown(session: "Session") -> str:
    """Render the whole session — workflow runs and agent tool calls in order.

    Rewritten in full each time rather than appended to: done / error / canceled
    would otherwise each need their own append path, and a run interrupted
    mid-write would leave a broken file behind.
    """
    lines: List[str] = ["# Session Log\n"]
    lines.append(f"- Started: {_fmt_ts(session.started_at)}")
    lines.append(f"- Timezone: {_tz_name()}")
    lines.append(f"- Folder: {session.run_dir}")
    if session.candidates_snapshot:
        lines.append(f"- Candidates: {session.candidates_snapshot}")
    if session.candidates_sha256:
        lines.append(f"- SHA-256: {session.candidates_sha256}")
    lines.append("")

    for n, entry in enumerate(session.entries, 1):
        if entry.get("kind") == "agent_tool":
            label = f"[{entry.get('server_id', '')}] {entry.get('tool', '')}"
            lines.append(f"## {n}. Agent tool — {label}\n")
            lines.extend(_step_markdown(entry, "### Call"))
            continue

        lines.append(f"## {n}. Workflow\n")
        lines.append("```xml")
        lines.append(entry.get("workflow_xml", ""))
        lines.append("```\n")
        lines.append(f"- Status: {entry.get('status', '')}")
        if entry.get("candidates_snapshot"):
            lines.append(f"- Candidates: {entry['candidates_snapshot']}")
        lines.append(f"- Started: {_fmt_ts(entry.get('started_at'))}")
        lines.append(f"- Finished: {_fmt_ts(entry.get('finished_at'))}")
        started, finished = entry.get("started_at"), entry.get("finished_at")
        if started and finished:
            lines.append(f"- Duration: {round(finished - started, 3)}s")
        if entry.get("error"):
            lines.append(f"- Error: {entry['error']}")
        lines.append("")
        for i, step in enumerate(entry.get("history", []), 1):
            label = f"[{step.get('server_id', '')}] {step.get('tool', '')}"
            lines.extend(_step_markdown(step, f"### Step {i}: {label}"))

    return "\n".join(lines)


def _write_session_log(app: FastAPI) -> Optional[str]:
    """Rewrite the session log on disk. Returns its path, or None if not possible."""
    session: Optional[Session] = getattr(app.state, "session", None)
    if session is None or not os.path.isdir(session.run_dir):
        return None
    path = os.path.join(session.run_dir, "session_log.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_build_session_markdown(session))
    return path


# ---------------------------------------------------------------------------
# Activity digest — what the chat agent is told actually happened
# ---------------------------------------------------------------------------
#
# The session log on disk is the complete archive; this is the briefing. They
# have different jobs, so this deliberately does not reuse _step_markdown:
# that renders one step as ~14 lines of timestamps and indented JSON, which is
# fine for a file and far too much for something injected into every turn.
#
# Scope is the current session only. Nothing is read back from the folders of
# earlier sessions — pressing "New session" starts the agent's picture over
# too, because _start_session installs a Session whose entries are empty.

ACTIVITY_ENTRIES = 2
"""How many of the most recent session entries are shown in full."""

ACTIVITY_STEPS = 20
"""Steps shown per workflow run, counted from the end."""

PLANNED_RUN_STEPS = 120
"""Steps quoted back for a run the agent started itself, counted from the end.

Far above ACTIVITY_STEPS because that run is the whole point of the call: a
ten-cycle campaign is thirty-odd steps, and the 20-step cap would silently drop
its first third."""

ACTIVITY_CHARS = 200
"""Cap on a single argument or result. Base64 images never reach here (they are
already reduced to metadata at execution time), so the real risk is an array."""


def _brief(value: Any, limit: int = ACTIVITY_CHARS) -> str:
    """Render one value small enough to sit on a single line."""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…(+{len(text) - limit} chars)"


def _call_line(step: dict) -> str:
    """One tool call as ``server.tool(arg=value) -> result``."""
    args = ", ".join(f"{k}={_brief(v)}" for k, v in (step.get("args") or {}).items())
    call = f"{step.get('server_id', '')}.{step.get('tool', '')}({args})"
    return f"{call} -> {_brief(step.get('result'))}"


def _workflow_digest(entry: dict, steps_shown: int = ACTIVITY_STEPS) -> List[str]:
    """One finished workflow run: how it went, what ran, and every step."""
    steps = entry.get("history") or []
    started, finished = entry.get("started_at"), entry.get("finished_at")
    took = f", {round(finished - started, 1)} s" if started and finished else ""
    lines = [f"### Workflow run — {entry.get('status', '?')}, {len(steps)} steps{took}"
             f", finished {_fmt_ts(finished)}"]
    if entry.get("error"):
        lines.append(f"Error: {entry['error']}")

    procedure = report.workflow_element(entry.get("workflow_xml") or "")
    if procedure:
        lines.append("Procedure as executed:")
        lines.append(procedure)

    if steps:
        lines.append("Steps:")
        shown = steps[-steps_shown:]
        if len(steps) > len(shown):
            lines.append(f"  … ({len(steps) - len(shown)} earlier steps omitted)")
        for n, step in enumerate(shown, len(steps) - len(shown) + 1):
            lines.append(f"  {n}. {_call_line(step)}")
    return lines


def _activity_digest() -> str:
    """What has happened in this session, for the agent's instructions.

    Empty when there is nothing to report, so the agent's prompt stays as it was
    before a session existed.
    """
    session: Optional[Session] = getattr(app.state, "session", None)
    if session is None:
        return ""

    lines: List[str] = []
    job: Optional[Job] = getattr(app.state, "current_workflow", None)
    if job and job.status in ("queued", "running"):
        # Worth stating plainly: while this is true _agent_call_tool refuses
        # every instrument call, and the agent should be able to say why.
        lines.append("A workflow is running right now, so instrument tools are "
                     "unavailable until it finishes.")

    entries = session.entries
    if not entries and not lines:
        return ""

    lines.append(f"Session folder: {session.run_dir} (full record: session_log.md)")
    shown = entries[-ACTIVITY_ENTRIES:]
    if len(entries) > len(shown):
        lines.append(f"({len(entries) - len(shown)} earlier entries omitted)")

    for entry in shown:
        lines.append("")
        if entry.get("kind") == "agent_tool":
            lines.append(f"### Agent tool — {_call_line(entry)}")
        else:
            lines.extend(_workflow_digest(entry))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Waiting for a planned workflow, so the agent can report what it actually did
# ---------------------------------------------------------------------------
#
# In Auto mode plan_workflow blocks here until the run it just laid out is over.
# It waits rather than starting the run itself because the page, not this
# module, turns a workspace into an executable AST and POSTs /workflow/start.
# That keeps one builder for the AST and makes the workspace the single source
# of truth: what executes is always what the user can see, and can edit, on
# screen.

WORKFLOW_START_TIMEOUT_S = 30
"""How long the page gets to act on a workflow event and POST /workflow/start.

Generous for a local round trip plus applyAgentWorkflow's toolbox rebuild, and
short enough that a closed tab does not hold the reply for long."""

WORKFLOW_RUN_TIMEOUT_S = float(AGENT_CONFIG.get("workflow_timeout_s") or 6 * 3600)
"""How long the run itself may take before the agent gives up waiting.

A campaign on real hardware is measured in hours, so this is a safety net rather
than a schedule — without it a wedged MCP call would hold the turn forever."""


def _job_digest(job: Job) -> str:
    """A finished run, in the same words get_session_activity uses.

    Built from the Job rather than from session.entries: the Job is unambiguously
    *this* run, whereas the session's newest entry is only probably it.
    """
    return "\n".join(_workflow_digest({
        "status": job.status,
        "workflow_xml": job.workflow_xml,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
        "history": job.history,
    }, steps_shown=PLANNED_RUN_STEPS))


async def _await_workflow(on_start: Optional[Callable[[str], None]] = None) -> dict:
    """Wait for the page to run the workflow just placed, and report the outcome.

    Called by plan_workflow immediately after the workflow event is queued for
    the page, with no await in between — so the job id snapshotted here is
    genuinely "before", and a different id appearing in current_workflow is the
    browser's POST /workflow/start arriving.

    ``on_start`` is handed the new job's id, so a Stop can cancel that exact run.

    Returns ``{"status": ..., "digest": ...}`` where status is one of:

    ``busy``
        Another run was already in flight; nothing was placed or started.
    ``not_started``
        The page never started one; nothing has happened.
    ``timeout``
        Still running when we stopped waiting.
    ``done`` / ``error`` / ``canceled``
        It finished, and the digest describes it.
    """
    prev: Optional[Job] = getattr(app.state, "current_workflow", None)
    prev_id = prev.id if prev is not None else None
    if prev is not None and prev.status in ("queued", "running"):
        # The page leaves the workspace alone while a run is on (see
        # applyAgentWorkflow), so nothing was placed and nothing will start.
        # Answering now rather than waiting out the start timeout for nothing.
        return {"status": "busy", "digest": ""}

    deadline = now_s() + WORKFLOW_START_TIMEOUT_S
    job: Optional[Job] = None
    while now_s() < deadline:
        cur: Optional[Job] = getattr(app.state, "current_workflow", None)
        if cur is not None and cur.id != prev_id:
            job = cur
            break
        await asyncio.sleep(0.1)
    if job is None:
        return {"status": "not_started", "digest": ""}

    if on_start is not None:
        on_start(job.id)
    try:
        # Polling for the start and an Event for the finish: the start window is
        # bounded and needs no new state on /workflow/start, while this one is
        # the unbounded wait and must not spin for hours.
        await asyncio.wait_for(job.completed.wait(), timeout=WORKFLOW_RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"status": "timeout", "digest": ""}
    return {"status": job.status, "digest": _job_digest(job)}


# ---------------------------------------------------------------------------
# Session report
# ---------------------------------------------------------------------------
#
# Written on demand only, unlike session_log.md which is rewritten after every
# entry. Both files are regenerated from session.entries each time, so editing
# report.md by hand is fine — it just does not survive the next generation.

REPORT_MD = "report.md"
REPORT_HTML = "report.html"

# Figures the report knows how to caption: file-name stem -> label key. Anything
# else in the folder is left alone. The stems match the file names nimo_tools'
# plot_history_best and plot_phase_diagram write, and have to be edited in step
# with them. They are spelled out rather than imported because importing
# nimo_tools pulls in the nimo library, which is too slow to do at module scope.
REPORT_FIGURES = (("best_objective", "fig_history"), ("phase_diagram", "fig_phase"))


def _session_dir() -> Optional[str]:
    session: Optional[Session] = getattr(app.state, "session", None)
    if session is None or not os.path.isdir(session.run_dir):
        return None
    return session.run_dir


def _report_direction() -> str:
    """Which way the campaign was optimizing, from the last proposal made.

    nimo is never told a direction globally — it is chosen per block — so the
    most recent choice is the best available answer.
    """
    session: Optional[Session] = getattr(app.state, "session", None)
    for entry in reversed(session.entries if session else []):
        steps = ([entry] if entry.get("kind") == "agent_tool"
                 else list(reversed(entry.get("history") or [])))
        for step in steps:
            if step.get("server_id") == "nimo" and step.get("tool") in report.PROPOSAL_TOOLS:
                return ("minimization" if step.get("tool") == "minimization"
                        else "maximization")
    return "maximization"


def _candidate_stats() -> Any:
    """The wrapper's own measured/unmeasured counts, or None if unreadable."""
    try:
        return app.state.nimo_wrapper._candidate_stats()
    except Exception:
        return None


def _collect_figures(notes: List[str],
                     lang: str = report.DEFAULT_LANG) -> List[report.Figure]:
    """Find the plots the run already produced. Draws nothing itself.

    Every figure in a report is a file that some plot step in a workflow wrote,
    which is what keeps the figures and the procedure printed beside them in
    step. Drawing one here instead would be wrong twice over: it would leave a
    PNG in the session folder that session_log.md has no entry for, and it would
    overwrite the file a plot step deliberately made — with a curve whose
    direction came from _report_direction()'s guess rather than from the block
    that ran, so a minimization campaign could come back plotted upside down.

    It also settles what the report may talk about. Deciding here whether a
    phase diagram was worth drawing meant guessing from the spread of the
    objective values, and saying so either way — which put the phase diagram
    into every report, including the minimization runs that never asked for one.
    """
    run_dir = _session_dir()
    figures: List[report.Figure] = []
    if not run_dir:
        return figures
    try:
        names = sorted(os.listdir(run_dir))
    except OSError:
        return figures
    for stem, label_key in REPORT_FIGURES:
        # The exact name, plus nimo's "_1", "_2" … variants, which is what
        # plot_history_best writes when there is more than one objective.
        found = [n for n in names
                 if n == f"{stem}.png"
                 or (n.startswith(f"{stem}_") and n.endswith(".png"))]
        for name in found:
            caption = report.label(lang, label_key)
            if len(found) > 1:
                caption = f"{caption} ({name})"
            figures.append(report.Figure(caption, os.path.join(run_dir, name)))
    if not figures:
        notes.append(report.label(lang, "note_no_figures"))
    return figures


def _collect_report_data(lang: str = report.DEFAULT_LANG) -> report.ReportData:
    """Gather everything the report needs. Blocking — it reads the CSVs.

    ``lang`` arrives up front rather than being set afterwards: the notes and
    figure captions written here are report content, so they need the same
    language as the headings around them.
    """
    session: Session = app.state.session
    wrapper = app.state.nimo_wrapper
    notes: List[str] = []

    job: Optional[Job] = getattr(app.state, "current_workflow", None)
    if job and job.status in ("queued", "running"):
        notes.append(report.label(lang, "note_running"))

    direction = _report_direction()
    figures = _collect_figures(notes, lang)
    stats = _candidate_stats()
    objectives = tuple(getattr(wrapper, "objective_names", ()) or ())
    best = None
    if objectives:
        best = report.best_objective(
            os.path.join(session.run_dir, "candidates.csv"),
            objectives[-1], minimize=(direction == "minimization"))

    return report.ReportData(
        started_at=session.started_at,
        run_dir=session.run_dir,
        candidates_snapshot=session.candidates_snapshot or "",
        candidates_sha256=session.candidates_sha256 or "",
        parameter_names=tuple(getattr(wrapper, "parameter_names", ()) or ()),
        objective_names=objectives,
        direction=direction,
        total=getattr(stats, "total", 0),
        measured=getattr(stats, "measured", 0),
        unmeasured=getattr(stats, "unmeasured", 0),
        best=best,
        runs=report.build_runs(session.entries),
        agent_calls=report.build_agent_calls(session.entries),
        figures=figures,
        notes=notes,
        lang=lang,
    )


async def _make_report() -> dict:
    """Write report.md and report.html into the session folder.

    Serialized here rather than at the call sites, so the agent's write_report
    tool cannot race the toolbar button over the same two files.
    """
    async with app.state.report_lock:
        return await _build_report()


async def _build_report() -> dict:
    session: Optional[Session] = getattr(app.state, "session", None)
    if session is None:
        return {"ok": False,
                "error": "No session yet — upload a candidates file to start one."}
    if not os.path.isdir(session.run_dir):
        return {"ok": False, "error": f"Session folder is missing: {session.run_dir}"}

    # One language for the whole report: the headings, the figure captions, the
    # notes and the summary all take it from here, so they cannot end up
    # disagreeing. English until the user has written something that says
    # otherwise — see _note_user_language.
    lang = session.language or report.DEFAULT_LANG

    try:
        # Reading the candidate CSVs and walking the session blocks; off the
        # event loop so a large session does not stall everything else.
        data = await asyncio.to_thread(_collect_report_data, lang)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    runtime = getattr(app.state, "agent", None)
    if runtime is not None:
        try:
            data.summary = await runtime.summarize(report.facts_text(data), lang)
        except Exception as e:
            log.warning("Report summary unavailable: %s", e)
    if not data.summary:
        data.notes.append(report.label(lang, "no_summary"))

    files = {}
    try:
        for name, text in ((REPORT_MD, report.to_markdown(data)),
                           (REPORT_HTML, report.to_html(data))):
            path = os.path.join(session.run_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            files[name] = path
    except OSError as e:
        return {"ok": False, "error": f"Could not write the report: {e}"}

    return {"ok": True, "run_dir": session.run_dir, "files": files,
            "summarized": bool(data.summary)}


def _report_path(name: str) -> Optional[str]:
    """Path of a generated report file, or None when it has not been made yet."""
    session: Optional[Session] = getattr(app.state, "session", None)
    if session is None:
        return None
    path = os.path.join(session.run_dir, name)
    return path if os.path.isfile(path) else None


@app.post("/session/report")
async def session_report():
    """Build the report without serving it — the agent's tool uses this."""
    return await _make_report()


def _report_error_page(message: str) -> HTMLResponse:
    return HTMLResponse(
        "<!DOCTYPE html><html lang='en'><meta charset='utf-8'>"
        "<title>Report</title>"
        "<body style='font-family:system-ui,sans-serif;padding:40px;color:#1f2328'>"
        f"<p>{html_escape(message)}</p></body></html>", status_code=404)


@app.get("/session/report/view")
async def session_report_view(generate: bool = False):
    """Serve report.html inline, so it opens in a tab and can be printed to PDF.

    With ``generate=1`` the report is built first, which is how the toolbar
    button works: one navigation into a new tab does the whole job. Opening the
    tab and building the report cannot be separate steps — the build takes as
    long as the model needs, and by then a popup is far outside the click that
    would have justified it.
    """
    if generate:
        result = await _make_report()
        if not result.get("ok"):
            return _report_error_page(
                f"Could not generate the report: {result.get('error', 'unknown error')}")
    path = _report_path(REPORT_HTML)
    if path is None:
        return _report_error_page(
            "No report has been generated yet. Press \"Generate report\" above the log.")
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/session/report/download")
async def session_report_download(fmt: str = "html"):
    """Download a report file as an attachment."""
    name = REPORT_MD if fmt == "md" else REPORT_HTML
    path = _report_path(name)
    if path is None:
        return {"ok": False, "error": "No report has been generated yet."}
    media = ("text/markdown; charset=utf-8" if fmt == "md"
             else "text/html; charset=utf-8")
    return FileResponse(path, media_type=media, filename=name)


# ---------------------------------------------------------------------------
# Session API
# ---------------------------------------------------------------------------

@app.get("/session")
async def session_info():
    """Where the current session is writing, for the UI to show."""
    session: Optional[Session] = getattr(app.state, "session", None)
    if session is None:
        return {"ok": True, "active": False}
    return {"ok": True, "active": True, "run_dir": session.run_dir,
            "started_at": session.started_at, "entries": len(session.entries)}


@app.post("/session/new")
async def session_new():
    """Start over: a new folder, a fresh copy of candidates.csv, empty history."""
    if not os.path.isfile(str(CANDIDATES_FILE)):
        return {"ok": False, "error": "No candidates file yet — upload one first."}
    job: Optional[Job] = app.state.current_workflow
    if job and job.status in ("queued", "running"):
        return {"ok": False, "error": "A workflow is running — wait for it to finish."}
    try:
        session = _start_session(app)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "run_dir": session.run_dir}


@app.post("/session/open")
async def session_open():
    """Open the session folder in the OS file manager."""
    session: Optional[Session] = getattr(app.state, "session", None)
    run_dir = session.run_dir if session else ""
    if not run_dir or not os.path.isdir(run_dir):
        return {"ok": False, "error": "No session folder yet"}
    try:
        _open_in_file_manager(run_dir)
        return {"ok": True, "path": run_dir}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# No download endpoint for the session log: it is written to the session folder
# on every entry, and the folder is one click away via /session/open. The report
# is the exception (/session/report/view and /download) — it is the file meant
# to be read and passed on, so it is reachable without going through the folder.


# Use the ``nimo-controller`` console script (see nimo_controller.cli) to run.