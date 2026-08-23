# Varda

[![CI](https://github.com/mluttikh/varda/actions/workflows/ci.yml/badge.svg)](https://github.com/mluttikh/varda/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/varda)](https://pypi.org/project/varda/)
[![Python](https://img.shields.io/pypi/pyversions/varda)](https://pypi.org/project/varda/)
[![Docs](https://img.shields.io/badge/docs-mluttikh.github.io%2Fvarda-2F6B57)](https://mluttikh.github.io/varda/)

**Dimensional modeling for [LinkML](https://linkml.io).**

Varda is a *profile* of LinkML: a small vocabulary that lets you say a class
is a fact table, that a column is a semi-additive measure, that a dimension
keeps history. It then checks those claims and generates from them.

A model annotated with Varda is still an ordinary LinkML schema. Every other
LinkML tool — `gen-pydantic`, `gen-owl`, `gen-json-schema` — will read it
happily and ignore what it does not understand.

```yaml
FctSale:
  annotations:
    varda:role: FACT
    varda:fact_type: TRANSACTION
    varda:grain: [order_number, product_key]
    varda:grain_statement: one row per product per line of a receipt
  attributes:
    order_number:
      annotations:
        varda:role: DEGENERATE_DIMENSION
    product_key:
      range: integer
      annotations:
        varda:role: FOREIGN_KEY
        varda:references: DimProduct
    customer_key:
      range: integer
      annotations:
        varda:role: FOREIGN_KEY
        varda:references: DimCustomer
    net_amount:
      range: decimal
      unit:
        symbol: EUR
      annotations:
        varda:role: MEASURE
        varda:additivity: ADDITIVE
```

```console
$ varda check model.yaml
8 tables checked against 35 rules (varda 0.1.0): 0 errors, 0 warnings

$ varda generate model.yaml --out out/
wrote out/docs/model.md
wrote out/sql/mart.sql
```

## Install

```console
pip install varda
```

Python 3.11+. The only runtime dependency is `linkml-runtime`.

## What it gives you

**Twelve annotations.** Seven on tables — `role`, `grain`,
`grain_statement`, `hierarchies`, `fact_type`, `scd`, `physical_name`. Five on
columns — `role`, `references`, `additivity`, `semi_additive_over`,
`physical_name`. That is the whole core
vocabulary, and it is deliberately the whole core vocabulary. Units are
LinkML's own `unit`, which Varda reads rather than restates.

**Thirty-five rules** that catch the mistakes worth catching:

| | |
| --- | --- |
| `V001`–`V003` | the annotations themselves — typos, bad enum values, unknown prefixes |
| `V101`–`V126` | structure — a fact without a grain, a foreign key pointing at a fact, a dimension with no natural key |
| `V201`–`V206` | measures — an unclassified measure, a semi-additive one that never says what it cannot cross |

The `V2xx` family exists because additivity is where the expensive errors
live. A structural mistake usually breaks a query. An additivity mistake
returns a number that looks entirely reasonable and is wrong, to someone who
will act on it.

**Two generators**, `sql` and `docs`, producing runnable DDL and a Markdown
reference. Both are deterministic: no timestamps, no environment, same model
in and same bytes out, so the output can be committed and diffed.

## Extending it

Varda's core is small on purpose. Anything specific to how *your*
organization works — cost centers, retention, data classification, ownership
— goes in an extension under your own prefix.

The smallest useful extension needs no Python at all. Write a LinkML schema
declaring your vocabulary, then a `varda.toml`:

```toml
[[extension]]
name = "acme"
prefix = "acme"
profile = "profiles/acme.yaml"
```

From then on `acme:cost_center` is a first-class annotation: checked for
typos, its enum values enforced, and listed by `varda ext`. Misspell it and
you get

```
ERROR V001  DimStore
        unknown table annotation 'acme:cost_center'; declare it in
        acme.yaml or fix the typo
```

Full documentation: **<https://mluttikh.github.io/varda/>**

An extension with code behind it adds rules and generators through
`varda.ext`, and ships as an installable package advertising the
`varda.extensions` entry point. See [`SPEC.md`](SPEC.md) for the interface
and `tests/fixtures/acme_ext/` for a complete worked example.

**One party, one namespace; extensions add, they never redefine.** An
extension may introduce annotations, enums and rules under its own prefix. It
may not add a value to `TableRole` or change what `SEMI_ADDITIVE` means —
every generator dispatches exhaustively on those, and the registry refuses at
load rather than warning.

## Commands

| | |
| --- | --- |
| `varda check MODEL` | validate; `--strict` fails on warnings too |
| `varda generate MODEL --out DIR` | write artifacts; fails closed |
| `varda rules` | list every rule, `-v` for reasoning |
| `varda ext` | describe active extensions and their vocabulary |
| `varda importmap` | print the LinkML import map |

Exit codes are part of the contract: `0` success, `1` the model or run
failed, `2` the invocation was wrong.

## Status

**0.1.0 — alpha.** The vocabulary and rule codes are stable enough to build
on; rule codes will not be renumbered. Analytical functions, model diffing,
lineage and the drift gate are deliberately not here yet — see `SPEC.md` for
what is deferred and why.

## Building the documentation

```console
pip install -e ".[docs]"
mkdocs serve
```

Then open **<http://127.0.0.1:8000/varda/>** — note the `/varda/` path, which
comes from `site_url` because this is a GitHub Pages project site rather than
a user site. Plain `http://127.0.0.1:8000/` redirects there.

The vocabulary, rules and command-line pages are generated from the package
itself into a git-ignored `docs/reference/`, so they cannot drift from the
code. `mkdocs serve` regenerates them on every rebuild and watches `src/` as
well as `docs/` — edit a rule's docstring and the page updates.

To build the static site the way CI does:

```console
python scripts/gen_reference.py
mkdocs build --strict
```

`--strict` turns a broken internal link into a failed build. The generator is
a standalone script rather than a plugin, so the site also builds under
[Zensical](https://zensical.org) and [ProperDocs](https://properdocs.org) from
the same `mkdocs.yml` — see `docs/design.md` for why that matters.

## License

MIT for the code. The profile vocabulary in `src/varda/profile/varda.yaml` is
CC0, so it can be reused anywhere without attribution — a vocabulary that
constrains its own reuse is not much of a vocabulary.
