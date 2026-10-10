"""The command line.

Six commands, each doing one thing, each a call into :mod:`varda.api` and
the printing around it. Exit codes are part of the contract because these
run in CI: ``0`` success, ``1`` the model or the run failed, ``2`` the
invocation was wrong. A tool that returns ``0`` for "I could not tell" is one
that turns a red build green.
"""

from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import sys
from typing import TYPE_CHECKING

from . import api, gen_sql
from .ext import ExtensionError, Options, RuleError
from .registry import Registry
from .vocabulary import TARGETS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .ext import Issue, Report
    from .snapshot import Model

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def _dialects() -> str:
    """Name the dialects the core ships, for the flag's help text."""
    return ", ".join(sorted(gen_sql.DIALECTS))


def _levels() -> str:
    """Name the constraint levels, for the flag's help text."""
    return ", ".join(gen_sql.LEVELS)


def _registry(
    args: argparse.Namespace, start: pathlib.Path | None = None
) -> Registry:
    """Settle the extension set for this run.

    The one handed to :func:`main`, if any; otherwise discovered from
    ``start`` — the model's directory, for a command that reads a model.
    """
    given: Registry | None = args.registry
    if given is not None:
        return given
    return Registry.discover(start, config=args.config)


def _load(args: argparse.Namespace) -> Model:
    """Load the model a command names, under its extension set."""
    path = pathlib.Path(args.model)
    return api.load(path, _registry(args, path.parent))


def _extensions_line(registry: Registry) -> str:
    """Name the active extensions, for the header of a run."""
    return ", ".join(f"{e.name} {e.version}" for e in registry.extensions)


def _stale(report: Report) -> bool:
    """Warn about every configured code no active extension registers.

    Exemptions and severity overrides together, because both name a rule
    and both go on meaning nothing the moment that rule is retired. Returns
    whether anything was reported; whether that stops the run is the
    caller's question, and the answer is yes under ``--strict`` only.
    """
    found = (
        ("exemption", report.stale_exemptions),
        ("severity override", report.stale_overrides),
    )
    for what, codes in found:
        if codes:
            print(
                f"warning: {what} names no registered rule: {'; '.join(codes)}",
                file=sys.stderr,
            )
    return report.stale


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _scope(model: Model, *, skipped: bool) -> str:
    """Say how many of the checked tables came from another file.

    Silent for a model in one file, which is most of them.
    """
    imported = sum(1 for t in model.tables if t.is_imported)
    if not imported:
        return ""
    return f" ({imported} imported, not reported)" if skipped else ""


def cmd_check(args: argparse.Namespace) -> int:
    """Validate a model against every active extension's rules."""
    model = _load(args)
    report = api.check(
        model, exempt=args.exempt or (), skip_imported=args.skip_imported
    )
    if _stale(report) and args.strict:
        return EXIT_FAIL

    for issue in report.issues:
        print(issue)

    errors, warnings = len(report.errors), len(report.warnings)
    print(
        f"\n{len(model.tables)} tables checked"
        f"{_scope(model, skipped=args.skip_imported)}"
        f" against {len(model.registry.rules)} rules "
        f"({_extensions_line(model.registry)}): "
        f"{errors} errors, {warnings} warnings"
    )
    if errors or (args.strict and warnings):
        return EXIT_FAIL
    return EXIT_OK


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def _report(issues: Sequence[Issue], *, forced: bool) -> None:
    """Print what is wrong, and what it means for this run."""
    for issue in issues:
        print(issue, file=sys.stderr)
    counts = {
        severity: sum(1 for i in issues if i.severity == severity)
        for severity in ("error", "warning")
    }
    found = ", ".join(
        f"{n} {s}" if n == 1 else f"{n} {s}s" for s, n in counts.items() if n
    )
    tail = (
        "generating anyway because --force was given"
        if forced
        else "nothing was written. Pass --force to generate anyway"
    )
    print(f"\n{found}; {tail}", file=sys.stderr)


def _generation(args: argparse.Namespace) -> api.Generation | int:
    """Check and generate as ``generate`` and ``verify`` both do.

    Returns the run, or the exit code that ends it — having printed why.
    Shared, so the two commands cannot disagree about whether a model is fit
    to generate from or what it generates.
    """
    model = _load(args)
    if not model.registry.generators:
        print("no generators registered", file=sys.stderr)
        return EXIT_USAGE
    options = Options(
        schema=args.schema,
        dialect=args.dialect,
        constraints=args.constraints,
    )
    try:
        generation = api.generate(
            model,
            options,
            only=args.only,
            exempt=args.exempt or (),
            skip_imported=args.skip_imported,
            strict=args.strict,
            force=args.force,
        )
    except api.UnknownGeneratorError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except api.NonconformingError as exc:
        _stale(exc.report)
        if exc.blocking:
            _report(exc.blocking, forced=False)
        return EXIT_FAIL
    except api.GeneratorFailedError as exc:
        print(f"{exc}\nnothing was written", file=sys.stderr)
        return EXIT_FAIL

    _stale(generation.report)
    if generation.forced_past:
        _report(generation.forced_past, forced=True)
    return generation


def cmd_generate(args: argparse.Namespace) -> int:
    """Run generators and write their output.

    :func:`varda.api.generate` checks the model and refuses one that does
    not conform, then runs every generator and collects every result before
    anything is written. This prints what it said, and writes.
    """
    generation = _generation(args)
    if isinstance(generation, int):
        return generation
    written = api.write(generation, args.out)
    for target in written:
        print(f"wrote {target}")
    print(
        f"\n{len(written)} artifacts from "
        f"{len(generation.generators)} generators"
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _diff(generation: api.Generation, out: pathlib.Path, rel: str) -> str:
    """Show how a file on disk differs from what the model produces."""
    on_disk = api.read_committed(out / rel) or ""
    return "".join(
        difflib.unified_diff(
            on_disk.splitlines(keepends=True),
            generation.artifacts[rel].splitlines(keepends=True),
            fromfile=f"{rel} (on disk)",
            tofile=f"{rel} (from the model)",
        )
    )


def cmd_verify(args: argparse.Namespace) -> int:
    """Say whether the files under ``--out`` are what the model produces.

    Generates exactly as ``generate`` would, writes nothing, and compares.
    Exits ``1`` on any difference, so CI fails on output somebody forgot to
    regenerate rather than deploying it.
    """
    generation = _generation(args)
    if isinstance(generation, int):
        return generation
    out = pathlib.Path(args.out)
    drift = api.compare(generation, out)
    for rel in drift.changed:
        print(f"{out / rel} differs")
        if args.diff:
            print(_diff(generation, out, rel), end="")
    for rel in drift.missing:
        print(f"{out / rel} is missing")
    for rel in drift.extra:
        # `generate` writes and never deletes, so it cannot fix this one.
        print(f"{out / rel} is no longer generated; delete it")
    ran = f"from {len(generation.generators)} generators"
    if drift.clean:
        print(f"{len(generation.artifacts)} artifacts {ran} up to date")
        return EXIT_OK
    if drift.changed or drift.missing:
        n = len(drift.changed) + len(drift.missing)
        print(
            f"\n{n} of {len(generation.artifacts)} artifacts {ran} out of "
            "date; run `varda generate` with the same options to update them"
        )
    return EXIT_FAIL


# ---------------------------------------------------------------------------
# The rules, ext and importmap commands
# ---------------------------------------------------------------------------


def cmd_rules(args: argparse.Namespace) -> int:
    """List every registered rule."""
    registry = _registry(args)
    overrides = registry.severities
    for rule in registry.rules:
        effective = overrides.get(rule.code, rule.severity)
        mark = "*" if effective != rule.severity else " "
        print(f"{rule.code}  {effective:<7}{mark} {rule.title}")
        if args.verbose and rule.doc:
            for line in rule.doc.strip().splitlines():
                print(f"          {line.strip()}")
            print()
    print(f"\n{len(registry.rules)} rules ({_extensions_line(registry)})")
    return EXIT_OK


def cmd_ext(args: argparse.Namespace) -> int:
    """Describe the active extensions."""
    registry = _registry(args)
    for active in registry.extensions:
        if args.name and active.name != args.name:
            continue
        print(f"{active.name} {active.version}  [{active.prefix}:]")
        print(f"    origin      {active.origin or 'unknown'}")
        print(f"    rule tag    {active.rule_tag}")
        n_rules = len(active.rules.rules) if active.rules else 0
        print(f"    rules       {n_rules}")
        if active.profile:
            print(f"    profile     {active.profile}")
            for target in TARGETS:
                tags = sorted(
                    t
                    for t in registry.vocabulary.declared(target)
                    if t.startswith(f"{active.prefix}:")
                )
                if tags:
                    print(f"    {target:<11} {', '.join(tags)}")
        if active.generators:
            names = ", ".join(g.name for g in active.generators)
            print(f"    generators  {names}")
        print()
    return EXIT_OK


def cmd_importmap(args: argparse.Namespace) -> int:
    """Print the map that resolves symbolic imports.

    ``--json`` prints it in the shape LinkML's own generators read, which is
    the whole reason a model may write `imports: - varda` and still be a
    schema other tools can build from::

        varda importmap --json > im.json
        gen-erdiagram --importmap im.json mart.yaml

    The default stays one line per prefix, for reading.
    """
    found = _registry(args).importmap
    if args.json:
        print(json.dumps(found, indent=2, sort_keys=True))
        return EXIT_OK
    for prefix, path in sorted(found.items()):
        print(f"{prefix}={path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _generation_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the flags ``generate`` and ``verify`` share.

    One definition, so a run ``verify`` compares is the run ``generate``
    with the same flags would write.
    """
    parser.add_argument("model")
    parser.add_argument("--out", default="out", help="output directory")
    parser.add_argument("--schema", default="mart", help="SQL schema name")
    # Not `choices`. The dialect reaches a generator as a plain string, and
    # an extension shipping its own SQL generator may know names the core
    # does not — argparse refusing them here would make the core's table the
    # limit of everyone's. `gen_sql.dialect` raises, naming what it knows.
    parser.add_argument(
        "--dialect",
        default=gen_sql.DEFAULT_DIALECT,
        metavar="NAME",
        help=f"which database the DDL is for ({_dialects()})",
    )
    # Not `choices`, for the reason above: the levels are Varda's vocabulary
    # but the string reaches a generator, and an extension's SQL generator
    # may resolve it against engines the core does not name.
    parser.add_argument(
        "--constraints",
        default=gen_sql.DEFAULT_LEVEL,
        metavar="LEVEL",
        help=(
            f"how much the database is asked to police ({_levels()}); "
            f"weaker levels move the claims into sql/assertions.sql"
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help="run only this generator; repeatable",
    )
    parser.add_argument(
        "--exempt",
        action="append",
        metavar="CODE",
        help="skip a rule; repeatable",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="stop on warnings as well as errors",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="generate even though the model does not conform",
    )
    # Here as well as on `check`, so the two commands cannot disagree about
    # whether a model is fit to generate from. It stops another schema's
    # findings from blocking this run; it does not stop the imported tables
    # being emitted, which they must be — a fact referencing a dimension the
    # file never creates is DDL that does not run.
    parser.add_argument(
        "--skip-imported",
        action="store_true",
        help=(
            "do not let findings against another schema's classes stop the "
            "run; their tables are still emitted"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="varda",
        description="Dimensional modeling for LinkML.",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        metavar="PATH",
        help=(
            "read this varda.toml instead of searching upward from the "
            "model's directory"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="validate a model")
    p_check.add_argument("model")
    p_check.add_argument(
        "--exempt",
        action="append",
        metavar="CODE",
        help="skip a rule; repeatable",
    )
    p_check.add_argument(
        "--strict",
        action="store_true",
        help="fail on warnings and on exemptions that name no rule",
    )
    p_check.add_argument(
        "--skip-imported",
        action="store_true",
        help=(
            "do not report findings against classes another schema declares; "
            "every rule still sees them"
        ),
    )
    p_check.set_defaults(fn=cmd_check)

    p_gen = sub.add_parser("generate", help="write artifacts from a model")
    _generation_arguments(p_gen)
    p_gen.set_defaults(fn=cmd_generate)

    p_verify = sub.add_parser(
        "verify",
        help="check the files under --out are what the model produces",
    )
    _generation_arguments(p_verify)
    p_verify.add_argument(
        "--diff",
        action="store_true",
        help="show how each file that differs would change",
    )
    p_verify.set_defaults(fn=cmd_verify)

    p_rules = sub.add_parser("rules", help="list conformance rules")
    p_rules.add_argument("-v", "--verbose", action="store_true")
    p_rules.set_defaults(fn=cmd_rules)

    p_ext = sub.add_parser("ext", help="describe active extensions")
    p_ext.add_argument("name", nargs="?")
    p_ext.set_defaults(fn=cmd_ext)

    p_map = sub.add_parser("importmap", help="print the LinkML import map")
    p_map.add_argument(
        "--json",
        action="store_true",
        help="print it as JSON, for a generator's --importmap",
    )
    p_map.set_defaults(fn=cmd_importmap)

    return parser


def main(
    argv: Sequence[str] | None = None, *, registry: Registry | None = None
) -> int:
    """Entry point.

    ``registry`` replaces discovery outright, for a caller that has already
    decided what is active — a test, or a tool embedding this one.
    """
    args = build_parser().parse_args(argv)
    args.registry = registry
    if args.config is not None and not args.config.is_file():
        print(f"no such file: {args.config}", file=sys.stderr)
        return EXIT_USAGE
    try:
        result: int = args.fn(args)
    except ExtensionError as exc:
        print(f"extension error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except RuleError as exc:
        # Caught here rather than in `cmd_check`, so that `generate` gets it
        # too: it validates before it writes, so a rule that raises stops it
        # at the same point an error in the model would, with nothing
        # written.
        print(f"rule error: {exc}\nnothing was checked", file=sys.stderr)
        return EXIT_FAIL
    except FileNotFoundError as exc:
        print(f"no such file: {exc.filename}", file=sys.stderr)
        return EXIT_USAGE
    return result


if __name__ == "__main__":
    raise SystemExit(main())
