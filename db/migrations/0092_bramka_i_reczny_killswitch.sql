-- 0092: alarmy krytyczne przez bramkę RenaCode + ręczny kill-switch z panelu
--
-- DECYZJA WŁAŚCICIELA 2026-10-04
--
-- 1. Alarm krytyczny (kill-switch, rozjazd z giełdą, martwy silnik, brak
--    kopii...) ma wychodzić poza panel. Do dziś bez skonfigurowanego kanału
--    użytkownika (CallMeBot/webhook) lądował tylko w dzwonku i czekał, aż
--    ktoś sam otworzy dashboard - 04.10 było tam ok. 53 nieprzeczytanych.
--    Od teraz `notify()` przekazuje każdy alarm krytyczny do bramki
--    powiadomień RenaCode (WhatsApp, zapasowo mail) - patrz
--    services/api/bramka.py.
--
-- 2. Ręczny kill-switch w zakładce Ryzyko: ta sama ścieżka co automat
--    (wiersz w `risk.killswitch_event`, silnik czyta go w każdym cyklu),
--    z hasłem, wpisem w audycie i alarmem przez bramkę.
--
-- `bramka` W ENUMIE KANAŁÓW. Przekazanie do bramki zostawia ślad
-- w `notify.delivery` jak każdy inny kanał - z tego samego powodu co tam:
-- odpowiedź na „dlaczego nie dostałem alarmu" ma być w bazie. Ten sam ślad
-- tłumi powtórki (alarm, który miga, nie może zasypać WhatsAppa), a liczą go
-- procesy w różnych podach - pamięć procesu by tu nie wystarczyła.
-- `notify.channel` tej wartości nie używa: bramka nie jest kanałem
-- użytkownika, tylko kanałem systemu (klucz w Sekrecie, odbiorcy w bramce),
-- a walidacja API (`Kanal`, `ChannelBody`) nadal jej nie przyjmuje.
--
-- `triggered_by` - KTO zatrzymał. NULL = system (limit albo próba
-- pożarowa). Bez tej kolumny ręczne zatrzymanie różniłoby się od automatu
-- wyłącznie zdaniem w `reason`, a to ta sama pułapka, którą 0029 zamknęło dla
-- `limit_name`.
--
-- WĄSKIE NADANIE DLA API. 0034 odebrało `trader_app` INSERT na tej tabeli,
-- bo API go nie potrzebowało. Teraz potrzebuje - ale tylko kolumn, które
-- opisują ŻĄDANIE zatrzymania. `released_*`, `triggered_at` i `proba_wynik`
-- zostają poza nadaniem: API nie wstawi zdarzenia od razu zwolnionego ani
-- z cofniętą datą. Zwolnienie ręczne idzie jak dotąd przez UPDATE trzech
-- kolumn z 0034.

BEGIN;

-- Jak w 0072: ADD COLUMN bierze ACCESS EXCLUSIVE na tabeli zatrzymań, którą
-- silnik czyta w każdym cyklu. Po 5 s czekania migracja pada i `api` próbuje
-- ponownie, zamiast trzymać kolejkę blokad.
SET LOCAL lock_timeout = '5s';

-- ADD VALUE w transakcji jest dozwolone od PostgreSQL 12; nowej wartości nie
-- używamy w tej samej transakcji, więc ograniczenie z dokumentacji nie dotyczy.
ALTER TYPE notify.channel_kind ADD VALUE IF NOT EXISTS 'bramka';

COMMENT ON TYPE notify.channel_kind IS
'Kanały dostarczania. `bramka` = bramka powiadomień RenaCode (WhatsApp/mail),
kanał systemowy dla alarmów krytycznych (0092) - występuje wyłącznie
w notify.delivery, nie w notify.channel ani w preferencjach.';

ALTER TABLE risk.killswitch_event
    ADD COLUMN IF NOT EXISTS triggered_by bigint REFERENCES app.user(id);

COMMENT ON COLUMN risk.killswitch_event.triggered_by IS
'Kto zażądał zatrzymania z panelu (0092). NULL = system: limit ryzyka albo
próba pożarowa.';

GRANT INSERT (engine, mode, reason, limit_name, triggered_by)
    ON risk.killswitch_event TO trader_app;

COMMIT;
