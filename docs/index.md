# Varda

**Dimensional modeling for [LinkML](https://linkml.io).**

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
8 tables checked against 36 rules (varda 0.1.0): 0 errors, 0 warnings
```

[Get started](getting-started.md){ .md-button .md-button--primary }
[Browse the rules](reference/rules.md){ .md-button }

## It is still LinkML

A model annotated with Varda is an ordinary LinkML schema. `gen-pydantic`,
`gen-owl`, `gen-json-schema` and every other LinkML generator will read it and
ignore what they do not understand.

Varda adds no syntax, forks no metamodel, and requires no change to LinkML.
That is the whole reason for choosing annotations over a new format.

## The core is small on purpose

**Twelve annotations.** Seven on tables, five on columns. That is the entire
core vocabulary, and it is deliberately the entire core vocabulary — see the
[vocabulary reference](reference/vocabulary.md).

**Thirty-six rules**, in three families:

| | |
| --- | --- |
| `V001`–`V003` | The annotations themselves — typos, bad enum values, unknown prefixes |
| `V101`–`V113` | Structure — a fact without a grain, a foreign key pointing at a fact, a dimension with no natural key |
| `V201`–`V206` | Measures — an unclassified measure, a semi-additive one that never says what it cannot cross |

**Two generators**, `sql` and `docs`. Both deterministic: no timestamps, no
environment, same model in and same bytes out, so output can be committed and
diffed.

Anything specific to how *your* organization works — cost centres, retention,
data classification, ownership — belongs in an
[extension](extending.md) under your own prefix, and the smallest useful one
needs no Python at all.

## Why additivity gets its own rule family

A structural mistake usually breaks a query, and someone notices.

An additivity mistake returns a number that looks entirely reasonable and is
wrong, to someone who will act on it. Summing an account balance across time,
or averaging a ratio, produces output that passes every sanity check a person
applies by eye. That is why `varda:additivity` is required on every measure
and why a semi-additive measure must name the dimension it cannot cross.
