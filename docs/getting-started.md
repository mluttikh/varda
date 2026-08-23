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
        annotations:
          varda:role: NATURAL_KEY
      country:
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
    they become a `UNIQUE` constraint in the generated DDL — and the sentence
    carries the intent a column list cannot.

## Check it

```console
$ varda check mart.yaml
2 tables checked against 29 rules (varda 0.1.0): 0 errors, 0 warnings
```

Introduce a mistake — misspell `varda:grain` as `varda:grian`, say — and:

```console
$ varda check mart.yaml
ERROR V001  FctOrder
        unknown table annotation 'varda:grian'; declare it in varda.yaml or fix the typo
ERROR V103  FctOrder
        no varda:grain; name the columns at which rows are unique

2 tables checked against 29 rules (varda 0.1.0): 2 errors, 0 warnings
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

Generation **fails closed**: every generator runs and every result is
collected before a single byte is written, so a generator that raises leaves
no partial output tree behind. A half-generated estate is worse than none —
it looks complete, and the stale parts are the ones nobody thinks to check.

Output is deterministic. Nothing is timestamped and nothing depends on the
environment, so the same model produces the same bytes and generated files
can be committed and diffed like any other source.

## In CI

```yaml title=".github/workflows/model.yml"
- run: pip install varda
- run: varda check mart.yaml --strict
```

Exit codes are part of the contract: `0` success, `1` the model or the run
failed, `2` the invocation was wrong.

## Next

- [Concepts](concepts.md) — what Varda means by grain, role, additivity and SCD
- [Vocabulary](reference/vocabulary.md) — all twelve annotations
- [Extending](extending.md) — adding your organization's own metadata
