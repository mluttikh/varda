# w3id registration

`.htaccess` in this directory is the redirect configuration for
**`https://w3id.org/varda`** — the permanent IRI the profile uses as its
schema `id`, and the namespace every `varda:` annotation expands into.

## Why w3id and not a domain we own

The IRI is copied into the `prefixes:` block of every model anyone writes,
and into every RDF graph generated from one. It can therefore never change.

A domain can lapse, be sold, or outlive the organization that registered it,
and a lapsed domain baked into published schemas is unrecoverable. w3id.org —
run by the W3C Permanent Identifier Community Group, ~995 namespaces — splits
the two concerns: the identifier is permanent, the redirect target is a
one-line change in this file.

LinkML itself works this way: `https://w3id.org/linkml/` is a 302 into GitHub
Pages. Using the same mechanism is also what the audience expects.

This is not an alternative to owning `varda-project.org`. If that domain is
ever registered, it becomes the *redirect target* here — published schemas
keep resolving and nothing downstream changes.

## Registering it

1. Fork <https://github.com/perma-id/w3id.org>.
2. Copy this `.htaccess` to a new top-level directory `varda/`.
3. Open a pull request. The CONTRIBUTING notes ask for a real contact and a
   short statement of what the namespace is for.

Until the PR merges, `https://w3id.org/varda` returns 404. Nothing in the
package depends on it resolving — `varda check` and `varda generate` read the
profile from the installed package, never over the network — so this is not a
release blocker. It matters for anyone consuming the generated RDF or
following the IRI to find out what `varda:additivity` means.

## Updating the target later

Change the target in `.htaccess` and open another PR. The identifier does not
move, so no published schema is affected.
