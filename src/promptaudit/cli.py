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
from .scanner import (
    UnscannedPackage,
    has_critical,
    packages_with_coverage,
    scan,
)

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
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress non-essential output (e.g. the post-scan star nudge).",
)
def scan_cmd(
    project_root: Path,
    cache_root: Path | None,
    corpus: Path | None,
    as_json: bool,
    force_refetch: bool,
    no_fetch: bool,
    fail_on_fetch_error: bool,
    quiet: bool,
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

    # Per-package fetch reasons (status="error" → coverage gap). Keyed by
    # (ecosystem, name, version) so the coverage pass below can attach the
    # fetch reason to the matching UnscannedPackage instead of a generic
    # "no_cache" when surfacing cold-cache misses under --no-fetch.
    fetch_reasons: dict[tuple[str, str, str], str] = {}
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
            fetch_reasons[
                (r.package.ecosystem, r.package.name, r.package.version)
            ] = r.message or "fetch_error"

    findings = scan(packages, cache_root=cache_root, corpus_path=corpus)

    # Coverage: which resolved packages actually had cached source text to scan?
    # A cold/partial cache under --no-fetch (or a total fetch failure) must not
    # masquerade as a clean scan — surface the unscanned packages and report the
    # actually-scanned count, not the resolved count.
    scanned_pkgs, missing_pkgs = packages_with_coverage(
        packages, cache_root=cache_root
    )
    unscanned: list[UnscannedPackage] = []
    for pkg in missing_pkgs:
        reason = fetch_reasons.get(
            (pkg.ecosystem, pkg.name, pkg.version), "no_cache"
        )
        unscanned.append(
            UnscannedPackage(
                package=pkg.name,
                version=pkg.version,
                ecosystem=pkg.ecosystem,
                reason=reason,
                via_path=tuple(pkg.via_path),
            )
        )
    actually_scanned = len(scanned_pkgs)

    if as_json:
        click.echo(render_json(findings, unscanned=unscanned))
    else:
        render_terminal(
            findings,
            console=console,
            scanned_packages=actually_scanned,
            unscanned=unscanned,
        )

    # Star CTA — one small line after a completed scan, suppressed for machine
    # output (--json) or when quiet. pipx/PyPI installers bypass the GitHub page
    # where starring happens, so this reconnects the install funnel to the star
    # funnel (<500 stars is the project's survival metric).
    if not as_json and not quiet:
        console.print(
            "[dim]★ star if PromptAudit caught a payload you'd have shipped: "
            "https://github.com/SuperMarioYL/promptaudit[/dim]"
        )

    if actually_scanned == 0:
        # Zero packages scanned = a coverage failure (cold cache under
        # --no-fetch, or every fetch errored). Never exit 0 clean — a cold-cache
        # --no-fetch run must not masquerade as a clean scan.
        console.print(
            "[bold red]✗ scanned 0 packages — run without --no-fetch to populate "
            "the cache, or review the fetch warnings above.[/bold red]"
        )
        sys.exit(EXIT_FETCH_ERROR)
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
