-- 0051: licznik nieudanych kodów drugiego składnika NA BILECIE.
--
-- CO BYŁO ZŁE
--
-- `/api/auth/mfa` nie liczyło nieudanych prób w ogóle. Cała ochrona przed
-- zgadywaniem siedziała w `/login` (`failed_logins`, `locked_until`, migracja
-- 0007), czyli WYŁĄCZNIE na pierwszym składniku. Drugi składnik był więc
-- chroniony samym rozmiarem przestrzeni kodów, a ta jest mała: sześć cyfr
-- i okno tolerancji zegara (`TOTP_WINDOW = 1`) dają TRZY ważne kody naraz,
-- czyli ~3/10^6 na strzał. Bez licznika, bez opóźnienia i bez ogranicznika
-- tempa w całym API kilka tysięcy żądań na bilet to ~1% szansy, a bilet można
-- wystawić ponownie w każdej chwili — kto zna hasło, wchodzi w ciągu godzin.
--
-- DLACZEGO LICZNIK NA SESJI, A NIE TYLKO NA KONCIE
--
-- Licznik na koncie (`app.user.failed_logins`) zamyka drogę dopiero po blokadzie
-- konta i jest wspólny dla wszystkich biletów. Licznik NA BILECIE zamyka ją
-- wcześniej i osobno: po trzech błędnych kodach ten konkretny bilet jest
-- unieważniony i nie da się go już użyć, niezależnie od tego, czy konto zdążyło
-- się zablokować. Dwie warstwy, bo bilet wygasa sam po pięciu minutach,
-- a blokada konta trwa kwadrans — żadna z nich sama nie pokrywa drugiej.
--
-- `NOT NULL DEFAULT 0`, bo brak prób i zero prób to tutaj ta sama rzecz:
-- kolumna liczy zdarzenia, a nie mierzy stan, którego mogło nie być.

ALTER TABLE app.session
    ADD COLUMN mfa_failed smallint NOT NULL DEFAULT 0;

COMMENT ON COLUMN app.session.mfa_failed IS
    'Nieudane kody drugiego składnika podane do TEGO biletu. Po przekroczeniu progu bilet jest unieważniany (revoked_at).';
