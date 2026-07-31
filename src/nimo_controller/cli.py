"""Console-script entry point: ``nimo-controller``."""

from __future__ import annotations

import argparse
import os
import uvicorn
import yaml

from .paths import ensure_user_dirs, resolve_config_path


def _read_config() -> dict:
    cfg_path = resolve_config_path()
    try:
        if cfg_path.is_file():
            with open(cfg_path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nimo-controller",
        description="Run the NIMO Controller web UI.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (overrides config.yaml)")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload (dev)")
    args = parser.parse_args()

    if args.config:
        os.environ["NIMO_CONFIG"] = args.config

    ensure_user_dirs()

    cfg = _read_config()
    port = args.port if args.port is not None else int(cfg.get("port", 8888))

    # NIMO runs in-process inside the web server (direct function calls),
    # so there is no separate NIMO MCP server to launch here.
    uvicorn.run(
        "nimo_controller.server:app",
        host=args.host,
        port=port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
