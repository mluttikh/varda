# Concepts

Varda models the parts of dimensional design that a general modeling language
has no words for. LinkML already knows about classes, slots, ranges,
cardinality, enumerations, inheritance and imports. It does not know about
facts, dimensions, grain, additivity or slowly-changing dimensions, because
those are dimensional-modeling concepts rather than general modeling
concepts.

## Tables have a role

Every annotated class declares [`varda:role`](reference/vocabulary.md):
`fact`, `dimension` or `bridge`. Nothing is inferred from the class name — a
table named `FctSale` is a fact because it says so, which is what lets one
organization write `Fct`, another `Fact`, and a third nothing at all.

**Facts** are measurement events. They have a declared grain, foreign keys to
dimensions, and zero or more measures. Nothing points at them.

**Dimensions** are descriptive context. One surrogate key, at least one
natural key, and attributes that are almost always textual or categorical.

**Bridges** resolve a many-to-many — a customer in three segments — and
usually carry an allocation factor, because a customer counted once per
segment is a customer counted three times.

## Grain is a column set and a sentence

`varda:grain` names the columns at which rows of a fact are unique, and
`varda:grain_statement` says the same thing in words — conventionally "one row
per …". Both are required on a fact, and each checks the other.

The columns are the grain proper: in every formal treatment of dimensional
modeling the grain *is* the set that identifies one row, which is why the
annotation named after the concept holds it. Declaring it makes the claim
testable. [`V114`](reference/rules.md#v114) checks the columns exist,
[`V115`](reference/rules.md#v115) checks they are foreign keys or degenerate
dimensions — a grain is what a row *is*, not what it records — and the SQL
generator turns the set into a `UNIQUE` constraint the database enforces.

The sentence is not redundant. It carries the intent a column list cannot:
*why* those columns, and what a row means to somebody reading the model rather
than querying it. A grain that cannot be stated in one sentence is a sign the
table is doing two jobs, and writing it is most of the value.
[`V104`](reference/rules.md#v104) flags a statement shorter than four words —
it cannot tell a good sentence from a bad one, but it catches
`grain_statement: daily`, which is the form the failure usually takes.

Nothing checks the sentence *against* the columns, and that is deliberate.

Such a rule would have to read English prose, and it would be right only for
the phrasings it recognised: silent on `each row is one shipment leg`, and
wrong about `one row per order line` over a grain of order number and line
number, which is how the sentence is normally written. A check that fires for
some phrasings and not others is unreliable rather than weak, and an
unreliable check is worse than none — it invites you to stop looking while
giving you nothing to lean on.

The failure such a rule would reach for is caught exactly, further down. A
grain missing a column becomes a `UNIQUE` constraint that fails on load:
language-independent, and impossible to write around.

So the division is clean. The columns are checked, by rules that are
deterministic about when they fire. The sentence is checked only for being a
sentence, and is otherwise yours — it exists to carry intent to a person, and
a grain statement written to satisfy a linter has already lost the thing it
was for.

## Columns have a role too

`varda:role` on a slot turns a flat list of columns into a structure that
generators and validators can reason about: `surrogate_key`, `natural_key`,
`foreign_key`, `measure`, `attribute`, `degenerate_dimension`.

The **natural key** is what a loader matches on — what makes two source rows
the same business entity. Without one, every load either creates duplicates or
has the matching rule written somewhere the model cannot see. The
**surrogate key** is the meaningless integer the facts actually join to.

A **degenerate dimension** is an identifier living on the fact with no
dimension table of its own — an order number, a ticket reference. It groups
the lines of one transaction and has no attributes worth a table.

## Additivity is the expensive one

Every measure declares `varda:additivity`.

| | |
| --- | --- |
| `additive` | Sums across every dimension. Sales amount, quantity, cost. |
| `semi_additive` | Sums across some dimensions but not others — almost always not across time. Account balance, inventory level, headcount. |
| `non_additive` | Never sum. Ratios, percentages, unit prices, temperatures. Aggregate the components and recompute. |

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

## History is a property of the dimension

`varda:scd` declares what happens when a source attribute changes: `type_0`
retain original, `type_1` overwrite, `type_2` add a row.

It sits on the table rather than per column, deliberately. A mixed dimension
— some attributes overwritten, others versioned — is a genuinely harder object
that this core does not model. Split it into two dimensions, or handle it in
an extension.

[`V113`](reference/rules.md#v113) is a warning rather than an error, because a
great many dimensions are genuinely type 1 and saying so feels like ceremony.
It stays a rule because "we never decided" and "we decided overwrite" look
identical in the model and cost very differently two years later.

## What Varda does not model

Data quality — whether the *rows* are right — is a different layer with a
different audience. Varda's rules answer "is this a legal dimensional model?",
never "is this data correct" and never "is this a good model". The last is a
review, and a rule that produces an argument gets switched off.
