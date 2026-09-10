from __future__ import annotations

from enum import Enum
from typing import Optional

import numpy as np
from PIL import Image as PILImage
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

class Color(str, Enum):
    RED = "red"
    YELLOW = "yellow"
    BLUE = "blue"


class LocationType(str, Enum):
    HOME = "home"
    COLOR_WELL = "color_well"
    MIX_WELL = "mix_well"


class RobotLocation(BaseModel):
    type: LocationType
    color: Optional[Color] = None
    well: Optional[int] = Field(default=None, ge=0, lt=12)

class PipetteContents(BaseModel):
    color: Color
    volume: float = Field(gt=0, le=1)


class RobotState(BaseModel):
    location: RobotLocation = Field(
        default_factory=lambda: RobotLocation(type=LocationType.HOME)
    )
    pipette: Optional[PipetteContents] = None


class MixWell(BaseModel):
    red: float = Field(default=0.0, ge=0)
    yellow: float = Field(default=0.0, ge=0)
    blue: float = Field(default=0.0, ge=0)

    def add(self, color: Color, volume: float) -> None:
        if color == Color.RED:
            self.red += volume
        elif color == Color.YELLOW:
            self.yellow += volume
        elif color == Color.BLUE:
            self.blue += volume

    @property
    def total_volume(self) -> float:
        return self.red + self.yellow + self.blue

def rgb_to_lab(r: int, g: int, b: int) -> np.ndarray:
    pixel = PILImage.new("RGB", (1, 1), (r, g, b)).convert("LAB").getpixel((0, 0))
    L = pixel[0] / 255.0 * 100.0
    a = pixel[1] - 128.0
    b = pixel[2] - 128.0
    return np.array([L, a, b])

def cie76(lab1: np.ndarray, lab2: np.ndarray) -> float:
    return float(np.linalg.norm(lab1 - lab2))

class ColorMatchingSDL:

    def __init__(self) -> None:
        self.state = RobotState()
        self.mix_wells = [MixWell() for _ in range(12)]

    def move_to_home(self) -> str:
        self.state.location = RobotLocation(type=LocationType.HOME)
        return "Robot moved to home position."

    def move_to_color_well(self, color: Color) -> str:
        self.state.location = RobotLocation(type=LocationType.COLOR_WELL, color=color)
        return f"Robot moved to {color.value} color well."

    def move_to_mix_well(self, well: int) -> str:
        if not 0 <= well < len(self.mix_wells):
            raise ToolError(f"Well number must be between 0 and 11.")
        self.state.location = RobotLocation(type=LocationType.MIX_WELL, well=well)
        return f"Robot moved to mix well {well}."


    def aspirate(self, volume: float) -> str:
        if volume == 0:
            return f"Skip aspiration."
        if self.state.pipette is not None:
            raise ToolError("Pipette is not empty.")
        if not 0 < volume <= 1:
            raise ToolError("Pipette volume must be between 0 and 1 mL.")

        location = self.state.location
        if location.type != LocationType.COLOR_WELL:
            raise ToolError("Robot must be at a color well to aspirate.")
        if location.color is None:
            raise ToolError("Current location has no color.")

        self.state.pipette = PipetteContents(color=location.color, volume=volume)
        return f"Robot aspirated {volume} uL of {location.color.value}."

    def dispense(self, volume: float) -> str:
        if volume == 0:
            return "Skip dispensing."
        pipette = self.state.pipette
        if pipette is None:
            raise ToolError("Pipette is empty.")
        if not 0 < volume <= pipette.volume:
            raise ToolError(f"Cannot dispense {volume} mL from {pipette.volume} mL.")

        location = self.state.location
        if location.type != LocationType.MIX_WELL:
            raise ToolError("Robot must be at a mix well to dispense.")
        if location.well is None:
            raise ToolError("Current location has no well number.")

        self.mix_wells[location.well].add(pipette.color, volume)

        remaining = pipette.volume - volume
        self.state.pipette = (
            None if remaining == 0
            else PipetteContents(color=pipette.color, volume=remaining)
        )

        return f"Robot dispensed {volume} mL of {pipette.color.value} into mix well {location.well}."

    def get_color_diff(self, well: int, target_hex: str) -> float:
        if not 0 <= well < len(self.mix_wells):
            raise ToolError("Well number must be between 0 and 11.")

        mix = self.mix_wells[well]
        if mix.total_volume == 0:
            raise ToolError(f"Mix well {well} is empty.")

        hex_clean = target_hex.lstrip("#")
        if len(hex_clean) != 6:
            raise ToolError(f"Invalid hex color: '{target_hex}'.")

        try:
            target_rgb = tuple(int(hex_clean[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            raise ToolError(f"Invalid hex color: '{target_hex}'.")

        target_lab = rgb_to_lab(*target_rgb)

        # Color estimation
        total = mix.total_volume
        color_rgb = {
            Color.RED: (0.90, 0.10, 0.15),
            Color.YELLOW: (0.95, 0.85, 0.05),
            Color.BLUE: (0.10, 0.35, 0.85),
        }
        fractions = {
            Color.RED: mix.red / total,
            Color.YELLOW: mix.yellow / total,
            Color.BLUE: mix.blue / total,
        }

        rgb = []
        for channel in range(3):
            value = 1.0
            for color in Color:
                value *= color_rgb[color][channel] ** fractions[color]
            rgb.append(round(value * 255))

        return cie76(target_lab, rgb_to_lab(*rgb))

# Get Git commit hash
import os
import subprocess

def git_hash_short() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stderr=subprocess.DEVNULL,
    ).decode("ascii").strip()


sdl = ColorMatchingSDL()
mcp = FastMCP(version=f"0.0.1+{git_hash_short()}")

mcp.tool(sdl.move_to_home)
mcp.tool(sdl.move_to_color_well)
mcp.tool(sdl.move_to_mix_well)
mcp.tool(sdl.aspirate)
mcp.tool(sdl.dispense)
mcp.tool(sdl.get_color_diff)

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8001)