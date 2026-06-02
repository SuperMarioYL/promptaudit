"""Rich terminal rendering for scan findings.

Two rendering modes:

* ``render_terminal`` — grouped, colorized rich table. Critical findings flagged
  red. Includes a summary line and a footer pointing at the hosted-CI tier
  (per go_to_market §8 / the commercial-audience build requirement).
* ``findings_to_json`` (re-exported from ``scanner``) — machine output for CI.

This module deliberately does no scanning — it consumes a ``list[Finding]``
and prints it. Keeping rendering separate means the same finding stream feeds
both human and machine consumers without divergence.
"""

from __future__ import annotations

from typing import Iterable

from rich.box import SIMPLE_HEAVY
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .scanner import Finding, findings_to_json, summarize

SEVERITY_STYLES = {
    "critical": "bold red",
    "high": "bold yellow",
    "medium": "cyan",
}
WAITLIST_URL = "https://github.com/supermario-leo/promptaudit#hosted-ci-waitlist"


def render_terminal(
    findings: Iterable[Finding],
    *,
    console: Console | None = None,
    scanned_packages: int | None = None,
) -> None:
    """Print a human-readable report to the terminal."""
    findings = list(findings)
    console = console or Console()

    if not findings:
        console.print(
            Panel(
                Text.assemble(
                    ("PromptAudit", "bold green"),
                    " — no prompt-injection payloads found.\n",
                    (
                        f"Scanned {scanned_packages} packages."
                        if scanned_packages is not None
                        else "Scanned cached package corpus."
                    ),
                ),
                border_style="green",
                box=SIMPLE_HEAVY,
            )
        )
        _print_footer(console)
        return

    counts = summarize(findings)
    header_text = Text.assemble(
        ("PromptAudit", "bold magenta"),
        " — ",
        (f"{counts['critical']} critical", SEVERITY_STYLES["critical"]),
        ", ",
        (f"{counts['high']} high", SEVERITY_STYLES["high"]),
        ", ",
        (f"{counts['medium']} medium", SEVERITY_STYLES["medium"]),
        f"  (across {len({(f.ecosystem, f.package, f.version) for f in findings})} packages)",
    )
    console.print(Panel(header_text, border_style="magenta", box=SIMPLE_HEAVY))

    table = Table(
        show_header=True,
        header_style="bold",
        box=SIMPLE_HEAVY,
        expand=True,
        pad_edge=False,
    )
    table.add_column("Sev", no_wrap=True, width=8)
    table.add_column("Package", no_wrap=True, overflow="fold", max_width=28)
    table.add_column("Rule", no_wrap=True, width=10)
    table.add_column("Location", no_wrap=True, overflow="fold", max_width=40)
    table.add_column("Snippet", overflow="fold")

    for f in findings:
        sev_style = SEVERITY_STYLES.get(f.severity, "white")
        pkg_label = f"{f.ecosystem}:{f.package}@{f.version}"
        location = f"{_shorten_path(f.source_file)}:{f.line}"
        snippet_text = Text(f.snippet, style=sev_style if f.severity == "critical" else "")
        table.add_row(
            Text(f.severity.upper(), style=sev_style),
            pkg_label,
            f.rule_id,
            location,
            snippet_text,
        )
    console.print(table)

    if counts["critical"]:
        console.print(
            Text.assemble(
                ("✗ ", "bold red"),
                (f"{counts['critical']} critical finding(s) — CI will fail.", "bold red"),
            )
        )
    elif counts["high"] or counts["medium"]:
        console.print(
            Text.assemble(
                ("⚠ ", "bold yellow"),
                ("non-critical findings only — review and triage.", "bold yellow"),
            )
        )

    _print_footer(console)


def render_json(findings: Iterable[Finding]) -> str:
    """JSON output for CI / machine consumers."""
    return findings_to_json(findings)


def _print_footer(console: Console) -> None:
    console.print(
        Text.assemble(
            ("Hosted CI (PR-blocking + daily corpus updates): ", "dim"),
            (WAITLIST_URL, "dim underline"),
        )
    )


def _shorten_path(path: str) -> str:
    """Trim cache root noise so the report stays readable."""
    parts = path.replace("\\", "/").split("/")
    if len(parts) <= 4:
        return path
    return ".../" + "/".join(parts[-3:])
