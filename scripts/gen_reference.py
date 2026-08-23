"""Generate the reference pages from the package itself.

Run before the site build. Nothing it produces is committed —
``docs/reference/`` is git-ignored — which is the point: a hand-written table
of twenty-two rules disagrees with the code within two releases, and the
disagreement is invisible because both halves look authoritative.

Everything here reads the same registry ``varda check`` reads, so the docs and
the tool cannot give different answers. It walks *every* active extension
rather than only Varda, so an organization building these docs with its own
extension installed gets its own vocabulary documented for free.

Deliberately writes plain files rather than using a site-generator plugin.
The static-site tooling in this corner of the ecosystem is in flux, and a
script that writes Markdown works with any of them; a plugin binds the docs to
one tool's extension API.
"""

from __future__ import annotations

import inspect
import pathlib
import subprocess
import sys
from typing import Any

from varda import registry, rules
from varda.ext import Extension

OUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "reference"

SEVERITY_ICON = {
    "error": ":octicons-x-circle-16:{ .sev-error }",
    "warning": ":octicons-alert-16:{ .sev-warning }",
    "info": ":octicons-info-16:{ .sev-info }",
}

FAMILIES = (
    ("V0", "Profile conformance", "The annotations themselves."),
    (
        "V1",
        "Dimensional structure",
        "Whether the shape is a legal dimensional model.",
    ),
    (
        "V2",
        "Measures",
        (
            "The most expensive class of error a dimensional model produces. "
            "A structural mistake usually breaks a query; an additivity "
            "mistake returns a number that looks entirely reasonable and is "
            "wrong, to someone who will act on it."
        ),
    ),
)


def _clean(text: object) -> str:
    """Collapse a LinkML description into one paragraph of Markdown."""
    return " ".join(str(text).split()) if text else ""


def _structured(ext: Any) -> list[str]:
    """Name the classes an annotation may take as its range.

    A profile class carrying `applies_to` declares annotations; one without
    it is the shape of an annotation's value. Told apart by that rather than
    by a hard-coded list, so an extension defining its own structured range
    is documented on the same terms as the core's.
    """
    view = ext.profile_view
    reader = Extension.reader(ext.prefix)
    return [
        str(name)
        for name, cls in view.schema.classes.items()
        if reader.get(cls, "applies_to") is None
    ]


def _range_link(rng: str, view: Any, structured: list[str]) -> str:
    """Link a range to the section describing it, when there is one."""
    if rng in view.schema.enums or rng in structured:
        return f"[`{rng}`](#{rng.lower()})"
    return f"`{rng}`"


def _structure_section(view: Any, name: str) -> list[str]:
    """Render the shape of one structured annotation value."""
    cls = view.schema.classes[name]
    out = [f"### {name}", ""]
    if cls.description:
        out += [_clean(cls.description), ""]
    out += ["| Key | Range | Meaning |", "| --- | --- | --- |"]
    for key, attr in (cls.attributes or {}).items():
        rng = f"`{attr.range or 'string'}`"
        if attr.multivalued:
            rng = f"{rng}, list"
        required = " **required**" if attr.required else ""
        out.append(
            f"| `{key}` | {rng}{required} | {_clean(attr.description)} |"
        )
    return [*out, ""]


def _annotation_rows(ext: Any, target: str) -> list[str]:
    """Render one table's worth of annotation rows for a target."""
    view = ext.profile_view
    # The same public entry point a third-party extension uses to read
    # its own annotations. Reaching into `registry` would document a
    # private path as though it were supported.
    reader = Extension.reader(ext.prefix)
    structured = _structured(ext)
    rows: list[str] = []
    for cls in view.schema.classes.values():
        if reader.get(cls, "applies_to") != target:
            continue
        for name, attr in (cls.attributes or {}).items():
            rng = _range_link(str(attr.range or "string"), view, structured)
            # A multivalued annotation takes a YAML list, and a reference
            # that renders it identically to a scalar one teaches the wrong
            # syntax. `varda:grain` is the case: `[a, b]`, not `a`.
            if attr.multivalued:
                rng = f"{rng}, list"
            required = " **required**" if attr.required else ""
            rows.append(
                f"| `{ext.prefix}:{name}` | {rng}{required} "
                f"| {_clean(attr.description)} |"
            )
    return sorted(rows)


def _enum_section(view: Any, name: str) -> list[str]:
    """Render one enumeration and its permissible values."""
    enum = view.schema.enums[name]
    out = [f"### `{name}`", ""]
    if enum.description:
        out += [_clean(enum.description), ""]
    out += ["| Value | Meaning |", "| --- | --- |"]
    for value, meta in (enum.permissible_values or {}).items():
        desc = _clean(getattr(meta, "description", ""))
        # A permissible value bound to an ontology term through LinkML's
        # `meaning:` is the one place a Varda model reaches outside itself.
        # Rendering the CURIE puts that on the page instead of leaving it in
        # the schema for whoever thinks to look.
        term = getattr(meta, "meaning", None)
        if term:
            desc = f"{desc} <br>Ontology term: `{term}`".strip()
        out.append(f"| `{value}` | {desc} |")
    out.append("")
    return out


def _extension_vocabulary(ext: Any, *, titled: bool) -> list[str]:
    """Render one extension's annotations and enumerations."""
    view = ext.profile_view
    out: list[str] = []
    if titled:
        out += [f"## {ext.name} — `{ext.prefix}:`", ""]

    for target in registry.TARGETS:
        rows = _annotation_rows(ext, target)
        if rows:
            out += [
                f"## Annotations on a {target}",
                "",
                "| Annotation | Range | Meaning |",
                "| --- | --- | --- |",
                *rows,
                "",
            ]

    structured = _structured(ext)
    if structured:
        out += [
            "## Structured values",
            "",
            (
                "The shapes an annotation's value takes when its range is "
                "not a scalar."
            ),
            "",
        ]
        for name in structured:
            out += _structure_section(view, name)

    if view.schema.enums:
        out += [
            "## Enumerations",
            "",
            (
                "These are closed. An extension may **not** add a value to "
                "one — see "
                "[Extending](../extending.md#what-an-extension-may-not-do)."
            ),
            "",
        ]
        for name in view.schema.enums:
            out += _enum_section(view, name)
    return out


def vocabulary() -> str:
    """Build the annotation and enumeration reference."""
    active = [e for e in registry.extensions() if e.profile_view is not None]
    out = [
        "# Vocabulary",
        "",
        (
            "Every annotation any active extension permits, generated from "
            "the profiles themselves. If an annotation is not listed here, "
            "rule [`V001`](rules.md#v001) rejects it."
        ),
        "",
    ]
    for ext in active:
        out += _extension_vocabulary(ext, titled=len(active) > 1)
    return "\n".join(out)


def rules_page() -> str:
    """Build the conformance-rule reference."""
    registered = rules.all_rules()
    overrides = registry.severities()
    out = [
        "# Rules",
        "",
        (
            f"{len(registered)} rules, generated from the registry. "
            "`varda rules` prints the same list."
        ),
        "",
        (
            "Severity is what the rule ships with. A repository overrides it "
            "in `varda.toml`, and an extension may propose a different "
            "default — see [Extending](../extending.md#severity)."
        ),
        "",
    ]
    for prefix, title, blurb in FAMILIES:
        group = [r for r in registered if r[0].startswith(prefix)]
        if not group:
            continue
        out += [f"## {title}", "", blurb, ""]
        for code, severity, rule_title, fn in group:
            effective = overrides.get(code, severity)
            icon = SEVERITY_ICON.get(effective, "")
            out += [
                f"### `{code}` {{ #{code.lower()} }}",
                "",
                f"{icon} **{effective}** — {rule_title}",
                "",
            ]
            if fn.__doc__:
                # `inspect.cleandoc`, not `textwrap.dedent`. A docstring's
                # first line is flush against the opening quotes while the
                # rest is indented, so the *common* prefix dedent looks for
                # is empty and it strips nothing — leaving four spaces on
                # every continuation line, which Markdown renders as a code
                # block. The reasoning then arrives as monospace with a copy
                # button instead of as prose.
                out += [inspect.cleandoc(fn.__doc__), ""]
    return "\n".join(out)


def cli_page() -> str:
    """Build the command reference from the CLI's own help output.

    Captured by running the tool rather than by introspecting ``argparse``.
    Reaching into a parser's private attributes would document a structure
    that happens to exist today; running ``--help`` documents exactly what a
    user sees.
    """
    out = [
        "# Command line",
        "",
        (
            "Exit codes are part of the contract, because these run in CI: "
            "`0` success, `1` the model or the run failed, `2` the "
            'invocation was wrong. A tool that returns `0` for "I could not '
            'tell" is one that turns a red build green.'
        ),
        "",
    ]
    for cmd in ("", "check", "generate", "rules", "ext", "importmap"):
        tail = [cmd] if cmd else []
        argv = [sys.executable, "-m", "varda", *tail, "--help"]
        result = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False
        )
        heading = f"`varda {cmd}`" if cmd else "`varda`"
        out += [
            f"## {heading}",
            "",
            "```console",
            result.stdout.strip(),
            "```",
            "",
        ]
    return "\n".join(out)


def main() -> None:
    """Write every generated reference page."""
    OUT.mkdir(parents=True, exist_ok=True)
    for filename, builder in (
        ("vocabulary.md", vocabulary),
        ("rules.md", rules_page),
        ("cli.md", cli_page),
    ):
        (OUT / filename).write_text(builder() + "\n", encoding="utf-8")
        sys.stdout.write(f"wrote docs/reference/{filename}\n")


if __name__ == "__main__":
    main()
