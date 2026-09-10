"""Evaluate planner accuracy per LLM over the eval_data tasks.

Each task lives in eval_data/<task_name>/
- expected.xml: expected workflow XML
- catalog.json: tool and candidate catalog for that task.
- instruction*.txt: natural language instruction for the task
For every model listed in MODELS below, each instruction is converted into workflow XML
once per seed in SEEDS, and the behavior of the generated XML is judged against
expected.xml. Two tables are printed: per-task o/x (one character per seed) and,
per instruction kind (detailed, succinct, ...), the mean number of tasks solved
per seed. Generated XML and .trace files are kept in each task's own
eval_data/<task_name>/results/.
"""

import asyncio
import json
import re
import sys
import traceback
from pathlib import Path

from pydantic_ai.exceptions import UnexpectedModelBehavior

from compare_xml import trace_lines, trace_of
from generate_clairify import generate as generate_clairify
from generate_xml import generate as generate_pydantic

# -- configuration: edit here, no command-line arguments ----------------------
MODELS = ["gpt-oss:120b", "gpt-oss:20b", "qwen3.6:35b", "qwen3.6:27b", "deepseek-r1:32b", "deepseek-r1:14b", "gemma4:31b", "gemma4:e4b"]  # model names to evaluate
# Generation methods to compare: name -> generate function (one per module).
METHODS = {"pydantic": generate_pydantic, "clairify": generate_clairify}
PROVIDER = None           # None = agent.provider from config.yaml, or openai
BASE_URL = None           # None = agent.base_url from config.yaml
THINKING = None           # None = the model's own default
TEMPERATURE = None        # fixed for reproducibility; None = model's default
SEEDS: list[int] = [0]   # one generation per seed; formatting slips
                                  # are (prompt, seed) luck, so sample several
TASKS: list[str] = []     # eval_data folder names to run, e.g. ["task00_ackley"]; empty = all
TIMEOUT_S: float | None = 120   # limit per generation in seconds; None = no limit
DATA_DIR = Path(__file__).parent / "eval_data"
# -----------------------------------------------------------------------------


def tasks() -> list[tuple[str, Path, Path]]:
    """(task label, instruction file, case dir) for every instruction*.txt."""
    cases = sorted(p for p in DATA_DIR.iterdir() if p.is_dir())
    if TASKS:
        known = {c.name for c in cases}
        missing = [t for t in TASKS if t not in known]
        if missing:
            sys.exit(f"unknown task(s) in TASKS: {', '.join(missing)} "
                     f"(available: {', '.join(sorted(known))})")
        cases = [c for c in cases if c.name in TASKS]
    out = []
    for case in cases:
        for instruction in sorted(case.glob("instruction*.txt")):
            out.append((f"{case.name}/{instruction.stem}", instruction, case))
    return out


def _save(path: Path, xml: str, xml_too: bool = True) -> None:
    """Write <path>.trace (and, unless xml_too=False, the XML itself)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if xml_too:
        path.write_text(xml, encoding="utf-8")
    trace = trace_lines(trace_of(xml))
    Path(str(path) + ".trace").write_text("\n".join(trace) + "\n",
                                          encoding="utf-8")


def main() -> None:
    # (case, instruction stem, column, seed) -> "o"/"x"/"E"/"T"
    results: dict[tuple[str, str, str, int], str] = {}
    for model in MODELS:
        for method, generate in METHODS.items():
            column = f"{model} ({method})"
            for task, instruction_file, case in tasks():
                for seed in SEEDS:
                    key = (case.name, instruction_file.stem, column, seed)
                    label = f"{task} seed={seed}"
                    print(f"=== {column} : {label} ===", file=sys.stderr)
                    expected_xml = (case / "expected.xml").read_text(encoding="utf-8")
                    # expected.xml itself already lives one level up — trace only.
                    _save(case / "results" / "expected.xml", expected_xml,
                          xml_too=False)
                    out_dir = (case / "results"
                               / re.sub(r"[^\w.-]", "_", model) / method)
                    out_path = out_dir / f"{instruction_file.stem}.seed{seed}.xml"
                    catalog_data = json.loads(
                        (case / "catalog.json").read_text(encoding="utf-8"))
                    try:
                        xml = generate(
                            instruction_file.read_text(encoding="utf-8"),
                            catalog_data, model, PROVIDER, BASE_URL,
                            THINKING, TEMPERATURE, seed,
                            timeout=TIMEOUT_S,
                            attempts_log=str(out_path) + ".attempts.log")
                    except asyncio.TimeoutError:
                        results[key] = "T"
                        print(f"[eval] {label}: TIMEOUT ({TIMEOUT_S}s)",
                              file=sys.stderr)
                        continue
                    except UnexpectedModelBehavior as e:
                        # Expected failure mode (the planner ran out of
                        # retries) — one line, not a traceback.
                        results[key] = "E"
                        print(f"[eval] {label}: ERROR — {e}\n"
                              f"       details: {out_path}.attempts.log",
                              file=sys.stderr)
                        continue
                    except (Exception, SystemExit):  # build_model exits via sys.exit
                        traceback.print_exc()
                        results[key] = "E"
                        continue
                    _save(out_path, xml)
                    # Judge from the saved artifact, so the verdict can always
                    # be reproduced from the files on disk.
                    ok = (trace_of(out_path.read_text(encoding="utf-8"))
                          == trace_of(expected_xml))
                    results[key] = "o" if ok else "x"
                    print(f"[eval] {label}: {'pass' if ok else 'FAIL'}",
                          file=sys.stderr)

    # Score table: one row per task, one column per (model, method) under a
    # spanning model header. A cell holds one number per instruction kind —
    # how many seeds passed — and the last row averages tasks solved per seed.
    instrs_by_case: dict[str, list[str]] = {}
    for _, instruction_file, case in tasks():
        instrs_by_case.setdefault(case.name, []).append(instruction_file.stem)
    case_names = list(instrs_by_case)
    groups = sorted({i for instrs in instrs_by_case.values() for i in instrs})
    short = {g: g.removeprefix("instruction_") or g for g in groups}
    pairs = [(model, method) for model in MODELS for method in METHODS]

    def col(model: str, method: str) -> str:
        return f"{model} ({method})"

    def score(case_name: str, column: str) -> str:
        """Seeds passed per instruction kind, e.g. '5|4'."""
        return "|".join(
            str(sum(results.get((case_name, g, column, s)) == "o" for s in SEEDS))
            if g in instrs_by_case[case_name] else "-"
            for g in groups)

    def average(column: str) -> str:
        """Mean tasks solved per seed, per instruction kind."""
        out = []
        for g in groups:
            cases = [cn for cn, instrs in instrs_by_case.items() if g in instrs]
            per_seed = [sum(results.get((cn, g, column, s)) == "o"
                            for cn in cases) for s in SEEDS]
            out.append(f"{sum(per_seed) / len(per_seed):.2f}")
        return "|".join(out)

    rows = [(cn, {col(m, mt): score(cn, col(m, mt)) for m, mt in pairs})
            for cn in case_names]
    avg = {col(m, mt): average(col(m, mt)) for m, mt in pairs}
    label_w = max(len(cn) for cn in case_names + ["average score"]) + 2
    col_w = {col(m, mt): max([len(mt), len(avg[col(m, mt)])]
                             + [len(cells[col(m, mt)]) for _, cells in rows])
             for m, mt in pairs}
    # A model name wider than its methods put together stretches its last column.
    span = {}
    for m in MODELS:
        cols = [col(m, mt) for mt in METHODS]
        width = sum(col_w[c] for c in cols) + 2 * (len(cols) - 1)
        if len(m) > width:
            col_w[cols[-1]] += len(m) - width
            width = len(m)
        span[m] = width

    def line(label: str, cells: dict[str, str]) -> str:
        return label.ljust(label_w) + "  ".join(
            cells[col(m, mt)].ljust(col_w[col(m, mt)]) for m, mt in pairs)

    print(f"{len(SEEDS)} seeds ({', '.join(map(str, SEEDS))}); "
          f"cell: seeds passed, {'|'.join(short[g] for g in groups)}; "
          f"average score: tasks solved per seed, out of {len(case_names)}")
    print("task".ljust(label_w) + "  ".join(m.ljust(span[m]) for m in MODELS))
    print(" " * label_w + "  ".join(mt.ljust(col_w[col(m, mt)])
                                    for m, mt in pairs))
    for cn, cells in rows:
        print(line(cn, cells))
    print("-" * (label_w + sum(col_w.values()) + 2 * (len(pairs) - 1)))
    print(line("average score", avg))


if __name__ == "__main__":
    main()