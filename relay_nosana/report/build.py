from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader


TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load_json(run_id: str, filename: str, out_dir: Path | None = None) -> dict:
    candidates = [
        out_dir / filename if out_dir else None,
        Path(f"out/{run_id}/{filename}"),
    ]
    for p in candidates:
        if p and p.exists():
            return json.loads(p.read_text())
    return {}


def _load_calibration(run_id: str, out_dir: Path | None = None) -> dict:
    return _load_json(run_id, "calibration.json", out_dir)


def _load_routing(run_id: str, out_dir: Path | None = None) -> dict:
    return _load_json(run_id, "routing_table.json", out_dir)


def _load_run_summary(run_id: str, out_dir: Path | None = None) -> dict:
    return _load_json(run_id, "run_summary.json", out_dir)


def _compute_headline(routing: dict, calibration: dict, run_summary: dict) -> dict:
    x = routing.get("routable_share_raw", 0.0)
    y = routing.get("routable_share_compiled", 0.0)
    cells = run_summary.get("cells", [])
    total_cost = sum(c.get("total_cost_usd", 0) or 0 for c in cells)
    n_instances = sum(c.get("n", 0) for c in cells) or 1
    cost_per_1k = (total_cost / n_instances) * 1000
    return {
        "x_pct": f"{x:.0%}",
        "y_pct": f"{y:.0%}",
        "cost_nosana_per_1k": f"${cost_per_1k:.2f}",
        "cost_frontier_per_1k": "N/A",
        "gpu_hours": "N/A",
    }


def build_capability_map_csv(run_id: str, out_path: Path, repo: str, out_dir: Path | None = None) -> None:
    calibration = _load_calibration(run_id, out_dir)
    cells = calibration.get("cells", [])
    if not cells:
        return
    fieldnames = [
        "repo", "task_type", "model", "quantization", "market", "gpu", "mode",
        "n", "metric_name", "score", "ci_low", "ci_high", "parse_fail_rate",
        "tokens_in_mean", "tokens_out_mean", "latency_p50_ms", "latency_p95_ms",
        "cost_per_1k_usd", "gpu_hours", "vllm_version", "run_id", "date",
    ]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for cell in cells:
            rid = cell.get("recipient_id", "")
            model = rid.split(":")[1].split("@")[0] if ":" in rid else rid
            writer.writerow({
                "repo": repo,
                "task_type": cell.get("task_type", ""),
                "model": model,
                "quantization": "",
                "market": "",
                "gpu": "",
                "mode": cell.get("mode", ""),
                "n": cell.get("n", 0),
                "metric_name": "",
                "score": cell.get("mean_score", ""),
                "ci_low": cell.get("ci_lo", ""),
                "ci_high": cell.get("ci_hi", ""),
                "parse_fail_rate": cell.get("parse_fail_rate", ""),
                "tokens_in_mean": cell.get("tokens_in_mean", ""),
                "tokens_out_mean": cell.get("tokens_out_mean", ""),
                "latency_p50_ms": cell.get("latency_p50_ms", ""),
                "latency_p95_ms": cell.get("latency_p95_ms", ""),
                "cost_per_1k_usd": "",
                "gpu_hours": "",
                "vllm_version": "",
                "run_id": run_id,
                "date": str(date.today()),
            })


def build_report(run_id: str, repo: str, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or Path(f"out/{run_id}")
    out_dir.mkdir(parents=True, exist_ok=True)

    calibration = _load_calibration(run_id, out_dir)
    routing = _load_routing(run_id, out_dir)
    run_summary = _load_run_summary(run_id, out_dir)
    headline = _compute_headline(routing, calibration, run_summary)

    ctx = {
        "run_id": run_id,
        "repo": repo,
        "date": str(date.today()),
        "headline": headline,
        "calibration_cells": calibration.get("cells", []),
        "routing_decisions": routing.get("decisions", []),
    }

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    try:
        tmpl = env.get_template("report.html.j2")
        html = tmpl.render(**ctx)
    except Exception:
        html = f"<html><body><pre>{json.dumps(ctx, indent=2)}</pre></body></html>"

    report_html = out_dir / "report.html"
    report_html.write_text(html)

    report_md = out_dir / "report.md"
    report_md.write_text(_md_report(ctx))

    csv_path = out_dir / "capability_map.csv"
    build_capability_map_csv(run_id, csv_path, repo, out_dir)

    return report_html


def _md_report(ctx: dict) -> str:
    h = ctx["headline"]
    lines = [
        f"# Relay × Nosana POC — {ctx['repo']}",
        f"Run: `{ctx['run_id']}`  Date: {ctx['date']}",
        "",
        "## Headline",
        f"> Relay's compiler raised the share of work routable to Nosana from "
        f"**{h['x_pct']}** to **{h['y_pct']}**, at "
        f"**{h['cost_nosana_per_1k']}** vs **{h['cost_frontier_per_1k']}** per 1,000 issues, "
        f"using **{h['gpu_hours']}** GPU-hours on Nosana.",
        "",
        "## Capability Matrix",
        "",
        "| task_type | recipient | mode | n | score | CI |",
        "|-----------|-----------|------|---|-------|----|",
    ]
    for c in ctx.get("calibration_cells", []):
        score = c.get("mean_score")
        score_s = f"{score:.3f}" if score is not None else "N/A"
        ci = f"[{c.get('ci_lo', 0):.3f}, {c.get('ci_hi', 0):.3f}]"
        lines.append(
            f"| {c.get('task_type','')} | {c.get('recipient_id','')} "
            f"| {c.get('mode','')} | {c.get('n',0)} | {score_s} | {ci} |"
        )
    return "\n".join(lines)
