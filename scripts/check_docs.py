#!/usr/bin/env python3
"""Check the documentation's console transcripts against real output.

`getting-started.md` shows what `varda check` prints. Those blocks were
verified by hand once and then went stale the moment a rule was added: the
page claimed zero warnings while the model it ships emitted one, and
recommended `--strict`, under which its own example exited non-zero.

Hand-verification does not survive contact with a changing rule set, so this
runs the page's own model and compares. It is deliberately narrow — it does
not diff whole transcripts, which would fail on cosmetic edits — and checks
the two things that actually go wrong: the model still validates, and the
counts quoted across the docs match what the tool reports.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from varda import __version__, registry

ROOT = Path(__file__).resolve().parents[1]
FENCE = "`" * 3
TUTORIAL = ROOT / "docs" / "getting-started.md"

#: Counts small enough to argue about are written as words in the prose, and
#: the rule count has outgrown the teens. Built rather than listed, because a
#: table that stops short is a gate that silently stops checking: the word
#: falls out of the mapping and is skipped exactly as "these" is.
_UNITS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)


def _number_words() -> dict[str, int]:
    """Map every English number word from zero to ninety-nine to its value."""
    out = {word: value for value, word in enumerate(_UNITS)}
    for tens in range(2, len(_TENS)):
        out[_TENS[tens]] = tens * 10
        for unit in range(1, 10):
            out[f"{_TENS[tens]}-{_UNITS[unit]}"] = tens * 10 + unit
    return out


NUMBER_WORDS = _number_words()


def _first_yaml_block(page: Path) -> str:
    pattern = re.escape(FENCE) + r"yaml[^\n]*\n(.*?)" + re.escape(FENCE)
    blocks = re.findall(pattern, page.read_text(), re.DOTALL)
    if not blocks:
        sys.exit(f"{page.name}: no yaml block to check")
    return str(blocks[0])


def _rules() -> tuple[int, list[str]]:
    """Read the rule count and every rule code from `varda rules`.

    Through the command rather than by importing the rule set, so what the
    docs quote is checked against what the tool actually prints.
    """
    out = subprocess.run(
        [sys.executable, "-m", "varda", "rules"],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout
    found = re.search(r"(\d+) rules", out)
    if not found:
        sys.exit("could not read the rule count from `varda rules`")
    codes = re.findall(r"^([A-Z]+\d{3})", out, re.MULTILINE)
    return int(found.group(1)), sorted(codes)


def _range_faults(pages: list[Path], codes: list[str]) -> list[str]:
    """Check every rule-code range quoted in the family tables.

    A range is correct only while its ends are the lowest and highest codes
    actually registered in that family. `V101`-`V130` stayed in three files
    after V131 landed, because nothing read it: the count gate sees totals,
    never ends.
    """
    out: list[str] = []
    for page in pages:
        for low, high in re.findall(
            r"`([A-Z]+\d{3})`[\u2013-]`([A-Z]+\d{3})`", page.read_text()
        ):
            family = [c for c in codes if c[:-2] == low[:-2]]
            where = f"{page.relative_to(ROOT)}: quotes {low}-{high}"
            if not family:
                out.append(f"{where}, and no rule is registered there")
            elif (low, high) != (family[0], family[-1]):
                out.append(f"{where}, the family runs {family[0]}-{family[-1]}")
    return out


def main() -> int:
    """Check the docs against the tool, and report what disagrees."""
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        model = Path(tmp) / "tutorial.yaml"
        model.write_text(_first_yaml_block(TUTORIAL))
        # S603: the arguments are this interpreter, literals, and a path
        # written a line above into a directory made a line before that.
        run = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "varda", "check", "--strict", str(model)],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
        )
    if run.returncode != 0:
        failures.append(
            f"the model in {TUTORIAL.name} does not pass --strict, which the "
            f"page tells the reader to run:\n{run.stdout.strip()}"
        )

    # Every page quoting a summary line must quote the current rule count.
    count, codes = _rules()
    pages = [*sorted((ROOT / "docs").rglob("*.md")), ROOT / "README.md"]
    failures += [
        f"{page.relative_to(ROOT)}: quotes {quoted} rules, there are {count}"
        for page in pages
        for quoted in re.findall(r"against (\d+) rules", page.read_text())
        if int(quoted) != count
    ]

    # The size of the vocabulary is quoted in prose as an argument about
    # restraint, so it is written as a word rather than a digit and is easy
    # to leave behind when an annotation comes or goes.
    table_anns = len(registry.declared_annotations("table"))
    column_anns = len(registry.declared_annotations("column"))
    # Any number word before "annotations", wherever it appears — bolded in
    # a heading, mid-sentence, or trailing a link. A word that is not a
    # number falls out of NUMBER_WORDS and is skipped, so "these
    # annotations" and "17 further annotations" match nothing.
    #
    # The rule count is written as a word in the same prose, and moves every
    # time a rule lands. The hyphen is in the character class because
    # "forty-three" is one word here.
    quoted_counts = (
        (r"([A-Za-z-]+) annotations", table_anns + column_anns),
        (r"([A-Za-z-]+) on tables", table_anns),
        (r"([A-Za-z-]+) on columns", column_anns),
        (r"([A-Za-z-]+) rules", count),
        (r"([A-Za-z-]+) core rules", count),
    )
    for page in pages:
        text = page.read_text()
        for pattern, actual in quoted_counts:
            for word in re.findall(pattern, text):
                said = NUMBER_WORDS.get(word.lower())
                if said is not None and said != actual:
                    failures.append(
                        f"{page.relative_to(ROOT)}: quotes {word.lower()} "
                        f"where there are {actual}"
                    )

    failures += _range_faults(pages, codes)

    # Console transcripts quote the version the tool prints in its summary
    # line, and every one of them went stale at 0.1.0 without a word.
    #
    # Only the space-separated form, which is literal output. The pin advice
    # is written `varda~=0.2.0` and is deliberately left alone: it stays
    # correct across every 0.2.x, so tying it to the exact version would
    # demand an edit that changes nothing.
    failures += [
        f"{page.relative_to(ROOT)}: a transcript says varda {quoted}, "
        f"this is {__version__}"
        for page in pages
        for quoted in re.findall(r"varda (\d+\.\d+\.\d+)", page.read_text())
        if quoted != __version__
    ]

    # SPEC.md quotes counts derived from the tree. They are the most
    # drift-prone sentences in the repository — every one of them has gone
    # stale at least once — so each is checked against the thing it counts.
    spec = ROOT / "SPEC.md"
    text = spec.read_text()
    src_lines = sum(
        len(f.read_text().splitlines())
        for f in (ROOT / "src" / "varda").rglob("*.py")
    )
    test_lines = len(
        (ROOT / "tests" / "test_varda.py").read_text().splitlines()
    )
    for label, pattern, actual in (
        ("lines of source", r"\*\*([\d,]+) lines of source\*\*", src_lines),
        ("core rules", r"the (\d+) core rules", count),
        (
            "annotations",
            r"— (\d+) annotations,",
            len(registry.declared_annotations("table"))
            + len(registry.declared_annotations("column")),
        ),
        ("test file lines", r"tests in\s+([\d,]+) lines", test_lines),
    ):
        found = re.search(pattern, text)
        if found and int(found.group(1).replace(",", "")) != actual:
            failures.append(
                f"SPEC.md: quotes {found.group(1)} {label}, "
                f"there are {actual:,}"
            )

    for line in failures:
        print(f"  {line}")
    if failures:
        return 1
    print(f"tutorial validates; {count} rules quoted consistently")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
