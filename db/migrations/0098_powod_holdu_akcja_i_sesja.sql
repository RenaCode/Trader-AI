-- 0098: dwa powody bezczynnosci toru equity - `ponizej_jednej_akcji`
-- i `swieca_przed_sesja`.
--
-- CO BYLO ZLE (2026-10-18, tor equity bez ani jednego wejscia)
--
--   * `ponizej_jednej_akcji`: akcje maja `lot_size` 1, a budzet na pozycje
--     (300-750 USD) bywa nizszy niz cena jednej akcji. Plan z iloscia ponizej
--     jednej sztuki odpadal dopiero przy wysylce (`zlecenia.sprawdz_zlecenie`:
--     "ilosc po zaokragleniu do kroku wynosi zero"), a dziennik decyzji
--     pokazywal wejscie, ktorego nigdy nie bylo. Petla odmawia teraz juz przy
--     decyzji (`loop.ponizej_kroku_ilosci`).
--   * `swieca_przed_sesja`: swieca 12:00-13:00 UTC (otwarcie 13:30) jest
--     rozstrzygana najwczesniej po otwarciu, czyli zawsze po
--     `MAKS_ZWLOKA_WEJSCIA` - i konczyla jako `decyzja_przeterminowana`,
--     licznik, ktory ma mierzyc spoznienie potoku danych, nie godzine
--     otwarcia gieldy.
--
-- Zbior powodow jest zamkniety (0040 -> 0067 -> 0075 -> 0080 -> 0091), wiec
-- bez tej migracji pierwszy taki hold wywrocilby zapis decyzji.
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
            'ponizej_jednej_akcji', 'swieca_przed_sesja'));

COMMIT;
