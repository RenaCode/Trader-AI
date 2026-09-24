-- 0056: typ zdarzenia `notify.no_channel` - alarm krytyczny bez drogi poza panel
--
-- POWOD
--
-- Czujka `dozor.py --co kanaly` (2026-09-24) zglaszala znalezisko WYLACZNIE
-- kodem wyjscia, bez powiadomienia. Uzasadnienie brzmialo: czujka sprawdzajaca
-- kanal alarmowy nie moze uzywac kanalu alarmowego, bo alarm o braku kanalu
-- wyslany brakujacym kanalem to ta sama wada, ktora opisuje "nadzor ginie
-- razem z nadzorowanym".
--
-- ROZUMOWANIE BYLO POPRAWNE, WNIOSEK ZLY - i to jest powod tej migracji.
--
-- Brak kanalu jest luka KONFIGURACYJNA, ktora nie mija sama: dopoki czlowiek
-- nie doda kanalu, stan trwa. Czujka bez tlumienia swiecila by wiec na czerwono
-- co dziesiec minut BEZ KONCA - a `trader-zywotnosc` uruchamia w tym samym
-- zadaniu osiem innych sprawdzen. Trwale czerwony Job uczy przewijac czerwone
-- Joby, czyli psuje sygnal dla WSZYSTKICH pozostalych czujek, nie tylko dla tej.
-- To jest ta sama wada, przed ktora broni sie `dozor.kod_wyjscia` i ktora
-- `sprawdz_swiezosc` opisuje przy tlumieniu: "wyciszony kanal to gorszy stan
-- niz brak czujki".
--
-- Obawa o obieg zamkniety zostaje w mocy, ale jest mniejsza od tamtej szkody
-- i daje sie nazwac wprost: powiadomienie o tym, ze alarmy nie wychodza poza
-- panel, trafia do panelu. Jest to ograniczenie z natury rzeczy - nie da sie
-- powiadomic kanalem, ktorego nie ma - i tresc zdarzenia mowi to otwarcie,
-- zamiast udawac, ze wiadomosc dotarla dalej.
--
-- DRUGI, NIEZALEZNY SLAD ZOSTAJE: przy PIERWSZYM zgloszeniu kod wyjscia jest
-- niezerowy, wiec Job swieci na czerwono raz. Dopiero powtorzenia sa tlumione,
-- a linia w logu wypisuje pelny stan przy kazdym przebiegu. Dokladnie ta sama
-- zasada, co przy `engine.down`.
--
-- CZEGO TO ZDARZENIE NIE ROBI: nie jest `always_on`. Zdarzenie always_on omija
-- cooldown katalogu, a to akurat MA byc przypominane rzadko - stan jest trwaly
-- i znany. Cooldown dobowy (86400 s) daje jedno przypomnienie na dobe, czyli
-- tyle, ile trzeba, zeby luka nie zostala zapomniana, i nie wiecej.

BEGIN;

INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'notify.no_channel',
    'Alarm krytyczny nie opuszcza panelu',
    'Zdarzenie krytyczne (kill-switch, martwy silnik, brak kopii) nie ma ' ||
    'włączonego kanału innego niż panel, więc dotrze do człowieka dopiero po ' ||
    'otwarciu dashboardu. To powiadomienie z konieczności przychodzi tą samą ' ||
    'drogą, której brak opisuje — skonfiguruj kanał `whatsapp` albo `webhook`.',
    'system', 'critical', false, 86400)
ON CONFLICT (code) DO NOTHING;

-- Preferencja dla istniejacych odbiorcow, zeby zdarzenie nie czekalo na to,
-- az ktos sam je wlaczy. Kanal `inapp`, bo jest jedynym, ktory na pewno istnieje
-- - a gdy powstanie wychodzacy, czujka przestanie zglaszac cokolwiek i to
-- powiadomienie zniknie razem z powodem.
INSERT INTO notify.preference (user_id, event_code, enabled, channels, cooldown_s)
SELECT u.id, 'notify.no_channel', true, ARRAY['inapp']::notify.channel_kind[], 86400
  FROM app."user" u
 WHERE u.is_active
ON CONFLICT (user_id, event_code) DO NOTHING;

COMMIT;
