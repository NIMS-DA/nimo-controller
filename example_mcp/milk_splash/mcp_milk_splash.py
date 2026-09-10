"""Milk-splash sample SDL: drop milk from a height onto a dish of milk.

The splash phase depends on the drop height and the volume of milk in the
dish, so the phase diagram over (volume, height) can be mapped with PDC.
"""

from __future__ import annotations

import os
import subprocess
from enum import Enum

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError


class DropperState(str, Enum):
    EMPTY = "empty"
    READY = "ready"


class MilkSplashSDL:
    """Simulated robot: a dropper on a vertical axis over a Petri dish."""

    def __init__(self) -> None:
        self.state = DropperState.EMPTY
        self.volume = 0
        self.height = 0

        self.last_volume = None
        self.last_height = None

    def aspirate_milk(self) -> str:
        """
        Robot aspirates milk into the dropper.
        Robot moves to height 0.
        """
        if self.state == DropperState.READY:
            raise ToolError("Dropper has already aspirated milk")

        self.state = DropperState.READY
        self.height = 0
        return "Robot aspirated milk."

    def drop_milk(self) -> str:
        """
        Robot drops milk from the current height.
        """
        if self.state != DropperState.READY:
            raise ToolError("Dropper needs to aspirate milk before dropping milk")

        if self.height == 0:
            raise ToolError("Robot cannot drop milk from height 0; "
                            "set_height must be called after aspirate_milk")

        self.state = DropperState.EMPTY
        self.last_height = self.height
        self.last_volume = self.volume

        return "Robot dropped milk successfully."

    def analyze_result(self) -> int:
        """
        Analyze the result of the last experiment.
        Returns an integer from 0 to 2 identifying the observed splash phase.
        """
        if self.last_volume is None or self.last_height is None:
            raise ToolError("Robot must drop milk before analyzing the result")
        if self.last_volume < 12 and self.last_height > 45 * self.last_volume + 60:
            return 2
        elif self.last_volume > 8 and self.last_height < 300:
            return 0
        else:
            return 1

    def set_height(self, height: float) -> str:
        """
        Move robot to specified height in mm.
        The height must be a positive value.
        """
        if height <= 0:
            raise ToolError("Height must be a positive value")
        self.height = height
        return f"Robot moved to height {height} mm."

    def set_volume(self, volume: float) -> str:
        """
        Set the volume of milk in the Petri dish in mL.
        The volume must not be negative.
        """
        if volume < 0:
            raise ToolError("Volume must not be negative")
        self.volume = volume
        return f"Volume set to {volume} mL."


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


sdl = MilkSplashSDL()
_hash = git_hash_short()
mcp = FastMCP("milk", version=f"0.0.1+{_hash}" if _hash else "0.0.1")

mcp.tool(sdl.set_height)
mcp.tool(sdl.set_volume)
mcp.tool(sdl.aspirate_milk)
mcp.tool(sdl.drop_milk)
mcp.tool(sdl.analyze_result)

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8001)
