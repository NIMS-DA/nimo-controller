from fastmcp import FastMCP


class SampleSDL:
    def __init__(self):
        # 相図パラメータの定義
        self.t_triple = 0
        self.p_triple = 5000
        self.slope = 50

    def get_phase(self, temperature: float, pressure: float) -> int:
        """
        指定された温度・圧力に対応する相を求める
        0: 固相, 1: 液相, 2: 気相
        """
        boundary = self.slope * (temperature - self.t_triple) + self.p_triple

        if pressure < boundary:
            return 2
        else:
            if temperature < self.t_triple:
                return 0
            else:
                return 1


sdl = SampleSDL()

mcp = FastMCP("Self-driving laboratory controller")
mcp.tool(sdl.get_phase)

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8001)