-- 0077: proba pozarowa czyta puls silnika - ogniwo 3 ze stanu bramki.
--
-- Audyt Fable U-12 (2026-10-03): trzy proby z rzedu (zdarzenia 23-25)
-- "NIEROZSTRZYGNIETA - ogniwo 3 NIE ZMIERZONO", bo w 10-minutowym oknie proby
-- nie bylo zamiaru wejscia, a tylko on zostawia slad w `journal.decision`.
-- `loop.run_once` zapisuje teraz w `risk.engine_heartbeat.detail` id
-- kill-switcha, ktory w tym cyklu wstrzymal wejscia; proba czyta ten wiersz.
-- Tylko odczyt - proba, ktora moglaby pisac puls, mierzylaby wlasny zapis.

BEGIN;

SET LOCAL lock_timeout = '5s';

GRANT SELECT ON risk.engine_heartbeat TO trader_proba;

COMMIT;
