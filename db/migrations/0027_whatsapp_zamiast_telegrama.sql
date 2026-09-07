-- 0027: WhatsApp wchodzi, Telegram wychodzi
--
-- Wlasciciel nie uzywa Telegrama i nie zamierza. Kanal, ktorego nikt nie
-- skonfiguruje, jest gorszy niz brak kanalu: widnieje w interfejsie jako
-- dzialajaca opcja i przy kazdym przegladzie trzeba sobie przypominac, ze
-- to martwy kod.
--
-- DLACZEGO PRZEPISUJEMY CALY TYP, A NIE DOPISUJEMY WARTOSCI
--
-- PostgreSQL potrafi ADD VALUE do enuma, ale nie potrafi DROP VALUE. Zeby
-- 'telegram' faktycznie znikl - a nie zostal jako wartosc, ktora nadal wolno
-- wstawic - trzeba zbudowac typ od nowa i przestawic na niego kolumny.
--
-- Sprawdzone przed napisaniem tej migracji (2026-09-07, produkcja):
--
--     notify.channel     kind='telegram'   0 wierszy
--     notify.delivery    kind='telegram'   0 wierszy
--     notify.preference  'telegram' w channels   0 wierszy
--
-- Konwersja nie ma wiec czego zgubic. Gdyby wiersze byly, ta migracja
-- zatrzymalaby sie na bledzie rzutowania - i tak ma byc: ciche skasowanie
-- historii dostarczen bylo by gorsze niz nieudana migracja.

BEGIN;

CREATE TYPE notify.channel_kind_new AS ENUM ('inapp', 'whatsapp', 'webhook');

-- Domyslna wartosc musi zejsc na czas zmiany typu - inaczej PostgreSQL probuje
-- rzutowac ja starym typem i odmawia.
ALTER TABLE notify.preference ALTER COLUMN channels DROP DEFAULT;

ALTER TABLE notify.channel
    ALTER COLUMN kind TYPE notify.channel_kind_new
    USING kind::text::notify.channel_kind_new;

ALTER TABLE notify.delivery
    ALTER COLUMN kind TYPE notify.channel_kind_new
    USING kind::text::notify.channel_kind_new;

ALTER TABLE notify.preference
    ALTER COLUMN channels TYPE notify.channel_kind_new[]
    USING channels::text[]::notify.channel_kind_new[];

ALTER TABLE notify.preference
    ALTER COLUMN channels SET DEFAULT '{inapp}'::notify.channel_kind_new[];

DROP TYPE notify.channel_kind;
ALTER TYPE notify.channel_kind_new RENAME TO channel_kind;

COMMENT ON TYPE notify.channel_kind IS
'Kanaly dostarczania powiadomien. Telegram usuniety w 0027 - wlasciciel go nie
uzywa. WhatsApp idzie przez CallMeBot: konfiguracja to {telefon, apikey}.';

COMMENT ON COLUMN notify.channel.config_enc IS
'JSON zaszyfrowany SecretBoxem. Ksztalt zalezy od `kind`:
  whatsapp -> {"telefon": "+48...", "apikey": "..."}
  webhook  -> {"url": "https://...", "secret": "..."}
Szyfrowane, bo klucz CallMeBota pozwala pisac na ten numer komukolwiek,
kto go ma.';

COMMIT;
