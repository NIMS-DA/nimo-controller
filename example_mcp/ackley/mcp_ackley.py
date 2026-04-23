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
        return -value


sdl = SampleSDL()

mcp = FastMCP()
mcp.tool(sdl.ackley_function)

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8001)