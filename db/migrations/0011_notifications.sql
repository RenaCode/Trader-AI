-- 0011: powiadomienia i ustawienia konta
--
-- Zasady wziete z praktyki platform tradingowych:
--   - pilne alerty (kill-switch, naruszenie limitu, martwy silnik) MUSZA dochodzic
--     zawsze; reszta jest konfigurowalna
--   - kazdy typ zdarzenia ma wlasny czas wyciszenia, bo inaczej jedna awaria
--     generuje setki powiadomien i uzytkownik przestaje je czytac
--   - godziny ciszy nie moga tlumic alertow krytycznych

CREATE SCHEMA IF NOT EXISTS notify;

CREATE TYPE notify.severity AS ENUM ('info', 'warning', 'critical');
CREATE TYPE notify.channel_kind AS ENUM ('inapp', 'telegram', 'webhook');

-- Kanaly dostarczania. Konfiguracja (token bota, URL) jest zaszyfrowana,
-- bo token Telegrama pozwala pisac w imieniu bota do kazdego jego czatu.
CREATE TABLE notify.channel (
    id          bigserial   PRIMARY KEY,
    user_id     bigint      NOT NULL REFERENCES app.user(id) ON DELETE CASCADE,
    kind        notify.channel_kind NOT NULL,
    label       text        NOT NULL,
    config_enc  bytea,                          -- JSON: {bot_token, chat_id} | {url, secret}
    hint        text,                           -- '••••1234' albo host webhooka
    enabled     boolean     NOT NULL DEFAULT true,
    verified_at timestamptz,                    -- kanal potwierdzony wiadomoscia testowa
    last_error  text,
    last_sent_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, kind, label)
);

-- Katalog typow zdarzen. Trzymany w bazie, a nie jako enum, zeby dolozenie
-- nowego typu nie wymagalo migracji - i zeby UI mial skad wziac opisy.
CREATE TABLE notify.event_type (
    code             text    PRIMARY KEY,
    label            text    NOT NULL,
    description      text    NOT NULL,
    category         text    NOT NULL,          -- 'ryzyko','handel','uczenie','konto','system'
    default_severity notify.severity NOT NULL,
    -- Alertu krytycznego nie da sie wylaczyc ani wyciszyc. Kill-switch, ktory
    -- zadzialal w ciszy, jest gorszy niz brak kill-switcha - bo daje zludzenie
    -- kontroli.
    always_on        boolean NOT NULL DEFAULT false,
    default_cooldown_s integer NOT NULL DEFAULT 300
);

-- Preferencje uzytkownika per typ zdarzenia.
CREATE TABLE notify.preference (
    user_id     bigint  NOT NULL REFERENCES app.user(id) ON DELETE CASCADE,
    event_code  text    NOT NULL REFERENCES notify.event_type(code) ON DELETE CASCADE,
    enabled     boolean NOT NULL DEFAULT true,
    channels    notify.channel_kind[] NOT NULL DEFAULT '{inapp}',
    cooldown_s  integer,                        -- NULL = wartosc domyslna typu
    PRIMARY KEY (user_id, event_code)
);

-- Godziny ciszy i ustawienia globalne powiadomien.
CREATE TABLE notify.settings (
    user_id       bigint  PRIMARY KEY REFERENCES app.user(id) ON DELETE CASCADE,
    quiet_from    time,                         -- np. 23:00
    quiet_to      time,                         -- np. 07:00
    timezone      text    NOT NULL DEFAULT 'Europe/Warsaw',
    -- Nawet w godzinach ciszy alerty krytyczne przechodza. To nie jest
    -- do wylaczenia - patrz komentarz przy always_on.
    daily_digest  boolean NOT NULL DEFAULT true,
    digest_at     time    NOT NULL DEFAULT '08:00'
);

-- Powiadomienia w aplikacji (dzwonek). Zrodlo prawdy dla wszystkich kanalow:
-- najpierw powstaje wiersz tutaj, potem proby dostarczenia.
CREATE TABLE notify.notification (
    id          bigserial   PRIMARY KEY,
    user_id     bigint      NOT NULL REFERENCES app.user(id) ON DELETE CASCADE,
    event_code  text        NOT NULL REFERENCES notify.event_type(code),
    severity    notify.severity NOT NULL,
    title       text        NOT NULL,
    body        text,
    data        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    engine      ref.engine,
    created_at  timestamptz NOT NULL DEFAULT now(),
    read_at     timestamptz
);

CREATE INDEX notification_unread_idx ON notify.notification (user_id, created_at DESC)
    WHERE read_at IS NULL;
CREATE INDEX notification_recent_idx ON notify.notification (user_id, created_at DESC);

-- Slad dostarczenia per kanal - zeby dalo sie odpowiedziec na pytanie
-- "dlaczego nie dostalem alertu".
CREATE TABLE notify.delivery (
    id              bigserial   PRIMARY KEY,
    notification_id bigint      NOT NULL REFERENCES notify.notification(id) ON DELETE CASCADE,
    channel_id      bigint      REFERENCES notify.channel(id) ON DELETE SET NULL,
    kind            notify.channel_kind NOT NULL,
    status          text        NOT NULL,       -- 'sent','failed','suppressed_quiet','suppressed_cooldown'
    attempts        integer     NOT NULL DEFAULT 0,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz
);

CREATE INDEX delivery_notification_idx ON notify.delivery (notification_id);

-- ---------------------------------------------------------------- katalog zdarzen
INSERT INTO notify.event_type (code, label, description, category, default_severity, always_on, default_cooldown_s) VALUES
    ('killswitch.triggered', 'Kill-switch zadziałał',
     'System zatrzymał handel po przekroczeniu limitu obsunięcia kapitału lub straty dziennej. Wznowienie wymaga ręcznego odblokowania.',
     'ryzyko', 'critical', true, 0),
    ('risk.violation', 'Naruszenie limitu ryzyka',
     'Zlecenie zostało zablokowane lub zmniejszone, bo przekraczało ustalony limit.',
     'ryzyko', 'warning', false, 300),
    ('engine.down', 'Silnik przestał odpowiadać',
     'Brak sygnału życia z pętli decyzyjnej dłużej niż 5 minut. Nowe zlecenia są wstrzymane.',
     'system', 'critical', true, 600),
    ('trade.opened', 'Otwarto pozycję',
     'System wszedł w nową pozycję.', 'handel', 'info', false, 0),
    ('trade.closed', 'Zamknięto pozycję',
     'Pozycja została zamknięta wraz z wynikiem.', 'handel', 'info', false, 0),
    ('order.rejected', 'Giełda odrzuciła zlecenie',
     'Zlecenie nie zostało przyjęte — najczęściej z powodu limitów konta lub błędu w parametrach.',
     'handel', 'warning', false, 120),
    ('learning.state_changed', 'Zmiana stanu uczenia',
     'Model przeszedł między stanami (rozgrzewka, uczy się, zwalidowany, degradacja).',
     'uczenie', 'warning', false, 0),
    ('learning.drift_high', 'Wysoki dryf cech',
     'Rozkład cech odjechał od treningowego (PSI powyżej 0,25) — skuteczność modelu może spadać.',
     'uczenie', 'warning', false, 86400),
    ('postmortem.ready', 'Nowa analiza post-mortem',
     'Dostępne jest podsumowanie zamkniętych transakcji.', 'uczenie', 'info', false, 0),
    ('news.high_impact', 'Wiadomość o dużym wpływie',
     'Skaner znalazł artykuł o wysokiej sile i wysokiej nowości dla obserwowanego instrumentu.',
     'handel', 'info', false, 600),
    ('auth.new_login', 'Logowanie z nowego urządzenia',
     'Ktoś zalogował się na konto z adresu IP lub przeglądarki, których wcześniej nie widziano.',
     'konto', 'warning', true, 0),
    ('credential.changed', 'Zmiana kluczy lub hasła',
     'Zmieniono hasło, klucz API giełdy albo ustawienia drugiego składnika.',
     'konto', 'warning', true, 0),
    ('feed.failing', 'Kanał wiadomości nie odpowiada',
     'Źródło RSS zwraca błędy dłużej niż godzinę — system traci część sygnału newsowego.',
     'system', 'info', false, 3600)
ON CONFLICT (code) DO NOTHING;

-- Kanal w aplikacji istnieje zawsze i nie wymaga konfiguracji.
INSERT INTO notify.channel (user_id, kind, label, enabled, verified_at)
SELECT id, 'inapp', 'W aplikacji', true, now() FROM app.user
ON CONFLICT DO NOTHING;

-- Domyslne preferencje: wszystko wlaczone na kanale w aplikacji.
INSERT INTO notify.preference (user_id, event_code, enabled, channels)
SELECT u.id, e.code, true, '{inapp}'::notify.channel_kind[]
FROM app.user u CROSS JOIN notify.event_type e
ON CONFLICT DO NOTHING;

INSERT INTO notify.settings (user_id) SELECT id FROM app.user
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------- profil konta
ALTER TABLE app.user
    ADD COLUMN display_name text,
    ADD COLUMN timezone     text NOT NULL DEFAULT 'Europe/Warsaw',
    ADD COLUMN locale       text NOT NULL DEFAULT 'pl';
