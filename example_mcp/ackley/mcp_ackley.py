import math

from fastmcp import FastMCP

class SampleSDL:
    def ackley_function(self, x1: float, x2: float) -> float:
        a = 20.0
        b = 0.2
        c = 2.0 * math.pi
        d_inv = 0.5

        part_1 = -b * math.sqrt(d_inv * (x1*x1 + x2*x2))
        part_2 = d_inv * (math.cos(c*x1) + math.cos(c*x2))
        value = -a * math.exp(part_1) - math.exp(part_2) + a + math.exp(1)
        return value

# Get Git commit hash
import os
import subprocess

def git_hash_short() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stderr=subprocess.DEVNULL,
    ).decode("ascii").strip()

sdl = SampleSDL()

mcp = FastMCP(version=f"0.0.1+{git_hash_short()}")
mcp.tool(sdl.ackley_function)

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8001)