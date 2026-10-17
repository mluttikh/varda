# An Open Data Contract Standard generator

**Status: proposal. Nothing here is implemented.** Investigated against ODCS
v3.1.0 (approved 2025-12-08), its 86,492-byte JSON Schema, ODPS v1.0.0, and
varda 0.3.0. Every count below was taken from the specification sources
rather than estimated; the survey the proposal came out of, with the wider
landscape and all sources, is at
`claude.ai/code/artifact/28aa1d75-6e27-41b3-8c2b-4d9d0aefb05f`.

## The finding this rests on

ODCS has no vocabulary for anything Varda is about. Grepping the v3.1.0
specification — `schema.md`, `data-quality.md`, `fundamentals.md` and the
full worked example — for the words this package is built on:

```
grain           0     surrogate       0
additivity      0     conformed       0
slowly changing 0     hierarchy       0
scd             0     star schema     0
```

Its one concession to grain is a single field on a schema object:

```yaml
dataGranularityDescription: Aggregation on columns txn_ref_dt, pmt_txn_id
```

That is `varda:grain_statement` exactly — the half `docs/design.md` argues
under *Why the grain sentence is not checked against the columns* cannot be
checked at all. ODCS has the half that cannot fail and not the half that
can.

The word `dimension` does appear, five times, and never means a dimension
table: in ODCS it is a data-quality dimension — `completeness`,
`timeliness`. Anything generated into an ODCS document has to keep the two
apart, and any prose bridging them has to say so early. `hierarchies`
likewise appears in the v3.0.0 changelog and means nested objects and
arrays, not a drill path.

## Why a contract and not another exporter

The Data Contract CLI, which speaks ODCS natively, exports twenty-eight
formats from a contract. Among them: SQL DDL across eight dialects,
SQLAlchemy models, JSON Schema, Pydantic, RDF, Markdown, Mermaid, dbt
models, Great Expectations suites and SodaCL checks.

Read plainly, that is the whole of Varda's generator surface and more,
maintained by a funded team. It is the argument against building any further
exporter here, and the argument for building this one. `varda check` is what
this package has that nothing else does; `varda generate` is a demonstration
that the model is complete enough to build from. A contract is the artifact
that makes the second one stop competing: emit ODCS once, and every one of
those twenty-eight formats becomes downstream of a Varda model without a
line of code here.

The other half of the argument is what a model is asked to adopt. Today a
Varda model is a file format nobody else reads, whose value has to be taken
on faith until somebody runs the tool. A Varda model that emits a valid ODCS
contract asks an organization to adopt a *modeling discipline* whose output
drops into the standard they are already converging on — and the field has
converged: the Data Contract Specification, the only real alternative,
carries a deprecation notice naming ODCS "the conceptual successor," and
ODCS is a Linux Foundation AI & Data incubation project under Bitol.

## The other track, which this note does not choose against

"Converged" is true within one half of the field, and the half has to be
named or this note argues for less than it appears to.

The standards that describe data sort into two groups that solve the same
problem with opposite trade-offs and do not read each other's files. ODCS,
ODPS, Apache Ossie, dbt and Varda are YAML with a JSON Schema behind them:
all the warehouse adoption, no formal semantics. The RDF Data Cube
vocabulary, QB4OLAP, DDI-CDI, DPROD and DCAT are RDF with OWL and SHACL
behind them: all the formal semantics, almost no warehouse adoption. QB has
been a W3C Recommendation since 2014 and carries the same
dimension/measure/attribute trichotomy Varda's column roles are a renaming
of, with twenty-one integrity constraints written as SPARQL — a vocabulary
plus a checkable rule set, which is this package's shape exactly.

Emitting ODCS is a bet on the first track. It is the right first bet: that
is where the adoption is, the mapping is mostly one-to-one, and the artifact
is a file somebody's platform already ingests. But it is a bet, and the
second track is reachable from here more cheaply than from anywhere else —
LinkML already generates OWL, SHACL and RDF from the same source, so what
that track needs is not a generator but `class_uri`, `slot_uri` and
`exact_mappings` onto vocabulary that exists. The profile has two
`meaning:` mappings today, both into OWL-Time, and no URI mappings at all.

Nothing here forecloses that, and neither should be built as though the
other were settled. The survey behind this paragraph, with both tracks
measured, is at
`claude.ai/code/artifact/26830052-d912-4e33-8a37-d83aa8368ab7`.

## What maps, and what does not

Fifteen of twenty-two mappings are one-to-one. Two are lossy. Seven have no
home in the standard, and those seven are the whole of what Varda knows.

| Varda | ODCS v3.1.0 | |
| --- | --- | --- |
| table name | `schema[].name` | clean |
| `varda:physical_name` (table) | `schema[].physicalName` | clean |
| column name | `properties[].name` | clean |
| `varda:physical_name` (column) | `properties[].physicalName` | clean |
| `range` | `logicalType` + `logicalTypeOptions` | clean |
| `sql_type(column, dialect)` | `physicalType` | clean |
| `varda:max_length` | `logicalTypeOptions.maxLength` | clean |
| `required` | `required` | clean |
| `varda:references` | `relationships[]` | clean |
| `SURROGATE_KEY` | `primaryKey`, `primaryKeyPosition` | clean |
| `unique_claims` | `quality: duplicateValues` | clean |
| `--dialect` | `servers[].type` | clean |
| `--schema` | `servers[].schema` | clean |
| `description` | `description` | clean |
| `varda:grain_statement` | `dataGranularityDescription` | clean |
| `varda:precision`, `:scale` | `physicalType` string only | lossy |
| `varda:hierarchies` | `customProperties`, flattened | lossy |
| `varda:grain` | — | no home |
| `varda:role` | — | no home |
| `varda:scd` | — | no home |
| `varda:fact_type` | — | no home |
| `varda:additivity` | — | no home |
| `varda:semi_additive_over` | — | no home |

Three things make the clean column cheaper than it looks.

**Every Varda dialect is already an ODCS server type.** The `servers[].type`
enumeration holds thirty-four values, and `duckdb`, `postgres`,
`postgresql`, `snowflake` and `sqlserver` are all among them. `--dialect`
becomes one field with no table in between.

**`physicalType` is derived here and authored there.** ODCS asks the person
writing the contract to type `varchar(18)` or `DOUBLE` per column, which is
the same trust in an unnamed dialect that `docs/design.md` describes under
*Why there is a dialect, and why its default is named* — an ODCS contract
written against PostgreSQL and deployed to SQL Server carries `TIMESTAMP`,
which there is a row-version counter. Varda computes that field instead of
accepting it, so a generated contract is correct per target and an author
cannot get it wrong.

**The timezone question is already answered on both sides.**
`timestamptz` maps to `logicalType: timestamp` with
`logicalTypeOptions.timezone: true`, which is exact. ODCS reached the same
problem independently and answered it as a facet where Varda answered it as
a type; the reasoning for the difference is in `design/temporal-types.md`
and turns on a fact about LinkML that does not apply to ODCS. Only
`defaultTimezone` is inexpressible from this side, and Varda has nothing to
put in it.

## The seven with no home

They go into `customProperties` — a flat array of `{property, value}`
available in most blocks, with no namespacing, no declared shape, and no
validation. It is a materially weaker seam than the one `ext.py` and
`registry.py` provide, and it is the one place where a Varda extension's
information genuinely degrades on the way out.

That is not an argument against emitting them. It is the argument for
emitting them *and* saying so upstream. Bitol runs an open TSC and an RFC
process, and v3.1.0 took a structural addition through it: `relationships`,
composite foreign keys included, did not exist before December 2025. The
grain proposal is the smallest and best-evidenced of the seven —
`dataGranularity.properties[]` beside the existing description, so the field
that already exists gains a checkable half. Three releases of this package
are the evidence that the column set is what catches the error.

Sequence matters: the generator first, the RFC second, so the proposal
arrives with a working implementation behind it rather than as an opinion.

## The quality block is the part that pays for itself

`gen_assertions` already computes every claim the model makes that a
database can be asked not to enforce. On `examples/retail.yaml` that is
twenty-seven claims in three shapes, and two of the three have an exact
ODCS library metric:

| Varda assertion | Count | ODCS |
| --- | --- | --- |
| `UNIQUE (…)`, `PRIMARY KEY (…)` | 13 | `duplicateValues` + `arguments` |
| `… is populated` | 5 | `nullValues`, `mustBe: 0` |
| orphan check on a reference | 9 | no library metric exists |

Eighteen of the twenty-seven become portable: a library metric is a name
and a comparison, and every engine that speaks ODCS knows how to run it,
where Varda's own SQL is portable across the seven dialects it was tested
under and no further.

The third is a real gap in the standard rather than in this mapping. The
maintained metric library is `rowCount`, `nullValues`, `missingValues`,
`invalidValues` and `duplicateValues` — nothing referential. An orphan check
has to be emitted as `type: sql`, whose query "must be written in the SQL
dialect specific to the provided server," with no translation offered. Varda
is unusually well placed to emit that correctly, since it already writes one
dialect at a time and knows which.

The shape, verified against the specification's own examples:

```yaml
schema:
  - name: DimCustomer
    physicalName: dim_customer
    physicalType: table
    dataGranularityDescription: One row per version of a customer.
    customProperties:
      - property: vardaRole
        value: DIMENSION
      - property: vardaScd
        value: TYPE_2
    quality:
      - metric: duplicateValues
        mustBe: 0
        dimension: uniqueness
        arguments:
          properties:
            - customer_id
            - valid_from
    properties:
      - name: valid_from
        logicalType: timestamp
        logicalTypeOptions:
          timezone: true
        physicalType: TIMESTAMP WITH TIME ZONE
        required: true
        customProperties:
          - property: vardaRole
            value: VERSION_START
```

## How it would be verified

Not by reading it, and not by diffing a golden file. Three levels, in the
order they are worth having:

1. **Against the published JSON Schema.** ODCS ships
   `schema/odcs-json-schema-latest.json`, 86,492 bytes with clean `$defs`,
   and the standard's own note that where the two disagree the prose wins.
   Validating emitted output against it is the same argument the sqlglot
   dialect tests already make: held to something maintained elsewhere rather
   than to nothing.
2. **Against an external tool.** `datacontract lint` accepts or rejects a
   file, which is a second judge that moves independently of this package.
3. **Round-tripped through the DDL.** `datacontract export sql --dialect
   postgres` on the emitted contract, against `sql/mart.sql` under
   `--dialect postgres`. The two will not match as text — this is the same
   condition the SQLAlchemy generator is in — so compare what a database
   builds from each, which is machinery that already exists.

The first is a test. The second and third want a network and a second tool,
so they belong in the gate at most and possibly nowhere: `run_all.sh` is a
script a contributor runs offline, and adding a download to it is a cost the
package has so far refused.

## What not to build

**The SLA, team, roles, support and pricing blocks.** Five of the eleven
ODCS sections describe an operational deployment rather than a model: a
latency guarantee with a regulatory driver, named stewards with `dateIn` and
`dateOut`, access grants with two levels of approver, Slack channels, a
price per megabyte. None is derivable from a star schema, and absorbing them
would double a vocabulary whose own profile argues that "fifteen annotations
is not a first cut on the way to forty."

A generated contract should leave them **absent rather than stubbed**, so
whatever owns them — a platform, a person, an ODPS data product one layer up
— fills them in without a merge conflict. ODPS already models this
relationship: it references contracts by `contractId` from its input and
output ports rather than inlining them.

**Ingest.** Reading an ODCS contract and producing a LinkML model is the
mirror image and much harder: a contract carries no grain, no additivity and
no SCD, so the result is a model that fails `varda check` on rules the
source could not have satisfied. The one honest version of this is a
scaffolding command that emits a model with `TODO` annotations, which is a
different feature with a different argument, and not this one.

**A second export surface.** Twenty-eight formats already exist downstream.
Anything added here that ODCS can already express is work done twice.

## Decisions this needs

**Where the contract's `version` and `status` come from.** ODCS requires
both. Varda has no vocabulary for either, and both are properties of a
release rather than of a model — LinkML's own `version` on the schema is the
obvious source for the first, and `status` has no source at all. Options: a
CLI flag, a `varda:` annotation, or emitting `status: draft` always and
documenting that the field is the deploying tool's to set. The last is
probably right and is the least work, but it makes the artifact one that
always needs an edit, which no other generator here produces.

**Whether `id` is stable.** ODCS wants a UUID that survives renames. The
LinkML schema `id` is a URI and already stable, but it is not a UUID and the
JSON Schema may or may not care. Determinism is non-negotiable —
`run_all.sh` diffs two runs — so generating one is out unless it is derived
from the schema id.

**The `customProperties` naming convention.** ODCS says names "should be in
camel case," so `varda:scd` becomes `vardaScd`, which loses the prefix that
makes a Varda annotation identifiable and collides with any other tool that
picks the same word. The alternatives are keeping the colon, using
`varda_scd`, or nesting everything under one `varda` property whose value is
an object. The third preserves the namespace and is the least idiomatic;
this wants deciding before anything ships, because it is what an RFC would
be arguing about.

## Reproducing the numbers

ODCS v3.1.0 as of 2026-08-30, from `bitol-io/open-data-contract-standard` at
`main`.

```console
$ curl -sfLO https://raw.githubusercontent.com/bitol-io/\
open-data-contract-standard/main/docs/schema.md
$ for w in grain additivity 'slowly changing' scd surrogate \
      conformed hierarchy 'star schema'; do
    printf '%-16s %s\n' "$w" "$(grep -ic "$w" schema.md)"
  done
```

The server enumeration and the required-key list are read out of the JSON
Schema directly:

```python
import json
import urllib.request

url = (
    "https://raw.githubusercontent.com/bitol-io/"
    "open-data-contract-standard/main/schema/odcs-json-schema-latest.json"
)
with urllib.request.urlopen(url) as handle:
    schema = json.load(handle)

print("required:", schema["required"])
servers = schema["$defs"]["Server"]
print(
    "types   :",
    next(
        node["enum"]
        for node in servers["properties"].values()
        if isinstance(node, dict) and "enum" in node
    ),
)
```

The assertion counts are this package's own:

```console
$ varda generate examples/retail.yaml --out /tmp/v
$ grep -c '^SELECT' /tmp/v/sql/assertions.sql
$ grep -cE '^-- .*(PRIMARY KEY|UNIQUE) \(' /tmp/v/sql/assertions.sql
$ grep -cE 'is populated' /tmp/v/sql/assertions.sql
$ grep -c 'AS "orphans"' /tmp/v/sql/assertions.sql
```
