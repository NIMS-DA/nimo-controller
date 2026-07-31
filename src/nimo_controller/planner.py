"""Workflow planner — turns one instruction into a Blockly workspace.

The chat agent can call a tool at a time, which is enough for "move the stage to
slot 3" but cannot express a campaign: a loop, a counter, or feeding a measured
value back into nimo's optimization history. Those concepts only exist as Blockly
blocks, so a campaign has to *become* a Blockly workspace.

This module does exactly that, in three steps:

1. ``build_catalog`` — what the workspace may contain, read from the connected
   servers and filtered down to what the toolbox actually offers.
2. a small pydantic ``Workflow`` model the LLM fills in, checked by ``validate``.
3. ``to_blockly_xml`` — the workspace XML the front end loads.

It deliberately stops there. The executable AST is *not* generated here: the
front end re-exports it from the live workspace, so what runs is always what the
user can see (and edit) on screen.

Nothing here imports ``server.py``; the agent passes the tool list and the
candidate parameter names in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, PromptedOutput, RunContext
from pydantic_ai.usage import RunUsage

BUILTIN_SERVER = "nimo"

# nimo tools that get no generic block, because the toolbox gives them a
# dedicated one or hides them entirely. Mirrors NIMO_HARDCODED in frontend.js.
NIMO_HARDCODED = frozenset({
    "selection", "maximization", "minimization", "update",
    "get_parameter_names", "get_proposal", "reinitialize",
})

# nimo tool -> the Blockly block that calls it. Mirrors NIMO_METHOD_TOOLS in
# frontend.js, reversed.
NIMO_METHOD_BLOCKS = {
    "selection": "nimo_selection",
    "maximization": "nimo_maximization",
    "minimization": "nimo_minimization",
}
_METHOD_BLOCK_TYPES = frozenset(NIMO_METHOD_BLOCKS.values())

# The dropdown on those three blocks. Named by hand in frontend.js, so it does
# not follow the "field name is the schema property" rule generated blocks obey.
METHOD_FIELD = "METHOD"

PROPOSAL_PREFIX = "proposal."
COUNTER_PREFIX = "counter."

# Blockly's FieldNumber bounds on repeat_n_with_index's TIMES field.
MIN_TIMES, MAX_TIMES = 1, 1000


def safe_slug(text: str) -> str:
    """Reduce a name to what a Blockly block type may contain.

    Must stay byte-identical to ``safeSlug`` in frontend.js: whitespace collapses
    to ``_`` first, and ``-`` survives the character filter. (``agent.py``'s
    ``_slug`` is a different function — it is for tool identifiers, not block
    types, and it does neither of those things.)
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", re.sub(r"\s+", "_", str(text).strip()))


def tool_stmt_type(server: str, name: str) -> str:
    """Block type of a generic tool block."""
    return f"mcp__{safe_slug(server)}__{safe_slug(name)}__stmt"


def nimo_var_type(name: str) -> str:
    """Block type of the value block that stands for ``proposal.<name>``."""
    return f"nimo_var__{safe_slug(name)}"


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """One argument of a tool, as the toolbox renders it."""

    name: str
    type: str = "string"
    required: bool = False
    enum: Optional[tuple[str, ...]] = None
    default: Any = None
    description: str = ""

    @property
    def is_field(self) -> bool:
        """True when Blockly draws this as a dropdown rather than a value input.

        A field has no socket, so nothing can be plugged into it — only a literal
        from the enum will do.
        """
        return bool(self.enum)

    def render(self) -> str:
        kind = "|".join(self.enum) if self.enum else self.type
        return f"{self.name}{'' if self.required else '?'}: {kind}"


@dataclass(frozen=True)
class ToolSpec:
    """One tool the workflow may call, and the block that calls it."""

    server: str
    name: str
    block_type: str
    description: str = ""
    params: tuple[ParamSpec, ...] = ()
    returns_number: bool = False

    @property
    def is_method_block(self) -> bool:
        """True for the three dedicated nimo proposal blocks.

        They are hand-written in frontend.js rather than generated from the
        schema, so their dropdown is named METHOD — not "method", the schema
        property it fills in.
        """
        return self.block_type in _METHOD_BLOCK_TYPES

    def param(self, name: str) -> Optional[ParamSpec]:
        for p in self.params:
            if p.name == name:
                return p
        return None

    def render(self) -> str:
        args = ", ".join(p.render() for p in self.params) or "-"
        head = self.description.strip().splitlines()[0] if self.description.strip() else ""
        ret = " -> number" if self.returns_number else ""
        return f"  {self.name}({args}){ret}{' - ' + head if head else ''}"


@dataclass
class Catalog:
    """Everything a generated workflow is allowed to mention."""

    servers: dict[str, tuple[ToolSpec, ...]] = field(default_factory=dict)
    parameters: tuple[str, ...] = ()
    """Candidate parameter names, usable as ``proposal.<name>``."""

    def tool(self, server: str, name: str) -> Optional[ToolSpec]:
        for spec in self.servers.get(server, ()):
            if spec.name == name:
                return spec
        return None

    def render(self) -> str:
        blocks = []
        for server, tools in self.servers.items():
            kind = "built-in" if server == BUILTIN_SERVER else "mcp"
            blocks.append("\n".join([f'server "{server}" ({kind}):',
                                     *(t.render() for t in tools)]))
        return "\n\n".join(blocks) or "(no tools available)"


_JSON_TYPES = ("integer", "number", "boolean", "string")


def _enum_values(schema: dict, prop: dict) -> Optional[tuple[str, ...]]:
    """Allowed values of one property, following a ``$ref`` into ``$defs``.

    FastMCP renders a Python Enum as a ``$ref``, so the inline ``enum`` case
    almost never fires — but both are handled, exactly as extractMethodEnum()
    does in frontend.js.
    """
    values = prop.get("enum")
    if isinstance(values, list) and values:
        return tuple(str(v) for v in values)
    ref = prop.get("$ref")
    if isinstance(ref, str):
        target = (schema.get("$defs") or {}).get(ref.rsplit("/", 1)[-1]) or {}
        values = target.get("enum")
        if isinstance(values, list) and values:
            return tuple(str(v) for v in values)
    return None


def _param_specs(input_schema: Optional[dict]) -> tuple[ParamSpec, ...]:
    """Flatten an MCP inputSchema into ParamSpecs, keeping declaration order.

    Order matters: the toolbox builds a block's fields and inputs by walking
    ``properties`` in order, so the rendered XML should follow the same order to
    stay readable next to a hand-built block.
    """
    schema = input_schema or {}
    required = set(schema.get("required") or ())
    specs = []
    for name, prop in (schema.get("properties") or {}).items():
        prop = prop if isinstance(prop, dict) else {}
        enum = _enum_values(schema, prop)
        declared = prop.get("type")
        specs.append(ParamSpec(
            name=name,
            type=declared if declared in _JSON_TYPES else ("string" if enum else (declared or "string")),
            required=name in required,
            enum=enum,
            default=prop.get("default"),
            description=str(prop.get("description") or ""),
        ))
    return tuple(specs)


def returns_number(output_schema: Optional[dict]) -> bool:
    """True when a tool returns a single int/float, so ``update`` can use it.

    Same rule as toolReturnsNumber() in frontend.js — including the FastMCP
    wrapper shape — because the two must agree on which blocks may go inside an
    update block.
    """
    schema = output_schema or {}
    if not isinstance(schema, dict):
        return False
    if schema.get("type") in ("number", "integer"):
        return True
    if schema.get("type") == "object" and schema.get("x-fastmcp-wrap-result") is True:
        result = (schema.get("properties") or {}).get("result") or {}
        return isinstance(result, dict) and result.get("type") in ("number", "integer")
    return False


def build_catalog(tools: list[dict], parameters: list[str]) -> Catalog:
    """Build the catalog from ``/tools`` specs and the candidate parameter names.

    Only tools the toolbox actually offers a block for are kept, so the planner
    cannot produce a workspace the front end would fail to load.
    """
    servers: dict[str, list[ToolSpec]] = {}
    for spec in tools:
        server = str(spec.get("server_id") or "")
        name = str(spec.get("name") or "")
        if not server or not name:
            continue
        schema = spec.get("input_schema") or {}
        if server == BUILTIN_SERVER and name in NIMO_HARDCODED:
            block = NIMO_METHOD_BLOCKS.get(name)
            if block is None:
                # update is the <update> block; the rest are not on the toolbox.
                continue
        else:
            block = tool_stmt_type(server, name)
        servers.setdefault(server, []).append(ToolSpec(
            server=server,
            name=name,
            block_type=block,
            description=str(spec.get("description") or ""),
            params=_param_specs(schema),
            returns_number=returns_number(spec.get("output_schema")),
        ))
    return Catalog(
        servers={k: tuple(v) for k, v in servers.items()},
        parameters=tuple(str(p) for p in parameters),
    )


# ---------------------------------------------------------------------------
# Structured output models — one per Blockly block
# ---------------------------------------------------------------------------
#
# Argument values are plain strings throughout. XML attributes are strings
# anyway, and so are the "proposal.<x>" / "counter.<x>" reference forms, so one
# type keeps the JSON schema the model has to fill in as small as possible.
# Type-checking against the tool schema happens in validate().


class ToolCall(BaseModel):
    """One tool invocation."""

    kind: Literal["tool"] = "tool"
    server: str = Field(description="Server name, e.g. 'nimo'.")
    name: str = Field(description="Tool name as listed for that server.")
    arguments: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Tool arguments. A value is either a literal (e.g. '0.5', 'PHYSBO'), "
            "'proposal.<parameter>' for a value proposed by nimo, or "
            "'counter.<name>' for the counter of an enclosing repeat block."
        ),
    )


class Update(BaseModel):
    """Runs one tool and feeds its numeric return to nimo as the objective."""

    kind: Literal["update"] = "update"
    tool: ToolCall = Field(description="Must be a tool that returns an int or float.")


class Repeat(BaseModel):
    """A fixed-count loop."""

    kind: Literal["repeat"] = "repeat"
    times: int = Field(ge=MIN_TIMES, le=MAX_TIMES, description="Number of iterations.")
    counter: str = Field(
        default="i",
        min_length=1,
        description="Name of the loop counter, referenced as 'counter.<name>'.",
    )
    body: list["Block"] = Field(default_factory=list)


class If(BaseModel):
    """The only condition available: a loop counter against an integer."""

    kind: Literal["if"] = "if"
    counter: str = Field(description="Counter of an enclosing repeat block.")
    op: Literal["==", "<", ">", "<=", ">="]
    value: int
    then: list["Block"] = Field(default_factory=list)
    otherwise: list["Block"] = Field(default_factory=list, description="The else branch.")


Block = Annotated[Union[ToolCall, Update, Repeat, If], Field(discriminator="kind")]


class Workflow(BaseModel):
    """A sequence of blocks — one stack in the Blockly workspace."""

    body: list[Block] = Field(default_factory=list)


Repeat.model_rebuild()
If.model_rebuild()
Workflow.model_rebuild()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
#
# Everything here is decidable without running anything. Two families of rule:
# the workflow must make sense (tools exist, references are in scope), and it
# must be *expressible in Blockly* — an undefined block type makes
# domToWorkspace throw, and loadWorkspaceXml then clears the workspace, so an
# unexpressible workflow reaches the user as an empty screen.


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _reference(value: str) -> Optional[tuple[str, str]]:
    """Split a ``proposal.x`` / ``counter.x`` reference into (kind, name)."""
    if value.startswith(PROPOSAL_PREFIX):
        return "proposal", value[len(PROPOSAL_PREFIX):]
    if value.startswith(COUNTER_PREFIX):
        return "counter", value[len(COUNTER_PREFIX):]
    return None


def _check_value(where: str, param: ParamSpec, value: str, catalog: Catalog,
                 counters: set[str]) -> list[str]:
    """Check one argument value: a reference, or a literal of the right type."""
    ref = _reference(value)
    if ref is not None:
        kind, name = ref
        if param.is_field:
            return [f"{where}: '{param.name}' is a dropdown with fixed choices "
                    f"({'|'.join(param.enum or ())}), so it cannot take {value!r}. "
                    "Give it one of the listed values."]
        if kind == "proposal":
            if not catalog.parameters:
                return [f"{where}: no candidate parameters are declared yet, so "
                        "proposal.<parameter> cannot be used. The user needs to "
                        "upload a candidates file first."]
            if name not in catalog.parameters:
                return [f"{where}: unknown proposal parameter '{name}'. "
                        f"Declared parameters: {', '.join(catalog.parameters)}"]
            return []
        if name not in counters:
            known = ", ".join(sorted(counters)) or "(none)"
            return [f"{where}: counter '{name}' is not in scope here. In scope: {known}"]
        return []
    if param.enum and value not in param.enum:
        return [f"{where}: '{value}' is not one of {'|'.join(param.enum)}"]
    if param.type in ("integer", "number") and not _is_number(value):
        return [f"{where}: '{value}' is not a {param.type}"]
    if param.type == "boolean" and value not in ("true", "false"):
        return [f"{where}: '{value}' is not a boolean (use 'true' or 'false')"]
    return []


def _check_tool(node: ToolCall, catalog: Catalog,
                counters: set[str]) -> tuple[list[str], Optional[ToolSpec]]:
    where = f'<tool server="{node.server}" name="{node.name}">'
    if node.server not in catalog.servers:
        known = ", ".join(catalog.servers) or "(none)"
        return [f"{where}: unknown server '{node.server}'. Available servers: {known}"], None

    spec = catalog.tool(node.server, node.name)
    if spec is None:
        known = ", ".join(t.name for t in catalog.servers[node.server]) or "(none)"
        return [f"{where}: server '{node.server}' has no usable tool '{node.name}'. "
                f"Its tools: {known}"], None

    errors: list[str] = []
    params = {p.name: p for p in spec.params}
    for key, value in node.arguments.items():
        param = params.get(key)
        if param is None:
            known = ", ".join(params) or "(none)"
            errors.append(f"{where}: unknown argument '{key}'. Accepted arguments: {known}")
            continue
        errors += _check_value(where, param, value, catalog, counters)
    for name, param in params.items():
        if param.required and name not in node.arguments:
            errors.append(f"{where}: required argument '{name}' is missing")
    return errors, spec


def _check_blocks(nodes: list[Block], catalog: Catalog, counters: set[str]) -> list[str]:
    errors: list[str] = []
    for node in nodes:
        if isinstance(node, ToolCall):
            errors += _check_tool(node, catalog, counters)[0]
        elif isinstance(node, Update):
            tool_errors, spec = _check_tool(node.tool, catalog, counters)
            errors += tool_errors
            if spec is not None and not spec.returns_number:
                errors.append(
                    f"<update>: '{node.tool.server}.{node.tool.name}' does not return "
                    "an int or float, so its result cannot become an objective value. "
                    "Put a numeric measurement tool inside <update> instead"
                )
        elif isinstance(node, Repeat):
            errors += _check_blocks(node.body, catalog, counters | {node.counter})
        elif isinstance(node, If):
            if node.counter not in counters:
                known = ", ".join(sorted(counters)) or "(none)"
                errors.append(f'<if counter="{node.counter}">: counter is not in scope '
                              f"here. In scope: {known}")
            errors += _check_blocks(node.then, catalog, counters)
            errors += _check_blocks(node.otherwise, catalog, counters)
    return errors


def _uses(nodes: list[Block], predicate) -> bool:
    """True when any tool call anywhere in the tree satisfies *predicate*."""
    for node in nodes:
        if isinstance(node, ToolCall) and predicate(node):
            return True
        if isinstance(node, Update) and predicate(node.tool):
            return True
        if isinstance(node, Repeat) and _uses(node.body, predicate):
            return True
        if isinstance(node, If) and (_uses(node.then, predicate)
                                     or _uses(node.otherwise, predicate)):
            return True
    return False


def validate(workflow: Workflow, catalog: Catalog) -> list[str]:
    """Return a list of human-readable problems; empty means the workflow is valid."""
    if not workflow.body:
        return ["the workflow is empty - it must contain at least one block"]
    errors = _check_blocks(workflow.body, catalog, set())

    # A proposal nobody consumes means the measurement runs at hard-coded
    # conditions while the optimizer talks to itself. It validates field by
    # field, so only this whole-workflow check catches it.
    proposes = _uses(workflow.body,
                     lambda t: t.server == BUILTIN_SERVER and t.name in NIMO_METHOD_BLOCKS)
    consumes = _uses(workflow.body,
                     lambda t: any(v.startswith(PROPOSAL_PREFIX) for v in t.arguments.values()))
    if proposes and not consumes:
        errors.append(
            "nimo proposes parameters but no tool uses proposal.<parameter>, so the "
            "proposal is discarded. Pass it to the measurement tool instead of literals"
        )
    return errors


# ---------------------------------------------------------------------------
# Blockly workspace XML
# ---------------------------------------------------------------------------
#
# The output is what Blockly.Xml.workspaceToDom would have produced for the same
# blocks, so the front end can load it with loadWorkspaceXml() and then export
# the executable AST from it exactly as if the user had dragged it together.
#
# Block ids are deliberately not emitted: the AST is built from the live
# workspace, so Blockly can number the blocks itself and there is no id to keep
# in sync.

_INDENT = "  "


def _xa(value: object) -> str:
    """Escape a value for use inside an XML attribute."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _xt(value: object) -> str:
    """Escape a value for use as element text (field values live here)."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _field(name: str, value: object, depth: int) -> str:
    return f'{_INDENT * depth}<field name="{_xa(name)}">{_xt(value)}</field>'


def _shadow_lines(param: ParamSpec, depth: int) -> list[str]:
    """The default shadow for a value input, as makeShadowXml() builds it."""
    pad = _INDENT * depth
    if param.type in ("number", "integer"):
        default = param.default if isinstance(param.default, (int, float)) \
            and not isinstance(param.default, bool) else 0
        return [f'{pad}<shadow type="math_number">',
                _field("NUM", default, depth + 1),
                f"{pad}</shadow>"]
    if param.type == "boolean":
        return [f'{pad}<shadow type="logic_boolean">',
                _field("BOOL", "TRUE" if param.default is True else "FALSE", depth + 1),
                f"{pad}</shadow>"]
    default = param.default if isinstance(param.default, str) else ""
    return [f'{pad}<shadow type="text">',
            _field("TEXT", default, depth + 1),
            f"{pad}</shadow>"]


def _value_block_lines(param: ParamSpec, value: str, depth: int) -> list[str]:
    """The real block that fills a value input: a reference, or a literal."""
    pad = _INDENT * depth
    ref = _reference(value)
    if ref is not None:
        kind, name = ref
        if kind == "proposal":
            return [f'{pad}<block type="{_xa(nimo_var_type(name))}">',
                    _field("VARNAME", name, depth + 1),
                    f"{pad}</block>"]
        return [f'{pad}<block type="loop_counter_ref">',
                _field("COUNTER_VAR", name, depth + 1),
                f"{pad}</block>"]
    if param.type in ("number", "integer"):
        return [f'{pad}<block type="math_number">',
                _field("NUM", value, depth + 1),
                f"{pad}</block>"]
    if param.type == "boolean":
        return [f'{pad}<block type="logic_boolean">',
                _field("BOOL", "TRUE" if value == "true" else "FALSE", depth + 1),
                f"{pad}</block>"]
    return [f'{pad}<block type="text">',
            _field("TEXT", value, depth + 1),
            f"{pad}</block>"]


def _tool_body_lines(node: ToolCall, spec: ToolSpec, depth: int) -> list[str]:
    """Fields and value inputs of one tool block, in schema order.

    Every value input gets its default shadow whether or not the workflow set
    the argument, so the block looks like one dragged out of the toolbox — and
    an argument left out still exports its default.
    """
    lines: list[str] = []
    for param in spec.params:
        value = node.arguments.get(param.name)
        if param.is_field:
            name = METHOD_FIELD if spec.is_method_block else param.name
            lines.append(_field(name, value if value is not None
                                else (param.enum[0] if param.enum else ""), depth))
            continue
        lines.append(f'{_INDENT * depth}<value name="{_xa(param.name)}">')
        lines += _shadow_lines(param, depth + 1)
        if value is not None:
            lines += _value_block_lines(param, value, depth + 1)
        lines.append(f"{_INDENT * depth}</value>")
    return lines


def _statement_lines(name: str, body: list[Block], catalog: Catalog, depth: int) -> list[str]:
    """A <statement> input. Omitted entirely when the branch is empty."""
    if not body:
        return []
    pad = _INDENT * depth
    return [f'{pad}<statement name="{_xa(name)}">',
            *_stack_lines(body, catalog, depth + 1),
            f"{pad}</statement>"]


def _block_lines(node: Block, rest: list[Block], catalog: Catalog, depth: int,
                 attrs: str = "") -> list[str]:
    """One block plus, nested inside its <next>, everything that follows it."""
    pad = _INDENT * depth
    inner: list[str] = []

    if isinstance(node, Repeat):
        block_type = "repeat_n_with_index"
        inner += [_field("TIMES", node.times, depth + 1),
                  _field("COUNTER_VAR", node.counter, depth + 1)]
        inner += _statement_lines("DO", node.body, catalog, depth + 1)
    elif isinstance(node, If):
        block_type = "if_counter"
        inner += [_field("COUNTER_VAR", node.counter, depth + 1),
                  _field("OP", node.op, depth + 1),
                  _field("VALUE", node.value, depth + 1)]
        inner += _statement_lines("THEN", node.then, catalog, depth + 1)
        inner += _statement_lines("ELSE", node.otherwise, catalog, depth + 1)
    elif isinstance(node, Update):
        block_type = "nimo_update"
        inner += _statement_lines("BODY", [node.tool], catalog, depth + 1)
    elif isinstance(node, ToolCall):
        spec = catalog.tool(node.server, node.name)
        if spec is None:                       # validate() rejects this first
            raise ValueError(f"no block for {node.server}.{node.name}")
        block_type = spec.block_type
        inner += _tool_body_lines(node, spec, depth + 1)
    else:
        raise TypeError(f"unknown block: {node!r}")

    if rest:
        inner.append(f"{_INDENT * (depth + 1)}<next>")
        inner += _block_lines(rest[0], rest[1:], catalog, depth + 2)
        inner.append(f"{_INDENT * (depth + 1)}</next>")

    open_tag = f'{pad}<block type="{_xa(block_type)}"{attrs}>'
    if not inner:
        return [f'{pad}<block type="{_xa(block_type)}"{attrs}/>']
    return [open_tag, *inner, f"{pad}</block>"]


def _stack_lines(body: list[Block], catalog: Catalog, depth: int,
                 attrs: str = "") -> list[str]:
    if not body:
        return []
    return _block_lines(body[0], body[1:], catalog, depth, attrs)


def to_blockly_xml(workflow: Workflow, catalog: Catalog) -> str:
    """Render a workflow as Blockly workspace XML.

    The whole workflow becomes one stack, so exportWorkflowAst() — which sorts
    top blocks by position and concatenates their chains — reads it back in the
    same order it was written.
    """
    lines = ['<xml xmlns="https://developers.google.com/blockly/xml">']
    lines += _stack_lines(workflow.body, catalog, 1, attrs=' x="20" y="20"')
    lines.append("</xml>")
    return "\n".join(lines)


def describe(workflow: Workflow) -> str:
    """A one-line summary of a workflow, for the chat log."""
    def walk(nodes: list[Block]) -> tuple[int, int]:
        tools = loops = 0
        for node in nodes:
            if isinstance(node, ToolCall):
                tools += 1
            elif isinstance(node, Update):
                tools += 1
            elif isinstance(node, Repeat):
                loops += 1
                t, l = walk(node.body)
                tools, loops = tools + t, loops + l
            elif isinstance(node, If):
                t, l = walk(node.then + node.otherwise)
                tools, loops = tools + t, loops + l
        return tools, loops

    tools, loops = walk(workflow.body)
    parts = [f"{tools} tool block(s)"]
    if loops:
        parts.append(f"{loops} loop(s)")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# The planner agent
# ---------------------------------------------------------------------------

INSTRUCTIONS = """\
You design closed-loop materials-experiment workflows for the NIMO Controller
and return them as a structured workflow, never as prose or XML.

A workflow is a sequence of blocks:

* tool     - call one tool of one server.
* update   - call one tool that returns an int or float; its return value is
             fed to nimo as the objective value of the current proposal, which
             advances the optimization. Use this for the measurement step.
* repeat   - run a body `times` times (1 to 1000); `counter` names the loop
             counter and is always required.
* if       - the ONLY condition available is a comparison between a loop
             counter and an integer (==, <, >, <=, >=). Conditions on measured
             values cannot be expressed.

Argument values are strings and take one of three forms:

* a literal, e.g. "0.5" or "PHYSBO";
* "proposal.<parameter>" - a parameter value chosen by the most recent nimo
  proposal. Only the declared parameters listed below may be used;
* "counter.<name>" - the counter of an enclosing repeat block.

Rules:

* nimo proposes the next experiment in one of three ways: `selection` for
  algorithms with no direction to optimize toward, `maximization` for a larger
  objective, `minimization` for a smaller one. Pick the one the user asked for.
* A typical optimization loop repeats: a nimo proposal, then a measurement tool
  wrapped in update, receiving proposal.<parameter> as its arguments. Emit the
  nimo proposal before any use of proposal.<parameter>.
* The controller reinitializes nimo automatically at workflow start; never emit
  a reinitialize step. nimo's update tool is not called directly either -
  express it with the update block.
* An argument whose allowed values are listed as a|b|c is a dropdown: it takes
  one of those literals and nothing else, never proposal.* or counter.*.
* Only use servers, tools and argument names that appear in the catalog below.
* Do not invent parameters; the declared parameters are fixed.

Example of a ten-cycle PHYSBO loop measuring with a tool named get_phase:

{"body": [
  {"kind": "repeat", "times": 10, "counter": "i", "body": [
    {"kind": "tool", "server": "nimo", "name": "maximization",
     "arguments": {"method": "PHYSBO"}},
    {"kind": "update", "tool": {
      "kind": "tool", "server": "sdl", "name": "get_phase",
      "arguments": {"temperature": "proposal.temperature",
                    "pressure": "proposal.pressure"}}}
  ]}
]}
"""


def build_planner_agent(model: Any) -> Agent:
    """Build the sub-agent that writes workflows, on top of *model*."""
    agent = Agent(
        model,
        deps_type=Catalog,
        # Prompted output, not the default tool output: the recursive block union
        # does not survive the trip through Ollama's tool definitions - the model
        # only sees "body: any[]" and guesses. PromptedOutput puts the full JSON
        # schema, $defs and all, into the instructions.
        output_type=PromptedOutput(Workflow),
        instructions=INSTRUCTIONS,
        retries=3,
    )

    @agent.instructions
    def catalog_instructions(ctx: RunContext[Catalog]) -> str:
        parameters = ", ".join(ctx.deps.parameters) or "(none declared)"
        return (f"Declared candidate parameters (usable as proposal.<name>): "
                f"{parameters}\n\nTool catalog:\n\n{ctx.deps.render()}")

    @agent.output_validator
    def check(ctx: RunContext[Catalog], workflow: Workflow) -> Workflow:
        errors = validate(workflow, ctx.deps)
        if errors:
            raise ModelRetry("Fix these problems and return the workflow again:\n- "
                             + "\n- ".join(errors))
        return workflow

    return agent


async def write_workflow(instruction: str, catalog: Catalog, model: Any,
                         usage: Optional[RunUsage] = None) -> Workflow:
    """Turn a natural-language instruction into a validated workflow.

    ``usage`` is passed through so the tokens this sub-agent spends are counted
    against the conversation that delegated to it, rather than vanishing.
    """
    result = await build_planner_agent(model).run(instruction, deps=catalog, usage=usage)
    return result.output
