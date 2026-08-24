# Varda — implementation specification

**Version 0.1.0 · status: reference implementation complete, ready to hand
over. The vocabulary it implements is experimental and will change; 1.0 is
where that stops.**

This document is the brief for whoever takes Varda forward. It says what
exists, what was deliberately left out, which decisions must not be quietly
reversed, and what to build next in what order.

---

## 1. What Varda is

A *profile* of LinkML that adds the vocabulary of dimensional modeling, the
rules that check a model against it, and generators that build from it.

The critical property, and the reason the design is shaped this way: **a model
annotated with Varda is still an ordinary LinkML schema.** Every other LinkML
tool reads it and ignores what it does not understand. Varda adds no new
syntax, forks no metamodel, and requires no changes to LinkML. Anything
proposed later that breaks this property is not a Varda feature.

The second critical property: **the core is small, and organizations extend
it themselves.** Cost centers, retention policy, data classification,
ownership — these differ per organization and none of them belong in Varda.
The extension mechanism is not a nice-to-have bolted on the side; it is the
reason the core can stay small enough to be correct.

## 2. What exists today

| Module | Lines | Responsibility |
| --- | --- | --- |
| `anns.py` | ~150 | Namespaced annotation reads. The one place LinkML's two annotation representations are reconciled. |
| `model.py` | ~550 | The typed view: `Table`, `Column`, `DimensionalModel`. The wall along the untyped LinkML runtime. |
| `ext.py` | ~180 | `Extension`, `Generator`, `Context`. **The only module a third party imports.** |
| `registry.py` | ~590 | Discovery, validation, lookup. Where colliding extensions are refused. |
| `rules.py` | ~1130 | `RuleSet`, `Finding`, and the 39 core rules. |
| `gen_sql.py` | ~290 | SQL DDL. |
| `gen_docs.py` | ~120 | Markdown reference. |
| `generators.py` | ~25 | Varda's generators, registered through the public interface. |
| `cli.py` | ~280 | Five commands. |

**3,361 lines of source**: 1,938 of code, 679 of docstrings, 182 of
comment, 562 blank. The prose share is deliberate and is house style —
this is a package other people extend, and the reasoning behind a
constraint is worth more to them than the constraint itself.

Plus `profile/varda.yaml` — 12 annotations, 5 enums — and 164 tests in
2,510 lines.

### The four seams

An extension plugs in at exactly four places. Everything else is internal.

1. **Vocabulary** — a LinkML schema declaring annotations under the
   extension's prefix. Read by `registry.declared_annotations`, enforced by
   rules `V001` and `V002`.
2. **Rules** — a `RuleSet` whose codes all begin with the extension's tag.
   Merged into `rules.all_rules()`.
3. **Generators** — `Generator(name, artifacts, run)`. Paths declared up
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
by load order makes behavior depend on discovery order, which is a
difference between machines nothing in either repository explains.
`varda.toml` overrides everything, because the repository can see itself and
an upstream cannot.

**I10 — The profile namespace never changes.** The schema `id` is
`https://w3id.org/varda` and the `varda:` prefix expands to
`https://w3id.org/varda/`. Both are copied into the `prefixes:` block of
every model anyone writes and into every RDF graph generated from one, so
changing either silently invalidates every model in the wild.

A w3id.org IRI is used rather than a hostname this project owns precisely so
that the *target* can move while the *identifier* does not — a domain can
lapse or be sold, and a lapsed domain baked into published schemas is
unrecoverable. Redirects live in `w3id/.htaccess`; changing where the IRI
points needs no change to the package. *`test_profile_namespace_is_pinned`*

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
| **Lineage and catalog export** | Every organization's catalog is different; this is extension territory until a second real consumer exists. | ~280 lines |
| **Extension conformance kit** | A test harness proving a third-party extension is deterministic and stays inside the public API. Worth nothing until third-party extensions exist. | ~230 lines |
| **`requires_profile` version pinning** | PEP 440 pin of an extension against the profile version. Pulls in `packaging`. Add when a second Varda version exists to be incompatible with. | ~40 lines |
| **M1 data-quality expectations** | Claims about *rows*, not about the model. A different layer with a different audience; do not blur it into the rules. | ~250 lines |
| **More generators** (SQLAlchemy, ERD, dbt, Pydantic) | Each is straightforward against the model layer. Add on demand, one per real consumer. | ~150 each |

## 5. Roadmap

**0.2 — make the output trustworthy.**
Add `varda verify`: regenerate into a temp tree, compare against what is
committed, exit non-zero on drift. Add `Artifact(path, compare)` so a
generator can declare that its output compares as RDF graphs or as a sorted
set of SQL statements rather than as bytes. This is the highest-value
remaining item and it is small.

**0.3 — make extensions safe to depend on.**
`requires_profile` pinning, plus the conformance kit: `varda ext --check
NAME` runs a third-party extension against a fixture model twice, asserts
identical findings and identical artifacts, and AST-scans for imports of
anything outside `varda.ext`.

**0.4 — generators on demand.**
SQLAlchemy models and an ERD are the two most asked for. Both are
mechanical against `model.py`.

**1.0 — freeze the vocabulary.**
The commitment at 1.0 is that annotations and rule codes do not change
meaning within the major version. Do not reach it before at least two real
organizations have written an extension, because the extension mechanism is
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
--check`, `actionlint`, `mypy --strict`, `pytest`, then `varda check
--strict` and `varda generate` against `examples/retail.yaml`, then
regenerates and diffs the two trees to prove output is deterministic. All
must pass. There is no "warnings are fine" mode.

### Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and every pull
request, in five parallel jobs:

| Job | What it does |
| --- | --- |
| `lint` | `ruff check` and `ruff format --check`, as separate steps so a failure says which |
| `types` | `mypy --strict`. One interpreter only — `python_version` is pinned to 3.11, so a matrix would repeat one answer |
| `test` | `pytest` on 3.11, 3.12, 3.13 and 3.14 on Linux, plus 3.12 on macOS and Windows |
| `gate` | `./run_all.sh` — the same script run locally |
| `package` | builds, `twine check --strict`, asserts the profile and `py.typed` are in the wheel, then installs the wheel in a clean venv and runs the CLI from it |

The `gate` job invoking `run_all.sh` rather than restating its steps is
deliberate: CI and the local gate cannot drift apart, and anything added to
one is added to both. When `varda verify` lands in 0.2 it goes in
`run_all.sh` and CI picks it up with no change to the workflow.

The two extra `test` cells are for path handling, not for language
differences. This package walks parent directories looking for `varda.toml`
and joins POSIX-style artifact paths, so a separator bug is plausible on
Windows in a way a 3.13-only language bug is not.

The `package` job's wheel check earns its place: the profile is data, and
setuptools will happily build a wheel without it if `package-data` is
misconfigured. That failure appears only for someone who `pip install`ed —
every rule fires "unknown annotation" because the vocabulary is not
there — and never for anyone working from a source checkout.

### Documentation

```console
pip install -e ".[docs]"
mkdocs serve          # http://127.0.0.1:8000/varda/
```

`docs/reference/` — vocabulary, rules, command line — is generated from the
package by `scripts/gen_reference.py` and git-ignored. `mkdocs serve` watches
`src/` and regenerates on every rebuild, in a subprocess: an in-process call
would read the already-imported module and silently regenerate stale pages.

The generator is a script rather than a site-generator plugin, so the docs
build unchanged under Material for MkDocs, ProperDocs and Zensical. That is
deliberate — MkDocs 1.x is unmaintained, its announced 2.0 breaks existing
plugins, and Material for MkDocs reaches end of life on 5 November 2026.
`mkdocs` is pinned `<2` so a release cannot break the build unattended.

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
3. A test provoking it, and if the rule is a judgment call, a docstring
   explaining the reasoning that `varda rules -v` will print.
4. Severity chosen deliberately: `error` only if it is unambiguously illegal.
   Anything arguable is a `warning`, or it gets switched off wholesale.

## 7. Release

Repository <https://github.com/mluttikh/varda>. PyPI project name `varda`.

There is no documentation site yet. `varda.readthedocs.io` and the bare
`varda` GitHub organisation are both held by an unrelated bioinformatics
project, so the natural home is GitHub Pages at
`mluttikh.github.io/varda` once there is something to publish. Until then
`README.md` and this file are the documentation.

Releases go out through `.github/workflows/release.yml`, triggered by
publishing a GitHub release. It re-runs the whole gate, checks the tag
agrees with the version in `pyproject.toml`, builds, and uploads via **PyPI
Trusted Publishing** — no API token is stored in the repository. A token in
repository secrets is a long-lived credential granting upload rights to
anyone who can run a workflow; OIDC issues one scoped to this workflow and
valid for minutes.

The gate runs again at release time rather than trusting an earlier green
CI run, because publishing is the one irreversible action here: a version
cannot be re-uploaded to PyPI once it exists, even after a yank.

`workflow_dispatch` publishes to TestPyPI, which is worth exercising before
the first real upload.

**One-time setup on PyPI**, at
<https://pypi.org/manage/account/publishing/> — add a pending publisher for
project `varda`, owner `mluttikh`, repository `varda`, workflow
`release.yml`, environment `pypi`. Until that exists the release workflow
fails at the upload step, which is the intended behaviour rather than a
surprise. Attaching required reviewers to the `pypi` environment in
repository settings puts a human between a merged tag and a permanent
upload.

### Before the first real release

1. **Reserve the PyPI name** with a `0.0.0` placeholder. It is the only part
   of this that somebody else can take while the work is in progress.
2. **Register the w3id namespace** — open a pull request against
   <https://github.com/perma-id/w3id.org> adding `varda/.htaccess`, using the
   file prepared in `w3id/`. Until it merges `https://w3id.org/varda`
   returns 404. Nothing in the package depends on it resolving — the profile
   is read from the installed package, never over the network — so this is
   not a release blocker, but it is what lets somebody follow the IRI to find
   out what `varda:additivity` means.
3. **Set up Trusted Publishing** on PyPI, as above.
