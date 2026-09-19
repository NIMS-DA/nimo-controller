"""How to reach NIMO: a stdio MCP subprocess.

NIMO runs out of process so nimo's own failure modes stay out of the app —
its ``sys.exit()`` paths, matplotlib's thread rules, and the GIL it holds
through a GP fit. Keeping the spawn recipe here (rather than in
``nimo_mcp``) also keeps ``import nimo`` — 4.5s and 2300 modules, and the
thing being isolated — out of the parent process entirely.
"""

from __future__ import annotations

import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

# Server identifier used in tool catalogs and workflow ASTs.
NIMO_MCP_NAME = "nimo"


def nimo_client(log_handler=None) -> Client:
    """A client for a freshly spawned NIMO server.

    The subprocess inherits this process's working directory on purpose:
    ``paths.resolve_config_path()`` looks for ``./config.yaml`` there, so a
    different cwd would silently give the two processes different data
    directories — and NIMO writes the run directory the app then reads.

    ``log_handler`` receives NIMO's log notifications — the channel a tool
    uses to say something in words without changing what it returns, e.g.
    that it substituted a selection algorithm.
    """
    return Client(StdioTransport(
        command=sys.executable,
        args=["-m", "nimo_controller.nimo_mcp", "--transport", "stdio"],
        cwd=os.getcwd(),
        # Stop the subprocess when the application closes.
        keep_alive=False,
    ), log_handler=log_handler)
