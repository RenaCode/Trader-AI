-- 0057: wskaznik dryfu przestaje nazywac sie PSI, bo nim nie jest
--
-- CO JEST NIE TAK
--
-- `learn.drift_metric.psi` i `learn.learning_state_log.max_psi` nie zawieraja
-- Population Stability Index. Zawieraja wielkosc liczona w `train.py`:
--
--     |srednia_nowa - srednia_stara| / odchylenie_stare
--         + |odchylenie_nowe / odchylenie_stare - 1|
--
-- czyli przesuniecie sredniej w jednostkach historycznego odchylenia PLUS
-- wzgledna zmiana samego odchylenia. Kod nazywa to uczciwie w komentarzu
-- ("uproszczony wskaznik dryfu"), ale nazwa kolumny, nazwa zmiennej i opis
-- zdarzenia mowia co innego.
--
-- DLACZEGO TO NIE JEST CZEPIANIE SIE O NAZWE
--
-- PSI ma POWSZECHNIE ZNANA SKALE i wlasnie dlatego nazwa ma tu koszt:
-- 0,1 to "brak dryfu", 0,25 to "istotny", 0,5 to "duzy". Opis zdarzenia
-- `learning.drift_high` cytowal dokladnie ten prog ("PSI powyzej 0,25"),
-- a progi czujki wynosza 0,5 i 1,0 - czyli w skali PSI bylyby to wartosci
-- katastrofalne, podczas gdy dla TEJ wielkosci sa normalnym zakresem pracy.
--
-- Zmierzone 2026-09-24: wskaznik `ret_long__4h` wynosi 2,43. Czytany jako PSI
-- znaczylby, ze rozklad cechy nie ma juz nic wspolnego z treningowym i model
-- nadaje sie do wyrzucenia. Czytany poprawnie znaczy: srednia przesunela sie
-- o okolo dwa historyczne odchylenia. Sprawdzone tego samego dnia - przesuniecie
-- jest PRAWDZIWE i pochodzi z rynku (wszystkie dziesiec instrumentow
-- jednoczesnie, kolejnosc zgodna ze zmiana ceny: NEAR +156%, ARB +149%,
-- BTC +9,7%), a mediana OOS Sharpe toru 4h w tym samym przebiegu pozostala
-- DODATNIA (+0,1145 przy 25/37 oknach dodatnich).
--
-- Dwie rozne odpowiedzi na to samo pytanie, wylacznie przez nazwe.
--
-- CZEGO TA MIGRACJA NIE ROBI
--
-- Nie zmienia nazw kolumn. `psi` i `max_psi` czyta `dozor.sprawdz_dryf`,
-- `train.py` i panel; przemianowanie jest zmiana kodu, nie opisu, i nalezy
-- do osobnej decyzji. Do tego czasu komentarz w schemacie jest jedynym
-- miejscem, w ktorym czytajacy `\\d+ learn.drift_metric` dowie sie, co trzyma
-- ta kolumna - i dlatego jest tutaj, a nie tylko w docstringu Pythona.
--
-- NIE RUSZA TEZ PROGOW. 0,5 i 1,0 dobrano dla TEJ wielkosci i one opisuja ja
-- poprawnie; falszywy byl wylacznie prog zacytowany w opisie zdarzenia.

BEGIN;

COMMENT ON COLUMN learn.drift_metric.psi IS
'NIE JEST TO Population Stability Index, mimo nazwy kolumny (migracja 0057).

Wielkosc liczona w services/engine/train.py:
    |srednia_nowa - srednia_stara| / odchylenie_stare
        + |odchylenie_nowe / odchylenie_stare - 1|

Skala jest INNA niz PSI: 0,5 i 1,0 to progi czujki `dozor.py --co dryf`
i sa normalnym zakresem pracy, a nie katastrofa. Nie przykladaj do tej liczby
regul "0,1 / 0,25 / 0,5" znanych z PSI.';

COMMENT ON COLUMN learn.learning_state_log.max_psi IS
'Najwiekszy wskaznik dryfu sposrod cech - patrz komentarz przy
learn.drift_metric.psi. To NIE jest PSI (migracja 0057).';

-- Opis zdarzenia cytowal prog z innej skali. Teraz mowi, co ta liczba znaczy,
-- i nie podaje wartosci, ktora nalezy do PSI, a nie do niej.
UPDATE notify.event_type
   SET description = 'Rozkład cech odjechał od treningowego: średnia przesunęła '
                     'się o istotny ułamek historycznego odchylenia i tam została. '
                     'Skuteczność modelu może spadać. Wskaźnik NIE jest PSI — '
                     'nie przykładaj do niego progu 0,25 (migracja 0057).'
 WHERE code = 'learning.drift_high';

COMMIT;
