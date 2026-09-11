-- 0044: artykuł pamięta, że został OCENIONY — także wtedy, gdy ocena była odmowna.
--
-- CO BYŁO ZŁE
--
-- `news_worker.run_cycle` deduplikuje po `content_hash`, ale wiersz w
-- `news.article` powstawał WYŁĄCZNIE po udanej analizie. Artykuł odrzucony przez
-- triaż, artykuł, na którym triaż padł, i artykuł uznany przez analizę za
-- nieistotny nie zostawiały ŻADNEGO śladu — więc w następnym cyklu wracały jako
-- „nowe" i były oceniane od nowa. Log produkcyjny z 2026-09-10 pokazuje to
-- wprost w każdym z 338 cykli:
--
--     kanałów 5, wpisów 146, nowych 65, zapisanych 0, przez filtr 0, ...
--
-- Ponieważ triaż odrzuca z założenia ~95% strumienia, praktycznie CAŁY ruch był
-- płatnie oceniany co 5 minut, czyli ~288 razy na dobę zamiast raz. To jest
-- przyczyna wypalania dobowego limitu Gemini w kilkanaście minut i schodzenia
-- triażu na płatnego Claude'a zaraz potem.
--
-- DLACZEGO KOLUMNY W `news.article`, A NIE OSOBNA TABELA `news.article_triage`
--
-- 1. Werdykt jest ATRYBUTEM artykułu, nie zdarzeniem o własnym życiu. Na artykuł
--    przypada dokładnie jedna aktualna ocena; ponowienie po awarii dostawcy ma ją
--    ZASTĄPIĆ, a nie dołożyć wiersz. Historia prób i tak jest w `news.api_spend`
--    (tam jest każde wywołanie, jego koszt i to, czy się udało) — osobna tabela
--    byłaby trzecim miejscem mówiącym o tym samym.
-- 2. Zapytanie o zaległości już teraz robi `LEFT JOIN news.article_analysis`.
--    Osobna tabela dokładałaby drugie złączenie DOKŁADNIE w tej ścieżce, którą ta
--    migracja ma odciążyć, i zamieniała „brak wiersza" na dwa różne znaczenia
--    (nieoceniony vs. oceniony i przepuszczony).
-- 3. Nadania: `news.article` ma już INSERT/UPDATE dla ról procesów (0013, 0034).
--    Nowa tabela wymagałaby własnych GRANT-ów, a brakujący GRANT objawiłby się
--    tak samo cicho jak błąd, który tu naprawiamy — worker łapie wyjątek i idzie
--    dalej.
--
-- ISTNIEJĄCE WIERSZE MAJĄ WERDYKT NIEZNANY, NIE „ODRZUCONY"
--
-- Produkcja ma dziesiątki tysięcy artykułów sprzed tej zmiany. NULL znaczy tu
-- „nikt tego nie oceniał" i takie artykuły MAJĄ prawo pójść na triaż raz — inaczej
-- backfill wpisałby im ocenę, której żaden model nie wydał. Nie ma tu żadnego
-- UPDATE-u na istniejących wierszach i to jest decyzja, a nie przeoczenie.

ALTER TABLE news.article
    ADD COLUMN IF NOT EXISTS triage_verdict    text,
    ADD COLUMN IF NOT EXISTS triage_reason     text,
    ADD COLUMN IF NOT EXISTS triage_model      text,
    ADD COLUMN IF NOT EXISTS triage_at         timestamptz,
    -- Licznik NIEUDANYCH prób, nie wszystkich. Rośnie wyłącznie przy awarii
    -- dostawcy; werdykt (przepuszczony/odrzucony) go nie rusza. DEFAULT 0 wypełnia
    -- istniejące wiersze bez przepisywania tabeli (Postgres ≥ 11).
    ADD COLUMN IF NOT EXISTS triage_attempts   smallint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS analysis_verdict  text,
    ADD COLUMN IF NOT EXISTS analysis_at       timestamptz,
    ADD COLUMN IF NOT EXISTS analysis_attempts smallint NOT NULL DEFAULT 0;

-- DLACZEGO OD RAZU TAKŻE WERDYKT ANALIZY, SKORO BŁĄD DOTYCZY TRIAŻU
--
-- `relevant = false` to ta sama pętla o jeden etap dalej i o rząd wielkości
-- droższa: analiza nie zapisuje wtedy nic do `news.article_analysis`, więc
-- artykuł wraca do zaległości i jest analizowany DROGIM modelem co cykl.
-- Naprawienie samego triażu przesunęłoby pętlę na tę ścieżkę zamiast ją zamknąć.
-- Świadomie NIE zapisujemy uzasadnienia analizy: dla wyników istotnych trzyma je
-- `news.article_analysis.reasoning`, a dla nieistotnych sam werdykt wystarcza,
-- żeby artykuł nie wrócił.

-- Zbiory zamknięte. Wartość spoza listy znaczy, że w workerze pojawiła się
-- ścieżka, o której baza nie wie — lepiej, żeby wywróciła się transakcja, niż
-- żeby do kolumny wpadł napis, po którym nikt później nie odfiltruje zaległości.
-- DROP + ADD zamiast samego ADD, żeby powtórne uruchomienie migracji nie padło
-- na istniejącym ograniczeniu.
ALTER TABLE news.article DROP CONSTRAINT IF EXISTS article_triage_verdict_chk;
ALTER TABLE news.article ADD CONSTRAINT article_triage_verdict_chk CHECK (
    triage_verdict IS NULL OR triage_verdict IN ('accepted', 'rejected', 'failed'));

ALTER TABLE news.article DROP CONSTRAINT IF EXISTS article_analysis_verdict_chk;
ALTER TABLE news.article ADD CONSTRAINT article_analysis_verdict_chk CHECK (
    analysis_verdict IS NULL OR analysis_verdict IN ('relevant', 'irrelevant', 'failed'));

-- Analiza nie mogła się odbyć bez przepuszczenia przez triaż. Werdykt analizy
-- przy `triage_verdict` innym niż 'accepted' znaczyłby, że jeden z dwóch zapisów
-- jest nieprawdziwy — a wtedy filtr zaległości opiera się na fikcji.
ALTER TABLE news.article DROP CONSTRAINT IF EXISTS article_analiza_po_triazu_chk;
ALTER TABLE news.article ADD CONSTRAINT article_analiza_po_triazu_chk CHECK (
    analysis_verdict IS NULL OR triage_verdict = 'accepted');

COMMENT ON COLUMN news.article.triage_verdict IS
    'Rozstrzygnięcie taniego filtru istotności. '
    '''accepted'' = poszedł do analizy, ''rejected'' = odsiany (KONIEC, nie wraca), '
    '''failed'' = dostawca nie odpowiedział — awaria, nie werdykt, więc wolno ponowić. '
    'NULL znaczy „jeszcze nie oceniany" (także dla artykułów sprzed migracji 0044 '
    'i dla pobranych w trybie --bez-analizy), a NIE „odrzucony".';
COMMENT ON COLUMN news.article.triage_reason IS
    'Uzasadnienie modelu przy werdykcie, treść błędu przy ''failed''. '
    'Jedyne miejsce, z którego da się policzyć, CZY triaż nie odsiewa za ostro — '
    'sam licznik odrzuceń tego nie powie.';
COMMENT ON COLUMN news.article.triage_model IS
    'Model, KTÓRY NAPRAWDĘ PRACOWAŁ, nie ten, o który prosiliśmy. Mieszanka '
    'dostawców schodzi na zapasowego przy limicie i bez tego pola nie da się '
    'odróżnić werdyktów Gemini od werdyktów Claude''a — a to nie jest ten sam pomiar.';
COMMENT ON COLUMN news.article.triage_at IS
    'Kiedy zapadł werdykt albo kiedy była OSTATNIA nieudana próba. Drugie '
    'znaczenie jest tu robocze: bez niego ponowienie po awarii trafiałoby '
    'w tego samego niedostępnego dostawcę jeszcze w tym samym cyklu i limit '
    'prób wyczerpywałby się w kilkanaście sekund.';
COMMENT ON COLUMN news.article.triage_attempts IS
    'Ile razy triaż tego artykułu padł z awarią. Limit ponowień i odstęp między '
    'nimi trzyma kod (news_worker.PROBY_PO_AWARII_MAX, PRZERWA_PO_AWARII_S); '
    'tu jest sam licznik.';
COMMENT ON COLUMN news.article.analysis_verdict IS
    '''relevant'' = jest wiersz w news.article_analysis, ''irrelevant'' = model '
    'uznał artykuł za nieistotny (ocena zapadła, wiersza analizy nie ma i nie '
    'będzie), ''failed'' = awaria dostawcy, wolno ponowić. NULL = analiza jeszcze '
    'nie ruszyła.';
COMMENT ON COLUMN news.article.analysis_at IS
    'Jak triage_at, dla drogiego etapu.';
COMMENT ON COLUMN news.article.analysis_attempts IS
    'Jak triage_attempts, dla drogiego etapu.';

-- Indeks pod JEDYNE zapytanie, które te kolumny czyta: wybór zaległości z okna
-- ostatniej doby. Warunek częściowy odsiewa 95% tabeli (odrzucone przez triaż)
-- już na poziomie indeksu, więc rosnące archiwum nie spowalnia cyklu.
--
-- Predykat jest CELOWO szerszy niż filtr w kodzie: mieści wszystko, co MOŻE
-- jeszcze wymagać pracy, a o tym, czy limit ponowień się wyczerpał, decyduje
-- `etap_do_wykonania` w workerze. Wpisanie liczby prób do predykatu związałoby
-- schemat bazy ze stałą w Pythonie i pierwsza zmiana tej stałej po cichu
-- unieważniłaby indeks.
CREATE INDEX IF NOT EXISTS article_do_oceny_idx
    ON news.article (event_time DESC)
    WHERE triage_verdict IS NULL OR triage_verdict IN ('accepted', 'failed');
