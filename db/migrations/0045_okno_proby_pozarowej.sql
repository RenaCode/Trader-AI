-- 0045: próba pożarowa ZAPOWIADA swoje okno, żeby czujka żywotności nie brała
--       jej skutku za awarię.
--
-- CO BYŁO ZŁE
--
-- Próba pożarowa (`proba_killswitcha.py`, CronJob 05:20 lokalnie) podnosi
-- `min_equity` powyżej kapitału i czeka, aż silnik SAM wywoła kill-switch,
-- a dozór SAM o nim powiadomi. Trwa to ~10 minut. Czujka żywotności
-- (`zywotnosc`, co 10 minut) trafia w to okno co rano, widzi aktywne zdarzenie
-- `risk.killswitch_event`, wysyła drugie powiadomienie i kończy się kodem 1.
-- Zmierzone 2026-09-22: próba 03:20-03:30 UTC zaliczona, czujka 03:30 UTC
-- „Failed" z linią `KILL-SWITCH crypto/demo: Kapitał 99913.31 spadł do progu
-- 100413.00 (9 min temu)`, czujka 07:00 „brak aktywnych zdarzeń".
--
-- Codzienny fałszywy alarm o stałej porze uczy ignorować alarm o tej porze —
-- a to jest dokładnie ta godzina, o której PRAWDZIWY kill-switch z nocnej
-- sesji byłby najbardziej prawdopodobny.
--
-- DLACZEGO ZNACZNIK, A NIE ROZPOZNAWANIE PO TREŚCI
--
-- Zdarzenie próby jest NIEODRÓŻNIALNE od prawdziwego: ten sam limit
-- (`min_equity`), ten sam powód, ten sam wiersz wstawiony przez ten sam
-- `RiskKernel`. To celowe — próba mierzy prawdziwą ścieżkę, nie atrapę.
-- Jedyne, co próba wie, a czego nie wie nikt inny, to ŻE TRWA. Więc mówi
-- to bazie, zanim podniesie próg.
--
-- DLACZEGO KOLUMNA W `risk.limit_def`, A NIE W `risk.killswitch_event`
--
-- Zdarzenie powstaje DOPIERO po podniesieniu progu, w cyklu silnika — czujka
-- może je zobaczyć wcześniej, niż próba zdąży cokolwiek na nim zapisać.
-- Znacznik na limicie idzie PRZED podniesieniem wartości, w tej samej
-- transakcji, więc nie ma okna, w którym zdarzenie istnieje bez znacznika.
-- Poza tym próba i tak nie ma prawa INSERT/UPDATE na cudze kolumny zdarzenia
-- (0034) i nie powinna go dostać.
--
-- DLACZEGO TERMIN, A NIE FLAGA
--
-- Próba, która zginie między podniesieniem a `finally`, zostawiłaby flagę
-- na zawsze — i każdy późniejszy PRAWDZIWY kill-switch na `min_equity`
-- wyglądałby jak próba. Termin wygasa sam. Okno ma być dłuższe niż
-- najdłuższa dopuszczalna próba (`CZEKAJ_SILNIK_S` + `CZEKAJ_DOZOR_S`
-- = 18 min) i krótsze niż cokolwiek, co dałoby się pomylić ze stanem trwałym.

ALTER TABLE risk.limit_def
    ADD COLUMN IF NOT EXISTS proba_do timestamptz;

COMMENT ON COLUMN risk.limit_def.proba_do IS
    'Do kiedy trwa próba pożarowa tego limitu. Zdarzenie kill-switcha na tym '
    'limicie przed tym terminem jest SKUTKIEM PRÓBY, nie alarmem. NULL = brak próby.';

-- Próba dostaje prawo do tej jednej kolumny — i nadal do żadnej innej
-- (`enabled`, `action` zostają poza jej zasięgiem, patrz 0034).
GRANT UPDATE (proba_do) ON risk.limit_def TO trader_proba;
