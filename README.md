# Comptroller

**Deterministic economic authorization layer for autonomous AI agents.**

Not cost monitoring. Not another LLM proxy. Comptroller answers one question
*before* an agent acts: is this principal permitted to incur this cost, now?

    Intent -> Identity -> Policy -> Atomic reservation -> ALLOW / DENY / ESCALATE
           -> Execute -> Capture / Release -> Tamper-evident audit

Existing AI gateways meter spend *after* the provider responds, so the request
that crosses the budget still executes. Comptroller reserves funds before the
call is made, across a hierarchy of budget scopes, atomically, and governs LLM
inference, tool calls, MCP purchases and real payments through one primitive.

## Guaranteed properties

For every action that passes an enforcement point, in `STRICT` mode:

- **P1 Authorization precedence** - every `CAPTURE` has a prior `ALLOW`
  authorization for that same hold, and at most one capture per hold, ever.
- **P2 Budget safety** - for every scope and window,
  `spent + reserved <= cap` at every instant, under arbitrary interleaving.
  Enforced by a database CHECK constraint, not application logic.
- **P3 Conservation** - every hold reaches exactly one terminal state and
  `captured + released <= held`.

**Scope limit, stated up front:** these hold for *mediated* actions. An agent
with direct network egress and its own provider key is outside the guarantee.
That case is *detected* at reconciliation, not prevented; prevention needs
egress policy Comptroller does not own.

## Status

Under construction, in public. Commit 1 of ~8: frozen core types.

Roadmap: atomic hierarchical reservation on Postgres, deterministic policy
engine, OpenAI/Anthropic-compatible proxy, MCP tool authorization, signed
human approval mandates, then a read-only reporting layer.