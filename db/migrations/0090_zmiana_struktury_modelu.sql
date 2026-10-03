-- 0090: bramka struktury modelu (audyt Fable F-3.1 / T8, 2026-10-03)
--
--   learn.model_version.zmiana_struktury  - czym `params` nowej wersji roznia
--                                           sie strukturalnie od aktywnej
--   learning.zmiana_struktury             - typ zdarzenia, wysyla dozor
--
-- CO SIE ZMIENIA W ZACHOWANIU
--
-- 28.09 format parametrow zmienil sie deployem kodu (progi per rezim) i w demo
-- wszedl sam: kazda wersja nie-`degraded` stawala sie `active`. Od tej
-- migracji trener (`train.zmiana_struktury`) porownuje nowa wersje z aktywna:
-- inny zestaw kluczy `params`, inna konfiguracja albo prog per rezim rozny
-- o >= 10% -> wersja zostaje `candidate`, opis zmiany idzie do tej kolumny,
-- a `dozor.py` (stan nauki, po treningu) wysyla `learning.zmiana_struktury`
-- (`warning`). Promocja: wpis w docs/hipotezy.md i reczny przebieg
-- z `--zatwierdz-promocje`.
--
-- DLACZEGO KOLUMNA, A NIE KLUCZ W `params`: `count(DISTINCT params)` jest N
-- deflacji (`train.n_konfiguracji`); opis zmiany w `params` rozdzielalby
-- konfiguracje, ktore roznia sie wylacznie tym napisem.
--
-- Kolumna nullable bez DEFAULT - ALTER bez przepisywania tabeli. `lock_timeout`,
-- zeby migrator nie stal w kolejce za trwajacym treningiem (trzyma blokady
-- na `learn.model_version` przez caly przebieg) i nie blokowal za soba odczytow.
--
-- KOLEJNOSC WDROZENIA: migracja PRZED obrazem trenera i dozoru. Trener bez
-- kolumny wywroci zapis wyniku; dozor bez typu zdarzenia - `notify` rzuca
-- `ValueError` na nieznany typ. Migrator chodzi jako kontener startowy `api`.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE learn.model_version
    ADD COLUMN IF NOT EXISTS zmiana_struktury text;

COMMENT ON COLUMN learn.model_version.zmiana_struktury IS
    'Opis zmiany struktury params wzgledem aktywnej wersji w chwili treningu '
    '(train.zmiana_struktury); NULL = bez zmiany albo brak aktywnej wersji.';

INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'learning.zmiana_struktury',
    'Zmiana struktury modelu',
    'Nowa wersja modelu ma inną strukturę parametrów niż aktywna (klucze, ' ||
    'konfiguracja albo progi per reżim różne o 10% lub więcej). Została ' ||
    'kandydatem; promocja wymaga wpisu w rejestrze hipotez i zgody człowieka.',
    'uczenie', 'warning', false, 0)
ON CONFLICT (code) DO NOTHING;

INSERT INTO notify.preference (user_id, event_code, enabled, channels)
SELECT u.id, 'learning.zmiana_struktury', true, ARRAY['inapp']::notify.channel_kind[]
  FROM app."user" u
 WHERE u.is_active
ON CONFLICT (user_id, event_code) DO NOTHING;

COMMIT;
