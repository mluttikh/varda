# A SQLAlchemy Core generator

**Status: proposal. Nothing here is implemented.** Researched against
SQLAlchemy 2.0.52 with a working prototype; every measurement below was taken
rather than estimated. Sections that survive into the package belong in
`docs/design.md`, in the register used there, and should be deleted from here
when they get there.

## What it emits, and what it does not

One Python module per model, holding SQLAlchemy **Core** table definitions: a
`MetaData` and one `sa.Table` per table. No ORM — no declarative base, no
mapped classes, no relationships, no session. A star schema has no object
graph to map. Rows are loaded in bulk and read in aggregate, and the ORM's
identity map and lazy loading are costs with nothing on the other side.

Core is also the layer that matches what Varda already knows. A `Table` is a
name, columns, types and constraints, which is exactly `gen_sql`'s subject
matter — so the two generators are two renderings of one model rather than
two models.

## Why it is worth having

The DDL is a file you run once. A `MetaData` is an object your code holds,
and it can carry three things `sql/mart.sql` cannot.

**Semantics that survive to runtime.** `sa.Table` and `sa.Column` both take an
`info` dict — an arbitrary payload SQLAlchemy stores and never interprets.
Varda's annotations go there, and a consumer reads them with no Varda
installed:

```
table info : {'role': 'FACT', 'grain': ['date_key', 'product_key',
              'store_key'], 'grain_statement': 'one row per product per
              store per day', 'fact_type': 'PERIODIC_SNAPSHOT'}
  quantity_on_hand SEMI_ADDITIVE  over=date_key
```

Which is enough to refuse an unsound query while it is still being built:

```
sum(quantity_on_hand) grouped by ['date_key']    -> sum(...)
sum(quantity_on_hand) grouped by ['product_key'] -> REFUSED
    quantity_on_hand is SEMI_ADDITIVE over date_key;
    group by it or do not sum across it
```

That is roughly fifteen lines of ordinary Python in the consumer's own
codebase. It matters because `varda:additivity` and `varda:semi_additive_over`
currently reach **zero lines** of any machine-readable output: `rules.py`
declares that an unclassified measure is the most expensive error a
dimensional model produces, checks it, and then writes a `CREATE TABLE` where
a semi-additive balance and an additive quantity are both `NUMERIC` and
nothing anywhere records the difference. This is the first Varda output where
that knowledge reaches something a program acts on.

**Descriptions that reach the database catalog.** `sa.Column(comment=...)`
becomes a real `COMMENT ON COLUMN` when the metadata is created:

```
CREATE TABLE mart.fct_inventory (...)
COMMENT ON TABLE mart.fct_inventory IS 'Stock position, measured at ...'
COMMENT ON COLUMN mart.fct_inventory.date_key IS 'The day this snapshot ...'
```

Varda's own DDL writes descriptions as `--` comments, which the parser
discards. Through this path they land where `information_schema`, dbt's docs
and every BI tool look for them.

**A migration baseline.** Alembic's autogenerate diffs a live database
against a `MetaData`, which is the object this emits. `SPEC.md` §4 defers
model diffing because it "needs a stable vocabulary to diff against" — this
does not settle that, but it does hand the standard tool the standard input.
*Unverified: Alembic was not run. Do not put this in user-facing docs until
it is.*

## The shape

```python
bridge_customer_segment = sa.Table(
    "bridge_customer_segment",
    metadata,
    sa.Column(
        "customer_key",
        sa.Integer(),
        sa.ForeignKey("dim_customer.customer_key"),
        nullable=False,
        info={"role": "FOREIGN_KEY"},
    ),
    sa.Column(
        "allocation_factor",
        sa.Numeric(9, 6),
        nullable=False,
        comment="The share of this customer attributable to this segment.",
        info={
            "role": "MEASURE",
            "additivity": "NON_ADDITIVE",
            "unit": "ratio",
        },
    ),
    sa.UniqueConstraint("customer_key", "segment_key"),
    info={"role": "BRIDGE", "grain": ["customer_key", "segment_key"]},
)
```

`MetaData(schema=...)` carries `--schema`, and foreign-key strings resolve
into it — the prototype's targets report `schema='mart'` without the schema
being written on each reference.

Definition order does not matter. SQLAlchemy resolves a `ForeignKey` string
lazily and `metadata.sorted_tables` orders `create_all` itself, so the module
needs none of `gen_sql`'s dependency ordering. Emitting in the same order
anyway is worth it for a reader comparing the two files.

The emitted Python must be what `ruff format` would produce, at 79 columns.
Generated output is read by people, the house rule applies to it, and a
module that reformats on the consumer's first commit produces a diff nobody
asked for. The prototype was 21 lines over 80 and `ruff format` fixed 13 of
them; the remaining 8 were long `comment=` strings, which the generator has
to wrap itself.

## What SQLAlchemy gets wrong for a warehouse

Its defaults are tuned for OLTP and the ORM. Five of them matter here, and
each was hit or measured rather than anticipated.

**An integer primary key becomes `SERIAL`.** SQLAlchemy assumes an integer
primary key is generated by the database. The prototype's first run died:

```
_duckdb.CatalogException: Type with name SERIAL does not exist!
LINE 3:   customer_key SERIAL NOT NULL,
```

A warehouse surrogate key is assigned by the loader, and a sequence default
would mask a loader bug that leaves it null. Every surrogate key needs
`autoincrement=False`.

**`sa.DateTime()` is `DATETIME` on SQL Server, at every server version** —
never `DATETIME2`. That is the bug the named dialects were introduced to
prevent, arriving through a different door: 3.33 ms resolution and a 1753
floor on the version period of every type-2 dimension.

**`sa.Date()` and `sa.Time()` are gated on the server version**, and the
version is unknown when nothing is connected:

| `server_version_info` | `sa.Date()` | `sa.Time()` | `sa.DateTime()` |
| --- | --- | --- | --- |
| `()` — the offline default | `DATETIME` | `DATETIME` | `DATETIME` |
| `(8, 0)` | `DATETIME` | `DATETIME` | `DATETIME` |
| `(10, 0)` and later | `DATE` | `TIME` | `DATETIME` |

Compiling DDL offline is exactly what a generated module is for, so the
offline default is the one that applies.

**An unsized `sa.String()` is `VARCHAR(max)` on SQL Server**, silently, and a
`UNIQUE` over such a column is a table that will not create — the failure
`docs/design.md` already describes when it explains why `sizes_strings`
refuses rather than widening.

**Identifier quoting is narrower than Varda's.** SQLAlchemy quotes reserved
words and anything not plain lower case and leaves the rest bare:

```sql
CREATE TABLE mart."DimOrder" (
        "select" INTEGER NOT NULL,
        "Mixed_Case" VARCHAR(10),
        plain VARCHAR(10),
```

This one is not a defect and needs no correction. It is a per-dialect list
maintained by people who do it for a living, which is the argument `sqlglot`
is already trusted on. It does mean the two DDL texts never match character
for character, which is why the check below compares catalogs.

## Types: a base table with overlays, which is `Dialect` again

The first four findings all say the same thing — a generic SQLAlchemy type is
not neutral, in precisely the way `gen_sql`'s module docstring says an
imagined neutral SQL is not neutral. The answer is the same shape as the one
already in the package: a base table, overlaid where Varda has a verified
opinion. SQLAlchemy spells the overlay `with_variant`:

```python
sa.DateTime().with_variant(mssql.DATETIME2(), "mssql")
# postgresql -> TIMESTAMP WITHOUT TIME ZONE      mssql -> DATETIME2
```

Measured, one type per row:

| Varda range | postgresql | duckdb | mssql |
| --- | --- | --- | --- |
| `uuid` (`sa.Uuid`) | `UUID` | `UUID` | `UNIQUEIDENTIFIER` |
| `boolean` | `BOOLEAN` | `BOOLEAN` | `BIT` |
| `double` | `DOUBLE PRECISION` | `DOUBLE PRECISION` | `DOUBLE PRECISION` |
| `datetime` + variant | `TIMESTAMP WITHOUT TIME ZONE` | same | `DATETIME2` |
| `date` + variant | `DATE` | `DATE` | `DATE` |

`sa.Uuid()` needs no variant: it is already `UUID` where there is one and
`CHAR(32)` where there is not, which is the same judgment `TYPES` makes.

There must not be a second type table. `gen_sql.TYPES` and `Dialect.types`
are where a range's meaning is decided, and a parallel mapping in another
generator is two answers to one question. The SQLAlchemy names are a
*rendering* of the same decision and belong beside it.

## How it is verified

Not by reading it. The prototype module was generated from
`examples/retail.yaml`, its DDL compiled for PostgreSQL, executed against
DuckDB, and the resulting catalog compared against the catalog that
`gen_sql`'s own output produces:

```
columns match     : True (42 columns)
constraints match : True (48 vs 48)
```

Two independent renderings of one model, proven equivalent through a real
database. This is the same argument `duckdb` and `sqlglot` are in the dev set
for, and it is the reason this generator is safe to own: it cannot drift from
`sql/mart.sql` without the drift being caught.

It needs no `duckdb_engine`. Compiling for PostgreSQL and executing on plain
DuckDB is what the suite already does, and it keeps the dependency at one.

The check should run at every `--constraints` level, since the levels are
exactly where the two renderings could disagree.

## Decisions still open

**Constraint naming.** SQLAlchemy's `MetaData(naming_convention=...)` turns a
bare `UNIQUE (code)` into `CONSTRAINT uq_dim_x_code UNIQUE (code)`. Named
constraints are much better for migrations — Alembic needs a name to drop or
alter one — and half the reason to want this generator is migrations. The
cost is that the emitted DDL then differs from `sql/mart.sql` by more than
whitespace, and the catalog comparison above needs to compare constraint
*shapes* rather than whole rows. Worth doing, and worth deciding
deliberately.

**How `--constraints` maps.** A `Table` object is a declaration; nothing is
enforced until `create_all` runs. So `asserted` is arguably what a `MetaData`
already is. The recommendation is to honor the level anyway, so that the
module and the DDL make the same claims and the equivalence check can run at
all three — with the dropped claims recorded in `info`, as the DDL records
them in comments.

**The unsized-string refusal.** `gen_sql` refuses an unsized string under
`--dialect sqlserver` only. This module is dialect-*neutral* — one file, every
engine — so there is no dialect to condition on. Three options, none obviously
right: refuse always, which is stricter than Varda is today; emit
`sa.String()` and accept `VARCHAR(max)`, which reintroduces the bug; or invent
a width in the variant, which is the default number nobody chose that `_facets`
exists to refuse. Neither shipped example is affected — `customer_id` is
`uuid`, not a bare string — so this can be settled when a model provokes it.

## What not to build

**The ORM layer.** No declarative base, no mapped classes. A star schema is
loaded in bulk and read in aggregate; the identity map, unit of work and lazy
loading are costs with no corresponding benefit, and mapped classes would
double the surface this generator has to keep correct. If somebody wants an
ORM, `MetaData` is what they map *against*.

**A runtime dependency on SQLAlchemy.** The generator emits text and imports
nothing. SQLAlchemy is a dev dependency so the suite can execute what was
emitted — 2.1 MB, against the 174 MB `linkml` already costs. `pyproject.toml`
already has the paragraph explaining why dev differs from runtime.

**A second dialect table.** See above. One decision, two renderings.

## Sequencing

1. **The generator and the equivalence test.** `gen_sqlalchemy.py`, the
   SQLAlchemy names beside `gen_sql.TYPES`, `python/mart.py` as the artifact,
   and the DuckDB catalog comparison at all three constraint levels.
2. **`info` as a documented contract.** What keys a consumer may rely on, in
   `docs/`, with the refuse-an-unsound-sum example. The keys are the
   annotation names, so there is nothing new to invent — but a consumer
   reading them needs to know they are stable.
3. **Alembic, verified.** Run autogenerate against a live database and a
   generated `MetaData`, and only then say in the docs that it works.

## Reproducing the numbers

SQLAlchemy 2.0.52, DuckDB 1.5.5, Python 3.12, on macOS 25.3.0.

The equivalence check, with `mart_tables.py` generated from
`examples/retail.yaml` and `varda.sql` from `gen_sql.generate(model, "mart",
"duckdb")`:

```python
import duckdb
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import mart_tables


def catalog(con):
    columns = con.execute("""
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns WHERE table_schema = 'mart'
        ORDER BY table_name, column_name""").fetchall()
    constraints = con.execute("""
        SELECT table_name, constraint_type,
               constraint_column_names::varchar
        FROM duckdb_constraints() WHERE schema_name = 'mart'
        ORDER BY table_name, constraint_type, 3""").fetchall()
    return columns, constraints


theirs = duckdb.connect()
theirs.execute(open("varda.sql").read())

ours = duckdb.connect()
ours.execute("CREATE SCHEMA mart;")
for table in mart_tables.metadata.sorted_tables:
    ours.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))

assert catalog(theirs) == catalog(ours)
```

The type tables were produced by compiling each `TypeEngine` against
`postgresql.dialect()`, `mssql.dialect()` and a `duckdb_engine` dialect
directly, and the server-version gating by setting `server_version_info` on a
fresh `mssql.dialect()` before compiling.
