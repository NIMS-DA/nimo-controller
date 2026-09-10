from __future__ import annotations

import os
import subprocess
from enum import Enum

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError


class PlateStage(str, Enum):
    EMPTY = "empty"
    LOADED = "loaded"
    PREPARED = "prepared"
    DRIED = "dried"
    IMAGED = "imaged"


class CoffeeRingSDL:
    def __init__(self) -> None:
        self.stage = PlateStage.EMPTY

        self.last_pva = None
        self.last_dtab = None

    def load_plate(self) -> str:
        """
        Robot loads an empty well plate into the liquid handler.
        Only one plate is on the stage at a time.
        """
        if self.stage != PlateStage.EMPTY:
            raise ToolError("A plate is loaded; it must be removed before loading another plate")

        self.stage = PlateStage.LOADED
        self.last_pva = None
        self.last_dtab = None
        return "Robot loaded an empty well plate onto the liquid handler."

    def prepare_sample(self, pva: float, dtab: float) -> str:
        """
        Robot prepares a sample solution in a mix plate.
        pva and dtab specify the concentration of each reagent (0 to 1).
        This function must be called when an empty well plate is loaded in the liquid handler.
        """

        if self.stage != PlateStage.LOADED:
            raise ToolError("An empty well plate must be stored in the liquid handler before calling this tool.")
        if pva < 0 or pva > 1:
            raise ToolError("PVA concentration must be between 0 and 1")
        if dtab < 0 or dtab > 1:
            raise ToolError("DTAB concentration must be between 0 and 1")

        self.stage = PlateStage.PREPARED
        self.last_pva = pva
        self.last_dtab = dtab
        return f"Robot deposited a droplet of PVA {pva} and DTAB {dtab}."

    def heat_plate(self) -> str:
        """
        Robot heats the stage until the droplet has dried completely.
        The sample solution must be prepared before calling this tool.
        """
        if self.stage != PlateStage.PREPARED:
            raise ToolError("The sample solution must be prepared before calling this tool")

        self.stage = PlateStage.DRIED
        return "Robot dried the droplet on the hotplate."

    def get_image(self) -> int:
        """
        Take an image of the dried droplet.
        Returns 1 when the ring is observed; otherwise 0 is returned.
        The well plate must be dried before calling this tool.
        """

        if self.stage != PlateStage.DRIED:
            raise ToolError("heat_plate() must be called before the droplet can be imaged")

        self.stage = PlateStage.IMAGED
        if self.last_pva <= 0.005 and self.last_dtab >= 0.005:
            return 1
        else:
            return 0

    def place_plate(self) -> str:
        """
        Robot places the well plate in the rack.
        The stage is left empty, ready for the next plate.
        """

        if self.stage != PlateStage.IMAGED:
            raise ToolError("get_image() must be called before the plate is placed in the rack")

        self.stage = PlateStage.EMPTY
        return "Robot placed the plate in the rack."


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


sdl = CoffeeRingSDL()
_hash = git_hash_short()
mcp = FastMCP("coffee", version=f"0.0.1+{_hash}" if _hash else "0.0.1")

mcp.tool(sdl.load_plate)
mcp.tool(sdl.prepare_sample)
mcp.tool(sdl.heat_plate)
mcp.tool(sdl.get_image)
mcp.tool(sdl.place_plate)

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8001)
