# Getting started

## Install

```console
pip install varda
```

Python 3.11 or newer. The only runtime dependency is `linkml-runtime`.

## Annotate a model

Varda reads an ordinary LinkML schema. Classes carrying `varda:` annotations
are tables; anything else in the file is left alone.

```yaml title="mart.yaml"
id: https://example.org/mart
name: mart
prefixes:
  linkml: https://w3id.org/linkml/
  varda: https://w3id.org/varda/
default_prefix: mart
default_range: string
imports:
  - linkml:types

classes:

  DimCustomer:
    annotations:
      varda:role: DIMENSION
      varda:scd: TYPE_2
    attributes:
      customer_key:
        range: integer
        annotations:
          varda:role: SURROGATE_KEY
      customer_id:
        required: true
        annotations:
          varda:role: NATURAL_KEY
      country:
        annotations:
          varda:role: ATTRIBUTE
      region:
        annotations:
          varda:role: ATTRIBUTE
      valid_from:
        range: datetime
        annotations:
          varda:role: VERSION_START
      is_current:
        range: boolean
        annotations:
          varda:role: IS_CURRENT

  FctOrder:
    annotations:
      varda:role: FACT
      varda:fact_type: TRANSACTION
      varda:grain: [order_line]
      varda:grain_statement: one row per line of a customer order
    attributes:
      order_line:
        annotations:
          varda:role: DEGENERATE_DIMENSION
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
          varda:precision: 18
          varda:scale: 2
```

Three things are doing the work here. `varda:role` on the class says what the
table *is*; `varda:role` on each column says what the column *is*; and the
grain pair says what one row of the fact represents — `varda:grain` as the
columns at which rows are unique, `varda:grain_statement` as the sentence.

!!! tip "The grain is the most valuable thing in the file"
    A fact table whose grain nobody can state in one sentence is one whose
    double counting nobody can rule out. If it takes two sentences, the table
    is usually doing two jobs.

    The two halves check each other. The columns make the claim testable —
    they become a `UNIQUE` constraint in the generated DDL, and a query in
    the generated assertions — and the sentence carries the intent a column
    list cannot.

## Check it

```console
$ varda check mart.yaml
2 tables checked against 49 rules (varda 0.3.0): 0 errors, 0 warnings
```

Introduce a mistake — misspell `varda:grain` as `varda:grian`, say — and:

```console
$ varda check mart.yaml
ERROR V001  FctOrder
        unknown table annotation 'varda:grian'; declare it in varda.yaml or fix the typo
ERROR V201  FctOrder
        no varda:grain; name the columns at which rows are unique

2 tables checked against 49 rules (varda 0.3.0): 2 errors, 0 warnings
```

`--strict` also fails on warnings, and on an exemption that names a rule
nobody registers — a suppression that has outlived the rule it suppressed.

## Generate from it

```console
$ varda generate mart.yaml --out out/
wrote out/docs/model.md
wrote out/sql/mart.sql

2 artifacts from 2 generators
```

Generation **fails closed**, twice over. The model is checked first and
errors refuse the run, because artifacts built from a model that does not
conform look finished and are not — a grain naming a column that does not
exist emits a table with no uniqueness at all. Then every generator runs and
every result is collected before a single byte is written, so a generator
that raises leaves no partial output tree behind. A half-generated estate is
worse than none: it looks complete, and the stale parts are the ones nobody
thinks to check.

`--force` generates regardless, for somebody mid-refactor who wants to see
the output. `--strict` refuses on warnings too, and `--exempt` skips a rule,
both meaning exactly what they mean to `check`.

Output is deterministic. Nothing is timestamped and nothing depends on the
environment, so the same model produces the same bytes and generated files
can be committed and diffed like any other source.

`--dialect` says which database the DDL is for — `postgres` by default, or
`duckdb`, `snowflake`, `sqlserver`. Worth setting rather than leaving: SQL
Server has no `BOOLEAN` and reads `TIMESTAMP` as a row-version counter with
no date in it, so `valid_from` on a type-2 dimension generated as PostgreSQL
would hold a number there.

```console
$ varda generate mart.yaml --out out/ --dialect sqlserver
```

## Add a drill path

A dimension can say how it is drilled. Add this to `DimCustomer`, coarsest
level first:

```yaml
    annotations:
      varda:role: DIMENSION
      varda:scd: TYPE_2
      varda:hierarchies:
        - name: geography
          levels: [country, region, customer_id]
```

Regenerate, and `out/docs/model.md` gains a line:

```
**Drill path** (geography): `country` → `region` → `customer_id`
```

Varda checks that the levels are real columns, distinct, at least two of
them, and the kind of column a reader can drill. It cannot check the part
that matters most — that each level rolls up into exactly one member above
it — because that is a claim about data, the same way the grain sentence is.

Optional, and there is more to it than a list of columns: weeks do not nest
inside months, a level's name is not always what tells its members apart, and
a snowflaked dimension reaches through a foreign key to find something
readable. [Concepts](concepts.md#hierarchies-how-a-dimension-is-drilled)
covers those, and `examples/snowflake.yaml` is a model that uses all of them.

## In CI

```yaml title=".github/workflows/model.yml"
- run: pip install varda
- run: varda check mart.yaml --strict
```

Exit codes are part of the contract: `0` success, `1` the model or the run
failed, `2` the invocation was wrong.

## Next

- [Concepts](concepts.md) — what Varda means by grain, role, additivity and SCD
- [Vocabulary](reference/vocabulary.md) — all fifteen annotations
- [Extending](extending.md) — adding your organization's own metadata

Two worked models ship with the source. `examples/retail.yaml` is a flat star
touching every part of the core; `examples/snowflake.yaml` carries the forms a
flat star has no use for — dimensions referencing dimensions, levels that
reach through a foreign key, and a dimension two sources identify
differently.
