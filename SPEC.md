# Varda — implementation specification

**Version 0.4.0 · status: reference implementation complete, ready to hand
over. The vocabulary it implements is experimental and will change; 1.0 is
where that stops.**

This document is the brief for whoever takes Varda forward. It says what
exists, what was deliberately left out, which decisions must not be quietly
reversed, and what to build next in what order.

---

## 1. What Varda is

A *profile* of LinkML that adds the vocabulary of dimensional modeling, the
rules that check a model against it, and generators that build from it.

The critical property, and the reason the design is shaped this way: **a model
annotated with Varda is still an ordinary LinkML schema.** Every other LinkML
tool reads it and ignores what it does not understand. Varda adds no new
syntax, forks no metamodel, and requires no changes to LinkML. Anything
proposed later that breaks this property is not a Varda feature.

It is also the property that was argued for three releases and never run, and
was false: `imports: - varda` reached a profile declaring four annotation
classes, a LinkML import is a union, and every stock generator emitted them.
The suite now runs `gen-erdiagram`, `gen-json-schema`, `gen-pydantic` and
`gen-owl` over both examples and asserts that nothing of Varda's own appears
in the output. A property this load-bearing is worth what it is tested at.

The second critical property: **the core is small, and organizations extend
it themselves.** Cost centers, retention policy, data classification,
ownership — these differ per organization and none of them belong in Varda.
The extension mechanism is not a nice-to-have bolted on the side; it is the
reason the core can stay small enough to be correct.

## 2. What exists today

| Module | Lines | Responsibility |
| --- | --- | --- |
| `snapshot.py` | ~790 | The model as frozen data: `Model`, `Table`, `Column`, `Hierarchy`, `Level`, `UniqueKey`, `Ref`, `Annotations`, what a model says an element means (`Mappings`, `Unit`, `Enumeration`), and the closed vocabularies as `StrEnum`s. **The public read API.** |
| `analysis.py` | ~355 | What Varda concludes from a snapshot: uniqueness claims, the key a foreign key lands on, what a column stores, creation order, level identity. Public, on a weaker promise. |
| `_linkml.py` | ~660 | Reads a LinkML schema into a snapshot. Internal, and the only module that knows LinkML exists. |
| `ext.py` | ~525 | `Extension`, `Rule`, `RuleSet`, `Finding`, `Issue`, `Report`, `Generator`, `Context`, `Options`, and re-exports of the snapshot and `analysis`. **The module a third party imports.** |
| `vocabulary.py` | ~270 | What the active profiles declare, read once: each annotation's range, `required` and `multivalued`, as V006 and V007 enforce them, and what each enum value means. |
| `registry.py` | ~810 | `Registry`: discovery, validation, and everything derived from one extension set. Where colliding extensions and malformed generator settings are refused. |
| `api.py` | ~410 | Varda as a library: `load`, `check`, `generate`, `write`, `compare`. The rule engine and fail-closed generation live here. |
| `rules.py` | ~2340 | The 67 core rules. |
| `gen_sql.py` | ~555 | SQL DDL, the dialects it is spelled in, and how much of it the database is asked to police. |
| `gen_docs.py` | ~255 | Markdown reference. |
| `gen_sqlalchemy.py` | ~460 | SQLAlchemy Core tables, database-neutral, carrying the annotations to runtime. |
| `gen_assertions.py` | ~180 | The model's claims as queries over the data, for where a constraint cannot or should not run. |
| `claims.py` | ~555 | What a model says about its data, as data: the closed claim shapes, and `implied`, what Varda reads a model as claiming. Public; `implied` on `analysis`'s weaker promise. |
| `gen_expectations.py` | ~380 | Renders every active extension's claims, Varda's included, as queries over the data. |
| `generators.py` | ~60 | Varda's generators, registered through the public interface. |
| `cli.py` | ~520 | Six commands, each a call into `api` and the printing around it. |

**9,199 lines of source**: 5,218 of code, 1,969 of docstrings, 601 of
comment, 1,411 blank. The prose share is deliberate and is house style —
this is a package other people extend, and the reasoning behind a
constraint is worth more to them than the constraint itself.

Plus two schemas. `profile/varda.yaml` is the vocabulary — 18 annotations,
8 enums — which the registry reads off disk and no model imports.
`profile/types.yaml` holds the one type a model may need to name, and is all
that `imports: - varda` resolves to; a LinkML import is a union, so what is
importable is kept to what is meant to be imported. And 554 tests in 9,128 lines.

### The five seams

An extension plugs in at exactly five places. Everything else is internal.

1. **Vocabulary** — a LinkML schema declaring annotations under the
   extension's prefix. Read into `Vocabulary`, enforced by rules `V001` and
   `V002`.
2. **Rules** — a `RuleSet` whose codes all begin with the extension's tag.
   Merged into `Registry.rules`.
3. **Generators** — `Generator(name, artifacts, run, settings)`. Paths
   declared up front so collisions are caught at load; a path ending in `/`
   claims a directory, for output named by the model. `settings` declares
   what the generator reads from its `[generator.<name>]` table in
   `varda.toml`.
4. **Claims** — a function from the model to what it claims about its
   data, built from the closed shapes in `varda.claims` and rendered beside
   Varda's own in `sql/expectations.sql`. A claim is about one table of the
   model being generated from, and one that is not stops the run with a
   `ClaimError` naming the extension.
5. **Distribution** — a `varda.extensions` entry point, or a `varda.toml`
   entry, or `[[extension]]` inline in TOML for the no-Python case.

## 3. Invariants

These are load-bearing. Each one is enforced in code and covered by a test.
Changing any of them is a design decision, not a refactor.

**I1 — One party, one namespace.** An extension owns exactly one prefix.
Reserved prefixes (`varda`, `linkml`, `skos`, …) are refused, duplicates are
refused. *`registry._check_prefixes`*

**I2 — Extensions add, they never redefine.** An extension may not redeclare
an enum or class that another extension owns. Widening `Additivity` from
outside would turn every generator's exhaustive dispatch into a silent
fallback path. *`registry._check_vocabulary_collisions`*

Varda may widen its **own** enums in a minor release — that is backward
compatible, since no existing model breaks when a new value becomes legal. A
missing enum value is a request to file, not a reason to fork.

**I3 — The core goes through the public interface.** `varda` is itself an
`Extension`. If the core ever needs a privilege the mechanism does not offer,
that is a defect in the mechanism. *`registry.varda_extension`*

**I4 — Generation fails closed.** Errors refuse the run before a generator
is reached, because artifacts built from a model that does not conform look
finished and are not. Past that, every generator runs and every result is
collected before a single byte is written. A half-generated output tree is
worse than none: it looks complete, and the stale parts are the ones nobody
thinks to check. Every path a generator produces is checked before that
point, not only every path it declares: under a directory claim the names
come from the model, so a table named `../x` would otherwise pass every
check at load and be written outside the tree. *`api.generate`,
`test_generate_fails_closed`, `test_generate_refuses_a_nonconforming_model`,
`test_a_produced_path_leaving_its_directory_is_refused`*

**I11 — Somebody else's code failing is named, not re-raised.** A generator
that raises is reported as `{name} failed: …`; so is a rule, as
`{code} ({extension}) failed: …`. Both stop the run. The checking half of
this was missing until 0.4: the engine called each rule bare, so a
third-party rule reached the operator as a traceback through a package they
did not write, with no code to exempt and no extension named — on the
command that runs on every pull request. Skipping the rule and reporting the
rest was the alternative and is worse: a rule that cannot run is a check that
is not happening, and exiting zero would be the tool saying a model conforms
when part of the question was never asked. *`api.check`, `cli.main`,
`test_a_rule_that_raises_names_itself_and_the_extension_that_shipped_it`*

**I12 — An extension imports one module.** Everything an extension runs on is
reachable from `varda.ext` — `Extension`, `RuleSet`, `Finding`, `Generator`,
`Context`, the `Model` a rule walks and every element under it, and
`analysis` — types included. This was stated in four places and false in all
of them until 0.4: `Finding` and `RuleSet` lived in `varda.rules`, and a rule
annotated with its argument's type named `DimensionalModel` in `varda.model`.
`test_the_worked_example_imports_only_the_interface_module` reads the
fixture, type-only imports included, rather than trusting its docstring,
which is the narrow form of the conformance scan below.

The model an extension reads is `varda.snapshot`, public and versioned: a
field is added in a minor release and removed or retyped only in a major
one. `varda.analysis` is public on a weaker promise — an answer may become
stricter in a minor release while its signature holds. The reasoning is in
`docs/design.md`, under *Why an extension reads a snapshot*.

**I5 — Generated output is deterministic.** No timestamps, no hostnames, no
environment, no dict-ordering dependence. Output that changes when the model
did not makes "is this current?" unanswerable. Tables are sorted by name for
the same reason.

**I6 — Unknown values raise; they never default.** An unmapped SQL range
raises `GenerationError` naming the column. A column silently typed `TEXT` is
a bug that surfaces years later as a comparison that does not do what it
looks like.

**I7 — A code names its concern, and never means two things.** The band
says what a rule is about: `V6xx` is hierarchies, `V7xx` is measures. Within
a released major version a code is permanent — it goes in commit messages and
exemption lists, so renumbering is a breaking change to humans. Before 1.0
that permanence is not yet in force, and the bands were introduced by
renumbering every code that existed. What holds unconditionally is the second half:
retire a code by deleting it, and never reuse it for something else.

**I8 — Severity conflicts are refused, not resolved.** Two extensions naming
different severities for one code is an error naming both parties. Resolving
by load order makes behavior depend on discovery order, which is a
difference between machines nothing in either repository explains.
`varda.toml` overrides everything, because the repository can see itself and
an upstream cannot.

**I10 — The profile namespace never changes.** The schema `id` is
`https://w3id.org/varda` and the `varda:` prefix expands to
`https://w3id.org/varda/`. Both are copied into the `prefixes:` block of
every model anyone writes and into every RDF graph generated from one, so
changing either silently invalidates every model in the wild.

A w3id.org IRI is used rather than a hostname this project owns precisely so
that the *target* can move while the *identifier* does not — a domain can
lapse or be sold, and a lapsed domain baked into published schemas is
unrecoverable. Redirects live in `w3id/.htaccess`; changing where the IRI
points needs no change to the package. *`test_profile_namespace_is_pinned`*

**I9 — The typed boundary holds.** `_linkml.py` is the wall along the
untyped LinkML runtime and the only module that reads it; what it builds is
a snapshot of frozen, concrete types holding nothing LinkML made — not a
`SlotDefinition`, and not the `extended_str` an annotation value arrives as.
`mypy --strict` passes, and `warn_return_any` is the rule doing the work. Do
not switch it off to make a stubborn line pass.
*`test_a_snapshot_holds_nothing_linkml_made`*

## 4. Deliberately not in 0.1

Each of these exists in a larger internal prototype and was cut. The
rationale matters as much as the list — several would be actively harmful to
add early.

| Deferred | Why | Rough size |
| --- | --- | --- |
| **Analytical functions** — `FunctionAnnotations`, parameters, implementation bindings | 17 further annotations for a feature most models never touch. Adding it early doubles the vocabulary a newcomer must read. | ~400 lines |
| **Drift gate** (`varda verify`) | Genuinely valuable and genuinely cheap; cut only because it is worth nothing until generated output is being committed. Added in 0.4. | ~150 lines |
| **Model diffing / evolution** | Needs a stable vocabulary to diff against. Building it before 1.0 means rebuilding it. | ~360 lines |
| **Lineage and catalog export** | Every organization's catalog is different; this is extension territory until a second real consumer exists. | ~280 lines |
| **Extension conformance kit** | A test harness proving a third-party extension is deterministic and stays inside the public API. Worth nothing until third-party extensions exist. | ~230 lines |
| **`requires_profile` version pinning** | PEP 440 pin of an extension against the profile version. Pulls in `packaging`. Add when a second Varda version exists to be incompatible with. | ~40 lines |
| **M1 data-quality expectations** | Claims about *rows*, not about the model. A different layer with a different audience; do not blur it into the rules. | ~250 lines |
| **More generators** (SQLAlchemy, ERD, dbt, Pydantic) | Each is straightforward against the model layer. Add on demand, one per real consumer. | ~150 each |

## 5. What has shipped, and what is next

Two lists, and the split is deliberate. This section used to number the work
that had not happened yet — "0.2 will add `varda verify`" — and every release
that went out for some other reason left it describing a version that had
come and gone without doing what it promised. It was renumbered twice in two
releases for exactly that reason. A version number is earned by a release,
not assigned to a plan, so the plan does not carry one.

### Shipped

**0.1 — the profile and its rules.** The annotation vocabulary, the
conformance rules, the SQL and Markdown generators, the extension mechanism.

**0.2 — say what the output is.** Type facets, so a column states its width
and a generated star does not truncate to one character on SQL Server. Named
dialects, because there is no neutral SQL and the type table was
PostgreSQL's the whole time. A `uuid` type. `V306` and `V307`, so several
natural keys are not silently merged into one constraint that enforces
neither.

**0.3 — the checks reach where they were aimed.** A review found nineteen
places where a discipline this codebase already holds stopped one step short.
The config file validates what it is given the way an extension already did;
the caches behind those checks cannot go stale; a declared artifact path is
constrained by being declared; the emitted DDL escapes its schema name and
states each key once; a range resolves through its `typeof`, so a type the
schema declares generates instead of stopping the generator. Plus the two
extension-discovery routes a third party actually uses, which had never been
tested — and which broke the moment they were.

**0.4 — the model as objects, not only as a script.** A SQLAlchemy
Core generator: a `MetaData` and one `sa.Table` per table, no ORM, and no
database named anywhere in it — generic types throughout, so one module
serves every engine while the DDL goes on naming one at a time. Worth more
than a second rendering, because `info` carries the annotations to runtime —
a consumer refuses to sum a semi-additive measure across the dimension it is
not additive over, with no Varda installed — and `comment` reaches the
database catalog, which the DDL's `--` comments do not. It is held to the DDL
by execution: both are run against DuckDB at every constraint level and the
catalogs compared, and where the generic types differ from the dialect tables
a test pins each divergence rather than correcting it.

**0.4 — a model imports what it needs and nothing else.** The profile
is split: the vocabulary stays where the registry reads it, and a new
`types.yaml` carries the one type a model names. `imports: - varda` means
what it always did and no model needs an edit; what changed is that four
annotation classes and five enums stop arriving with it and stop appearing in
every stock generator's output. `Extension.types` generalizes it, the import
map is built from it alone, and `varda importmap --json` prints the map in
the shape a generator's `--importmap` reads.

**0.4 — the operator chooses when the check runs.** A constraint is a
claim about the data *and* an instruction to verify it on every write, and
Varda emitted the two together because SQL spells them with one word.
Checking is twenty-three times the cost of a bulk load on the engines that
really do it, and impossible on the ones that do not. `--constraints` now
takes a level — `enforced`, `asserted`, `none` — which `Dialect` renders into
whatever the engine has for saying it, and the claims a weaker level drops
move into `sql/assertions.sql` rather than disappearing. Design note and
measurements in `design/constraint-enforcement.md`.

**0.4 — the boundary is where it says it is.** Three findings from an
architecture review, and each is a discipline this codebase already holds
stopping one step short of where it was aimed. `Finding` and `RuleSet` move
to `varda.ext`, so the sentence four files repeat — an extension imports the
interface module — is true for the first time; `varda.rules` re-exports both,
so nothing written against the old import breaks. A rule that raises is now
caught and named the way a generator that raises has been since 0.1, on the
command that runs in everybody's CI rather than the one that writes files.
And a class reached through `imports:` is reported as what it is: findings
carry the file that declared them, `--skip-imported` drops another schema's
findings without dropping its tables from any rule, and `V801` reports a
physical-name collision against a class this model declares.

**0.4 — the extension interface, settled.** Breaking, once. The
model a rule walks is `varda.snapshot`, frozen data holding nothing from
LinkML, and what Varda concludes from it is `varda.analysis`; a rule yields
`Finding(subject, message)` and the engine adds the rest; the registry is an
object, found from the model's directory; and `varda.api` is the library the
CLI is built on. Every output tree is byte-identical but one ordering, which
now agrees across the three SQL artifacts. Reasoning in `docs/design.md`;
migration in `docs/extending.md`.

**0.4 — the committed output is checked.** `varda verify` generates
exactly as `generate` would with the same flags, writes nothing, and exits
`1` when a file under `--out` differs, is missing, or is one a generator
declares and no longer produces; `--diff` shows how. It is
`api.compare` and the printing around it. Line endings are not compared, so
a CRLF checkout passes, and a file no generator declares is never judged.

**Unreleased — a generator can write what the model names.** Two changes to
the generator seam, both additive, because an OKF bundle — one Markdown file
per table, for agents to read — could not be written against it. A
declaration ending in `/` claims a directory: nothing may overlap it, case
ignored, every path produced under it is checked when it is produced, and
`verify` reports any file in it the run did not produce, which is the
removed-table half of the manifest the drift gate was waiting for. And a
generator declares `settings`, read from its own `[generator.<name>]` table
in `varda.toml`, checked at load for unknown tables, unknown keys and wrong
types, and handed over read-only on `Context.settings`. A file rather than
a flag, so `generate` and `verify` cannot disagree about it.

**Unreleased — the snapshot carries what a model says it means.** A model
could give every table a `class_uri` and every measure a QUDT unit, pass
`check`, and reach every rule and generator with none of it: the snapshot
held Varda's annotations and a unit reduced to one label. It now holds
LinkML's own metaslots as written — `uri` (`class_uri`, `slot_uri`),
`title`, `aliases` and `mappings` on `Table` and `Column`,
`Column.unit_of_measure` with `unit` still its label, and `Model.enums`
with the `meaning` of each value — plus `Model.prefixes` and
`Model.expand`, and `Vocabulary.meaning` for the profile's own values.
Additive, and no vocabulary: nothing is annotated differently, no rule
reads any of it yet, and no generator's output changes. An undeclared IRI
is `None` rather than the one LinkML would mint, which is the same for a
`date_key` in every table; what such a column is called in RDF is the
generator's to decide. *`test_what_a_model_says_an_element_means_reaches_the_snapshot`*

**Unreleased — a profile's declarations are enforced.** A profile declares
each annotation as a LinkML attribute, and LinkML enforces none of it on an
annotation, so `required`, the range and `multivalued` were documentation:
an extension wanting `cost_center` on every table wrote the declaration and
a rule saying it again. V006 reports a required annotation, or a required
field of a structured one, that is missing or blank; V007 a value its range
does not admit — a whole number, true or false, a number, a mapping where
one is declared, one value where a list is not. Enums stay V002's and the
type facets V803's, and every case a dedicated rule already reports is
skipped, named in `REPORTED_ELSEWHERE` and provoked by a test, so one
mistake is one finding. A scalar where `multivalued` allows a list is one
value of it, as `grain: order_id` always was. A profile class may name the
field a bare value stands for with `<prefix>:shorthand`, which is how a
`Level` is written as a column name, and the registry refuses one naming no
field. Two findings Varda did not make before: a hierarchy written as a
bare name was silently dropped, and `grain_statement: [a, b]` reached the
docs as a Python list. *`test_v006_an_extensions_required_annotation_is_enforced`*

**Unreleased — the claims no constraint can state.** `sql/expectations.sql`,
from a fifth built-in generator, holds what a model says about its data
that DDL cannot: a type-2 dimension's versions are each in force for some
time and do not overlap, at most one is current, and the current one is the
latest — per business key, each claim holding a versioning column less that
column; and in a dimension that is not type 2, a level reached through a
foreign key sits under one parent, and a level declaring a key apart from
its name is named one way per key. A file of its own because
`assertions.sql` returns nothing exactly when the dropped constraints would
have held. Plain SQL, a flag read as `CAST(flag AS INTEGER) = 1`, executed
against DuckDB and parsed for every engine the assertions are. A plain
level's strict roll-up is not checked, because its column identifies a
member only under its parent; that needs a declaration, which is the next
step's. See `docs/design.md`, *Why expectations are a file of their own*.
*`test_the_expectations_find_a_broken_history`*

**Unreleased — claims are a seam.** What `expectations.sql` checks is now
data: `varda.claims` holds five closed shapes — `Determines`,
`PeriodNonEmpty`, `PeriodsDisjoint`, `AtMostOneFlagged`, `FlaggedIsLatest`
— each with the element it is about and the sentence that labels it.
`claims.implied` is what Varda reads a model as claiming, public on
`analysis`'s weaker promise, and the generator only renders, dispatching
exhaustively. An extension adds claims through `Extension.claims`, rendered
beside Varda's and labelled `(claimed by <name>)`; a claim about a table
of another model, naming a column of another table, or with no statement
or no key is refused with `ClaimError`, as is a claims function that
raises. Data rather than SQL, so `varda.ext` stays free of SQL and another
renderer — a dbt test, a quality suite — reads the same claims without
deciding again what the model meant. Output is unchanged but for one
column alias: a dependency counts `"dependents"`, not `"parents"` or
`"names"`. The worked extension shows the seam doing what Varda does not:
Acme holds a dimension marked `acme:strict_hierarchies` to strict roll-up.
*`test_an_extensions_claim_is_rendered_and_says_who_made_it`*

**Unreleased — what a semi-additive measure takes instead.** Two gaps the
semantic layers had closed and Varda had not. `varda:semi_additive_over` is
multivalued, so a headcount snapshot per month and per scenario sums across
neither; one name written alone is one of them, as every model before this
wrote it. And `varda:semi_additive_aggregate` — `LAST`, `FIRST`, `MIN`,
`MAX`, `AVERAGE` — says what the measure takes across those keys in place
of the sum, which is the half of the answer every report otherwise supplies
for itself. V703 checks each key and refuses one named twice; V708 refuses
an aggregate on anything but a semi-additive measure, V709 a first or last
across more than one order, and V710 warns on a semi-additive measure that
names none. The snapshot gains `Column.semi_additive_keys` and
`Column.semi_additive_aggregate`, with `semi_additive_over` kept as a
property answering the first key. The docs page names every key and the
aggregate; the SQLAlchemy `info` mapping carries `semi_additive_over` as a
list of names, always, and `semi_additive_aggregate` beside it. The retail
example's stock on hand takes `LAST`.
*`test_a_measure_may_be_semi_additive_over_several_keys`*

**Unreleased — reporting as it is now.** A fact could reach a type-2
dimension only as it was when the fact happened. A `DURABLE_KEY` column
names one entity across all its versions, and a foreign key saying
`varda:lands_on: DURABLE_KEY` reaches it, so a fact carrying both kinds of
key reports by the attributes then or the attributes now — Kimball's type
7, with history still declared per table. The durable key's uniqueness is
derived as the natural key's is, and the history expectations are checked
per durable key as well. No `FOREIGN KEY` can land on a key that repeats
per version, so the DDL says why in a comment, the SQLAlchemy module emits
no `ForeignKey` and carries `lands_on` in `info`, and the orphan check
still runs; `analysis.Reference.enforceable` is the one place that decides
it. V308 refuses two durable keys, V309 warns on one that is not required,
V409 refuses `lands_on` off a foreign key or onto a durable key the target
lacks, and V103, V304, V305 and V605 cover the new role. The retail example
carries one. *`test_a_durable_reference_has_no_foreign_key_and_says_why`*

**Unreleased — one step of history, in a column.** Kimball's type 3:
`varda:previous_of: category` on `prior_category` says the column holds
what `category` was before its last change, so a report can group by the
new value or the one it replaced. V507 refuses one that names no attribute
of its own table, names itself, sits on anything but an attribute, or
chains round in a cycle — a chain that ends is legal; V508 one that stores
another type or is narrower than its current value, which would cut the
longest values on the first change; and V509 one on a fact or a bridge,
whose rows are not revised, or on a `TYPE_0` dimension, which keeps every
original value. The snapshot gains `Column.previous_of`; the docs page and
the SQLAlchemy `info` mapping carry it. History is still declared per
table: this names a column's meaning, not a second slowly-changing type.
The retail example's products keep their prior category.
*`test_v507_a_chain_of_previous_values_that_comes_back`*

**Unreleased — a hierarchy says what it guarantees.** A hierarchy declares
`strict` and `complete`, after Lenz and Shoshani's summarizability
conditions, and `sql/expectations.sql` checks both. `complete`, true by
default, claims every row holding a level holds the one above it — a brand
with no category drops out of every roll-up by category — through a sixth
claim shape, `Requires`; not where the parent column is required, which the
DDL states, and not for `complete: false`, which a path that may skip a
level says instead. `strict`, false by default because a plain level names a
member only under its parent, says each level's own key identifies it:
`analysis.identities` then gives each level its key alone, and every level
is held to one parent, as only a level reached through a foreign key was
before. Roll-up stays unchecked on a type-2 dimension, whose rows are
versions; completeness is checked there too. The snapshot's `Hierarchy`
gains both, the docs page marks a strict path and one that may skip levels,
and the SQLAlchemy `info` mapping carries both for every hierarchy, which
committed output will show as drift. Retail's date paths are strict and its
store geography may skip a region; its expectations gain ten queries.
*`test_a_strict_hierarchy_holds_each_member_to_one_parent`*

**Unreleased — trees of any depth.** A hierarchy declares its `kind`:
`LEVELS`, the default and everything before this; `PARENT_CHILD`, a tree
each row places itself in through `parent`, a foreign key into its own
table; or `BRIDGED`, the same tree flattened into a bridge naming its
`ancestor`, `descendant` and `depth`, so a query reaches a whole
organization with one join. Each kind is read through its own fields: V608
refuses a hierarchy missing what its kind needs or carrying another kind's,
V609 a parent that is not a foreign key into the hierarchy's own table, and
V610 a bridge that is not a BRIDGE, ends that do not both reference the
dimension or are one column, and a depth that is not a whole number; V603
counts levels only on a path of levels, and `levels` is no longer required.
`sql/expectations.sql` gains three claim shapes and checks with them that no
member is its own parent (`Differs`), that the closure lists every
descendant as its own ancestor (`Reflexive`), and that the depth is zero
exactly there (`Distance`) — the last two about the bridge, which holds
every column they name. A longer cycle needs a recursive query and is not
checked. `claims.implied` now orders claims by the table each is about.
Found on the way: an enum-ranged field of a structured annotation reached
no rule, so `kind: TREE` passed; V007 now reads it against its enum. The
snapshot's `Hierarchy` gains `kind`, `parent`, `bridge`, `ancestor`,
`descendant`, `depth` and `written`; the docs page renders each kind, and
the SQLAlchemy `info` mapping carries `kind` and the fields that kind uses.
The snowflake example's salespeople report up a line held both ways.
*`test_a_tree_and_its_closure_are_checked_against_the_data`*

**Unreleased — every value is a term.** Each value of each of Varda's enums
now has a `meaning` of its own, `varda:<Enum>/<VALUE>` —
`https://w3id.org/varda/ColumnRole/MEASURE` — keyed by the enum as well as
the value, because `SURROGATE_KEY` is a role in one and the key a reference
lands on in another. What an outside vocabulary says about the same thing
moved into the mappings, graded: `MEASURE` is close to `qb:MeasureProperty`,
a semi-additive `MIN` is exactly `qb4o:Min`, and `VERSION_START`, which
until now *meant* `time:hasBeginning`, is related to it — the column is
where the beginning is written down, not the beginning. The profile declares
`qb` and `qb4o` for them. `Vocabulary.meaning` answers Varda's own IRI, and
`Vocabulary.enumeration` reads an enum whole with every IRI expanded, so an
exporter has the terms without knowing the profile's prefixes. The
vocabulary reference states the convention once per enum and lists each
alignment on its value's row. Found on the way: `HierarchyKind` was missing
from the test that holds each enum to its Python mirror, which now fails if
the profile adds an enum the list does not name.
*`test_every_value_of_the_profile_is_a_term_of_its_own`*

### Next, in the order it is worth doing

**Finish the drift gate when something needs it.** `verify` compares text,
which is right while every generator's output is deterministic text. Add
`Artifact(path, compare)` when a generator's output can differ harmlessly —
RDF, where two serializations of one graph are equal — so it can declare how
it compares. And `verify` sees a file left behind only if a generator that
ran declares it. Under a claimed directory that now covers every file, so a
table removed from the model is caught; one left by a generator since
removed still needs a manifest of what the last run wrote, which `write`
could keep and prune against. Neither is worth building before the case
that needs it.

**Make extensions safe to depend on.** `requires_profile` pinning, plus the
conformance kit: `varda ext --check NAME` runs a third-party extension against
a fixture model twice, asserts identical findings and identical artifacts, and
AST-scans for imports of anything outside `varda.ext`. The scan exists in
narrow form as `test_the_worked_example_imports_only_the_interface_module`,
which is worth reading first — it is what settled I12. The question it would
otherwise have had to assume an answer to, whether the model's read API is
public, is settled: it is `varda.snapshot`.

**What the settled interface makes small.** Each follows from it and was
kept out of it so the interface broke once: `varda check --format
json|sarif`, now a formatter over `Report` rather than a parser; exemptions
for one table or column rather than a whole rule; and dialects an extension
registers rather than names.

**Finish the SQLAlchemy generator.** The generator and its equivalence check
have shipped; two things from `design/sqlalchemy-generator.md` have not.
Document what `info` keys a consumer may rely on, so reading them is a
contract rather than a guess. And decide on a constraint naming convention,
which Alembic needs to alter or drop a constraint and which costs the
byte-level agreement between the module and `sql/mart.sql` — then run Alembic
autogenerate against a live database before any documentation says it works.

**More generators on demand.** An ERD is the next most asked for, and is
mechanical against `snapshot.py` — and it is where the relationships go that
writing foreign keys as LinkML references would have put in front of
`gen-erdiagram` with the wrong cardinality; see `docs/design.md`, *Why a
foreign key is an annotation and not a LinkML reference*.

**1.0 — freeze the vocabulary.**
The commitment at 1.0 is that annotations and rule codes do not change
meaning within the major version. Do not reach it before at least two real
organizations have written an extension, because the extension mechanism is
the part most likely to need a breaking change and 1.0 is where that stops
being possible.

Explicitly *not* on the roadmap: a web UI, a metadata server, a query
engine, runtime data validation. Varda describes models and generates from
them. Anything that runs in production alongside the data is a different
product.

## 6. Working on it

```console
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
./run_all.sh
```

`run_all.sh` is the gate. It runs, in order: `ruff check`, `ruff format
--check`, `actionlint`, `mypy --strict`, `pytest`, then `varda check
--strict` and `varda generate` against `examples/retail.yaml`, then
regenerates and diffs the two trees to prove output is deterministic. All
must pass. There is no "warnings are fine" mode.

### Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and every pull
request, in five parallel jobs:

| Job | What it does |
| --- | --- |
| `lint` | `ruff check` and `ruff format --check`, as separate steps so a failure says which |
| `types` | `mypy --strict`. One interpreter only — `python_version` is pinned to 3.11, so a matrix would repeat one answer |
| `test` | `pytest` on 3.11, 3.12, 3.13, 3.14 and 3.15 on Linux, plus 3.12 on macOS and Windows |
| `gate` | `./run_all.sh` — the same script run locally |
| `package` | builds, `twine check --strict`, asserts the profile and `py.typed` are in the wheel, then installs the wheel in a clean venv and runs the CLI from it |

The `gate` job invoking `run_all.sh` rather than restating its steps is
deliberate: CI and the local gate cannot drift apart, and anything added to
one is added to both. `varda verify` went into `run_all.sh` that way, and
CI picked it up with no change to the workflow.

The two extra `test` cells are for path handling, not for language
differences. This package walks parent directories looking for `varda.toml`
and joins POSIX-style artifact paths, so a separator bug is plausible on
Windows in a way a 3.13-only language bug is not.

The `package` job's wheel check earns its place: the profile is data, and
setuptools will happily build a wheel without it if `package-data` is
misconfigured. That failure appears only for someone who `pip install`ed —
every rule fires "unknown annotation" because the vocabulary is not
there — and never for anyone working from a source checkout.

### Documentation

```console
pip install -e ".[docs]"
mkdocs serve          # http://127.0.0.1:8000/varda/
```

`docs/reference/` — vocabulary, rules, command line — is generated from the
package by `scripts/gen_reference.py` and git-ignored. `mkdocs serve` watches
`src/` and regenerates on every rebuild, in a subprocess: an in-process call
would read the already-imported module and silently regenerate stale pages.

The generator is a script rather than a site-generator plugin, so the docs
build unchanged under Material for MkDocs, ProperDocs and Zensical. That is
deliberate — MkDocs 1.x is unmaintained, its announced 2.0 breaks existing
plugins, and Material for MkDocs reaches end of life on 5 November 2026.
`mkdocs` is pinned `<2` so a release cannot break the build unattended.

### House rules

- **80 columns**, enforced. Applies to generated output too — it is read by
  people even though it is written by a machine.
- **Comments argue; docstrings explain why.** A comment restating the code
  is noise. A comment saying why the obvious approach was rejected is the
  most valuable line in the file. Follow the existing density.
- **Ruff deviations are justified in `pyproject.toml`,** each with the
  reason it is specific to this codebase rather than to taste. There are
  four. Adding a fifth needs the same treatment; blanket `noqa` does not.
- **Every rule needs a test that provokes it,** named for the failure it
  catches rather than for the rule number.
- **The example must stay clean.** `examples/retail.yaml` passes with zero
  findings, and there is a test asserting it. An example that does not
  conform is worse than none: it is the first thing anybody copies.

### Definition of done for a new rule

1. Registered with a code in the right family and a title that reads as a
   statement of the property, not of the violation.
2. A message that says what to do, not only what is wrong — `V001` names the
   profile file to declare the annotation in.
3. A test provoking it, and if the rule is a judgment call, a docstring
   explaining the reasoning that `varda rules -v` will print.
4. Severity chosen deliberately: `error` only if it is unambiguously illegal.
   Anything arguable is a `warning`, or it gets switched off wholesale.

## 7. Release

Repository <https://github.com/mluttikh/varda>. PyPI project name `varda`.

The documentation site is GitHub Pages at
<https://mluttikh.github.io/varda/>, built from `docs/` by
`.github/workflows/docs.yml`. `varda.readthedocs.io` and the bare `varda`
GitHub organization are both held by an unrelated bioinformatics project,
which is why neither is the home.

Releases go out through `.github/workflows/release.yml`, triggered by
publishing a GitHub release. It re-runs the whole gate, checks the tag,
the distribution metadata and `__version__` all agree, builds, and
uploads via **PyPI
Trusted Publishing** — no API token is stored in the repository. A token in
repository secrets is a long-lived credential granting upload rights to
anyone who can run a workflow; OIDC issues one scoped to this workflow and
valid for minutes.

The gate runs again at release time rather than trusting an earlier green
CI run, because publishing is the one irreversible action here: a version
cannot be re-uploaded to PyPI once it exists, even after a yank.

`workflow_dispatch` publishes to TestPyPI, which is worth exercising before
the first real upload.

**One-time setup on PyPI**, at
<https://pypi.org/manage/account/publishing/> — add a pending publisher for
project `varda`, owner `mluttikh`, repository `varda`, workflow
`release.yml`, environment `pypi`. Until that exists the release workflow
fails at the upload step, which is the intended behavior rather than a
surprise. Attaching required reviewers to the `pypi` environment in
repository settings puts a human between a merged tag and a permanent
upload.

### Before the first real release

1. **Reserve the PyPI name** with a `0.0.0` placeholder. It is the only part
   of this that somebody else can take while the work is in progress.
2. **Register the w3id namespace** — open a pull request against
   <https://github.com/perma-id/w3id.org> adding `varda/.htaccess`, using the
   file prepared in `w3id/`. Until it merges `https://w3id.org/varda`
   returns 404. Nothing in the package depends on it resolving — the profile
   is read from the installed package, never over the network — so this is
   not a release blocker, but it is what lets somebody follow the IRI to find
   out what `varda:additivity` means.
3. **Set up Trusted Publishing** on PyPI, as above.
