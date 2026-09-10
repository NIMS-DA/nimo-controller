"""Spec-prompted XML planner — the comparison method for run_eval.

Instead of pydantic structured output, the LLM is shown a textual spec of the
nimo-workflow XML dialect and writes the <workflow> fragment directly, in the
style of xdl-generation / CLAIRify (https://github.com/ac-rad/xdl-generation):
generate, verify against the spec, feed the errors back, repeat. The reply is
parsed into the same pydantic Workflow model and checked by the same
validate() as the structured planner, with the same 3 retries — only the
output representation differs, so evaluation compares representations, not
validators.
"""

import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

from pydantic import ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.usage import RunUsage

from generate_xml import cli_main, generate_with
from nimo_controller.planner import (Catalog, If, Repeat, ToolCall, Update,
                                     Workflow, normalize, validate)

XML_INSTRUCTIONS = """\
You design closed-loop materials-experiment workflows for the NIMO Controller
and return them as ONE XML fragment: a single <workflow> element, with no
prose, no code fences and nothing outside it.

Elements:

* <repeat counter="i" times="10">...</repeat> - run the body `times` times
  (1 to 1000); `counter` is always required. The counter is 0-based: it is 0
  on the first round and times-1 on the last, so "the first 5 rounds" is
  counter &lt; 5.
* <if counter="i" op="&lt;" value="5"><then>...</then><else>...</else></if> -
  the ONLY condition available is a comparison between a loop counter and an
  integer (op is one of == &lt; &gt; &lt;= &gt;=, XML-escaped). Conditions on
  measured values cannot be expressed. <else> may be omitted.
* <nimo tool="selection" method="RE"/> - nimo proposes the next experiment.
  RE, ES (exhaustive search), DOE, BLOX and PDC search without a direction to
  optimize toward - never give them a "minimization" attribute. Only PHYSBO
  optimizes the objective: set "minimization" to "true" to optimize toward a
  smaller objective, "false" (or omit it) for a larger one. PTR
  targets an objective range: set "ptr_lower" / "ptr_upper" to its bounds
  (numbers; omit a bound to leave that side unconstrained).
* <nimo tool="update"><call .../></nimo> - the measurement step: runs the one
  wrapped call and feeds its numeric return to nimo as the objective value of
  the current proposal, which advances the optimization. The wrapped tool
  must return an int or float.
* <call server="sdl" tool="get_phase" temperature="proposal.temperature"/> -
  call one tool of one MCP server; arguments are attributes.
* Other nimo built-ins are called as <nimo tool="<name>" .../> with their
  arguments as attributes; plot_history_best takes the same minimization
  flag convention as selection instead of a mode argument.

Argument values are strings and take one of three forms:

* a literal, e.g. "0.5" or "PHYSBO";
* "proposal.<parameter>" - a parameter value chosen by the most recent nimo
  proposal. Only the declared parameters listed below may be used;
* "counter.<name>" - the counter of an enclosing repeat block.

Rules:

* A typical optimization loop repeats: a nimo proposal, then a measurement
  call wrapped in <nimo tool="update">, receiving proposal.<parameter> as its
  arguments. Emit the nimo proposal before any use of proposal.<parameter>.
* The controller reinitializes nimo automatically at workflow start; never
  emit a reinitialize step, and never call nimo's update tool with arguments -
  express it only as the wrapping element.
* An argument whose allowed values are listed as a|b|c is a dropdown: it
  takes one of those literals and nothing else, never proposal.* or
  counter.*.
* Only use servers, tools and argument names that appear in the catalog below.
* Do not invent parameters; the declared parameters are fixed.

Example of a ten-cycle PHYSBO loop measuring with a tool named get_phase,
followed by one more block once the loop is over:

<workflow>
  <repeat counter="i" times="10">
    <nimo tool="selection" method="PHYSBO" minimization="false"/>
    <nimo tool="update">
      <call server="sdl" tool="get_phase" temperature="proposal.temperature" pressure="proposal.pressure"/>
    </nimo>
  </repeat>
  <call server="sdl" tool="shutdown"/>
</workflow>

Example of a twenty-cycle run whose first five cycles use RE and whose
remaining fifteen use PHYSBO:

<workflow>
  <repeat counter="i" times="20">
    <if counter="i" op="&lt;" value="5">
      <then><nimo tool="selection" method="RE"/></then>
      <else><nimo tool="selection" method="PHYSBO" minimization="false"/></else>
    </if>
    <nimo tool="update">
      <call server="sdl" tool="get_phase" temperature="proposal.temperature" pressure="proposal.pressure"/>
    </nimo>
  </repeat>
</workflow>
"""


def _nimo_call(el: ET.Element, catalog: Catalog) -> ToolCall:
    name = el.get("tool")
    if not name:
        raise ValueError('<nimo> needs a tool="..." attribute')
    args = {k: v for k, v in el.attrib.items() if k != "tool"}
    flag = args.pop("minimization", None)
    if flag not in (None, "true", "false"):
        raise ValueError(f'minimization must be "true" or "false", not "{flag}"')
    if name == "selection":
        # The selection tool takes the flag as a regular argument.
        if flag is not None:
            args["minimization"] = flag
    elif name == "plot_history_best":
        args["mode"] = "minimization" if flag == "true" else "maximization"
    elif flag is not None:
        raise ValueError(f'<nimo tool="{name}"> takes no minimization attribute')
    return ToolCall(server="nimo", name=name, arguments=args)


def _block(el: ET.Element, catalog: Catalog) -> Any:
    if el.tag == "repeat":
        if not el.get("counter"):
            raise ValueError('<repeat> needs a counter="..." attribute')
        return Repeat(times=int(el.get("times", "1")), counter=el.get("counter"),
                      body=[_block(c, catalog) for c in el])
    if el.tag == "if":
        then_el, else_el = el.find("then"), el.find("else")
        return If(counter=el.get("counter", ""), op=el.get("op", "=="),
                  value=int(el.get("value", "0")),
                  then=[_block(c, catalog) for c in then_el] if then_el is not None else [],
                  otherwise=[_block(c, catalog) for c in else_el] if else_el is not None else [])
    if el.tag == "nimo":
        if el.get("tool") == "update":
            inner = list(el)
            if len(inner) != 1:
                raise ValueError('<nimo tool="update"> must wrap exactly one call')
            tool = _block(inner[0], catalog)
            if not isinstance(tool, ToolCall):
                raise ValueError('<nimo tool="update"> must wrap a tool call')
            return Update(tool=tool)
        return _nimo_call(el, catalog)
    if el.tag == "call":
        server, tool = el.get("server"), el.get("tool")
        if not server or not tool:
            raise ValueError('<call> needs server="..." and tool="..." attributes')
        args = {k: v for k, v in el.attrib.items() if k not in ("server", "tool")}
        return ToolCall(server=server, name=tool, arguments=args)
    raise ValueError(f"unknown element <{el.tag}>; allowed: repeat, if, nimo, call")


def parse_workflow_xml(text: str, catalog: Catalog) -> Workflow:
    """The LLM's reply -> Workflow model. Raises ValueError with a message
    the model can act on."""
    m = re.search(r"<workflow[\s>].*</workflow>|<workflow\s*/>", text, re.S)
    if not m:
        raise ValueError("the reply must contain exactly one <workflow>...</workflow> element")
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError as e:
        raise ValueError(f"not well-formed XML: {e}")
    try:
        # normalize here, not just via validate(): write_workflow_from_xml
        # re-parses the accepted reply, so that copy needs it too.
        return normalize(Workflow(body=[_block(c, catalog) for c in root]))
    except ValidationError as e:
        raise ValueError(str(e))


def build_xml_planner_agent(model: Any) -> Agent:
    """The spec-prompted twin of planner.build_planner_agent: free-text XML
    out, but the same catalog instructions, the same validate() and the same
    retry budget."""
    agent = Agent(model, deps_type=Catalog, instructions=XML_INSTRUCTIONS,
                  retries=3)

    @agent.instructions
    def catalog_instructions(ctx: RunContext[Catalog]) -> str:
        parameters = ", ".join(ctx.deps.parameters) or "(none declared)"
        return (f"Declared candidate parameters (usable as proposal.<name>): "
                f"{parameters}\n\nTool catalog:\n\n{ctx.deps.render()}")

    @agent.output_validator
    def check(ctx: RunContext[Catalog], text: str) -> str:
        try:
            workflow = parse_workflow_xml(text, ctx.deps)
        except ValueError as e:
            raise ModelRetry("Fix these problems and return the workflow "
                             f"again:\n- {e}")
        errors = validate(workflow, ctx.deps)
        if errors:
            raise ModelRetry("Fix these problems and return the workflow again:\n- "
                             + "\n- ".join(errors))
        return text

    return agent


async def write_workflow_from_xml(instruction: str, catalog: Catalog, model: Any,
                                  usage: Optional[RunUsage] = None,
                                  event_stream_handler: Any = None) -> Workflow:
    """Spec-prompted counterpart of planner.write_workflow."""
    result = await build_xml_planner_agent(model).run(
        instruction, deps=catalog, usage=usage,
        event_stream_handler=event_stream_handler)
    return parse_workflow_xml(result.output, catalog)


def generate(instruction: str, catalog_data: dict, model_name: str,
             provider: str | None = None, base_url: str | None = None,
             thinking: str | None = None, temperature: float | None = None,
             seed: int | None = None, timeout: float | None = None,
             attempts_log: str | None = None) -> str:
    """The CLAIRify-style spec-prompted planner as XML generation."""
    return generate_with(write_workflow_from_xml, instruction, catalog_data,
                         model_name, provider, base_url, thinking, temperature,
                         seed, timeout, attempts_log)


if __name__ == "__main__":
    cli_main(generate, __doc__)
