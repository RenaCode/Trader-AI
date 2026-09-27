-- 0065: dziennik LIKWIDACJI po kill-switchu — co zamknięto, czego NIE i dlaczego.
--
-- CO SIĘ ZMIENIA W ZACHOWANIU
--
-- Do dziś kill-switch robił dwie rzeczy: wstrzymywał WEJŚCIA (`loop.run_once`)
-- i zdejmował niewypełnione wejścia z arkusza (`gielda_exec.anuluj_wejscia`).
-- Otwarta ekspozycja ZOSTAWAŁA, a wyjścia szły jako `PostOnly`, czyli mogły się
-- nie wypełnić — na spadającym rynku dokładnie wtedy, gdy trzeba wyjść.
-- Zmierzone 2026-09-27: 3 z 6 zleceń z limitem odrzucone przez PostOnly.
--
-- Właściciel zatwierdził 2026-09-27 likwidację ZLECENIEM RYNKOWYM, po
-- przedstawieniu kosztu: poślizg takera jest NIEOGRANICZONY i NIEZMIERZONY
-- (`POSLIZG_TAKER` ma n=0), a ścieżki `execution='gielda' AND
-- order_type='market'` nie użyto w tym systemie ANI RAZU.
--
-- DLACZEGO OSOBNA TABELA, A NIE SAM `exec.order`
--
-- Trzy pytania, na które `exec.order` nie odpowiada i nie może:
--
--   1. CZEGO NIE ZAMKNIĘTO I DLACZEGO. Pozycja, której zlecenia nie wysłano
--      (pył pod minimum giełdy, niekompletne reguły), nie ma wiersza w
--      `exec.order` — więc nie ma śladu ŻADNEGO. Cisza znaczyłaby wtedy
--      jednocześnie „nie było czego zamykać" i „likwidacja pominęła pozycję",
--      czyli nie znaczyłaby nic. To jest ta sama wada, którą 0041 naprawiło
--      po stronie wejść (`journal.pominiecie`).
--
--   2. CZY TO JUŻ ROBILIŚMY. Kill-switch wyzwala się wielokrotnie, a cykl
--      chodzi co 300 s. Dwa razy zlikwidowana pozycja długa to pozycja KRÓTKA
--      — objaw nie wygląda na awarię, tylko na strategię. `UNIQUE
--      (killswitch_event_id, position_id)` zamienia to pytanie w ograniczenie
--      bazy: `INSERT ... ON CONFLICT DO NOTHING RETURNING id` bez zwróconego
--      wiersza znaczy „ta para już była" i jest to rozstrzygnięcie ATOMOWE,
--      niezależne od tego, czy giełda pamięta nasz `orderLinkId`.
--
--   3. JAKI BYŁ POŚLIZG TAKERA. Do pomiaru potrzebne są trzy liczby Z CHWILI
--      PRZED WYSŁANIEM: cena odniesienia, bid i ask. `exec.order` ma
--      `limit_price`, a zlecenie rynkowe limitu NIE MA — wpisanie tam czegoś
--      byłoby zapisaniem nieprawdy o tym, co giełda dostała.
--
-- DLACZEGO `bid` I `ask` OSOBNO, A NIE SAMA CENA ODNIESIENIA
--
-- `services/engine/poslizg.py` mówi wprost, czego mu brakuje: „Brakuje
-- czwartej — ceny mid z arkusza w chwili składania zlecenia. Bez niej nie da
-- się rozdzielić poślizgu na spread i na ruch rynku w czasie oczekiwania."
-- Dla zleceń z limitem tej liczby nie było skąd wziąć. Dla likwidacji jest:
-- arkusz czytamy tuż przed wysłaniem, więc zapisanie obu stron kosztuje ZERO
-- dodatkowych zapytań i rozdziela dwa składniki poślizgu, których suma jest
-- dziś jedyną mierzalną wielkością.
--
-- DLACZEGO W SCHEMACIE `exec`, A NIE `risk`
--
-- To jest zapis EGZEKUCJI — co wysłaliśmy, co weszło, po ile. Schemat `risk`
-- trzyma limity i zatrzymania, czyli decyzje o tym, czy wolno handlować;
-- dołożenie tam tabeli zapisywanej przez silnik oznaczałoby, że proces
-- nadzorowany pisze do schematu nadzorcy (0034). Nadania dla `exec` już
-- rozstrzygają to poprawnie: silnik pisze, czujka czyta, dozorca nie widzi.

CREATE TABLE exec.likwidacja (
    id                  bigserial   PRIMARY KEY,
    -- PO CZYM LIKWIDACJA JEST IDEMPOTENTNA. Zdarzenie, nie „dzień" i nie
    -- „cykl": zwolnienie kill-switcha i jego ponowne wystrzelenie to nowa
    -- decyzja o wyjściu z ryzyka i ma prawo złożyć nowe zlecenie.
    killswitch_event_id bigint      NOT NULL REFERENCES risk.killswitch_event(id),
    position_id         bigint      NOT NULL REFERENCES exec.position(id),
    engine              ref.engine  NOT NULL,
    mode                ref.trade_mode NOT NULL,
    instrument_id       integer     NOT NULL REFERENCES ref.instrument(id),
    -- Wiersz zlecenia, gdy je wysłano. NULL znaczy „nie wysłaliśmy nic" i jest
    -- wtedy jedyną prawdą — patrz `stan = 'pominieta'`.
    order_id            bigint      REFERENCES exec.order(id),
    client_order_id     text        NOT NULL,
    strona              text        NOT NULL CHECK (strona IN ('buy','sell')),
    ilosc_pozycji       numeric(28,12) NOT NULL,
    ilosc_zlecona       numeric(28,12),
    ilosc_wypelniona    numeric(28,12),
    -- CENA ODNIESIENIA SPRZED WYSŁANIA. Dla sprzedaży `bid`, dla kupna `ask`:
    -- to jest cena, po której taker MOŻE liczyć na wykonanie w chwili wysyłki,
    -- więc odległość od niej jest dokładnie tym, co płacimy za natychmiastowość.
    cena_odniesienia    numeric(28,12),
    zrodlo_odniesienia  text,
    bid                 numeric(28,12),
    ask                 numeric(28,12),
    arkusz_at           timestamptz,
    cena_wypelnienia    numeric(28,12),
    prowizja            numeric(28,12),
    prowizja_ccy        text,
    -- SZEŚĆ STANÓW, NIE DWA. „Nie zamknięto" ma trzy różne przyczyny i każda
    -- wymaga innej reakcji człowieka: pominięta (nie wysłaliśmy), odrzucona
    -- (giełda odmówiła), nieznana (odpowiedź nie dotarła — pozycja MOŻE być
    -- zamknięta). Zlanie ich w jedno zabrałoby tej tabeli cały sens.
    stan                text        NOT NULL
        CHECK (stan IN ('zlecona','wypelniona','czesciowa','odrzucona',
                        'nieznana','pominieta')),
    powod               text,
    zlecona_at          timestamptz NOT NULL DEFAULT now(),
    rozstrzygnieta_at   timestamptz,
    UNIQUE (killswitch_event_id, position_id)
);

COMMENT ON TABLE exec.likwidacja IS
    'Jedna próba zamknięcia JEDNEJ pozycji zleceniem rynkowym po kill-switchu. '
    'UNIQUE (killswitch_event_id, position_id) jest ograniczeniem '
    'idempotencji, nie indeksem dla wygody: druga próba sprzedaje pozycję, '
    'której już nie ma, czyli otwiera ją w drugą stronę.';

COMMENT ON COLUMN exec.likwidacja.cena_odniesienia IS
    'Cena Z ARKUSZA SPRZED WYSŁANIA: bid przy sprzedaży, ask przy kupnie. '
    'Razem z exec.fill.price i ilością daje PIERWSZY pomiar poślizgu takera '
    '(POSLIZG_TAKER ma dziś n=0). Dla zlecenia z limitem taki pomiar nie '
    'istnieje: limit wypełnia się po cenie limitu z definicji.';

COMMENT ON COLUMN exec.likwidacja.stan IS
    'zlecona | wypelniona | czesciowa | odrzucona | nieznana | pominieta. '
    '„pominieta" znaczy NIE WYSŁALIŚMY NIC i `powod` mówi dlaczego — '
    'to jedyny ślad po ekspozycji, której likwidacja nie ruszyła.';

-- Odczyt po zdarzeniu: „co zrobiła likwidacja dla tego zatrzymania".
CREATE INDEX likwidacja_zdarzenie_idx ON exec.likwidacja (killswitch_event_id);

-- ---------------------------------------------------------------- nadania
--
-- `ALTER DEFAULT PRIVILEGES ... IN SCHEMA exec` z 0034 nadaje już silnikowi
-- SELECT/INSERT/UPDATE, a czujce SELECT — dla tabel tworzonych przez rolę
-- `trader`. Wypisujemy je mimo to WPROST, bo nadanie wynikające z domyślnych
-- uprawnień znika przy pierwszej zmianie tamtej migracji i nikt tego nie
-- zauważy: proces przestanie zapisywać likwidację, a brak wiersza wygląda
-- identycznie jak „nie było czego likwidować".
GRANT SELECT, INSERT, UPDATE ON exec.likwidacja TO trader_silnik;
GRANT USAGE, SELECT ON SEQUENCE exec.likwidacja_id_seq TO trader_silnik;
GRANT SELECT ON exec.likwidacja TO trader_czujka;

-- DOZORCA I PRÓBA NIE DOSTAJĄ NICZEGO, i to jest decyzja, a nie przeoczenie.
-- 0034 daje im SELECT wyłącznie na `exec.equity_point`, `exec.position`
-- i `exec.order` — czyli na to, na czym opierają werdykt. Dziennik likwidacji
-- nie jest przesłanką żadnego limitu; nadzorca, który go czyta, zaczyna
-- oceniać JAKOŚĆ egzekucji, a to jest rola silnika i pomiaru poślizgu.
REVOKE ALL ON exec.likwidacja FROM trader_dozorca, trader_proba;
