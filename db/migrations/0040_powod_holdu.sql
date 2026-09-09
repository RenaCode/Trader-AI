-- 0040: powód wstrzymania się od ruchu i progi obowiązujące przy decyzji.
--
-- PO CO. Na 818 decyzji produkcyjnych 782 to `hold`, a dziennik nie mówi
-- o żadnej z nich, DLACZEGO. Najgorszy przypadek wyglądał tak: uzasadnienie
-- „Wynik złożony -0,644 przy pewności 70%" przy progu wejścia 0,278 — wynik dwa
-- i pół raza ponad progiem, `risk_verdict = 'allow'`, `risk_reason` puste
-- i decyzja o nierobieniu niczego. Prawdziwym powodem było to, że short na
-- instrumencie spotowym jest niewykonalny (`loop.py`, gałąź `krotka_pozycja`),
-- ale ta gałąź podmienia akcję na `hold` PRZED gałęzią ryzyka, więc do dziennika
-- nie trafia po niej ani jeden bit.
--
-- Klasyfikacja 782 holdów przez odtworzenie wyniku i progu (zapytanie niżej,
-- policzone na produkcji 2026-09-09):
--
--     665  słaby wynik            |wynik| < próg wejścia
--      60  trzyma pozycję         pozycja otwarta, wynik ponad progiem wyjścia
--      57  short niewykonalny     wejście krótkie na instrumencie bez shortów
--       0  za niska pewność       ten warunek nigdy nie był wiążący
--
-- Te 57 to najczęstsza REALNA przyczyna blokowania wejść i jedyna, której
-- z dziennika nie dało się odczytać wprost.
--
-- PROGI ZAPISUJEMY PRZY DECYZJI, a nie doklejamy złączeniem do
-- `learn.model_version.params`. Wersja modelu jest bytem o krótszym życiu niż
-- decyzja: `services/ingest/purge_demo.py` kasuje całą tabelę `learn.model_version`
-- przy czyszczeniu demo, a `journal.decision` jest z założenia niemutowalna
-- i wieczna. Po takim czyszczeniu pytanie „czy ten wynik był ponad progiem"
-- przestawało mieć odpowiedź. Próg jest częścią decyzji, nie częścią modelu.

ALTER TABLE journal.decision
    ADD COLUMN IF NOT EXISTS powod_holdu  text,
    ADD COLUMN IF NOT EXISTS prog_wejscia double precision,
    ADD COLUMN IF NOT EXISTS prog_wyjscia double precision;

-- Zbiór zamknięty. Powód spoza listy znaczy, że w pętli decyzyjnej pojawiła się
-- gałąź, o której dziennik nie wie — i lepiej, żeby wywróciła się transakcja,
-- niż żeby do tabeli wpadł napis, którego nikt później nie policzy.
ALTER TABLE journal.decision
    ADD CONSTRAINT decision_powod_holdu_chk CHECK (
        powod_holdu IS NULL OR powod_holdu IN (
            'slaby_wynik', 'niska_pewnosc', 'short_niewykonalny',
            'trzyma_pozycje', 'wstrzymanie_ryzykiem'));

-- Powód TYLKO dla holdu. Wypełniony przy `enter_long` znaczyłby, że pole opisuje
-- coś innego, niż mówi jego nazwa - a to jest dokładnie ta klasa cichego
-- rozjazdu, którą ta migracja zamyka.
ALTER TABLE journal.decision
    ADD CONSTRAINT decision_powod_tylko_dla_holdu_chk CHECK (
        powod_holdu IS NULL OR action = 'hold');

COMMENT ON COLUMN journal.decision.powod_holdu IS
    'Która gałąź wyboru akcji zamieniła zamiar na bezczynność. '
    'NULL przy action=hold znaczy „decyzja sprzed migracji 0040, powodu nie dało '
    'się odtworzyć", a nie „bez powodu".';
COMMENT ON COLUMN journal.decision.prog_wejscia IS
    'Próg |wyniku| otwierający pozycję, OBOWIĄZUJĄCY W CHWILI DECYZJI. '
    'Centyl rozkładu z panelu treningowego albo stała awaryjna loop.py, gdy '
    'nie było aktywnej wersji modelu.';
COMMENT ON COLUMN journal.decision.prog_wyjscia IS
    'Próg |wyniku|, poniżej którego otwarta pozycja jest zamykana.';


-- ------------------------------------------------------------------ BACKFILL
--
-- `journal.decision` ma wyzwalacz `decision_immutable` (migracja 0005), więc
-- zwykły UPDATE się wywala. Zdejmujemy go na czas JEDNEGO, opisanego tu kroku
-- i zakładamy z powrotem. Cały plik idzie w jednej transakcji (db/migrate.py),
-- więc błąd w środku cofa także zdjęcie wyzwalacza.
--
-- Backfill dotyczy 818 wierszy z okna 2026-08-27..2026-09-09 i NIE JEST
-- zgadywaniem — poniżej jest wypisane, co dokładnie z czego wynika i gdzie
-- kończy się to, co da się ustalić.

ALTER TABLE journal.decision DISABLE TRIGGER decision_immutable;


-- KROK 1: PROGI. Tylko dla wierszy, które mają wersję modelu.
--
-- Dla nich próg jest zapisany wprost w `params` i odczyt jest deterministyczny
-- (sprawdzone: 0 wierszy z wersją modelu i bez `enter_threshold`/`exit_threshold`).
--
-- 42 WIERSZY Z 27.08 ZOSTAJE Z NULL-em, ŚWIADOMIE. Mają `model_version_id IS NULL`,
-- bo powstały w oknie 09:20–11:31 UTC, między pierwszym commitem silnika
-- a poprawką, która zaczęła zapisywać wersję modelu do dziennika. Kuszące jest
-- wpisać im stałe z kodu (loop.py: 0,25 i 0,12), ale to byłoby NIEPRAWDĄ:
-- w tym samym oknie istniały aktywne wersje modelu z progami 0,2399, 0,2378
-- i 0,2359, a `active_model()` sięga po stałe tylko wtedy, gdy aktywnej wersji
-- NIE MA. Której z nich użyto o danej godzinie, nie da się dziś rozstrzygnąć —
-- `promoted_at` bywa ustawione także dla wersji o statusie `candidate`, więc
-- historia promocji nie jest wiarygodna. Pusta kolumna jest tu uczciwsza
-- od liczby.
UPDATE journal.decision d
   SET prog_wejscia = (mv.params->>'enter_threshold')::double precision,
       prog_wyjscia = (mv.params->>'exit_threshold')::double precision
  FROM learn.model_version mv
 WHERE mv.id = d.model_version_id
   AND d.prog_wejscia IS NULL
   AND mv.params ? 'enter_threshold'
   AND mv.params ? 'exit_threshold';


-- KROK 2: POWÓD HOLDU. Odtwarzany z samego dziennika i z progów modelu.
--
-- Kolejność warunków jest kolejnością gałęzi w `loop.py` — inna kolejność dałaby
-- inne etykiety dla tych samych wierszy.
--
-- WYNIK ODTWARZAMY DWOMA WZORAMI, bo reguła zmieniła się w trakcie okna
-- (commit „brakujacy sygnal byl liczony jako GLOS NA NEUTRALNOSC"): wcześniej
-- wynik był SUMĄ wkładów, po zmianie sumą wkładów podzieloną przez masę wag
-- faktycznie obecnych. Granicę epok znaczy `pokrycie_sygnalow`: NULL = wiersz
-- sprzed migracji 0039, czyli sprzed renormalizacji. To ta sama reguła, którą
-- utrwala tests/test_pokrycie_sygnalow.py::test_wynik_DA_SIE_ODTWORZYC_z_samego_dziennika.
--
-- CZY TO ZALEŻY OD `ref.instrument.kind`? NIE — i to jest istotne, bo `kind`
-- jest stanem BIEŻĄCYM, nie migawką, więc zmiana rodzaju instrumentu zmieniałaby
-- wstecz odpowiedź o starą decyzję. Etykiety `short_niewykonalny` nie stawiamy
-- na podstawie rodzaju instrumentu, tylko na podstawie tego, że wiersz W OGÓLE
-- JEST HOLDEM: przy `risk_verdict='allow'`, bez otwartej pozycji i z wynikiem
-- ponad obydwoma progami jedyną gałęzią w kodzie, która produkuje `hold`, jest
-- blokada krótkiej pozycji. Gdyby instrument dopuszczał shorty, ten sam wiersz
-- miałby `action='enter_short'`. Sprawdzone na produkcji: wszystkie 57 takich
-- wierszy ma wynik UJEMNY, ani jednego dodatniego — czyli ani jednego, którego
-- ta gałąź by nie tłumaczyła.
--
-- STAŁE 0,25 I 0,12 W COALESCE dotyczą wyłącznie 42 wierszy bez wersji modelu
-- i NIE ROZSTRZYGAJĄ ich etykiety. Wszystkie 30 tamtejszych holdów bez pozycji
-- ma |wynik| ≤ 0,2294, czyli poniżej i stałej z kodu (0,25), i najniższego progu
-- jakiejkolwiek wersji modelu istniejącej w tym oknie (0,2359). Etykieta
-- `slaby_wynik` trzyma się więc przy każdym kandydacie na próg — inaczej niż
-- sama liczba progu, której dlatego nie zapisujemy (krok 1). Z tego samego
-- powodu nie szkodzi, że ówczesne MIN_CONFIDENCE wynosiło 0,35, a nie 0,25:
-- gałąź pewności jest w tych wierszach nieosiągalna, bo wcześniej łapie je
-- warunek słabego wyniku.
WITH odtworzone AS (
    SELECT d.id,
           d.confidence,
           d.target_qty,
           d.risk_verdict,
           CASE
               WHEN d.pokrycie_sygnalow IS NULL
                   THEN greatest(-1.0, least(1.0, COALESCE(sum(c.contribution), 0.0)))
               WHEN COALESCE(sum(abs(c.weight)), 0.0) = 0.0
                   THEN 0.0
               ELSE greatest(-1.0, least(1.0,
                        sum(c.contribution) / sum(abs(c.weight))))
           END AS wynik,
           COALESCE((mv.params->>'enter_threshold')::double precision, 0.25) AS prog_we,
           COALESCE((mv.params->>'min_confidence')::double precision, 0.25) AS prog_pewnosci
      FROM journal.decision d
      LEFT JOIN learn.model_version mv ON mv.id = d.model_version_id
      LEFT JOIN journal.decision_contribution c
             ON c.decision_id = d.id AND c.kind = 'signal'
     WHERE d.action = 'hold'
       AND d.powod_holdu IS NULL
     GROUP BY d.id, d.confidence, d.target_qty, d.risk_verdict,
              d.pokrycie_sygnalow, mv.params
)
UPDATE journal.decision d
   SET powod_holdu = CASE
           -- Wstrzymanie wejść (kill-switch, limit konta, niepełny pomiar
           -- kapitału) ZAWSZE zapisuje werdykt inny niż 'allow' - patrz
           -- `werdykt_wstrzymania` w loop.py. Dlatego ten warunek jest pierwszy
           -- i dlatego pozostałe mogą już zakładać werdykt 'allow'.
           WHEN o.risk_verdict <> 'allow'                THEN 'wstrzymanie_ryzykiem'
           -- Hold z otwartą pozycją zapisuje jej wielkość w `target_qty`;
           -- hold bez pozycji zapisuje NULL. To skutek uboczny, ale jednoznaczny.
           WHEN o.target_qty IS NOT NULL                 THEN 'trzyma_pozycje'
           WHEN abs(o.wynik) < o.prog_we                 THEN 'slaby_wynik'
           WHEN o.confidence < o.prog_pewnosci           THEN 'niska_pewnosc'
           WHEN o.wynik < 0                              THEN 'short_niewykonalny'
           -- Wynik dodatni ponad progami przy werdykcie 'allow' i bez pozycji
           -- NIE MA gałęzi w kodzie, która dałaby hold. Zostawiamy NULL zamiast
           -- zgadywać; krok 3 na to zareaguje.
           ELSE NULL
       END
  FROM odtworzone o
 WHERE o.id = d.id;


-- KROK 3: SPRAWDZENIE, ŻE ODTWORZENIE NIE MILCZY.
--
-- Backfill, który zostawia wiersze niewyjaśnione, wygląda z zewnątrz dokładnie
-- tak samo jak backfill, który zadziałał — dopóki ktoś nie policzy NULL-i. Ten
-- blok liczy je od razu i wywraca całą migrację, jeśli jakikolwiek hold został
-- bez powodu. Na produkcji sprawdzone przed napisaniem tego pliku:
-- 782 holdy, 0 niewyjaśnionych.
DO $$
DECLARE
    bez_powodu integer;
BEGIN
    SELECT count(*) INTO bez_powodu
      FROM journal.decision
     WHERE action = 'hold' AND powod_holdu IS NULL;
    IF bez_powodu > 0 THEN
        RAISE EXCEPTION
            'backfill 0040 zostawil % holdow bez powodu - odtworzenie nie pokrywa '
            'jakiejs galezi wyboru akcji w loop.py', bez_powodu;
    END IF;
END
$$;

ALTER TABLE journal.decision ENABLE TRIGGER decision_immutable;
