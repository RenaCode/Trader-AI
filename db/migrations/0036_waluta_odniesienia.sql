-- 0036: druga krzywa kapitału — w walucie, w której właściciel wpłaca i wypłaca
--
-- POWÓD
--
-- Warunek właściciela brzmi „wpłacę 10 tysięcy i nie chcę, żeby rachunek
-- poszedł na minus". Cały system mierzy jednak w `quote_ccy`, czyli w USDC,
-- a rail fiatowy podmiotu unijnego to EUR. `min_equity` z 0028 jest progiem
-- w USDC, `equity` w exec.equity_point jest w USDC i wszystkie procenty liczą
-- się z liczb w USDC.
--
-- Znaczy to, że stan
--
--     kill-switch zielony, wszystkie limity w normie, rachunek 10 000 USDC
--
-- jest W PEŁNI ZGODNY ze stanem
--
--     właściciel ma o kilka procent mniej, niż wpłacił.
--
-- Bezpiecznik jest z konstrukcji ślepy na tę stratę, bo mierzy ją w walucie,
-- w której jej nie widać. To nie jest hipoteza. Zmierzone na USDCEUR z
-- api.bybit.eu, 453 świece dzienne 2025-06-13 … 2026-09-08:
--
--     min 0,8318   max 0,8806   zakres 5,87%
--
-- przy zmierzonym wyniku strategii około 2,9% rocznie. Ruch kursu o 5% w roku
-- — całkowicie zwyczajny — jest większy niż dwuletnia przewaga strategii.
--
-- CZEGO TA MIGRACJA NIE ROBI
--
-- NIE ZMIENIA TEGO, W CZYM LICZY RISK KERNEL. Ani jeden limit, próg ani
-- kolumna, z której czyta `services/engine/risk.py`, nie jest tu ruszona.
-- Zmiana jednostki bezpiecznika byłaby zmianą jego ZACHOWANIA, a nie dodaniem
-- miary — i jest decyzją właściciela, nie skutkiem ubocznym migracji. Tutaj
-- powstaje wyłącznie DRUGA LICZBA, pokazywana obok pierwszej.
--
-- DLACZEGO KURS MUSI BYĆ ZAPISYWANY W CZASIE, A NIE ODCZYTYWANY „TERAZ"
--
-- Przeliczenie historycznej krzywej dzisiejszym kursem pokazałoby przeszłość,
-- której nie było: rachunek sprzed pół roku wyglądałby tak, jakby właściciel
-- wtedy sprzedał USDC po dzisiejszym kursie. Przy 5,9% zakresu jest to błąd
-- większy niż dwuletni wynik strategii — czyli druga krzywa kłamałaby dokładnie
-- o tyle, o ile miała ostrzegać.
--
-- To ten sam błąd, przed którym broni bitemporalność w `feat.as_of()`: liczba
-- musi być przypisana do chwili, w której BYŁA ZNANA, a nie do chwili, w której
-- ktoś na nią patrzy. Dlatego tabela ma dwa znaczniki czasu i oba są potrzebne.

BEGIN;

-- ------------------------------------------------------ kurs w czasie

CREATE TABLE IF NOT EXISTS market.fx_rate (
    -- Waluta, w której mierzy silnik. Kolumna, a nie stała 'USDC': jeżeli
    -- kiedyś dojdzie tor rozliczany w USDT, dwie serie muszą dać się odróżnić,
    -- a nie nadpisać po cichu.
    base_ccy    text        NOT NULL,
    -- Waluta odniesienia właściciela: 'EUR', 'PLN'.
    quote_ccy   text        NOT NULL,

    -- CZAS, DO KTÓREGO KURS SIĘ ODNOSI — i zarazem najwcześniejsza chwila,
    -- w której ten kurs był ZNANY.
    --
    -- Dla świecy dziennej jest to jej KONIEC, nie początek. Zamknięcie świecy
    -- zaczynającej się 2026-09-06 00:00 jest znane dopiero 2026-09-07 00:00;
    -- ostemplowanie go początkiem świecy wstawiłoby do historii cenę z
    -- przyszłości i przeliczyło wczorajszą krzywą kursem, którego wczoraj nikt
    -- nie znał. Ten sam błąd co świeca niezamknięta w potoku danych
    -- (tests/test_swieca_zamknieta.py), tylko trudniejszy do zauważenia, bo
    -- kurs stablecoina zmienia się wolno.
    event_time  timestamptz NOT NULL,

    -- CHECK, bo kurs zerowy lub ujemny nie jest kursem, a przeliczenie przez
    -- niego nie wywraca się — daje po cichu zero i wygląda jak strata całości.
    rate        numeric(28,12) NOT NULL CHECK (rate > 0),

    -- Skąd. Pełny opis, nie sam kod giełdy: 'bybit.eu:USDCEUR:spot:1d' i
    -- 'bybit.eu:USDCEUR:spot:ticker' to dwie różne jakości tej samej liczby
    -- i po roku nikt tego nie odtworzy z pamięci.
    source      text        NOT NULL,

    -- Kiedy MY się o nim dowiedzieliśmy. Drugi wymiar czasu: pozwala
    -- odpowiedzieć na pytanie „co system widział wtedy", a nie tylko „jak było
    -- naprawdę". Bez niego backfill wykonany dziś byłby nieodróżnialny od
    -- odczytu sprzed pół roku.
    ingested_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (base_ccy, quote_ccy, event_time)
);

COMMENT ON TABLE market.fx_rate IS
'Kurs waluty pomiaru (USDC) do waluty odniesienia właściciela (EUR/PLN), ZAPISANY
W CZASIE. Służy WYŁĄCZNIE drugiej krzywej kapitału i raportowaniu — risk kernel
nie czyta tej tabeli i nie ma czytać: zmiana jednostki bezpiecznika jest decyzją
właściciela, nie skutkiem ubocznym istnienia kursu w bazie.';

COMMENT ON COLUMN market.fx_rate.event_time IS
'Chwila, w której kurs BYŁ ZNANY. Dla świecy dziennej to jej koniec, nie
początek — zamknięcie ostemplowane początkiem świecy jest ceną z przyszłości.';

COMMENT ON COLUMN market.fx_rate.ingested_at IS
'Kiedy wiersz trafił do bazy. Razem z event_time daje dwa wymiary czasu: „jak
było" i „co wiedzieliśmy". Backfill wykonany po fakcie ma być rozpoznawalny.';

CREATE INDEX IF NOT EXISTS fx_rate_para_czas_idx
    ON market.fx_rate (base_ccy, quote_ccy, event_time DESC);

-- Uprawnienia: schemat `market` jest objęty pętlą nadań z 0034 (silnik zapisuje,
-- czujka czyta) i domyślnym nadaniem dla roli API z 0013. Nowa tabela dziedziczy
-- je przez ALTER DEFAULT PRIVILEGES, więc świadomie nie ma tu ani jednego GRANT.
-- Dozorca ryzyka i próba pożarowa NIE dostają dostępu i to jest zamierzone:
-- nadzorca, który zaczyna czytać kurs, prędzej czy później zacznie nim liczyć
-- próg — a to jest właśnie ta zmiana zachowania bezpiecznika, której ta migracja
-- nie robi.

-- --------------------------------------------- waluta odniesienia jako USTAWIENIE

-- DLACZEGO TABELA, A NIE STAŁA W KODZIE
--
-- Właściciel może liczyć w PLN, nie w EUR. Waluta odniesienia wpisana w kod
-- oznaczałaby, że zmiana sposobu, w jaki człowiek patrzy na własne pieniądze,
-- wymaga wdrożenia. Tabela z jednym wierszem jest tu tańsza niż zmienna
-- środowiskowa, bo zmienną trzeba by trzymać zgodną w charcie, w cronie i w API
-- — czyli w trzech miejscach, które rozjeżdżają się po cichu.
CREATE TABLE IF NOT EXISTS app.waluta_odniesienia (
    -- Wiersz jest z definicji jeden. Klucz główny na stałej `true` z CHECK-iem
    -- wymusza to po stronie bazy; bez tego drugi wiersz nie byłby awarią, tylko
    -- niedeterminizmem — dwie krzywe, zależnie od tego, którą wybrał planner.
    jedyny      boolean     PRIMARY KEY DEFAULT true CHECK (jedyny),

    -- Kod ISO waluty, w której właściciel wpłaca i wypłaca.
    kod         text        NOT NULL CHECK (kod ~ '^[A-Z]{3}$'),

    -- Waluta, w której mierzy silnik. W bazie, a nie w kodzie, bo to ona
    -- decyduje o tym, której pary szukamy — i bo rozjazd między nią a
    -- `ref.instrument.quote_ccy` musi dać się zobaczyć zapytaniem.
    kod_mierzony text       NOT NULL DEFAULT 'USDC' CHECK (kod_mierzony ~ '^[A-Z]{3,5}$'),

    zmieniono   timestamptz NOT NULL DEFAULT now(),
    zmienil     bigint      REFERENCES app.user(id)
);

COMMENT ON TABLE app.waluta_odniesienia IS
'Waluta, w której właściciel liczy swój wynik. NIE jest walutą, w której liczy
risk kernel — ten dalej mierzy w USDC. Zmiana tego wiersza zmienia drugą krzywą
i raport, i NIC WIĘCEJ.';

-- EUR jako wartość początkowa, bo rail fiatowy podmiotu unijnego to EUR i to
-- w tej walucie właściciel faktycznie przelewa. Wiersz istnieje od razu, bo
-- „brak ustawienia" i „ustawione na EUR" to dwa różne stany, a raport, który
-- milczy z powodu pustej tabeli, wygląda dokładnie jak raport, który milczy
-- z powodu braku kursu.
INSERT INTO app.waluta_odniesienia (jedyny, kod, kod_mierzony)
VALUES (true, 'EUR', 'USDC')
ON CONFLICT (jedyny) DO NOTHING;

-- Odczyt dla procesów, które budują drugą krzywą. Zapis zostaje przy roli API
-- (0013, ALTER DEFAULT PRIVILEGES na schemacie app) — ustawienie zmienia
-- człowiek przez interfejs, nie silnik w trakcie cyklu.
--
-- Dozorca ryzyka NIE dostaje nawet odczytu. Nadzorca, który wie, w jakiej
-- walucie właściciel liczy, prędzej czy później zacznie w niej liczyć próg —
-- a to jest właśnie ta zmiana zachowania bezpiecznika, której ta migracja
-- świadomie nie robi.
GRANT SELECT ON app.waluta_odniesienia TO trader_silnik, trader_czujka;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON app.waluta_odniesienia
    FROM trader_silnik, trader_czujka;

COMMIT;
