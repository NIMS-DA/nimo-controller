# Planner evaluation

Measures how well the planner turns a natural-language instruction into a
workflow: each generated workflow is compared against a reference by
**behavior**, not by structure, so a 10-round loop with a mid-way `<if>` and
two 5-round loops count as the same answer.

## Files

| File | Role |
| --- | --- |
| `build_catalog.py` | Snapshot the tool catalog (nimo built-ins + MCP servers) and the candidate parameters to JSON |
| `generate_xml.py` | Generate a workflow with the app's structured-output planner (`pydantic` method) |
| `generate_clairify.py` | Generate a workflow with the CLAIRify-style spec-prompted XML planner (`clairify` method) |
| `compare_xml.py` | Judge two nimo-workflow XMLs behaviorally; writes `.trace` files for diffing |
| `run_eval.py` | Run every model × method × task and print the result matrix |
| `eval_data/<task>/` | One evaluation task: `catalog.json`, `instruction*.txt`, `expected.xml` |
| `testcases/`, `test_compare_xml.py` | Tests that compare_xml itself judges correctly |

## 1. Build the catalog for a task

The MCP servers must be running while this step runs; everything afterwards
works offline from the JSON.

```bash
uv run python evaluation/build_catalog.py evaluation/eval_data/task01_ackley_re/catalog.json --candidates example_mcp/optimization/candidates.csv --mcp ackley=http://127.0.0.1:8001/mcp
```

- `--candidates` — the CSV whose header defines the parameters (all columns but the last) and the objective (the last column).
- `--mcp NAME=URL` — repeatable. Omit it to use `mcp_servers` from `config.yaml`.

## 2. Define the task

Put these in `eval_data/<task>/`:

- `catalog.json` — from step 1.
- `instruction*.txt` — one file per phrasing (e.g. `instruction_detailed.txt`,
  `instruction_succinct.txt`, `instruction_ja.txt`). Each is evaluated separately.
- `expected.xml` — the reference workflow in nimo-workflow XML. Only the
  `<workflow>` element matters for judging; `<servers>` and `<candidates>` are
  run metadata and ignored.

Write instructions that pin down the behavior (cycle counts, which method,
where the switch happens). Ambiguous wording makes several different workflows
correct, which the comparison cannot express.

## 3. Run the evaluation

`run_eval.py` takes no arguments — edit the constants at the top of the file
(`MODELS`, `METHODS`, `SEEDS`, `TASKS`, `PROVIDER`, `BASE_URL`, `THINKING`,
`TEMPERATURE`, `TIMEOUT_S`). Every task is generated once per seed, so listing
several seeds shows how much of a result is luck.

```bash
uv run python evaluation/run_eval.py
```

Progress goes to stderr; the tables go to stdout, so `2>nul` (PowerShell:
`2>$null`) leaves just the report:

```
seeds: 1, 2, 3   cell: detailed|succinct, one char per seed (o=pass x=wrong E=error T=timeout)
task               gpt-oss:20b (pydantic)  gpt-oss:20b (clairify)
task01_ackley_re   ooo|oxo                 ooo|ooo
task02_ackley_doe  oxo|xxx                 ooo|oxx

avg detailed       1.67/2                  2.00/2
avg succinct       0.67/2                  1.33/2
pass total         13/24 (54.17%)          17/24 (70.83%)
```

One row per task; a cell holds one group per instruction file (in the header's
order, separated by `|`) and one character per seed inside a group: `o` behaves
like `expected.xml`, `x` differs, `E` generation failed, `T` timed out. The
`avg <kind>` rows are the mean number of tasks solved per seed for that
instruction kind, and `pass total` counts every generation.

Artifacts are written to `eval_data/<task>/results/`: `expected.xml.trace` plus
`<model>/<method>/<instruction>.seed<N>.xml`, its `.trace`, and an
`.attempts.log` of the model's retries.

## 4. Investigate a failure

The `.trace` files are the flattened tool-call sequences the verdict is based
on — one call per line, so a plain diff shows what actually differs:

```bash
diff evaluation/eval_data/task01_ackley_re/results/gpt-oss_20b/pydantic/instruction_detailed.seed42.xml.trace evaluation/eval_data/task01_ackley_re/results/expected.xml.trace
```

To retry a single case without running the whole matrix, both generators have
the same CLI (`--catalog`, `--model`, `--provider`, `--base-url`, `--thinking`,
`--output`; the instruction is a positional argument or stdin):

```bash
uv run python evaluation/generate_clairify.py "Minimize the Ackley function in 10 cycles." --catalog evaluation/eval_data/task01_ackley_re/catalog.json --model gpt-oss:20b
```

Compare any two XMLs directly with:

```bash
uv run python evaluation/compare_xml.py generated.xml expected.xml
```

## Testing the judge

`compare_xml` decides every verdict, so it has its own tests. Files in
`testcases/` follow a naming convention that is the expectation:
`<n>_<name>.xml` is a base workflow, `<n>_<name>_eq_<reason>.xml` must behave
the same as its base, `<n>_<name>_ne_<reason>.xml` must not. Adding a test case
is dropping in a file.

```bash
uv run pytest evaluation/ -q
```
