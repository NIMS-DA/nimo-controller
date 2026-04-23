"""Resource and runtime-data path resolution.

Templates / static / default config are read from the installed package via
``importlib.resources``.  Anything written at runtime (uploaded candidates,
optimization results) lives under the user data directory provided by
``platformdirs`` so the package itself stays read-only.
"""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path

import platformdirs

_APP_NAME = "nimo-controller"

# Read-only resources packaged inside the wheel.
_PKG = files("nimo_controller")
TEMPLATES_DIR: Path = Path(str(_PKG / "templates"))
STATIC_DIR: Path = Path(str(_PKG / "static"))
DEFAULT_CONFIG: Path = Path(str(_PKG / "data" / "default_config.yaml"))
NIMO_SERVER_SCRIPT: Path = Path(str(_PKG / "nimo_mcp" / "nimo_server.py"))

# Writable per-user locations.
USER_DATA_DIR: Path = Path(
    os.getenv("NIMO_DATA_DIR", platformdirs.user_data_dir(_APP_NAME))
)
USER_CONFIG_DIR: Path = Path(
    os.getenv("NIMO_CONFIG_DIR", platformdirs.user_config_dir(_APP_NAME))
)
USER_CONFIG_FILE: Path = USER_CONFIG_DIR / "config.yaml"

CANDIDATES_FILE: Path = USER_DATA_DIR / "candidates.csv"
RESULTS_DIR: Path = USER_DATA_DIR / "results"


def ensure_user_dirs() -> None:
    """Create user data/config directories if they don't already exist."""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def resolve_config_path() -> Path:
    """Return the config file to use, in priority order.

    1. ``./config.yaml`` in the current working directory (development convenience)
    2. ``<user-config-dir>/config.yaml``
    3. The default config bundled with the package
    """

    cwd_cfg = Path.cwd() / "config.yaml"
    if cwd_cfg.is_file():
        return cwd_cfg

    if USER_CONFIG_FILE.is_file():
        return USER_CONFIG_FILE

    return DEFAULT_CONFIG
