# Concepts

Varda models the parts of dimensional design that a general modeling language
has no words for. LinkML already knows about classes, slots, ranges,
cardinality, enumerations, inheritance and imports. It does not know about
facts, dimensions, grain, additivity or slowly-changing dimensions, because
those are dimensional-modeling concepts rather than general modeling
concepts.

Three of them carry most of the weight: what one row of a fact *is*, how a
measure may be aggregated, and what happens when a dimension value changes.
Everything below builds to those.

## Tables have a role

Every annotated class declares [`varda:role`](reference/vocabulary.md):
`FACT`, `DIMENSION` or `BRIDGE`. Nothing is inferred from the class name — a
table named `FctSale` is a fact because it says so, which is what lets one
organization write `Fct`, another `Fact`, and a third nothing at all.

**Facts** are measurement events. They have a declared grain, foreign keys to
dimensions, and zero or more measures. Nothing points at them.

**Dimensions** are descriptive context. One surrogate key, at least one
natural key, and attributes that are almost always textual or categorical.

**Bridges** resolve a many-to-many — a customer in three segments — and
usually carry an allocation factor, because a customer counted once per
segment is a customer counted three times.

## Columns have a role too

`varda:role` on a slot turns a flat list of columns into a structure that
generators and validators can reason about.

| Role | On | What it is |
| --- | --- | --- |
| `SURROGATE_KEY` | dimension | The meaningless key facts join to. Exactly one. |
| `NATURAL_KEY` | dimension | The business identity a loader matches on. |
| `FOREIGN_KEY` | fact, bridge, dimension | A reference to another table's surrogate key. |
| `DEGENERATE_DIMENSION` | fact | An identifier with no dimension table of its own. |
| `MEASURE` | fact, bridge | A numeric quantity that is aggregated. |
| `ATTRIBUTE` | any | Descriptive context, grouped and filtered on. |
| `VERSION_START`, `VERSION_END`, `IS_CURRENT`, `VERSION_NUMBER` | type-2 dimension | What marks one version of a row off from another. |

The **natural key** is what a loader matches on — what makes two source rows
the same business entity. Without one, every load either creates duplicates or
has the matching rule written somewhere the model cannot see. The
**surrogate key** is the meaningless integer the facts actually join to.

A **degenerate dimension** is an identifier living on the fact with no
dimension table of its own — an order number, a ticket reference. It groups
the lines of one transaction and has no attributes worth a table.

The versioning roles belong to [history](#history-what-happens-when-a-value-changes),
below.

## Grain: what one row is

A fact declares its grain twice, in two forms that do different jobs.

`varda:grain` names the columns at which rows are unique — the set that
together identifies exactly one row. This is the grain proper: in every formal
treatment of dimensional modeling the grain *is* that set. Stating it makes
the claim testable. [`V114`](reference/rules.md#v114) checks the columns
exist, [`V115`](reference/rules.md#v115) checks each is a foreign key or a
degenerate dimension — a grain is what a row *is*, not what it records — and
the SQL generator turns the set into a `UNIQUE` constraint the database
enforces.

`varda:grain_statement` says the same thing in words, conventionally "one row
per …". It carries what a column list cannot: *why* those columns, and what a
row means to somebody reading the model rather than querying it. A grain that
cannot be stated in one sentence is a sign the table is doing two jobs, and
writing it is most of the value. [`V104`](reference/rules.md#v104) checks it
is a sentence and nothing further.

Nothing compares the sentence to the columns, deliberately — see
[Design notes](design.md#why-the-grain-sentence-is-not-checked-against-the-columns).

## Additivity is the expensive one

Every measure declares `varda:additivity`.

| | |
| --- | --- |
| `ADDITIVE` | Sums across every dimension. Sales amount, quantity, cost. |
| `SEMI_ADDITIVE` | Sums across some dimensions but not others — almost always not across time. Account balance, inventory level, headcount. |
| `NON_ADDITIVE` | Never sum. Ratios, percentages, unit prices, temperatures. Aggregate the components and recompute. |

A semi-additive measure must also declare `varda:semi_additive_over`, naming
the foreign key it may not be summed across, and
[`V203`](reference/rules.md#v203) checks that column actually exists on the
table. A constraint naming a dimension the fact does not have is a constraint
that silently never applies, and it looks exactly like one that does.

!!! warning "Why this family is `error` and not `warning`"
    Summing something that must not be summed produces a number that looks
    fine. Nobody's query breaks, no test fails, and the wrong figure reaches
    someone who acts on it. This is the one class of modeling error that is
    both easy to make and invisible afterwards.

## History: what happens when a value changes

`varda:scd` declares how a dimension responds when a source attribute
changes: `TYPE_0` retain the original, `TYPE_1` overwrite it, `TYPE_2` add a
row.

It sits on the table rather than per column, deliberately. A mixed dimension
— some attributes overwritten, others versioned — is a genuinely harder object
that this core does not model. Split it into two dimensions, or handle it in
an extension.

[`V113`](reference/rules.md#v113) is a warning rather than an error, because a
great many dimensions are genuinely type 1 and saying so feels like ceremony.
It stays a rule because "we never decided" and "we decided overwrite" look
identical in the model and cost very differently two years later.

### How a type-2 dimension versions

Type 2 keeps a row per change. It does not say how the current row is found,
because the field does not agree on that, and three mechanisms are all in
normal use:

| Strategy | Columns | The current row is |
| --- | --- | --- |
| window | `VERSION_START` + `VERSION_END` | the one whose end is null or far-future |
| flagged | `VERSION_START` + `IS_CURRENT` | the flagged one; the end derived from the next row |
| counter | `VERSION_NUMBER` | the highest per natural key |

Varda accepts all three, and insists only that *one* of them be marked —
because a dimension versioning by an undeclared mechanism cannot have its
uniqueness generated. That constraint is the return on saying so: a type-2
dimension is unique on its natural key **plus** the discriminator, never the
natural key alone, which would reject the second version of every row.

The period follows SQL:2011 — closed at the start, open at the end. A row is
in force from `VERSION_START` up to but not including `VERSION_END`, which is
what stops consecutive versions overlapping at their boundary.

!!! note "Version time is usually not business time"
    `VERSION_START` is named for the version rather than for validity because
    in practice it records when the warehouse *noticed* a change, not when the
    change was true. dbt's `dbt_valid_from` and Data Vault's `LOAD_DATE` are
    both this, whatever they are called. Keeping both would be bitemporality,
    which this core does not model.

## When one thing has two identities

`varda:role` says what part a column plays. LinkML's own `unique_keys` says
which combinations are unique. They are different claims — a surrogate key is
unique and is not a business key, and `valid_from` belongs in a type-2
dimension's unique key while being a version marker rather than an identity —
so Varda reads both.

Most dimensions need only the roles. Varda derives the constraint: a type-2
dimension is unique on its natural key plus whatever marks versions apart.

That derivation assumes one natural key. A dimension loaded from several
sources often has more than one, because each source identifies the thing its
own way:

```yaml
DimProduct:
  annotations:
    varda:role: DIMENSION
    varda:scd: TYPE_2
  unique_keys:
    by_barcode:
      description: Sources that supply a barcode.
      unique_key_slots: [gtin, valid_from]
    by_supplier_part:
      description: Sources identifying an item by supplier and part number.
      unique_key_slots: [supplier_code, supplier_part_number, valid_from]
```

Two keys are two constraints, and that is the whole point. Merging them into
one is weaker than either alone, and inert as well: a row whose `gtin` is null
passes a merged key without being checked at all. Kept apart, each row is
caught by whichever key its own source populated.

Declaring them replaces the derived constraint rather than adding to it, so a
table states its uniqueness in one place or the other.
[`V128`](reference/rules.md#v128) checks the columns exist, because LinkML
accepts a key over a misspelled slot without complaint.
[`V129`](reference/rules.md#v129) checks a business key on a type-2 dimension
carries a version marker — without one it claims the business key does not
repeat, which on a type-2 table is false.
[`V130`](reference/rules.md#v130) warns about a natural key none of them
cover.

!!! note "Two sources, one thing, two rows"
    A unique key stops one source loading the same product twice. Nothing
    structural can tell you that a row from one source and a row from another
    are the *same* product — that is a matching decision the ETL makes, and
    Varda has no column for where it landed.

## Hierarchies: how a dimension is drilled

`varda:hierarchies` declares the named paths a reader drills down. Levels run
from least to most granular, which is the direction every system that models
hierarchies uses:

```yaml
DimStore:
  annotations:
    varda:role: DIMENSION
    varda:hierarchies:
      - name: geography
        levels: [country, region, store_code]
```

When the coarser levels are their own tables — a snowflake — the only
geography columns on `DimCity` are the keys, and a path of keys reads as
integers. Reach through the key instead:

```yaml
DimCity:
  annotations:
    varda:role: DIMENSION
    varda:hierarchies:
      - name: geography
        levels: [country_key.country_name, state_key.state_name, city_name]
```

Both halves are checked: the near name must be a foreign key of this table,
and the far one a column of the dimension it points at. A level that names a
foreign key without reaching through it is an error, because `4718` is not
something anyone drills into.

A hierarchy makes two claims, and only one of them is checkable. The path —
offer country, then region, then store — is a declaration, and Varda checks
that the levels are real columns, distinct, at least two of them, on a
dimension, and the kind of column that can be a level. The other claim is that
each level rolls up into exactly one member of the level above it, and that is
a statement about data. Nothing here can test it, the same way nothing tests
the grain sentence.

Three things go wrong often enough to be worth naming.

A level answers two questions. What a member is *called*, and which member
it *is*. Usually one column does both. When it does not — `city_name` holds
"Springfield" for cities in three states — nothing extra is needed, because
the hierarchy has already said what tells them apart:

```yaml
levels: [country_name, state_name, city_name]
```

A level's identity is its own key preceded by the key of every coarser level,
so `city_name` is identified by country, state and city together. Varda
derives that from the order; it is never written out.

Declare a `key` only when what names a level is not what tells its members
apart — a level showing `product_name` where two products share a name:

```yaml
levels:
  - brand
  - {column: product_name, key: sku}
```

The key defaults to the foreign key for a level reached through one and to
the naming column otherwise. A surrogate key or a foreign key may be a `key`
and never a `column`, which is one rule from both sides: they identify well
and read badly.

[`V127`](reference/rules.md#v127) checks a declared key exists and is the
kind of column that can identify something. Whether members are actually
distinct is a claim about data, and nothing checks it.

**Weeks do not roll up into months.** `[year, quarter, month, week, date]`
looks like the obvious calendar hierarchy and is wrong: a week straddles month
boundaries and, under ISO 8601, year boundaries. Real calendars carry two
paths that share their finest level and diverge in the middle, which is why a
column may appear in more than one hierarchy:

```yaml
    varda:hierarchies:
      - name: calendar
        description: How the business reports externally.
        levels: [calendar_year, calendar_month, calendar_date]
      - name: iso_week
        description: How operations plans its weeks.
        levels: [iso_year, iso_week, calendar_date]
```

A `description` is optional and earns its place exactly here: when a
dimension carries several paths, the names alone rarely say which one a
reader wants.

**A hierarchy in a type-1 dimension rewrites history.** Districts get redrawn
and products get recategorized, and when the dimension overwrites, last year's
numbers move into this year's regions. That is often what people want; it is
never what they expect. A dimension whose hierarchy has to stay put under
historical reporting is type 2, and the reasoning is the same one in
[History](#history-what-happens-when-a-value-changes) — the choice is cheap to
make now and expensive to discover later.

Only fixed-depth paths are modeled. A hierarchy whose branches end at
different depths, whose levels can be skipped, or where a child has more than
one parent is not a list of columns — it is a bridge table, and the many-to-many
it resolves is what [`BRIDGE`](#tables-have-a-role) is for.

## What Varda does not model

Data quality — whether the *rows* are right — is a different layer with a
different audience. Varda's rules answer "is this a legal dimensional model?",
never "is this data correct" and never "is this a good model". The last is a
review, and a rule that produces an argument gets switched off.
