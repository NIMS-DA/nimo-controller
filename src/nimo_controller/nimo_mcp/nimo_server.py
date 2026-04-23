"""NIMO MCP Server — exposes the NIMO optimization library as MCP tools.

This module wraps the nimo library in a FastMCP server so that the
NIMO Controller UI can call optimization routines via MCP.

Directory layout (managed automatically):

    nimo_mcp/
    ├── nimo_server.py      # this file
    ├── candidates.csv      # master candidates uploaded by the user
    └── results/
        └── <timestamp>/    # one directory per workflow run
            ├── candidates.csv   # working copy (updated by nimo)
            └── proposals.csv    # proposals generated during the run
"""

import contextlib
import csv
import io
import os
import shutil
from datetime import datetime
import sys

import nimo

from enum import Enum
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from typing import Dict, List

from ..paths import CANDIDATES_FILE, RESULTS_DIR, ensure_user_dirs

@contextlib.contextmanager
def suppress_stdout():
    """Temporarily redirect stdout to discard nimo's print output."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


class SelectionMethod(str, Enum):
    """Supported parameter selection algorithms."""
    RE = "RE"
    PDC = "PDC"
    PHYSBO = "PHYSBO"


class NimoWrapper:
    """Stateful wrapper around the nimo library.

    Manages the candidates file, per-run result directories, and
    optimization state (history, iteration counter).

    Typical lifecycle for a single workflow run:
        1. User uploads candidates.csv via the UI.
        2. ``reinitialize()`` is called at workflow start — creates a
           timestamped run directory, copies candidates.csv into it,
           and initializes nimo.
        3. ``selection()`` / ``update()`` are called in a loop.
        4. ``visualize_best()`` may be called to plot progress.
    """

    def __init__(self):
        self.n_objectives = 1
        self.n_proposals = 1

        ensure_user_dirs()
        self.candidates_file = str(CANDIDATES_FILE)
        self.results_dir = str(RESULTS_DIR)
        os.makedirs(self.results_dir, exist_ok=True)

        self.parameter_names: List[str] = []
        self.res_history = None
        self.iteration = 0
        self.run_dir: str = ""
        self.proposals_file: str = ""

        if os.path.isfile(self.candidates_file):
            self._load_parameters()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_parameters(self) -> None:
        """Read column headers from candidates.csv and derive parameter names.

        The last ``n_objectives`` columns are treated as objective values;
        everything before them is a parameter.
        """
        with open(self.candidates_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
        self.parameter_names = cols[: -self.n_objectives]

    def _init_from_candidates(self) -> None:
        """Start a new optimization run.

        1. Re-reads parameter names from the master candidates.csv.
        2. Creates a timestamped subdirectory under ``results/``.
        3. Copies the master candidates.csv into that directory.
        4. Points ``proposals_file`` at the run directory.
        5. Initializes nimo history and resets the iteration counter.
        """
        self._load_parameters()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = os.path.join(self.results_dir, ts)
        os.makedirs(self.run_dir, exist_ok=True)

        run_candidates = os.path.join(self.run_dir, "candidates.csv")
        shutil.copy2(self.candidates_file, run_candidates)

        self.proposals_file = os.path.join(self.run_dir, "proposals.csv")

        with suppress_stdout():
            self.res_history = nimo.history(
                input_file=run_candidates,
                num_objectives=self.n_objectives,
            )
        self.iteration = 0

    def _run_candidates_file(self) -> str:
        """Return the path to candidates.csv in the current run directory."""
        return os.path.join(self.run_dir, "candidates.csv")

    # ------------------------------------------------------------------
    # MCP tools
    # ------------------------------------------------------------------

    def get_parameter_names(self) -> List[str]:
        """Return the input parameter names derived from candidates.csv.

        Returns an empty list if no candidates file has been loaded yet.
        """
        return self.parameter_names

    def reinitialize(self) -> str:
        """Create a new run directory and reset optimization state.

        This is called automatically at the start of each workflow
        execution.  It re-reads the master candidates.csv, so any
        changes (e.g. a freshly uploaded file) take effect immediately.

        Returns:
            A confirmation message listing the parameter names.
        """
        self._init_from_candidates()
        return f"Reinitialized with parameters: {self.parameter_names}"

    def selection(self, method: SelectionMethod) -> Dict[str, float]:
        """Select the next experimental parameters.

        Runs the specified optimization algorithm on the current
        candidates and writes the result to proposals.csv in the
        run directory.

        Args:
            method: Algorithm to use (RE, PDC, or PHYSBO).

        Returns:
            A dictionary mapping each parameter name to its proposed value.
        """
        with suppress_stdout():
            nimo.selection(
                method=method,
                input_file=self._run_candidates_file(),
                output_file=self.proposals_file,
                num_objectives=self.n_objectives,
                num_proposals=self.n_proposals,
            )
        return self.get_proposal()

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

    def plot_history_best(self) -> Image:
        """Plot the best objective value found so far across iterations.

        Returns: Image file of the plot.
        """
        filename = "best_objective.png"
        with suppress_stdout():
            nimo.visualization.plot_history.best(
                input_file=self.res_history,
                num_cycles=self.iteration,
                fig_folder=self.run_dir,
                filename=filename
            )
        return Image(path=os.path.join(self.run_dir, filename))
    
    def plot_phase_diagram(self) -> Image:
        """Plot the best objective value found so far across iterations.

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

wrapper = NimoWrapper()

mcp = FastMCP("NIMO MCP controller")
mcp.tool(wrapper.get_parameter_names)
mcp.tool(wrapper.reinitialize)
mcp.tool(wrapper.selection)
mcp.tool(wrapper.get_proposal)
mcp.tool(wrapper.update)
mcp.tool(wrapper.plot_history_best)
mcp.tool(wrapper.plot_phase_diagram)

if __name__ == "__main__":
    mcp.run(transport="stdio", log_level="DEBUG")