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

An extension with code behind it imports `varda.ext` and nothing else.

```python title="acme_ext/__init__.py"
import pathlib

from varda.ext import Extension
from varda.rules import Finding, RuleSet

HERE = pathlib.Path(__file__).parent
RULES = RuleSet(tag="ACME")
A = Extension.reader("acme")


@RULES.rule("ACME101", "error", "Every table names a cost center")
def acme101(model):
    for table in model.tables:
        if not A.get(table.cls, "cost_center"):
            yield Finding(
                "ACME101",
                "error",
                str(table),
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

## Severity

An extension may propose a different severity for any rule, including one it
does not own:

```python
EXTENSION = Extension(..., severity_defaults={"V205": "error"})
```

Two extensions naming *different* severities for one code is refused, with
both parties named:

```
group wants V205 at 'error' and team wants 'info'. Nothing here can
adjudicate that — set it in varda.toml, which overrides both
```

Resolving that by load order would make behavior depend on discovery order —
a difference between two machines that nothing in either repository explains.

`varda.toml` overrides everything, because the repository can see itself and
an upstream cannot:

```toml title="varda.toml"
exempt = ["V205"]

[severity]
V113 = "error"
```

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

## Seeing what is active

```console
$ varda ext
varda 0.2.0  [varda:]
    origin      built in
    rule tag    V
    rules       29
    table       varda:fact_type, varda:grain, varda:grain_statement, ...
    column      varda:additivity, varda:physical_name, varda:references, ...
    generators  sql, docs

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
