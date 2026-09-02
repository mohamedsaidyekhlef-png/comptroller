# Comptroller - design contract

Read this before changing anything. These decisions are load-bearing.

## Non-negotiables

1. **No LLM in the authorization path.** Enforcement is declarative and
   deterministic. Any model-driven component is read-only and advisory, and
   may only *propose* policy diffs for a human to merge.
2. **Identity comes from credentials, never payloads.** `Principal` is
   constructed only via `from_credential`. A CI test greps src/ to enforce
   this. An injected agent must not be able to name itself.
3. **No floats.** Integer micros (10^-6 currency units) everywhere.
   Cross-currency arithmetic raises.
4. **DENY and ESCALATE are return values, not exceptions.** Only engine
   faults raise. In STRICT mode the caller treats a fault as DENY.
5. **Policy rules can only DENY or ESCALATE.** ALLOW is the absence of a
   rule. There is no priority field; evaluation order is fixed in code:
   validate, resolve principal, verify mandate, DENY rules, per-intent
   ceiling, hierarchical reserve, ESCALATE rules, allow.
6. **P2 is a database CHECK constraint.** Application bugs fail closed.
7. **Overage is a first-class column, outside the P2 constraint.** When
   actual cost exceeds the hold: spent += LEAST(actual, held) and
   overage += GREATEST(0, actual - held). This keeps P2 exactly true and
   makes estimation error alarmable. Realized cost is spent + overage.
8. **STRICT is the only mode with guarantees.** AVAILABLE mode is a named,
   documented degradation, and PAYMENT may never be listed in it.

## Frozen surface (changing these is a rewrite)

Money, Principal, Intent, Estimate, the Decision union
(Allowed | Denied | Escalated), the authorize/capture/release signatures,
the reason-code enum, and the two table CHECK constraints.

## Deliberately loose (additive)

Predicate vocabulary, pricebook format, window kinds, adapters, reporting.

## Concurrency rules

- Reserve with a conditional atomic UPDATE whose predicate references only
  the row being updated. READ COMMITTED suffices: Postgres re-evaluates the
  WHERE clause after a lock wait, so there is no lost-update window.
  SERIALIZABLE would only be needed if the predicate spanned other rows.
- Acquire budget rows in canonical sorted (scope_path, window_kind,
  window_key) order to *prevent* deadlock rather than detect it.
- One transaction per authorize; any single window failing rolls back all,
  so hierarchical reservation is atomic.
- intent_id is UNIQUE: idempotency is enforced by the database.
- Store debited_windows on the hold so release unwinds exactly what was
  debited, even if the policy version changed since.
- SET LOCAL lock_timeout bounds tail latency; a timeout is a fault.

## Build order

1. Core types (Money, Principal). <- done
2. Intent, Estimate, Decision union, reason codes.
3. Postgres migration with p2_budget_safety CHECK; reservation transaction.
4. Hold state machine: capture, release, expiry reaper.
5. Policy engine: JSON Schema, compiler, fixed-order evaluator.
6. Hash-chained ledger and verify-chain CLI.
7. OpenAI/Anthropic proxy (streaming + cache tokens), MCP interceptor.
8. Mandates: Ed25519, intent binding, jti burn.

Then: property state machine (P1/P2/P3 under crash and interleaving), the
prompt-injection demo, and the 100-agents-on-$10 demo. Both demos run in CI
and generate the numbers printed in the README.