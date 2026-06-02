"""Rule loading and compilation for the PromptAudit scanner.

The rule corpus lives at ``promptaudit/corpus/seed_payloads.yaml`` and is the
single source of truth for what counts as a prompt-injection payload. Each YAML
rule is compiled to a ``CompiledRule`` once at load time; the scanner then
matches every compiled rule against the cached package text.

Two ``kind`` values are supported:

* ``regex``   — Python ``re`` pattern. Whatever flags the pattern declares
  inline (e.g. ``(?is)``) are the ones used.
* ``literal`` — case-insensitive substring; compiled to ``re.escape(...)``
  with ``re.IGNORECASE`` so the scanner has a single matching codepath.

The ``ecosystems`` field gates whether a rule applies to a given source. The
sentinel ``any`` matches every ecosystem; ``mcp`` is a virtual surface keyed
off ecosystem hints in the cache (currently npm/PyPI only, but rules tagged
``mcp`` are still loaded so the corpus can describe MCP-server payloads
without a code change once an MCP cache lands).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Iterable

import yaml

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2}
VALID_SEVERITIES = frozenset(SEVERITY_ORDER)
VALID_KINDS = frozenset({"literal", "regex"})
ANY_ECOSYSTEM = "any"


class RuleError(ValueError):
    """Raised when the corpus YAML is malformed or a rule fails to compile."""


@dataclass(frozen=True)
class CompiledRule:
    id: str
    description: str
    severity: str
    ecosystems: tuple[str, ...]
    kind: str
    pattern_source: str
    pattern: re.Pattern[str]
    rationale: str = ""
    provenance: str = ""

    def applies_to(self, ecosystem: str) -> bool:
        if ANY_ECOSYSTEM in self.ecosystems:
            return True
        return ecosystem in self.ecosystems


def load_rules(corpus_path: Path | None = None) -> list[CompiledRule]:
    """Load and compile the seed corpus.

    With no argument, reads the packaged ``corpus/seed_payloads.yaml``. Pass a
    path to load an override corpus (test fixtures, user-supplied catalogs).
    """
    text = _read_corpus_text(corpus_path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuleError(f"corpus YAML is not parseable: {exc}") from exc

    if not isinstance(data, dict):
        raise RuleError("corpus root must be a mapping with a 'rules' key")
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleError("corpus must contain a non-empty 'rules' list")

    compiled: list[CompiledRule] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw_rules):
        rule = _compile_rule(entry, index)
        if rule.id in seen_ids:
            raise RuleError(f"duplicate rule id: {rule.id}")
        seen_ids.add(rule.id)
        compiled.append(rule)
    return compiled


def filter_for(rules: Iterable[CompiledRule], ecosystem: str) -> list[CompiledRule]:
    """Return the subset of ``rules`` that apply to ``ecosystem``."""
    return [r for r in rules if r.applies_to(ecosystem)]


def _read_corpus_text(corpus_path: Path | None) -> str:
    if corpus_path is not None:
        return Path(corpus_path).read_text(encoding="utf-8")
    # Packaged corpus — works whether installed or run from a source checkout.
    return (
        resources.files("promptaudit.corpus")
        .joinpath("seed_payloads.yaml")
        .read_text(encoding="utf-8")
    )


def _compile_rule(entry: object, index: int) -> CompiledRule:
    if not isinstance(entry, dict):
        raise RuleError(f"rule #{index} is not a mapping")

    rule_id = entry.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise RuleError(f"rule #{index} missing 'id'")

    severity = entry.get("severity")
    if severity not in VALID_SEVERITIES:
        raise RuleError(
            f"rule {rule_id} has invalid severity {severity!r}; "
            f"expected one of {sorted(VALID_SEVERITIES)}"
        )

    kind = entry.get("kind")
    if kind not in VALID_KINDS:
        raise RuleError(
            f"rule {rule_id} has invalid kind {kind!r}; "
            f"expected one of {sorted(VALID_KINDS)}"
        )

    pattern_source = entry.get("pattern")
    if not isinstance(pattern_source, str) or not pattern_source:
        raise RuleError(f"rule {rule_id} has empty 'pattern'")

    ecosystems_raw = entry.get("ecosystems") or [ANY_ECOSYSTEM]
    if not isinstance(ecosystems_raw, list) or not all(
        isinstance(e, str) for e in ecosystems_raw
    ):
        raise RuleError(f"rule {rule_id} 'ecosystems' must be a list of strings")
    ecosystems = tuple(ecosystems_raw)

    try:
        if kind == "literal":
            compiled_pattern = re.compile(re.escape(pattern_source), re.IGNORECASE)
        else:
            compiled_pattern = re.compile(pattern_source)
    except re.error as exc:
        raise RuleError(f"rule {rule_id} pattern failed to compile: {exc}") from exc

    description = entry.get("description") or ""
    rationale = entry.get("rationale") or ""
    provenance = entry.get("provenance") or ""
    if not isinstance(description, str):
        raise RuleError(f"rule {rule_id} 'description' must be a string")

    return CompiledRule(
        id=rule_id,
        description=description,
        severity=severity,
        ecosystems=ecosystems,
        kind=kind,
        pattern_source=pattern_source,
        pattern=compiled_pattern,
        rationale=rationale if isinstance(rationale, str) else "",
        provenance=provenance if isinstance(provenance, str) else "",
    )
