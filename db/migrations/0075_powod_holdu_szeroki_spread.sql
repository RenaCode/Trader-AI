-- 0075: powod bezczynnosci `szeroki_spread` - wejscie zablokowane kosztem.
--
-- CO BYLO ZLE (audyt Fable F-1.5, 2026-10-03)
--
-- Wlasna ksiega, 7 dni: NEARUSDC srednio 21,2 pb spreadu, DOGE 7,6, reszta
-- <= 6 pb. NEAR dal 4 rundy gieldowe i -218 USD, czyli 59% straty gieldowej.
-- Petla blokuje teraz NOWE wejscie w instrument, ktorego sredni
-- `book_spread_bps` z 7 dni przekracza 15 pb (`uniwersum.za_szeroki_spread`).
-- Wyjscia ida bez zmian.
--
-- Zbior powodow jest zamkniety (0040, przepisany w 0067), wiec bez tej
-- migracji pierwszy taki hold wywrocilby zapis decyzji - i to jest
-- zachowanie zamierzone. KOLEJNOSC WDROZENIA: migracja PRZED kodem petli.
--
-- BLOKADA: `journal.decision` czyta petla co cykl. DROP/ADD CONSTRAINT bierze
-- ACCESS EXCLUSIVE, a ADD CHECK skanuje tabele (kilkanascie tysiecy wierszy -
-- ulamek sekundy). Groznie jest dopiero CZEKANIE w kolejce za dlugim odczytem,
-- wiec jak w 0070/0072: po 5 s rezygnujemy, migracja pada, `api` restartuje
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
            'szeroki_spread'));

COMMIT;
