-- 0042: uzupełnienie `nav` wstecz — żeby szczyt jednostki był szczytem
--
-- POWÓD
--
-- Migracja 0031 dodała `exec.equity_point.nav` i przestawiła obsunięcie oraz
-- stratę dzienną z kapitału na wartość jednostki. Dodała KOLUMNĘ, ale nie
-- uzupełniła jej dla punktów już istniejących. Krzywa produkcyjna zaczyna się
-- 2026-08-27 09:07, a `nav` dopiero 2026-09-07 11:49: 3190 z 3714 punktów
-- (86%) nie ma tej wartości.
--
-- Dla `max(nav)` punkt bez `nav` nie jest punktem NIŻSZYM — jest punktem
-- NIEISTNIEJĄCYM. Szczyt sprzed migracji przepada więc bez śladu, a obsunięcie
-- liczy się od szczytu MŁODSZEGO NIŻ RACHUNEK. Pomiar na produkcji 2026-09-09:
--
--     szczyt equity : 100105,995041609656  (2026-08-27..09-09, cała krzywa)
--     equity teraz  : 100063,092070322300
--     obsunięcie rzeczywiste                       0,0429%
--
--     max(nav)      : 1,000731879327  (tylko od 2026-09-07 11:49)
--     nav teraz     : 1,000630920703
--     obsunięcie mierzone przez bezpiecznik        0,0101%
--
-- Zaniżenie 4,2x. Kierunek jest JEDNOSTRONNY: szczyt z krótszego okna jest
-- zawsze mniejszy albo równy prawdziwemu, więc obsunięcie wychodzi zawsze
-- mniejsze — nigdy większe. Gdyby `nav` zaczęło się zapisywać w dołku (nowy
-- tryb, purge_demo.py, migracja przerwana), rachunek 9% pod wodą dostałby do
-- dyspozycji kolejne 10% przy progu 10%, bez jednego błędu w dzienniku.
--
-- DLACZEGO TO WOLNO ODTWORZYĆ, A NIE ZGADNĄĆ
--
-- `nav = equity / units`, a `units` zmienia się WYŁĄCZNIE przy przepływie:
-- wpłata kupuje jednostki, wypłata je umarza, wynik handlu nie rusza ich wcale.
-- Jeśli więc WSZYSTKIE przepływy danego rachunku wydarzyły się PRZED pierwszym
-- punktem krzywej, to `units` było stałe przez całą jej długość i równe sumie
-- z `exec.cash_flow`. Wtedy `nav` w każdym punkcie jest wyliczalne dokładnie,
-- bez żadnego założenia o tym, czego nie wiemy.
--
-- Rachunek demonstracyjny spełnia ten warunek: jeden przepływ, 100 000 jednostek,
-- 2026-08-27 00:00, czyli dziewięć godzin przed pierwszym punktem krzywej.
-- Sprawdzenie na 524 punktach, które `nav` już mają: max |nav*units − equity|
-- wynosi 3,0e-8, czyli tyle, ile waży zaokrąglenie numeric(28,12).
--
-- CZEGO TA MIGRACJA NIE ROBI
--
-- Rachunku z przepływem W TRAKCIE krzywej NIE RUSZA. Odtworzenie `units` w
-- każdym punkcie wymagałoby znajomości `nav` w chwili tamtego przepływu, czyli
-- dokładnie tej liczby, której szukamy. Zgadnięcie jej dałoby krzywą wyglądającą
-- na kompletną i mierzącą coś innego niż wynik — gorzej niż brak danych, bo
-- brak danych jest widoczny. Takie punkty zostają NULL, a bezpiecznik cofa się
-- dla nich na `equity` (services/engine/risk_rules.py, `_nav_wiarygodne`).
--
-- BEZ WYŁĄCZANIA WYZWALACZY
--
-- `exec.equity_point` nie ma wyzwalacza niezmienności — w odróżnieniu od
-- `journal.decision` (0005), `journal.decision_contribution` (0005) i
-- `journal.pominiecie` (0041). Sprawdzone w pg_trigger na produkcji: dla tej
-- tabeli nie ma ani jednego wyzwalacza niewewnętrznego. Krzywa kapitału jest
-- pomiarem odtwarzalnym z pozycji i przepływów, a nie zapisem decyzji, więc
-- świadomie nie jest chroniona jak dziennik. Gdyby kiedyś była — ten UPDATE
-- zacząłby padać, a nie po cichu nie robić nic.
--
-- Hypertable bez kompresji (timescaledb_information.hypertables:
-- equity_point.compression_enabled = false), więc UPDATE po chunkach działa.

BEGIN;

WITH przeplywy AS (
    -- Suma przepływów rachunku i moment OSTATNIEGO z nich.
    SELECT engine, mode,
           sum(amount)        AS wplaty,
           sum(units)         AS units,
           max(event_time)    AS ostatni_przeplyw
      FROM exec.cash_flow
     GROUP BY engine, mode
), krzywa AS (
    SELECT engine, mode, min(event_time) AS pierwszy_punkt
      FROM exec.equity_point
     GROUP BY engine, mode
), odtwarzalne AS (
    -- WARUNEK, KTÓRY CZYNI TO ODTWORZENIEM, A NIE ZGADYWANIEM: żaden przepływ
    -- nie wydarzył się w trakcie krzywej, więc `units` było na niej stałe.
    SELECT p.engine, p.mode, p.wplaty, p.units
      FROM przeplywy p
      JOIN krzywa k ON k.engine = p.engine AND k.mode = p.mode
     WHERE p.ostatni_przeplyw <= k.pierwszy_punkt
       AND p.units > 0
)
UPDATE exec.equity_point ep
   SET nav          = ep.equity / o.units,
       units        = o.units,
       wplaty_netto = o.wplaty
  FROM odtwarzalne o
 WHERE ep.engine = o.engine
   AND ep.mode   = o.mode
   AND ep.nav IS NULL;

COMMIT;
