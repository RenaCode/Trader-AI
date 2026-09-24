-- 0055: `ref.session_calendar` dostaje opis swojego stanu - mechanizm SWIADOMIE
--       nieuzywany, a nie zapomniany
--
-- POWOD
--
-- Tabela istnieje od migracji 0001 i do dzis (2026-09-24) NIE ZAPISAL DO NIEJ
-- NIC ZADEN PROCES: zero wierszy na produkcji, zero odwolan w `services/`.
-- To jest dokladnie ten ksztalt, ktory ten projekt tropi gdzie indziej -
-- mechanizm zbudowany i niewpiety. Dwa inne z tej samej rodziny zostaly wlasnie
-- domkniete: `learn.postmortem` (nikt nie pisal) i `decision_contribution`
-- z `kind='news'` (schemat czekal od 0005).
--
-- DLACZEGO TEN ZOSTAJE NIEUZYWANY, A NIE ZOSTAJE WPIETY
--
-- Silnik equity potrzebuje odpowiedzi na pytanie "czy rynek jest teraz otwarty".
-- Kalendarz w naszej bazie odpowiadalby na nie WLASNA kopia wiedzy o godzinach
-- sesji i o swietach - kopia, ktora trzeba czyms wypelniac, ktorej wypelnianie
-- trzeba czyms pilnowac, i ktora moze sie rozjechac z gielda bez zadnego objawu.
-- Rozjazd oznaczalby zlecenia skladane po godzinach albo cisze w otwartej sesji.
--
-- `services/engine/sesja.py` pyta zamiast tego `/v2/clock` Alpaki - czyli TEN
-- SAM glos, ktory przyjmie albo odrzuci nasze zlecenie. Kosztuje to jedno
-- zapytanie na cykl decyzyjny (jedno na piec minut) i nie da sie z nim rozjechac.
--
-- DLACZEGO OPIS, A NIE `DROP TABLE`
--
-- Usuniecie bylo rozwazane i zostalo odrzucone, a powod nie brzmi "na wszelki
-- wypadek":
--
--   * Szkoda, przed ktora bronimy sie w tym projekcie, to NIEJASNOSC, a nie
--     obecnosc pustej tabeli. Mechanizm niewpiety jest grozny wtedy, gdy ktos
--     bierze go za dzialajacy - `\d+ ref.session_calendar` pokazuje teraz wprost,
--     ze nie dziala i dlaczego.
--   * `DROP` jest nieodwracalny, a migracje w tym repozytorium ida tylko do
--     przodu. Zysk (jedna tabela mniej w `\dt`) nie rownowazy utraty opcji.
--   * Kalendarz sesji ma zastosowanie, ktorego zegar gieldy NIE OBSLUGUJE:
--     backtest potrzebuje godzin sesji SPRZED dnia dzisiejszego, a `/v2/clock`
--     mowi wylacznie o teraz. Gdyby trener equity kiedys tego potrzebowal,
--     to jest tabela, do ktorej te dane trafia - z Alpaca `/v2/calendar`.
--
-- WARUNEK ZMIANY TEJ DECYZJI jest wiec konkretny: pierwszy proces, ktory
-- naprawde potrzebuje HISTORYCZNYCH godzin sesji. Do tego czasu komentarz
-- ponizej jest cala trescia tej tabeli.

BEGIN;

COMMENT ON TABLE ref.session_calendar IS
'NIEUZYWANA SWIADOMIE (migracja 0055, 2026-09-24). Zero wierszy od 0001.

Stan sesji dla silnika equity pochodzi z zegara gieldy (/v2/clock Alpaki) przez
services/engine/sesja.py, a nie stad - wlasna kopia godzin sesji i swiat moze
sie z gielda rozjechac bez objawu, a rozjazd znaczy zlecenia po godzinach albo
cisze w otwartej sesji.

NIE WYPELNIAJ tej tabeli po to, zeby odpowiadac na pytanie "czy rynek jest teraz
otwarty" - na to odpowiada gielda. Wlasciwe zastosowanie jest inne i dotad
niepotrzebne: HISTORYCZNE godziny sesji dla backtestu, ktorych /v2/clock nie
podaje. Zrodlem byloby wtedy Alpaca /v2/calendar.';

COMMENT ON COLUMN ref.session_calendar.open_at IS
'Otwarcie sesji. Do backtestu, nie do decyzji "czy handlowac teraz" - patrz komentarz tabeli.';

COMMIT;
