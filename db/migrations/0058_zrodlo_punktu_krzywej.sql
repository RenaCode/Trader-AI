-- 0058: krzywa kapitalu przestaje byc anonimowa - kto zapisal punkt
--
-- CO JEST NIE TAK
--
-- Do `exec.equity_point` pisza TRZY procesy z TRZEMA roznymi definicjami
-- kapitalu, a wiersz nie mowi, ktora z nich zawiera:
--
--   loop.seed_equity            STALA: START_EQUITY i nav = 1.0 na sztywno
--   loop.update_equity          REKONSTRUKCJA z naszego dziennika wypelnien
--   kapital_gieldy.zapisz_punkt ODCZYT z ksiag gieldy (Alpaca /v2/account)
--
-- Trzeci doszedl 2026-09-22 razem z workerem `kapital-equity` i od tego dnia
-- seria `equity/demo` ma DWOCH pisarzy naraz: petla silnika zasiewa punkt
-- (`seed_equity` wykonuje sie PRZED bramka `--wymagaj-modelu`, wiec dziala
-- takze bez modelu), a worker dopisuje odczyty z ksiag co 900 s.
--
-- Czytelnicy - `RiskKernel`, dozorca ryzyka i `wznowienie.stan_konta` - biora
-- z tej serii `max`, ostatni punkt i punkt sprzed doby BEZ pytania, kto
-- zapisal. Zlanie definicji nie objawia sie wiec bledem; objawia sie miara
-- liczona z dwoch roznych rzeczy naraz.
--
-- DLACZEGO TO JEST GROZNE, A NIE TYLKO NIEELEGANCKIE
--
-- `stan_kapitalu.SQL_NAV_AGREGAT` to `max(nav)` BEZ OGRANICZENIA CZASOWEGO,
-- po calej historii serii. Zasiane `nav = 1.0` - twierdzenie "rachunek jest
-- dokladnie rowny wplatom", ktorego nikt nie zmierzyl - zostaje wiec szczytem
-- NA ZAWSZE. Limit `max_drawdown` dla equity/demo to 10% z akcja `killswitch`
-- (migracja 0008), wiec konto paper ponizej 90% zgadnietej wplaty daje
-- kill-switch, ktory NIE MIJA Z DOBA i nie mija nigdy: przyczyna jest
-- arytmetyczna, a dozorca chodzi z `--engine crypto`, wiec dla equity NIE MA
-- procesu zdolnego go zwolnic. Zostaje `POST /api/risk/killswitch/{id}/release`.
--
-- Dzis szkoda jest USPIONA, nie nieistniejaca: zmierzone 2026-09-26 na
-- produkcji - 367 punktow equity/demo, WSZYSTKIE z `nav = 1.000000`, zero
-- kill-switchy equity w calej historii, zero instrumentow
-- `engine='equity' AND tradeable`. Skoro nic nie handluje, trzy definicje daja
-- trywialnie te sama liczbe. Naprawa jest wiec tania wlasnie teraz, bo nie ma
-- historii do uratowania.
--
-- CO ROBI TA MIGRACJA
--
-- Dodaje kolumne `zrodlo`. Od tej chwili kazdy nowy punkt mowi, z ktorej
-- definicji pochodzi, a `stan_kapitalu` potrafi policzyc szczyt po WLASCIWEJ
-- serii (patrz `_ZASIEW_NIE_NALEZY` w tym module).
--
-- CZEGO TA MIGRACJA NIE ROBI, SWIADOMIE
--
--  1. NIE ZGADUJE ZRODLA WIERSZY ISTNIEJACYCH. Zostaja z `zrodlo IS NULL`,
--     czyli "wiersz sprzed tej migracji, zrodla nie znamy", i wchodza do
--     KAZDEJ miary. Dopisanie im zrodla po heurystyce (`equity = 100000`,
--     `realized_cum = 0`) bylo by zamienieniem niewiedzy w potwierdzenie -
--     a wykluczenie ich z miary byloby powtorzeniem bledu migracji 0031,
--     ktora dodala kolumne `nav` bez danych: szczyt liczony od dnia migracji
--     jest zawsze mniejszy albo rowny prawdziwemu, czyli obsuniecie wychodzi
--     ZANIZONE, i to jednostronnie.
--  2. NIE USUWA zasianego punktu z produkcji. Usuniecie wiersza krzywej jest
--     zmiana HISTORII, od ktorej licza sie szczyt i obsuniecie, czyli krokiem
--     nieodwracalnym - wymaga kopii `exec.equity_point` dla equity/demo,
--     a brak kopii musi taka operacje PRZERWAC, nie tylko opatrzyc
--     ostrzezeniem. To osobna decyzja wlasciciela i osobne polecenie.
--     Naprawa mechanizmu jej nie potrzebuje: szczyt serii pisanej z ksiag
--     nie widzi punktow zasianych, a nowych `seed_equity` dla equity nie
--     tworzy.
--  3. NIE WYMUSZA `NOT NULL` ani wartosci domyslnej. Domysl wpisywalby
--     jednemu z trzech pisarzy zrodlo drugiego przy pierwszym INSERCIE, ktory
--     o kolumnie zapomni - czyli utrwalalby nieprawde zamiast braku.
--
-- ZBIOR DOPUSZCZALNYCH WARTOSCI stoi w `CHECK`, a nie w enumie: enum wymaga
-- osobnej migracji na kazda nowa wartosc i `ALTER TYPE` nie dziala w jednej
-- transakcji z uzyciem tej wartosci. Nazwy w Pythonie: `stan_kapitalu.ZRODLA`.

BEGIN;

ALTER TABLE exec.equity_point ADD COLUMN IF NOT EXISTS zrodlo text;

ALTER TABLE exec.equity_point DROP CONSTRAINT IF EXISTS equity_point_zrodlo_check;
ALTER TABLE exec.equity_point ADD CONSTRAINT equity_point_zrodlo_check
    CHECK (zrodlo IS NULL
           OR zrodlo IN ('zasiew', 'petla_silnika', 'ksiegi_gieldy'));

COMMENT ON COLUMN exec.equity_point.zrodlo IS
'KTORA DEFINICJA KAPITALU ZAWIERA TEN WIERSZ (migracja 0058).

  zasiew         loop.seed_equity - STALA: START_EQUITY i nav = 1.0 na sztywno.
                 Dla silnika liczacego krzywa SAM jest to legalny poczatek
                 (units == wplaty == equity w chwili zero). Dla serii pisanej
                 z ksiag gieldy jest to punkt z innej definicji i NIE WCHODZI
                 do szczytu - patrz services/engine/stan_kapitalu.py.
  petla_silnika  loop.update_equity - rekonstrukcja z naszego dziennika:
                 wplaty + zrealizowany + niezrealizowany z exec.position.
  ksiegi_gieldy  kapital_gieldy.zapisz_punkt - odczyt z ksiag gieldy.
  NULL           wiersz sprzed migracji 0058, zrodla NIE ZNAMY. Wchodzi do
                 kazdej miary - wykluczenie zanizaloby obsuniecie, dokladnie
                 tak jak brak wstecznego nav po migracji 0031.

JEDEN PISARZ NA SERIE. Ktory - mowi stan_kapitalu.KRZYWA_Z_KSIAG_GIELDY;
dla toru equity obowiazuja KSIEGI GIELDY (decyzja 2026-09-26). Pilnuja tego
loop.seed_equity, loop.update_equity i kapital_gieldy.zapisz_punkt.';

COMMIT;
