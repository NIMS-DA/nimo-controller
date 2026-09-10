"""Judge whether two nimo-workflow XMLs behave the same.

Structurally different workflows can be equivalent — a 20-round repeat whose
<if> switches method halfway behaves exactly like two 10-round repeats. So
instead of comparing trees, this symbolically executes each <workflow> with
the same semantics as server.py's _exec_node (0-based loop counters, <if>
against the counter) and compares the resulting flat sequences of tool calls.
Built-in tools are written as <nimo tool="selection" method="RE"/> — with
minimization="true" marking a minimizing proposal, and <nimo tool="update">
wrapping the measured call — and MCP server tools as
<call server="ackley" tool="ackley_function" .../>; repeat and if are the
structural tags. Everything outside <workflow> (servers, candidates
snapshot/sha256) is run metadata and ignored.

    uv run python evaluation/compare_xml.py generated.xml expected.xml

Each input's flattened trace is saved next to it as <file>.trace, one tool
call per line, so a mismatch can be inspected afterwards with
``diff generated.xml.trace expected.xml.trace``. Exits 0 when equivalent,
1 when not.
"""

import argparse
import sys
import xml.etree.ElementTree as ET

_OPS = {"==": lambda a, b: a == b, "<": lambda a, b: a < b,
        ">": lambda a, b: a > b, "<=": lambda a, b: a <= b,
        ">=": lambda a, b: a >= b}


def _arg_value(v: str, counters: dict):
    """Resolve one attribute value the way the executor would.

    counter.<name> becomes the current loop index; a numeric literal is
    canonicalized so "10" and "10.0" compare equal; anything else (including
    the symbolic proposal.<x>) is compared as text.
    """
    if v.startswith("counter."):
        name = v[len("counter."):]
        if name not in counters:
            raise ValueError(f"counter '{name}' not in scope")
        return counters[name]
    try:
        return float(v)
    except ValueError:
        return v


def _run(node: ET.Element, counters: dict, trace: list) -> None:
    for el in node:
        if el.tag == "repeat":
            counter = el.get("counter")
            for r in range(int(el.get("times", "1"))):
                if counter:
                    counters[counter] = r
                _run(el, counters, trace)
            if counter:
                counters.pop(counter, None)
        elif el.tag == "if":
            name = el.get("counter", "")
            if name not in counters:
                raise ValueError(f"if: counter '{name}' not in scope")
            cond = _OPS[el.get("op", "==")](counters[name],
                                            int(el.get("value", "0")))
            branch = el.find("then") if cond else el.find("else")
            if branch is not None:
                _run(branch, counters, trace)
        elif el.tag == "nimo":
            # Built-in tool call: <nimo tool="selection" method="RE"/>.
            name = el.get("tool")
            if name is None:
                raise ValueError("<nimo> is missing the tool attribute")
            if name == "update":
                # <nimo tool="update"> wraps the measured call: run it, then
                # the built-in update receives its numeric result.
                _run(el, counters, trace)
                trace.append(("nimo", "update", (("objs", "<last_result>"),)))
                continue
            args = {k: _arg_value(v, counters) for k, v in el.attrib.items()
                    if k != "tool"}
            # minimization="false" and an absent flag mean the same behavior.
            if args.get("minimization") == "false":
                del args["minimization"]
            trace.append(("nimo", name, tuple(sorted(args.items()))))
        elif el.tag == "call":
            # MCP server tool call: <call server=".." tool=".." arg="..."/>.
            server, name = el.get("server"), el.get("tool")
            if not server or not name:
                raise ValueError("<call> needs server and tool attributes")
            args = {k: _arg_value(v, counters) for k, v in el.attrib.items()
                    if k not in ("server", "tool")}
            trace.append((server, name, tuple(sorted(args.items()))))
        else:
            raise ValueError(f"unknown workflow element: <{el.tag}>")


def trace_of(xml_text: str) -> list:
    """Flatten one nimo-workflow document into its sequence of tool calls."""
    root = ET.fromstring(xml_text)
    workflow = root.find("workflow")
    trace: list = []
    if workflow is not None:
        _run(workflow, {}, trace)
    return trace


def same_behavior(expected_xml: str, actual_xml: str) -> bool:
    return trace_of(expected_xml) == trace_of(actual_xml)


def trace_lines(trace: list) -> list[str]:
    """One diff-friendly text line per tool call."""
    return [f"[{server}] {name} " + " ".join(f"{k}={v}" for k, v in args)
            for server, name, args in trace]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generated", help="XML produced by generate_xml.py")
    parser.add_argument("expected", help="reference XML")
    args = parser.parse_args()

    traces = []
    for path in (args.generated, args.expected):
        with open(path, encoding="utf-8") as f:
            traces.append(trace_of(f.read()))
        with open(path + ".trace", "w", encoding="utf-8") as f:
            f.write("\n".join(trace_lines(traces[-1])) + "\n")

    generated_trace, expected_trace = traces
    if generated_trace == expected_trace:
        print(f"EQUIVALENT ({len(expected_trace)} tool calls)")
        return
    print(f"DIFFERENT (expected {len(expected_trace)} tool calls, "
          f"generated {len(generated_trace)})")
    sys.exit(1)


if __name__ == "__main__":
    main()
