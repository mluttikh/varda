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

**Generation fails closed.** Every generator runs and every result is
collected before a byte is written.

**Generated output is deterministic.** No timestamps, no hostnames, no
environment, no dict-ordering dependence. Tables are sorted by name so that
moving a class in the source does not reorder the output.

**Unknown values raise; they never default.** An unmapped SQL range raises
`GenerationError` naming the column. A column silently typed `TEXT` is a bug
that surfaces years later as a comparison that does not do what it looks
like.

**Rule codes are permanent.** `V203` goes in commit messages and exemption
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

Twelve annotations is not a first cut on the way to forty. It is the
deliberate size.

Every annotation in the core is one a newcomer has to read before they can
write a model, and one that every organization inherits whether it wants it
or not. Anything that differs between organizations — and cost centers,
retention and classification all do — costs less as an extension than as a
core concept everybody must ignore.

The parts deliberately left out of 0.1, with the reasoning for each, are
listed in `SPEC.md` §4. Analytical functions, model diffing, lineage export
and the drift gate all exist in a larger internal prototype and were cut.

## Why a level carries a name and a key

A level answers two questions that look like one. *What is this called* —
`city_name` holds "Springfield" — and *which member is it* — one of the
Springfields in Illinois, Massachusetts and Missouri. SML and AtScale keep
them apart as `name_column` and `key_columns`; SQL Server Analysis Services
splits the same pair across attribute relationships and user hierarchies.
Varda keeps them apart too, and defaults the second so that most levels do
not have to say it.

The default is whatever already identifies the level. A level reached through
a foreign key is identified by that key, because a foreign key selects exactly
one row of the dimension it points at. A level naming a column of its own
table is identified by that column. Both are right often enough that the bare
form carries most hierarchies:

```yaml
levels: [country_key.country_name, state_key.state_name, city_name]
```

Neither is right in a denormalized dimension whose levels are labels.
`country_name`, `state_name` and `city_name` describe a correct path, and no
single one of them identifies a member — which is exactly the case a star
schema produces, since denormalizing is the point. There the key is declared:

```yaml
levels:
  - country_name
  - {column: state_name, key: [country_name, state_name]}
  - {column: city_name,  key: [country_name, state_name, city_name]}
```

The alternative was to require a column that identifies on its own and let a
modeler add one. That works for a date dimension, which really does carry
`year_month`, and not for geography, where `US-WA-Springfield` is a column
invented to satisfy a validator. A vocabulary that asks for data to be
reshaped to suit it has the dependency backwards.

Nothing checks that a key is unique — that is a claim about data, the same
bargain [the grain sentence](#why-the-grain-sentence-is-not-checked-against-the-columns)
makes. [`V127`](reference/rules.md#v127) checks the columns exist and are the
kind that can identify something.

A surrogate key and a foreign key make good keys and bad names: nobody drills
into `4718`. So they are legal in `key` and refused in `column`, which is the
same distinction from the other side.

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
themselves. `V205` still asks every measure to declare a unit — two measures
in different currencies add up cleanly and wrongly — it just asks the
question of LinkML's slot.

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

A hand-written table of thirty-six rules disagrees with the code within two
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
