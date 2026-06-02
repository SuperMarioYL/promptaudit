"""Run the compiled rule corpus over cached package text and emit findings.

Inputs:

* a list of ``ResolvedPackage`` (from ``resolver.resolve``)
* a cache root populated by ``fetcher.fetch_all`` (or a freshly-supplied root
  for tests / one-off scans)
* an optional override corpus path

Outputs: a list of ``Finding`` records, one per (package, file, rule, match)
quadruple. The scanner is deliberately a pure function over the cache so it
can be unit-tested without any network I/O.

Match locality: rule patterns are applied to the full text of each source file
(README / summary / errors). Once a match lands, the scanner walks back to the
nearest newline boundary to compute a (line, column) and pulls a single-line
snippet truncated to ``SNIPPET_MAX_CHARS`` for the report.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .fetcher import DEFAULT_CACHE_ROOT, cache_dir_for
from .resolver import ResolvedPackage
from .rules import SEVERITY_ORDER, CompiledRule, filter_for, load_rules

SOURCE_FILES = ("readme.md", "summary.txt", "errors.txt")
SNIPPET_MAX_CHARS = 240
# Cap matches per (file, rule) so a single repetitive payload doesn't drown the
# report. The fetcher already bounds file size; this is the second guard.
MAX_MATCHES_PER_RULE = 5


@dataclass(frozen=True)
class Finding:
    package: str
    version: str
    ecosystem: str
    source_file: str
    line: int
    column: int
    snippet: str
    rule_id: str
    rule_description: str
    severity: str
    via_path: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["via_path"] = list(self.via_path)
        return d


def scan(
    packages: Iterable[ResolvedPackage],
    cache_root: Path | None = None,
    *,
    corpus_path: Path | None = None,
    rules: list[CompiledRule] | None = None,
) -> list[Finding]:
    """Scan every cached source file of every package against the rule corpus."""
    cache_root = (cache_root or DEFAULT_CACHE_ROOT).expanduser()
    all_rules = rules if rules is not None else load_rules(corpus_path)
    findings: list[Finding] = []
    for pkg in packages:
        pkg_dir = cache_dir_for(pkg, cache_root)
        if not pkg_dir.exists():
            continue
        eco_rules = filter_for(all_rules, pkg.ecosystem)
        if not eco_rules:
            continue
        findings.extend(_scan_package(pkg, pkg_dir, eco_rules))
    findings.sort(key=_finding_sort_key)
    return findings


def scan_text(
    text: str,
    *,
    package: str = "<text>",
    version: str = "0.0.0",
    ecosystem: str = "any",
    source_file: str = "<inline>",
    via_path: tuple[str, ...] = (),
    rules: list[CompiledRule] | None = None,
    corpus_path: Path | None = None,
) -> list[Finding]:
    """Scan an arbitrary text blob. Used by tests and ad-hoc CLI input."""
    all_rules = rules if rules is not None else load_rules(corpus_path)
    eco_rules = filter_for(all_rules, ecosystem)
    findings = list(
        _scan_blob(
            text=text,
            package_name=package,
            version=version,
            ecosystem=ecosystem,
            source_file=source_file,
            via_path=via_path,
            rules=eco_rules,
        )
    )
    findings.sort(key=_finding_sort_key)
    return findings


def has_critical(findings: Iterable[Finding]) -> bool:
    """True iff at least one finding is severity=critical. Drives the CLI exit code."""
    return any(f.severity == "critical" for f in findings)


def summarize(findings: Iterable[Finding]) -> dict[str, int]:
    """Count findings by severity (always returns all three keys)."""
    counts = {"critical": 0, "high": 0, "medium": 0}
    for f in findings:
        if f.severity in counts:
            counts[f.severity] += 1
    return counts


def findings_to_json(findings: Iterable[Finding]) -> str:
    """Serialize findings to a stable JSON document for CI consumption."""
    payload = {
        "schema": "promptaudit.findings/v1",
        "findings": [f.to_dict() for f in findings],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _scan_package(
    pkg: ResolvedPackage, pkg_dir: Path, rules: list[CompiledRule]
) -> Iterable[Finding]:
    for source_name in SOURCE_FILES:
        source_path = pkg_dir / source_name
        if not source_path.exists():
            continue
        try:
            text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        yield from _scan_blob(
            text=text,
            package_name=pkg.name,
            version=pkg.version,
            ecosystem=pkg.ecosystem,
            source_file=str(source_path),
            via_path=pkg.via_path,
            rules=rules,
        )


def _scan_blob(
    *,
    text: str,
    package_name: str,
    version: str,
    ecosystem: str,
    source_file: str,
    via_path: tuple[str, ...],
    rules: list[CompiledRule],
) -> Iterable[Finding]:
    if not text:
        return
    line_starts = _line_starts(text)
    for rule in rules:
        matches = 0
        for match in rule.pattern.finditer(text):
            if matches >= MAX_MATCHES_PER_RULE:
                break
            matches += 1
            offset = match.start()
            line, column = _offset_to_line_col(offset, line_starts)
            snippet = _extract_snippet(text, match.start(), match.end())
            yield Finding(
                package=package_name,
                version=version,
                ecosystem=ecosystem,
                source_file=source_file,
                line=line,
                column=column,
                snippet=snippet,
                rule_id=rule.id,
                rule_description=rule.description,
                severity=rule.severity,
                via_path=tuple(via_path),
            )


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _offset_to_line_col(offset: int, line_starts: list[int]) -> tuple[int, int]:
    # Binary search for the largest line_start <= offset.
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1, offset - line_starts[lo] + 1


def _extract_snippet(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1  # 0 if no preceding newline
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    snippet = text[line_start:line_end].strip()
    if len(snippet) <= SNIPPET_MAX_CHARS:
        return snippet
    # Center the match within the truncated window.
    match_len = end - start
    pad = max(0, (SNIPPET_MAX_CHARS - match_len) // 2)
    rel_start = start - line_start
    window_start = max(0, rel_start - pad)
    window_end = min(len(snippet), window_start + SNIPPET_MAX_CHARS)
    prefix = "…" if window_start > 0 else ""
    suffix = "…" if window_end < len(snippet) else ""
    return f"{prefix}{snippet[window_start:window_end]}{suffix}"


def _finding_sort_key(finding: Finding) -> tuple:
    return (
        SEVERITY_ORDER.get(finding.severity, 99),
        finding.ecosystem,
        finding.package,
        finding.version,
        finding.source_file,
        finding.line,
        finding.rule_id,
    )
