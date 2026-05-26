#!/usr/bin/env python3
"""
Aggregate per-session AMS HIL KPI JSON files into a multi-session
summary + a regression/flake roster.

Usage:
    scripts/hil_kpi_report.py [--kpi-dir .kpi] [--since 2026-05-01]
                              [--md .kpi/summary.md]
                              [--csv .kpi/trend.csv]

Reads every `*.json` in `--kpi-dir` (default `.kpi/` in cwd; emitted by
`tests/hil/ams/kpi_plugin.py`), aggregates the numbers, and writes:

  - a human-readable Markdown summary
  - a per-session CSV time-series (for dashboards / spreadsheets)
  - a regression roster (tests that passed in an earlier session and
    failed in a later one)
  - a flake roster (tests with mixed outcomes across sessions)

See `docs/ams-hil/test-plan-v1.5.0.md` §4 for the KPI catalogue and
the operator-hours heuristic.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def load_sessions(kpi_dir: Path, since_iso: str | None) -> list[dict]:
    if not kpi_dir.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(kpi_dir.glob("*.json")):
        try:
            with p.open() as f:
                s = json.load(f)
        except Exception:
            continue
        if since_iso and s.get("started_at_iso", "") < since_iso:
            continue
        out.append(s)
    out.sort(key=lambda s: s.get("started_at_unix", 0))
    return out


def summarise(sessions: list[dict]) -> dict[str, Any]:
    if not sessions:
        return {"empty": True}

    agg: dict[str, Any] = {
        "session_count":           len(sessions),
        "first_session_iso":       sessions[0]["started_at_iso"],
        "last_session_iso":        sessions[-1]["started_at_iso"],
        "cumulative_bench_hours":  sum(s["session_wall_clock_s"] for s in sessions) / 3600,
        "cumulative_test_hours":   sum(s["total_test_time_s"]    for s in sessions) / 3600,
        "cumulative_power_cycles": sum(s["power_cycle_count"]    for s in sessions),
        "cumulative_flash_cycles": sum(s["flash_cycle_count"]    for s in sessions),
        "cumulative_bl_triggers":  sum(s["bl_trigger_count"]     for s in sessions),
        "cumulative_frames_observed": sum(s["frames_observed_total"] for s in sessions),
        "cumulative_block_f_cycles":  sum(s["block_f_cycles_completed"] for s in sessions),
        "cumulative_tests_executed":  sum(s["tests_executed"]    for s in sessions),
        "cumulative_tests_passed":    sum(s["tests_passed"]      for s in sessions),
        "cumulative_tests_failed":    sum(s["tests_failed"]      for s in sessions),
        "cumulative_tests_errored":   sum(s["tests_errored"]     for s in sessions),
        "cumulative_tests_skipped":   sum(s["tests_skipped"]     for s in sessions),
        "equivalent_operator_hours":  sum(s["equivalent_operator_hours"] for s in sessions),
    }

    util_samples = [s["bench_utilisation_pct"] for s in sessions
                    if s["session_wall_clock_s"] > 0]
    if util_samples:
        agg["mean_bench_utilisation_pct"] = statistics.mean(util_samples)
        agg["median_bench_utilisation_pct"] = statistics.median(util_samples)

    bl_means = [s["bl_discover_latency_ms_mean"] for s in sessions
                if s.get("bl_discover_latency_ms_mean") is not None]
    if bl_means:
        agg["mean_bl_discover_latency_ms"] = statistics.mean(bl_means)

    # Regression + flake detection.
    outcomes: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for s in sessions:
        t = s["started_at_unix"]
        for n in s["failed_tests"] + s["errored_tests"]:
            outcomes[n].append((t, "fail"))
    # We don't have explicit per-test pass nodeids in the ledger
    # (would blow the file size up), so the regression detector is
    # coarse: a test that "appears in failed_tests in session N but
    # not in N-1" is a regression candidate. Flakes are tests that
    # toggle between fail-states across sessions without a stable
    # streak.

    regressions: list[str] = []
    flakes:      list[str] = []
    for nodeid, hits in outcomes.items():
        if len(hits) >= 2:
            # If failure count == total sessions, it's "stable broken".
            # If it's a fraction, it's flake/regression-shaped.
            failed_in = len(hits)
            total = len(sessions)
            if failed_in < total:
                flakes.append(nodeid)
        else:
            # Single failure -> regression candidate (if it later passes).
            regressions.append(nodeid)

    agg["regression_candidates"] = sorted(regressions)
    agg["flake_candidates"]      = sorted(flakes)

    return agg


def render_md(agg: dict[str, Any], sessions: list[dict]) -> str:
    if agg.get("empty"):
        return "# AMS HIL KPI summary\n\nNo sessions in `--kpi-dir` yet.\n"

    lines: list[str] = []
    lines.append("# AMS HIL KPI summary\n")
    lines.append(f"Sessions in window: **{agg['session_count']}**  ")
    lines.append(f"First: `{agg['first_session_iso']}`  ")
    lines.append(f"Last:  `{agg['last_session_iso']}`\n")

    lines.append("## Cumulative testing exposure\n")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Bench-hours (wall-clock) | **{agg['cumulative_bench_hours']:.2f} h** |")
    lines.append(f"| Active test-hours | {agg['cumulative_test_hours']:.2f} h |")
    lines.append(f"| Power cycles | {agg['cumulative_power_cycles']} |")
    lines.append(f"| Flash cycles | {agg['cumulative_flash_cycles']} |")
    lines.append(f"| BL triggers fired | {agg['cumulative_bl_triggers']} |")
    lines.append(f"| Frames observed | {agg['cumulative_frames_observed']:,} |")
    lines.append(f"| Block F soak cycles completed | {agg['cumulative_block_f_cycles']} |")
    lines.append(f"| **Equivalent operator hours (estimate)** | **{agg['equivalent_operator_hours']:.1f} h** |")
    lines.append("")

    lines.append("## Test outcomes (cumulative)\n")
    lines.append("| | Count |")
    lines.append("|---|---|")
    lines.append(f"| Passed | {agg['cumulative_tests_passed']} |")
    lines.append(f"| Failed | {agg['cumulative_tests_failed']} |")
    lines.append(f"| Errored | {agg['cumulative_tests_errored']} |")
    lines.append(f"| Skipped | {agg['cumulative_tests_skipped']} |")
    lines.append(f"| Executed (passed+failed+errored) | {agg['cumulative_tests_executed']} |")
    lines.append("")

    if "mean_bench_utilisation_pct" in agg:
        lines.append("## Bench utilisation\n")
        lines.append(f"- Mean: {agg['mean_bench_utilisation_pct']:.1f} %  ")
        lines.append(f"- Median: {agg['median_bench_utilisation_pct']:.1f} %\n")

    if "mean_bl_discover_latency_ms" in agg:
        lines.append("## BL DISCOVER latency\n")
        lines.append(f"- Cross-session mean: {agg['mean_bl_discover_latency_ms']:.1f} ms\n")

    if agg["regression_candidates"]:
        lines.append("## Regression candidates (single-session failures)\n")
        for n in agg["regression_candidates"][:20]:
            lines.append(f"- `{n}`")
        if len(agg["regression_candidates"]) > 20:
            lines.append(f"- ... and {len(agg['regression_candidates']) - 20} more")
        lines.append("")

    if agg["flake_candidates"]:
        lines.append("## Flake candidates (mixed outcomes across sessions)\n")
        for n in agg["flake_candidates"][:20]:
            lines.append(f"- `{n}`")
        if len(agg["flake_candidates"]) > 20:
            lines.append(f"- ... and {len(agg['flake_candidates']) - 20} more")
        lines.append("")

    lines.append("## Last 10 sessions\n")
    lines.append("| When | Wall clock | Tests | Pass | Fail | Power cycles | Flash | Triggers |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in sessions[-10:]:
        lines.append(
            f"| {s['started_at_iso']} "
            f"| {s['session_wall_clock_s']/60:.1f} min "
            f"| {s['tests_executed']} "
            f"| {s['tests_passed']} "
            f"| {s['tests_failed']} "
            f"| {s['power_cycle_count']} "
            f"| {s['flash_cycle_count']} "
            f"| {s['bl_trigger_count']} |"
        )

    return "\n".join(lines) + "\n"


def write_csv(path: Path, sessions: list[dict]) -> None:
    if not sessions:
        return
    cols = [
        "started_at_iso",
        "session_wall_clock_s",
        "total_test_time_s",
        "bench_utilisation_pct",
        "tests_executed",
        "tests_passed",
        "tests_failed",
        "tests_errored",
        "tests_skipped",
        "power_cycle_count",
        "flash_cycle_count",
        "bl_trigger_count",
        "frames_observed_total",
        "block_f_cycles_completed",
        "equivalent_operator_hours",
        "git_sha",
        "bench",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for s in sessions:
            w.writerow(s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kpi-dir", default=".kpi", type=Path,
                    help="Directory of per-session JSON ledgers (default .kpi/)")
    ap.add_argument("--since", default=None,
                    help="ISO date/datetime; ignore sessions before this")
    ap.add_argument("--md", default=None, type=Path,
                    help="Write Markdown summary to this path (default .kpi/summary.md)")
    ap.add_argument("--csv", default=None, type=Path,
                    help="Write per-session CSV to this path (default .kpi/trend.csv)")
    ap.add_argument("--stdout", action="store_true",
                    help="Also print the Markdown summary to stdout")
    args = ap.parse_args()

    sessions = load_sessions(args.kpi_dir, args.since)
    agg = summarise(sessions)
    md = render_md(agg, sessions)

    md_path  = args.md  or args.kpi_dir / "summary.md"
    csv_path = args.csv or args.kpi_dir / "trend.csv"

    args.kpi_dir.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md)
    write_csv(csv_path, sessions)

    if args.stdout:
        print(md)
    print(f"wrote {md_path}")
    print(f"wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
