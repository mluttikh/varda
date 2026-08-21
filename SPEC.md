# Varda — implementation specification

**Version 0.1.0 · status: reference implementation complete, ready to hand
over**

This document is the brief for whoever takes Varda forward. It says what
exists, what was deliberately left out, which decisions must not be quietly
reversed, and what to build next in what order.

---

## 1. What Varda is

A *profile* of LinkML that adds the vocabulary of dimensional modelling, the
rules that check a model against it, and generators that build from it.

The critical property, and the reason the design is shaped this way: **a model
annotated with Varda is still an ordinary LinkML schema.** Every other LinkML
tool reads it and ignores what it does not understand. Varda adds no new
syntax, forks no metamodel, and requires no changes to LinkML. Anything
proposed later that breaks this property is not a Varda feature.

The second critical property: **the core is small, and organisations extend
it themselves.** Cost centres, retention policy, data classification,
ownership — these differ per organisation and none of them belong in Varda.
The extension mechanism is not a nice-to-have bolted on the side; it is the
reason the core can stay small enough to be correct.

## 2. What exists today

| Module | Lines | Responsibility |
| --- | --- | --- |
| `anns.py` | ~100 | Namespaced annotation reads. The one place LinkML's two annotation representations are reconciled. |
| `model.py` | ~260 | The typed view: `Table`, `Column`, `DimensionalModel`. The wall along the untyped LinkML runtime. |
| `ext.py` | ~180 | `Extension`, `Generator`, `Context`. **The only module a third party imports.** |
| `registry.py` | ~590 | Discovery, validation, lookup. Where colliding extensions are refused. |
| `rules.py` | ~600 | `RuleSet`, `Finding`, and the 22 core rules. |
| `gen_sql.py` | ~130 | SQL DDL. |
| `gen_docs.py` | ~90 | Markdown reference. |
| `generators.py` | ~25 | Varda's generators, registered through the public interface. |
| `cli.py` | ~275 | Five commands. |

**2,288 lines of source**: 1,224 of code, 555 of docstrings, 91 of
comment, 418 blank. The prose share is deliberate and is house style —
this is a package other people extend, and the reasoning behind a
constraint is worth more to them than the constraint itself.

Plus `profile/varda.yaml` — 11 annotations, 5 enums — and 87 tests in
1,300 lines.

### The four seams

An extension plugs in at exactly four places. Everything else is internal.

1. **Vocabulary** — a LinkML schema declaring annotations under the
   extension's prefix. Read by `registry.declared_annotations`, enforced by
   rules `V001` and `V002`.
2. **Rules** — a `RuleSet` whose codes all begin with the extension's tag.
   Merged into `rules.all_rules()`.
3. **Generators** — `Generator(name, artefacts, run)`. Paths declared up
   front so collisions are caught at load.
4. **Distribution** — a `varda.extensions` entry point, or a `varda.toml`
   entry, or `[[extension]]` inline in TOML for the no-Python case.

## 3. Invariants

These are load-bearing. Each one is enforced in code and covered by a test.
Changing any of them is a design decision, not a refactor.

**I1 — One party, one namespace.** An extension owns exactly one prefix.
Reserved prefixes (`varda`, `linkml`, `skos`, …) are refused, duplicates are
refused. *`registry._check_prefixes`*

**I2 — Extensions add, they never redefine.** An extension may not redeclare
an enum or class that another extension owns. Widening `Additivity` from
outside would turn every generator's exhaustive dispatch into a silent
fallback path. *`registry._check_vocabulary_collisions`*

Varda may widen its **own** enums in a minor release — that is backward
compatible, since no existing model breaks when a new value becomes legal. A
missing enum value is a request to file, not a reason to fork.

**I3 — The core goes through the public interface.** `varda` is itself an
`Extension`. If the core ever needs a privilege the mechanism does not offer,
that is a defect in the mechanism. *`registry.varda_extension`*

**I4 — Generation fails closed.** Every generator runs and every result is
collected before a single byte is written. A half-generated output tree is
worse than none: it looks complete, and the stale parts are the ones nobody
thinks to check. *`cli.cmd_generate`, `test_generate_fails_closed`*

**I5 — Generated output is deterministic.** No timestamps, no hostnames, no
environment, no dict-ordering dependence. Output that changes when the model
did not makes "is this current?" unanswerable. Tables are sorted by name for
the same reason.

**I6 — Unknown values raise; they never default.** An unmapped SQL range
raises `GenerationError` naming the column. A column silently typed `TEXT` is
a bug that surfaces years later as a comparison that does not do what it
looks like.

**I7 — Rule codes are permanent.** `V203` goes in commit messages and
exemption lists. Renumbering is a breaking change to humans. Retire a code by
deleting it; never reuse it.

**I8 — Severity conflicts are refused, not resolved.** Two extensions naming
different severities for one code is an error naming both parties. Resolving
by load order makes behaviour depend on discovery order, which is a
difference between machines nothing in either repository explains.
`varda.toml` overrides everything, because the repository can see itself and
an upstream cannot.

**I9 — The typed boundary holds.** `model.py` is the wall along the untyped
LinkML runtime; everything it returns is a concrete type. `mypy --strict`
passes, and `warn_return_any` is the rule doing the work. Do not switch it
off to make a stubborn line pass.

## 4. Deliberately not in 0.1

Each of these exists in a larger internal prototype and was cut. The
rationale matters as much as the list — several would be actively harmful to
add early.

| Deferred | Why | Rough size |
| --- | --- | --- |
| **Analytical functions** — `FunctionAnnotations`, parameters, implementation bindings | 17 further annotations for a feature most models never touch. Adding it early doubles the vocabulary a newcomer must read. | ~400 lines |
| **Drift gate** (`varda verify`) | Genuinely valuable and genuinely cheap; cut only because it is worth nothing until generated output is being committed. **First thing to add.** | ~150 lines |
| **Model diffing / evolution** | Needs a stable vocabulary to diff against. Building it before 1.0 means rebuilding it. | ~360 lines |
| **Lineage and catalog export** | Every organisation's catalog is different; this is extension territory until a second real consumer exists. | ~280 lines |
| **Extension conformance kit** | A test harness proving a third-party extension is deterministic and stays inside the public API. Worth nothing until third-party extensions exist. | ~230 lines |
| **`requires_profile` version pinning** | PEP 440 pin of an extension against the profile version. Pulls in `packaging`. Add when a second Varda version exists to be incompatible with. | ~40 lines |
| **M1 data-quality expectations** | Claims about *rows*, not about the model. A different layer with a different audience; do not blur it into the rules. | ~250 lines |
| **More generators** (SQLAlchemy, ERD, dbt, Pydantic) | Each is straightforward against the model layer. Add on demand, one per real consumer. | ~150 each |

## 5. Roadmap

**0.2 — make the output trustworthy.**
Add `varda verify`: regenerate into a temp tree, compare against what is
committed, exit non-zero on drift. Add `Artefact(path, compare)` so a
generator can declare that its output compares as RDF graphs or as a sorted
set of SQL statements rather than as bytes. This is the highest-value
remaining item and it is small.

**0.3 — make extensions safe to depend on.**
`requires_profile` pinning, plus the conformance kit: `varda ext --check
NAME` runs a third-party extension against a fixture model twice, asserts
identical findings and identical artefacts, and AST-scans for imports of
anything outside `varda.ext`.

**0.4 — generators on demand.**
SQLAlchemy models and an ERD are the two most asked for. Both are
mechanical against `model.py`.

**1.0 — freeze the vocabulary.**
The commitment at 1.0 is that annotations and rule codes do not change
meaning within the major version. Do not reach it before at least two real
organisations have written an extension, because the extension mechanism is
the part most likely to need a breaking change and 1.0 is where that stops
being possible.

Explicitly *not* on the roadmap: a web UI, a metadata server, a query
engine, runtime data validation. Varda describes models and generates from
them. Anything that runs in production alongside the data is a different
product.

## 6. Working on it

```console
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
./run_all.sh
```

`run_all.sh` is the gate. It runs, in order: `ruff check`, `ruff format
--check`, `mypy --strict`, `pytest`, then `varda check` and `varda generate`
against `examples/retail.yaml`. All five must pass. There is no
"warnings are fine" mode.

### House rules

- **80 columns**, enforced. Applies to generated output too — it is read by
  people even though it is written by a machine.
- **Comments argue; docstrings explain why.** A comment restating the code
  is noise. A comment saying why the obvious approach was rejected is the
  most valuable line in the file. Follow the existing density.
- **Ruff deviations are justified in `pyproject.toml`,** each with the
  reason it is specific to this codebase rather than to taste. There are
  four. Adding a fifth needs the same treatment; blanket `noqa` does not.
- **Every rule needs a test that provokes it,** named for the failure it
  catches rather than for the rule number.
- **The example must stay clean.** `examples/retail.yaml` passes with zero
  findings, and there is a test asserting it. An example that does not
  conform is worse than none: it is the first thing anybody copies.

### Definition of done for a new rule

1. Registered with a code in the right family and a title that reads as a
   statement of the property, not of the violation.
2. A message that says what to do, not only what is wrong — `V001` names the
   profile file to declare the annotation in.
3. A test provoking it, and if the rule is a judgement call, a docstring
   explaining the reasoning that `varda rules -v` will print.
4. Severity chosen deliberately: `error` only if it is unambiguously illegal.
   Anything arguable is a `warning`, or it gets switched off wholesale.

## 7. Release

Name reserved on PyPI as `varda`. Repository `varda-project/varda`, docs at
`varda-project.readthedocs.io` — the bare `varda` GitHub org and
readthedocs subdomain are held by an unrelated bioinformatics project.

```console
python -m build && twine upload dist/*
```

Before the first real release: reserve the PyPI name with a `0.0.0`
placeholder if it is not already taken. It is the only part of this that
somebody else can take while the work is in progress.
