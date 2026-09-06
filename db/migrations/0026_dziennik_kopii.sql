-- 0026: dziennik kopii zapasowych — żeby dało się zauważyć, że ich nie ma
--
-- DLACZEGO
--
-- Zadanie kopii wykonuje `pg_dump`, a potem `pg_restore --list`, który zawodzi
-- na pliku obciętym. To dobra weryfikacja i komentarz przy niej uczciwie
-- zaznacza jej granicę: dowodzi, że archiwum jest czytelne, nie że dane są
-- kompletne.
--
-- Czego nie było: **nikt nie sprawdzał, czy kopia w ogóle powstała**. Zadanie
-- zakończone błędem widać w `kubectl get jobs`, zawieszony harmonogram nie widać
-- nigdzie, a pełny wolumen albo padnięty węzeł objawiają się tak samo — ciszą.
-- Kopia zapasowa, o której braku dowiadujemy się w dniu odtwarzania, jest gorsza
-- niż jej brak, bo przez cały ten czas daje fałszywe poczucie zabezpieczenia.
--
-- CO TU TRAFIA
--
-- Wiersz zapisywany PO udanej weryfikacji, więc jego obecność znaczy „istnieje
-- czytelny zrzut". Rozmiar jest tu po to, żeby wykryć zrzut, który nagle
-- skurczył się o rząd wielkości: taki plik nadal przechodzi `pg_restore --list`,
-- bo jest poprawnym archiwum — po prostu prawie pustym.

CREATE TABLE app.backup_run (
    id          bigserial   PRIMARY KEY,
    finished_at timestamptz NOT NULL DEFAULT now(),
    plik        text        NOT NULL,
    bajtow      bigint      NOT NULL CHECK (bajtow > 0)
);

CREATE INDEX backup_run_finished_at ON app.backup_run (finished_at DESC);

COMMENT ON TABLE app.backup_run IS
    'Jeden wiersz na UDANĄ i zweryfikowaną kopię. Czyta to czujka '
    '(services/engine/dozor.py --co kopie), bo brak kopii nie objawia się niczym.';

-- Własny typ zdarzenia, a nie doklejenie się do `engine.down`.
--
-- Użycie cudzego kodu zdarzenia zaśmieciłoby historię powiadomień i zakładkę
-- w interfejsie: „silnik przestał odpowiadać" przy działającym silniku uczy
-- czytać alarmy nieuważnie, a to jedyna rzecz, której alarm nie może robić.
--
-- `always_on`, bo utraty kopii nie da się „przyjąć do wiadomości" — ona nie
-- mija sama. Cooldown dobowy: problem trwa aż do ręcznej naprawy, więc
-- powtarzanie częstsze niż raz na dobę byłoby hałasem.
INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES
    ('backup.missing', 'Brak świeżej kopii zapasowej',
     'Nie powstała zweryfikowana kopia zapasowa w oczekiwanym czasie albo ostatnia '
     'skurczyła się o ponad połowę. Zrzut, który stracił dane, nadal przechodzi '
     'pg_restore --list, bo jest poprawnym archiwum — po prostu prawie pustym.',
     'system', 'critical', true, 86400)
ON CONFLICT (code) DO NOTHING;
