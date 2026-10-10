"""Varda as a library: load, check, generate, write.

The command line is a thin layer over this module, so a pre-commit hook, a
dbt integration or a notebook gets the same behaviour it does — the same
rules, the same exemptions, the same refusal to generate from a model that
does not conform — without importing anything private.

Nothing here prints and nothing here exits. A run that should stop raises a
typed error carrying what the caller needs to explain why.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import _linkml
from .ext import (
    Context,
    Issue,
    Options,
    Report,
    RuleError,
    Severity,
)
from .registry import Registry
from .snapshot import table_of

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .ext import Finding, Generator, Rule
    from .snapshot import Model

__all__ = [
    "Generation",
    "GeneratorFailedError",
    "NonconformingError",
    "UnknownGeneratorError",
    "check",
    "generate",
    "load",
    "write",
]


def load(path: str | pathlib.Path, registry: Registry | None = None) -> Model:
    """Read a model under an extension set.

    Without one, the set is discovered from the model's own directory —
    installed extensions and the nearest ``varda.toml`` above the model —
    so the answer depends on where the model is and not on where the
    caller happens to be running.
    """
    source = pathlib.Path(path)
    if registry is None:
        registry = Registry.discover(source.parent)
    return _linkml.read(source, registry)


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


def check(
    model: Model,
    *,
    exempt: Iterable[str] = (),
    skip_imported: bool = False,
) -> Report:
    """Run every rule of the model's extension set and report the findings.

    ``exempt`` names rules to skip, in addition to the ones ``varda.toml``
    exempts. Severity overrides come from the registry, which has already
    refused any two extensions disagree about.

    ``skip_imported`` drops the findings whose subject was declared in
    another file — not the tables, the findings. Every rule still sees every
    table, which is the point: a physical name that collides with an
    imported one is a fault of *this* model that only a rule seeing both can
    find. What the flag removes is the report of a fault somebody else has
    to fix.

    A rule that raises stops the run with a :class:`~varda.ext.RuleError`
    naming it and the extension that shipped it. Reporting the surviving
    rules and passing would be the tool saying a model conforms when part of
    the question was never asked.
    """
    registry = model.registry
    skip = {*registry.exemptions, *exempt}
    overrides = registry.severities
    issues: list[Issue] = []
    ran = 0
    for rule in registry.rules:
        if rule.code in skip:
            continue
        ran += 1
        for finding in _run(registry, rule, model):
            table = table_of(finding.subject)
            if table.is_imported and skip_imported:
                continue
            issues.append(
                _issue(rule, finding, overrides.get(rule.code, rule.severity))
            )
    return Report(
        model=model,
        issues=tuple(issues),
        rules_run=ran,
        stale_exemptions=registry.unknown_codes(skip),
        stale_overrides=registry.unknown_codes(overrides),
    )


def _run(registry: Registry, rule: Rule, model: Model) -> list[Finding]:
    """Run one rule to completion, or raise naming it.

    Drained here rather than streamed to the caller. A rule is usually a
    generator, so its body does not run until something iterates it, and
    iterating inside the ``try`` is the only place its raise can be caught.
    """
    try:
        return list(rule.check(model))
    except Exception as exc:
        owner = registry.owner(rule.code)
        msg = f"{rule.code} ({owner}) failed: {type(exc).__name__}: {exc}"
        raise RuleError(msg) from exc


def _issue(rule: Rule, finding: Finding, severity: Severity) -> Issue:
    """Report one finding, with what only the engine can say about it."""
    return Issue(
        rule=rule,
        severity=severity,
        subject=finding.subject,
        message=finding.message,
        origin=table_of(finding.subject).origin or "",
    )


def blocking(report: Report, *, strict: bool = False) -> tuple[Issue, ...]:
    """Select the issues that stop a run: errors, and warnings if strict."""
    stop = {Severity.ERROR, Severity.WARNING} if strict else {Severity.ERROR}
    return tuple(i for i in report.issues if i.severity in stop)


# ---------------------------------------------------------------------------
# Generating
# ---------------------------------------------------------------------------


class UnknownGeneratorError(KeyError):
    """A run asked for a generator nobody registers."""

    def __str__(self) -> str:
        """Render the message rather than its quoted repr."""
        return str(self.args[0])


class NonconformingError(Exception):
    """The model is not fit to generate from, and nothing was run.

    ``blocking`` holds the issues that stopped it, and is empty when what
    stopped it was a configured code naming no rule under ``strict``.
    """

    def __init__(self, report: Report, blocking: tuple[Issue, ...]) -> None:
        """Hold what stopped the run."""
        super().__init__("the model does not conform")
        self.report = report
        self.blocking = blocking


class GeneratorFailedError(Exception):
    """A generator raised, or produced a path it never declared."""


@dataclass(frozen=True)
class Generation:
    """What one run produced, before anything is written."""

    report: Report
    #: The issues that would have stopped the run, generated past because
    #: ``force`` was given. Empty for a model that conforms.
    forced_past: tuple[Issue, ...]
    #: ``{relative path: content}``, every generator's output together.
    artifacts: dict[str, str] = field(default_factory=dict)
    generators: tuple[str, ...] = ()


def generate(  # noqa: PLR0913 — one keyword per CLI flag, all optional
    model: Model,
    options: Options | None = None,
    *,
    only: Sequence[str] | None = None,
    exempt: Iterable[str] = (),
    skip_imported: bool = False,
    strict: bool = False,
    force: bool = False,
) -> Generation:
    """Check a model, then run its generators, writing nothing.

    Errors refuse the run before a generator is reached — under ``strict``,
    warnings and configured codes naming no rule do too — because artifacts
    built from a model that does not conform look finished and are not.
    ``force`` generates regardless, for somebody mid-refactor who wants to
    see the output.

    Then every generator runs and every result is collected before anything
    is returned, so :func:`write` is never handed half a tree.
    """
    chosen = _selected(model.registry, only)
    report = check(model, exempt=exempt, skip_imported=skip_imported)
    if strict and report.stale:
        raise NonconformingError(report, ())
    stopping = blocking(report, strict=strict)
    if stopping and not force:
        raise NonconformingError(report, stopping)
    ctx = Context(model=model, options=options or Options())
    return Generation(
        report=report,
        forced_past=stopping,
        artifacts=_collect(chosen, ctx),
        generators=tuple(g.name for g in chosen),
    )


def _selected(
    registry: Registry, only: Sequence[str] | None
) -> list[Generator]:
    """Pick the generators to run, or raise on a name nobody registers."""
    available = registry.generators
    if not only:
        return list(available)
    by_name = {g.name: g for g in available}
    missing = [n for n in only if n not in by_name]
    if missing:
        known = ", ".join(sorted(by_name)) or "none"
        msg = f"unknown generator(s): {', '.join(missing)}. Known: {known}"
        raise UnknownGeneratorError(msg)
    return [by_name[n] for n in only]


def _collect(chosen: list[Generator], ctx: Context) -> dict[str, str]:
    """Run every generator and gather what it produced."""
    collected: dict[str, str] = {}
    for gen in chosen:
        try:
            produced = gen.run(ctx)
        except Exception as exc:
            # Deliberately broad. A third-party generator may raise anything,
            # and the job here is to fail closed with a message naming the
            # culprit rather than a traceback through somebody else's code.
            msg = f"{gen.name} failed: {type(exc).__name__}: {exc}"
            raise GeneratorFailedError(msg) from exc
        undeclared = sorted(set(produced) - set(gen.artifacts))
        if undeclared:
            msg = (
                f"{gen.name} wrote undeclared path(s): {', '.join(undeclared)}"
            )
            raise GeneratorFailedError(msg)
        collected.update(produced)
    return collected


def write(
    generation: Generation, out: str | pathlib.Path
) -> list[pathlib.Path]:
    """Write a run's artifacts under ``out``, in path order."""
    root = pathlib.Path(out)
    written: list[pathlib.Path] = []
    for rel, content in sorted(generation.artifacts.items()):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written
