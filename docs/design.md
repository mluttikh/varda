# Design notes

The decisions that are expensive to reverse, and why they went the way they
did. The full handover specification lives in
[`SPEC.md`](https://github.com/mluttikh/varda/blob/main/SPEC.md).

## Invariants

Each is enforced in code and covered by a test. Changing one is a design
decision, not a refactor.

**One party, one namespace.** An extension owns exactly one prefix. Reserved
prefixes are refused; duplicates are refused.

**Extensions add, they never redefine.** An extension may not redeclare an
enum or class another extension owns. Widening `Additivity` from outside
would turn every generator's exhaustive dispatch into a silent fallback.

**The core goes through the public interface.** `varda` is itself an
`Extension`. If the core needed a privilege the mechanism does not offer,
that is a defect in the mechanism.

**Generation fails closed.** A model that does not conform is refused
before any generator runs, and once they run, every result is collected
before a byte is written.

**Generated output is deterministic.** No timestamps, no hostnames, no
environment, no dict-ordering dependence. Tables are sorted by name so that
moving a class in the source does not reorder the output.

**Unknown values raise; they never default.** An unmapped SQL range raises
`GenerationError` naming the column. A column silently typed `TEXT` is a bug
that surfaces years later as a comparison that does not do what it looks
like.

**Rule codes are permanent.** `V703` goes in commit messages and exemption
lists. Retire a code by deleting it; never reuse it.

**Severity conflicts are refused, not resolved.** See
[Extending](extending.md#severity).

**The typed boundary holds.** `model.py` is the wall along the untyped LinkML
runtime; everything it returns is a concrete type. `mypy --strict` passes.

**The profile namespace never changes.** See below.

## Why the namespace is a w3id.org IRI

The schema `id` is `https://w3id.org/varda` and the `varda:` prefix expands
to `https://w3id.org/varda/`.

Both are copied into the `prefixes:` block of every model anyone writes and
into every RDF graph generated from one. They can therefore never change.

A domain can lapse, be sold, or outlive the organization that registered it,
and a lapsed domain baked into published schemas is unrecoverable.
[w3id.org](https://w3id.org), run by the W3C Permanent Identifier Community
Group, separates the identifier — permanent — from the redirect target, which
is a one-line change. LinkML itself works this way.

## Why annotations rather than a new format

A model annotated with Varda is a legal LinkML schema. Every other LinkML
generator consumes it and ignores what it does not understand.

The alternative — forking the metamodel, or inventing a format — buys
stricter validation at the cost of leaving the ecosystem. Nothing that reads
LinkML would read the fork, and every tool would have to be rewritten. The
validation Varda gives up is recovered by `varda check`, which is a smaller
thing to maintain than a parser.

## Why the vocabulary is this small

Fifteen annotations is not a first cut on the way to forty. It is the
deliberate size.

Every annotation in the core is one a newcomer has to read before they can
write a model, and one that every organization inherits whether it wants it
or not. Anything that differs between organizations — and cost centers,
retention and classification all do — costs less as an extension than as a
core concept everybody must ignore.

The parts deliberately left out of 0.1, with the reasoning for each, are
listed in `SPEC.md` §4. Analytical functions, model diffing, lineage export
and the drift gate all exist in a larger internal prototype and were cut.

## Why a level's identity is derived and not declared

A level answers two questions that look like one. *What is this called* —
`city_name` holds "Springfield" — and *which member is it* — one of the
Springfields in Illinois, Massachusetts and Missouri. Mondrian keeps them
apart as `column` and `nameColumn`, SQL Server Analysis Services as
`KeyColumn` and `NameColumn`, SML and AtScale as `key_columns` and
`name_column`. Varda keeps them apart too, and writes down almost none of it.

The reason is that a hierarchy has already said what distinguishes a member.
Mondrian does not ask for a compound key; it asks whether a level's column is
unique across all parents, and when it is not, keys the member by the path of
levels above it. That path is the ordered list. So a level's identity is its
own key preceded by the key of every coarser level, and Varda derives it:

```yaml
levels: [country_name, state_name, city_name]
```

`city_name` is identified by country, state and city together. Nothing is
declared, and the same list in a snowflake — `country_key.country_name` and
the rest — derives `[country_key, state_key, city_name]` instead, because a
reference level is keyed by the foreign key it reaches through.

What remains is the case those defaults cannot reach: a level whose name is
not what tells its members apart, shown as `product_name` and identified by
`sku`. That is a single column, so `key` is a single column.

The alternative is to have a level declare the columns that identify it.
Such a declaration is the ancestor path written out by hand — a field
restating what the ordered list already says — and a level omitting it would
fall back to its own single column, which is wrong for exactly the
denormalized dimension a star schema exists to produce.

Nothing checks that members are distinct — that is a claim about data, the
same bargain [the grain sentence](#why-the-grain-sentence-is-not-checked-against-the-columns)
makes. [`V607`](reference/rules.md#v607) checks a declared key exists and is
the kind of column that can identify something.

A surrogate key and a foreign key make good keys and bad names: nobody drills
into `4718`. So they are legal in `key` and refused in `column`, which is the
same distinction from the other side.

## Why uniqueness is LinkML's and roles are Varda's

A role says what part a column plays: the business identity a loader matches
on, the meaningless key facts join to, the instant a version began. LinkML's
`unique_keys` says which combinations of columns are unique. Neither follows
from the other — a surrogate key is unique and is not a business key,
`valid_from` belongs in a type-2 dimension's unique key without being an
identity — so both are read, and neither restates the other. Where a native
does say the same thing as an annotation, the annotation goes; this is not
that case.

Where the constraint comes from is the part worth stating. Varda derives one
from the roles: a type-2 dimension is unique on its natural key plus a version
marker. That derivation assumes a dimension has one natural key, which stops
being true the moment it is loaded from several sources that each identify the
thing their own way — a product with a barcode from one and a supplier part
number from another. Concatenating every natural key column into one
constraint produces something weaker than any single key, and inert as well,
since a null on one side leaves the row unchecked.

So a declared `unique_keys` replaces the derived constraint rather than joining
it. A table says it one way or the other, and one fact keeps one place to be
written.

And where several natural keys are present and nothing is declared, nothing is
derived. The concatenated constraint above was emitted in exactly that case
for a long time, on the reasoning that a merged key is better than no key; it
is not. Two natural keys mean either one compound identity — a store known by
its chain code and its store number — or two alternative ones, and the two
want opposite constraints: `UNIQUE (a, b)` for the first, `UNIQUE (a)` and
`UNIQUE (b)` for the second. A role says which columns are business
identifiers and cannot say how many identities they make, so `V306` asks the
model, as an error. Both silent answers are wrong: the merged form enforces
something nobody meant, and deriving nothing leaves an identity the database
does not hold.

One thing Varda does not read, and it is worth knowing which way it falls.
LinkML's `unique_keys` carries `consider_nulls_inequal`, and the defaults are
opposite on the two sides: LinkML counts two nulls as **equal** — the rows are
duplicates — while SQL counts them as distinct and admits both. The emitted
`UNIQUE` is always SQL's reading, so a key over a nullable column is enforced
more loosely than the schema states. That is usually the reading a warehouse
wants, and it is the only portable one: `UNIQUE NULLS NOT DISTINCT` is
PostgreSQL 15 and later, and DuckDB will not parse it. `V307` asks the
question where it has one answer — a dimension whose only identity is
nullable is a dimension whose uniqueness does not hold for the rows a repeated
load produces.

One asymmetry is worth knowing. Columns inherit — `class_induced_slots` pulls
a parent's slots down — and `unique_keys` do not: LinkML drops them from the
induced class. Varda walks `class_ancestors` by hand, because a table that
inherits its columns and loses the constraint over them is a disagreement
nobody can debug.

## What LinkML's OWL output carries

LinkML's OWL generator turns Varda's scalar annotations into ordinary triples
— `varda:role "DIMENSION"`, `varda:scd "TYPE_1"`. `varda:hierarchies` is
structured rather than scalar, and comes out as a single opaque literal
holding a printed Python structure. Nothing can query it.

So a model's roles and SCD types reach an RDF consumer and its drill paths do
not. That is worth knowing before reading the OWL output as a complete
mapping of a model.

## Why units are LinkML's and not Varda's

A unit is not a dimensional modeling concept. Nothing about a star schema
changes when a measure is euros rather than kilograms: the grain, the join
paths and the additivity rules are all the same either way. A unit is a
property of a quantity, and quantities are older and better standardized
than dimensional modeling is.

LinkML already carries one. Its `unit` slot has `slot_uri: qudt:unit` and a
`UnitOfMeasure` range holding `symbol`, `ucum_code`, `abbreviation`,
`descriptive_name` and `exact_mappings` — so a model can name a unit as
loosely or as precisely as it needs, and bind it to QUDT or UCUM, without
Varda inventing a vocabulary for it.

```yaml
net_amount:
  range: decimal
  unit:
    symbol: EUR
```

A `varda:unit` alongside that would be a second place to write one fact,
and two places to write one fact is how models come to disagree with
themselves. `V705` still asks every measure to declare a unit — two measures
in different currencies add up cleanly and wrongly — it just asks the
question of LinkML's slot.

## Why type facets are Varda's and not LinkML's

A unit is LinkML's because LinkML has one. A width is Varda's because LinkML
has none: there is no `maximum_length` on a slot or on a type, and no
`precision` or `scale` anywhere in the metamodel. The only native way to
bound a string is `pattern: '^.{0,80}$'`, which is a validation constraint no
generator reads back out as a width. So the choice is not between reading a
native and restating it. It is between carrying the fact and dropping it.

Dropping it is not neutral, which is the part worth stating. A column with no
declared width emits a bare `VARCHAR`, and a bare `VARCHAR` is unbounded in
PostgreSQL and means `VARCHAR(1)` in SQL Server — every string column in a
generated star truncating to one character on one major engine. A bare
`NUMERIC` is exact and unconstrained in PostgreSQL and `DECIMAL(18, 3)` in
DuckDB, where a unit price of 0.123456 is stored as 0.123 and nothing is
raised. A dialect settles the spelling of a type and not its width, so a
fact the model never stated is still a number the database picks.

```yaml
customer_id:
  range: string
  annotations:
    varda:role: NATURAL_KEY
    varda:max_length: 20

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

They are facets rather than one `physical_type: VARCHAR(80)` because a facet
is a claim about the column and an opaque type string is a claim about one
database. `V803` can check that a width sits on a range that has one, that a
scale has a precision beside it, and that the scale fits — none of which is
reachable inside a string the generator has to pass through untouched. The
same three numbers are re-spelled per dialect — see below — where a stored
`VARCHAR(80)` would already have chosen one engine for every reader.

Nothing gets a default. A width nobody chose is the same silent fallback the
range table refuses when it raises rather than mapping an unknown range to
`TEXT`: it would be wrong in the direction that looks like working output.
`V707` asks the question where it costs most — of a decimal measure, which
is a number somebody acts on — and asks nothing of strings, because a warning
that fires a dozen times on a small star is a warning that gets switched off,
and it would take the measures with it.

## Why there is a dialect, and why its default is named

There is no neutral SQL to emit, and saying there was is how the wrong thing
got emitted for years. The type table was PostgreSQL's the whole time. Run
against SQL Server it produced `BOOLEAN`, which does not exist there, and
`TIMESTAMP`, which does exist and names a row-version counter with no date in
it — so `valid_from` and `valid_to`, the two columns every `V5xx` rule is
about, would have held an incrementing number on a dimension those rules had
just certified. Nothing would have reported it.

So the default dialect is `postgres`, not `neutral`. The output has not
changed; the claim about it has. A dialect that is named can be checked, and
one that is assumed cannot.

```
varda generate mart.yaml --dialect sqlserver
```

| | `postgres` · `duckdb` · `snowflake` | `sqlserver` |
| --- | --- | --- |
| `datetime` | `TIMESTAMP` | `DATETIME2` |
| `boolean` | `BOOLEAN` | `BIT` |
| `double` | `DOUBLE PRECISION` | `FLOAT` |
| `uuid` | `UUID` | `UNIQUEIDENTIFIER` |

A `Dialect` carries what actually differs and nothing else: the types with
another name, and how a schema is asked for. It is not an abstraction over
SQL. That is a transpiler, and a transpiler is a far larger thing to own than
a generator — the moment this grows a second opinion about `SELECT`, the
right answer is to stop and hand the output to one.

Engines are added by being verified, not by being plausible. `bigquery` and
`oracle` are absent for that reason: BigQuery has no `UNIQUE` constraint and
wants `NOT ENFORCED` on the keys it does have, and Oracle has no `CREATE
SCHEMA`, no UUID type, and spells half the base table differently. Neither is
a row in a type table, and half a dialect emits a file that looks right and
does not run.

DuckDB is the only engine the test suite can execute, which would leave three
tables nobody has ever run — the same condition that produced the `TIMESTAMP`
bug. So the tables are checked against sqlglot, which maintains the same
mapping as its entire purpose, for every type in every dialect. Where the two
disagree, one of them has moved and a person has to look.

One dialect refuses rather than emits. Under `sqlserver` a string column with
no `varda:max_length` stops generation, because a `VARCHAR` with no length is
a `VARCHAR(1)` there and silently truncates every value to one character.
Widening it to `VARCHAR(MAX)` instead would have been the accommodating
choice and the wrong one: `MAX` columns cannot be key columns, so a natural
key emitted that way takes the whole file down at its `UNIQUE` constraint,
having read perfectly well.

## Why the grain sentence is not checked against the columns

A fact declares its grain twice — as columns and as a sentence — and nothing
compares them. That looks like an omission and is not.

A rule that compared them would have to read English prose, and it would be
right only for the phrasings it recognized: silent on `each row is one
shipment leg`, and wrong about `one row per order line` over a grain of order
number and line number, which is how the sentence is normally written. A check
that fires for some phrasings and not others is unreliable rather than weak,
and an unreliable check is worse than none — it invites you to stop looking
while giving you nothing to lean on.

The failure such a rule would reach for is caught exactly, further down. A
grain missing a column becomes a `UNIQUE` constraint that fails on load:
language-independent, and impossible to write around.

There is a second cost, and it is the one that decides it. A rule shaping the
sentence teaches people to write for the rule. The sentence exists to carry
intent to a person, and a grain statement written to satisfy a linter has
already lost the thing it was for.

## Why type 2 does not require a validity window

The obvious rule — a type-2 dimension must have a start and an end column —
would reject working warehouses.

Type 2 is defined by keeping a row per change, not by how the current row is
identified, and at least three mechanisms are in normal use: a closed period,
a start plus a current flag with the end derived from the next row, and a bare
counter with no timestamps at all. Data Vault does not store an end column;
it computes one as a view over an insert-only satellite. Kimball recommends
effective date, expiration date and current indicator together, but that is a
recommendation rather than the definition.

So the rules are the ones that hold across all three. A versioning column on a
type 0 or type 1 dimension is an error, because those keep no versions to
bound. An end with no start is an error, because it bounds nothing. Two
columns claiming one role is an error. A type-2 dimension marking none of them
is a warning — something must distinguish the rows, and Varda cannot insist
which.

The same reasoning names the roles. `VERSION_START` rather than `valid_from`,
because dbt's `dbt_valid_from` and Data Vault's `LOAD_DATE` both record when
the warehouse observed a change rather than when it was true. A role named for
validity would invite asserting business time about a load timestamp, and
nothing in the model could tell.

## Why generated docs

The [vocabulary](reference/vocabulary.md), [rules](reference/rules.md) and
[command line](reference/cli.md) pages are built from the package at
docs-build time and never committed.

A hand-written table of forty-eight rules disagrees with the code within two
releases, and the disagreement is invisible because both halves look
authoritative. Reading the same registry `varda check` reads means the docs
and the tool cannot give different answers.

The same script documents *every* active extension, so an organization
building these docs with its own extension installed gets its own vocabulary
documented for free.

## Why the docs generator is a script, not a plugin

`scripts/gen_reference.py` writes plain Markdown into a git-ignored
`docs/reference/` before the site build, rather than hooking into a
generator's plugin API.

The static-site tooling here is in flux. MkDocs last shipped 1.6.1 in August
2024 and has announced a 2.0 that will not support existing plugins, themes
or config files. Material for MkDocs reaches end of life on 5 November 2026,
with [Zensical](https://zensical.org) as its successor, and
[ProperDocs](https://properdocs.org) continuing MkDocs 1.x.

A plugin would bind these docs to whichever of those wins. A script that
writes Markdown works with all of them — verified: this site builds
identically under Material for MkDocs, ProperDocs and Zensical, from the same
unchanged `mkdocs.yml`.
