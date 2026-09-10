"""Have the planner turn a natural-language instruction into nimo-workflow XML.

Uses the tool catalog written by build_catalog.py, so no server needs to be
running. The provider and base URL come from config.yaml (``agent.provider`` /
``agent.base_url``, same as the app) unless overridden on the command line;
the model name and thinking level are CLI arguments because the app chooses
them in the UI, not the config. Progress is logged to stderr; the XML (the same
``<nimo-workflow>`` format the app writes as its execution log, see
data/nimo-workflow-0.0.2.xsd) goes to stdout or --output.

    uv run python evaluation/generate_xml.py "PHYSBOで10サイクル最適化して" \
        --model gpt-5-mini --thinking low
"""

import argparse
import asyncio
import json
import os
import queue
import shutil
import sys
import threading
import time
from typing import Any

import httpx
import yaml

from pydantic_ai import capture_run_messages
from pydantic_ai.messages import (
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
)
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from pydantic_ai.usage import RunUsage

from nimo_controller.agent import THINKING_LEVELS
from nimo_controller.paths import resolve_config_path
from nimo_controller.planner import Workflow, build_catalog, write_workflow


# ---------------------------------------------------------------------------
# nimo-workflow XML — mirrors server.py's _workflow_xml() output, but fed from
# the planner's Workflow model (whose argument values are already the
# "proposal.<x>" / "counter.<x>" / literal strings the format uses) and from
# the catalog JSON instead of live app state.
# ---------------------------------------------------------------------------

def _xa(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _tool_xml(t: Any, indent: str) -> str:
    """A tool call: <nimo tool=..> for built-ins, <call server=.. tool=..> for
    MCP server tools. The selection tool carries its direction in the
    minimization argument, so it renders as-is; plot_history_best still
    spells its direction as a mode argument, rendered as the same
    minimization flag.
    """
    flag = ""
    args = dict(t.arguments)
    if t.server == "nimo":
        if t.name == "plot_history_best" and "mode" in args:
            if args.pop("mode") == "minimization":
                flag = ' minimization="true"'
        tag, attrs = "nimo", f' tool="{_xa(t.name)}"'
    else:
        tag, attrs = "call", f' server="{_xa(t.server)}" tool="{_xa(t.name)}"'
    for k, v in args.items():
        attrs += f' {_xa(k)}="{_xa(v)}"'
    return f"{indent}<{tag}{attrs}{flag}/>"


def _branch_xml(name: str, body: list, indent: str) -> str:
    if not body:
        return f"{indent}<{name}/>"
    inner = "\n".join(_block_xml(b, indent + "  ") for b in body)
    return f"{indent}<{name}>\n{inner}\n{indent}</{name}>"


def _block_xml(b: Any, indent: str) -> str:
    if b.kind == "tool":
        return _tool_xml(b, indent)
    if b.kind == "repeat":
        body = [_block_xml(x, indent + "  ") for x in b.body]
        inner = ("\n" + "\n".join(body) + f"\n{indent}") if body else ""
        return (f'{indent}<repeat counter="{_xa(b.counter)}" '
                f'times="{b.times}">{inner}</repeat>')
    if b.kind == "if":
        lines = [f'{indent}<if counter="{_xa(b.counter)}" op="{_xa(b.op)}" '
                 f'value="{b.value}">']
        lines.append(_branch_xml("then", b.then, indent + "  "))
        if b.otherwise:
            lines.append(_branch_xml("else", b.otherwise, indent + "  "))
        lines.append(f"{indent}</if>")
        return "\n".join(lines)
    # update block: <nimo tool="update"> wrapping the measured call
    return (f'{indent}<nimo tool="update">\n'
            f"{_block_xml(b.tool, indent + '  ')}\n{indent}</nimo>")


_TYPE_NAMES = {"number": "float", "integer": "int", "string": "str",
               "boolean": "bool"}


def _params_attr(tool: dict) -> str:
    """Compact signature, e.g. params="{x1: float, x2: float}"; "" if no args."""
    props = (tool.get("input_schema") or {}).get("properties") or {}
    if not props:
        return ""
    sig = ", ".join(f"{k}: {_TYPE_NAMES.get(v.get('type'), v.get('type') or 'any')}"
                    for k, v in props.items())
    return f' params="{{{_xa(sig)}}}"'


def to_nimo_workflow_xml(workflow: Workflow, data: dict) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<nimo-workflow version="0.0.4">']
    # MCP servers only — nimo's built-in tools are implied by the version.
    lines.append("  <servers>")
    for sid, meta in (data.get("servers") or {}).items():
        attrs = f' name="{_xa(sid)}"'
        if meta.get("version"):
            attrs += f' version="{_xa(meta["version"])}"'
        tools = [t for t in (data.get("tools") or []) if t["server_id"] == sid]
        if not tools:
            lines.append(f"    <server{attrs}/>")
            continue
        lines.append(f"    <server{attrs}>")
        for t in tools:
            lines.append(f'      <tool name="{_xa(t["name"])}"{_params_attr(t)}/>')
        lines.append("    </server>")
    lines.append("  </servers>")
    params = data.get("parameters") or []
    objectives = data.get("objectives") or []
    if params or objectives:
        lines.append("  <candidates>")
        for p in params:
            lines.append(f'    <parameter name="{_xa(p)}"/>')
        for o in objectives:
            lines.append(f'    <objective name="{_xa(o)}"/>')
        lines.append("  </candidates>")
    else:
        lines.append("  <candidates/>")
    proc = [_block_xml(b, "    ") for b in workflow.body]
    if proc:
        lines.append("  <workflow>")
        lines.extend(proc)
        lines.append("  </workflow>")
    else:
        lines.append("  <workflow/>")
    lines.append("</nimo-workflow>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model + progress
# ---------------------------------------------------------------------------

# Extra seconds a timed-out run gets to unwind before it is abandoned.
ABANDON_GRACE_S = 15.0

def build_model(name: str, provider: str, base_url: str | None,
                thinking: str | None, temperature: float | None = None,
                seed: int | None = None, timeout: float | None = None) -> Any:
    """Wrap *name* for *provider*.

    Settings are attached to the model itself because the planner agent's
    run() is not given model settings. temperature/seed pin down sampling for
    reproducible evaluation (best-effort — see run_eval.py). ``timeout`` also
    becomes the HTTP read timeout, so a server that goes silent mid-stream
    fails at the transport instead of relying on task cancellation.
    """
    fields: dict[str, Any] = {}
    if thinking is not None:
        fields["thinking"] = {"true": True, "false": False}.get(thinking, thinking)
    if temperature is not None:
        fields["temperature"] = temperature
    if seed is not None:
        fields["seed"] = seed
    settings = ModelSettings(**fields) if fields else None

    # read = longest silence tolerated between chunks, not the whole run.
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(
        connect=10.0, read=timeout or 600.0, write=10.0, pool=10.0))

    if provider == "ollama":
        url = base_url or "http://127.0.0.1:11434/v1"
        return OllamaModel(name, settings=settings,
                           provider=OllamaProvider(base_url=url,
                                                   http_client=http_client))
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("the OPENAI_API_KEY environment variable is not set")
    p = (OpenAIProvider(base_url=base_url, http_client=http_client) if base_url
         else OpenAIProvider(http_client=http_client))
    return OpenAIChatModel(name, provider=p, settings=settings)


def make_progress(timeout: float | None = None) -> tuple[Any, dict]:
    """Progress logger for the planner run.

    Thinking overwrites a single status line (carriage return) showing the
    tail of the stream, and the line is erased once thinking ends — it shows
    liveness without filling the log. When stderr is not a tty (redirected),
    thinking prints nothing at all. The workflow JSON body prints one dot per
    ~500 chars as before.

    Returns the event_stream_handler (called once per model request, so its
    call count is the attempt number) and its state dict.
    """
    state = {"attempt": 0, "chars": 0, "mode": None, "t0": time.monotonic(),
             "tail": "", "prev_len": 0, "abandoned": False}
    tty = sys.stderr.isatty()
    width = max(20, shutil.get_terminal_size(fallback=(100, 24)).columns - 1)

    def clear_status_line() -> None:
        if state["prev_len"]:
            sys.stderr.write("\r" + " " * state["prev_len"] + "\r")
            state["prev_len"] = 0

    def newline_if_streaming() -> None:
        if state["mode"] == "thinking":
            clear_status_line()   # thinking leaves no trace in the log
        elif state["mode"] is not None:
            sys.stderr.write("\n")
        state["mode"] = None
        sys.stderr.flush()

    def note(mode: str, text: str) -> None:
        if state["abandoned"]:
            return   # an abandoned run must not scribble over the next one
        if state["mode"] != mode:
            newline_if_streaming()
            state["mode"], state["chars"], state["tail"] = mode, 0, ""
            if mode != "thinking":
                sys.stderr.write(f"[planner] {mode} ")
        if mode == "thinking":
            if not tty:
                return
            state["tail"] = (state["tail"] + text)[-120:]
            flat = " ".join(state["tail"].split())
            line = ("[planner] thinking: …" + flat)[:width]
            pad = max(state["prev_len"] - len(line), 0)
            sys.stderr.write("\r" + line + " " * pad)
            state["prev_len"] = len(line)
        else:
            state["chars"] += len(text)
            while state["chars"] >= 500:
                state["chars"] -= 500
                sys.stderr.write(".")
        sys.stderr.flush()

    async def handler(ctx: Any, events: Any) -> None:
        state["attempt"] += 1
        if not state["abandoned"]:
            newline_if_streaming()
            label = ("" if state["attempt"] == 1
                     else " (validation failed, retrying)")
            elapsed = time.monotonic() - state["t0"]
            limit = f"limit {timeout:.0f}s" if timeout else "no limit"
            print(f"[planner] model request #{state['attempt']}{label} "
                  f"({elapsed:.0f}s elapsed, {limit})",
                  file=sys.stderr, flush=True)
        async for ev in events:
            if isinstance(ev, PartStartEvent):
                text = getattr(ev.part, "content", "") or ""
                if isinstance(ev.part, ThinkingPart):
                    note("thinking", text)
                elif isinstance(ev.part, TextPart):
                    note("writing workflow", text)
            elif isinstance(ev, PartDeltaEvent):
                text = getattr(ev.delta, "content_delta", "") or ""
                if isinstance(ev.delta, ThinkingPartDelta):
                    note("thinking", text)
                elif isinstance(ev.delta, TextPartDelta):
                    note("writing workflow", text)

    handler.finish = newline_if_streaming  # type: ignore[attr-defined]
    return handler, state


def _attempts_log_text(messages: list) -> str:
    """The run's conversation as a per-attempt transcript.

    Shows, in order, each model attempt (thinking + raw output) and the
    validation feedback that rejected it; the last output without a
    rejection is the accepted one. System/user prompts are omitted — they
    are the same for every attempt and known from the inputs.
    """
    lines: list[str] = []
    attempt = 0
    for msg in messages:
        if isinstance(msg, ModelResponse):
            attempt += 1
            thinking = [p.content for p in msg.parts
                        if isinstance(p, ThinkingPart) and p.content]
            if thinking:
                lines.append(f"--- attempt {attempt}: thinking ---")
                lines.extend(thinking)
            lines.append(f"--- attempt {attempt}: output ---")
            outputs = []
            for p in msg.parts:
                if isinstance(p, TextPart) and p.content:
                    outputs.append(p.content)
                elif isinstance(p, ToolCallPart):
                    args = (p.args_as_json_str()
                            if hasattr(p, "args_as_json_str") else str(p.args))
                    outputs.append(f"[tool call {p.tool_name}] {args}")
            lines.extend(outputs or ["(empty)"])
        else:
            for p in getattr(msg, "parts", []):
                if isinstance(p, RetryPromptPart):
                    content = p.content
                    if not isinstance(content, str):
                        content = "\n".join(str(e) for e in content) \
                            if isinstance(content, list) else str(content)
                    lines.append(f"--- attempt {attempt} rejected ---")
                    lines.append(content)
    return "\n".join(lines) + "\n"


def generate_with(write_fn: Any, instruction: str, catalog_data: dict,
                  model_name: str, provider: str | None = None,
                  base_url: str | None = None, thinking: str | None = None,
                  temperature: float | None = None,
                  seed: int | None = None,
                  timeout: float | None = None,
                  attempts_log: str | None = None) -> str:
    """Run one workflow writer on *instruction* and return nimo-workflow XML.

    ``write_fn`` is a write_workflow-shaped coroutine function (this module's
    structured-output planner, or generate_clairify's spec-prompted one).
    ``catalog_data`` is a parsed catalog JSON from build_catalog.py. provider
    and base_url default to config.yaml's agent section (then openai).
    ``timeout`` limits the whole run in seconds (asyncio.TimeoutError on
    expiry; None = no limit). ``attempts_log`` names a file that receives the
    per-attempt transcript (thinking, raw outputs, validation feedback) —
    written even when the run fails or times out. Progress and retries are
    logged to stderr.
    """
    catalog = build_catalog(catalog_data["tools"], catalog_data["parameters"])

    cfg_path = resolve_config_path()
    agent_cfg: dict = {}
    if cfg_path.is_file():
        with open(cfg_path, encoding="utf-8") as f:
            agent_cfg = (yaml.safe_load(f) or {}).get("agent") or {}
    provider = provider or agent_cfg.get("provider", "openai")
    base_url = base_url or agent_cfg.get("base_url")

    model = build_model(model_name, provider, base_url, thinking,
                        temperature, seed, timeout)
    handler, state = make_progress(timeout)
    usage = RunUsage()   # filled in place by the run
    done: queue.Queue = queue.Queue(1)

    def run_planner() -> None:
        # capture_run_messages is a contextvar, so it has to be entered on the
        # thread that does the run — the attempts log is written here too, so
        # an abandoned run still leaves its transcript behind.
        with capture_run_messages() as messages:
            try:
                done.put(("ok", asyncio.run(asyncio.wait_for(
                    write_fn(instruction, catalog, model, usage=usage,
                             event_stream_handler=handler), timeout))))
            except BaseException as e:   # reraised on the calling thread
                done.put(("error", e))
            finally:
                handler.finish()   # erase the thinking line, errors included
                if attempts_log:
                    os.makedirs(os.path.dirname(attempts_log) or ".",
                                exist_ok=True)
                    with open(attempts_log, "w", encoding="utf-8") as f:
                        f.write(_attempts_log_text(list(messages)))

    # asyncio.wait_for stops a well-behaved run, but it waits for the task it
    # cancelled: a stream that swallows CancelledError keeps the run going
    # (observed: a 60s limit still running after 4275s). So the real bound is
    # this wait — past it the thread is abandoned, daemon so it cannot hold up
    # the process, and silenced so it cannot scribble over the next run.
    threading.Thread(target=run_planner, daemon=True).start()
    try:
        outcome, value = done.get(
            timeout=None if timeout is None else timeout + ABANDON_GRACE_S)
    except queue.Empty:
        state["abandoned"] = True
        raise asyncio.TimeoutError(
            f"generation did not stop {ABANDON_GRACE_S:.0f}s after its "
            f"{timeout:.0f}s timeout; abandoned") from None
    if outcome == "error":
        raise value
    print(f"[planner] done in {time.monotonic() - state['t0']:.0f}s — "
          f"{state['attempt']} request(s), {usage.input_tokens} in / "
          f"{usage.output_tokens} out tokens", file=sys.stderr)
    return to_nimo_workflow_xml(value, catalog_data)


def generate(instruction: str, catalog_data: dict, model_name: str,
             provider: str | None = None, base_url: str | None = None,
             thinking: str | None = None, temperature: float | None = None,
             seed: int | None = None, timeout: float | None = None,
             attempts_log: str | None = None) -> str:
    """The app's structured-output (pydantic) planner as XML generation."""
    return generate_with(write_workflow, instruction, catalog_data, model_name,
                         provider, base_url, thinking, temperature, seed,
                         timeout, attempts_log)


def cli_main(generate_fn: Any, doc: str) -> None:
    """Shared command line for the generator modules."""
    parser = argparse.ArgumentParser(description=doc)
    parser.add_argument("instruction", nargs="?",
                        help="natural-language instruction (default: read stdin)")
    parser.add_argument("--catalog", default="evaluation/catalog.json",
                        help="catalog JSON from build_catalog.py")
    parser.add_argument("--model", required=True, help="model name")
    parser.add_argument("--provider", choices=("openai", "ollama"),
                        help="default: agent.provider from config.yaml, "
                             "or openai")
    parser.add_argument("--base-url",
                        help="default: agent.base_url from config.yaml; for "
                             "ollama falls back to http://127.0.0.1:11434/v1")
    parser.add_argument("--thinking",
                        choices=("true", "false") + THINKING_LEVELS,
                        help="reasoning effort (default: model's own)")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds allowed for the whole generation; "
                             "0 or less means no limit (default: %(default)s)")
    parser.add_argument("--output", help="write XML here instead of stdout")
    args = parser.parse_args()

    instruction = args.instruction or sys.stdin.read()
    if not instruction.strip():
        sys.exit("empty instruction")

    with open(args.catalog, encoding="utf-8") as f:
        data = json.load(f)

    try:
        xml = generate_fn(instruction, data, args.model,
                          args.provider, args.base_url, args.thinking,
                          timeout=args.timeout if args.timeout > 0 else None)
    except asyncio.TimeoutError as e:
        sys.exit(f"timed out after {args.timeout:.0f}s"
                 + (f": {e}" if str(e) else "")
                 + " (raise --timeout, or pass 0 for no limit)")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(xml)
    else:
        print(xml)


if __name__ == "__main__":
    cli_main(generate, __doc__)
