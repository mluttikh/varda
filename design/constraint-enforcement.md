# Constraint enforcement in generated DDL

**Status: shipped, unreleased.** `--constraints` and `sql/assertions.sql`
exist; the reasoning that survives into the package lives in
`docs/design.md`, under *Why enforcement is a level, not a switch* and *Why
the claims outlive the constraints*.

What is kept here is what does not belong in user documentation: the
measurements the design rests on and how to reproduce them, the engine
behavior that was verified rather than assumed, the alternatives that were
rejected, and what is left to do.

## The problem it solved

Varda's DDL enforced the model and offered no way not to. Every uniqueness
claim became a `UNIQUE` constraint, every reference a `FOREIGN KEY`, every
surrogate key a `PRIMARY KEY`, and the database policed all of them on every
write.

That is the right default and it is not always what an operator wants. A
warehouse loaded by an ETL layer that already guarantees these properties —
by construction, or because the source system guarantees them — pays for the
check twice, and the second payment is taken row by row, forever, on the
critical path of every load. The engines that cannot enforce keys at all
(lakehouses, BigQuery) make the same point from the other direction: they
have the same claims and no way to express them.

## What each piece of enforcement costs

Five kinds reach the DDL: `PRIMARY KEY` on surrogate keys, `NOT NULL` on
required columns, `FOREIGN KEY` on references, `UNIQUE` on the grain and on
natural and declared keys, and the type facets — `VARCHAR(n)`,
`NUMERIC(p, s)`. These are the numbers `docs/design.md` quotes and the
reason two of the five are never dropped.

They do not cost the same. Measured on DuckDB against a 200,000-row dimension
and a 2,000,000-row fact, best of three runs (see *Reproducing the numbers*):

| Emitted | Dimension | Fact | Total | Relative |
| --- | --- | --- | --- | --- |
| nothing enforced | 0.02s | 0.06s | 0.08s | 1x |
| `PRIMARY KEY` only | 0.06s | 0.07s | 0.12s | 1.4x |
| `UNIQUE` only | 0.11s | 0.81s | 0.92s | 11x |
| `FOREIGN KEY` only (needs the PK) | 0.05s | 1.06s | 1.11s | 13x |
| everything Varda emits | 0.15s | 1.82s | 1.97s | 23x |

Each figure is the minimum of three runs and is rounded on its own, so the
columns do not always sum exactly. The base row is small enough that its
run-to-run variance moves the multipliers by a few percent; the shape is
stable, the third digit is not.

The total is large — a load takes more than twenty times as long — and the
distribution matters more than the total. Enforcement adds about 1.89s here,
and the dimension's `PRIMARY KEY` accounts for 0.04s of it: some two percent.
The rest is the fact table's grain `UNIQUE` and its foreign keys. `NOT NULL`
and the type facets do not register at all.

This is the number that shapes the design. A single on/off switch makes an
operator discard the constraint responsible for two percent of the cost along
with the ones responsible for the other ninety-eight — and the cheap one is
the surrogate key, which is what makes a dimension a dimension.

## Three facts about the engines

Each was verified rather than assumed, which is the standard `Dialect`
already holds itself to.

**A constraint has two meanings, and Varda currently fuses them.** It is an
assertion about the data, and it is an instruction to police every write.
Snowflake separates them: it accepts `PRIMARY KEY`, `UNIQUE` and
`FOREIGN KEY`, enforces none of them, and uses them for join elimination when
they carry `RELY`. Databricks and BigQuery do the same through
`NOT ENFORCED`. That is exactly the semantics an operator means by "the ETL
guarantees it" — and it keeps the optimizer benefit that deleting the
constraint throws away. Varda has no way to say it.

The corollary is worth stating plainly: **under `--dialect snowflake` the
DDL Varda emits today is already unenforced.** Snowflake enforces `NOT NULL`
and nothing else. The load cost above is paid only on `postgres`, `duckdb`
and `sqlserver`.

**DuckDB can express neither escape hatch.** Against DuckDB 1.5.5:

```
REFUSED  ALTER TABLE a ADD CONSTRAINT u UNIQUE (x)     NotImplementedException
REFUSED  ALTER TABLE c ADD FOREIGN KEY (k) REFERENCES  NotImplementedException
REFUSED  CREATE TABLE n (x INTEGER, UNIQUE (x) NOT ENFORCED)   ParserException
OK       CREATE UNIQUE INDEX ix ON i (x)
```

Both obvious designs fail here. A companion `constraints.sql` of
`ALTER TABLE` statements, run after the load, does not work on DuckDB. Nor
does emitting the constraint marked unenforced. On DuckDB, omission is the
only available form, and DuckDB is the engine the test suite executes.

**The levels nest; per-kind switches do not.** Also against DuckDB:

```sql
CREATE TABLE d (k INTEGER);                             -- no PK
CREATE TABLE f (k INTEGER, FOREIGN KEY (k) REFERENCES d (k));
-- Binder Error: no primary key or unique constraint for referenced table "d"
```

Standard SQL requires a foreign key's target to carry a primary key or a
unique constraint. Drop a dimension's `PRIMARY KEY` and every fact's foreign
key becomes DDL that does not parse. Four independent booleans would let an
operator select a combination that cannot be emitted; an ordered level cannot.

One fact is *not* verified and must be before it is relied on. PostgreSQL 18
added `NOT ENFORCED` constraints. Whether that covers foreign keys as well as
`CHECK` needs testing against a real 18 server. Until it is, `postgres`
carries no `unenforced` value and `asserted` drops its references — the
direction that emits less rather than emitting something that may not
parse.

## What not to build

**A model-level annotation** — `varda:enforce: false`. Enforcement is a
property of the deployment, not of the model. The same model goes to a
development DuckDB, where the check is wanted precisely because it catches
loader bugs early, and to a production lakehouse that cannot have it at all.
An annotation forces one answer for every target and makes the model file
environment-specific, which is the property `SPEC.md` §1 exists to protect.

**A `varda.toml` key** — not yet. It is the right home eventually for a
team-wide default, because it makes the choice reviewable and versioned
rather than living in one person's shell history. But `CONFIG_KEYS` is
deliberately four names about extensions and rules, and adding a generation
section widens what the file is for. Ship the flag; add the key when someone
is typing it every day.

**A separate `constraints.sql` of `ALTER TABLE` statements.** The appealing
version of this feature — load bare, then add the constraints, then have them
enforced from that point on. DuckDB does not support it, and DuckDB is the
engine the tests execute. The assertions file does the same job everywhere.

**Refusing `--constraints asserted` on a dialect that cannot mark a
constraint.** This was in the first draft of this note, on a "no silent
degradation" argument, beside a table that said the same invocation should
emit comments. Implementation forced the contradiction into the open and the
table was right. `asserted` names an intention — these claims hold, do not
check them on every write — and that intention is expressible on every
engine, as `RELY` on one and as a comment plus an assertion query on
another. Refusing would have made the flag unusable on DuckDB, which is the
case that prompted the feature. The degradation is not silent: the header
names the level unconditionally, and the dropped claims are written into the
file.

## What is left

**Verify PostgreSQL 18's `NOT ENFORCED`.** Noted under the engine facts
above and still untested. If it covers foreign keys, `postgres` gains a
`unenforced` value and `asserted` stops dropping references there.

**`bigquery` and `oracle`.** `Dialect.unenforced` was BigQuery's second
blocker and is now a field — `NOT ENFORCED` is the value it wants. What
remains for both is the type table, and for Oracle the absence of
`CREATE SCHEMA`. Note that `sql/assertions.sql` already runs against both
today: it is plain SQL and was parse-checked under each.

**A `varda.toml` key**, if `--constraints` turns out to be typed on every
invocation. See *What not to build* — the argument there is about
sequencing, not about the idea.

## Reproducing the numbers

Apple M1 Max, macOS 25.3.0, Python 3.12.0, DuckDB 1.5.5. Best of three runs
per row, timing the `INSERT` alone.

```python
import time, duckdb

DIM_ROWS, FACT_ROWS, REPS = 200_000, 2_000_000, 3


def ddl(*, pk, uniq, fk):
    dim = [
        "product_key INTEGER" + (" PRIMARY KEY" if pk else ""),
        "sku VARCHAR(20) NOT NULL",
        "brand VARCHAR(60)",
    ]
    if uniq:
        dim.append("UNIQUE (sku)")
    fact = [
        "product_key INTEGER NOT NULL",
        "order_line INTEGER NOT NULL",
        "quantity INTEGER NOT NULL",
        "amount NUMERIC(12,2) NOT NULL",
    ]
    if fk:
        fact.append(
            "FOREIGN KEY (product_key) REFERENCES dim_product (product_key)"
        )
    if uniq:
        fact.append("UNIQUE (product_key, order_line)")
    return (
        f"CREATE TABLE dim_product ({','.join(dim)});",
        f"CREATE TABLE fct_sale ({','.join(fact)});",
    )


DIM = (
    f"INSERT INTO dim_product SELECT i, 'SKU' || i, 'B' || (i % 50) "
    f"FROM range({DIM_ROWS}) t(i);"
)
FACT = (
    f"INSERT INTO fct_sale SELECT (i % {DIM_ROWS}), i, (i % 7) + 1, "
    f"(i % 1000) * 1.25 FROM range({FACT_ROWS}) t(i);"
)


def run(stmts):
    con = duckdb.connect()
    for statement in stmts:
        con.execute(statement)
    start = time.perf_counter()
    con.execute(DIM)
    dim = time.perf_counter() - start
    start = time.perf_counter()
    con.execute(FACT)
    fact = time.perf_counter() - start
    con.close()
    return dim, fact


base = 0.0
for label, kw in (
    ("nothing enforced", dict(pk=0, uniq=0, fk=0)),
    ("PRIMARY KEY only", dict(pk=1, uniq=0, fk=0)),
    ("UNIQUE only", dict(pk=0, uniq=1, fk=0)),
    ("FK only", dict(pk=1, uniq=0, fk=1)),
    ("everything", dict(pk=1, uniq=1, fk=1)),
):
    runs = [run(ddl(**kw)) for _ in range(REPS)]
    dim, fact = min(r[0] for r in runs), min(r[1] for r in runs)
    base = base if label != "nothing enforced" else dim + fact
    print(
        f"{label:20} {dim:6.2f}s {fact:6.2f}s {dim + fact:6.2f}s "
        f"{(dim + fact) / base:5.1f}x"
    )
```
