# What a datetime column cannot say

**Status: shipped.** `timestamptz` is declared in `profile/types.yaml`,
mapped in both generators, and `examples/retail.yaml` versions on it. The
argument for it — the silent downgrade, the hazard at a version boundary, and
why it is a type rather than an annotation — is in `docs/design.md` under
"Why a timezone-aware datetime is a type Varda declares", and has been
deleted from here.

What stays is the half that did **not** ship, and the measurements behind it.
Investigated against DuckDB 1.5.5, SQLAlchemy 2.0.52 and sqlglot 30.17.0;
every number was taken rather than estimated.

## The decisions, as taken

**The name is `timestamptz`.** The alternative was `instant`, which says what
the value is — a fixed point on the timeline as against a wall-clock reading
— and is the word `java.time` and most temporal modeling use. `timestamptz`
won on being what PostgreSQL, DuckDB and Snowflake all call it: this is the
`V8xx` band's kind of vocabulary, physical and about what the column will be,
and it sits beside `uuid`, which was named the same way. A reader who has met
the type in a warehouse recognizes it without being taught.

**No rule warns about a naive `VERSION_START`.** Varda could, in the `V5xx`
band, on the reasoning that a version boundary is exactly where the ambiguity
costs something. Against, and decisive: a great many warehouses store UTC in
naive columns and are entirely correct to, so the warning fires on models
that have already made the decision properly. A rule that is right about the
hazard and wrong about most models is one people switch off, and `SPEC.md`
says severity is chosen deliberately — anything arguable is a warning or it
does not ship. The type is available; using it stays the modeler's call.

## What the engines spell

Verified by compiling, and by parsing each form under sqlglot.

| | timezone-aware | fractional precision |
| --- | --- | --- |
| PostgreSQL (the base table) | `TIMESTAMP WITH TIME ZONE` | `TIMESTAMP(3) WITH TIME ZONE` |
| DuckDB | accepts the base | `TIMESTAMP(3)` |
| Snowflake | accepts the base | `TIMESTAMP_NTZ(3)` |
| SQL Server | `DATETIMEOFFSET` | `DATETIME2(3)` |

The left column is what shipped: one entry in `gen_sql.TYPES`, one override
in the `sqlserver` dialect, and `sa.DateTime(timezone=True)` in the module.
sqlglot prefers `TIMESTAMPTZ` where the base table spells it in full, which
is the same disagreement `DOUBLE` and `DOUBLE PRECISION` already have and is
normalized through the suite's `TYPE_ALIASES`.

One landmine worth recording. `sa.TIMESTAMP()` compiles to `TIMESTAMP` on SQL
Server, which is the row-version counter, not a time. Varda maps both
datetimes to `sa.DateTime` and is not exposed to it, and it must stay that
way: this is the same trap the named dialects were introduced for, still
armed.

## Why fractional precision is not the other half of this

**Timezone is free on both sides.** `sa.DateTime(timezone=True)` is a generic
SQLAlchemy flag that renders correctly per dialect — `TIMESTAMP WITH TIME
ZONE` on PostgreSQL, `DATETIMEOFFSET` on SQL Server — so the module keeps
naming no database. Both generators land on the same catalog:

```
DDL       : [('t', 'valid_from', 'TIMESTAMP WITH TIME ZONE', 'NO')]
SQLAlchemy: [('t', 'valid_from', 'TIMESTAMP WITH TIME ZONE', 'NO')]
match     : True
```

**Precision is not.** `sa.DateTime` takes no precision argument and
`sa.TIMESTAMP` takes none either; only `postgresql.TIMESTAMP(precision=)` and
`mssql.DATETIME2(precision=)` do. Supporting it means either the module
silently says less than the DDL — and DuckDB records `TIMESTAMP(3)` as the
distinct type `TIMESTAMP_MS`, so the equivalence check would fail rather than
shrug — or `with_variant` comes back, one release after it was removed on the
argument that a portability layer must not name a database.

The motivation is weak on its own terms too. Every default is fine-grained:
`DATETIME2` keeps 100 ns, PostgreSQL and DuckDB keep microseconds, Snowflake
nanoseconds. So a facet would be a facility for deliberately *narrowing* a
column rather than a fix for a bad default.

It is worth stating what narrowing costs, because it is not obvious. Every
type-2 dimension gets `UNIQUE (natural_key, valid_from)` from
`Table._derived_key`. At millisecond precision:

```sql
CREATE TABLE v (valid_from TIMESTAMP_MS);
INSERT INTO v VALUES ('2026-01-01 00:00:00.0001'),
                     ('2026-01-01 00:00:00.0002');
-- one distinct value, two rows
```

Two versions a hundred microseconds apart collide, and the constraint Varda
derived rejects the second version of the row.

The machinery is ready if the motivation ever arrives: `FACETS` and
`FACET_MINIMUM` are keyed by facet name against a set of ranges, and `V803`
already reports a facet on a range that cannot carry it, so a `precision`
entry admitting `datetime` is a two-line change plus whatever is decided
about the module.

## What else was considered and not built

**`TIME WITH TIME ZONE`.** PostgreSQL's own documentation calls its
usefulness questionable, and an offset attached to a time with no date does
not identify anything. A column that needs a zone needs a date beside it.

**Bitemporality.** The profile already states that separating business
validity from load time is out of scope, and nothing here reopens it. A
timezone-aware instant says *when*, not *which kind of when*.

**A second type per precision.** `timestamp_ms`, `timestamp_us` and the rest
would put the facet in the range and multiply the vocabulary by the number of
precisions anybody wants. If precision is ever added it is a facet.

## Reproducing the numbers

DuckDB 1.5.5, SQLAlchemy 2.0.52, sqlglot 30.17.0, Python 3.12, macOS 25.3.0.

The precision collapse:

```python
import duckdb

con = duckdb.connect()
con.execute("CREATE TABLE p (valid_from TIMESTAMP_MS)")
con.execute(
    "INSERT INTO p VALUES "
    "('2026-01-01 00:00:00.0001'), ('2026-01-01 00:00:00.0002')"
)
print(con.execute("SELECT valid_from, count(*) FROM p GROUP BY 1").fetchall())
```

The type tables were produced by compiling each `TypeEngine` against
`postgresql.dialect()` and an `mssql.dialect()` with `server_version_info`
set, and every spelling was parsed under sqlglot as `postgres`, `duckdb`,
`snowflake` and `tsql`. The two rows the aware form adds are now in the suite
rather than here — `test_timestamptz_emits_the_engines_aware_type` and
`test_every_range_renders_what_it_is_expected_to`.
