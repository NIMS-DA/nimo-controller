import math
import os
import subprocess

from mcp.server.fastmcp import FastMCP


def git_hash_short() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except Exception:
        return ""


mcp = FastMCP(name="sdl", host="127.0.0.1", port=8001)

_hash = git_hash_short()
mcp._mcp_server.version = f"0.0.1+{_hash}" if _hash else "0.0.1"


@mcp.tool()
def ackley_function(x1: float, x2: float) -> float:
    """Evaluate the Ackley function at (x1, x2).
    """
    a = 20.0
    b = 0.2
    c = 2.0 * math.pi
    d_inv = 0.5

    part_1 = -b * math.sqrt(d_inv * (x1 * x1 + x2 * x2))
    part_2 = d_inv * (math.cos(c * x1) + math.cos(c * x2))
    value = -a * math.exp(part_1) - math.exp(part_2) + a + math.exp(1)
    return value


@mcp.tool()
def rosenbrock_function(x1: float, x2: float) -> float:
    """Evaluate the Rosenbrock function at (x1, x2).
    """
    return 100.0 * (x2 - x1 * x1) ** 2 + (1.0 - x1) ** 2


@mcp.tool()
def beale_function(x1: float, x2: float) -> float:
    """Evaluate the Beale function at (x1, x2).
    """
    return ((1.5 - x1 + x1 * x2) ** 2
            + (2.25 - x1 + x1 * x2 ** 2) ** 2
            + (2.625 - x1 + x1 * x2 ** 3) ** 2)


@mcp.tool()
def booth_function(x1: float, x2: float) -> float:
    """Evaluate the Booth function at (x1, x2).
    """
    return (x1 + 2.0 * x2 - 7.0) ** 2 + (2.0 * x1 + x2 - 5.0) ** 2


@mcp.tool()
def matyas_function(x1: float, x2: float) -> float:
    """Evaluate the Matyas function at (x1, x2).
    """
    return 0.26 * (x1 * x1 + x2 * x2) - 0.48 * x1 * x2


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
