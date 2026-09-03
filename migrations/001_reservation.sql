-- Commit 3: the reservation engine.
--
-- P2 lives in this file, not in application code. The CHECK on budget_window
-- is the only thing standing between a race and an overspend, so it must be
-- impossible to deploy the schema without it. Application logic can be
-- bypassed by a bug; a CHECK constraint cannot be bypassed by anything short
-- of an ALTER TABLE.

CREATE TYPE window_kind AS ENUM ('DAY', 'LIFETIME');
CREATE TYPE hold_state AS ENUM ('PENDING', 'CAPTURED', 'RELEASED', 'EXPIRED');

-- What an operator configures. Caps live here.
CREATE TABLE budget_policy (
    scope_path  text        NOT NULL,
    window_kind window_kind NOT NULL,
    cap_micros  bigint      NOT NULL CHECK (cap_micros >= 0),
    currency    char(3)     NOT NULL,
    PRIMARY KEY (scope_path, window_kind)
);

-- What is actually being counted, materialised per window. A DAY window is
-- keyed by date_trunc('day', now()) evaluated on the server, so an agent
-- cannot roll itself into a fresh window by lying about the time.
CREATE TABLE budget_window (
    scope_path      text        NOT NULL,
    window_kind     window_kind NOT NULL,
    window_start    timestamptz NOT NULL,
    cap_micros      bigint      NOT NULL CHECK (cap_micros >= 0),
    currency        char(3)     NOT NULL,
    spent_micros    bigint      NOT NULL DEFAULT 0 CHECK (spent_micros >= 0),
    reserved_micros bigint      NOT NULL DEFAULT 0 CHECK (reserved_micros >= 0),
    overage_micros  bigint      NOT NULL DEFAULT 0 CHECK (overage_micros >= 0),
    PRIMARY KEY (scope_path, window_kind, window_start),

    -- P2, budget safety.
    CONSTRAINT p2_budget_safety
        CHECK (spent_micros + reserved_micros <= cap_micros)
);

CREATE TABLE hold (
    hold_id         uuid        PRIMARY KEY,
    intent_id       uuid        NOT NULL UNIQUE,   -- makes authorize idempotent
    scope_path      text        NOT NULL,
    fingerprint     char(64)    NOT NULL,
    kind            text        NOT NULL,
    currency        char(3)     NOT NULL,
    held_micros     bigint      NOT NULL CHECK (held_micros >= 0),
    captured_micros bigint      CHECK (captured_micros IS NULL OR captured_micros >= 0),
    overage_micros  bigint      CHECK (overage_micros IS NULL OR overage_micros >= 0),
    state           hold_state  NOT NULL,
    policy_version  text        NOT NULL,
    debited         jsonb       NOT NULL,          -- exact windows to unwind
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    resolved_at     timestamptz,

    CONSTRAINT expiry_after_creation CHECK (expires_at > created_at),

    -- P3, conservation. One resolution per hold, and the columns that describe
    -- that resolution must agree with the state naming it.
    CONSTRAINT p3_state_consistency CHECK (
        (state = 'PENDING'  AND captured_micros IS NULL     AND resolved_at IS NULL)
     OR (state = 'EXPIRED'  AND captured_micros IS NULL     AND resolved_at IS NOT NULL)
     OR (state = 'RELEASED' AND captured_micros IS NULL     AND resolved_at IS NOT NULL)
     OR (state = 'CAPTURED' AND captured_micros IS NOT NULL AND resolved_at IS NOT NULL)
    )
);

CREATE INDEX hold_reaper ON hold (expires_at) WHERE state = 'PENDING';
CREATE INDEX hold_fingerprint ON hold (scope_path, fingerprint, created_at DESC);

-- Append-only. The hash chain arrives in a later commit; the sequence is here
-- now because P1 is stated in terms of ledger order.
CREATE TABLE ledger (
    seq            bigserial   PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    entry_type     text        NOT NULL CHECK (entry_type IN
                       ('AUTHORIZE','CAPTURE','RELEASE','EXPIRE','DENY','BUDGET_BREACH')),
    hold_id        uuid,
    intent_id      uuid,
    scope_path     text        NOT NULL,
    fingerprint    char(64),
    amount_micros  bigint,
    currency       char(3),
    reason_code    text,
    detail         text,
    policy_version text        NOT NULL
);

CREATE INDEX ledger_hold ON ledger (hold_id, seq);