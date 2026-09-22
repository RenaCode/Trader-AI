-- 0048: trzeci powód pominięcia — DECYZJA ODŁOŻONA, bo sygnały są niepełne.
--
-- CO BYŁO ZŁE (zmierzone w journal.decision, 2026-09-22)
--
-- Od 12.09 `pokrycie_sygnalow` w KAŻDEJ decyzji crypto/demo wynosi 0,02–0,10,
-- a cecha `rsi` jest w 0 z 60 decyzji dziennie; do 10.09 pokrycie było 1,00.
-- Mechanizm: `baa4521` słusznie ograniczył cechy do świecy decyzyjnej, ale
-- cechy pochodne (funding, arkusz, long_ratio, oi, newsy) są dla świecy T
-- dostępne od razu, a cenowe 20–35 min później (history_worker 1200 s +
-- features 900 s). `MIN_CECH = 4` przepuszczał 8 cech pochodnych, pierwszy
-- cykl po zamknięciu decydował, a `decided_for_bar` zamykał świecę. Silnik
-- przez 11 dni handlował strategią dwusygnałową (funding + martwy news_shock),
-- której trener nigdy nie widział — wejście ARBUSDC 22.09 przy wyniku +0,897
-- to `funding_z = −2,72` i nic więcej.
--
-- Gałąź „odłożona, nie odwołana" w loop.py istniała i była poprawna — tylko
-- bramka przed nią nie mierzyła tego, co miało decydować. Teraz mierzy
-- POKRYCIE WAG (ta sama liczba, która idzie do dziennika), a odłożenie
-- zostawia ślad pod własnym powodem, żeby „potok spóźniony" dało się policzyć
-- osobno od „za mało cech" (worker stoi) i „brak ceny".
ALTER TABLE journal.pominiecie DROP CONSTRAINT IF EXISTS pominiecie_powod_check;
ALTER TABLE journal.pominiecie
    ADD CONSTRAINT pominiecie_powod_check
    CHECK (powod IN ('za_malo_cech', 'brak_ceny', 'niepelne_sygnaly'));
