-- 0069: czas ostatniej aktywności sesji — limit bezczynności.
--
-- CO BYŁO ZŁE
--
-- Sesja żyła pełne `SESSION_TTL` (12 h) od zalogowania, niezależnie od tego,
-- czy ktokolwiek z niej korzystał. Panel zostawiony otwarty na cudzym albo
-- wspólnym komputerze dawał dostęp do kluczy API giełdy i kill-switcha do
-- końca tych 12 godzin.
--
-- `current_user` odrzuca teraz (i unieważnia) sesję, w której od
-- `last_seen_at` minęło więcej niż `SESSION_IDLE` (30 min), a przy ruchu
-- odświeża kolumnę najwyżej raz na minutę — żeby każde żądanie panelu nie
-- było zapisem do bazy.
--
-- `NOT NULL DEFAULT now()`: sesje istniejące w chwili migracji dostają
-- „ostatnio widziana teraz”, czyli pełne 30 minut, zamiast natychmiastowego
-- wylogowania wszystkich przy wdrożeniu. Uprawnienia roli aplikacji są
-- nadane na całą tabelę (0013), więc nowa kolumna nie wymaga GRANT-a.

ALTER TABLE app.session
    ADD COLUMN last_seen_at timestamptz NOT NULL DEFAULT now();

COMMENT ON COLUMN app.session.last_seen_at IS
    'Ostatnie uwierzytelnione żądanie z tej sesji (z dokładnością do minuty). Po 30 min bez ruchu sesja jest unieważniana.';
