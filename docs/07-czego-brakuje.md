# Czego brakuje: przegląd projektu wobec działających botów

Zlecenie właściciela, 2026-09-07: *sprawdź, czy niczego nie pominęliśmy
w projektowaniu aplikacji.*

Przegląd zestawia to, co komercyjne systemy automatycznego handlu mają jako
standard, z tym, co **sprawdziłem w naszym kodzie** — nie z tym, co deklaruje
dokumentacja. Każda pozycja poniżej jest zweryfikowana odczytem źródeł.

---

## Co mamy mocniejsze niż typowy bot

Warto zacząć od tego, bo wyznacza, czego NIE trzeba dobudowywać.

| rzecz | stan |
|---|---|
| **Dziennik decyzji append-only** | wymuszony triggerem (`0005`), zapisywany PRZED poznaniem wyniku, ze snapshotem cech |
| **Wspólny risk kernel** | te same reguły w produkcji i w symulacji — backtest nie może mierzyć innego systemu |
| **Bitemporalność** | `feat.as_of()` filtruje po `event_time` i `ingested_at`; model nie uczy się na wiedzy z przyszłości |
| **Kill-switch z nadzorcą w osobnym procesie** | `docs/06` |
| **Metryki wyłącznie out-of-sample** z licznikiem hipotez | nie ma gdzie wpisać „wyniku backtestu" |
| **Ostrzeżenie o prawie wypłaty na kluczu API** | `bybit.py` sprawdza uprawnienia i mówi wprost |

Ślad audytowy, który w komercyjnych narzędziach bywa funkcją „instytucjonalną"
za dopłatą, jest tu warunkiem konstrukcyjnym od migracji 0005.

---

## Luki, w kolejności tego, ile kosztują przy przejściu na live

### 1. Ścieżka egzekucji do giełdy NIE ISTNIEJE

Adapter Bybit jest **wyłącznie do odczytu**: świece, finansowanie, arkusz, salda,
sprawdzenie połączenia. W całym repozytorium nie ma ani jednego wywołania
`/v5/order`. Cała egzekucja to wewnętrzny rachunek papierowy.

To jest znane i zapisane w README, ale konsekwencje projektowe są większe, niż
sugeruje jedno zdanie o „wąskim gardle podmiotu unijnego". Wszystko poniżej
w tej sekcji jest tego skutkiem — nie osobnymi brakami.

**Idempotencja zleceń.** Przy składaniu zlecenia przez sieć odpowiedź może nie
dojść, mimo że zlecenie weszło. Bez identyfikatora nadanego przez nas
(`orderLinkId` u Bybita) ponowienie tworzy DRUGĄ pozycję. Nie mamy tego, bo nie
mamy składania — ale to musi powstać razem z nim, nie po nim.

**Wypełnienia częściowe.** Symulacja zna dwa stany: wypełnione albo nie
(`portfolio._wypelnione`). Giełda zna trzeci — wypełnione w części. Strategia
zaprojektowana na wypełnienia całkowite musi wiedzieć, co zrobić z połową.

**Uzgadnianie z giełdą.** Nikt nie porównuje naszego obrazu pozycji z tym, co
giełda faktycznie trzyma. Na rachunku papierowym to tautologia; na żywym jest
jedyną obroną przed cichym rozjazdem stanu.

### 2. Wpłaty i wypłaty nie istnieją w modelu kapitału

```python
# services/engine/loop.py
equity = START_EQUITY + realized + unrealized
```

Kapitał to **stała** plus wynik handlu. Nie ma pojęcia wpłaty ani wypłaty.

Na rachunku papierowym jest to poprawne. Na żywym — przy wpłacie 10 000 USD,
o której mowa w `docs/05` — każde dołożenie środków wygląda jak zysk, a każda
wypłata jak strata. Uderza to jednocześnie w **cztery** mechanizmy:

- `peak` skacze przy wpłacie, więc limit obsunięcia liczy się od zawyżonego szczytu,
- `day_ago` przy wypłacie pokazuje stratę dzienną, której nie było,
- **`min_equity` — próg kapitału z `docs/05` — przestaje znaczyć to, co miał**,
- krzywa kapitału w interfejsie miesza wynik strategii z ruchami gotówki.

To jest luka **przed** wpłatą realnych środków, nie po niej.

### 3. Poślizg jest ZAŁOŻONY, nie mierzony

```python
POSLIZG_TAKER = 0.0004     # 4 punkty bazowe
POSLIZG_MAKER = 0.0        # zero
```

Dwie stałe. Nigdzie w projekcie nie ma pomiaru poślizgu faktycznego — mimo że
mamy do tego dane: `market.book_snapshot` trzyma arkusz, więc różnicę między
ceną decyzji a ceną wykonania da się policzyć, gdy tylko pojawi się egzekucja.

Waga tego jest policzalna. Runda kosztuje 0,200%, a zmierzona mediana zwrotu
na transakcję jest rzędu dziesiątych procenta. **Błąd o 4 punkty bazowe w tę
czy w tamtą przesuwa wynik o kilkanaście procent jego wartości** — czyli mniej
więcej tyle, ile cała zmierzona przewaga.

`POSLIZG_MAKER = 0` jest przy tym założeniem najmocniejszym: mówi, że zlecenie
z limitem wykonuje się dokładnie po cenie limitu. Jest to prawda z definicji
**wtedy, gdy się wykona** — a model wypełnień to obsługuje. Ale nie obejmuje
przypadku, w którym część zlecenia wchodzi po cenie gorszej.

### 4. Ponawianie u giełdy jest zadeklarowane, ale nikt go nie robi

`ExchangeError` niesie flagę `retryable`, ustawianą poprawnie dla przekroczeń
czasu, błędów połączenia i HTTP 429. Czyta ją **dokładnie dwóch** odbiorców:
`backfill.py` i `funding.py`, i to tylko po to, żeby przerwać pętlę.

Nie ma nigdzie wykładniczego odczekania ani ponowienia. Flaga opisuje intencję,
której nikt nie wykonuje — dokładnie ten sam kształt, co niedawno naprawiony
brak ponawiania u dostawcy analizy wiadomości, gdzie objawiało się to jako
„błędów 7" i cisza.

### 5. Brak eksportu transakcji

Żaden endpoint nie oddaje historii w formacie do dalszego użytku. Przy handlu
automatycznym liczba transakcji rośnie szybko, a każda jest zdarzeniem
podatkowym. Dane są w bazie i są kompletne — brakuje wyłącznie wyjścia.

To jedyna pozycja na tej liście, którą można zbudować **w każdej chwili**, bo
nie zależy od egzekucji.

### 6. Dryf zegara nie jest sprawdzany

Podpis zapytania prywatnego u Bybita zawiera znacznik czasu i `recv_window`.
Zegar rozjechany o więcej niż to okno powoduje odrzucanie **wszystkich** zapytań
prywatnych — objawia się to jako błąd uwierzytelnienia, czyli kieruje szukanie
w stronę klucza zamiast w stronę czasu.

Kontener nie ma własnego NTP; polega na zegarze węzła. Nie jest to dziś problem
i może nim nigdy nie być — ale koszt sprawdzenia to jedno zapytanie
o `serverTime` przy starcie.

### 7. Rozdzielenie uprawnień bazodanowych

Silnik, dozorca i API chodzą na tej samej roli `trader_app`. Nic nie **wymusza**,
że silnik nie zwolni sobie kill-switcha — dziś tego nie robi, bo nie ma takiego
kodu, a nie dlatego, że nie może. Zapisane już w `docs/06` jako świadomy dług.

---

## Czego świadomie NIE robimy

Nie każda luka jest brakiem.

- **Sub-konto na giełdzie.** Standardowe zalecenie: bot handluje z odizolowanego
  salda. U nas konto demo jest z natury odseparowane; przy przejściu na live to
  staje się wymogiem, nie dobrą praktyką.
- **Zlecenia stop-loss i take-profit po stronie giełdy.** Strategia wychodzi
  decyzją na zamknięciu świecy, a nie progiem cenowym. Zlecenie ochronne
  u brokera zmieniłoby strategię, a nie tylko jej wykonanie — to hipoteza do
  zmierzenia, nie brakująca funkcja.
- **Więcej ustawień w interfejsie.** Im więcej pokręteł, tym więcej stopni
  swobody i tym łatwiej dopasować się do przeszłości. Ograniczenie jest tu
  celowe.

---

## Kolejność, gdyby robić to jutro

1. **Wpłaty i wypłaty w modelu kapitału** — jedyna luka, która psuje działające
   dziś zabezpieczenie (`min_equity`), i to zanim ktokolwiek wpłaci pieniądze.
2. **Eksport transakcji** — tani, niezależny od egzekucji, rośnie z czasem.
3. **Ponawianie u giełdy** — flaga już jest, brakuje pętli.
4. **Sprawdzenie zegara przy starcie** — jedno zapytanie.
5. Reszta wchodzi razem z egzekucją i nie ma sensu wcześniej.
