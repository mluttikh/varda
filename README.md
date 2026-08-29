# Varda

[![CI](https://github.com/mluttikh/varda/actions/workflows/ci.yml/badge.svg)](https://github.com/mluttikh/varda/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/varda)](https://pypi.org/project/varda/)
[![Python](https://img.shields.io/pypi/pyversions/varda)](https://pypi.org/project/varda/)
[![Docs](https://img.shields.io/badge/docs-mluttikh.github.io%2Fvarda-2F6B57)](https://mluttikh.github.io/varda/)
[![Status: experimental](https://img.shields.io/badge/status-experimental-orange)](#status)

**Dimensional modeling for [LinkML](https://linkml.io).**

> **Experimental.** Varda is published so it can be tried, not because the
> design has settled. The vocabulary will change, sometimes in ways that break
> a model written against an earlier version. Pin `varda~=0.3.0`.

Varda is a *profile* of LinkML: a small vocabulary that lets you say a class
is a fact table, that a column is a semi-additive measure, that a dimension
keeps history. It then checks those claims and generates from them.

A model annotated with Varda is still an ordinary LinkML schema. Every other
LinkML tool — `gen-pydantic`, `gen-owl`, `gen-json-schema` — will read it
happily and ignore what it does not understand. The suite runs those four
generators over the shipped examples and asserts that nothing of Varda's own
appears in what they emit, because a claim like this one is worth what it is
tested at.

A model that names `uuid` writes `imports: - varda`, which resolves through a
map Varda prints for the tool that needs it:

```console
$ varda importmap --json > im.json
$ gen-erdiagram --importmap im.json mart.yaml
```

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
8 tables checked against 48 rules (varda 0.3.0): 0 errors, 0 warnings

$ varda generate model.yaml --out out/
wrote out/docs/model.md
wrote out/python/mart.py
wrote out/sql/assertions.sql
wrote out/sql/mart.sql
```

## Install

```console
pip install varda
```

Python 3.11+. The only runtime dependency is `linkml-runtime`.

## What it gives you

**Fifteen annotations.** Seven on tables — `role`, `grain`,
`grain_statement`, `hierarchies`, `fact_type`, `scd`, `physical_name`. Eight
on columns — `role`, `references`, `additivity`, `semi_additive_over`,
`physical_name`, `max_length`, `precision`, `scale`. That is the whole core
vocabulary, and it is deliberately the whole core vocabulary. Units are
LinkML's own `unit`, which Varda reads rather than restates.

**Forty-eight rules** that catch the mistakes worth catching. A code names
its concern, so the band tells you where to look:

| | |
| --- | --- |
| `V001`–`V004` | the annotations themselves — typos, bad enum values, unknown prefixes, unknown fields |
| `V101`–`V104` | roles — what a table is, what a column is, and where each role is legal |
| `V201`–`V204` | grain — a fact with no grain, a grain naming a column that does not exist |
| `V301`–`V307` | identity — a dimension with no natural key, a business key that repeats, two identities with nothing to tell them apart |
| `V401`–`V405` | references — a foreign key pointing at a fact, one naming no target |
| `V501`–`V506` | time — a version period on a dimension that keeps no versions |
| `V601`–`V607` | hierarchies — a level that is not a column, a path of one level |
| `V701`–`V707` | measures — an unclassified measure, a semi-additive one that never says what it cannot cross, a decimal one that never says what it keeps |
| `V801`–`V804` | physical naming and types — two classes emitting one table, a width on a column that has none, a range naming nothing |

The `V7xx` family exists because additivity is where the expensive errors
live. A structural mistake usually breaks a query. An additivity mistake
returns a number that looks entirely reasonable and is wrong, to someone who
will act on it.

**Four generators** — `sql`, `docs`, `assertions` and `sqlalchemy` —
producing runnable DDL, a Markdown reference, the model's claims as queries
over the data, and SQLAlchemy Core table definitions. All deterministic: no
timestamps, no environment, same model in and same bytes out, so the output
can be committed and diffed.

**The annotations reach runtime.** `sqlalchemy` emits a `MetaData` and one
`sa.Table` per table — Core, not the ORM — with every annotation on the
`info` mapping SQLAlchemy stores and never interprets. A consumer holding the
module, with no Varda installed, can refuse a query before it runs:

```
sum(quantity_on_hand) grouped by ['product_key'] → REFUSED
    quantity_on_hand is SEMI_ADDITIVE over date_key
```

The module and `sql/mart.sql` are held together by execution: both are run
against DuckDB at every constraint level and the resulting catalogs compared.

**Three levels of enforcement** — `varda generate mart.yaml --constraints
asserted`. A constraint is two things at once: a claim about the data, and an
instruction to check it on every write. Checking dominates a bulk load, and
some warehouses cannot do it at all, so the two can be separated. What the
database stops policing does not stop being checked — it moves into
`sql/assertions.sql`, which runs once per load and reports what it finds
instead of aborting a transaction.

**Four dialects** — `varda generate mart.yaml --dialect sqlserver`. Named
rather than assumed: there is no neutral SQL, and a `TIMESTAMP` emitted for
`valid_from` is a row-version counter on SQL Server rather than a time.

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
| `varda generate MODEL --out DIR` | validate, then write artifacts; `--force` to build from a model that does not conform |
| `varda rules` | list every rule, `-v` for reasoning |
| `varda ext` | describe active extensions and their vocabulary |
| `varda importmap` | print the LinkML import map |

Exit codes are part of the contract: `0` success, `1` the model or run
failed, `2` the invocation was wrong.

## Status

**0.3.0 — experimental.** Published so it can be tried, not because the design
has settled. Expect the vocabulary to change, sometimes in ways that break a
model written against an earlier version. No warehouse of real size has been
modeled in it yet, and no third party has written an extension. Pin it:

```
varda~=0.3.0
```

The cost of a break is bounded. A Varda model is annotated YAML, so a break is
a find-and-replace over your schema rather than a migration of anything you
have loaded, and the model stays an ordinary LinkML schema either way —
`gen-pydantic`, `gen-owl` and the rest carry on regardless.

One thing is settled: Varda will not grow syntax or fork the metamodel. The
annotation-only design is the premise, not a stage.

Rule codes are not settled yet. They were all renumbered during 0.1 so that a
code names its concern — `V6xx` is hierarchies, `V7xx` is measures — and more
renumbering before 1.0 is possible if a band stops describing what is in it.
A code is never reused for a different rule, and at 1.0 the numbers freeze.

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
