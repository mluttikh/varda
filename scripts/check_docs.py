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

from varda import registry

ROOT = Path(__file__).resolve().parents[1]
FENCE = "`" * 3
TUTORIAL = ROOT / "docs" / "getting-started.md"

#: Counts small enough to argue about are written as words in the prose.
#: Only the range the vocabulary could plausibly reach is listed, so a word
#: that is not a number here is left alone rather than guessed at.
NUMBER_WORDS = {
    word: value
    for value, word in enumerate(
        (
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
            "twenty",
        )
    )
}


def _first_yaml_block(page: Path) -> str:
    pattern = re.escape(FENCE) + r"yaml[^\n]*\n(.*?)" + re.escape(FENCE)
    blocks = re.findall(pattern, page.read_text(), re.DOTALL)
    if not blocks:
        sys.exit(f"{page.name}: no yaml block to check")
    return str(blocks[0])


def _rule_count() -> int:
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
    return int(found.group(1))


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
    count = _rule_count()
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
    quoted_counts = (
        (r"([A-Za-z]+) annotations", table_anns + column_anns),
        (r"([A-Za-z]+) on tables", table_anns),
        (r"([A-Za-z]+) on columns", column_anns),
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
