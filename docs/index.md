# Varda

**Dimensional modeling for [LinkML](https://linkml.io).**

!!! warning "Experimental"
    Varda is published so it can be tried, not because the design has
    settled. The vocabulary will change, sometimes in ways that break a
    model written against an earlier version. Pin `varda~=0.3.0` — see
    [Status](#status).

Varda is a *profile* of LinkML: a small vocabulary that lets you say a class
is a fact table, that a column is a semi-additive measure, that a dimension
keeps history. It then checks those claims and generates from them.

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
```

[Get started](getting-started.md){ .md-button .md-button--primary }
[Browse the rules](reference/rules.md){ .md-button }

## It is still LinkML

A model annotated with Varda is an ordinary LinkML schema. `gen-pydantic`,
`gen-owl`, `gen-json-schema` and every other LinkML generator will read it and
ignore what they do not understand — and the test suite runs those four over
the shipped examples to check it, rather than asserting it in prose.

A model naming `uuid` imports Varda's type schema, which resolves through
`varda importmap --json` and a generator's own `--importmap`. Nothing of the
vocabulary comes with it — see [the design notes](design.md) for why the
profile is split and which half a model imports.

Varda adds no syntax, forks no metamodel, and requires no change to LinkML.
That is the whole reason for choosing annotations over a new format.

## The core is small on purpose

**Fifteen annotations.** Seven on tables, eight on columns. That is the entire
core vocabulary, and it is deliberately the entire core vocabulary — see the
[vocabulary reference](reference/vocabulary.md).

**Forty-eight rules**, in nine bands. A code names its concern:

| | |
| --- | --- |
| `V001`–`V004` | The annotations themselves — typos, bad enum values, unknown prefixes, unknown fields |
| `V101`–`V104` | Roles — what a table is, what a column is, and where each role is legal |
| `V201`–`V204` | Grain — a fact with no grain, a grain naming a column that does not exist |
| `V301`–`V307` | Identity — a dimension with no natural key, a business key that repeats, two identities with nothing to tell them apart |
| `V401`–`V405` | References — a foreign key pointing at a fact, one naming no target |
| `V501`–`V506` | Time — a version period on a dimension that keeps no versions |
| `V601`–`V607` | Hierarchies — a level that is not a column, a path of one level |
| `V701`–`V707` | Measures — an unclassified measure, a semi-additive one that never says what it cannot cross, a decimal one that never says what it keeps |
| `V801`–`V804` | Physical naming and types — two classes emitting one table, a width on a column that has none, a range naming nothing |

**Four generators** — `sql`, `docs`, `assertions` and `sqlalchemy`. All
deterministic: no timestamps, no environment, same model in and same bytes
out, so output can be committed and diffed.

**The annotations reach runtime.** `sqlalchemy` emits SQLAlchemy Core tables
— a `MetaData` and one `sa.Table`, no ORM — carrying every annotation on the
`info` mapping. A consumer can read `SEMI_ADDITIVE over date_key` off a
column and refuse the sum while the query is still being built, with no Varda
installed. It names no database — generic types, one module for every engine
— while the DDL goes on naming one at a time, and the two are checked against
each other by building both in a real database and comparing catalogs.

**Three levels of enforcement** — `--constraints enforced` (the default),
`asserted`, `none`. A constraint claims something about the data *and* asks
the database to check it on every write; the checking is most of the cost of
a bulk load, and a lakehouse cannot do it at all. The weaker levels stop the
checking without losing the claim, which moves into `sql/assertions.sql`.

**Four dialects** for the DDL — `postgres` (the default), `duckdb`,
`snowflake`, `sqlserver`. Named rather than assumed, because there is no
neutral SQL: `TIMESTAMP` is a row-version counter in T-SQL, and a type-2
dimension generated without knowing that has a version period holding no
dates.

Anything specific to how *your* organization works — cost centers, retention,
data classification, ownership — belongs in an
[extension](extending.md) under your own prefix, and the smallest useful one
needs no Python at all.

## Why additivity gets its own rule band

A structural mistake usually breaks a query, and someone notices.

An additivity mistake returns a number that looks entirely reasonable and is
wrong, to someone who will act on it. Summing an account balance across time,
or averaging a ratio, produces output that passes every sanity check a person
applies by eye. That is why `varda:additivity` is required on every measure
and why a semi-additive measure must name the dimension it cannot cross.

## Status

**0.3.0 — experimental.** The code is tested and the generators are
deterministic; what is unsettled is the vocabulary itself. Expect it to change,
sometimes in ways that break a model written against an earlier version. No
warehouse of real size has been modeled in Varda yet, and no third party has
written an extension.

The cost of a break is bounded. A Varda model is annotated YAML, so it is a
find-and-replace over your schema rather than a migration of anything you have
loaded, and the model stays an ordinary LinkML schema either way.

One thing is settled: Varda will not grow syntax or fork the metamodel. The
annotation-only design is the premise, not a stage.

Rule codes are not settled yet. Every code was renumbered during 0.1 so that
a code names its concern, and more renumbering before 1.0 is possible if a
band stops describing what is in it. A code is never reused for a different
rule, and at 1.0 the numbers freeze.
