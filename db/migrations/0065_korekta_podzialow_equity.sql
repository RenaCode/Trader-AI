-- 0064: slad korekty swiec o podzialy akcji
--
-- ZGODA WLASCICIELA: 2026-09-27, wprost ("wchodzimy"), po przedstawieniu tego,
-- co operacja robi z danymi produkcyjnymi.
--
-- CO TA MIGRACJA ROBI, A CZEGO NIE ROBI
--
-- NIE STAWIA ZNACZNIKA `swiece_skorygowane_o_podzialy`. To jest stwierdzenie
-- o TRESCI swiec, a migracja swiec nie dotyka - postawiony tutaj bylby
-- kłamstwem od pierwszej sekundy, bo `train.py` przestalby blokowac trening
-- na szeregu, ktory nadal ma w sobie trzy skoki x10. Znacznik stawia
-- `services/ingest/korekta_podzialow.py --znacznik`, po instrumencie i tylko
-- temu, dla ktorego przepisanie szeregu FAKTYCZNIE sie udalo.
--
-- ROBI natomiast miejsce na DOWOD. Znacznik to jedno slowo `true` w
-- `ref.instrument.meta` - nie mowi, kiedy korekta zaszla, o jakiej korekcie
-- mowa (`split` czy `all`, a to dwie rozne definicje zwrotu), ile swiec
-- naprawde sie zmienilo ani czy potem nie uniewaznilo go kolejne zdarzenie.
-- Bez tych liczen "potwierdzone" i "wpisane, zeby ruszylo" wygladaja w bazie
-- identycznie - a to jest jedyny sposob, w jaki bramka podzialow moze zawiesc
-- (patrz komentarz przy `ZNACZNIK_KOREKTY_PODZIALOW` w adapterze Alpaki).
--
-- DLACZEGO OSOBNA TABELA, A NIE WIECEJ KLUCZY W `meta`
--
-- `meta` trzyma KONFIGURACJE instrumentu (feed, sektor) - jeden wiersz, stan
-- biezacy, nadpisywany. Tu potrzebna jest HISTORIA zdarzen: przepisanie szeregu,
-- postawienie znacznika, jego uniewaznienie przez kolejny podzial. Wpychanie
-- listy zdarzen do jsonb konczy sie tym, ze ostatnie nadpisuje poprzednie
-- i nie da sie odpowiedziec na pytanie "od kiedy ten szereg jest skorygowany".
--
-- TRZY ZDARZENIA, KTORE TU LADUJA
--
--   swiece_przepisane  - caly szereg (instrument, timeframe) pobrany ponownie
--                        z `adjustment=split` i zapisany W JEDNEJ TRANSAKCJI
--                        razem z tym wierszem. Albo oba, albo nic.
--   znacznik           - `meta->'swiece_skorygowane_o_podzialy' = true`
--                        postawiony; takze w jednej transakcji z tym wierszem.
--   uniewazniony       - `backfill.py` zauwazyl, ze dostawca oddaje dla swiec,
--                        ktore JUZ MAMY, ceny przeskalowane o staly czynnik.
--                        Alpaca koryguje wzgledem DZIS, wiec to znaczy kolejny
--                        podzial: cala zapisana historia stala sie
--                        nieskorygowana i znacznik zostaje zdjety, zeby bramka
--                        w `train.py` znow zatrzymala trening.
--
-- ODWROTNOSC: DROP TABLE market.korekta_swiec;
-- (sam znacznik zdejmuje sie osobno:
--  UPDATE ref.instrument SET meta = meta - 'swiece_skorygowane_o_podzialy'
--   WHERE engine = 'equity';)

BEGIN;

CREATE TABLE market.korekta_swiec (
    id            bigserial   PRIMARY KEY,
    instrument_id integer     NOT NULL REFERENCES ref.instrument(id),
    timeframe     text        NOT NULL,

    -- CHECK, a nie sam komentarz: wartosc spoza tej listy znaczy, ze pojawila
    -- sie czwarta sciezka, o ktorej ta migracja nie wie - i lepiej, zeby zapis
    -- sie wywrocil, niz zeby slad wpadl pod nazwa, ktorej nikt nie szuka.
    zdarzenie     text        NOT NULL
                  CHECK (zdarzenie IN ('swiece_przepisane', 'znacznik',
                                       'uniewazniony')),

    -- O JAKA KOREKTE POPROSZONO DOSTAWCE. `split` i `all` daja INNY szereg
    -- zwrotow (`all` wlicza dywidendy, ktorych zaden rachunek silnika nie
    -- przyjmuje), wiec bez tej kolumny "skorygowane" nie znaczy jednej rzeczy.
    adjustment    text        NOT NULL,

    -- Okno, ktore faktycznie przepisano - a nie okno, o ktore poproszono.
    okno_od       timestamptz,
    okno_do       timestamptz,

    swiec_w_bazie     integer,
    swiec_pobranych   integer,
    -- ILE WIERSZY ZMIENILO WARTOSC. To jest liczba, ktora mowi, czy korekta
    -- w ogole czegos dotknela: zero znaczy, ze w tym szeregu nie bylo podzialu,
    -- i to jest uczciwy wynik, nie awaria.
    swiec_zmienionych integer,

    -- Zaobserwowany czynnik przeskalowania (mediana `nowa/stara`) tam, gdzie
    -- ceny sie zmienily. Dla podzialu 10:1 to okolo 0,1.
    wspolczynnik  numeric(20,10),

    szczegol      text,
    wykonano_at   timestamptz NOT NULL DEFAULT now()
);

-- Pytanie zadaje sie zawsze "co ostatnio zrobiono temu instrumentowi".
CREATE INDEX korekta_swiec_instrument_idx
    ON market.korekta_swiec (instrument_id, timeframe, wykonano_at DESC);

COMMENT ON TABLE market.korekta_swiec IS
'Slad korekty swiec o zdarzenia korporacyjne: przepisanie szeregu, postawienie
znacznika `swiece_skorygowane_o_podzialy` i jego uniewaznienie przez kolejny
podzial. Znacznik w ref.instrument.meta mowi TYLKO "true"; tu stoi, kiedy,
o jaka korekte i ile wierszy sie zmienilo.';

COMMENT ON COLUMN market.korekta_swiec.swiec_zmienionych IS
'Ile wierszy market.candle zmienilo wartosc. Zero znaczy "w tym szeregu nie bylo
podzialu" - korekta nic nie policzyla inaczej, a jej wartoscia jest to, ze
NASTEPNY podzial nie zatruje szeregu.';

-- UPRAWNIENIA: bez nowych nadan, i to jest sprawdzone, a nie zalozone.
-- Migracja 0034 ustawila dla schematu `market` domyslne nadanie
-- SELECT, INSERT, UPDATE dla `trader_silnik` i SELECT dla `trader_czujka`
-- (ALTER DEFAULT PRIVILEGES FOR ROLE trader), a 0013 to samo dla `trader_app`.
-- Tabela powstaje jako `trader`, wiec obejmuja ja te domysly. Skrypt korekty
-- chodzi z poda `trader-api` (tam jest SECRETS_KEY i klucz Alpaki), czyli rola
-- `trader_app`; `backfill.py` w workerze - rola `trader_silnik`. Oba pisza.

COMMIT;
