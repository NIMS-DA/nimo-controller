"""Session report — the readable counterpart to session_log.md.

``session_log.md`` is the archive: every step, every argument, every result, in
full. This is the report a person actually reads — what was measured, what came
out, and what the best point so far is.

Both renderings are written from the same ``ReportData``; neither is converted
from the other, so a change to one has to be made to both.

* ``report.md``   — plain text, editable and diffable. Figures are referenced
  as sibling files, so it only makes sense inside the session folder.
* ``report.html`` — figures are inlined as data URIs, so this single file
  carries its own pictures anywhere. To get a PDF, print it from the browser:
  that typesets better than a hand-built layout, and it avoids the font
  embedding of a PDF library, which silently drops Japanese glyphs. Its one
  network dependency is KaTeX, for maths in the summary; without it the
  formulae read as their ``$...$`` source and nothing else changes.

This module must stay dependency-free and cheap to import — ``server.py``
imports it at module scope, so anything pulled in here (pydantic-ai, matplotlib)
would be paid for on every start-up.
"""

from __future__ import annotations

import base64
import csv
import html
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

# nimo tools that hand back a set of proposed parameters, opening a cycle.
PROPOSAL_TOOLS = ("selection", "maximization", "minimization")

CELL_CHARS = 80
"""Cap on a single table cell, so one fat array cannot wreck the layout."""

# The report follows the language the user is writing in, decided once by the
# agent runtime and passed down, so the headings never end up in a different
# language from the summary underneath them.
LABELS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Experiment report",
        "session_started": "Session started",
        "folder": "Folder",
        "candidates_file": "Candidates file",
        "sha256": "SHA-256",
        "summary": "Summary",
        "ai_note": "This section was written automatically from the record.",
        "conditions": "Experimental conditions",
        "parameters": "Parameters",
        "objective": "Objective",
        "candidates": "Candidate points",
        "candidates_fmt": "{total} in total / {measured} measured / {unmeasured} unmeasured",
        "best_fmt": "Best {name}: {value}",
        "workflows": "Workflows run",
        "run_fmt": "Run {n}",
        "steps_fmt": "{n} steps",
        "seconds_fmt": ", {n} s",
        "error": "Error",
        "procedure": "Procedure as executed:",
        "no_runs": "No workflow runs were recorded.",
        "agent_ops": "Tools the agent called directly",
        "th_time": "Time",
        "th_tool": "Tool",
        "th_args": "Arguments",
        "th_result": "Result",
        "figures": "Figures",
        "notes": "Notes",
        "footer": "The complete step-by-step record is in `session_log.md`, in the same folder.",
        "footer_html": "The complete step-by-step record is in "
                       "<code>session_log.md</code>, in the same folder.",
        "maximization": "maximization",
        "minimization": "minimization",
        "measurement": "measurement",
        "objective_col": "{name} (objective)",
        "no_summary": "No summary was generated (the agent may be disabled, "
                      "or no model selected).",
        "fig_history": "Best objective so far",
        "fig_phase": "Phase diagram",
        "note_no_figures": "No plots in the session folder. Add a plot step to "
                           "the workflow to have figures in the report.",
        "note_running": "A workflow was still running when this report was "
                        "written, so its results are not included yet.",
    },
    "ja": {
        "title": "実験レポート",
        "session_started": "セッション開始",
        "folder": "フォルダ",
        "candidates_file": "候補ファイル",
        "sha256": "SHA-256",
        "summary": "要約",
        "ai_note": "この節は AI が記録から自動生成したものです。",
        "conditions": "実験条件",
        "parameters": "パラメータ",
        "objective": "目的関数",
        "candidates": "候補点",
        "candidates_fmt": "全 {total} 点 / 測定済み {measured} 点 / 未測定 {unmeasured} 点",
        "best_fmt": "最良の {name}: {value}",
        "workflows": "実行したワークフロー",
        "run_fmt": "実行 {n}",
        "steps_fmt": "{n} ステップ",
        "seconds_fmt": ", {n} 秒",
        "error": "エラー",
        "procedure": "実行された手順:",
        "no_runs": "実行記録はありません。",
        "agent_ops": "エージェントが直接行った操作",
        "th_time": "時刻",
        "th_tool": "ツール",
        "th_args": "引数",
        "th_result": "結果",
        "figures": "図",
        "notes": "注記",
        "footer": "全ステップの完全な記録は同じフォルダの `session_log.md` にあります。",
        "footer_html": "全ステップの完全な記録は同じフォルダの "
                       "<code>session_log.md</code> にあります。",
        "maximization": "最大化",
        "minimization": "最小化",
        "measurement": "測定値",
        "objective_col": "{name} (目的値)",
        "no_summary": "AI による要約は生成されませんでした"
                      "(エージェントが無効か、モデル未選択の可能性があります)。",
        "fig_history": "最良目的値の推移",
        "fig_phase": "相図",
        "note_no_figures": "セッションフォルダにプロットがありません。"
                           "レポートに図を載せるには、ワークフローに"
                           "プロット手順を入れてください。",
        "note_running": "レポート作成時点で実行中のワークフローがあり、"
                        "その結果はまだ含まれていません。",
    },
}

DEFAULT_LANG = "en"

# Kana and CJK ideographs. Enough to tell a Japanese prompt from an English one,
# which is the only distinction the report needs.
_JAPANESE_CHARS = re.compile(r"[぀-ヿ一-龯]")

LANGUAGE_MIN_LETTERS = 8
"""How much Latin text counts as a deliberate switch to English.

A Japanese conversation is full of short ASCII replies — "ok", "run it", "yes" —
and one of those must not flip the report into English."""


def detect_language(text: str) -> Optional[str]:
    """``"ja"`` / ``"en"`` for *text*, or None when it says nothing either way.

    None means "no signal", and callers are expected to keep whatever language
    they already had. That is what stops a one-word reply — "ok", "yes" — from
    overriding the language of the conversation around it.

    It lives in this module rather than in agent.py so that server.py can reach
    it: server.py imports report at module scope and agent only on demand.
    """
    if _JAPANESE_CHARS.search(text):
        return "ja"
    if len(re.findall(r"[A-Za-z]", text)) >= LANGUAGE_MIN_LETTERS:
        return "en"
    return None


def label(lang: str, key: str, **fmt: Any) -> str:
    """One label in *lang*, falling back to English for an unknown language."""
    table = LABELS.get(lang) or LABELS[DEFAULT_LANG]
    text = table.get(key) or LABELS[DEFAULT_LANG].get(key, key)
    return text.format(**fmt) if fmt else text


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Cycle:
    """One proposal → measurement → update turn of the optimization loop."""

    index: int
    params: dict = field(default_factory=dict)
    measurement: Any = None
    objective: Optional[float] = None
    tool: str = ""
    """Which tool produced the measurement, so the column can name it."""


@dataclass
class RunSection:
    """One workflow run, as it actually executed."""

    index: int
    status: str = ""
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    workflow_xml: str = ""
    cycles: list[Cycle] = field(default_factory=list)
    step_count: int = 0

    @property
    def duration(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return round(self.finished_at - self.started_at, 1)
        return None


@dataclass
class Figure:
    title: str
    path: str


@dataclass
class ReportData:
    """Everything the report needs, already resolved."""

    started_at: Optional[float] = None
    run_dir: str = ""
    candidates_snapshot: str = ""
    candidates_sha256: str = ""
    parameter_names: tuple[str, ...] = ()
    objective_names: tuple[str, ...] = ()
    direction: str = "maximization"
    total: int = 0
    measured: int = 0
    unmeasured: int = 0
    best: Optional[tuple[float, dict]] = None
    summary: str = ""
    """Prose written by the model. Empty means the section is skipped."""
    runs: list[RunSection] = field(default_factory=list)
    agent_calls: list[dict] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Why something is missing — an absent figure, an unfinished run."""
    lang: str = DEFAULT_LANG
    """Language for every label, chosen from the language the user is writing in."""

    @property
    def objective_name(self) -> str:
        return self.objective_names[-1] if self.objective_names else "objective"

    def t(self, key: str, **fmt: Any) -> str:
        """Shorthand for a label in this report's language."""
        return label(self.lang, key, **fmt)


# ---------------------------------------------------------------------------
# Collecting
# ---------------------------------------------------------------------------


def workflow_element(document: str) -> str:
    """The <workflow> element alone, dropping <servers> and <candidates>.

    Note that "<workflow" cannot match the root "<nimo-workflow" element, whose
    name is preceded by a hyphen.
    """
    start = document.find("<workflow")
    if start < 0:
        return ""
    end = document.find("</workflow>", start)
    if end < 0:
        close = document.find("/>", start)      # an empty <workflow/>
        return document[start:close + 2] if close >= 0 else ""

    # Slicing at "<workflow" drops that line's indent but not the inner lines',
    # which would leave the element looking mis-nested. The closing tag sits at
    # the element's own indent, so it says how much to take off the rest.
    lines = document[start:end + len("</workflow>")].splitlines()
    pad = " " * (len(lines[-1]) - len(lines[-1].lstrip()))
    if pad:
        lines[1:] = [ln[len(pad):] if ln.startswith(pad) else ln for ln in lines[1:]]
    return "\n".join(lines)


def _is_proposal(step: dict) -> bool:
    return step.get("server_id") == "nimo" and step.get("tool") in PROPOSAL_TOOLS


def _is_update(step: dict) -> bool:
    return step.get("server_id") == "nimo" and step.get("tool") == "update"


def build_cycles(history: list[dict]) -> list[Cycle]:
    """Fold a flat step list back into proposal/measure/update cycles.

    The loop structure is gone from ``history`` — it records leaf tool calls
    only — but the shape is recoverable: a nimo proposal opens a cycle, the
    synthesized nimo.update closes it, and whatever ran in between was the
    measurement.
    """
    cycles: list[Cycle] = []
    current: Optional[Cycle] = None

    def open_cycle() -> Cycle:
        cycle = Cycle(index=len(cycles) + 1)
        cycles.append(cycle)
        return cycle

    for step in history:
        if _is_proposal(step):
            result = step.get("result")
            current = open_cycle()
            current.params = result if isinstance(result, dict) else {}
        elif _is_update(step):
            if current is None:
                current = open_cycle()
            objs = (step.get("args") or {}).get("objs")
            current.objective = objs if isinstance(objs, (int, float)) else None
            current = None                      # the cycle is closed
        else:
            # A measurement, or a stray call such as a plot. Either way it
            # belongs to the cycle in flight; without one it starts its own.
            if current is None:
                current = open_cycle()
            current.measurement = step.get("result")
            current.tool = f"{step.get('server_id', '')}.{step.get('tool', '')}"
    return cycles


def build_runs(entries: list[dict]) -> list[RunSection]:
    """One RunSection per workflow entry, in the order they ran."""
    runs: list[RunSection] = []
    for entry in entries:
        if entry.get("kind") == "agent_tool":
            continue
        history = entry.get("history") or []
        runs.append(RunSection(
            index=len(runs) + 1,
            status=str(entry.get("status") or ""),
            error=entry.get("error"),
            started_at=entry.get("started_at"),
            finished_at=entry.get("finished_at"),
            workflow_xml=workflow_element(entry.get("workflow_xml") or ""),
            cycles=build_cycles(history),
            step_count=len(history),
        ))
    return runs


def build_agent_calls(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("kind") == "agent_tool"]


def best_objective(csv_path: str, objective: str,
                   minimize: bool = False) -> Optional[tuple[float, dict]]:
    """Best measured objective in the working candidates file, with its row.

    Read from the CSV rather than from the wrapper's history because that is the
    only session-wide view: ``start_workflow`` rebuilds ``res_history`` for each
    run, so the in-memory copy does not span the whole campaign.
    """
    best: Optional[tuple[float, dict]] = None
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    value = float(row[objective])
                except (TypeError, ValueError, KeyError):
                    continue
                if best is None or (value < best[0] if minimize else value > best[0]):
                    params = {k: v for k, v in row.items() if k != objective}
                    best = (value, params)
    except OSError:
        return None
    return best


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _num(value: Any) -> str:
    """Format a number without the noise of full float repr."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _cell(value)
    if isinstance(value, int):
        return str(value)
    text = f"{value:.6g}"
    return text


def _cell(value: Any, limit: int = CELL_CHARS) -> str:
    """One value, short enough for a table cell."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _num(value)
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _ts(value: Optional[float]) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _direction_label(direction: str, lang: str = DEFAULT_LANG) -> str:
    return label(lang, "minimization" if direction == "minimization" else "maximization")


def _cycle_row(cycle: Cycle, params: tuple[str, ...]) -> list[str]:
    return ([str(cycle.index)]
            + [_cell(cycle.params.get(p)) for p in params]
            + [_cell(cycle.measurement), _cell(cycle.objective)])


def _cycle_header(data: ReportData, run: "RunSection") -> list[str]:
    """Column names for a run's cycle table.

    When every cycle in the run measured with the same tool, that tool's name
    becomes the column heading; a run that mixed tools falls back to the generic
    "measurement" label. The specific name is worth reaching for because the
    measurement is usually fed straight through to the objective, so the two
    columns hold the same number — under a generic heading that reads as an
    unexplained repetition, and "measurement" alone reads as a count of
    measurements rather than as a reading.
    """
    tools = {c.tool for c in run.cycles if c.tool}
    measurement = tools.pop() if len(tools) == 1 else data.t("measurement")
    return ["#", *data.parameter_names, measurement,
            data.t("objective_col", name=data.objective_name)]


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    esc = lambda c: c.replace("|", "\\|")          # noqa: E731 - table cells only
    out = ["| " + " | ".join(esc(h) for h in header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(esc(c) for c in row) + " |" for row in rows]
    out.append("")
    return out


def to_markdown(data: ReportData) -> str:
    """The editable original. Figures are referenced as sibling files."""
    t = data.t
    lines = [f"# {t('title')}", ""]
    lines.append(f"- {t('session_started')}: {_ts(data.started_at)}")
    lines.append(f"- {t('folder')}: `{data.run_dir}`")
    if data.candidates_snapshot:
        lines.append(f"- {t('candidates_file')}: `{data.candidates_snapshot}`")
    if data.candidates_sha256:
        lines.append(f"- {t('sha256')}: `{data.candidates_sha256}`")
    lines.append("")

    if data.summary:
        lines += [f"## {t('summary')}", "", f"> {t('ai_note')}", "", data.summary, ""]

    lines += [f"## {t('conditions')}", ""]
    lines.append(f"- {t('parameters')}: {', '.join(data.parameter_names) or '—'}")
    lines.append(f"- {t('objective')}: {data.objective_name}"
                 f" ({_direction_label(data.direction, data.lang)})")
    lines.append(f"- {t('candidates')}: "
                 + t("candidates_fmt", total=data.total, measured=data.measured,
                     unmeasured=data.unmeasured))
    if data.best:
        value, params = data.best
        detail = ", ".join(f"{k}={_cell(v)}" for k, v in params.items())
        lines.append("- **"
                     + t("best_fmt", name=data.objective_name, value=_num(value))
                     + f"** ({detail})")
    lines.append("")

    lines += [f"## {t('workflows')}", ""]
    if data.runs:
        for run in data.runs:
            took = t("seconds_fmt", n=run.duration) if run.duration is not None else ""
            lines.append(f"### {t('run_fmt', n=run.index)} — {run.status}"
                         f" ({t('steps_fmt', n=run.step_count)}{took})")
            lines.append("")
            if run.error:
                lines += [f"- {t('error')}: {run.error}", ""]
            if run.workflow_xml:
                lines += [t("procedure"), "", "```xml", run.workflow_xml, "```", ""]
            if run.cycles:
                lines += _md_table(_cycle_header(data, run),
                                   [_cycle_row(c, data.parameter_names) for c in run.cycles])
    else:
        lines += [t("no_runs"), ""]

    if data.agent_calls:
        lines += [f"## {t('agent_ops')}", ""]
        lines += _md_table(
            [t("th_time"), t("th_tool"), t("th_args"), t("th_result")],
            [[_ts(c.get("started_at")), f"{c.get('server_id')}.{c.get('tool')}",
              _cell(c.get("args")), _cell(c.get("result"))] for c in data.agent_calls])

    # Only figures whose file is really there, so the markdown never points at
    # an image that is not next to it. The HTML applies the same test by way of
    # failing to inline a file it cannot read.
    figures = [f for f in data.figures if os.path.isfile(f.path)]
    if figures:
        lines += [f"## {t('figures')}", ""]
        for fig in figures:
            lines += [f"### {fig.title}", "",
                      f"![{fig.title}]({os.path.basename(fig.path)})", ""]

    if data.notes:
        lines += [f"## {t('notes')}", ""] + [f"- {n}" for n in data.notes] + [""]

    lines += ["---", "", t("footer")]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """\
:root { color-scheme: light; }
body { font-family: "Helvetica Neue", Arial, "Hiragino Sans", "Yu Gothic UI",
       "Meiryo", sans-serif; line-height: 1.7; color: #1f2328; background: #fff;
       max-width: 900px; margin: 0 auto; padding: 40px 24px 80px; }
h1 { font-size: 26px; border-bottom: 2px solid #d0d7de; padding-bottom: 10px; }
h2 { font-size: 20px; margin-top: 36px; border-bottom: 1px solid #d0d7de;
     padding-bottom: 6px; }
h3 { font-size: 16px; margin-top: 24px; }
dl.meta { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px;
          margin: 16px 0; font-size: 14px; }
dl.meta dt { color: #656d76; }
dl.meta dd { margin: 0; font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
             word-break: break-all; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }
th, td { border: 1px solid #d0d7de; padding: 6px 10px; text-align: left; }
th { background: #f6f8fa; font-weight: 600; }
tbody tr:nth-child(even) { background: #fafbfc; }
pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
      padding: 12px; overflow-x: auto; font-size: 13px; line-height: 1.5; }
code { font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; }
figure { margin: 20px 0; }
figure img { max-width: 100%; height: auto; border: 1px solid #d0d7de;
             border-radius: 6px; }
figcaption { font-size: 13px; color: #656d76; margin-top: 6px; }
.ai-note { background: #fff8e6; border-left: 4px solid #d4a72c; padding: 10px 14px;
           font-size: 13px; color: #6b5b1f; margin: 12px 0; }
.summary p { margin: 0 0 12px; }
/* A display equation can be wider than the column. On screen it scrolls; on
   paper there is nowhere to scroll to, so let it out of the box instead. */
.summary .katex-display { overflow-x: auto; overflow-y: hidden; }
.best { background: #eaf5ea; border-left: 4px solid #2da44e; padding: 10px 14px;
        margin: 12px 0; }
.status-error, .status-canceled { color: #cf222e; font-weight: 600; }
.status-done { color: #1a7f37; font-weight: 600; }
footer { margin-top: 48px; padding-top: 12px; border-top: 1px solid #d0d7de;
         font-size: 13px; color: #656d76; }
@media print {
  body { max-width: none; padding: 0; font-size: 11pt; }
  h2 { page-break-after: avoid; }
  h3 { page-break-after: avoid; }
  table, figure, pre { page-break-inside: avoid; }
  /* On screen a long line scrolls; on paper there is nowhere to scroll to, so
     it must wrap or the end of the workflow XML is simply cut off. */
  pre { white-space: pre-wrap; word-break: break-word; }
  .summary .katex-display { overflow-x: visible; }
  @page { margin: 16mm; }
}
"""


_KATEX = "https://cdn.jsdelivr.net/npm/katex@0.16.21/dist"
"""Same version and CDN as the chat log; change this and templates/index.html
together.

The two surfaces show the same model's maths, so they have to agree. On two
different KaTeX builds a formula could typeset in the answer and fail in the
report, leaving no way to tell which rendering to believe."""

_MATH_SPAN = re.compile(
    r"(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^\n$]+?\$)")
"""TeX spans, in the same four forms the chat log accepts."""

# The delimiters KaTeX is told to look for, matching MATH_SPAN above and
# KATEX_DELIMS in frontend.js. Scoped to .summary when it runs: that is the only
# part a model writes, and a stray "$" in a folder path must not start a formula.
_KATEX_BOOT = (
    "<script>document.addEventListener('DOMContentLoaded',function(){"
    "var el=document.querySelector('.summary');"
    "if(el&&typeof renderMathInElement==='function'){"
    "renderMathInElement(el,{delimiters:["
    "{left:'$$',right:'$$',display:true},"
    "{left:'\\\\[',right:'\\\\]',display:true},"
    "{left:'\\\\(',right:'\\\\)',display:false},"
    "{left:'$',right:'$',display:false}],throwOnError:false});}});</script>"
)


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _summary_html(text: str) -> list[str]:
    """The summary as <p> blocks, with any TeX left in one piece.

    Maths is masked before the text is escaped and split, then put back. A
    display equation carries blank lines of its own, and splitting on those
    would hand KaTeX an opening delimiter in one paragraph and its closing one
    in the next — which renders as neither maths nor readable source.

    The placeholder uses NUL because a model's prose cannot contain one; a
    printable marker could occur in the summary by chance.
    """
    math: list[str] = []

    def stash(match: re.Match) -> str:
        math.append(match.group(0))
        return f"\x00MATH{len(math) - 1}\x00"

    paragraphs = []
    for para in _MATH_SPAN.sub(stash, text).split("\n\n"):
        if not para.strip():
            continue
        # Restored escaped, exactly as the chat log does it: KaTeX reads
        # textContent, so a "<" that went in as &lt; reaches it as "<".
        body = re.sub(r"\x00MATH(\d+)\x00",
                      lambda m: _e(math[int(m.group(1))]), _e(para.strip()))
        paragraphs.append(f"<p>{body}</p>")
    return paragraphs


def _data_uri(path: str) -> Optional[str]:
    """Inline a PNG so the report stays a single portable file."""
    try:
        with open(path, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None


def _html_table(header: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>"
                   for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def to_html(data: ReportData) -> str:
    """One file to open anywhere, and to print to get a PDF.

    Figures are inlined, so the pictures travel with it. The single thing that
    wants the network is KaTeX, which typesets the maths in the summary; when it
    cannot be fetched the formulae read as their ``$...$`` source and the rest
    of the document is unaffected.
    """
    out: list[str] = []
    add = out.append
    t = data.t

    add("<!DOCTYPE html>")
    add(f'<html lang="{_e(data.lang)}"><head><meta charset="utf-8">')
    add('<meta name="viewport" content="width=device-width, initial-scale=1">')
    add(f"<title>{_e(t('title'))} — {_e(_ts(data.started_at))}</title>")
    add(f'<link rel="stylesheet" href="{_KATEX}/katex.min.css">')
    add(f'<script defer src="{_KATEX}/katex.min.js"></script>')
    add(f'<script defer src="{_KATEX}/contrib/auto-render.min.js"></script>')
    add(f"<style>{_CSS}</style></head><body>")

    add(f"<h1>{_e(t('title'))}</h1>")
    add("<dl class='meta'>")
    add(f"<dt>{_e(t('session_started'))}</dt><dd>{_e(_ts(data.started_at))}</dd>")
    add(f"<dt>{_e(t('folder'))}</dt><dd>{_e(data.run_dir)}</dd>")
    if data.candidates_snapshot:
        add(f"<dt>{_e(t('candidates_file'))}</dt><dd>{_e(data.candidates_snapshot)}</dd>")
    if data.candidates_sha256:
        add(f"<dt>{_e(t('sha256'))}</dt><dd>{_e(data.candidates_sha256)}</dd>")
    add("</dl>")

    if data.summary:
        add(f"<h2>{_e(t('summary'))}</h2>")
        add(f"<div class='ai-note'>{_e(t('ai_note'))}</div>")
        add("<div class='summary'>")
        out.extend(_summary_html(data.summary))
        add("</div>")

    add(f"<h2>{_e(t('conditions'))}</h2>")
    add("<dl class='meta'>")
    add(f"<dt>{_e(t('parameters'))}</dt>"
        f"<dd>{_e(', '.join(data.parameter_names) or '—')}</dd>")
    add(f"<dt>{_e(t('objective'))}</dt><dd>{_e(data.objective_name)} "
        f"({_e(_direction_label(data.direction, data.lang))})</dd>")
    add(f"<dt>{_e(t('candidates'))}</dt><dd>"
        + _e(t("candidates_fmt", total=data.total, measured=data.measured,
               unmeasured=data.unmeasured)) + "</dd>")
    add("</dl>")
    if data.best:
        value, params = data.best
        detail = ", ".join(f"{k}={_cell(v)}" for k, v in params.items())
        # Split the label on where the value goes, so the number can be bolded
        # without assuming which side of it the words sit on.
        head, _, tail = t("best_fmt", name=data.objective_name,
                          value="\x00").partition("\x00")
        add(f"<div class='best'>{_e(head)}<strong>{_e(_num(value))}</strong>"
            f"{_e(tail)} — {_e(detail)}</div>")

    add(f"<h2>{_e(t('workflows'))}</h2>")
    if not data.runs:
        add(f"<p>{_e(t('no_runs'))}</p>")
    for run in data.runs:
        took = t("seconds_fmt", n=run.duration) if run.duration is not None else ""
        cls = f"status-{run.status}" if run.status else ""
        add(f"<h3>{_e(t('run_fmt', n=run.index))} — "
            f"<span class='{cls}'>{_e(run.status)}</span> "
            f"({_e(t('steps_fmt', n=run.step_count))}{_e(took)})</h3>")
        if run.error:
            add(f"<p class='status-error'>{_e(t('error'))}: {_e(run.error)}</p>")
        if run.workflow_xml:
            add(f"<pre><code>{_e(run.workflow_xml)}</code></pre>")
        if run.cycles:
            add(_html_table(_cycle_header(data, run),
                            [_cycle_row(c, data.parameter_names) for c in run.cycles]))

    if data.agent_calls:
        add(f"<h2>{_e(t('agent_ops'))}</h2>")
        add(_html_table(
            [t("th_time"), t("th_tool"), t("th_args"), t("th_result")],
            [[_ts(c.get("started_at")), f"{c.get('server_id')}.{c.get('tool')}",
              _cell(c.get("args")), _cell(c.get("result"))] for c in data.agent_calls]))

    figures = [(f, _data_uri(f.path)) for f in data.figures]
    if any(uri for _, uri in figures):
        add(f"<h2>{_e(t('figures'))}</h2>")
        for fig, uri in figures:
            if not uri:
                continue
            add(f"<figure><img src='{uri}' alt='{_e(fig.title)}'>"
                f"<figcaption>{_e(fig.title)}</figcaption></figure>")

    if data.notes:
        add(f"<h2>{_e(t('notes'))}</h2><ul>")
        for note in data.notes:
            add(f"<li>{_e(note)}</li>")
        add("</ul>")

    # The only label kept as markup: it carries a <code> element.
    add(f"<footer>{t('footer_html')}</footer>")
    add(_KATEX_BOOT)
    add("</body></html>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Facts for the model
# ---------------------------------------------------------------------------


def facts_text(data: ReportData) -> str:
    """The record as compact text, for the model to summarise.

    Everything here is measured; nothing is interpreted. The summary the model
    writes on top of it is the only part of the report that is not a fact.

    Always English, whatever language the report comes out in: this is the
    model's input, never shown to anyone, and the output language is set by the
    prompt. One canonical form keeps the summariser's job identical either way.
    """
    lines = [f"Parameters: {', '.join(data.parameter_names) or '(none)'}",
             f"Objective: {data.objective_name} ({data.direction})",
             f"Candidates: {data.total} total / {data.measured} measured "
             f"/ {data.unmeasured} unmeasured"]
    if data.best:
        value, params = data.best
        lines.append(f"Best: {data.objective_name}={_num(value)} at "
                     + ", ".join(f"{k}={_cell(v)}" for k, v in params.items()))
    for run in data.runs:
        lines.append("")
        lines.append(f"[Run {run.index}] {run.status}, {run.step_count} steps"
                     + (f", {run.duration} s" if run.duration is not None else ""))
        if run.error:
            lines.append(f"  Error: {run.error}")
        if run.workflow_xml:
            lines.append("  Procedure:")
            lines += [f"    {ln}" for ln in run.workflow_xml.splitlines()]
        for cycle in run.cycles:
            params = ", ".join(f"{k}={_cell(v)}" for k, v in cycle.params.items()) or "-"
            # Named after the producing tool, for the reason _cycle_header
            # gives: "measurement = 3" reads as a count rather than a reading.
            measured = f"{cycle.tool or 'measurement'} = {_cell(cycle.measurement)}"
            lines.append(f"  cycle {cycle.index}: {params} -> {measured}"
                         f" -> objective {_cell(cycle.objective)}")
    for call in data.agent_calls:
        lines.append(f"[Agent tool] {call.get('server_id')}.{call.get('tool')}"
                     f"({_cell(call.get('args'))}) -> {_cell(call.get('result'))}")
    return "\n".join(lines)
