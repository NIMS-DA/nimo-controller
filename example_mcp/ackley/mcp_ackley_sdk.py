"""The Ackley sample, written with the official MCP SDK.

Same tool, same port and same candidates.csv as mcp_ackley.py, so the workflow
you build in NIMO Controller is identical — run one or the other, not both.
What differs is the library underneath: `mcp.server.fastmcp.FastMCP` from the
official SDK instead of the `fastmcp` package, and plain decorated functions
instead of methods on an SDL object.

Worth knowing when writing a server this way:

* A tool returning a bare number cannot be described by an MCP output schema,
  which must be an object, so the SDK wraps the value as {"result": ...}. The
  `fastmcp` package marks that wrapping and unwraps it again on the client
  side; the official SDK does not mark it. NIMO Controller reads the tool's
  output schema to recognise the wrapper either way, so an `update` block gets
  the number and not a one-key dict.
* The SDK has no `version=` argument, and NIMO Controller records the server
  version in the workflow XML and the report. See git_hash_short() below.

    cd example_mcp/ackley
    uv run mcp_ackley_sdk.py
"""

import math
import os
import subprocess

from mcp.server.fastmcp import FastMCP


def git_hash_short() -> str:
    """The commit this is running, or "" when it is not a git checkout."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except Exception:
        return ""


mcp = FastMCP(name="sdl", host="127.0.0.1", port=8001)

# FastMCP takes no version, but the low-level server it wraps does, and that is
# what fills in serverInfo. Set here so a run can be traced back to the code
# that produced it — NIMO Controller copies this string into <server version=…>
# and into the report.
_hash = git_hash_short()
mcp._mcp_server.version = f"0.0.1+{_hash}" if _hash else "0.0.1"


@mcp.tool()
def ackley_function(x1: float, x2: float) -> float:
    """Evaluate the negated Ackley function at (x1, x2).

    Negated because PHYSBO maximizes, so the Ackley minimum at the origin has
    to be this objective's maximum.
    """
    a = 20.0
    b = 0.2
    c = 2.0 * math.pi
    d_inv = 0.5

    part_1 = -b * math.sqrt(d_inv * (x1 * x1 + x2 * x2))
    part_2 = d_inv * (math.cos(c * x1) + math.cos(c * x2))
    value = -a * math.exp(part_1) - math.exp(part_2) + a + math.exp(1)
    return -value


if __name__ == "__main__":
    # A `def` tool runs on the event loop — the SDK awaits async tools and calls
    # sync ones directly — so the server answers nothing else until it returns.
    # That is free for arithmetic like this, but a tool that drives real
    # hardware should be `async def` and await its waits, or a Cancel pressed
    # mid-measurement will not be read until the measurement is over.
    mcp.run(transport="streamable-http")
