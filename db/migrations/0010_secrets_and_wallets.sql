-- 0010: sekrety aplikacji (klucz Anthropic) i portfele obserwowane
--
-- Sekrety trzymamy w bazie, a nie w zmiennych srodowiskowych, zeby dalo sie je
-- podac i wymienic z interfejsu bez redeployu. Wartosc jest zaszyfrowana tym
-- samym kluczem AES-GCM co klucze gield; API nigdy jej nie zwraca.

CREATE TABLE app.secret (
    name        text        PRIMARY KEY,
    value_enc   bytea       NOT NULL,
    hint        text        NOT NULL,           -- '••••abcd', do pokazania w UI
    description text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  bigint      REFERENCES app.user(id)
);

COMMENT ON TABLE app.secret IS
'Sekrety konfiguracyjne aplikacji. Wartosc wylacznie zaszyfrowana; odczyt tylko
po stronie serwera, nigdy przez API.';

-- Portfele. WYLACZNIE obserwowane - adres albo klucz publiczny (xpub).
--
-- Klucze prywatne i frazy odzyskiwania sa tu SWIADOMIE niemozliwe do zapisania.
-- Aplikacja webowa wystawiona na internet to najgorsze miejsce na klucz dajacy
-- prawo wydania srodkow: wyciek bazy, bledna konfiguracja backupu albo jedna
-- luka w zaleznosci oznaczaja nieodwracalna strate. Do sledzenia salda i wplat
-- wystarczy adres, a systemowi nic wiecej nie jest potrzebne - handel odbywa sie
-- na gieldzie, przez klucz API bez prawa wyplaty.
CREATE TABLE app.wallet (
    id          bigserial   PRIMARY KEY,
    user_id     bigint      NOT NULL REFERENCES app.user(id) ON DELETE CASCADE,
    chain       text        NOT NULL CHECK (chain IN ('bitcoin', 'ethereum')),
    label       text        NOT NULL,
    address     text,                            -- pojedynczy adres
    xpub        text,                            -- klucz publiczny (wiele adresow)
    -- Kolumna istnieje po to, zeby intencja byla jawna w schemacie, a nie tylko
    -- w komentarzu w kodzie. CHECK ponizej nie pozwala jej wylaczyc.
    watch_only  boolean     NOT NULL DEFAULT true,
    balance     numeric(28,12),
    balance_at  timestamptz,
    last_error  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT wallet_is_watch_only CHECK (watch_only),
    CONSTRAINT wallet_needs_identifier CHECK (address IS NOT NULL OR xpub IS NOT NULL),
    UNIQUE (user_id, chain, label)
);

COMMENT ON COLUMN app.wallet.watch_only IS
'Zawsze true, wymuszone CHECK-iem. Klucze prywatne i frazy odzyskiwania nie sa
i nie beda przechowywane w tym systemie.';

-- Historia salda portfela - do wykresu i do liczenia calkowitych aktywow.
CREATE TABLE app.wallet_balance (
    wallet_id  bigint      NOT NULL REFERENCES app.wallet(id) ON DELETE CASCADE,
    event_time timestamptz NOT NULL,
    balance    numeric(28,12) NOT NULL,
    PRIMARY KEY (wallet_id, event_time)
);
