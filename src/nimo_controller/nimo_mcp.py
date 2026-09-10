"""NIMO tools — the optimization backend, served over MCP.

* ``NimoWrapper`` — a plain-Python, stateful wrapper around the ``nimo``
  optimization library (candidates file, per-run result dirs, history).
* ``mcp`` — the FastMCP server exposing it.

This runs as its own process: the controller spawns it over stdio (see
``nimo_client.py``), so nimo's ``sys.exit()`` paths, matplotlib's thread
rules and the GIL it holds through a fit stay out of the app. Run it by hand
with ``python -m nimo_controller.nimo_mcp`` (``--transport streamable-http``
for HTTP instead).

Under stdio the protocol owns the real stdout, so everything nimo prints is
diverted to stderr before nimo is even imported — see the fd handling below.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

# --- stdio transport: take fd 1 away from everything else --------------------
# Under stdio the JSON-RPC frames own stdout, but nimo (and the compiled code
# under it) prints freely, and one stray line corrupts the protocol.
# redirect_stdout() cannot cover that: it only swaps sys.stdout, so it misses
# import-time output and anything writing to the descriptor directly. So
# before nimo is imported, move the real stdout aside and point fd 1 at
# stderr. Every print, from Python or C, then lands on stderr, and the
# protocol stream is reachable only through the saved descriptor — which only
# _run_stdio() hands to the transport.
_PROTO_FD: Optional[int] = None
if __name__ == "__main__" and "streamable-http" not in sys.argv:
    _PROTO_FD = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(1, "w", buffering=1, errors="replace")

# A selection runs on a worker thread (asyncio.to_thread, so the transport
# stays responsive through a fit), but matplotlib's default backend here is a
# GUI one — TkAgg on Windows — which must live on the main thread. The first
# plot only warns; the second dies with "main thread is not in main loop" and
# can take the process down with "Tcl_AsyncDelete: async handler deleted by the
# wrong thread". We only ever write figures to disk, so pin the non-interactive
# backend. Set before importing nimo, which imports pyplot at module scope.
os.environ.setdefault("MPLBACKEND", "Agg")
try:
    import matplotlib
    # force=True also covers matplotlib having been imported before us.
    matplotlib.use("Agg", force=True)
except Exception:  # pragma: no cover - plotting simply stays as configured
    pass

import nimo
from fastmcp import Client, Context, FastMCP
from fastmcp.utilities.types import Image

from .paths import CANDIDATES_FILE, RESULTS_DIR, ensure_user_dirs


# Untouched copy of the candidates the session began with, kept beside the
# working candidates.csv (which is rewritten as results come in).
INITIAL_CANDIDATES = "candidates_initial.csv"


@contextlib.contextmanager
def suppress_stdout():
    """Temporarily redirect stdout to discard nimo's print output."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


class Method(str, Enum):
    """Every proposal algorithm nimo offers.

    RE/ES/DOE/BLOX/PDC search without a direction to optimize toward;
    PHYSBO optimizes toward one and honors the ``minimization`` flag;
    PTR targets an objective range given by ``ptr_lower`` / ``ptr_upper``.
    """
    RE = "RE"
    ES = "ES"
    DOE = "DOE"
    BLOX = "BLOX"
    PDC = "PDC"
    PHYSBO = "PHYSBO"
    PTR = "PTR"


# The directional subset — keep in sync with DEDICATED_METHODS in frontend.js
# and OPTIMIZATION_METHODS in planner.py.
OPTIMIZATION_METHODS = {"PHYSBO"}


class PlotMode(str, Enum):
    """Which way a progress curve should run.

    Spelled out rather than a boolean so the block and the agent read the same
    words as the selection block direction that produced the data.
    """
    MAXIMIZATION = "maximization"
    MINIMIZATION = "minimization"


# What each algorithm needs from candidates.csv before it can run. RE, ES and
# DOE are absent because they need no measurements (random / exhaustive /
# design of experiments). When a requirement is unmet the call falls back to RE
# rather than failing — nimo itself says "use RE for selection" in this case.
# Minimums mirror where each nimo ai_tool actually crashes below: BLOX needs 3
# because its RandomForest grid search uses 3-fold CV (ai_tool_blox.py, cv=3).
FALLBACK_METHOD = "RE"
# PTR needs 2: it fits a physbo GP per objective and resolves "min"/"max"
# range placeholders from the observed values.
_MIN_MEASURED = {"PDC": 1, "BLOX": 3, "PHYSBO": 1, "PTR": 2}
# PDC classifies phases, so a single observed value gives it nothing to
# separate; nimo reaches a sys.exit() in that state (ai_tool_pdc.py:126-128).
_NEEDS_TWO_PHASES = {"PDC"}


@dataclass
class CandidateStats:
    """How much of candidates.csv has been measured."""
    total: int
    measured: int
    unmeasured: int
    distinct: int


class NimoWrapper:
    """Stateful wrapper around the nimo library.

    Manages the candidates file, the session result directory, and optimization
    state (history, iteration counter).

    Typical lifecycle, spanning as many workflow runs as the user makes:
        1. User uploads candidates.csv via the UI.
        2. ``start_session()`` creates a timestamped directory, copies
           candidates.csv into it, and initializes nimo. Called once per
           session — at app startup, on upload, or from "New session".
        3. ``selection()`` / ``update()`` are called in a loop, across any
           number of workflow runs, all building on the same history.
        4. ``plot_history_best()`` may be called to plot progress.
    """

    def __init__(self):
        self.n_objectives = 1
        self.n_proposals = 1

        ensure_user_dirs()
        self.candidates_file = str(CANDIDATES_FILE)
        self.results_dir = str(RESULTS_DIR)
        os.makedirs(self.results_dir, exist_ok=True)

        self.parameter_names: List[str] = []
        self.objective_names: List[str] = []
        self.res_history = None
        self.iteration = 0
        self.run_dir: str = ""
        self.proposals_file: str = ""
        self.workflow_count = 0
        # Messages about the last tool call worth showing the user — currently
        # only algorithm substitutions. Cleared at the start of each call.
        self.notes: List[str] = []

        if os.path.isfile(self.candidates_file):
            self._load_parameters()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_parameters(self) -> None:
        """Read column headers from candidates.csv and derive column names.

        The last ``n_objectives`` columns are treated as objective values;
        everything before them is a parameter.
        """
        with open(self.candidates_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
        self.parameter_names = cols[: -self.n_objectives]
        self.objective_names = cols[-self.n_objectives:]

    def _init_from_candidates(self) -> None:
        """Start a new session folder from the master candidates.csv.

        1. Re-reads parameter names from the master candidates.csv.
        2. Creates a timestamped subdirectory under ``results/``.
        3. Copies the master candidates.csv into that directory.
        4. Points ``proposals_file`` at the session directory.
        5. Initializes nimo history and resets the iteration counter.
        """
        self._load_parameters()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = os.path.join(self.results_dir, ts)
        os.makedirs(self.run_dir, exist_ok=True)

        run_candidates = os.path.join(self.run_dir, "candidates.csv")
        shutil.copy2(self.candidates_file, run_candidates)
        # The working copy above is rewritten as results come in, so keep an
        # untouched one to show what the session started from.
        shutil.copy2(self.candidates_file,
                     os.path.join(self.run_dir, INITIAL_CANDIDATES))

        self.proposals_file = os.path.join(self.run_dir, "proposals.csv")

        with suppress_stdout():
            self.res_history = nimo.history(
                input_file=run_candidates,
                num_objectives=self.n_objectives,
            )
        self.iteration = 0
        self.workflow_count = 0

    def _run_candidates_file(self) -> str:
        """Return the path to candidates.csv in the current session directory."""
        return os.path.join(self.run_dir, "candidates.csv")

    def _candidate_stats(self) -> CandidateStats:
        """Count measured and unmeasured rows in the working candidates.csv.

        A row counts as measured when its last objective column holds a number;
        that is the same test nimo makes (it treats non-numeric cells as NaN).
        """
        total = measured = 0
        values = set()
        key = self.objective_names[-1] if self.objective_names else None
        with open(self._run_candidates_file(), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                total += 1
                if key is None:
                    continue
                try:
                    values.add(float(row[key]))
                except (TypeError, ValueError):
                    continue
                measured += 1
        return CandidateStats(total=total, measured=measured,
                              unmeasured=total - measured, distinct=len(values))

    def _resolve_method(self, method: str) -> str:
        """Check the data supports *method*, falling back to RE when it does not.

        Raises when nothing can be proposed at all — RE would be no better off.
        Any substitution is recorded in ``notes`` so it never happens silently.
        """
        name = str(getattr(method, "value", method))
        stats = self._candidate_stats()

        if stats.unmeasured < 1:
            raise RuntimeError(
                f"No candidates left to propose: all {stats.total} rows in "
                "candidates.csv already have a measured value.")

        def fall_back(reason: str) -> str:
            self.notes.append(f"{FALLBACK_METHOD} was used instead of {name}: {reason}.")
            return FALLBACK_METHOD

        need = _MIN_MEASURED.get(name, 0)
        if stats.measured < need:
            return fall_back(f"it needs at least {need} measured rows to learn "
                             f"from, found {stats.measured}")
        if name in _NEEDS_TWO_PHASES and stats.distinct < 2:
            return fall_back(f"it needs at least 2 distinct measured values to "
                             f"tell phases apart, found {stats.distinct}")
        return name

    def start_workflow(self) -> str:
        """Snapshot the candidates and reset per-workflow optimization state.

        History and the iteration counter restart so each workflow's plot shows
        that run's own progress, taking everything known so far as cycle 0.
        The two must move together: update() tags new rows with ``itt+1`` and
        plot_history_best() draws ``num_cycles=iteration``, so a fresh history
        under a stale counter would not line up.

        Returns:
            The snapshot's file name (not a full path), for the log and XML.
        """
        self.workflow_count += 1
        name = f"candidates_workflow_{self.workflow_count:02d}.csv"
        run_candidates = self._run_candidates_file()
        shutil.copy2(run_candidates, os.path.join(self.run_dir, name))

        with suppress_stdout():
            self.res_history = nimo.history(
                input_file=run_candidates,
                num_objectives=self.n_objectives,
            )
        self.iteration = 0
        return name

    def start_session(self) -> Dict[str, Any]:
        """Begin a new session: a fresh folder and a clean optimization state.

        A session spans however many workflow runs the user makes — they all
        share one candidates.csv and keep building on the same history, so this
        is deliberately *not* called per run. Re-reads the master
        candidates.csv, so a freshly uploaded file takes effect here.

        Returns:
            The new run directory and the candidates columns it was built from.
        """
        self._init_from_candidates()
        return self.get_session_info()

    def get_session_info(self) -> Dict[str, Any]:
        """Return the current run directory and the candidates columns.

        The controller renders these into the workflow XML and the report, and
        reads the run directory's files directly, so they have to be readable
        from outside this process.
        """
        return {"run_dir": self.run_dir,
                "parameters": list(self.parameter_names),
                "objectives": list(self.objective_names)}

    def get_candidate_stats(self) -> Dict[str, int]:
        """Return how much of candidates.csv has been measured."""
        stats = self._candidate_stats()
        return {"total": stats.total, "measured": stats.measured,
                "unmeasured": stats.unmeasured, "distinct": stats.distinct}

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def get_parameter_names(self) -> List[str]:
        """Return the input parameter names derived from candidates.csv.

        Returns an empty list if no candidates file has been loaded yet.
        """
        return self.parameter_names

    def _select(self, method: str, minimization: Optional[bool] = None,
                ptr_ranges: Optional[list] = None) -> Dict[str, float]:
        """Run one nimo selection and return the resulting proposal.

        The method is vetted against the data first — several algorithms crash
        or sys.exit() deep inside nimo when there is nothing to learn from, so
        they are swapped for RE instead (recorded in ``notes``).

        ``minimization`` / ``ptr_ranges`` are left as None for the methods
        that do not use them; nimo would ignore them anyway, but omitting them
        keeps the call honest about which methods use what.
        """
        self.notes = []
        method = self._resolve_method(method)
        if method == FALLBACK_METHOD:
            ptr_ranges = None                 # the fallback takes none
        with suppress_stdout():
            nimo.selection(
                method=method,
                input_file=self._run_candidates_file(),
                output_file=self.proposals_file,
                num_objectives=self.n_objectives,
                num_proposals=self.n_proposals,
                minimization=minimization,
                ptr_ranges=ptr_ranges,
            )
        return self.get_proposal()

    async def selection(self, method: Method, minimization: bool = False,
                        ptr_lower: Optional[float] = None,
                        ptr_upper: Optional[float] = None,
                        ctx: Optional[Context] = None) -> Dict[str, float]:
        """Propose the next experimental parameters.

        Runs the specified algorithm on the current candidates and writes the
        result to proposals.csv in the session directory.

        Args:
            method: Algorithm to use. RE, ES, DOE, BLOX and PDC search without
                a direction to optimize toward; PHYSBO optimizes the
                objective; PTR proposes candidates whose predicted objective
                falls in a target range.
            minimization: True to optimize toward a smaller objective, False
                for a larger one. Only meaningful for PHYSBO.
            ptr_lower: Lower bound of the PTR target range. Omitted means
                unbounded below. Only meaningful for PTR.
            ptr_upper: Upper bound of the PTR target range. Omitted means
                unbounded above. Only meaningful for PTR.

        Returns:
            A dictionary mapping each parameter name to its proposed value.
        """
        name = str(getattr(method, "value", method))
        if name in OPTIMIZATION_METHODS:
            call = lambda: self._select(  # noqa: E731
                method, minimization=bool(minimization))
        elif name == "PTR":
            # nimo resolves the "min" / "max" placeholders to the observed
            # extremes, i.e. an omitted bound does not constrain that side.
            ranges = [[ptr_lower if ptr_lower is not None else "min",
                       ptr_upper if ptr_upper is not None else "max"]]
            call = lambda: self._select(method, ptr_ranges=ranges)  # noqa: E731
        else:
            call = lambda: self._select(method)  # noqa: E731

        # Off the event loop: a GP fit holds the interpreter for seconds, and
        # the transport still has to answer pings and cancellations.
        proposal = await asyncio.to_thread(call)
        # A silent method substitution would be misleading, and the return
        # value is the proposal itself — so it travels as an MCP log message,
        # which the controller shows beside the step.
        if ctx is not None:
            for note in self.notes:
                await ctx.info(note)
        return proposal

    def get_proposal(self) -> Dict[str, float]:
        """Return the most recently proposed parameter values.

        Reads the first row of proposals.csv in the current run directory.

        Returns:
            A dictionary mapping each parameter name to its value.
        """
        with open(self.proposals_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            row = next(reader)
        return {k: float(row[k]) for k in self.parameter_names}

    def update(self, objs: float) -> str:
        """Record an experimental result and advance the optimization.

        Writes the objective value back into the run's candidates.csv
        and updates the nimo history.

        Args:
            objs: The observed objective value for the latest proposal.

        Returns:
            A confirmation message.
        """
        run_candidates = self._run_candidates_file()
        with suppress_stdout():
            nimo.output_update(
                input_file=self.proposals_file,
                output_file=run_candidates,
                num_objectives=self.n_objectives,
                objective_values=[objs],
            )
            self.res_history = nimo.history(
                input_file=run_candidates,
                num_objectives=self.n_objectives,
                itt=self.iteration,
                history_file=self.res_history,
            )
        self.iteration += 1
        return "Updated experimental results."

    def plot_history_best(self, mode: PlotMode = PlotMode.MAXIMIZATION) -> Image:
        """Plot the best objective value found so far across iterations.

        Args:
            mode: "maximization" to track the running highest value,
                "minimization" the running lowest. Set it to match the
                direction of the selection that produced the data, or the
                curve runs the wrong way.

        Returns: Image file of the plot.
        """
        filename = "best_objective.png"
        # nimo takes a boolean; the comparison works whether mode arrives as the
        # enum member or as the plain string a tool call delivers.
        with suppress_stdout():
            nimo.visualization.plot_history.best(
                input_file=self.res_history,
                num_cycles=self.iteration,
                fig_folder=self.run_dir,
                filename=filename,
                minimization=(mode == PlotMode.MINIMIZATION),
            )
        return Image(path=os.path.join(self.run_dir, filename))

    def plot_convex_hull(self) -> Image:
        """Plot how the convex hull area of the proposed parameters grew.

        The area covered by all parameter points observed so far, per cycle —
        a diversity measure for selection algorithms such as BLOX. Only
        available when there are exactly two parameters.

        Returns: Image file of the plot.
        """
        filename = "convex_hull.png"
        with suppress_stdout():
            nimo.visualization.plot_history.convex_hull(
                input_file=self.res_history,
                num_cycles=self.iteration,
                fig_folder=self.run_dir,
                filename=filename,
            )
        return Image(path=os.path.join(self.run_dir, filename))

    def plot_distribution(self) -> Image:
        """Plot the distribution of the measured objective values.

        A histogram for a single objective (a scatter for two, 3D scatter for
        three). Reads the measured rows of the current run's candidates.csv.

        Returns: Image file of the plot.
        """
        filename = "distribution.png"
        with suppress_stdout():
            nimo.visualization.plot_distribution.plot(
                input_file=self._run_candidates_file(),
                num_objectives=self.n_objectives,
                fig_folder=self.run_dir,
                filename=filename,
            )
        return Image(path=os.path.join(self.run_dir, filename))

    def plot_phase_diagram(self) -> Image:
        """Plot the constructed phase diagram for the current run.

        Returns: Image file of the plot.
        """
        filename_diagram = "phase_diagram.png"
        with suppress_stdout():
            nimo.visualization.plot_phase_diagram.plot(
                input_file=self._run_candidates_file(),
                fig_folder=self.run_dir,
                filename_diagram=filename_diagram
            )
        return Image(path=os.path.join(self.run_dir, filename_diagram))


# Module-level singleton used for direct in-process calls.
wrapper = NimoWrapper()

# FastMCP object — the server behind `python -m nimo_controller.nimo_mcp`.
#
# The second group is session plumbing the controller drives: it has to be
# reachable over the transport, but neither the Blockly toolbox nor the chat
# agent should offer it, so those names are also listed in NIMO_HARDCODED
# (planner.py) and its frontend.js twin, which keeps them off the toolbox and
# out of the planner's catalog.
mcp = FastMCP("NIMO MCP controller")
mcp.tool(wrapper.get_parameter_names)
mcp.tool(wrapper.selection)
mcp.tool(wrapper.update)
mcp.tool(wrapper.plot_history_best)
mcp.tool(wrapper.plot_convex_hull)
mcp.tool(wrapper.plot_distribution)
mcp.tool(wrapper.plot_phase_diagram)

mcp.tool(wrapper.start_session)
mcp.tool(wrapper.start_workflow)
mcp.tool(wrapper.get_session_info)
mcp.tool(wrapper.get_candidate_stats)
mcp.tool(wrapper.get_proposal)


async def _run_stdio(proto_fd: int, log_level: str = "WARNING") -> None:
    """Serve on stdio, writing frames to *proto_fd* rather than sys.stdout.

    FastMCP's own stdio runner takes the protocol stream from sys.stdout,
    which here deliberately points at stderr so nimo's prints are harmless.
    This mirrors run_stdio_async() with the stream passed in instead.
    """
    import logging
    from io import TextIOWrapper

    import anyio
    from mcp.server.stdio import stdio_server

    # The parent shows this server's stderr, so keep it to what it needs to
    # hear; per-message tracing is not that.
    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.WARNING),
                        stream=sys.stderr, force=True)

    stdout = anyio.wrap_file(TextIOWrapper(os.fdopen(proto_fd, "wb"),
                                           encoding="utf-8", line_buffering=True))
    server = mcp._mcp_server
    async with mcp._lifespan_manager():
        async with stdio_server(stdout=stdout) as (read_stream, write_stream):
            await server.run(read_stream, write_stream,
                             server.create_initialization_options())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NIMO MCP Server (standalone)")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "streamable-http"],
        help="Transport to use (default: stdio)",
    )
    parser.add_argument("--host", default="localhost", help="HTTP bind host (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port (default: 8000)")
    parser.add_argument("--path", default="/mcp", help="HTTP mount path (default: /mcp)")
    parser.add_argument("--log-level", default="WARNING",
                        help="Log level (default: WARNING)")
    args = parser.parse_args()

    if args.transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            path=args.path,
            log_level=args.log_level,
        )
    else:
        # _PROTO_FD is the real stdout, saved before nimo could print to it.
        assert _PROTO_FD is not None
        asyncio.run(_run_stdio(_PROTO_FD, args.log_level))
