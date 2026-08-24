"""The command line.

Five commands, each doing one thing. Exit codes are part of the contract
because these run in CI: ``0`` success, ``1`` the model or the run failed,
``2`` the invocation was wrong. A tool that returns ``0`` for "I could not
tell" is one that turns a red build green.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import TYPE_CHECKING

from . import registry
from .ext import Context, ExtensionError
from .model import DimensionalModel
from .rules import Finding, all_rules, check, unknown_codes

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .ext import Generator

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def _load(path: str) -> DimensionalModel:
    """Load a model, resolving symbolic profile imports."""
    return DimensionalModel.load(path, importmap=registry.importmap())


def _extensions_line() -> str:
    """Name the active extensions, for the header of a run."""
    names = [
        f"{active.name} {active.version}" for active in registry.extensions()
    ]
    return ", ".join(names)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    """Validate a model against every active extension's rules."""
    model = _load(args.model)
    exempt = [*registry.exemptions(), *(args.exempt or [])]

    stale = unknown_codes(exempt)
    if stale:
        where = "; ".join(stale)
        print(
            f"warning: exemption names no registered rule: {where}",
            file=sys.stderr,
        )
        if args.strict:
            return EXIT_FAIL

    findings = check(model, exemptions=exempt)
    for finding in findings:
        print(finding)

    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")
    tables = len(model.tables)
    print(
        f"\n{tables} tables checked against {len(all_rules())} rules "
        f"({_extensions_line()}): {errors} errors, {warnings} warnings"
    )
    if errors or (args.strict and warnings):
        return EXIT_FAIL
    return EXIT_OK


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def _selected(only: Sequence[str] | None) -> list[Generator]:
    """Pick the generators to run, or raise on a name nobody registers."""
    available = registry.generators()
    if not only:
        return list(available)
    by_name = {g.name: g for g in available}
    missing = [n for n in only if n not in by_name]
    if missing:
        known = ", ".join(sorted(by_name)) or "none"
        msg = f"unknown generator(s): {', '.join(missing)}. Known: {known}"
        raise KeyError(msg)
    return [by_name[n] for n in only]


def _blocking(
    model: DimensionalModel, args: argparse.Namespace
) -> list[Finding]:
    """Collect the findings that should stop a run, given its flags.

    The same rules `check` runs and the same exemptions, so the two commands
    cannot disagree about whether a model is fit to generate from.
    """
    exempt = [*registry.exemptions(), *(args.exempt or [])]
    stop = {"error", "warning"} if args.strict else {"error"}
    return [f for f in check(model, exemptions=exempt) if f.severity in stop]


def _report(findings: list[Finding], *, forced: bool) -> None:
    """Print what is wrong, and what it means for this run."""
    for finding in findings:
        print(finding, file=sys.stderr)
    counts = {
        severity: sum(1 for f in findings if f.severity == severity)
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


def cmd_generate(args: argparse.Namespace) -> int:
    """Run generators and write their output.

    The model is validated first, and errors stop the run. Generating from a
    model that does not conform produces artifacts that look finished and are
    not: a grain naming a column that does not exist emits a table with no
    uniqueness at all, and two classes sharing a physical name emit one table
    where the model says two. `--force` generates regardless, for somebody
    mid-refactor who wants to see the output; `--strict` stops on warnings
    too, matching `check`.

    Then two phases. Every generator runs and every result is collected
    before a single file is written, so a generator that raises half way
    through leaves no partial output tree behind. A half-generated estate is
    worse than none: it looks complete, and the parts that are stale are the
    parts nobody thinks to check.
    """
    model = _load(args.model)
    try:
        chosen = _selected(args.only)
    except KeyError as exc:
        print(str(exc).strip("'\""), file=sys.stderr)
        return EXIT_USAGE
    if not chosen:
        print("no generators registered", file=sys.stderr)
        return EXIT_USAGE

    blocking = _blocking(model, args)
    if blocking:
        _report(blocking, forced=args.force)
        if not args.force:
            return EXIT_FAIL

    ctx = Context(model=model, source=model.source, schema=args.schema)
    collected: dict[str, str] = {}
    for gen in chosen:
        try:
            produced = gen.run(ctx)
        except Exception as exc:  # noqa: BLE001
            # Deliberately broad. A third-party generator may raise anything,
            # and the CLI's job at this point is to fail closed with a legible
            # message naming the culprit, rather than to hand the operator a
            # traceback through somebody else's code. Nothing has been written
            # yet, which is the whole reason for collecting before writing.
            print(
                f"{gen.name} failed: {type(exc).__name__}: {exc}\n"
                f"nothing was written",
                file=sys.stderr,
            )
            return EXIT_FAIL
        undeclared = sorted(set(produced) - set(gen.artifacts))
        if undeclared:
            print(
                f"{gen.name} wrote undeclared path(s): {', '.join(undeclared)}",
                file=sys.stderr,
            )
            return EXIT_FAIL
        collected.update(produced)

    out = pathlib.Path(args.out)
    for rel, content in sorted(collected.items()):
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        print(f"wrote {target}")
    print(f"\n{len(collected)} artifacts from {len(chosen)} generators")
    return EXIT_OK


# ---------------------------------------------------------------------------
# The rules, ext and importmap commands
# ---------------------------------------------------------------------------


def cmd_rules(args: argparse.Namespace) -> int:
    """List every registered rule."""
    overrides = registry.severities()
    for code, severity, title, fn in all_rules():
        effective = overrides.get(code, severity)
        mark = "*" if effective != severity else " "
        print(f"{code}  {effective:<7}{mark} {title}")
        if args.verbose and fn.__doc__:
            for line in fn.__doc__.strip().splitlines():
                print(f"          {line.strip()}")
            print()
    print(f"\n{len(all_rules())} rules ({_extensions_line()})")
    return EXIT_OK


def cmd_ext(args: argparse.Namespace) -> int:
    """Describe the active extensions."""
    for active in registry.extensions():
        if args.name and active.name != args.name:
            continue
        print(f"{active.name} {active.version}  [{active.prefix}:]")
        print(f"    origin      {active.origin or 'unknown'}")
        print(f"    rule tag    {active.rule_tag}")
        n_rules = len(active.rules.rules) if active.rules else 0
        print(f"    rules       {n_rules}")
        if active.profile:
            print(f"    profile     {active.profile}")
            for target in registry.TARGETS:
                tags = sorted(
                    t
                    for t in registry.declared_annotations(target)
                    if t.startswith(f"{active.prefix}:")
                )
                if tags:
                    print(f"    {target:<11} {', '.join(tags)}")
        if active.generators:
            names = ", ".join(g.name for g in active.generators)
            print(f"    generators  {names}")
        print()
    return EXIT_OK


def cmd_importmap(_: argparse.Namespace) -> int:
    """Print the map that resolves symbolic profile imports."""
    for prefix, path in sorted(registry.importmap().items()):
        print(f"{prefix}={path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="varda",
        description="Dimensional modeling for LinkML.",
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
    p_check.set_defaults(fn=cmd_check)

    p_gen = sub.add_parser("generate", help="write artifacts from a model")
    p_gen.add_argument("model")
    p_gen.add_argument("--out", default="out", help="output directory")
    p_gen.add_argument("--schema", default="mart", help="SQL schema name")
    p_gen.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help="run only this generator; repeatable",
    )
    p_gen.add_argument(
        "--exempt",
        action="append",
        metavar="CODE",
        help="skip a rule; repeatable",
    )
    p_gen.add_argument(
        "--strict",
        action="store_true",
        help="stop on warnings as well as errors",
    )
    p_gen.add_argument(
        "--force",
        action="store_true",
        help="generate even though the model does not conform",
    )
    p_gen.set_defaults(fn=cmd_generate)

    p_rules = sub.add_parser("rules", help="list conformance rules")
    p_rules.add_argument("-v", "--verbose", action="store_true")
    p_rules.set_defaults(fn=cmd_rules)

    p_ext = sub.add_parser("ext", help="describe active extensions")
    p_ext.add_argument("name", nargs="?")
    p_ext.set_defaults(fn=cmd_ext)

    p_map = sub.add_parser("importmap", help="print the LinkML import map")
    p_map.set_defaults(fn=cmd_importmap)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    try:
        result: int = args.fn(args)
    except ExtensionError as exc:
        print(f"extension error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except FileNotFoundError as exc:
        print(f"no such file: {exc.filename}", file=sys.stderr)
        return EXIT_USAGE
    return result


if __name__ == "__main__":
    raise SystemExit(main())
