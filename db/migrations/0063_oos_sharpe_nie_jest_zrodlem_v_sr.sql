-- `oos_sharpe` NIE JEST zrodlem V[SR_n] - migracja 0062 twierdzila, ze jest.
--
-- CO BYLO NIE TAK. 0062 dopisala do `learn.hypothesis_trial.oos_sharpe` komentarz
-- konczacy sie zdaniem "Zrodlo V[SR_n] dla deflowanego Sharpe". To zdanie jest
-- FALSZYWE i komentarz stoi w produkcyjnej bazie, wiec czyta go kazdy, kto
-- zaglada do schematu przez `\d+`.
--
-- Kazdy wiersz tej kolumny to Sharpe z JEDNEGO okna walk-forward (zwyciezca tego
-- okna). Rozrzut takich liczb to rozrzut MIEDZY OKNAMI, a Bailey potrzebuje
-- rozrzutu MIEDZY KONFIGURACJAMI. Zmierzone na produkcji 2026-09-27, tor 4h:
--
--     miedzy OKNAMI         (learn.wf_metric, n=2353)      sd = 0,2627
--     miedzy PRZEBIEGAMI    (learning_state_log, n=29)     sd = 0,0270
--     miedzy KONFIGURACJAMI (DISTINCT params, n=28)        sd = 0,0266
--     podstawienie z H0     (T=1500)                       sd = 0,0260
--
-- Roznica jest dziesieciokrotna, bo rozrzut miedzy oknami mierzy zmiennosc RYNKU
-- okno po oknie oraz szum estymacji Sharpe'a z proby jednego okna (mediana 35
-- transakcji). Zaden z tych skladnikow nie jest rozrzutem konfiguracji. Przy
-- SR +0,0762 poprzeczka selekcji rosla z +0,062 na +0,615, a DSR spadal z 0,70
-- na 0,00 - czyli z "nie przechodzi o wlos" na "nie przechodzi o rzad wielkosci".
--
-- CZEGO TA MIGRACJA NIE ROBI. Nie kasuje kolumny i nie przestaje jej wypelniac.
-- Sharpe OOS zwyciezcy okna jest prawdziwa i uzyteczna dana - tylko o czym innym,
-- dokladnie ta klasa bledu, ktora tropi `services/engine/koszty.py`. Zmienia sie
-- wylacznie to, czym ta kolumna SIE NAZYWA w schemacie.
--
-- Zrodlem V[SR_n] jest od teraz `learn.learning_state_log.median_oos_sharpe`
-- grupowany po DISTINCT `learn.model_version.params` - patrz
-- `services/engine/train.odchylenie_prob_z_bazy`.
COMMENT ON COLUMN learn.hypothesis_trial.oos_sharpe IS
    'Sharpe na transakcje OUT-OF-SAMPLE zwyciezcy okna walk-forward, sredni po oknach, '
    'w ktorych ta konfiguracja zostala wybrana. NULL znaczy: OOS nie liczono - kandydat '
    'odrzucony na oknie treningowym nigdy nie wchodzi do symulacji poza oknem. '
    'NIE JEST ZRODLEM V[SR_n] dla deflowanego Sharpe, wbrew komentarzowi z migracji 0062: '
    'rozrzut tych wartosci to rozrzut miedzy OKNAMI (zmierzone 2026-09-27: sd 0,2627), '
    'a nie miedzy KONFIGURACJAMI (sd 0,0266) - roznica dziesieciokrotna. V[SR_n] bierze '
    'sie z learn.learning_state_log.median_oos_sharpe grupowanego po DISTINCT '
    'learn.model_version.params (services/engine/train.odchylenie_prob_z_bazy).';

COMMENT ON COLUMN learn.learning_state_log.median_oos_sharpe IS
    'Mediana Sharpe na transakcje po oknach OOS tego przebiegu. Grupowana po DISTINCT '
    'learn.model_version.params daje V[SR_n] dla deflowanego Sharpe - rozrzut MIEDZY '
    'KONFIGURACJAMI. Przebiegi tej samej konfiguracji usrednia sie w jedna probe, bo dwa '
    'kolejne nocne przebiegi dziela 39 z 40 okien (rho ~0,97) i nie sa niezalezne. '
    'UWAGA: to mediana po oknach, a SR w bramce jest Sharpe ZLACZONEJ proby transakcji - '
    'dwie rozne statystyki, i tej roznicy nikt jeszcze nie zmierzyl.';
