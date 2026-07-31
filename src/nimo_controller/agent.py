"""Chat agent — a thin pydantic-ai wrapper for the prompt box under the log.

``config.yaml`` only says *where* to talk to, via ``agent.provider``:

* ``openai`` — the API key is read from the ``OPENAI_API_KEY`` environment
  variable; ``base_url`` is optional (for OpenAI-compatible endpoints).
* ``ollama`` — ``base_url`` is required; a self-hosted server needs no API key.

Which model to use and how hard it should reason are picked in the UI, not
configured, so this starts with no model at all: the provider is built up front
(it is what lists the available models) and the agent itself only exists once
``apply_settings`` has been given a model.

The agent acts in two ways. Single operations go straight to the tools of the
connected MCP servers. Anything that repeats, optimizes or branches on a cycle
count is delegated to ``planner.py``, which writes it as a Blockly workspace —
loops and nimo's proposal/update cycle are Blockly concepts and cannot be
expressed as a series of individual tool calls.

IMPORTANT: importing ``pydantic_ai`` is slow, so ``server.py`` imports this
module only when ``enable_agent`` is true. Keep it out of the import path of
anything that loads at startup unconditionally.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

from pydantic_core import SchemaValidator, core_schema

from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.settings import ModelSettings

from .planner import build_catalog, describe, to_blockly_xml, write_workflow

# Effort levels accepted by ``agent.thinking`` alongside true/false.
THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")

SYSTEM_PROMPT = (
    "You are a helpful assistant embedded in NIMO Controller, a tool for running "
    "self-driving-laboratory workflows. Answer the user's questions concisely.\n\n"
    "You have two ways to act.\n\n"
    "* Call an instrument tool directly when the request is one or two concrete "
    "operations on the equipment (\"move the stage to slot 3\", \"measure the "
    "phase at 800 K and 1 atm\"). Tool names are of the form mcp__<server>__<tool>. "
    "Prefer this — it is immediate and easy to follow.\n"
    "* Call plan_workflow when the request describes a campaign rather than an "
    "operation: repetition, optimization over the candidate parameters, or a "
    "condition on the cycle number. That tool designs the whole procedure and "
    "lays it out as blocks in the Blockly workspace. It does not execute "
    "anything itself.\n\n"
    "Anything involving nimo's optimizer — selection, maximization, minimization, "
    "feeding a measurement back as an objective value, \"optimize\", \"N cycles\" — "
    "must go through plan_workflow. Those steps cannot be issued as single tool "
    "calls.\n\n"
    "Talking about a run\n\n"
    "Never say a workflow is running, has started, or is about to start unless "
    "plan_workflow told you so. When it runs, plan_workflow returns only once the "
    "run is over and hands you the steps that actually executed with their "
    "measured values: that is the real outcome, so quote those numbers rather "
    "than promising results later. When it does not run, nothing has happened and "
    "the user has to press Run.\n\n"
    "Once a workflow has run you may keep working in the same reply — read a "
    "session file, or plan the follow-up the user asked for. Run at most two "
    "workflows per reply, and only design a second one when the user asked for "
    "it: never start a second run to check or improve the first.\n\n"
    "Some tools drive real laboratory hardware or mutate optimization state, so "
    "call a tool only when the user's request clearly calls for it, and pass "
    "exactly the arguments they asked for. If a tool returns an error, tell the "
    "user what failed instead of retrying blindly. "
    "Report what actually happened, including failures, in the user's language."
)

SUMMARY_PROMPT = """\
You are a research assistant writing the summary section of a report on a
self-driving-laboratory session. Work only from the record you are given.

Write three to five paragraphs of continuous prose, in this order: what the
experiment was trying to achieve; what procedure ran and how many times; the
results and the best point found; what looks worth trying next.

Rules:

- Use only the numbers that appear in the record. Do not round them or invent
  any.
- Do not state anything the record does not show. Mark a conjecture as a
  conjecture.
- Say nothing about figures. Which ones the report contains is decided by the
  report generator, not by you, and the record does not say which it produced —
  so you cannot know. Never write that a figure was or was not made, and never
  ask for one; the report explains in its own notes anything it left out.
- No bullet points and no headings.
- No preamble and no acknowledgement; return the summary text alone.
- Write it in {language}.
"""
"""Filled in by ``summarize``, so ``{language}`` must stay the only brace pair
in here — any other would have to be doubled to survive ``str.format``."""

LANGUAGE_NAMES = {"ja": "Japanese", "en": "English"}
"""Which language ``summarize`` is asked to write in. Deciding *which* one is
report.detect_language's job — the server records it as the user types, because
this module's conversation history does not survive an interrupted turn."""

# How long a tool call waits for the user before it gives up and is denied.
APPROVAL_TIMEOUT_S = 600

# Runs that actually reached the instruments, per reply. Counted in
# _note_workflow rather than at design time, so a rejected design does not use
# up the budget for a run that never happened.
MAX_WORKFLOWS_PER_TURN = 2

# Designs attempted per reply, whether or not any of them ran. Bounds a model
# that keeps producing invalid workflows without ever reaching the hardware.
MAX_WORKFLOW_ATTEMPTS_PER_TURN = 4

# How often the chat stream is nudged while a run holds the turn open.
HEARTBEAT_S = 15

# Tool arguments arrive already shaped by the model; the MCP server does the
# real validation, so accept anything here (same approach as ExternalToolset).
TOOL_SCHEMA_VALIDATOR = SchemaValidator(schema=core_schema.any_schema())


def _slug(text: str) -> str:
    """Reduce a server or tool name to what a tool identifier may contain."""
    return re.sub(r"[^0-9A-Za-z_]", "_", str(text))


class AgentConfigError(RuntimeError):
    """Raised when the ``agent`` section of config.yaml is missing or invalid."""


class McpToolset(AbstractToolset):
    """Exposes the app's connected MCP servers to the agent as tools.

    Tools are listed fresh on every run rather than cached: the server set is
    editable at runtime from the Settings dialog, so a snapshot would eventually
    hand the agent a closed client.
    """

    def __init__(self, runtime: "AgentRuntime"):
        self._rt = runtime

    @property
    def id(self) -> Optional[str]:
        return "mcp"

    async def get_tools(self, ctx: RunContext) -> Dict[str, ToolsetTool]:
        tools: Dict[str, ToolsetTool] = {}
        routes: Dict[str, Tuple[str, str]] = {}
        for spec in await self._rt.list_tools():
            server_id, tool = str(spec.get("server_id") or ""), str(spec.get("name") or "")
            if not server_id or not tool:
                continue
            # Same shape as the Blockly block types (mcp__<server>__<tool>).
            name = f"mcp__{_slug(server_id)}__{_slug(tool)}"
            routes[name] = (server_id, tool)
            schema = spec.get("input_schema") or {"type": "object", "properties": {}}
            tools[name] = ToolsetTool(
                toolset=self,
                tool_def=ToolDefinition(
                    name=name,
                    description=spec.get("description") or "",
                    parameters_json_schema=schema,
                ),
                max_retries=1,
                args_validator=TOOL_SCHEMA_VALIDATOR,
            )
        # call_tool() only receives the flattened name, so keep the mapping back
        # to (server, tool) — the UI needs it too, for the badge colour.
        self._rt.set_tool_routes(routes)
        return tools

    async def call_tool(self, name: str, tool_args: Dict[str, Any],
                        ctx: RunContext, tool: ToolsetTool) -> Any:
        route = self._rt.route_for(name)
        if route is None:
            return {"error": f"unknown tool: {name}"}
        server_id, tool_name = route
        call_id = ctx.tool_call_id or name

        if not self._rt.auto_approve:
            approved = await self._rt.await_approval(call_id)
            if not approved:
                return "Tool call denied by the user."

        output = await self._rt.call_tool(server_id, tool_name, tool_args or {}, call_id)
        # Stash the full output (which may carry base64 images) for the UI and
        # hand the model the data alone, so images never enter its context.
        self._rt.record_tool_output(call_id, output)
        if "error" in output:
            return {"error": output["error"]}
        return output.get("data")


class AgentRuntime:
    """Holds the configured agent, its conversation history, and a turn lock.

    A single instance lives on ``app.state.agent`` — one conversation per
    server, mirroring how ``app.state.current_workflow`` holds a single job.
    """

    def __init__(
        self,
        raw: Dict[str, Any],
        list_tools: Optional[Callable[[], Awaitable[List[dict]]]] = None,
        call_tool: Optional[Callable[[str, str, dict, str], Awaitable[dict]]] = None,
        list_parameters: Optional[Callable[[], Awaitable[List[str]]]] = None,
        activity: Optional[Callable[[], str]] = None,
        make_report: Optional[Callable[[], Awaitable[dict]]] = None,
        await_workflow: Optional[Callable[..., Awaitable[dict]]] = None,
    ):
        self.provider: str = str(raw.get("provider") or "openai").strip().lower()
        self.base_url: Optional[str] = (str(raw.get("base_url")).strip()
                                        if raw.get("base_url") else None)
        # Both are chosen in the UI, never configured.
        self.model: str = ""
        self.thinking: Any = None
        # Set per turn from the Auto mode toggle.
        self.auto_approve: bool = True
        # Tool gateway, supplied by server.py so this module stays independent of it.
        self._list_tools = list_tools
        self._call_tool = call_tool
        self._list_parameters = list_parameters
        # Synchronous, unlike the others: it only reads state the server already
        # holds in memory, so there is nothing to await.
        self._activity = activity
        self._make_report = make_report
        # Blocks for the whole run, so this is the one gateway that can hold a
        # turn for hours.
        self._await_workflow = await_workflow
        # The run this turn launched, held only so a Stop can cancel that exact
        # one and leave a workflow the user started by hand alone.
        self._active_workflow: Optional[str] = None
        # Both per reply, reset at the top of stream_reply.
        self._workflow_runs = 0
        self._workflow_attempts = 0
        self._routes: Dict[str, Tuple[str, str]] = {}
        self._pending: Dict[str, asyncio.Future] = {}
        self._tool_outputs: Dict[str, dict] = {}
        # Out-of-band events a local tool wants the UI to see while the turn is
        # still running — stream_reply drains this alongside the model's own
        # events. Nothing arrives here from the model, only from our own tools.
        self._ui: asyncio.Queue = asyncio.Queue()
        # An Event, not a flag: stream_reply races it against the next event so
        # a stop lands immediately, rather than waiting for one to arrive.
        self._abort = asyncio.Event()
        # Conversation history, replaced wholesale after each completed turn.
        self.messages: List[Any] = []
        # Serializes turns so two overlapping requests cannot interleave history,
        # and so settings cannot be swapped mid-reply.
        self.lock = asyncio.Lock()
        # Built up front: it validates the connection settings and is what
        # list_models() talks to before any model has been picked.
        self._provider: Any = self._build_provider()
        self._agent: Any = None

    @property
    def ready(self) -> bool:
        """True once a model has been chosen and the agent exists."""
        return self._agent is not None

    # -- setup ------------------------------------------------------------

    def _rebuild(self) -> None:
        """(Re)create the agent from the current model/thinking values.

        Deliberately leaves ``self.messages`` alone: switching models keeps the
        conversation going, since pydantic-ai's history types are model-agnostic.
        """
        settings = (ModelSettings(thinking=self.thinking)
                    if self.thinking is not None else None)
        toolsets = [McpToolset(self)] if self._list_tools and self._call_tool else []
        agent = Agent(self._build_model(), instructions=SYSTEM_PROMPT,
                      model_settings=settings, toolsets=toolsets)
        self._register_local_tools(agent)
        self._register_activity(agent)
        self._agent = agent

    def _register_activity(self, agent: Any) -> None:
        """Tell the agent, every turn, what has actually happened in the lab.

        Registered as instructions rather than pushed into the conversation:
        pydantic-ai rebuilds instructions on every run and sends only the most
        recent set, so this is always current and never accumulates stale copies
        of itself in the history. That is what makes it work for a workflow the
        page starts *after* the turn that designed it has already ended.
        """
        if not self._activity:
            return

        @agent.instructions
        def recent_activity() -> str:
            digest = self._activity()
            if not digest:
                return ""
            return (
                "A record of what has actually been done in the laboratory this "
                "session, oldest first. Treat it as ground truth about results.\n\n"
                "The user can edit the blocks in the workspace before running "
                "them, so a workflow shown here may differ from the one you "
                "designed — this is what really ran. When you delegate to "
                "plan_workflow, copy any number you want acted on into the "
                "instruction: the planner cannot see this record.\n\n"
            ) + digest

    def _register_local_tools(self, agent: Any) -> None:
        """Attach the tools this module implements itself.

        Unlike the MCP tools these are not routed through the gateway, so they
        need registering on every rebuilt Agent rather than being listed by a
        toolset. Each one is guarded by the gateway it actually needs, so a
        missing catalog does not also cost the user the report.
        """
        if self._make_report:
            self._register_report_tool(agent)
        if not (self._list_tools and self._list_parameters):
            return

        @agent.tool
        async def plan_workflow(ctx: RunContext, instruction: str) -> str:
            """Design a multi-step experimental workflow and lay it out in Blockly.

            Use for anything that repeats, optimizes, or branches on a cycle
            count — and for every request that involves nimo's proposal/update
            loop. Not for a single instrument operation; call that instrument's
            tool directly instead.

            Args:
                instruction: What the workflow must achieve, in one
                    self-contained sentence. Include the number of cycles, the
                    algorithm if the user named one, and which measurement feeds
                    the optimizer. Never invent parameter values such as a
                    temperature or a concentration: the optimizer chooses those.
                    State only what the user asked for.

            Returns:
                What was placed in the workspace, and — when it runs — every
                step it actually executed with its measured values.
            """
            return await self._plan_workflow(instruction, ctx.usage)

    def _register_report_tool(self, agent: Any) -> None:
        @agent.tool_plain
        async def write_report() -> str:
            """Write a report on this session and save it in the session folder.

            Produces report.md and a self-contained report.html covering the
            experimental conditions, every workflow that ran with its measured
            values, the best result so far, and the plots. Use it when the user
            asks for a report, a summary document, or something to show someone.

            Returns:
                Where the report was written, or why it could not be.
            """
            result = await self._make_report()
            if not result.get("ok"):
                return f"Could not write the report: {result.get('error', 'unknown error')}"
            return (f"Report written to {result.get('run_dir', '')} "
                    "(report.md and report.html). Tell the user they can open it "
                    "with the link on the report card in the log, and that printing "
                    "that page from the browser saves it as a PDF.")

    @staticmethod
    def _parse_thinking(value: Any) -> Any:
        """Normalize ``agent.thinking`` into what ModelSettings accepts.

        Returns None when unset, so the model keeps its own default. Note that
        ``false`` is a request, not a guarantee: models that always reason
        (gpt-oss, for one) ignore it and keep emitting a thinking phase.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        level = str(value).strip().lower()
        if level in THINKING_LEVELS:
            return level
        raise AgentConfigError(
            f"invalid agent.thinking: {value!r} "
            f"(expected true, false, or one of {', '.join(THINKING_LEVELS)})"
        )

    def _build_provider(self) -> Any:
        """Create the backend connection. Built once and reused across rebuilds."""
        if self.provider == "ollama":
            if not self.base_url:
                raise AgentConfigError(
                    "agent.base_url is required for provider 'ollama' "
                    '(e.g. base_url: "http://localhost:11434/v1")'
                )
            return OllamaProvider(base_url=self.base_url)

        if self.provider == "openai":
            if not os.environ.get("OPENAI_API_KEY"):
                raise AgentConfigError(
                    "the OPENAI_API_KEY environment variable is not set "
                    "(required for provider 'openai')"
                )
            return (OpenAIProvider(base_url=self.base_url) if self.base_url
                    else OpenAIProvider())

        raise AgentConfigError(
            f"unknown agent.provider: {self.provider!r} (expected 'openai' or 'ollama')"
        )

    def _build_model(self) -> Any:
        """Wrap the current model name for the configured provider."""
        if not self.model:
            raise AgentConfigError("no model selected")
        if self.provider == "ollama":
            return OllamaModel(self.model, provider=self._provider)
        return OpenAIChatModel(self.model, provider=self._provider)

    # -- runtime settings -------------------------------------------------

    async def list_models(self) -> List[str]:
        """Return the model ids the configured endpoint offers.

        Uses the provider's own OpenAI client, so the base URL and credentials
        already in play are reused rather than reconstructed.
        """
        result = await self._provider.client.models.list()
        return sorted(m.id for m in result.data)

    def apply_settings(self, model: str, thinking: Any) -> None:
        """Switch the model and/or reasoning effort, keeping the conversation.

        Everything is validated before anything is assigned, so a rejected
        change leaves the running agent exactly as it was.
        """
        model = str(model or "").strip()
        if not model:
            raise AgentConfigError("model must not be empty")
        thinking = self._parse_thinking(thinking)

        previous = (self.model, self.thinking, self._agent)
        self.model, self.thinking = model, thinking
        try:
            self._rebuild()
        except Exception:
            # Put back exactly what was running, including "nothing yet".
            self.model, self.thinking, self._agent = previous
            raise

    # -- tools ------------------------------------------------------------

    async def list_tools(self) -> List[dict]:
        """Tool specs from every connected server, via the gateway."""
        return await self._list_tools() if self._list_tools else []

    async def list_parameters(self) -> List[str]:
        """Candidate parameter names, via the gateway. Never raises.

        Empty before a candidates file has been uploaded — which the planner
        treats as "proposal.<name> is unusable", not as an error.
        """
        if not self._list_parameters:
            return []
        try:
            return await self._list_parameters()
        except Exception:
            return []

    async def call_tool(self, server_id: str, tool: str, args: dict,
                        call_id: str = "") -> dict:
        """Run one tool through the gateway. Never raises.

        ``call_id`` is passed straight through — what the gateway does with it
        (tracking a cancellable handle) is its own concern.
        """
        if not self._call_tool:
            return {"error": "tools are not available"}
        try:
            return await self._call_tool(server_id, tool, args, call_id)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def set_tool_routes(self, routes: Dict[str, Tuple[str, str]]) -> None:
        self._routes = routes

    def route_for(self, name: str) -> Optional[Tuple[str, str]]:
        """Map a flattened tool name back to ``(server_id, tool)``."""
        return self._routes.get(name)

    def record_tool_output(self, call_id: str, output: dict) -> None:
        self._tool_outputs[call_id] = output

    def take_tool_output(self, call_id: str) -> Optional[dict]:
        """Pop the stashed output so it is streamed to the UI exactly once."""
        return self._tool_outputs.pop(call_id, None)

    # -- reports ----------------------------------------------------------

    async def summarize(self, facts: str, language: str = "en") -> str:
        """Write the prose summary for a session report. Never raises.

        Runs a throwaway agent with no tools and no history, rather than the
        conversation's own: a report must not depend on what was said in chat,
        and asking for one must not leave a trace in it either.

        ``language`` is what the summary is written in — the prompt itself stays
        English whichever way it is set.

        Returns "" when there is no model or the call fails, which the caller
        takes as "leave the summary section out".
        """
        if not facts.strip():
            return ""
        name = LANGUAGE_NAMES.get(language, LANGUAGE_NAMES["en"])
        try:
            agent = Agent(self._build_model(),
                          instructions=SUMMARY_PROMPT.format(language=name),
                          model_settings=(ModelSettings(thinking=self.thinking)
                                          if self.thinking is not None else None))
            result = await agent.run(facts)
            return str(result.output or "").strip()
        except Exception:
            return ""

    # -- workflows --------------------------------------------------------

    def emit(self, event: str, payload: Dict[str, Any]) -> None:
        """Push an event to the UI from inside a tool, mid-turn."""
        self._ui.put_nowait((event, payload))

    def take_active_workflow(self) -> Optional[str]:
        """Take the id of the run this turn launched, and forget it.

        Taking rather than reading, so a second Stop cannot cancel a run that
        has already moved on.
        """
        workflow_id, self._active_workflow = self._active_workflow, None
        return workflow_id

    async def _heartbeat(self) -> None:
        """Keep the chat stream alive while a run is blocking the turn.

        stream_reply races the UI queue, so anything emitted here leaves as an
        SSE frame straight away. The page ignores event names it does not know,
        which is the whole point — this exists for the connection, not for it.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            self.emit("ping", {})

    def _note_workflow(self, workflow_id: str) -> None:
        """Remember the run the page started, so a Stop can reach it.

        Also where a run is counted against MAX_WORKFLOWS_PER_TURN: this is
        called the moment the gateway sees the job the page started, which is
        exactly when the instruments begin doing something.
        """
        self._active_workflow = workflow_id
        self._workflow_runs += 1

    @staticmethod
    def _workflow_outcome(result: Dict[str, Any]) -> str:
        """Turn the gateway's verdict into what the model is told."""
        status = str(result.get("status") or "")
        if status == "busy":
            return ("Another workflow was already running, so the workspace was left "
                    "alone: nothing was placed and nothing was started. Tell the user "
                    "to wait for the current run to finish, or cancel it, and say "
                    "plainly that this workflow has not been set up.")
        if status == "not_started":
            return ("The workflow is in the Blockly workspace but no run ever started, "
                    "so nothing has happened and there are no results. Do not say it "
                    "is running. Tell the user what the workflow does and ask them to "
                    "press Run.")
        if status == "timeout":
            return ("The workflow is still running — I stopped waiting for it. It has "
                    "not finished and there are no results yet. Say exactly that, and "
                    "tell the user its progress is in the log on screen. Do not call "
                    "any more tools: they are refused while a workflow runs.")
        # The digest below looks the same whether the run finished or was cut
        # short part-way, so the outcome has to be stated in words. Without it a
        # canceled campaign reads as a complete one and its partial results get
        # reported as the answer.
        head = {
            "done": "The workflow ran to completion.",
            "error": "The workflow failed part-way through.",
            "canceled": "The workflow was canceled before it finished.",
        }.get(status, f"The workflow ended with status {status!r}.")
        return (f"{head} This is what actually happened:\n\n"
                f"{result.get('digest') or '(nothing was recorded)'}\n\n"
                "These are the real measured values — report them exactly as they "
                "appear, and describe the outcome as it is above rather than as "
                "success. You may now call further tools if the user's request needs "
                "them. Do not design another workflow unless the user asked for one.")

    async def _plan_workflow(self, instruction: str, usage: Any = None) -> str:
        """Write a workflow for *instruction* and put it in the Blockly workspace.

        Only the workspace is produced here. The page turns it back into an
        executable AST with the same code path a hand-built workflow uses, so
        what runs is whatever is on screen — including any edit the user makes
        before pressing Run.

        In Auto mode the page starts that run while this call is still open, and
        this waits for it: the reply is then written *after* the run, from the
        steps that actually executed rather than from a guess about what will
        happen. It is also what frees the model to keep calling tools in the
        same reply: the server refuses instrument calls while a workflow runs,
        and by the time this returns the run is over, so the refusal is not in
        force.
        """
        if self._workflow_runs >= MAX_WORKFLOWS_PER_TURN:
            return (f"You have already run {self._workflow_runs} workflows in this "
                    "reply, which is the limit. Nothing was placed and nothing was "
                    "run this time. Report what those runs produced and ask the user "
                    "before running anything else.")
        if self._workflow_attempts >= MAX_WORKFLOW_ATTEMPTS_PER_TURN:
            return (f"{self._workflow_attempts} attempts to design a workflow in this "
                    "reply have not produced a run, and nothing was placed or run "
                    "this time either. Stop here: tell the user what went wrong with "
                    "the attempts so far and ask how they want to proceed.")
        # Attempts are counted before the planner runs, so a string of invalid
        # designs is bounded even though none of them reaches the instruments.
        # Runs are counted in _note_workflow instead — see MAX_WORKFLOWS_PER_TURN.
        self._workflow_attempts += 1

        catalog = build_catalog(await self.list_tools(), await self.list_parameters())
        try:
            workflow = await write_workflow(instruction, catalog,
                                            self._build_model(), usage=usage)
        except UnexpectedModelBehavior as e:
            # No retry here: the planner already exhausted its own. Reporting
            # the failure lets the model fall back to direct tool calls.
            return f"Could not design a valid workflow ({e}). Nothing was placed or run."

        # Read once. The user can flip Auto mid-turn, but the page has already
        # been told which mode this workflow was emitted in, and re-reading it
        # after the wait would abandon a run that is genuinely under way.
        auto = self.auto_approve
        self.emit("workflow", {"xml": to_blockly_xml(workflow, catalog),
                               "auto_run": auto,
                               "summary": describe(workflow)})
        if not auto:
            return ("The workflow is now in the Blockly workspace but nothing has run. "
                    "Tell the user what it does and ask them to check the blocks — "
                    "editing them is fine — and press Run when they are ready.")
        if self._await_workflow is None:
            return ("The workflow is now in the Blockly workspace. I cannot see from "
                    "here whether it runs, so tell the user what it does and do not "
                    "claim it has started.")

        # No await between the emit above and the wait below: the gateway
        # snapshots the current job id to recognise the run the page is about to
        # start, and that snapshot has to be taken before one can appear.
        beat = asyncio.ensure_future(self._heartbeat())
        try:
            result = await self._await_workflow(on_start=self._note_workflow)
        finally:
            beat.cancel()
        # Cleared only on the way out normally, never in a finally. Whether the
        # run dies with the turn is /agent/cancel's decision — a Stop takes it
        # down, a dropped connection leaves it for reattachIfRunning — and
        # clearing this while being cancelled would race that endpoint for the
        # id it needs. A leftover id is harmless: /agent/cancel checks the job is
        # still the current one and still running.
        self._active_workflow = None
        return self._workflow_outcome(result)

    # -- approval ---------------------------------------------------------

    async def await_approval(self, call_id: str) -> bool:
        """Block until the user approves this call, or long enough to give up.

        Timing out counts as a denial: an abandoned tab must not leave a tool
        call — and the HTTP request streaming it — waiting forever.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[call_id] = fut
        try:
            return bool(await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_S))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False
        finally:
            self._pending.pop(call_id, None)

    def approve(self, call_id: str, approved: bool) -> bool:
        """Resolve a waiting tool call. False if nothing was waiting on it."""
        fut = self._pending.get(call_id)
        if fut is None or fut.done():
            return False
        fut.set_result(bool(approved))
        return True

    def request_abort(self) -> None:
        """Ask the current turn to stop as soon as it can.

        Anything waiting on approval is released as *denied*, so a stop never
        leaves a tool running that the user never agreed to.
        """
        self._abort.set()
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(False)

    # -- chat -------------------------------------------------------------

    def reset(self) -> None:
        """Forget the conversation so the next turn starts fresh."""
        self.messages = []
        self._tool_outputs.clear()
        self._drain_ui()

    def _drain_ui(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Take everything queued for the UI, leaving the queue empty."""
        out = []
        while not self._ui.empty():
            out.append(self._ui.get_nowait())
        return out

    def history(self) -> List[dict]:
        """Project the conversation into items the chat log can redraw.

        The parts are flattened into the same vocabulary the live stream uses,
        so the UI has one renderer for both. Tool images are not included —
        only the data ever reached the model, which is all history holds.
        """
        items: List[dict] = []
        bubble: Optional[dict] = None

        def flush() -> None:
            nonlocal bubble
            if bubble and (bubble["text"] or bubble["thinking"]):
                items.append(bubble)
            bubble = None

        def stamp(value: Any) -> Optional[float]:
            try:
                return value.timestamp()
            except Exception:
                return None

        for msg in self.messages:
            if isinstance(msg, ModelRequest):
                flush()
                for part in msg.parts:
                    if isinstance(part, UserPromptPart):
                        content = part.content
                        text = content if isinstance(content, str) else " ".join(
                            str(c) for c in content if isinstance(c, str))
                        items.append({"kind": "user", "text": text,
                                      "time": stamp(part.timestamp)})
                    elif isinstance(part, ToolReturnPart):
                        items.append({"kind": "tool_output",
                                      "call_id": part.tool_call_id,
                                      "output": {"data": part.content}})
            elif isinstance(msg, ModelResponse):
                model = getattr(msg, "model_name", "") or ""
                when = stamp(getattr(msg, "timestamp", None))
                for part in msg.parts:
                    if isinstance(part, (ThinkingPart, TextPart)):
                        if bubble is None:
                            bubble = {"kind": "agent", "thinking": "", "text": "",
                                      "model": model, "time": when}
                        key = "thinking" if isinstance(part, ThinkingPart) else "text"
                        bubble[key] += part.content or ""
                    elif isinstance(part, ToolCallPart):
                        # A tool card breaks the bubble, exactly as it does live.
                        flush()
                        server_id, tool = (self.route_for(part.tool_name)
                                           or ("", part.tool_name))
                        try:
                            args = part.args_as_dict()
                        except Exception:
                            args = {}
                        items.append({"kind": "tool_call",
                                      "call_id": part.tool_call_id,
                                      "server_id": server_id, "tool": tool,
                                      "args": args, "time": when})
        flush()
        return items

    async def stream_reply(self, prompt: str) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
        """Stream one turn, yielding ``(event, payload)`` pairs.

        Emits ``("thinking", {"text": ...})`` while a reasoning model works
        through the problem and ``("delta", {"text": ...})`` for the answer
        itself, then ``("done", {})``. Models without a reasoning phase simply
        never emit a thinking event. A stop ends the turn with ``("canceled", {})``.

        History is only committed once the stream completes, so a failed or
        abandoned turn leaves the conversation untouched.
        """
        if self._agent is None:
            raise AgentConfigError("no model selected — pick one from the Model list")
        self._abort.clear()   # never carry a previous turn's stop over
        self._drain_ui()      # nor a previous turn's unsent UI events
        self._workflow_runs = 0        # both guards are per reply
        self._workflow_attempts = 0
        async with self._agent.run_stream_events(
            prompt, message_history=self.messages
        ) as events:
            iterator = events.__aiter__()
            nxt: Optional[asyncio.Future] = None
            while True:
                # Waiting on the next event alone would make a stop land only
                # once one arrives — useless during a long tool call or while
                # the model is still working up to its first token. A tool's own
                # UI events are raced the same way, so they reach the page while
                # the model is still writing rather than after the turn.
                if nxt is None:
                    nxt = asyncio.ensure_future(iterator.__anext__())
                stop = asyncio.ensure_future(self._abort.wait())
                ui = asyncio.ensure_future(self._ui.get())
                done, _ = await asyncio.wait({nxt, stop, ui},
                                             return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    nxt.cancel()
                    ui.cancel()
                    # Leaving the async with tears the run down, which cancels
                    # whatever tool coroutine was in flight.
                    yield "canceled", {}
                    return
                stop.cancel()
                if ui in done:
                    # nxt is deliberately left running — the model event it is
                    # waiting for has not been consumed.
                    yield ui.result()
                    continue
                ui.cancel()
                try:
                    ev = nxt.result()
                except StopAsyncIteration:
                    break
                finally:
                    nxt = None

                # A part's first token arrives on the start event, not as a delta.
                if isinstance(ev, PartStartEvent):
                    text = getattr(ev.part, "content", "") or ""
                    if not text:
                        continue
                    if isinstance(ev.part, ThinkingPart):
                        yield "thinking", {"text": text}
                    elif isinstance(ev.part, TextPart):
                        yield "delta", {"text": text}
                elif isinstance(ev, PartDeltaEvent):
                    text = getattr(ev.delta, "content_delta", "") or ""
                    if not text:
                        continue
                    if isinstance(ev.delta, ThinkingPartDelta):
                        yield "thinking", {"text": text}
                    elif isinstance(ev.delta, TextPartDelta):
                        yield "delta", {"text": text}
                elif isinstance(ev, FunctionToolCallEvent):
                    # Arrives while the tool runs, so an approval prompt still
                    # reaches the client before call_tool() stops waiting.
                    route = self.route_for(ev.part.tool_name)
                    server_id, tool = route or ("", ev.part.tool_name)
                    try:
                        args = ev.part.args_as_dict()
                    except Exception:
                        args = {}
                    yield "tool_call", {
                        "call_id": ev.part.tool_call_id,
                        "server_id": server_id,
                        "tool": tool,
                        "args": args,
                        # Only MCP tools are gated: they are the ones that reach
                        # the instruments, and McpToolset.call_tool is what waits
                        # for the answer. An unrouted name is one of our own
                        # tools, which would leave the buttons doing nothing.
                        "needs_approval": not self.auto_approve and route is not None,
                    }
                elif isinstance(ev, FunctionToolResultEvent):
                    call_id = getattr(ev.part, "tool_call_id", "") or ""
                    output = self.take_tool_output(call_id)
                    if output is None:
                        output = {"data": getattr(ev.part, "content", None)}
                    yield "tool_output", {"call_id": call_id, "output": output}
                elif isinstance(ev, AgentRunResultEvent):
                    self.messages = ev.result.all_messages()
        # A tool that emitted on its way out can finish after the last model
        # event, so flush before the turn is declared over.
        for event in self._drain_ui():
            yield event
        yield "done", {}
