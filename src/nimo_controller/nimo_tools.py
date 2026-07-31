"""NIMO tools — in-process optimization backend for the NIMO Controller.

This module bundles two things that used to live under ``nimo_mcp/``:

* ``NimoWrapper`` — a plain-Python, stateful wrapper around the ``nimo``
  optimization library (candidates file, per-run result dirs, history).
* ``LocalNimoClient`` — a thin adapter that presents the small subset of the
  FastMCP ``Client`` interface ``server.py`` uses, but backed by direct calls
  into the ``NimoWrapper`` singleton. No MCP server (HTTP subprocess or
  persistent in-process client) is run for tool execution.

A FastMCP ``mcp`` object is still built so tool *schemas* can be snapshotted
once at startup (``snapshot_nimo_tools``) — keeping ``/tools`` output identical
to before — and so the module can optionally be run as a standalone MCP server
(``python -m nimo_controller.nimo_tools``).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

# NIMO's plot tools run on a worker thread (LocalNimoClient.call_tool hands
# every call to asyncio.to_thread), but matplotlib's default backend here is a
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
from fastmcp import Client, FastMCP
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


class SelectionMethod(str, Enum):
    """Search algorithms with no notion of a direction to optimize toward."""
    RE = "RE"
    ES = "ES"
    DOE = "DOE"
    BLOX = "BLOX"
    PDC = "PDC"


class OptimizationMethod(str, Enum):
    """Algorithms that optimize toward a direction (nimo's ``minimization``)."""
    PHYSBO = "PHYSBO"
    NTS = "NTS"


class PlotMode(str, Enum):
    """Which way a progress curve should run.

    Spelled out rather than a boolean so the block and the agent read the same
    words as the maximization / minimization blocks that produced the data.
    """
    MAXIMIZATION = "maximization"
    MINIMIZATION = "minimization"


# What each algorithm needs from candidates.csv before it can run. RE, ES and
# DOE are absent because they need no measurements (random / exhaustive /
# design of experiments). When a requirement is unmet the call falls back to RE
# rather than failing — nimo itself says "use RE for selection" in this case.
FALLBACK_METHOD = "RE"
_NEEDS_MEASURED = {"PDC", "BLOX", "PHYSBO", "NTS"}
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

        if name in _NEEDS_MEASURED and stats.measured < 1:
            return fall_back("it needs measured data to learn from, and no rows "
                             "have been measured yet")
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

    def start_session(self) -> str:
        """Begin a new session: a fresh folder and a clean optimization state.

        A session spans however many workflow runs the user makes — they all
        share one candidates.csv and keep building on the same history, so this
        is deliberately *not* called per run. Re-reads the master
        candidates.csv, so a freshly uploaded file takes effect here.

        Returns:
            A confirmation message listing the parameter names.
        """
        self._init_from_candidates()
        return f"New session with parameters: {self.parameter_names}"

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def get_parameter_names(self) -> List[str]:
        """Return the input parameter names derived from candidates.csv.

        Returns an empty list if no candidates file has been loaded yet.
        """
        return self.parameter_names

    def _select(self, method: str, minimization: Optional[bool] = None) -> Dict[str, float]:
        """Run one nimo selection and return the resulting proposal.

        The method is vetted against the data first — several algorithms crash
        or sys.exit() deep inside nimo when there is nothing to learn from, so
        they are swapped for RE instead (recorded in ``notes``).

        ``minimization`` is left as None for the direction-less algorithms; nimo
        treats None as False, so passing it either way would be harmless, but
        omitting it keeps the call honest about which methods use it.
        """
        self.notes = []
        method = self._resolve_method(method)
        with suppress_stdout():
            nimo.selection(
                method=method,
                input_file=self._run_candidates_file(),
                output_file=self.proposals_file,
                num_objectives=self.n_objectives,
                num_proposals=self.n_proposals,
                minimization=minimization,
            )
        return self.get_proposal()

    def selection(self, method: SelectionMethod) -> Dict[str, float]:
        """Select the next experimental parameters by search, without optimizing.

        Runs the specified algorithm on the current candidates and writes the
        result to proposals.csv in the session directory. These algorithms do
        not optimize toward a direction — use maximization or minimization for
        that.

        Args:
            method: Algorithm to use (RE, ES, DOE, BLOX or PDC).

        Returns:
            A dictionary mapping each parameter name to its proposed value.
        """
        return self._select(method)

    def maximization(self, method: OptimizationMethod) -> Dict[str, float]:
        """Propose the next parameters, optimizing toward a larger objective.

        Args:
            method: Algorithm to use (PHYSBO or NTS).

        Returns:
            A dictionary mapping each parameter name to its proposed value.
        """
        return self._select(method, minimization=False)

    def minimization(self, method: OptimizationMethod) -> Dict[str, float]:
        """Propose the next parameters, optimizing toward a smaller objective.

        Args:
            method: Algorithm to use (PHYSBO or NTS).

        Returns:
            A dictionary mapping each parameter name to its proposed value.
        """
        return self._select(method, minimization=True)

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
                maximization / minimization block that produced the data, or
                the curve runs the wrong way.

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

# FastMCP object — used only to snapshot tool schemas at startup and to allow
# running this module as a standalone MCP server (see __main__ below).
#
# Deliberately absent, so neither the Blockly toolbox nor the chat agent sees them:
#   start_session — when a session begins is the app's decision, not the model's
#   get_proposal  — plumbing that reads one row of proposals.csv; only meaningful
#                   immediately after selection()
# Both stay callable in-process: LocalNimoClient.call_tool resolves by getattr,
# not through this registration, so _resolve_nimo_vars still works.
mcp = FastMCP("NIMO MCP controller")
mcp.tool(wrapper.get_parameter_names)
mcp.tool(wrapper.selection)
mcp.tool(wrapper.maximization)
mcp.tool(wrapper.minimization)
mcp.tool(wrapper.update)
mcp.tool(wrapper.plot_history_best)
mcp.tool(wrapper.plot_phase_diagram)


# ---------------------------------------------------------------------------
# In-process client adapter (replaces the FastMCP Client for NIMO)
# ---------------------------------------------------------------------------

@dataclass
class _ToolMeta:
    """Mimics a FastMCP tool object for ``list_tools()`` consumers."""
    name: str
    description: str
    inputSchema: dict
    outputSchema: Any = None
    execution: Any = None  # _attr(t, "execution") -> None -> taskSupport "forbidden"


@dataclass
class _Result:
    """Mimics a FastMCP call result: ``.data`` plus optional image ``.content``.

    ``notes`` has no MCP counterpart — it carries wrapper messages (currently
    algorithm substitutions) out to the caller so they can reach the log.
    """
    data: Any = None
    content: Optional[list] = None
    notes: Optional[list] = None


class LocalNimoClient:
    """Direct-call replacement for the NIMO FastMCP ``Client``.

    Implements just the surface ``server.py`` uses: ``call_tool``,
    ``list_tools``, ``initialize_result`` and the async-context-manager
    protocol. Never advertises task capability (``initialize_result = None``),
    so the workflow engine falls through to the plain call path for NIMO.
    """

    initialize_result = None  # -> _supports_tasks(client) returns False

    def __init__(self, wrapper: Any, tools: list[_ToolMeta]):
        self._wrapper = wrapper
        self._tools = tools

    async def __aenter__(self) -> "LocalNimoClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def list_tools(self) -> list[_ToolMeta]:
        return self._tools

    async def call_tool(self, name: str, args: Optional[dict] = None, task: bool = False) -> _Result:
        fn = getattr(self._wrapper, name, None)
        if not callable(fn):
            raise KeyError(f"Unknown NIMO tool: {name}")
        # NIMO methods are synchronous (and some are slow) — run off the event loop.
        result = await asyncio.to_thread(fn, **(args or {}))
        # Taken, not copied, so a note can never carry over to the next call.
        notes = self._wrapper.notes or None
        self._wrapper.notes = []

        # Plot tools return a fastmcp Image; convert to MCP-style image content
        # so server.extract_images() can read it unchanged.
        if isinstance(result, Image):
            ic = await asyncio.to_thread(result.to_image_content)
            return _Result(data=None, notes=notes, content=[{
                "type": "image",
                "data": getattr(ic, "data", ""),
                "mimeType": getattr(ic, "mimeType", "image/png"),
            }])
        return _Result(data=result, notes=notes)


async def snapshot_nimo_tools(mcp_obj: Any = mcp) -> list[_ToolMeta]:
    """Read the NIMO tool schemas once via a transient in-memory FastMCP client.

    This does NOT start a server or subprocess — it is an in-process handshake
    against the already-built ``mcp`` object — and yields the same schema data
    the old ``client_nimo.list_tools()`` produced.
    """
    async with Client(mcp_obj) as c:
        tools = await c.list_tools()
        return [
            _ToolMeta(
                name=t.name,
                description=t.description or "",
                inputSchema=t.inputSchema,
                outputSchema=getattr(t, "outputSchema", None),
            )
            for t in tools
        ]


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
    parser.add_argument("--log-level", default="INFO", help="Log level (default: INFO)")
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
        mcp.run(transport="stdio", log_level=args.log_level)
