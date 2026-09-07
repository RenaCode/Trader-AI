-- 0031: wpłaty i wypłaty jako osobny byt, plus jednostka odporna na nie
--
-- POWÓD
--
-- Kapitał był liczony jako STAŁA plus wynik handlu:
--
--     equity = START_EQUITY + realized + unrealized     (loop.py)
--
-- Nie było pojęcia wpłaty ani wypłaty. Na rachunku papierowym to poprawne —
-- kapitał startowy jest stały i nikt nic nie dokłada. Przy realnych środkach
-- każde dołożenie wygląda jak zysk, a wypłata jak strata, i uderza to
-- jednocześnie w cztery mechanizmy:
--
--   * `peak` skacze przy wpłacie, więc obsunięcie liczy się od zawyżonego szczytu
--   * `day_ago` przy wypłacie pokazuje stratę dzienną, której nie było
--   * `min_equity` — próg kapitału z docs/05 — przestaje znaczyć to, co miał
--   * krzywa kapitału miesza wynik strategii z ruchami gotówki
--
-- To jest luka PRZED wpłatą realnych środków, nie po niej.
--
-- ROZWIĄZANIE: JEDNOSTKA UCZESTNICTWA
--
-- Rozdzielamy dwie rzeczy, które do tej pory były jedną liczbą:
--
--   equity  — ile pieniędzy jest na rachunku. Rośnie od wpłat, spada od wypłat.
--             Tym mierzy się wielkość pozycji i próg kapitału, bo one dotyczą
--             PIENIĘDZY.
--
--   nav     — wartość jednostki. Startuje od 1.0 i zmienia się WYŁĄCZNIE od
--             wyniku handlu. Wpłata kupuje jednostki po bieżącym nav, wypłata
--             je umarza — nav się przy tym nie rusza. Tym mierzy się obsunięcie
--             i stratę dzienną, bo one dotyczą UMIEJĘTNOŚCI.
--
-- Jest to ten sam mechanizm, którym fundusze liczą wynik niezależnie od
-- napływów. Bez niego każdy limit oparty na obsunięciu mierzy sumę dwóch
-- rzeczy i nie potrafi ich rozróżnić.
--
-- CO SIĘ NIE ZMIENIA
--
-- Na rachunku demonstracyjnym przepływ jest jeden — kapitał startowy — więc
-- nav jest dokładnie proporcjonalne do equity i wszystkie zmierzone dotąd
-- wyniki zostają bez zmian. Symulacja walk-forward nie zna przepływów w ogóle
-- i nie musi: tam nav == equity/START_EQUITY z definicji.

BEGIN;

CREATE TABLE exec.cash_flow (
    id         bigserial   PRIMARY KEY,
    engine     ref.engine  NOT NULL,
    mode       ref.trade_mode NOT NULL,
    event_time timestamptz NOT NULL DEFAULT now(),
    -- Dodatnia = wpłata, ujemna = wypłata. Jedna kolumna zamiast kwoty i typu:
    -- dwie kolumny, które muszą się zgadzać, prędzej czy później się rozjadą.
    amount     numeric(28,12) NOT NULL CHECK (amount <> 0),
    -- Ile jednostek powstało (wpłata) albo zniknęło (wypłata). Liczone przy
    -- zapisie z bieżącego nav; trzymane, bo późniejsze przeliczenie wymagałoby
    -- odtworzenia nav sprzed miesięcy.
    units      numeric(28,12) NOT NULL,
    nav        numeric(28,12) NOT NULL,   -- wartość jednostki w chwili przepływu
    note       text,
    created_by bigint REFERENCES app.user(id)
);

CREATE INDEX cash_flow_engine_time_idx
    ON exec.cash_flow (engine, mode, event_time DESC);

COMMENT ON TABLE exec.cash_flow IS
'Wpłaty i wypłaty. KAŻDA zmiana kapitału, która NIE POCHODZI z handlu, musi być
tutaj — inaczej obsunięcie i strata dzienna mierzą ruch gotówki jako wynik
strategii.';

-- Wartość jednostki i liczba jednostek w każdym punkcie krzywej. Bez nich
-- historia nie da się odtworzyć: sam `equity` nie mówi, ile z niego pochodzi
-- z handlu, a ile z wpłat.
ALTER TABLE exec.equity_point ADD COLUMN IF NOT EXISTS nav numeric(28,12);
ALTER TABLE exec.equity_point ADD COLUMN IF NOT EXISTS units numeric(28,12);
ALTER TABLE exec.equity_point ADD COLUMN IF NOT EXISTS wplaty_netto numeric(28,12);

COMMENT ON COLUMN exec.equity_point.nav IS
'Wartość jednostki uczestnictwa. Startuje od 1.0, zmienia się WYŁĄCZNIE od wyniku
handlu. Obsunięcie i strata dzienna liczą się z niej, nie z equity.';

-- Kapitał startowy rachunku demonstracyjnego jako pierwszy przepływ.
--
-- Bez tego wiersza suma przepływów wynosi zero, a kapitał wyszedłby zerowy przy
-- pierwszym przeliczeniu. Kwota musi zgadzać się z risk_rules.START_EQUITY —
-- pilnuje tego test, bo rozjazd nie objawiłby się awarią, tylko innym kapitałem
-- niż ten, na którym mierzono wynik.
INSERT INTO exec.cash_flow (engine, mode, event_time, amount, units, nav, note)
SELECT e.engine, m.mode, timestamptz '2026-08-27 00:00:00+00', 100000, 100000, 1.0,
       'Kapitał startowy rachunku demonstracyjnego (migracja 0031)'
FROM (VALUES ('crypto'::ref.engine), ('equity'::ref.engine)) AS e(engine),
     (VALUES ('demo'::ref.trade_mode)) AS m(mode)
ON CONFLICT DO NOTHING;

-- Próg kapitału może być teraz wyrażony jako część WPŁAT NETTO.
--
-- Wartość kwotowa przestaje działać, gdy właściciel wypłaci część środków:
-- rachunek spada zgodnie z jego wolą, a próg zostaje w miejscu i uruchamia
-- kill-switch bez powodu. Ułamek wpłat przesuwa się razem z nimi i wyraża to,
-- o co naprawdę chodzi: "nie strać więcej niż tyle z tego, co włożyłem".
COMMENT ON COLUMN risk.limit_def.unit IS
'Jednostka wartości limitu:
  pct        - procent (strata dzienna, obsunięcie — liczone z nav, nie z equity)
  pct_equity - procent KAPITAŁU w chwili oceny
  pct_wplat  - procent WPŁAT NETTO; przesuwa się razem z wypłatami, więc próg
               kapitału nie uruchamia się od tego, że właściciel wypłacił swoje
  quote_ccy  - kwota w walucie kwotowanej
  count      - liczba sztuk';

COMMIT;
