"""
Generate catalog from MCP servers and candidates file.

Command example:
uv run python evaluation/build_catalog.py evaluation/catalog.json \
    --candidates example_mcp/optimization/candidates.csv \
    --mcp sdl=http://127.0.0.1:8001/mcp
"""

import argparse
import asyncio
import csv
import json
import sys

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)

log("[catalog] started catalog generation.")

import yaml
from fastmcp import Client

from nimo_controller.nimo_client import NIMO_MCP_NAME, nimo_client
from nimo_controller.paths import CANDIDATES_FILE, resolve_config_path


def _tool_entry(server_id: str, t) -> dict:
    return {"server_id": server_id, "name": t.name, "description": t.description,
            "input_schema": t.inputSchema,
            "output_schema": getattr(t, "outputSchema", None)}


async def collect(mcp_servers: dict) -> tuple[list[dict], dict]:
    # Over stdio, like the app: the catalog then describes exactly the tool
    # surface a run would see.
    log("[catalog] starting nimo (stdio)...")
    async with nimo_client() as c:
        tools = [_tool_entry(NIMO_MCP_NAME, t) for t in await c.list_tools()]
    log(f"[catalog] nimo: {len(tools)} tools")
    servers: dict = {}
    for sid, url in mcp_servers.items():
        log(f"[catalog] connecting {sid} ({url})...")
        async with Client(url) as c:
            listed = await c.list_tools()
            tools.extend(_tool_entry(sid, t) for t in listed)
            info = getattr(c.initialize_result, "serverInfo", None)
            servers[sid] = {"version": getattr(info, "version", None)}
            log(f"[catalog] {sid}: {len(listed)} tools")
    return tools, servers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", nargs="?", default="evaluation/catalog.json",
                        help="output JSON path (default: %(default)s)")
    parser.add_argument("--candidates", default=str(CANDIDATES_FILE),
                        help="path to candidates file to take parameter names from")
    parser.add_argument("--mcp", action="append", default=[], metavar="NAME=URL",
                        help="MCP servers to connect "
                             "(default: mcp_servers from config.yaml)")
    args = parser.parse_args()

    log(f"[catalog] reading parameters from {args.candidates}")
    try:
        with open(args.candidates, newline="", encoding="utf-8") as f:
            cols = next(csv.reader(f), [])
    except FileNotFoundError:
        sys.exit(f"candidates file not found: {args.candidates}")
    # Last column is the objective — same fixed n_objectives = 1 as NimoWrapper.
    params, objectives = cols[:-1], cols[-1:]

    if args.mcp:
        try:
            mcp_servers = dict(spec.split("=", 1) for spec in args.mcp)
        except ValueError:
            sys.exit("--mcp expects NAME=URL")
    else:
        cfg_path = resolve_config_path()
        cfg: dict = {}
        if cfg_path.is_file():
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        mcp_servers = cfg.get("mcp_servers") or {}

    tools, servers = asyncio.run(collect(mcp_servers))
    catalog = {"tools": tools, "parameters": params,
               "objectives": objectives, "servers": servers}

    with open(args.catalog, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2)
    sids = sorted({t["server_id"] for t in tools})
    print(f"Wrote {args.catalog}: {len(tools)} tools ({', '.join(sids)}); "
          f"parameters: {params}; objectives: {objectives}")


if __name__ == "__main__":

    main()
