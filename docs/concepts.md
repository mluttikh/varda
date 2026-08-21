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

## Grain is a sentence

`varda:grain` states what exactly one row represents. Conventionally "one row
per …".

This is the most important sentence in a model, and writing it is most of the
value. A grain that cannot be stated in one sentence is a sign the table is
doing two jobs. Rule [`V104`](reference/rules.md#v104) flags a grain shorter
than four words — it cannot tell a good grain from a bad one, and pretending
otherwise would make it an argument rather than a check, but it does catch
`grain: daily`, which is the form the failure almost always takes.

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
