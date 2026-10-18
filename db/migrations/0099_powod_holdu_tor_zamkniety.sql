-- 0099: powod bezczynnosci `tor_zamkniety`.
--
-- CO SIE ZMIENILO (2026-10-18, decyzja wlasciciela B-0 = STOP)
--
--   * Tor crypto zamkniety w calosci - spot (`spot_eu`, H-9) i perp (B-0) -
--     jako wynik negatywny (`services/engine/tory.py`, `ZAMKNIETE`). Silnik
--     demo na torze zamknietym chodzi dalej, ale nowe wejscia zamienia na
--     `hold` z tym powodem; wyjscia ida bez zmian, wiec otwarte pozycje
--     domykaja sie same. Wczesniej demo wchodzilo jako pomiar egzekucji
--     (H-10, B-0 krok 1) - obu pomiarow juz nie ma.
--
-- Zbior powodow jest zamkniety (0040 -> 0067 -> 0075 -> 0080 -> 0091 ->
-- 0098), wiec bez tej migracji pierwszy taki hold wywrocilby zapis decyzji.
-- KOLEJNOSC WDROZENIA: migracja PRZED kodem petli.
--
-- BLOKADA: jak w 0075 - po 5 s rezygnujemy, migracja pada, `api` restartuje
-- sie i probuje ponownie.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE journal.decision
    DROP CONSTRAINT IF EXISTS decision_powod_holdu_chk;

ALTER TABLE journal.decision
    ADD CONSTRAINT decision_powod_holdu_chk CHECK (
        powod_holdu IS NULL OR powod_holdu IN (
            'slaby_wynik', 'niska_pewnosc', 'short_niewykonalny',
            'trzyma_pozycje', 'wstrzymanie_ryzykiem', 'rezim_bez_progu',
            'szeroki_spread', 'decyzja_przeterminowana',
            'filtr_trendu', 'poza_skladem', 'poza_decyzja_dobowa',
            'ponizej_jednej_akcji', 'swieca_przed_sesja',
            'tor_zamkniety'));

COMMIT;
