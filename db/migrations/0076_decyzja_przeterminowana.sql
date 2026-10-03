-- 0076: nowy powód holdu `decyzja_przeterminowana` (audyt Fable 2026-10-03, F-4.4 / T7).
--
-- CO SIĘ ZMIENIA W ZACHOWANIU
--
-- Wejście odpada, gdy od zamknięcia świecy decyzyjnej minęło ponad 20 minut
-- (`loop.MAKS_ZWLOKA_WEJSCIA`). Cena decyzyjna to zamknięcie świecy, więc
-- sygnał po godzinie opisuje rynek, którego już nie ma. Zmierzone przed
-- poprawką potoku: decyzje 63–88 min po zamknięciu, kupna wypełniane 76–95 pb
-- nad ceną decyzji (NEAR #86, BTC #83). Wyjść to nie dotyczy.
--
-- DLACZEGO TO WYMAGA MIGRACJI
--
-- Zbiór powodów jest zamknięty CHECK-iem (0040, rozszerzony w 0067), więc
-- pierwszy taki hold bez tej migracji wywróciłby zapis decyzji. Kolumna jest
-- `text`, więc zmienia się wyłącznie ograniczenie.
--
-- KOLEJNOŚĆ WDROŻENIA: migracja PRZED obrazem silnika. Odwrotnie pierwszy
-- spóźniony cykl kończy się błędem CHECK i cofnięciem całej transakcji decyzji.
ALTER TABLE journal.decision
    DROP CONSTRAINT IF EXISTS decision_powod_holdu_chk;

ALTER TABLE journal.decision
    ADD CONSTRAINT decision_powod_holdu_chk CHECK (
        powod_holdu IS NULL OR powod_holdu IN (
            'slaby_wynik', 'niska_pewnosc', 'short_niewykonalny',
            'trzyma_pozycje', 'wstrzymanie_ryzykiem', 'rezim_bez_progu',
            'decyzja_przeterminowana'));
