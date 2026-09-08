-- 0035: puls procesów zbierających dane — zawieszony worker przestaje wyglądać na żywy
--
-- POWÓD
--
-- Pętla decyzyjna zapisuje `risk.engine_heartbeat`, dozorca ryzyka
-- `risk.dozorca_heartbeat`, a `dozor.py --co zywotnosc` pilnuje obu. Cztery
-- procesy, od których zależy CAŁE wejście danych — market, history, features,
-- news — nie zapisują pulsu NIGDZIE.
--
-- Skutek jest dokładnie tym trybem awarii, przed którym broni cała reszta tego
-- schematu: proces zawieszony (a nie martwy) na gnieździe TCP bez limitu czasu
-- albo na blokadzie w bazie nie kończy się, nie podnosi licznika restartów
-- i nie wypisuje nic do logu. `kubectl get pods` pokazuje `Running` bez końca.
-- Licznik pięciu awarii pod rząd, który te moduły już mają, NIE ŁAPIE tego
-- przypadku: zawieszony cykl nigdy się nie kończy, więc nigdy nie zwiększa
-- licznika.
--
-- CZEMU TO NIE DUBLUJE `--co swiezosc`
--
-- Czujka świeżości wykrywa SKUTEK — dane przestały napływać — i to jest dobra
-- obrona, której to nie zastępuje. Ale:
--
--   * wykrywa go po czasie jednej świecy źródłowej albo dłużej (przy 1h próg
--     wynosi trzy świece, czyli trzy godziny),
--   * nie odróżnia „worker stoi" od „giełda nie oddaje danych" — a to dwie
--     różne awarie, z dwiema różnymi reakcjami,
--   * nie mówi, KTÓRY z czterech procesów zamilkł. Przy jednej tabeli
--     `feat.feature_value` sygnał jest ten sam niezależnie od tego, czy stanął
--     `market`, `history` czy `features`.
--
-- Puls odpowiada na inne pytanie: czy PROCES domknął cykl. Świeżość odpowiada
-- na pytanie, czy DANE są aktualne. Zdrowy proces może nie mieć danych (giełda
-- milczy) i to nie jest ta sama awaria co proces, który stoi.
--
-- DLACZEGO OSOBNA TABELA, A NIE JEDNA Z DWÓCH ISTNIEJĄCYCH
--
-- `risk.engine_heartbeat` ma w kluczu głównym `engine` typu `ref.engine`.
-- Dopisanie się do niej wymagałoby rozszerzenia enuma SILNIKÓW HANDLOWYCH
-- o „features" i „news". To ten sam argument, który w 0029 wyprowadził puls
-- dozorcy do osobnej tabeli — worker nie jest silnikiem i nie ma być pozycją
-- w enumie, po którym filtruje się zlecenia, pozycje i limity.
--
-- `risk.dozorca_heartbeat` ma kształt, który by pasował (nazwa + znacznik +
-- detail), ale 0034 zawiera na niej wprost:
--
--     REVOKE ALL ON risk.dozorca_heartbeat FROM trader_silnik;
--
-- z uzasadnieniem, że gdyby silnik mógł odświeżać puls dozorcy, martwy dozorca
-- wyglądałby w czujce dokładnie jak żywy. Workery chodzą na roli
-- `trader_silnik` (values.yaml: `dbRole` domyślnie `silnik`), więc wpuszczenie
-- ich do tamtej tabeli oznaczałoby cofnięcie tej własności — czyli zepsucie
-- działającego mechanizmu bezpieczeństwa dla wygody zapisu w innym.
--
-- Trzecia tabela jest tu tańsza niż jedna z tych dwóch zmian.
--
-- CO ZAWIERA I DLACZEGO WŁAŚNIE TO
--
-- `last_beat_at` to znacznik ostatniego UDANEGO cyklu, nie startu procesu ani
-- nie ostatniej próby. Proces, który wstał i od razu się zawiesił, nie ma prawa
-- wyglądać na zdrowy — a puls bity na starcie dawałby mu kwadrans zdrowia przy
-- każdym restarcie w pętli CrashLoopBackOff.
--
-- `ostatnia_proba_at`, `awarii_pod_rzad` i `ostatni_blad` istnieją po to, żeby
-- czujka umiała ODRÓŻNIĆ dwa przypadki, które przy samym `last_beat_at`
-- wyglądają identycznie:
--
--     worker stoi                    -> ostatnia_proba_at też jest stara
--     worker kończy cykle błędem     -> ostatnia_proba_at jest świeża,
--                                       awarii_pod_rzad rośnie, jest opis błędu
--
-- To jest różnica między „zajrzyj, czy pod żyje" a „giełda odmawia od godziny".
-- Ani jedno, ani drugie nie odświeża `last_beat_at`, więc alarm jest JEDEN —
-- te kolumny zmieniają tylko jego treść, nie liczbę powiadomień.
--
-- `interwal_s` JEST W WIERSZU, A NIE W CZUJCE.
--
-- Próg martwoty musi wynikać z interwału danego workera: `market` chodzi co
-- 60 s, `history` co 1200 s. Jedna liczba dla wszystkich albo krzyczałaby na
-- historii, albo milczała przy rynku. Interwał jest jednak podawany w komendzie
-- w `values.yaml` (`--interval`), a czujka jest osobnym procesem — słownik
-- progów wpisany w `dozor.py` byłby DRUGĄ kopią tej samej liczby, rozjeżdżającą
-- się po cichu przy pierwszej zmianie w charcie. Dokładnie ten rozjazd zdarzył
-- się już 2026-09-04 między listą horyzontów w `workers.features` a
-- `trainerCron.horyzonty` i kosztował 21 godzin martwych cech.
--
-- Worker zna swój interwał, bo dostał go w argumencie, więc deklaruje go razem
-- z pulsem. Czujka liczy próg z wiersza i nie ma czego rozjechać.

BEGIN;

CREATE TABLE IF NOT EXISTS risk.worker_heartbeat (
    -- Klucz to ROLA w potoku, nie egzemplarz procesu: każdy worker ma jeden
    -- Deployment ze `strategy: Recreate` i `replicas: 1`, więc dwa procesy tej
    -- samej roli piszące w ten sam wiersz to stan, któremu chart już zapobiega.
    -- Stała nazwa oznacza też, że treść powiadomienia i jego tłumienie nie
    -- zmieniają się, gdy ktoś przestawi `--bar` albo `--engine`; zmienne idą
    -- do `detail`.
    worker            text        PRIMARY KEY,

    -- Interwał zadeklarowany przez sam proces — patrz nagłówek. CHECK, bo próg
    -- policzony z zera albo z liczby ujemnej alarmowałby zawsze.
    interwal_s        integer     NOT NULL CHECK (interwal_s > 0),

    -- Ostatni UDANY cykl. To jest jedyna kolumna, z której liczy się martwota.
    last_beat_at      timestamptz NOT NULL,

    -- Ostatni cykl JAKIKOLWIEK — udany czy zakończony błędem. Różnica między
    -- tymi dwoma znacznikami to cała informacja o tym, czy proces stoi, czy
    -- pracuje i nie daje rady.
    ostatnia_proba_at timestamptz NOT NULL,

    -- Ten sam licznik, który po pięciu awariach kończy proces. W bazie jest po
    -- to, żeby dało się go zobaczyć BEZ czytania logu poda — log poda nikt nie
    -- czyta o trzeciej w nocy, a po restarcie kontenera nie ma go już wcale.
    awarii_pod_rzad   integer     NOT NULL DEFAULT 0 CHECK (awarii_pod_rzad >= 0),

    -- Opis ostatniej awarii, przycięty przy zapisie. Pełny ślad zostaje w logu;
    -- tu ma się zmieścić tyle, żeby powiadomienie mówiło, czego dotyczy.
    ostatni_blad      text,

    detail            jsonb
);

COMMENT ON TABLE risk.worker_heartbeat IS
'Puls procesów zbierających dane (market, history, features, news). Osobno od
risk.engine_heartbeat, bo worker nie jest silnikiem handlowym i nie ma go
w enumie ref.engine; osobno od risk.dozorca_heartbeat, bo 0034 świadomie odebrał
roli silnika prawo zapisu do pulsu nadzorcy.';

COMMENT ON COLUMN risk.worker_heartbeat.last_beat_at IS
'Koniec ostatniego UDANEGO cyklu. Nie start procesu: proces, który wstał i od
razu się zawiesił, nie może wyglądać na zdrowy.';

COMMENT ON COLUMN risk.worker_heartbeat.interwal_s IS
'Interwał deklarowany przez sam proces. Próg martwoty liczy się z niego, żeby
nie istniała druga kopia tej liczby poza komendą w values.yaml.';

-- ------------------------------------------------------------- uprawnienia
--
-- `risk` to schemat, w którym „nikt nie pomyślał o uprawnieniach" znaczy
-- „bezpiecznik jest zapisywalny przez to, co ma zatrzymywać". 0034 ustawił dla
-- niego domyślne nadanie SELECT dla silnika i czujki, więc bez poniższych linii
-- workery dostałyby InsufficientPrivilege przy pierwszym zapisie pulsu.

-- Workery chodzą na roli silnika (values.yaml: dbRole domyślnie `silnik`)
-- i piszą WYŁĄCZNIE własny puls. Bez DELETE: skasowanie wiersza jest cichszym
-- sposobem uciszenia czujki niż jego sfałszowanie, bo nie zostawia nawet
-- starego znacznika.
GRANT SELECT, INSERT, UPDATE ON risk.worker_heartbeat TO trader_silnik;
REVOKE DELETE, TRUNCATE ON risk.worker_heartbeat FROM trader_silnik;

-- Czujka ma to CZYTAĆ i nic więcej — tak samo jak oba pozostałe pulsy. Proces
-- diagnostyczny z prawem zapisu do obserwowanego stanu prędzej czy później
-- zacznie mierzyć siebie zamiast systemu.
GRANT SELECT ON risk.worker_heartbeat TO trader_czujka;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON risk.worker_heartbeat FROM trader_czujka;

-- API dostaje ten wiersz przez domyślne nadanie z 0013 (SELECT, INSERT, UPDATE
-- na przyszłych tabelach). SELECT zostaje — dashboard ma prawo pokazać, czy
-- workery żyją. Zapis odbieramy: API jest jedyną z tych ról wystawioną na
-- internet, a sfałszowany puls to czujka, która melduje zdrowie martwego
-- potoku. To ten sam argument, którym 0034 zawęziło rolę API na
-- risk.killswitch_event i risk.limit_def.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON risk.worker_heartbeat FROM trader_app;

-- Dozorca ryzyka nie zbiera danych i nie ma powodu dotykać tej tabeli. Czyta,
-- bo ocenia stan konta i przydaje mu się kontekst; nie pisze.
GRANT SELECT ON risk.worker_heartbeat TO trader_dozorca;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON risk.worker_heartbeat FROM trader_dozorca;

COMMIT;
