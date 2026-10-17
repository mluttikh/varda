# Extending

Varda's core is small on purpose. Cost centers, retention policy, data
classification, ownership — these differ per organization, and none of them
belong in Varda. The extension mechanism is not bolted on the side; it is the
reason the core can stay small enough to be correct.

## The smallest useful extension has no Python in it

Write a LinkML schema declaring your vocabulary:

```yaml title="profiles/acme.yaml"
id: https://acme.example/varda/acme
name: acme
prefixes:
  linkml: https://w3id.org/linkml/
  acme: https://acme.example/varda/acme/
default_prefix: acme
default_range: string
imports:
  - linkml:types

enums:
  Sensitivity:
    permissible_values:
      public:
        description: May leave Acme.
      internal:
        description: Acme staff only.
      restricted:
        description: Named access list only.

classes:
  AcmeTableAnnotations:
    annotations:
      acme:applies_to: table
    attributes:
      cost_center:
        description: The cost center that pays for this table's storage.
        required: true
      retention_days:
        range: integer

  AcmeColumnAnnotations:
    annotations:
      acme:applies_to: column
    attributes:
      sensitivity:
        range: Sensitivity
```

Then three lines of configuration:

```toml title="varda.toml"
[[extension]]
name = "acme"
prefix = "acme"
profile = "profiles/acme.yaml"
```

From then on `acme:cost_center` is first-class. It is checked for typos, its
enum values are enforced, and `varda ext` lists it:

```console
$ varda check mart.yaml
ERROR V001  DimStore
        unknown table annotation 'acme:cost_center'; declare it in acme.yaml or fix the typo
ERROR V002  DimSegment
        acme:sensitivity is 'secret', which is not a value of Sensitivity;
        expected one of public, internal, restricted
```

That is Varda's own validator policing *your* vocabulary — which is what
makes writing an extension worth doing rather than just agreeing on a
convention in a wiki.

## Adding rules

An extension with code behind it imports `varda.ext` and nothing else. Every
type it runs on is there, including the ones its rules are annotated with:
`Extension`, `RuleSet`, `Finding`, `Generator`, `Context`, the `Model` a
rule walks and everything under it, and `analysis`.

```python title="acme_ext/__init__.py"
import pathlib
from collections.abc import Iterator

from varda.ext import Extension, Finding, Model, RuleSet

HERE = pathlib.Path(__file__).parent
RULES = RuleSet(tag="ACME")


@RULES.rule("ACME101", "error", "Every table names a cost center")
def acme101(model: Model) -> Iterator[Finding]:
    for table in model.tables:
        if not table.annotations.of("acme").get("cost_center"):
            yield Finding(
                table,
                "no acme:cost_center; storage has to be billed to someone",
            )


EXTENSION = Extension(
    name="acme",
    prefix="acme",
    version="1.2.0",
    profile=HERE / "acme.yaml",
    rules=RULES,
    package="acme_ext",
)
```

A rule yields what is wrong and with what: the table, column, hierarchy or
unique key, and a message. The code and severity are the decorator's, and
the engine adds them — along with the file the subject was declared in, when
it arrived through `imports:`.

Ship it as a package advertising the entry point:

```toml title="pyproject.toml"
[project.entry-points."varda.extensions"]
acme = "acme_ext:EXTENSION"
```

Every rule code must begin with the extension's tag, which defaults to the
prefix upper-cased. That is checked at registration, where the error can name
the offending rule — because a rule code is a public identifier that ends up
in commit messages and exemption lists, and two meanings for one code is a
suppression that silently changes what it suppresses.

## Reading the model

A rule and a generator both walk a `Model`: frozen data, read once, holding
nothing from LinkML. `model.tables` are sorted by name, each table's
`columns` are in declaration order with inherited ones included, and the
selections a rule usually wants are already there — `model.facts`,
`table.foreign_keys`, `table.measures`, `table.versioning`.

Every element carries its annotations as written, across every prefix.
An extension reads its own with `annotations.of`, which strips the prefix
and sees nothing outside it:

```python
table.annotations.of("acme").get("cost_center")
column.annotations.of("acme").get("sensitivity")
```

A name the model wrote that should resolve to something — a foreign key's
target, a grain column, a hierarchy level's key — is a `Ref`: the name, and
the element it resolved to, or `None`. A model that fails `varda check` is
still read, so a rule can report what is wrong with it.

What Varda *concludes* from a model is `analysis`, not the model itself:

```python
from varda.ext import analysis

analysis.unique_claims(table)  # what the DDL makes UNIQUE
analysis.reference(column)  # the table and key a foreign key lands on
analysis.creation_order(model)  # every reference target before its referrer
analysis.identities(hierarchy)  # what identifies a member of each level
```

The two carry different promises. The model's fields change only when the
vocabulary does. An answer from `analysis` may become stricter in a minor
release — a derived key that declines to guess in one more case — while
its signature holds. An extension that needs the model's literal statement
reads the model.

## Adding generators

```python
from varda.ext import Generator


def run(ctx):
    return {"acme/inventory.csv": render(ctx.model)}


GENERATOR = Generator(
    name="acme-inventory",
    artifacts=("acme/inventory.csv",),
    run=run,
)
```

`run` returns `{path: content}` and never touches the disk. That is what makes
generation fail closed. Paths are declared up front so that two extensions
claiming the same output file are caught at load rather than by one silently
overwriting the other.

An extension that declares a **type** — not an annotation, an actual LinkML
type a model will name as a range — puts it in a separate schema and points
`Extension.types` at it. That schema, and only that schema, goes into the
import map, so a model can write `imports: - acme` and get the type without
getting the vocabulary. A LinkML import is a union: anything an importable
schema declares turns up in every generator's output for every model that
imports it, which is why `types` refuses a class or an enum at load. An
extension adding annotations only — most of them — declares no types schema
and is not importable at all.

`ctx` is a closed record — the model, and the `Options` the invocation chose:
`ctx.options.schema`, `ctx.options.dialect`, and `ctx.options.constraints`,
one of `enforced`, `asserted` or `none`. All three are plain strings rather
than resolved objects, so a generator emitting something other than SQL can
ignore them and one emitting SQL for an engine the core has never heard of is
not limited to the core's table. Every field is defaulted, so a generator
written against an earlier version keeps working when a new one is added.

## Using Varda as a library

The command line is a thin layer over `varda.api`, so anything it does is
available without it — to a pre-commit hook, a dbt integration or a
notebook — with the same refusals:

```python
from varda import api

model = api.load("mart.yaml")  # extensions found from the model's directory
report = api.check(model)  # report.errors, report.warnings
generation = api.generate(model)  # raises api.NonconformingError on errors
api.write(generation, "out")
drift = api.compare(generation, "out")  # what `varda verify` reports
assert drift.clean  # drift.changed, drift.missing, drift.extra
```

`api.load` discovers the extension set the way `varda check` does: installed
entry points, and the nearest `varda.toml` above the model. To decide it
yourself, pass a `Registry` — `Registry.of(EXTENSION)` is Varda plus exactly
that extension and nothing installed, which is what a test of an extension
wants.

## Severity

An extension may propose a different severity for any rule, including one it
does not own:

```python
EXTENSION = Extension(..., severity_defaults={"V705": "error"})
```

Two extensions naming *different* severities for one code is refused, with
both parties named:

```
group wants V705 at 'error' and team wants 'info'. Nothing here can
adjudicate that — set it in varda.toml, which overrides both
```

Resolving that by load order would make behavior depend on discovery order —
a difference between two machines that nothing in either repository explains.

`varda.toml` overrides everything, because the repository can see itself and
an upstream cannot:

```toml title="varda.toml"
exempt = ["V705"]

[severity]
V502 = "error"
```

The three severities are `error`, `warning` and `info`. Varda's own rules use
only the first two — a rule that is arguable is a `warning`, and one that is
unambiguously illegal is an `error`. `info` is there for extensions, which
have reason to report things Varda does not: a house convention worth naming
in a build log without failing it, or a metric an extension wants to surface
alongside the findings. A rule registered at `info` never stops a run, not
even under `--strict`.

There is deliberately **no severity floor** an upstream can set and a team
cannot lower. It would not work: the repository can exempt the rule outright,
so a floor is a lock with its key taped beside it.

## What an extension may not do

**One party, one namespace; extensions add, they never redefine.**

An extension may introduce annotations, enumerations and rules under its own
prefix. It may **not**:

- claim a reserved prefix (`varda`, `linkml`, `skos`, `dcterms`, `owl`, …)
- claim a prefix another extension already has
- redeclare an enumeration or class another extension owns
- add a value to `TableRole`, `Additivity`, or any other core enum

The last is the important one. Every generator dispatches exhaustively on
those closed vocabularies and raises on a value it cannot map. Widening one
from outside turns that discipline into a silent fallback path.

All of it is refused at load with an error naming both parties, rather than
warned about. The cost of raising is a clear error at startup; the cost of
warning is an estate that half-works for two years because nobody reads
startup warnings.

!!! note "A missing enum value is a request to file, not a reason to fork"
    Varda may widen its **own** enums in a minor release. That is backward
    compatible — no existing model breaks when a new value becomes legal.

## Migrating an extension written for 0.3

The interface changed once, deliberately, before any extension outside this
repository depended on it. What moved:

| 0.3 | Now |
| --- | --- |
| `from varda.rules import Finding, RuleSet` | `from varda.ext import Finding, RuleSet` |
| `from varda.model import DimensionalModel` | `from varda.ext import Model` |
| `Finding("ACME101", "error", str(table), msg)` | `Finding(table, msg)` |
| `Extension.reader("acme").get(table.cls, "cost_center")` | `table.annotations.of("acme").get("cost_center")` |
| `column.references` — a string | `column.references` — a `Ref`; `.name` is the string, `.target` the table |
| `table.unique_claims`, `level.identity` | `analysis.unique_claims(table)`, `analysis.identities(hierarchy)` |
| `hierarchy.resolved`, `hierarchy.levels` | `hierarchy.levels` (the `Level`s), `hierarchy.specs` (as written) |
| `ctx.schema`, `ctx.dialect`, `ctx.constraints` | `ctx.options.schema`, `ctx.options.dialect`, `ctx.options.constraints` |
| `registry.using(EXTENSION)` in a test | `api.load(path, Registry.of(EXTENSION))` |

Roles, additivity, slowly-changing types and fact types are `StrEnum`s now.
A member compares equal to its string, so `column.role == "MEASURE"` keeps
working; formatting one with `!r` shows the enum rather than the string.

## Seeing what is active

```console
$ varda ext
varda 0.4.0  [varda:]
    origin      built in
    rule tag    V
    rules       53
    table       varda:fact_type, varda:grain, varda:grain_statement, ...
    column      varda:additivity, varda:physical_name, varda:references, ...
    generators  sql, assertions, docs, sqlalchemy

acme 1.2.0  [acme:]
    origin      varda.toml (/srv/mart/varda.toml)
    rule tag    ACME
    rules       2
    table       acme:cost_center, acme:retention_days
    column      acme:sensitivity
```

Varda itself appears in that list because it *is* an `Extension`. If the core
ever needed a privilege the mechanism does not offer, that would be a defect
in the mechanism — and this way it is a load error rather than something a
third party discovers the hard way.
