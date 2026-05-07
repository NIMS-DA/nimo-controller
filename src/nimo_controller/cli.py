"""Console-script entry point: ``nimo-controller``."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
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


def _wait_for_mcp_server(port: int, timeout: float = 15.0, interval: float = 0.3) -> bool:
    """TCP-connect to host:port until it succeeds (server is up) or timeout."""
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=1):
                return True
        except OSError:
            time.sleep(interval)
    return False


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

    # ------------------------------------------------------------------
    # If nimo_mcp_port is set, start the NIMO MCP HTTP server first.
    # ------------------------------------------------------------------
    nimo_proc: subprocess.Popen | None = None
    nimo_mcp_port: int = cfg.get("nimo_mcp_port", 8008)

    nimo_mcp_port = int(nimo_mcp_port)
    nimo_mcp_url = f"http://127.0.0.1:{nimo_mcp_port}/mcp"

    cmd = [
        sys.executable, "-m", "nimo_controller.nimo_mcp.nimo_server",
        "--transport", "streamable-http",
        "--port", str(nimo_mcp_port),
    ]
    print(f"[nimo-mcp] Starting NIMO MCP server on port {nimo_mcp_port}", flush=True)
    nimo_proc = subprocess.Popen(cmd)

    if _wait_for_mcp_server(nimo_mcp_port):
        print("[nimo-mcp] NIMO MCP server is ready.", flush=True)
    else:
        print(
            "[nimo-mcp] WARNING: NIMO MCP server did not respond within timeout. "
            "Continuing anyway.",
            flush=True,
        )

    # ------------------------------------------------------------------
    # Graceful shutdown: stop the MCP subprocess on exit.
    # ------------------------------------------------------------------
    def _stop_nimo_proc() -> None:
        if nimo_proc and nimo_proc.poll() is None:
            print("[nimo-mcp] Stopping NIMO MCP server …", flush=True)
            nimo_proc.terminate()
            try:
                nimo_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                nimo_proc.kill()

    # ------------------------------------------------------------------
    # Start the main web server.
    # ------------------------------------------------------------------
    try:
        uvicorn.run(
            "nimo_controller.server:app",
            host=args.host,
            port=port,
            reload=args.reload,
        )
    finally:
        _stop_nimo_proc()


if __name__ == "__main__":
    main()