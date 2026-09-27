-- 0067: próg wejścia liczony PER REŻIM — nowy powód holdu `rezim_bez_progu`.
--
-- CO SIĘ ZMIENIA W ZACHOWANIU
--
-- `ENTER_PCTL = 0.88` (train.py) znaczy „handluj w 12% najsilniejszych
-- sytuacji". Percentyl liczył się jednak na JEDNEJ liście |wyniku| ze
-- wszystkich reżimów razem i dawał JEDNĄ liczbę, mimo że wagi są brane per
-- reżim (`signals.combine(..., weights.get(regime))`), więc skala wyniku
-- zależy od reżimu.
--
-- ZMIERZONE NA PRODUKCJI (crypto/demo, 2026-09-08..2026-09-27 — czyli od
-- wdrożenia renormalizacji pokrycia wag; wynik odtworzony jako
-- sum(contribution)/sum(abs(weight)) po journal.decision_contribution):
--
--   reżim       decyzji   p88 reżimu   % ponad progiem 0,2798
--   range           646       0,1573                      7,0
--   high_vol        306       0,4304                     34,0
--   trend_up        175       0,3187                     18,9
--   news_shock        3       0,0095                      0,0
--
-- Zamiar 12% nie był dotrzymany w ŻADNYM reżimie. Od teraz percentyl liczy się
-- osobno na rozkładzie każdego reżimu, a wynik idzie do
-- `learn.model_version.params.enter_threshold_per_regime`.
--
-- DLACZEGO TO WYMAGA MIGRACJI
--
-- Percentyl z garstki obserwacji nie jest percentylem, więc reżim, który na
-- oknach treningowych nie uzbierał 50 obserwacji, NIE DOSTAJE progu wejścia
-- i pętla decyzyjna w nim nie wchodzi (uzasadnienie kierunku błędu:
-- services/engine/bramki.py). Taki hold musi mieć własny powód:
-- `slaby_wynik` byłby zdaniem nieprawdziwym, bo wynik może być dowolnie mocny,
-- a NULL znaczyłby „gałąź bez powodu", czyli to, co naprawiało 0040.
--
-- Zbiór powodów jest zamknięty (0040), więc bez tej migracji pierwszy taki
-- hold wywróciłby zapis decyzji — i to jest zachowanie zamierzone: nowa gałąź
-- w pętli albo jest opisana w dzienniku, albo nie ma prawa się wykonać.
ALTER TABLE journal.decision
    DROP CONSTRAINT IF EXISTS decision_powod_holdu_chk;

ALTER TABLE journal.decision
    ADD CONSTRAINT decision_powod_holdu_chk CHECK (
        powod_holdu IS NULL OR powod_holdu IN (
            'slaby_wynik', 'niska_pewnosc', 'short_niewykonalny',
            'trzyma_pozycje', 'wstrzymanie_ryzykiem', 'rezim_bez_progu'));

COMMENT ON COLUMN journal.decision.prog_wejscia IS
    'Próg |wyniku| obowiązujący W TYM REŻIMIE w chwili decyzji (od 2026-09-27; '
    'wcześniej jedna liczba wspólna dla wszystkich reżimów). NULL znaczy, że '
    'próg dla tego reżimu NIE BYŁ SKALIBROWANY — patrz powod_holdu = '
    '''rezim_bez_progu''. Zapisany przy decyzji, nie odczytywany z params '
    'wersji modelu: params są nadpisywane co trening, a decyzja jest faktem '
    'z konkretnej chwili.';
