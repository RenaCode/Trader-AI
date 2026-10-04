-- 0091: strategia "momentum + filtr trendu" na demo (decyzja właściciela "A",
-- 2026-10-04; docs/hipotezy.md, E-1).
--
--   journal.decision.strategia      - która reguła podjęła decyzję
--   decision_powod_holdu_chk        - trzy powody holdu tej reguły
--
-- CO SIĘ ZMIENIA W ZACHOWANIU
--
-- Silnik crypto/demo chodzi z `--strategia momentum` (charts/trader/values.yaml):
-- zamrożona reguła R4 z H-3 zamiast ensemble, 15% kapitału, wejścia tylko na
-- zamknięciu doby UTC. Model nie jest czytany, więc `model_version_id` jest
-- NULL - a NULL znaczył dotąd "założenia początkowe, brak modelu". Bez nowej
-- kolumny decyzji reguły nie dałoby się odróżnić od ensemble bez modelu, a pomiar
-- egzekucji (`poslizg.py --strategia momentum_filtr_trendu`) liczyłby razem
-- zlecenia dwóch różnych strategii.
--
-- NULL w `strategia` = decyzja sprzed tej migracji (wtedy zawsze ensemble).
-- Nie przepisujemy wstecz: `journal.decision` jest append-only (0005).
--
-- Powody holdu (zbiór zamknięty CHECK-iem od 0040, ostatnio 0080):
--   filtr_trendu         BTC pod EMA 50 d - cel = gotówka
--   poza_skladem         instrument poza top 3 po momentum 30 d
--   poza_decyzja_dobowa  instrument w składzie, ale wejście tylko o północy UTC
--
-- BLOKADA: jak 0075/0080 - ADD COLUMN bez DEFAULT nie przepisuje tabeli, ADD
-- CHECK skanuje kilkadziesiąt tysięcy wierszy (ułamek sekundy). Groźne jest
-- czekanie w kolejce za odczytem pętli, więc po 5 s rezygnujemy, migracja pada,
-- `api` restartuje się i próbuje ponownie.
--
-- KOLEJNOŚĆ WDROŻENIA: migracja PRZED obrazem silnika (migrator chodzi jako
-- kontener startowy `api`, silnik startuje po `sleep 90`). Odwrotnie pierwszy
-- zapis decyzji wywróci się na nieznanej kolumnie i cofnie transakcję cyklu.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE journal.decision
    ADD COLUMN IF NOT EXISTS strategia text;

ALTER TABLE journal.decision
    DROP CONSTRAINT IF EXISTS decision_strategia_chk;

ALTER TABLE journal.decision
    ADD CONSTRAINT decision_strategia_chk CHECK (
        strategia IS NULL OR strategia IN ('ensemble', 'momentum_filtr_trendu'));

COMMENT ON COLUMN journal.decision.strategia IS
    'Reguła, która wybrała akcję (loop.STRATEGIE): ensemble = wagi z aktywnej '
    'wersji modelu albo założenia początkowe; momentum_filtr_trendu = zamrożona '
    'reguła R4 (H-3) na demo, pomiar egzekucji. NULL = decyzja sprzed 0091 (ensemble).';

ALTER TABLE journal.decision
    DROP CONSTRAINT IF EXISTS decision_powod_holdu_chk;

ALTER TABLE journal.decision
    ADD CONSTRAINT decision_powod_holdu_chk CHECK (
        powod_holdu IS NULL OR powod_holdu IN (
            'slaby_wynik', 'niska_pewnosc', 'short_niewykonalny',
            'trzyma_pozycje', 'wstrzymanie_ryzykiem', 'rezim_bez_progu',
            'szeroki_spread', 'decyzja_przeterminowana',
            'filtr_trendu', 'poza_skladem', 'poza_decyzja_dobowa'));

COMMIT;
