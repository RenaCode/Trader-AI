-- 0072: wynik próby pożarowej w kolumnie — `risk.killswitch_event.proba_wynik`.
--
-- CO BYŁO ZŁE (audyt 3 T5, 2026-10-03)
--
-- Zdarzenia 16–25 (24.09–03.10) to dziesięć prób z rzędu „NIEDOKOŃCZONA”: ogniwa
-- 1-2 i 4 zadziałały, ogniwa 3 (silnik wstrzymał wejścia) nie dało się zmierzyć,
-- bo w oknie próby nie było żadnego zamiaru wejścia. Log mimo to pisał „Od
-- ostatniego potwierdzenia: 1.0 doby (próg 7)”: `_dni_od_potwierdzenia` liczył
-- KAŻDĄ próbę, która wywołała zdarzenie, bo zwolnienie z `finally` zapisuje tę
-- samą trójkę kolumn co zwolnienie po sukcesie. Docstring sam to przyznawał
-- i wskazywał kolumnę jako właściwe miejsce na rozróżnienie.
--
-- CO ZNACZY KOLUMNA (zapisuje ją wyłącznie `proba_killswitcha.py`)
--
--   'zaliczona'      — zmierzone i działające wszystkie cztery ogniwa,
--   'czesciowa'      — ogniwa 1-2 i 4 zadziałały, ogniwa 3 NIE ZMIERZONO
--                      (brak zamiaru wejścia w oknie próby),
--   'niedokonczona'  — każdy inny koniec: przerwanie, błąd, ogniwo niezmierzone
--                      poza trzecim,
--   NULL             — zdarzenie nie z próby ALBO próba sprzed tej migracji.
--
-- BEZ BACKFILLU. Starsze notatki („PRÓBA POŻAROWA — …”, „… NIEDOKOŃCZONA — …”)
-- nie mówią, które ogniwo nie przeszło, więc każda wartość wpisana wstecz byłaby
-- zgadywaniem. Licznik ciszy traktuje NULL tak, jak przed tą migracją; licznik
-- PEŁNEGO potwierdzenia startuje od zera i mówi to wprost.

BEGIN;

-- BLOKADA: ADD COLUMN bierze ACCESS EXCLUSIVE na `risk.killswitch_event`, a CHECK
-- skanuje tabele (dziesiatki wierszy - ulamek sekundy). Groznie jest dopiero
-- CZEKANIE: ALTER w kolejce za dlugim odczytem blokuje kazdy kolejny odczyt
-- tabeli zatrzyman, czyli cykl silnika. Jak w 0070: po 5 s rezygnujemy,
-- migracja pada, `api` restartuje sie i probuje ponownie.
SET LOCAL lock_timeout = '5s';

ALTER TABLE risk.killswitch_event
    ADD COLUMN IF NOT EXISTS proba_wynik text
        CHECK (proba_wynik IN ('zaliczona', 'czesciowa', 'niedokonczona'));

GRANT UPDATE (proba_wynik) ON risk.killswitch_event TO trader_proba;

COMMIT;
