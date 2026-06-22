"""``promptaudit`` CLI entrypoint.

Three commands:

* ``scan``  — full pipeline (resolve → fetch → scan → report). The default
  command users invoke. Exits 1 if any ``critical`` finding lands, 0 otherwise.
* ``fetch`` — resolve + fetch only; useful for warming the cache in CI before
  the scan step, or for inspecting what corpus the scanner will see.
* ``rules`` — print the loaded corpus (counts by severity + IDs); helps users
  understand what the scanner is looking for without grepping YAML.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console

from . import __version__
from .fetcher import DEFAULT_CACHE_ROOT, fetch_all
from .report import render_json, render_terminal
from .resolver import ResolverError, resolve
from .rules import SEVERITY_ORDER, load_rules
from .scanner import UnscannedPackage, has_critical, scan

EXIT_OK = 0
EXIT_CRITICAL_FOUND = 1
EXIT_USAGE_ERROR = 2
EXIT_FETCH_ERROR = 3  # coverage gap: a dep could not be fetched / was unscanned


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="promptaudit")
def main() -> None:
    """Dep-tree scanner that catches prompt-injection payloads aimed at your Coding Agent."""


@main.command("scan")
@click.argument(
    "project_root",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=".",
)
@click.option(
    "--cache-root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help=f"Cache directory (default: {DEFAULT_CACHE_ROOT}).",
)
@click.option(
    "--corpus",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Override corpus YAML (default: packaged seed_payloads.yaml).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit JSON to stdout instead of the rich terminal report.",
)
@click.option(
    "--force-refetch",
    is_flag=True,
    default=False,
    help="Re-download package text even if a cache entry exists.",
)
@click.option(
    "--no-fetch",
    is_flag=True,
    default=False,
    help="Skip the fetch step; scan whatever is already in the cache.",
)
@click.option(
    "--fail-on-fetch-error",
    is_flag=True,
    default=False,
    help=(
        "Exit non-zero (3) if any dependency could not be fetched and was "
        "left unscanned. Use in CI to gate on scan coverage, not just findings."
    ),
)
def scan_cmd(
    project_root: Path,
    cache_root: Path | None,
    corpus: Path | None,
    as_json: bool,
    force_refetch: bool,
    no_fetch: bool,
    fail_on_fetch_error: bool,
) -> None:
    """Resolve, fetch, and scan PROJECT_ROOT for prompt-injection payloads."""
    console = Console(stderr=as_json)  # keep stdout clean when emitting JSON
    try:
        packages = resolve(project_root)
    except ResolverError as exc:
        click.echo(f"promptaudit: {exc}", err=True)
        sys.exit(EXIT_USAGE_ERROR)

    if not packages:
        click.echo("promptaudit: no dependencies resolved — nothing to scan.", err=True)
        sys.exit(EXIT_OK)

    unscanned: list[UnscannedPackage] = []
    if not no_fetch:
        console.print(
            f"[dim]Fetching corpus for {len(packages)} package(s)...[/dim]"
        )
        fetch_results = fetch_all(packages, cache_root=cache_root, force=force_refetch)
        for r in (r for r in fetch_results if r.status == "error"):
            console.print(
                f"[yellow]fetch warning:[/yellow] "
                f"{r.package.ecosystem} {r.package.name}@{r.package.version}: {r.message}"
            )
            unscanned.append(
                UnscannedPackage(
                    package=r.package.name,
                    version=r.package.version,
                    ecosystem=r.package.ecosystem,
                    reason=r.message or "fetch_error",
                    via_path=tuple(r.package.via_path),
                )
            )

    findings = scan(packages, cache_root=cache_root, corpus_path=corpus)

    if as_json:
        click.echo(render_json(findings, unscanned=unscanned))
    else:
        render_terminal(
            findings,
            console=console,
            scanned_packages=len(packages),
            unscanned=unscanned,
        )

    if has_critical(findings):
        sys.exit(EXIT_CRITICAL_FOUND)
    if unscanned and fail_on_fetch_error:
        sys.exit(EXIT_FETCH_ERROR)
    sys.exit(EXIT_OK)


@main.command("fetch")
@click.argument(
    "project_root",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=".",
)
@click.option(
    "--cache-root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
)
@click.option("--force", is_flag=True, default=False, help="Re-fetch even if cached.")
def fetch_cmd(project_root: Path, cache_root: Path | None, force: bool) -> None:
    """Resolve PROJECT_ROOT and warm the per-package text cache."""
    try:
        packages = resolve(project_root)
    except ResolverError as exc:
        click.echo(f"promptaudit: {exc}", err=True)
        sys.exit(EXIT_USAGE_ERROR)
    results = fetch_all(packages, cache_root=cache_root, force=force)
    ok = sum(1 for r in results if r.status == "ok")
    skipped = sum(1 for r in results if r.status == "skipped")
    errored = sum(1 for r in results if r.status == "error")
    click.echo(
        f"fetched: ok={ok} skipped={skipped} errors={errored} "
        f"(cache={(cache_root or DEFAULT_CACHE_ROOT)})"
    )
    sys.exit(EXIT_OK if errored == 0 else EXIT_CRITICAL_FOUND)


@main.command("rules")
@click.option(
    "--corpus",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
)
def rules_cmd(corpus: Path | None) -> None:
    """List the loaded rule corpus, grouped by severity."""
    rules = load_rules(corpus)
    rules_by_severity: dict[str, list] = {sev: [] for sev in SEVERITY_ORDER}
    for r in rules:
        rules_by_severity.setdefault(r.severity, []).append(r)
    console = Console()
    console.print(f"[bold]PromptAudit corpus[/bold]: {len(rules)} rules loaded")
    for severity in sorted(rules_by_severity, key=lambda s: SEVERITY_ORDER.get(s, 99)):
        entries = rules_by_severity[severity]
        if not entries:
            continue
        console.print(f"\n[bold]{severity.upper()}[/bold] ({len(entries)})")
        for r in entries:
            console.print(f"  [dim]{r.id}[/dim]  {r.description}")


if __name__ == "__main__":  # pragma: no cover
    main()
