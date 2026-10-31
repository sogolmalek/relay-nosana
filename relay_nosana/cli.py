from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(name="relay", help="Relay × Nosana POC CLI", add_completion=False)
nosana_app = typer.Typer(help="Nosana deployment commands")
app.add_typer(nosana_app, name="nosana")


def _settings():
    from .config import get_settings
    return get_settings()


# ---------------------------------------------------------------------------
# relay repo check
# ---------------------------------------------------------------------------

@app.command("repo")
def repo_cmd(
    action: str = typer.Argument(..., help="check | ingest"),
    target: str = typer.Argument(..., help="OWNER/REPO"),
):
    """Subcommand: relay repo check OWNER/REPO  |  relay repo ingest OWNER/REPO"""
    if action == "check":
        _repo_check(target)
    elif action == "ingest":
        _repo_ingest(target)
    else:
        typer.echo(f"Unknown action: {action}")
        raise typer.Exit(1)


def _repo_check(target: str) -> None:
    from .github.repo_check import check_repo
    settings = _settings()
    token = settings.github_token
    if not token:
        typer.echo("GITHUB_TOKEN is not set", err=True)
        raise typer.Exit(1)

    parts = target.split("/", 1)
    if len(parts) != 2:
        typer.echo("Target must be OWNER/REPO", err=True)
        raise typer.Exit(1)
    owner, repo = parts

    result = asyncio.run(check_repo(owner, repo, token))
    typer.echo(str(result))


def _repo_ingest(target: str) -> None:
    from .github.ingest import ingest
    settings = _settings()
    token = settings.github_token
    if not token:
        typer.echo("GITHUB_TOKEN is not set", err=True)
        raise typer.Exit(1)
    parts = target.split("/", 1)
    owner, repo = parts
    typer.echo(f"Ingesting {owner}/{repo}...")
    data = asyncio.run(ingest(owner, repo, token))
    typer.echo(f"Done. {len(data['issues'])} issues, {len(data['file_trees'])} tree refs.")


# ---------------------------------------------------------------------------
# relay calibrate
# ---------------------------------------------------------------------------

@app.command()
def calibrate(
    provider: str = typer.Option("mock", help="mock | local | nosana"),
    config: Optional[Path] = typer.Option(None, help="Path to relay.nosana.yaml"),
):
    """Run calibration against a provider."""
    asyncio.run(_calibrate_async(provider, config))


async def _calibrate_async(provider: str, config: Optional[Path]) -> None:
    from .config import get_settings
    from .tasks.label import LabelTask
    from .tasks.duplicate import DuplicateTask
    from .tasks.localize import LocalizeTask
    from .calibrate import calibrate as do_calibrate
    from .github.ingest import ingest

    settings = get_settings(config)
    recipient = _make_recipient(provider, settings)

    if settings.repo and settings.repo != "OWNER/REPO" and settings.github_token:
        owner, repo = settings.repo.split("/", 1)
        typer.echo(f"Ingesting {owner}/{repo}...")
        data = await ingest(owner, repo, settings.github_token)
    else:
        typer.echo("No real repo configured; using empty data for mock calibration.")
        data = {"issues": _mock_issues(), "file_trees": _mock_file_trees()}

    tasks = [
        LabelTask(settings.label_map),
        DuplicateTask(settings.duplicate.candidates_k, settings.seed),
    ]
    if settings.localize.enabled:
        tasks.append(LocalizeTask(settings.localize.exclude_globs, settings.localize.candidates_k, settings.localize.retrieval))
    else:
        typer.echo("  localize: PENDING (disabled for M0)")
    instances_by_task = {t.name: t.build_instances(data) for t in tasks}
    for k, v in instances_by_task.items():
        typer.echo(f"  {k}: {len(v)} instances")

    run_id = f"cal-{uuid.uuid4().hex[:8]}"
    result = await do_calibrate(
        tasks=tasks,
        instances_by_task=instances_by_task,
        recipients=[recipient],
        n_per_task=settings.calibration.n_per_task,
        seed=settings.seed,
        bootstrap=settings.calibration.bootstrap,
        run_id=run_id,
        all_issues=data["issues"],
    )
    typer.echo(f"Calibration complete. Run ID: {run_id}")
    for cell in result["cells"]:
        score = cell.get("mean_score")
        typer.echo(
            f"  {cell['task_type']:12} {cell['mode']:8} {cell['recipient_id']:30}"
            f"  score={score:.3f}" if score is not None else "  score=N/A"
        )


# ---------------------------------------------------------------------------
# relay route
# ---------------------------------------------------------------------------

@app.command()
def route(
    run_id: str = typer.Argument(..., help="Calibration run ID"),
    config: Optional[Path] = typer.Option(None),
):
    """Compute routing table from a calibration run."""
    from .config import get_settings
    from .route import compute_routing_table, compute_routable_share, save_routing_table

    settings = get_settings(config)
    cal_path = Path(f"out/{run_id}/calibration.json")
    if not cal_path.exists():
        typer.echo(f"Calibration not found: {cal_path}", err=True)
        raise typer.Exit(1)

    cal = json.loads(cal_path.read_text())
    decisions = compute_routing_table(
        cal["cells"],
        threshold_ratio=settings.routing.threshold_ratio,
        abs_floor=settings.routing.abs_floor,
    )
    instances_count: dict[str, int] = {}
    for d in decisions:
        instances_count[d.task_type] = instances_count.get(d.task_type, 100)

    shares = compute_routable_share(decisions, instances_count)
    out_path = Path(f"out/{run_id}/routing_table.json")
    save_routing_table(decisions, shares, out_path)
    typer.echo(f"Routing table saved to {out_path}")
    typer.echo(f"Routable share — raw: {shares['raw']:.1%}  compiled: {shares['compiled']:.1%}")


# ---------------------------------------------------------------------------
# relay run
# ---------------------------------------------------------------------------

@app.command("run")
def run_cmd(
    cal_run_id: str = typer.Argument(..., help="Calibration run ID"),
    mode: str = typer.Option("compiled", help="compiled | raw"),
    provider: str = typer.Option("mock"),
    config: Optional[Path] = typer.Option(None),
):
    """Full run using the routing table from a calibration."""
    asyncio.run(_run_async(cal_run_id, mode, provider, config))


async def _run_async(cal_run_id: str, mode: str, provider: str, config: Optional[Path]) -> None:
    from .config import get_settings
    from .run import full_run
    from .route import compute_routing_table, compute_routable_share
    from .tasks.label import LabelTask
    from .tasks.duplicate import DuplicateTask
    from .tasks.localize import LocalizeTask

    settings = get_settings(config)
    recipient = _make_recipient(provider, settings)

    cal_path = Path(f"out/{cal_run_id}/calibration.json")
    if not cal_path.exists():
        typer.echo(f"Calibration not found: {cal_path}", err=True)
        raise typer.Exit(1)

    cal = json.loads(cal_path.read_text())
    cal_ids: set[str] = set()
    for ids in json.loads(
        (Path(f"out/{cal_run_id}/calibration.json")
         .read_text())
    ).get("cal_ids_by_task", {}).values():
        cal_ids.update(ids)

    decisions = compute_routing_table(
        cal["cells"],
        threshold_ratio=settings.routing.threshold_ratio,
        abs_floor=settings.routing.abs_floor,
    )

    data = {"issues": _mock_issues(), "file_trees": _mock_file_trees()}
    tasks = [
        LabelTask(settings.label_map),
        DuplicateTask(settings.duplicate.candidates_k, settings.seed),
    ]
    if settings.localize.enabled:
        tasks.append(LocalizeTask(settings.localize.exclude_globs, settings.localize.candidates_k, settings.localize.retrieval))
    else:
        typer.echo("  localize: PENDING (disabled for M0)")
    all_instances = {t.name: [i for i in t.build_instances(data) if i.id not in cal_ids] for t in tasks}

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    result = await full_run(
        tasks=tasks,
        instances_by_task=all_instances,
        routing_decisions=decisions,
        recipients_by_id={recipient.id: recipient},
        run_id=run_id,
        all_issues=data["issues"],
    )
    typer.echo(f"Run complete. Run ID: {run_id}")


# ---------------------------------------------------------------------------
# relay report
# ---------------------------------------------------------------------------

@app.command()
def report(
    run_id: str = typer.Argument(...),
    config: Optional[Path] = typer.Option(None),
):
    """Generate HTML + Markdown report for a run."""
    from .config import get_settings
    from .report.build import build_report
    settings = get_settings(config)
    report_path = build_report(run_id, settings.repo)
    typer.echo(f"Report written to {report_path}")


# ---------------------------------------------------------------------------
# relay demo
# ---------------------------------------------------------------------------

@app.command()
def demo(
    target: str = typer.Argument("OWNER/REPO"),
    provider: str = typer.Option("mock"),
    config: Optional[Path] = typer.Option(None),
):
    """Full end-to-end demo: ingest → calibrate → route → run → report."""
    asyncio.run(_demo_async(target, provider, config))


async def _demo_async(target: str, provider: str, config: Optional[Path]) -> None:
    from .config import get_settings, reset_settings
    from .tasks.label import LabelTask
    from .tasks.duplicate import DuplicateTask
    from .tasks.localize import LocalizeTask
    from .calibrate import calibrate as do_calibrate
    from .route import compute_routing_table, compute_routable_share, save_routing_table
    from .run import full_run
    from .report.build import build_report

    reset_settings()
    settings = get_settings(config)
    recipient = _make_recipient(provider, settings)

    # Ingest or use mock
    if settings.github_token and target != "OWNER/REPO":
        owner, repo = target.split("/", 1)
        from .github.ingest import ingest
        typer.echo(f"Ingesting {owner}/{repo}...")
        data = await ingest(owner, repo, settings.github_token)
    else:
        typer.echo("Using mock data (set GITHUB_TOKEN and repo for real data)")
        data = {"issues": _mock_issues(), "file_trees": _mock_file_trees()}

    tasks = [
        LabelTask(settings.label_map),
        DuplicateTask(settings.duplicate.candidates_k, settings.seed),
    ]
    if settings.localize.enabled:
        tasks.append(LocalizeTask(settings.localize.exclude_globs, settings.localize.candidates_k, settings.localize.retrieval))
    else:
        typer.echo("  localize: PENDING (disabled for M0)")

    instances_by_task = {t.name: t.build_instances(data) for t in tasks}
    typer.echo("Instances built:")
    for k, v in instances_by_task.items():
        typer.echo(f"  {k}: {len(v)}")

    run_id = f"demo-{uuid.uuid4().hex[:8]}"

    # Calibrate
    typer.echo("Running calibration...")
    cal_result = await do_calibrate(
        tasks=tasks,
        instances_by_task=instances_by_task,
        recipients=[recipient],
        n_per_task=min(settings.calibration.n_per_task, 10),
        seed=settings.seed,
        bootstrap=100,
        run_id=run_id,
        all_issues=data["issues"],
    )

    # Route
    decisions = compute_routing_table(
        cal_result["cells"],
        threshold_ratio=settings.routing.threshold_ratio,
        abs_floor=settings.routing.abs_floor,
    )
    instances_count = {k: len(v) for k, v in instances_by_task.items()}
    shares = compute_routable_share(decisions, instances_count)
    save_routing_table(decisions, shares, Path(f"out/{run_id}/routing_table.json"))
    typer.echo(f"Routable share — raw: {shares['raw']:.1%}  compiled: {shares['compiled']:.1%}")

    # Full run
    cal_ids: set[str] = set()
    for ids in cal_result.get("cal_ids_by_task", {}).values():
        cal_ids.update(ids)
    run_instances = {k: [i for i in v if i.id not in cal_ids] for k, v in instances_by_task.items()}

    typer.echo("Running full pipeline...")
    await full_run(
        tasks=tasks,
        instances_by_task=run_instances,
        routing_decisions=decisions,
        recipients_by_id={recipient.id: recipient},
        run_id=run_id,
        all_issues=data["issues"],
    )

    # Report
    report_path = build_report(run_id, target)
    typer.echo(f"\nDone. Report: {report_path}")
    typer.echo(f"Run ID: {run_id}")


# ---------------------------------------------------------------------------
# Nosana subcommands
# ---------------------------------------------------------------------------

@nosana_app.command("balance")
def nosana_balance(config: Optional[Path] = typer.Option(None)):
    """Show available Nosana credits."""
    from .config import get_settings
    from .providers.nosana import NosanaClient
    settings = get_settings(config)
    if not settings.nosana_api_key:
        typer.echo("NOSANA_API_KEY is not set", err=True)
        raise typer.Exit(1)
    client = NosanaClient(settings.nosana_api_key, base_url=settings.nosana.base_url)
    bal = asyncio.run(client.get_balance())
    avail = asyncio.run(client.available_credits())
    typer.echo(json.dumps(bal, indent=2))
    typer.echo(f"\nAvailable credits: {avail:.4f}")


@nosana_app.command("markets")
def nosana_markets(config: Optional[Path] = typer.Option(None)):
    """List available Nosana markets."""
    from .config import get_settings
    from .providers.nosana import NosanaClient
    settings = get_settings(config)
    if not settings.nosana_api_key:
        typer.echo("NOSANA_API_KEY is not set", err=True)
        raise typer.Exit(1)
    client = NosanaClient(settings.nosana_api_key, base_url=settings.nosana.base_url)
    try:
        markets = asyncio.run(client.list_markets())
        typer.echo(json.dumps(markets, indent=2))
    except Exception as e:
        typer.echo(f"Could not fetch markets: {e}", err=True)
        typer.echo("(Market list may not be exposed via API — configure via relay.nosana.yaml)")


@nosana_app.command("up")
def nosana_up(
    model: str = typer.Argument(..., help="Model name from config"),
    config: Optional[Path] = typer.Option(None),
):
    """Start a Nosana deployment for a model."""
    asyncio.run(_nosana_up_async(model, config))


async def _nosana_up_async(model: str, config: Optional[Path]) -> None:
    from .config import get_settings
    from .providers.nosana import NosanaRecipient
    settings = get_settings(config)
    model_cfgs = [m for m in settings.nosana.models if m.name == model]
    if not model_cfgs:
        typer.echo(f"Model '{model}' not found in config", err=True)
        raise typer.Exit(1)
    model_cfg = model_cfgs[0]
    recipient = NosanaRecipient(
        model_cfg,
        settings.nosana_api_key,
        concurrency=settings.nosana.concurrency,
        ready_timeout_min=settings.nosana.ready_timeout_min,
        deployment_timeout_min=settings.nosana.deployment_timeout_min,
        max_credits_per_run=settings.nosana.max_credits_per_run,
    )
    await recipient._deploy()
    typer.echo(f"Deployment ID: {recipient._deployment_id}")
    typer.echo(f"Base URL: {recipient._base_url}")
    typer.echo(f"Startup: {recipient.startup_seconds:.0f}s")


@nosana_app.command("down")
def nosana_down(
    deployment_id: str = typer.Argument(...),
    config: Optional[Path] = typer.Option(None),
):
    """Stop a Nosana deployment."""
    from .config import get_settings
    from .providers.nosana import NosanaClient
    settings = get_settings(config)
    client = NosanaClient(settings.nosana_api_key, base_url=settings.nosana.base_url)
    asyncio.run(client.stop_deployment(deployment_id))
    typer.echo(f"Stopped {deployment_id}")


@nosana_app.command("cleanup")
def nosana_cleanup(config: Optional[Path] = typer.Option(None)):
    """Stop every running deployment whose name starts with relay-poc-."""
    asyncio.run(_cleanup_async(config))


async def _cleanup_async(config: Optional[Path]) -> None:
    from .config import get_settings
    from .providers.nosana import NosanaClient
    settings = get_settings(config)
    client = NosanaClient(settings.nosana_api_key, base_url=settings.nosana.base_url)
    deployments = await client.list_deployments()
    stopped = 0
    for d in deployments:
        name = d.get("name", "")
        status = d.get("status", "")
        if name.startswith("relay-poc-") and status not in ("STOPPED", "ARCHIVED"):
            did = d.get("id") or d.get("deploymentId")
            try:
                await client.stop_deployment(did)
                typer.echo(f"Stopped: {name} ({did})")
                stopped += 1
            except Exception as e:
                typer.echo(f"Failed to stop {name}: {e}", err=True)
    typer.echo(f"Cleanup complete. Stopped {stopped} deployment(s).")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_recipient(provider: str, settings):
    if provider == "mock":
        from .providers.mock import MockRecipient
        return MockRecipient(seed=settings.seed)
    elif provider == "local":
        from .providers.local import LocalRecipient
        return LocalRecipient("llama3", base_url="http://localhost:11434/v1")
    elif provider == "nosana":
        from .providers.nosana import NosanaRecipient
        if not settings.nosana.models:
            raise ValueError("No Nosana models configured")
        m = settings.nosana.models[0]
        return NosanaRecipient(
            m, settings.nosana_api_key,
            base_url=settings.nosana.base_url,
            max_credits_per_run=settings.nosana.max_credits_per_run,
        )
    elif provider == "frontier":
        from .providers.frontier import FrontierRecipient
        return FrontierRecipient(settings.frontier.model, api_key=settings.anthropic_api_key)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def _mock_issues() -> list[dict]:
    """Minimal synthetic issues for smoke tests and mock demo."""
    issues = []
    label_cycle = ["bug", "enhancement", "documentation", "question", "bug"]
    for i in range(1, 31):
        lbl = label_cycle[(i - 1) % len(label_cycle)]
        issues.append({
            "number": i,
            "title": f"Issue {i}: something {'crashes' if lbl=='bug' else 'to improve'}",
            "body": (
                f"Describe the {'bug' if lbl=='bug' else 'feature'} here.\n"
                f"Version: 1.{i}.0\n"
                f"Error: {'TypeError: cannot read property' if lbl=='bug' else ''}\n"
                f"Stack: at src/module{i}.py:42"
            ),
            "state": "CLOSED",
            "createdAt": f"2023-0{(i % 9) + 1}-01T00:00:00Z",
            "closedAt": f"2023-0{(i % 9) + 1}-15T00:00:00Z",
            "repository": "mock/repo",
            "author": {"login": f"user{i}"},
            "labels": {"nodes": [{"name": lbl}]},
            "comments": {"nodes": [
                {
                    "body": f"This seems related to module{i}.py",
                    "createdAt": f"2023-0{(i % 9) + 1}-02T00:00:00Z",
                    "author": {"login": "reviewer"},
                }
            ]},
            "timelineItems": {"nodes": [
                {
                    "__typename": "ClosedEvent",
                    "createdAt": f"2023-0{(i % 9) + 1}-15T00:00:00Z",
                    "closer": {
                        "__typename": "PullRequest",
                        "number": 100 + i,
                        "merged": True,
                        "mergedAt": f"2023-0{(i % 9) + 1}-15T00:00:00Z",
                        "baseRefOid": f"abc{i:04d}",
                        "changedFiles": [f"src/module{i}.py", f"src/util{i}.py"],
                    },
                }
            ]},
        })
    # Add a few duplicates
    for i in range(31, 36):
        issues.append({
            "number": i,
            "title": f"Duplicate of issue {i - 20}",
            "body": f"Same as #{i - 20}.",
            "state": "CLOSED",
            "createdAt": f"2023-06-01T00:00:00Z",
            "closedAt": f"2023-06-05T00:00:00Z",
            "repository": "mock/repo",
            "author": {"login": "user"},
            "labels": {"nodes": [{"name": "duplicate"}]},
            "comments": {"nodes": []},
            "timelineItems": {"nodes": [
                {
                    "__typename": "MarkedAsDuplicateEvent",
                    "createdAt": f"2023-06-05T00:00:00Z",
                    "canonical": {"number": i - 20},
                }
            ]},
        })
    return issues


def _mock_file_trees() -> dict[str, list[str]]:
    """One tree per mock base commit, keyed "<repository>/<baseRefOid>"."""
    return {f"mock/repo/abc{i:04d}": _mock_files() for i in range(1, 31)}


def _mock_files() -> list[str]:
    files = []
    for i in range(1, 31):
        files += [f"src/module{i}.py", f"src/util{i}.py"]
    files += ["README.md", "docs/guide.md", "tests/test_module.py"]
    return files
