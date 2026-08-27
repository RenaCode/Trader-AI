-- 0007: risk kernel + warstwa aplikacyjna (auth, klucze API, audyt)

-- ---------------------------------------------------------------- ryzyko
-- Limity sa egzekwowane w KODZIE risk kernela na podstawie tych wierszy.
-- Model moze CHCIEC wejsc w pozycje; kernel decyduje czy wolno.
CREATE TABLE risk.limit_def (
    id       serial      PRIMARY KEY,
    engine   ref.engine  NOT NULL,
    mode     ref.trade_mode NOT NULL,
    name     text        NOT NULL,                  -- 'max_daily_loss','max_drawdown','max_position_notional'
    scope    text        NOT NULL DEFAULT 'engine', -- 'engine' | 'instrument'
    instrument_id integer REFERENCES ref.instrument(id),
    value    numeric(28,12) NOT NULL,
    unit     text        NOT NULL,                  -- 'pct','quote_ccy','count'
    action   text        NOT NULL DEFAULT 'block'   -- 'block' | 'downsize' | 'killswitch'
             CHECK (action IN ('block','downsize','killswitch')),
    enabled  boolean     NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (engine, mode, name, scope, instrument_id)
);

CREATE TABLE risk.limit_usage (
    limit_def_id integer     NOT NULL REFERENCES risk.limit_def(id) ON DELETE CASCADE,
    event_time   timestamptz NOT NULL,
    used         numeric(28,12) NOT NULL,
    threshold    numeric(28,12) NOT NULL,
    utilization  double precision NOT NULL,         -- used / threshold
    PRIMARY KEY (limit_def_id, event_time)
);
SELECT create_hypertable('risk.limit_usage', 'event_time', chunk_time_interval => interval '30 days');

CREATE TABLE risk.violation (
    id           bigserial   PRIMARY KEY,
    limit_def_id integer     NOT NULL REFERENCES risk.limit_def(id),
    engine       ref.engine  NOT NULL,
    decision_id  bigint      REFERENCES journal.decision(id),
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    observed     numeric(28,12) NOT NULL,
    threshold    numeric(28,12) NOT NULL,
    action_taken text        NOT NULL,
    detail       jsonb
);

CREATE INDEX violation_time_idx ON risk.violation (engine, occurred_at DESC);

-- Kill-switch wymaga RECZNEGO odblokowania. Celowo — automatyczne wznowienie
-- po drawdownie to najprostsza droga do powtorzenia tej samej straty.
CREATE TABLE risk.killswitch_event (
    id           bigserial   PRIMARY KEY,
    engine       ref.engine  NOT NULL,
    mode         ref.trade_mode NOT NULL,
    triggered_at timestamptz NOT NULL DEFAULT now(),
    reason       text        NOT NULL,
    violation_id bigint      REFERENCES risk.violation(id),
    released_at  timestamptz,
    released_by  bigint,                            -- FK -> app.user, dodany nizej
    release_note text
);

-- Heartbeat silnikow — dead-man switch. Brak sygnalu => wstrzymanie i alert.
CREATE TABLE risk.engine_heartbeat (
    engine        ref.engine  PRIMARY KEY,
    mode          ref.trade_mode NOT NULL,
    last_beat_at  timestamptz NOT NULL,
    status        text        NOT NULL,             -- 'running','paused','halted'
    detail        jsonb
);

-- ---------------------------------------------------------------- aplikacja
CREATE TABLE app.user (
    id            bigserial   PRIMARY KEY,
    email         citext,
    login         text        NOT NULL UNIQUE,
    password_hash text        NOT NULL,             -- argon2id
    role          text        NOT NULL DEFAULT 'owner' CHECK (role IN ('owner','viewer')),
    is_active     boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz,
    failed_logins integer     NOT NULL DEFAULT 0,
    locked_until  timestamptz
);

ALTER TABLE risk.killswitch_event
    ADD CONSTRAINT killswitch_released_by_fk FOREIGN KEY (released_by) REFERENCES app.user(id);

-- TOTP (RFC 6238). Sekret NIGDY plaintext — szyfrowany kluczem z k8s Secret.
CREATE TABLE app.totp_secret (
    user_id      bigint      PRIMARY KEY REFERENCES app.user(id) ON DELETE CASCADE,
    secret_enc   bytea       NOT NULL,
    confirmed_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.backup_code (
    id        bigserial   PRIMARY KEY,
    user_id   bigint      NOT NULL REFERENCES app.user(id) ON DELETE CASCADE,
    code_hash text        NOT NULL,
    used_at   timestamptz
);

-- Miejsce na WebAuthn/passkey — mocniejszy standard niz TOTP, planowany przed live.
CREATE TABLE app.webauthn_credential (
    id             bigserial   PRIMARY KEY,
    user_id        bigint      NOT NULL REFERENCES app.user(id) ON DELETE CASCADE,
    credential_id  bytea       NOT NULL UNIQUE,
    public_key     bytea       NOT NULL,
    sign_count     bigint      NOT NULL DEFAULT 0,
    label          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_used_at   timestamptz
);

CREATE TABLE app.session (
    id             bigserial   PRIMARY KEY,
    user_id        bigint      NOT NULL REFERENCES app.user(id) ON DELETE CASCADE,
    token_hash     bytea       NOT NULL UNIQUE,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL,
    revoked_at     timestamptz,
    mfa_satisfied  boolean     NOT NULL DEFAULT false,
    ip             inet,
    user_agent     text
);

CREATE INDEX session_active_idx ON app.session (user_id)
    WHERE revoked_at IS NULL;

-- Klucze API do gield. Wylacznie zaszyfrowane; API nigdy nie zwraca odszyfrowanej
-- wartosci, tylko key_hint (4 ostatnie znaki). Klucz live docelowo BEZ prawa wyplaty
-- i z whitelista IP VPS-a.
CREATE TABLE app.api_credential (
    id             bigserial   PRIMARY KEY,
    user_id        bigint      NOT NULL REFERENCES app.user(id),
    venue_id       smallint    NOT NULL REFERENCES ref.venue(id),
    mode           ref.trade_mode NOT NULL,
    label          text        NOT NULL,
    key_enc        bytea       NOT NULL,
    secret_enc     bytea       NOT NULL,
    passphrase_enc bytea,
    key_hint       text        NOT NULL,            -- '••••1234'
    permissions    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    can_withdraw   boolean     NOT NULL DEFAULT false,
    active         boolean     NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_used_at   timestamptz,
    last_error     text,
    UNIQUE (venue_id, mode, label)
);

-- Zabezpieczenie schematem: klucz z prawem wyplaty nie moze byc aktywny.
ALTER TABLE app.api_credential
    ADD CONSTRAINT api_credential_no_active_withdraw
    CHECK (NOT (active AND can_withdraw));

CREATE TABLE app.audit_log (
    id           bigserial   PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    actor_user_id bigint     REFERENCES app.user(id),
    action       text        NOT NULL,              -- 'credential.create','mode.switch','killswitch.release'
    entity       text,
    entity_id    text,
    before       jsonb,
    after        jsonb,
    ip           inet,
    user_agent   text
);

CREATE INDEX audit_log_at_idx     ON app.audit_log (at DESC);
CREATE INDEX audit_log_action_idx ON app.audit_log (action, at DESC);
