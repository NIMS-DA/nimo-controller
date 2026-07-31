"""Resource and runtime-data path resolution.

Templates and static assets are read from the installed package via
``importlib.resources``.  Anything written at runtime (uploaded candidates,
optimization results) lives under a data directory that is:

1. taken from the ``data_dir`` key in ``config.yaml`` when present, or
2. ``~/.nimo-controller`` otherwise (holding ``candidates.csv`` and a
   ``results`` subfolder),

so the package itself stays read-only and the location is the same regardless
of where the app is launched from.
"""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path

import platformdirs
import yaml

_APP_NAME = "nimo-controller"

# Read-only resources packaged inside the wheel.
_PKG = files("nimo_controller")
TEMPLATES_DIR: Path = Path(str(_PKG / "templates"))
STATIC_DIR: Path = Path(str(_PKG / "static"))

# User config location (used only to locate config.yaml). Independent of the
# data directory, so resolving the config never depends on data_dir.
USER_CONFIG_DIR: Path = Path(
    os.getenv("NIMO_CONFIG_DIR", platformdirs.user_config_dir(_APP_NAME))
)
USER_CONFIG_FILE: Path = USER_CONFIG_DIR / "config.yaml"

# What the UI last chose, remembered so the next launch starts where the last
# one left off. Written by the app, unlike config.yaml which the user writes,
# and kept beside it rather than in the data directory: it is a preference about
# the agent, not part of any one campaign's results.
AGENT_STATE_FILE: Path = USER_CONFIG_DIR / "agent-state.json"


def resolve_config_path() -> Path:
    """Return the config file to use, in priority order.

    1. ``./config.yaml`` in the current working directory (development convenience)
    2. ``<user-config-dir>/config.yaml``

    No config file is bundled with the package, so the user path is returned
    even when it does not exist yet; callers treat a missing file as empty
    config and fall back to their own defaults.
    """

    cwd_cfg = Path.cwd() / "config.yaml"
    if cwd_cfg.is_file():
        return cwd_cfg

    return USER_CONFIG_FILE


def _resolve_data_dir() -> Path:
    """Determine the writable data directory.

    Uses the ``data_dir`` key from config.yaml when set; otherwise falls back
    to ``~/.nimo-controller`` (a fixed per-user folder, independent of where
    the app is launched from). A relative ``data_dir`` is resolved against the
    current working directory, and a leading ``~`` is expanded.
    """
    try:
        cfg_path = resolve_config_path()
        if cfg_path.is_file():
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            data_dir = cfg.get("data_dir")
            if data_dir:
                return Path(str(data_dir)).expanduser().resolve()
    except Exception:
        # Any problem reading the config -> fall back to the default folder.
        pass
    return Path.home() / ".nimo-controller"


# Writable per-user locations.
USER_DATA_DIR: Path = _resolve_data_dir()
CANDIDATES_FILE: Path = USER_DATA_DIR / "candidates.csv"
RESULTS_DIR: Path = USER_DATA_DIR / "results"


def ensure_user_dirs() -> None:
    """Create the data/config directories if they don't already exist."""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)