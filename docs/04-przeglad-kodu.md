# Przegląd kodu i funkcjonalności

Data: 2026-08-27. Zakres: całe repozytorium (10,4 tys. linii kodu poza
zależnościami), stan produkcyjny na VPS, dane w bazie.

Metoda: czytanie kodu ścieżka po ścieżce (ingestia → cechy → sygnały → decyzja →
egzekucja → API → UI), a następnie **weryfikacja każdego podejrzenia na
działającym systemie**. Żadna pozycja niżej nie jest hipotezą z lektury — każda
została potwierdzona zapytaniem do bazy, uruchomieniem fragmentu kodu albo
wywołaniem zewnętrznego API.

---

## 1. Błędy krytyczne — znalezione i naprawione

Wspólny mianownik pierwszych trzech: **ujawniały się tylko w tym jednym
przypadku, na którym najbardziej zależy** — gdy strategia zaczyna działać.
Dopóki wyniki były złe, wszystko wyglądało poprawnie.

### 1.1 Brama do handlu na żywo nie miała minimum liczby okien

```python
status = ("validated" if pct >= 70 and median and median > 0.05
          else "degraded" if total >= 5 and pct < 40 else "learning")
```

Warunek `degraded` wymagał `total >= 5`. Warunek `validated` — czyli ten, który
otwiera drogę do prawdziwych pieniędzy — **nie wymagał niczego**.

Przebieg z 2026-08-27 miał 40 okien, z czego 39 odrzucono jako nieinformatywne
(za mało transakcji). Zostało jedno, dodatnie. To dawało `pct = 100%`,
`median = 0.136` i status **„Kryteria spełnione"** — na podstawie jednego okna.

Naprawa: `MIN_WINDOWS_FOR_VALIDATION = 20` jako warunek konieczny, plus osobny
komunikat odróżniający *brak wyniku* od *wyniku ujemnego*. To są dwie różne
rzeczy i mylenie ich jest tu najgroźniejsze.

### 1.2 Wagi z treningu nigdy nie trafiały do produkcji

`best_w` było liczone w każdym oknie, użyte do oceny out-of-sample — i wyrzucane.
Nie zapisywano go nigdzie. Pętla decyzyjna wołała `evaluate(..., None, ...)`,
czyli pracowała wiecznie na założeniach początkowych.

Warstwa uczenia mierzyła więc, czy kalibracja **pomogłaby**, nie mając żadnej
drogi, żeby ją zastosować. Wymaganie „algorytm ma się uczyć i doskonalić" było
spełnione w połowie pomiarowej i w zerowej wykonawczej.

Naprawa: trener zapisuje medianę wag po oknach do `learn.model_version.params`,
pętla czyta je z rejestru. Czyta **wyłącznie** wersje o statusie `active` —
wersja `candidate` (taka, która nie wykazała przewagi) nadal nie ma prawa
sterować produkcją.

### 1.3 Trener wywracał się, gdy wynik był dobry

Nowa wersja modelu ustawiana była na `active` **przed** wycofaniem poprzedniej,
co łamie unikalny indeks „jedna aktywna wersja na model". Błąd nie ujawnił się
przez cały dotychczasowy rozwój, bo każdy przebieg kończył się statusem
`degraded` → `candidate`, gdzie konfliktu nie ma.

Trener potrafił więc dokończyć **tylko wtedy, gdy strategia nie działała.**
Naprawa: odwrócona kolejność.

### 1.4 Pętla decyzyjna: do 48 decyzji na jedną świecę

Pętla chodzi co 5 minut. Symulacja, na której kalibrowane są wagi, zakłada
dokładnie jedną decyzję na świecę. Przy świecy 4-godzinnej produkcja podejmowałaby
ich 48 — rozjazd trening–produkcja brany nie z danych, nie z kodu, tylko
z **harmonogramu**.

Naprawa: warunek jednej decyzji na świecę (`decided_for_bar`).

### 1.5 `feat.as_of(NULL)` mieszało horyzonty

Funkcja z `p_feature_ids = NULL` zwraca komplet cech wszystkich horyzontów.
Silnik 4-godzinny dostawałby mieszankę cech 4 h i 5 min, której trener nigdy nie
widział. Naprawa: pętla podaje jawną listę identyfikatorów swojego horyzontu.

### 1.6 Trener nie sprawdzał `ingested_at`

Cały projekt opiera się na jednym zdaniu w `feat.as_of()`:
`AND fv.ingested_at <= p_as_of`. Trener **omijał tę funkcję** i czytał
`feat.feature_value` wprost, filtrując wyłącznie po `event_time`.

Działało to poprawnie — ale przez przypadek, bo potok datuje cechy wstecz
prawidłowo. Trener polegał na tym, zamiast to sprawdzać. Różnica ujawnia się
dokładnie wtedy, gdy potok przestanie datować poprawnie, czyli wtedy, gdy jest
za późno. Naprawa: strażnik `ingested_at <= event_time + jedna świeca`.

### 1.7 Wykres kalibracji mierzył sam siebie

```python
predictions.append((min(1.0, abs(t) * 50 + 0.5), t > 0))
```

„Prognoza" powstawała z wielkości **zrealizowanego zwrotu** — czyli z rzeczy
znanej dopiero po fakcie. Wykres kalibracji pokazywał więc zgodność wyniku
z samym sobą i zawsze wyglądałby dobrze.

Kalibracja odpowiada na pytanie „czy sygnał, który twierdził 70%, trafiał
w 70% przypadków". Naprawa: każda transakcja zapamiętuje pewność **z chwili
wejścia**; wykres porównuje ją z faktycznym wynikiem.

### 1.8 Obsunięcie liczone tylko na zrealizowanym wyniku

Krzywa kapitału aktualizowana była wyłącznie przy zamknięciu pozycji. Pozycja
przeceniona w międzyczasie o kilkanaście procent nie zostawiała śladu, o ile
zdążyła wrócić przed zamknięciem — raportowane obsunięcie było systematycznie
mniejsze niż prawdziwe. Naprawa: wycena co świecę (mark-to-market).

---

## 2. Ingestia danych — dwie wady tej samej rodziny

Obie produkowały **zero rekordów bez zgłoszenia błędu**, czyli najgorszy możliwy
tryb awarii: wygląda na sukces.

### 2.1 Dwie puste odpowiedzi = koniec historii

`backfill.py` uznawał dwie puste strony za koniec dostępnych danych.
Instrument notowany później niż zadany początek zwracał wtedy zero świec
i kończył się komunikatem o powodzeniu. Naprawa: pusta odpowiedź to dziura do
przeskoczenia; końcem jest dojście do teraźniejszości.

### 2.2 Wznawianie tylko do przodu

Oba backfille wznawiały od **najnowszego** posiadanego rekordu. Wystarczyło, że
proces bieżący dociągnął świeży ogonek, a zadanie „ściągnij cztery lata" widziało
„mam dane aż do teraz" i kończyło się zerem.

Trafiłem na to na żywo: worker `history` wstawił 27 najnowszych stawek
finansowania, po czym pełny backfill czterech lat zwrócił **0 stawek** dla
wszystkich instrumentów. Naprawa: liczone są oba brzegi — dziura przed
najstarszym rekordem i ogon po najnowszym.

### 2.3 Dwa kanały RSS z dziesięciu były zablokowane

- **SEC EDGAR 8-K** — HTTP 403. SEC wymaga adresu kontaktowego w `User-Agent`.
  Dodatkowo URL był **po prostu zły**: `action=getcompany` bez podanej spółki
  zwraca 200 i zero wpisów. Poprawiony na `action=getcurrent`, daje 40 wpisów.
- **Bitcoin Magazine** — HTTP 403 za Cloudflare, odrzuca każdy niebrowserowy
  nagłówek.

System zapisywał `last_status = http_403` do bazy, więc awaria nie była cicha —
ale nikt nie był o niej powiadamiany. Naprawa: nagłówek z kontaktem, zapasowy
nagłówek przeglądarkowy **wyłącznie po odmowie 403**, oraz odróżnienie stanu
`pusty_kanal` (200 + zero wpisów) od `ok`.

---

## 3. Otwarte — do Twojej decyzji

### 3.1 Rola bazy danych aplikacji — NAPRAWIONE

Aplikacja łączyła się rolą `trader`, będącą superużytkownikiem. Mogła więc
`DROP`, `TRUNCATE`, a co najgorsze `ALTER TABLE ... DISABLE TRIGGER` — czyli
wyłączyć wyzwalacz, na którym opiera się niezmienność dziennika decyzji.

Wdrożone (migracje 0013, 0014): rola `trader_app` bez własności jakiejkolwiek
tabeli. `DELETE` nadany wybiórczo, tylko na obiektach konfiguracyjnych
użytkownika. Migracje chodzą rolą właściciela, hasło roli aplikacji ustawiane
poza plikiem migracji (inaczej rotacja wymagałaby edycji zastosowanego pliku).

`db/check_privileges.py` sprawdza **obie strony** — że rola potrafi to, czego
potrzebuje, i że nie potrafi tego, przed czym chroni. Sam test pozytywny byłby
bezwartościowy: przeszedłby także dla superużytkownika. 21/21 na produkcji.

Wpadka po drodze warta odnotowania: pierwsza wersja pominęła
`public.schema_migrations`, którą czyta `/api/health`. Sonda gotowości zaczęła
zwracać 500, Kubernetes nie skierował ruchu do nowego poda i stara wersja dalej
obsługiwała żądania. Awaria nie dotarła do użytkownika — dokładnie po to jest
sonda.

### 3.2 Kopie zapasowe bazy — WDROŻONE

Cztery lata świec da się odtworzyć z giełdy. **Dziennika decyzji nie da się** —
to jedyny zapis tego, co system myślał w danym momencie, i cała podstawa oceny,
czy się uczy.

Wdrożone: `CronJob` codziennie o 03:17 UTC, zrzut na dysk hosta (poza wolumen
bazy), weryfikacja każdego archiwum przez `pg_restore --list`, retencja 14 dni.
Pierwsze uruchomienie **padło** na braku prawa zapisu (kubelet tworzy katalog
jako `root:root`, obraz bazy działa jako uid 1000) — naprawione kontenerem
inicjującym. Bez próbnego uruchomienia zadanie padałoby co noc, a jedynym
śladem byłby status `Failed` w historii.

Pozostaje: kopia leży na **tym samym serwerze** co baza. Chroni przed pomyłką
w SQL i uszkodzeniem wolumenu, nie chroni przed utratą maszyny. Zalecenie:
odkładanie zrzutu na zasób zewnętrzny.

### 3.3 Kod TOTP da się użyć dwa razy — NAPRAWIONE

`verify_totp(valid_window=1)` przyjmował kod przez ~90 sekund i nigdzie nie
zapisywaliśmy, że kod już zużyto — przechwycony działał w tym oknie wielokrotnie.
RFC 6238 §5.2 wymaga wprost odrzucenia drugiego użycia tego samego kroku.

Funkcja zwraca teraz numer zaakceptowanego kroku zamiast `bool`; krok ląduje
w `app.totp_secret.last_step` (migracja 0015) i jest podawany przy następnym
sprawdzeniu. Zapis idzie **przed** utworzeniem sesji — odwrotna kolejność
zostawiałaby okno, w którym nieudany kolejny krok pozostawia kod ważny.
9 testów, w tym kanarek sprawdzający, że ochrona **nie** blokuje prawidłowych
kolejnych logowań.

### 3.4 `constant_time_eq` był kodem martwym — NAPRAWIONE

Funkcja napisana specjalnie do porównań kryptograficznych i nieużywana nigdzie.
Podpięta do porównania kodu TOTP przy okazji §3.3.

Zabawny epilog: przepisywanie `SecretBox` (rotacja klucza) usunęło ją godzinę
później razem z blokiem, w którym siedziała. Moduł nadal importował się
poprawnie, bo `NameError` wystąpiłby dopiero w czasie wywołania — wyłapał to
dopiero import w teście. Kanarek potwierdza, że funkcja jest naprawdę używana:
jej usunięcie łamie 8 testów TOTP.

### 3.5 Rotacja `SECRETS_KEY` — WDROŻONA

Klucz bez możliwości wymiany jest zobowiązaniem na zawsze: jego zmiana czyniła
wszystkie zaszyfrowane wartości nieodczytywalnymi, bez ostrzeżenia, dopiero
przy pierwszym odczycie.

`SECRETS_KEY_OLD` (opcjonalny) trzyma poprzedni klucz — odszyfrowanie próbuje
bieżącego, potem starego; zapis idzie zawsze bieżącym. Format szyfrogramu się
nie zmienia, więc **nie ma migracji danych**.

Procedura: ustaw stary + nowy klucz → wdrożenie → `services/rotate_secrets.py
--wykonaj` → usunięcie starego klucza. Skrypt jest idempotentny, przepisuje
wiersz po wierszu w osobnych transakcjach i domyślnie tylko raportuje. Osobny
test-kanarek pokazuje, dlaczego kroku z przepisaniem nie wolno pominąć.

### 3.6 Limit wydatków na Anthropica — WDROŻONY

Pętla wiadomości nie miała górnego ograniczenia kosztu. Przy awarii deduplikacji
system analizowałby te same artykuły w kółko.

Wdrożone: tabela `news.api_spend` (migracja 0012) rejestruje **każde** wywołanie
modelu wraz z kosztem, plus twardy limit dobowy (`ANTHROPIC_DAILY_USD`, domyślnie
5 USD przy zmierzonym zużyciu ~0,60 USD/dobę). Przy okazji: koszt triażu nie był
liczony w ogóle, więc raportowana kwota była systematycznie zaniżona.

Pozostaje: ustaw również limit po stronie Anthropica (Console → Billing →
Spend limits) — limit w aplikacji nie zadziała, jeśli to sama aplikacja
zawiedzie.

### 3.7 Testy — CZĘŚCIOWO

Jest test dymny end-to-end w Playwright, który realnie chroni. Dołożone 23 testy
jednostkowe reguł ryzyka i symulacji portfela — każdy zweryfikowany kanarkiem,
czyli wstrzyknięciem znanego błędu i sprawdzeniem, że test go łapie. Jeden
kanarek okazał się za słaby i przeszedł; test został przez to wzmocniony.

Te testy wykryły realny defekt produkcyjny: kill-switch był oceniany wyłącznie
przy próbie wejścia w pozycję, więc obsunięcie kapitału podczas samego trzymania
pozycji nie uruchamiało go wcale — czyli limit nie działał w tej jednej sytuacji,
dla której istnieje.

Pozostaje: `security.py` i routery API.

### 3.8 Redundantne agregaty — USUNIĘTE, dane osierocone — ZOSTAWIONE

Agregaty ciągłe `candle_5m`, `candle_1h`, `candle_1d` nie były odczytywane przez
żaden fragment kodu, a mimo to trzy polityki odświeżały je co 1–5 minut. Tworzyły
przy tym drugie źródło tej samej prawdy — dokładnie to, przed czym ostrzegał
komentarz w migracji 0002. Zgodność zmierzona przed usunięciem: średnio
0,000006%, maksymalnie 0,033%. Usunięte migracją 0016.

25 920 świec 15-minutowych **zostawiam**, choć też są nieużywane. To prawdziwe
obserwacje rynku kosztujące ~2 MB; kasowanie realnych danych dla porządku nie
daje nic w zamian, a odtworzyć ich może się nie dać. Korygując własne wcześniejsze
sformułowanie: „osierocone" nie jest samo w sobie powodem do usunięcia.

---

## 4. Co jest zrobione dobrze

Uczciwość wymaga wskazania też tego, co się broni:

- **Bitemporalność** jest przeprowadzona konsekwentnie przez cały schemat,
  a `feat.as_of()` to jedno miejsce, w którym zapisana jest cała ochrona przed
  uczeniem się z przyszłości.
- **Dziennik decyzji jest append-only** wymuszony wyzwalaczem, nie konwencją.
- **Bezpieczeństwo kluczy na poziomie schematu**: `CHECK (NOT (active AND
  can_withdraw))` i `CHECK (watch_only)` to ograniczenia, których nie da się
  obejść błędem w kodzie aplikacji.
- **Risk kernel** sprawdza w kolejności od najszerszego do najwęższego, więc
  nie da się zmniejszyć zlecenia i wysłać go mimo aktywnego kill-switcha.
- **Migracje są niezmienne i weryfikowane sumą kontrolną** — runner odmawia
  startu, jeśli zastosowany plik się zmienił.
- **Argon2id 64 MiB / t=3 / p=4** znacząco powyżej minimum OWASP.
- Komentarze w kodzie tłumaczą **dlaczego**, nie **co** — i to właśnie one
  pozwoliły znaleźć część powyższych błędów przez porównanie deklaracji
  z implementacją.

---

## 5. Kolejność, w jakiej to bym robił

Wszystkie pozycje z pierwotnej listy są zamknięte. Zostają dwie rzeczy, które
świadomie zostawiam do decyzji:

1. **Odkładanie kopii zapasowej poza serwer** (§3.2). Kopia leży dziś na tej
   samej maszynie co baza — chroni przed pomyłką w SQL i uszkodzeniem wolumenu,
   nie chroni przed utratą serwera. To wymaga zewnętrznego zasobu i decyzji,
   gdzie ma trafiać.
2. **Testy routerów API** (§3.7). Wymagają bazy testowej, więc są innej klasy
   niż 55 testów czystej logiki, które już są.

Zrobione w trakcie tego przeglądu: §3.1 (rola bazy), §3.2 (kopie zapasowe),
§3.3 (TOTP), §3.4 (`constant_time_eq`), §3.5 (rotacja klucza), §3.6 (limit
wydatków), §3.7 (55 testów), §3.8 (agregaty), §2.3 (kanały RSS).


---

## 6. Znalezione po pierwszym przeglądzie

### 6.1 Węzeł jest przeciążony pamięciowo — DO DECYZJI

VPS ma 7,8 GB pamięci, z czego 5,3 GB jest zajęte, a wolne pozostaje ~145 MB
(2,5 GB licząc bufory). Suma limitów pamięci wszystkich podów to **186% pamięci
fizycznej**, a limitów CPU — 335%.

Overcommit sam w sobie nie jest błędem: pody rzadko sięgają limitów jednocześnie.
Ale na tej maszynie działa też Dietetyk-AI, Ogrodnik i strona RenaCode, a baza
Trader-AI zajmuje 900 MB w stanie spoczynku. Ciężkie zadanie treningowe
z limitem 2–4 GB może w takich warunkach zostać eksmitowane przez kubelet.

Nie zaobserwowałem eksmisji ani OOM w zdarzeniach — więc to jest ryzyko, nie
awaria. Wart odnotowania, bo objawiłby się jako „trening losowo się restartuje",
czyli najtrudniejsza do zdiagnozowania klasa problemu.

Zalecenie: obniżyć limity pamięci zadań okresowych do faktycznego zużycia
(trening bierze 388 MB przy limicie 2 GB) albo dołożyć pamięci do maszyny.

### 6.2 Wydajność walk-forward — NAPRAWIONE

Profiler pokazał dwie przyczyny, obie o identycznym wyniku po naprawie:

1. **Sygnały liczone od nowa dla każdej próby wag.** Sygnały nie zależą od wag;
   zależy od nich dopiero ich zsumowanie. Trener sprawdza 60 układów wag na tym
   samym oknie i liczył komplet ośmiu sygnałów od nowa dla każdego.

2. **4,3 mln wywołań `round()` na wyniki lądujące w koszu.** `combine()`
   budowało pełne rozbicie na wkłady przy każdej świecy każdej próby, a symulacja
   tego rozbicia nie czyta — jest potrzebne wyłącznie dziennikowi i interfejsowi.
   Około 30% czasu szło na zaokrąglanie liczb, które natychmiast były wyrzucane.

Pomiar: 10 prób na oknie 1080 świec × 24 instrumenty spadło z 5,68 s do 1,04 s
(**5,5×**). Na VPS-ie przebieg pozostaje rzędu godziny, bo maszyna jest wielokrotnie
wolniejsza od stacji deweloperskiej — ale iteracja lokalna stała się praktyczna.

**Sprawdzone i odrzucone jako przyczyna** (żeby nie wracać do tego drugi raz):
brak indeksu na `feat.feature_value`. `EXPLAIN ANALYZE` pokazał, że klucz główny
`(feature_def_id, instrument_id, event_time)` jest wykorzystywany prawidłowo —
zapytanie o cechy trwa 154 ms, a pełne wczytanie panelu 0,4 s na instrument.
Hipoteza była błędna i dobrze, że sprawdziłem ją planem zapytania zamiast dodać
niepotrzebny indeks na tabeli o 4,2 mln wierszy.

### 6.3 Cechy poza siatką świec — rozjazd trening–produkcja

**Najpoważniejszy błąd znaleziony w tej sesji**, i to taki, którego żaden wynik
nie sygnalizował.

Cechy cenowe powstają z `time_bucket()`, więc siedzą dokładnie na granicach
świec: 00:00, 04:00, 08:00. Cechy liczone na własnej siatce — funding i news —
zaczynały od `now() - N dni`, czyli od przypadkowej chwili z mikrosekundami,
i lądowały na znacznikach w rodzaju `16:19:49.408934`.

Pomiar przed naprawą:

| Cecha | Na siatce 4 h |
|---|---|
| `rsi__4h`, `rel_strength__4h`, pozostałe cenowe | **100%** |
| `funding_z__4h` | **0%** |
| `news_sent__4h` | **0%** |

**Skutek był asymetryczny i dlatego niewidoczny:**

- **Trening** łączy cechy z cenami przez `p.t = f.event_time`. Funding i news
  **nigdy** nie wchodziły do panelu — oba sygnały były martwe w *każdym*
  dotychczasowym przebiegu walk-forward, łącznie z tym, w którym raportowałem
  funding jako wartościowy nowy sygnał.
- **Produkcja** czyta przez `feat.as_of()`, które bierze najświeższą wartość
  każdej cechy w oknie świeżości. Tam funding działał normalnie — decyzja
  produkcyjna, którą pokazywałem, miała `funding_carry` jako drugi co do
  wielkości wkład.

To jest dokładnie ten rozjazd trening–produkcja, przed którym broni cała reszta
architektury: **silnik używał sygnału, którego trener nigdy nie widział.**

Wykryte przypadkiem: detektor martwych sygnałów zgłosił `funding_carry` jako
stały, co nie miało sensu, bo `|z| >= 1` zdarza się w 21% świec. Gdyby nie ten
detektor — dodany godzinę wcześniej z zupełnie innego powodu — błąd zostałby
w systemie.

Usunięto **1 754 706 wierszy** poza siatką (dokładnie połowę tabeli cech) i
przeliczono od nowa.

**Zabezpieczenie na przyszłość:** `loop.py --sprawdz` dostał kontrolę
strukturalną — zgłasza błąd, gdy jakakolwiek cecha horyzontu leży poza siatką.
Silnik uruchamia ją przy starcie, więc nawrót tej klasy błędu **zatrzyma pod**
zamiast cicho unieważnić trening.

To najlepszy argument za tym, że kontrola wykonująca prawdziwe zapytania jest
warta więcej niż kolejny test jednostkowy: żaden przenośny test nie miałby jak
tego zobaczyć, bo nie ma bazy.
