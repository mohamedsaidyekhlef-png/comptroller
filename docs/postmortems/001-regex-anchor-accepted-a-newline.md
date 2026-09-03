# 001: the identity validator accepted a trailing newline

Status: fixed in commit 1
Found by: a unit test written before the bug was suspected
Severity if shipped: high, silent, and permanent

## What happened

`Principal` validated each identity segment with:

    ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$

In Python, `$` matches at the end of the string *or* immediately before a
trailing newline. So `"acme\n"` passed validation. The test
`test_path_traversal_and_injection_rejected` asserted that it should not, and
failed with `DID NOT RAISE IdentityError`.

The fix was two characters: `\A` and `\Z`, which match only the absolute start
and end of the string.

## Why it mattered

`Principal.scope_path` is not decoration. It is the primary key of every
budget window and the scope written on every ledger row. A tenant named
`"acme\n"` would have produced a scope path containing a newline, which means:

- a second budget window for what a human reads as the same tenant, so a cap
  configured for `tenant/acme` would not apply to it, and spend would be
  counted against a cap of zero rather than denied;
- ledger rows that break any line-oriented reader, with an attacker-chosen
  suffix able to imitate a following row;
- a corrupt primary key that cannot be repaired later without rewriting
  history, since the ledger is append-only.

The damage would have been invisible until an audit, which is the worst
property a data bug can have.

## Class of bug

Trusting a validator's *intent* instead of its *semantics*. `^...$` reads as
"the whole string" to almost everyone; it does not mean that. The same trap
exists in `re.match` without `fullmatch`, in `str.strip` assumptions, and in
any regex ported from a language whose anchors behave differently.

## What changed beyond the fix

The injection cases now include `\r`, `\t`, a null byte, and a leading
newline, not just the path traversal string that motivated the test. The
original test only tried `../admin`, which the old regex already rejected;
the newline case was added on the suspicion that the anchors were wrong, and
that suspicion was correct.

## What I would do differently

Write the adversarial cases first, before the validator, and include the
boring control characters every time. The interesting attack string is not
usually the one that gets through.