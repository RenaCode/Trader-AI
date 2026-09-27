-- 0068: świeca ODŁOŻONA i nigdy nie rozstrzygnięta ma swój alarm
--
-- CO ZAMYKA TĘ DZIURĘ I SKĄD SIĘ WZIĘŁA
--
-- Do 2026-09-22 pętla decyzyjna handlowała na niepełnym komplecie sygnałów:
-- przez jedenaście dni pokrycie wag stało na 0,02–0,10, `atr_pct` nie istniał
-- w 590 z 646 decyzji reżimu `range`, a wejścia powstawały z jednego sygnału
-- `funding_carry` wzmocnionego dwudziestokrotnie (commit d8c7759 mierzy to
-- dokładnie). Poprawka postawiła bramkę POKRYCIA WAG: poniżej 0,9 decyzja jest
-- ODKŁADANA do następnego cyklu w oknie świecy, ze śladem `niepelne_sygnaly`
-- w `journal.pominiecie`.
--
-- Bramka jest słuszna i zostaje. Zamieniła jednak awarię GŁOŚNĄ (system handluje
-- bzdurą, widać to w dzienniku decyzji) na awarię CICHĄ: przy trwałym braku cech
-- każda świeca zostanie odłożona i nikt nie wejdzie w rynek — a silnik bez wejść
-- wygląda dokładnie tak samo jak silnik na spokojnym rynku. Puls workerów bije,
-- świece przychodzą, zadania świecą na zielono.
--
-- Żadna z jedenastu czujek `dozor.py` tego nie widziała: `journal.pominiecie` nie
-- było czytane przez ani jedną z nich (sprawdzone grepem 2026-09-27), a
-- `odlozone_niepelne` żyło wyłącznie w liczniku w pamięci pętli.
--
-- MIARA JEST DOKŁADNA, NIE PRZYBLIŻONA. Pytanie brzmi: czy świeca, którą pętla
-- ODŁOŻYŁA, dostała potem decyzję W SWOIM OKNIE. Zmierzone na produkcji
-- 2026-09-27 na całej historii `journal.pominiecie`:
--
--     dzień        pominięć  bez decyzji w świecy
--     2026-09-27         70                     0
--     2026-09-26        100                     0
--     ...                ...                    0
--     2026-09-15         60                     0
--
-- Zero w każdym dniu, bez wyjątku. Odłożenie jest więc NORMALNĄ pracą maszyny
-- (cechy pochodne są gotowe od razu po zamknięciu świecy, cenowe 20–35 min
-- później, więc pierwszy cykl odkłada, a któryś z następnych rozstrzyga) i ma
-- czystą, zmierzoną wartość odniesienia. Każda wartość większa od zera znaczy
-- świecę, w której NIE BYŁO decyzji — ani wejścia, ani wyjścia, ani `hold`.
--
-- CZEGO TA CZUJKA NIE ZASTĄPI: martwej pętli. Pętla, która nie chodzi wcale, nie
-- zapisuje też odłożeń, więc ta miara milczy — i słusznie, bo o tym stanie mówi
-- `risk.engine_heartbeat` i czujka `engine.down`. Ta pilnuje stanu odwrotnego:
-- pętla ŻYJE, cyklicznie odkłada i nigdy nie wraca.

INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES
    ('engine.decyzje_odlozone', 'Świeca odłożona i nierozstrzygnięta',
     'Pętla decyzyjna odłożyła świecę z powodu niepełnych sygnałów i nigdy do '
     'niej nie wróciła — w tej świecy nie było ŻADNEJ decyzji. Handel stoi, ale '
     'nie z powodu rynku ani ryzyka, tylko z powodu braku danych; silnik bez '
     'wejść wygląda przy tym identycznie jak silnik na spokojnym rynku. '
     'Wartością odniesienia jest ZERO: zmierzone na całej historii produkcji '
     'każde odłożenie kończyło się decyzją w swoim oknie.',
     'ryzyko', 'critical', true, 0)
ON CONFLICT (code) DO NOTHING;
