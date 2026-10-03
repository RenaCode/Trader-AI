-- 0076: dwie zmiany nadan z audytu Fable (2026-10-03) w jednym pliku.
--
-- (1) U-9: `trader_silnik` traci SELECT na `app.secret`.
--
-- 0034 nadalo silnikowi odczyt tabeli sekretow ustawien (klucze dostawcow LLM
-- i inne wartosci z zakladki Ustawienia). Zaden proces tej roli jej nie czyta:
-- klucze LLM ida przez widok `app.secret_llm` (0052), ktory chodzi prawami
-- wlasciciela, wiec dalej dziala. Procesy tej roli (engine, market, history)
-- maja SECRETS_KEY - odczyt tabeli byl wiec odczytem sekretow, nie szyfrogramow.
--
-- (2) U-12: proba pozarowa czyta puls silnika - ogniwo 3 ze stanu bramki.
--
-- Trzy proby z rzedu (zdarzenia 23-25) "NIEROZSTRZYGNIETA - ogniwo 3 NIE
-- ZMIERZONO", bo w 10-minutowym oknie nie bylo zamiaru wejscia, a tylko on
-- zostawia slad w `journal.decision`. `loop.run_once` zapisuje teraz
-- w `risk.engine_heartbeat.detail` id kill-switcha, ktory w tym cyklu
-- wstrzymal wejscia; proba czyta ten wiersz. Tylko odczyt - proba, ktora
-- moglaby pisac puls, mierzylaby wlasny zapis.

BEGIN;

SET LOCAL lock_timeout = '5s';

REVOKE SELECT ON app.secret FROM trader_silnik;

GRANT SELECT ON risk.engine_heartbeat TO trader_proba;

COMMIT;
