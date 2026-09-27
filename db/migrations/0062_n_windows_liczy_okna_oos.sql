-- n_windows ZNACZY "ile okien OOS policzono", i do 2026-09-27 nie znaczylo nic.
--
-- CO BYLO NIE TAK. `learn.hypothesis_trial` dostawal przy kazdym INSERT-cie
-- `n_windows = 1` i nigdy nie byl aktualizowany, a `oos_sharpe` zostawal NULL.
-- Dwie kolumny tego samego wiersza mowily wiec co innego: jedna, ze jedno okno
-- out-of-sample juz policzono, druga, ze wyniku nie ma. Zmierzone na produkcji
-- 2026-09-27: 16 050 wierszy, 16 050 z `oos_sharpe IS NULL`, wszystkie
-- z `n_windows = 1`.
--
-- Nie bylo to pole martwe przez przypadek - bylo martwe MYLNIE. Jedynka wygladala
-- jak licznik, wiec kazdy rachunek, ktory chcialby jej uzyc jako dzielnika
-- (srednia Sharpe'a OOS po oknach, w ktorych konfiguracja wygrala), dzielilby
-- przez jeden i oddawal polowe prawdziwej wartosci przy dwoch oknach.
--
-- CO ROBI TA MIGRACJA. Ustawia 0 tam, gdzie wyniku OOS nie ma - czyli
-- przywraca kolumnie jedno znaczenie. Zadnej wartosci nie kasuje: warunek
-- `oos_sharpe IS NULL` dotyczy wylacznie wierszy bez wyniku, a wiersze
-- z wynikiem od tej pory pisze `train.py` razem z liczba okien.
--
-- DLACZEGO 0, A NIE NULL. NULL znaczylby "nie wiadomo, ile okien". Wiadomo
-- dokladnie: zero, bo `oos_sharpe` jest puste, a jedyne miejsce, ktore te
-- wiersze tworzylo, robilo to PRZED symulacja poza oknem.
UPDATE learn.hypothesis_trial
   SET n_windows = 0
 WHERE oos_sharpe IS NULL
   AND (n_windows IS NULL OR n_windows <> 0);

COMMENT ON COLUMN learn.hypothesis_trial.n_windows IS
    'Ile okien walk-forward ma dla tej konfiguracji policzony wynik OUT-OF-SAMPLE. '
    'Zero znaczy: konfiguracja byla oceniona tylko in-sample i nigdy nie wygrala okna. '
    'Wypelnia services/engine/train.py PO petli walk-forward, razem z oos_sharpe.';

COMMENT ON COLUMN learn.hypothesis_trial.oos_sharpe IS
    'Sharpe na transakcje OUT-OF-SAMPLE tej konfiguracji, sredni po oknach, w ktorych '
    'zostala wybrana. NULL znaczy: OOS nie liczono - kandydat odrzucony na oknie '
    'treningowym nigdy nie wchodzi do symulacji poza oknem. Zrodlo V[SR_n] dla '
    'deflowanego Sharpe (services/engine/statystyka.py).';
