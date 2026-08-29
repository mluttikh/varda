# What a datetime column cannot say

**Status: proposal. Nothing here is implemented.** Investigated against
DuckDB 1.5.5, SQLAlchemy 2.0.52 and sqlglot 30.17.0; every measurement was
taken rather than estimated. Sections that survive into the package belong in
`docs/design.md`, in the register used there, and should be deleted from here
when they get there.

Stacked on the SQLAlchemy generator, because the argument against fractional
precision below depends on that module naming no database.

## The finding

Varda does not refuse a timezone-aware declaration. It accepts one and throws
it away.

Declaring `timestamptz: {typeof: datetime}` in a schema and ranging a type-2
dimension's `valid_from` on it:

```
type_chain      : ('timestamptz', 'datetime')
check findings  : none
```
```sql
"valid_from" TIMESTAMP NOT NULL,
```

Zero findings under `--strict`, and a naive column. `sql_type` walks the type
chain nearest-first, does not find `timestamptz` in `TYPES`, falls through to
`datetime`, and emits the base spelling. The model states one thing and the
warehouse gets another, with nothing anywhere reporting it.

That fallback is not a bug. It is the 0.3 `typeof` resolution working as
designed and as its docstring argues for: a schema's own
`money: {typeof: decimal}` should reach `NUMERIC` rather than stopping the
generator. The trouble is that it is right for a **refinement** and wrong for
a **variant** — a subtype that exists precisely to mark a distinction from
its parent — and nothing distinguishes the two. Varda cannot tell them apart,
which is the argument for Varda declaring this type itself rather than
leaving each model to declare it and be silently downgraded.

## Why it matters here rather than in general

Two hazards, both demonstrated.

**A naive column names a different instant for every reader.** The same
stored value, read under three zones:

```
naive column: 2026-06-01 02:30:00   aware column, in UTC: 2026-06-01 00:30:00

read as GMT0       -> 2026-06-01 02:30:00 UTC
read as Etc/GMT-2  -> 2026-06-01 00:30:00 UTC
read as Etc/GMT+5  -> 2026-06-01 07:30:00 UTC
```

The profile states that a version period is closed at the start and open at
the end following SQL:2011, and that "that convention is what stops
consecutive versions overlapping at their boundary." The convention is about
instants. Two loads that disagree about the zone produce overlapping or
gapped periods that the schema cannot rule out and no rule can see.

**Precision decides whether Varda's own derived key is viable.** Every type-2
dimension gets `UNIQUE (natural_key, valid_from)` from `Table._derived_key`.
At millisecond precision:

```sql
CREATE TABLE v (valid_from TIMESTAMP_MS);
INSERT INTO v VALUES ('2026-01-01 00:00:00.0001'),
                     ('2026-01-01 00:00:00.0002');
-- one distinct value, two rows
```

Two versions a hundred microseconds apart collide, and the constraint Varda
derived rejects the second version of the row. That is a Varda constraint
failing on a property Varda has no way to state.

## What the engines spell

Verified by compiling, and by parsing each form under sqlglot.

| | timezone-aware | fractional precision |
| --- | --- | --- |
| PostgreSQL (the base table) | `TIMESTAMP WITH TIME ZONE` | `TIMESTAMP(3) WITH TIME ZONE` |
| DuckDB | accepts the base | `TIMESTAMP(3)` |
| Snowflake | accepts the base | `TIMESTAMP_NTZ(3)` |
| SQL Server | `DATETIMEOFFSET` | `DATETIME2(3)` |

Only SQL Server needs an override for the timezone-aware form, which is one
entry in `Dialect.types` — the same shape `uuid` already has.

One landmine worth recording. `sa.TIMESTAMP()` compiles to `TIMESTAMP` on SQL
Server, which is the row-version counter, not a time. Varda maps `datetime`
to `sa.DateTime` and is not exposed to it, and it must stay that way: this is
the same trap the named dialects were introduced for, still armed.

## The asymmetry that decides the design

**Timezone is free on both sides.** `sa.DateTime(timezone=True)` is a generic
SQLAlchemy flag that renders correctly per dialect — `TIMESTAMP WITH TIME
ZONE` on PostgreSQL, `DATETIMEOFFSET` on SQL Server — so the module keeps
naming no database. Both generators land on the same catalog:

```
DDL       : [('t', 'valid_from', 'TIMESTAMP WITH TIME ZONE', 'NO')]
SQLAlchemy: [('t', 'valid_from', 'TIMESTAMP WITH TIME ZONE', 'NO')]
match     : True
```

**Fractional precision is not.** `sa.DateTime` takes no precision argument
and `sa.TIMESTAMP` takes none either; only `postgresql.TIMESTAMP(precision=)`
and `mssql.DATETIME2(precision=)` do. Supporting it means either the module
silently says less than the DDL — and DuckDB records `TIMESTAMP(3)` as the
distinct type `TIMESTAMP_MS`, so the equivalence check would fail rather than
shrug — or `with_variant` comes back, one release after it was removed on the
argument that a portability layer must not name a database.

## The design

**Declare a timezone-aware datetime as a type in `profile/types.yaml`.**

```yaml
timestamptz:
  typeof: datetime
  uri: xsd:dateTime
```

Then one entry in `gen_sql.TYPES` for the base spelling, one override in the
`sqlserver` dialect, and one entry in `gen_sqlalchemy.TYPES` rendering
`sa.DateTime(timezone=True)`. `V804` already reports a range naming nothing,
so a model writing `range: timestamptz` without `imports: - varda` is caught
by machinery that exists.

Three reasons it is a type rather than an annotation, and the third is the
one that decides it.

It matches the `uuid` precedent exactly: LinkML's built-in set has no such
type, so a model has no legal way to say it, and declaring it in `types.yaml`
is what keeps the model an ordinary LinkML schema.

`Dialect.types` is keyed by range. A type drops into the table that already
exists; a facet could not be expressed there at all, and `sql_type` would
have to start rewriting the string it looks up.

And **a range is visible to every stock LinkML generator; an annotation is
not.** `gen-owl`, `gen-json-schema` and `gen-pydantic` all read ranges and
none of them read `varda:` annotations. Having just spent a release making a
Varda model something those tools read correctly, putting a distinction this
load-bearing where they cannot see it would be working against that.

## What not to build

**Fractional precision, for now.** The defaults are all fine-grained and
sane: `DATETIME2` keeps 100 ns, PostgreSQL and DuckDB keep microseconds,
Snowflake nanoseconds. So this is a facility for deliberately *narrowing* a
column rather than a fix for a bad default, which is much weaker motivation
than the timezone gap — and it is the half that would cost the SQLAlchemy
module its neutrality. The machinery is ready when the motivation arrives:
`FACETS` and `FACET_MINIMUM` are keyed by facet name against a set of ranges,
and `V803` already reports a facet on a range that cannot carry it, so a
`precision` entry admitting `datetime` is a two-line change plus whatever is
decided about the module.

**`TIME WITH TIME ZONE`.** PostgreSQL's own documentation calls its
usefulness questionable, and an offset attached to a time with no date does
not identify anything. A column that needs a zone needs a date beside it.

**Bitemporality.** The profile already states that separating business
validity from load time is out of scope, and nothing here reopens it. A
timezone-aware instant says *when*, not *which kind of when*.

**A second type per precision.** `timestamp_ms`, `timestamp_us` and the rest
would put the facet in the range and multiply the vocabulary by the number of
precisions anybody wants. If precision is ever added it is a facet.

## Decisions this needs

**The name.** `timestamptz` is what PostgreSQL and DuckDB call it and reads
as SQL. `instant` says what it is — a fixed point on the timeline as against
a wall-clock reading — and is the word `java.time` and most temporal modeling
use. This goes into a vocabulary that freezes at 1.0 and is copied into every
model that uses it, so it is worth choosing rather than defaulting.

**Whether a rule nudges.** Varda could warn when a type-2 dimension's
`VERSION_START` is naive, in the V5xx band, on the reasoning that a version
boundary is exactly where the ambiguity costs something. Against: a great
many warehouses store UTC in naive columns and are entirely correct to, so
the warning would fire on models that have already made the decision
properly. A rule that is right about the hazard and wrong about most models
is one people switch off, and `SPEC.md` says severity is chosen deliberately
— anything arguable is a warning or it does not ship.

## Sequencing

1. **The type**, through all three generators, with the SQL Server override
   and the equivalence check extended to cover it.
2. **The example**, if the name lands: one column of `examples/retail.yaml`
   ranged on it, so the shipped model demonstrates the distinction rather
   than only documenting it.
3. **The rule**, if it is wanted, once there is a model to try it against.

## Reproducing the numbers

DuckDB 1.5.5, SQLAlchemy 2.0.52, sqlglot 30.17.0, Python 3.12, macOS 25.3.0.

The silent downgrade, which is the finding everything else rests on:

```python
import pathlib
import tempfile

import yaml

from varda import gen_sql, registry, rules
from varda.model import TYPES as TYPES_FILE
from varda.model import DimensionalModel

out = pathlib.Path(tempfile.mkdtemp())
types = yaml.safe_load(TYPES_FILE.read_text())
types["types"]["timestamptz"] = {
    "typeof": "datetime",
    "uri": "xsd:dateTime",
}
(out / "varda.yaml").write_text(yaml.safe_dump(types, sort_keys=False))
(out / "m.yaml").write_text(
    yaml.safe_dump(
        {
            "id": "https://example.org/t",
            "name": "t",
            "prefixes": {"varda": "https://w3id.org/varda/"},
            "default_prefix": "t",
            "default_range": "string",
            "imports": ["linkml:types", "varda"],
            "classes": {
                "DimThing": {
                    "annotations": {
                        "varda:role": "DIMENSION",
                        "varda:scd": "TYPE_2",
                    },
                    "attributes": {
                        "d_key": {
                            "range": "integer",
                            "annotations": {"varda:role": "SURROGATE_KEY"},
                        },
                        "d_id": {
                            "required": True,
                            "annotations": {"varda:role": "NATURAL_KEY"},
                        },
                        "valid_from": {
                            "range": "timestamptz",
                            "required": True,
                            "annotations": {"varda:role": "VERSION_START"},
                        },
                    },
                }
            },
        }
    )
)

model = DimensionalModel.load(
    out / "m.yaml", importmap={"varda": str(out / "varda")}
)
column = model.table("DimThing").column("valid_from")
print("type_chain     :", column.type_chain)
print("check findings :", sorted({f.rule for f in rules.check(model)}))
print(
    "emitted        :",
    next(
        line.strip()
        for line in gen_sql.generate(model).splitlines()
        if "valid_from" in line
    ),
)
```

The two hazards:

```python
import duckdb

con = duckdb.connect()
con.execute("CREATE TABLE v (naive TIMESTAMP, aware TIMESTAMPTZ)")
con.execute(
    "INSERT INTO v VALUES ('2026-06-01 02:30:00', '2026-06-01 02:30:00+02')"
)
for zone in ("GMT0", "Etc/GMT-2", "Etc/GMT+5"):
    read = con.execute(
        f"SELECT (naive AT TIME ZONE '{zone}') AT TIME ZONE 'UTC' FROM v"
    ).fetchone()
    print(f"read as {zone:10} -> {read[0]} UTC")

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
`snowflake` and `tsql`.
