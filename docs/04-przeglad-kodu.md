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

### 3.3 Kod TOTP da się użyć dwa razy

`verify_totp(valid_window=1)` przyjmuje kod przez ~90 sekund i nigdzie nie
zapisujemy, że dany kod już zużyto. Przechwycony kod działa ponownie w tym oknie.
Standardowa naprawa: zapis ostatniego zaakceptowanego kroku czasowego.
Priorytet obniżony, bo 2FA jest obecnie wyłączone — ale **przed** włączeniem
trzeba to zrobić.

### 3.4 `constant_time_eq` jest kodem martwym

Funkcja napisana specjalnie do porównań kryptograficznych, nieużywana nigdzie.
Porównanie tokenu CSRF robi zwykłe `!=`. Praktyczne ryzyko jest tu bliskie zeru
(atakujący i tak nie odczyta ciasteczka), ale skoro funkcja istnieje, powinna być
podpięta albo usunięta. Zostawianie nieużywanego kodu bezpieczeństwa tworzy
złudzenie ochrony.

### 3.5 Brak procedury rotacji `SECRETS_KEY`

Klucz nie ma wersji. Jego zmiana czyni wszystkie zaszyfrowane wartości
nieodczytywalnymi — bez ostrzeżenia, przy pierwszym odczycie. Zalecenie: numer
wersji klucza w rekordzie i obsługa dwóch kluczy naraz na czas migracji.

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

### 3.8 Osierocone dane i redundantne agregaty

- 25 920 świec 15-minutowych, nieużywanych przez żaden horyzont.
- Agregat ciągły `market.candle_1h` liczy godzinówki z 1-minutówek, a od dziś
  mamy też własne świece Bybita pod `timeframe='1h'`. Dwa źródła tej samej
  prawdy to dokładnie to, przed czym ostrzega komentarz w migracji 0002.
  **Zmierzyłem rozjazd**: średnio 0,000006%, maksymalnie 0,033%. Praktycznie
  zgodne, ale należy wybrać jedno źródło i to zapisać.

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

1. **Ochrona TOTP przed powtórzeniem** (§3.3) — zanim włączymy 2FA.
2. Testy `security.py` i routerów API (§3.7 — reguły ryzyka i symulacja
   portfela mają już 23 testy).
3. Odkładanie kopii poza serwer (§3.2).
4. Rotacja `SECRETS_KEY` (§3.5), sprzątanie danych (§3.8), `constant_time_eq`
   (§3.4).

Zrobione w trakcie tego przeglądu: §3.1 (rola bazy), §3.2 (kopie zapasowe),
§3.6 (limit wydatków), §2.3 (kanały RSS), część §3.7 (testy reguł ryzyka).
