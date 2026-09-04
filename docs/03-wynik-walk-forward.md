# Pierwszy walk-forward: wynik negatywny

Data: 2026-08-27. Dane: 90 dni realnych świec 1-minutowych z Bybita
(BTCUSDT, ETHUSDT, SOLUSDT), 697 tys. wartości cech.

## Wynik

| Miara | Wartość |
|---|---|
| Okna walk-forward (informatywne) | 29 |
| Okna dodatnie out-of-sample | **7 (24,1%)** |
| Mediana Sharpe na transakcję | **−0,152** |
| Najgorsze obsunięcie w oknie testowym | −8,02% |
| Przetestowanych konfiguracji wag | 1 520 |
| Stan | **degraded** |

Próg przejścia na live to 70% dodatnich okien. Wynik jest od niego odległy
i **nie jest to kwestia dostrojenia** — 1 520 przetestowanych konfiguracji wag
nie dało układu, który wychodziłby dodatni poza próbką.

Wersja modelu została zapisana jako `candidate`, **nie** `active`. Silnik dalej
pracuje na jawnych założeniach początkowych; żadne przeuczone wagi nie zostały
promowane. To zadziałało tak, jak miało.

## Diagnoza: to nie są koszty, to brak przewagi

Rozdzielenie dwóch hipotez przez uruchomienie tej samej symulacji z kosztami i bez:

| | Sharpe / transakcję | Zwrot łączny | Średni zwrot na transakcję |
|---|---|---|---|
| Z kosztami | −0,204 | −65,95% | −0,084% |
| **Bez jakichkolwiek kosztów** | **−0,009** | **−2,93%** | **−0,004%** |

Koszt rundy: 0,190% (prowizja 0,055% × 2 + poślizg 0,04% × 2).

**Przewaga brutto wynosi −0,004% na transakcję, czyli statystycznie zero.**

To rozróżnienie jest kluczowe. Gdyby brutto wynosiło np. +0,15% przy koszcie
0,19%, wniosek brzmiałby „handluj rzadziej i taniej" — sygnały miałyby treść,
tylko egzekucja by ją zjadała. Tutaj brutto jest zerowe, więc rzadszy handel
oznacza jedynie wolniejsze tracenie.

**Obecny zestaw sygnałów nie ma zdolności predykcyjnej na świecach
5-minutowych.**

## Dlaczego to był przewidywalny wynik

Sygnały w wersji pierwszej to podręcznikowe wskaźniki techniczne — RSI, iloraz
EMA, zwroty, z-score wolumenu, pozycja w zakresie — zastosowane do
pięciominutowych świec najpłynniejszych par krypto. To najbardziej zarbitrażowany
fragment rynku, obserwowany przez tysiące uczestników z lepszą infrastrukturą.
Znalezienie tam przewagi tymi narzędziami byłoby zaskoczeniem, nie normą.

Wartością tego etapu nie jest wynik, tylko to, że **system go wykrył i pokazał**,
zamiast wyprodukować ładny backtest. Dokładnie po to zbudowano walk-forward,
licznik hipotez i blokadę promocji.

Warto też zauważyć: 24% dodatnich okien to **mniej niż 50% oczekiwane od rzutu
monetą**. Przy zerowej przewadze brutto i realnych kosztach tak właśnie wygląda
rozkład — nie ma tu paradoksu.

## Co dalej — hipotezy warte przetestowania

Kolejność odzwierciedla stosunek szansy na przewagę do kosztu wdrożenia.

1. **Rzadsze decyzje na dłuższym horyzoncie.** 1 657 transakcji w 90 dni na trzech
   instrumentach to ~6 dziennie na instrument. Przy dowolnej realnej przewadze
   ta częstotliwość jest zabójcza. Świece 1 h / 4 h zamiast 5 min.
2. **Mikrostruktura orderbooka.** Zbieramy już snapshoty (imbalance, spread),
   ale żaden sygnał ich nie używa. Nierównowaga kolejki ma udokumentowaną,
   krótkoterminową wartość predykcyjną — w przeciwieństwie do RSI.
3. **Funding rate perpetuali.** Koszt utrzymania pozycji jest bezpośrednim
   sygnałem pozycjonowania rynku i jest publiczny.
4. **Przekrojowość zamiast kierunku.** Siła względna BTC/ETH/SOL wobec siebie
   zamiast prognozowania kierunku każdego z osobna — pozycja neutralna rynkowo
   eliminuje największe źródło szumu.
5. **Sygnał newsowy z filtrem nowości.** Jedyny nieprzetestowany element
   pierwotnego projektu; wymaga klucza Anthropic.

Każda z nich to nowa hipoteza, którą trzeba przepuścić przez ten sam walk-forward.
Licznik przetestowanych konfiguracji rośnie dalej — i o to chodzi, żeby przy
ewentualnym późniejszym sukcesie dało się uczciwie zdyskontować wynik przez
liczbę podejść.

## Czego NIE należy robić

- **Nie dostrajać progów wejścia/wyjścia, aż wynik wyjdzie dodatni.** Przy 29
  oknach i zerowej przewadze brutto zawsze da się znaleźć próg, który wygląda
  dobrze. To jest definicja przeuczenia i dlatego istnieje licznik hipotez.
- **Nie wydłużać okna treningowego w nadziei na lepsze dopasowanie.** Problem
  nie leży w za małej próbce do dopasowania, tylko w braku sygnału do dopasowania.
- **Nie promować modelu `candidate` ręcznie.** Blokada jest jedyną rzeczą, która
  dzieli ten system od handlowania na podstawie szumu.

---

# Druga iteracja: świeca 4 h, 12 instrumentów

Data: 2026-08-27, kilka godzin po pierwszej. Dane: 4 lata (2022-08 … 2026-08),
420 tys. świec godzinowych, 1,1 mln wartości cech, 12 instrumentów.

## Wynik

| Miara | 5 min / 3 instr. | **4 h / 12 instr.** |
|---|---|---|
| Okna informatywne | 29 | **33** |
| Okna dodatnie OOS | 7 (24,1%) | **20 (60,6%)** |
| Mediana Sharpe/transakcję | −0,152 | **+0,153** |
| Najgorsze obsunięcie | −8,02% | **−37,07%** |
| Konfiguracji przetestowanych | 1 520 | 2 400 |
| Stan | degraded | **learning** |

Próg przejścia na live to 70% dodatnich okien. Jest 61%.

## Czy to zasługa kalibracji, czy samego horyzontu?

Porównanie na tych samych danych i tym samym protokole, przy **stałych** wagach
(brak dopasowania, więc wynik jest w pełni out-of-sample):

| Wagi | Okna | Dodatnie | Mediana Sharpe |
|---|---|---|---|
| Założenia początkowe (ręczne) | 40 | 17 (42,5%) | −0,043 |
| Równe (1/7 każda) | 21 | 8 (38,1%) | −0,065 |

Sprawdzałem hipotezę, że kalibracja tylko spłaszcza wagi w stronę równych —
wyuczone wagi są wyraźnie bardziej płaskie niż ręczne założenia. **Hipoteza
okazała się fałszywa**: równe wagi wypadają gorzej niż ręczne. Poprawa nie
bierze się z samego spłaszczenia.

Legalne porównanie brzmi więc: **42,5% (ustalone wagi) → 60,6% (kalibracja
walk-forward)**, przy czym ta druga liczba pochodzi z protokołu, w którym wagi
każdego okna dobierano wyłącznie na jego własnym oknie treningowym.

**Czego NIE wolno z tego cytować.** Uruchomiłem też ocenę uśrednionych wag
produkcyjnych na tych samych oknach, z których je wyliczono — wyszło 62,5%.
Ta liczba jest skażona, bo dane oceny brały udział w doborze wag. Zostawiam ją
tu wyłącznie jako ostrzeżenie, że da się ją policzyć i że wygląda przekonująco.

## Zastrzeżenie, które waży więcej niż wynik

**Najgorsze obsunięcie w oknie testowym wynosi −37%, a skonfigurowany limit
`max_drawdown` to 10% z akcją `killswitch`.**

Symulacja walk-forward **nie stosuje risk kernela**: nie skaluje pozycji, nie
zna limitu ekspozycji, nie zatrzymuje się po serii strat. To znaczy, że wyniki
powyżej nie są osiągalne w tej konfiguracji — realny system zatrzymałby handel
zanim doszedłby do połowy tego obsunięcia.

Poprzednie −8,02% nie jest z tym porównywalne: liczone było wyłącznie na wyniku
zrealizowanym, więc systematycznie zaniżone (patrz przegląd kodu §1.8).

Wniosek: zanim cokolwiek pójdzie na żywo, symulacja musi przejść przez ten sam
risk kernel co produkcja. Dopóki tego nie robi, mierzy strategię, której system
i tak nie wykonałby.

## Interpretacja

Wynik przeszedł z „brak przewagi" (24%, mediana ujemna) do „coś tu jest, ale za
mało" (61%, mediana dodatnia). To istotna zmiana, ale **61% nie jest przewagą** —
przy 33 oknach i instrumentach silnie skorelowanych ze sobą (efektywna liczba
niezależnych obserwacji jest mniejsza niż 33) taki wynik mieści się w rozrzucie,
jakiego można oczekiwać przy niewielkiej przewadze i przy jej braku.

Doszła też selekcja na poziomie meta: horyzont 4 h wybrałem PO zobaczeniu, że
5 min zawodzi. To jest forma wielokrotnego testowania, której licznik hipotez
nie łapie, bo liczy konfiguracje wag, nie decyzje projektowe.

## Co dalej

1. **Risk kernel w symulacji** — bez tego wyniki opisują strategię, której
   system nie wykona.
2. Pozostałe hipotezy z pierwszej iteracji: mikrostruktura orderbooka,
   przekrojowość (siła względna zamiast kierunku), sygnał newsowy.
3. Dopiero potem — kolejny walk-forward, z pełną świadomością, że licznik
   hipotez urósł do 2 400 i każdy następny wynik trzeba o to dyskontować.

---

# Trzecia iteracja: risk kernel w symulacji, 24 instrumenty

Data: 2026-08-27, tego samego dnia co dwie poprzednie.

## Co się zmieniło i dlaczego

Druga iteracja raportowała obsunięcie −37% przy skonfigurowanym kill-switchu na
10%. Opisywała więc strategię, której ten system **nie wykonałby** — a to jest
gorsze niż brak backtestu, bo wygląda na wynik.

Przyczyna była strukturalna: symulacja liczyła każdy instrument osobno. Limit
łącznej ekspozycji, limit częstotliwości zleceń i kill-switch są własnościami
**konta**, więc dwanaście niezależnych symulacji nigdy ich nie naruszy. Strata
na SOL nie wstrzymywała handlu na BTC, choć w produkcji wstrzymałaby.

Symulacja jest teraz portfelowa i przechodzi przez **tę samą funkcję limitów**
co produkcja (`services/engine/risk_rules.py`). Napisanie drugiej implementacji
„na potrzeby backtestu" byłoby gwarancją rozjazdu przy pierwszej zmianie reguł.

## Skutek: −37% było artefaktem

| | Bez risk kernela | Z risk kernelem |
|---|---|---|
| Najgorsze obsunięcie | −37,07% | **−1,21%** |

Limit łącznej ekspozycji to 15 000 na 100 000 kapitału, więc nawet całkowita
strata wszystkich pozycji daje −15%. Poprzednia liczba brała się z tego, że
symulacja per instrument angażowała cały kapitał na każdym z osobna.

To działa w obie strony i trzeba to powiedzieć wprost: **zyski są równie
ograniczone**. System ryzykuje najwyżej 15% kapitału naraz, więc i zarabia
proporcjonalnie mniej, niż sugerowały wcześniejsze liczby.

## Trzy błędy w rozdziale kapitału, znalezione przy okazji

1. **Przesubskrybowanie.** Bazowa wielkość zlecenia 3 000 przy limicie 15 000
   i dwunastu instrumentach to chęć zaangażowania 36 000 — dwa i pół raza ponad
   limit. Mieściło się pięć pozycji z dwunastu. Nie było to bezpieczniejsze
   (ryzyko wyznacza limit, nie liczba odrzuconych zleceń), tylko bardziej
   skoncentrowane. Budżet liczony jako `limit / liczba_instrumentów` daje pełną
   dywersyfikację przy **niezmienionym** ryzyku łącznym.

2. **Przydział alfabetyczny.** Przy niedoborze kapitału o tym, które wejścia
   przejdą, decydowała kolejność rozpatrywania — a instrumenty szły
   `ORDER BY symbol`. ADAUSDT był systematycznie faworyzowany względem XRPUSDT.
   Pętla rozdzielona na dwa przebiegi: najpierw ocena wszystkich, potem
   działanie w kolejności malejącej siły sygnału.

3. **Młody instrument kasował całe okno.** Warunek dopuszczenia okna sprawdzał
   `min(długość_szeregu)` po **wszystkich** instrumentach. SUI, TIA i SEI weszły
   na giełdę w 2023, więc we wcześniejszych oknach ich szereg jest pusty i `min()`
   wynosiło zero. Kosztowało to dziewięć okien z czterdziestu — prawie czwartą
   część dowodów, bez żadnego komunikatu.

## Błąd produkcyjny znaleziony przez testy

Test symulacji nie potrafił doprowadzić do uruchomienia kill-switcha mimo straty
przekraczającej próg. Przyczyna okazała się produkcyjna:

```python
verdict = risk.check(...) if action.startswith("enter") else <allow>
```

Limity były oceniane **wyłącznie przy próbie wejścia w pozycję**. Jeśli silnik
trzymał otwarte pozycje i nie próbował wchodzić w nowe — a właśnie tak zachowuje
się podczas gwałtownego spadku, bo sygnały są wtedy niespójne — obsunięcie
kapitału nie było przez nikogo oceniane. **Kill-switch na 10% nie miał szans
zadziałać dokładnie w tej sytuacji, dla której istnieje.**

Naprawa: `evaluate_account()` wołane raz na cykl, niezależnie od tego, czy
silnik chce w cokolwiek wejść.

## Uczciwie o liczbie decyzji projektowych

W ciągu jednego dnia zmieniłem: horyzont (5 min → 4 h), liczbę instrumentów
(3 → 12 → 24), sposób liczenia progu wejścia (stała → percentyl), jednostkę
oceny (per instrument → portfel) i regułę dopuszczania okna. Każda z tych zmian
ma uzasadnienie **niezależne od wyniku** — koszt względem amplitudy, moc
statystyczna, zgodność z produkcją, poprawność.

Ale wszystkie zapadły **po zobaczeniu, że poprzednia wersja zawodzi**, i licznik
hipotez (2 400) tego nie łapie, bo liczy konfiguracje wag, nie decyzje
projektowe. Przy czytaniu każdego wyniku z tej serii trzeba to trzymać w głowie.
Prawdziwym testem będzie zachowanie na danych, których jeszcze nie ma — czyli
najbliższe miesiące fazy demo.

## Wynik trzeciej iteracji

| Miara | 5 min / 3 instr. | 4 h / 12 instr. | **4 h / 24 instr. + risk kernel** |
|---|---|---|---|
| Okna informatywne | 29 | 33 | **29** |
| Okna dodatnie | 24,1% | 60,6% | **55,2%** |
| Mediana Sharpe/transakcję | −0,152 | +0,153 | **+0,038** |
| Najgorsze obsunięcie | −8,02% | −37,07% | **−1,97%** |
| Błąd kalibracji (ECE) | — | — | 0,124 |

Stan: `learning`. Próg przejścia na live to 70% dodatnich okien; jest 55%.

### Dlaczego wynik SPADŁ i dlaczego to dobrze

Przed poprawką „młody instrument nie kasuje okna" walk-forward liczył 25 okien
i dawał 64% dodatnich przy medianie +0,198. Po poprawce liczy 29 okien — 55%
i +0,038.

Dziewięć dołożonych okien to okresy z lat 2022–2023, wcześniej **po cichu
pomijane**, bo SUI, TIA i SEI nie były jeszcze notowane. Były trudniejsze
i obniżyły wynik.

To jest właściwy kierunek dla poprawki poprawnościowej: usunęła korzystne
obciążenie, którego nikt nie wprowadził celowo i którego nic nie sygnalizowało.
Wynik 64% opisywał strategię ocenianą wyłącznie na łatwiejszym fragmencie
historii.

### Czego algorytm się nauczył

Najciekawsza zmiana jest w reżimie konsolidacji (`range`), gdzie założenia
początkowe stawiały na powrót do średniej:

| Sygnał | Założenie | Wyuczona | Zmiana |
|---|---|---|---|
| Powrót do średniej przy RSI | 0,26 | 0,089 | **−0,171** |
| Pozycja przy krawędzi zakresu | 0,22 | 0,110 | −0,110 |
| Zgodność zwrotów (momentum) | 0,08 | 0,178 | **+0,098** |
| Kierunek trendu z EMA | 0,08 | 0,147 | +0,067 |
| Nietypowy wolumen | 0,09 | 0,156 | +0,066 |
| Skrajne finansowanie | 0,15 | 0,182 | +0,032 |

Model **odrzucił podręcznikowe założenie**, że w konsolidacji opłaca się grać na
powrót do średniej, i przesunął wagę do momentum. Nie twierdzę, że to prawda
o rynku — twierdzę, że tak wypadło na czterech latach danych, out-of-sample,
i że jest to konkretna, sprawdzalna zmiana w rozumowaniu systemu, a nie
ogólnik o „uczeniu się".

Wagi w każdym reżimie sumują się do jedności, więc wzrost jednego sygnału zawsze
odbywa się kosztem pozostałych.

### Kalibracja

ECE 0,124 znaczy, że deklarowana pewność rozmija się z trafnością średnio
o 12 punktów procentowych. To dużo. Warstwa uczenia mierzy to od dziś poprawnie
(wcześniej wykres kalibracji porównywał wynik sam ze sobą), więc jest to
pierwszy uczciwy odczyt tej miary — i pierwsza rzecz do poprawy w kolejnej
iteracji.

---

# Czwarta iteracja: sygnał przekrojowy

## Hipoteza

Wszystkie dotychczasowe siedem sygnałów opisuje instrument **sam ze sobą**:
jego własny zwrot, jego własne RSI, jego własną zmienność. W rynku, w którym
wszystko porusza się razem — a krypto jest takim rynkiem — większość tej
informacji to wspólny czynnik, czyli po prostu „czy dziś rośnie cały rynek".

Ranking zwrotów odejmuje ten czynnik z definicji: gdy wszystko rośnie o 5%,
każdy instrument ma ten sam zwrot bezwzględny, ale ranking dalej mówi, kto
rośnie mocniej od reszty.

Kierunek wybrany **przed** zobaczeniem wyniku: momentum przekrojowe (mocniejsi
utrzymują przewagę), bo na horyzoncie tygodniowym to ten efekt jest
udokumentowany.

**Druga hipoteza z pierwszej iteracji — mikrostruktura orderbooka — nie jest
jeszcze testowalna.** `market.book_snapshot` ma retencję 90 dni i zbieramy go od
niedawna: 5 686 snapshotów z jednego dnia. Wróci, gdy uzbiera się historia.

## Wynik

| | 7 sygnałów | **8 sygnałów** |
|---|---|---|
| Okna informatywne | 29 | **38** |
| Okna dodatnie | 55,2% | **57,9%** |
| Mediana Sharpe | +0,038 | **+0,069** |
| Obsunięcie | −1,97% | −1,90% |
| Błąd kalibracji (ECE) | 0,124 | **0,083** |

## Czego to NIE dowodzi

Kalibracja **obniżyła** wagę nowego sygnału: 0,15 → 0,085 w trendzie i
0,14 → 0,089 w konsolidacji. W reżimie trendu jest to najniżej ważony sygnał
z ośmiu.

Walk-forward mówi więc: siła względna, tak jak ją policzyłem, **nie zasłużyła na
swoje miejsce**. Poprawa wyniku (55,2% → 57,9%) bierze się głównie z większej
liczby informatywnych okien, nie z wartości nowego sygnału.

Wariant odwrotny — powrót do średniej przekrojowej — pozostaje osobną hipotezą.
Nie odwracam znaku i nie liczę drugi raz: to byłoby dobieranie kierunku pod
wynik, czyli dokładnie to, przed czym broni cały ten protokół.

## Martwy sygnał zjadał 17% budżetu wag

Przy okazji znalazłem coś, co psuło wszystkie dotychczasowe pomiary pewności.

`news_sent` jest **stale zerowy** na całej czteroletniej historii — nie ma
klucza Anthropic, więc skaner nie działa. Mimo to losowe przeszukiwanie
przypisało sygnałowi `news_shock` wagę **0,169 w reżimie konsolidacji**, czyli
najwyższą w tym reżimie. Przeszukiwanie nie miało jak tego zauważyć: martwy
sygnał daje wkład zero niezależnie od wagi, więc żadna waga nie pogarszała
wyniku.

Konsekwencje były trzy i tylko pierwsza jest nieszkodliwa:

1. Wynik złożony nie zmieniał się — wkład zerowego sygnału to zero.
2. **Pewność była systematycznie zaniżana**, bo martwy sygnał wchodzi do
   mianownika (`conf_den += weight`) z własną pewnością 0,1. To jest ta sama
   pewność, której kalibrację mierzyłem i która wyszła rozjechana o 12 punktów
   procentowych.
3. Waga była **całkowicie przypadkowa** i w dniu wpięcia klucza nagle
   zaczęłaby działać, nigdy nie będąc skalibrowana.

Trener wyklucza teraz z kalibracji sygnały bez zmienności na oknie treningowym
(niezerowy wynik rzadziej niż w 1% świec) i renormalizuje resztę, żeby zwolniony
budżet trafił do żywych sygnałów zamiast zniknąć.

Próg 1%, nie 0%, celowo: sygnał odzywający się rzadko to co innego niż sygnał
martwy — i jest na to osobny test.

---

# Piąta iteracja: pomiar bez znanych obciążeń

Po naprawie siatki cech i wykluczeniu martwych sygnałów z kalibracji.

| Miara | Wartość |
|---|---|
| Okna informatywne | **40 z 40** |
| Okna dodatnie OOS | **21 (52,5%)** |
| Mediana Sharpe/transakcję | **+0,029** |
| Najgorsze obsunięcie | **−0,84%** |
| Błąd kalibracji (ECE) | 0,109 |
| Przetestowanych konfiguracji | 8 700 |

## Najważniejsza obserwacja z całej serii

Wynik **spadał za każdym razem, gdy usuwałem jakieś obciążenie pomiaru**:

| Zmiana | Dodatnie okna |
|---|---|
| 5 min, 3 instrumenty | 24,1% |
| 4 h, 12 instrumentów, bez risk kernela | 60,6% |
| + risk kernel, 24 instrumenty | 64,0% |
| + młody instrument nie kasuje okna | 55,2% |
| + sygnał przekrojowy | 57,9% |
| **+ naprawiona siatka cech, martwe sygnały wykluczone** | **52,5%** |

Dwie ostatnie poprawki to czysta poprawność pomiaru — nie zmieniają strategii,
tylko przestają ją faworyzować. Obie obniżyły wynik.

To jest sygnatura systemu, który sam sobie schlebiał. Teraz przestał.

**52,5% dodatnich okien przy medianie Sharpe +0,029 to wynik nieodróżnialny od
rzutu monetą.** Próg przejścia na live wynosi 70%. Strategia w obecnej postaci
**nie wykazuje przewagi** — i to jest najbardziej wiarygodna liczba, jaką mam,
bo policzona po usunięciu wszystkich obciążeń, które udało mi się znaleźć.

## Co mówi kalibracja wag

`news_shock` ma teraz jawne zero — bez klucza Anthropic sygnał nie ma z czego
powstać i kalibracja nie przypisuje mu już przypadkowej wagi.

`funding_carry`, po raz pierwszy faktycznie obecny w treningu, wylądował
praktycznie na swoim założeniu początkowym (0,10 → 0,092 w trendzie,
0,13 → 0,139 w konsolidacji). Kalibracja ani go nie faworyzuje, ani nie odrzuca.

Największy zysk zanotował `volume_breakout` (+0,071 i +0,088), a największą
stratę `mean_reversion_rsi` w konsolidacji (0,22 → 0,111) — powtórzenie
obserwacji z trzeciej iteracji, tym razem na czystszych danych.

## Co dalej

1. **Mikrostruktura orderbooka** — jedyna nieprzetestowana hipoteza z pierwszej
   iteracji. Wymaga uzbierania historii (dziś jeden dzień przy retencji 90 dni).
2. **Sygnał newsowy** — czeka na klucz.
3. **Przekrojowy powrót do średniej** — wariant odwrotny do przetestowanego.

Żadna z nich nie jest obietnicą. Cztery iteracje w jeden dzień pokazały przede
wszystkim, ile pomiar potrafi kłamać, zanim się go naprawi.

---

# Szósta iteracja: dlaczego to nie działa — odpowiedź ilościowa

Po naprawie datowania cech, dopuszczeniu wag ujemnych i wyliczaniu wag ze
zdolności predykcyjnej zamiast losowania.

## Wyniki kolejnych prób

| Metoda doboru wag | Dodatnie okna | Mediana Sharpe |
|---|---|---|
| Losowanie, wagi nieujemne | 50,0% | +0,016 |
| Losowanie, wagi ze znakiem | 55,0% | +0,022 |
| + kandydat z wag liczonych z IC | 52,5% | +0,020 |

Wynik trzyma się w przedziale 50–55% **niezależnie od tego, co robię
z ważeniem**. To jest przedział, jaki daje przypadek.

Wagi z IC wygrały tylko w **7 z 40 okien** — na oknie treningowym przegrywały
z najlepszym losowaniem. To samo w sobie jest informacją: selekcja po Sharpie
okna treningowego wybiera to, co najlepiej dopasowało się do szumu tego okna,
a nie to, co uogólnia.

## Dlaczego żadne ważenie nie pomoże

Zmierzona siła sygnałów (IC) przełożona na przewagę w procentach, przy
odchyleniu zwrotu 3,224% na horyzoncie 12 godzin i koszcie rundy 0,190%:

| Sygnał | \|IC\| | Przewaga / transakcję | Pokrycie kosztu |
|---|---|---|---|
| volume_breakout | 0,039 | 0,1257% | **66%** |
| mean_reversion_rsi | 0,036 | 0,1161% | 61% |
| trend_follow | 0,022 | 0,0709% | 37% |
| range_extreme | 0,019 | 0,0613% | 32% |
| momentum_multi | 0,019 | 0,0613% | 32% |
| cross_section | 0,015 | 0,0484% | 25% |
| funding_carry | 0,009 | 0,0290% | 15% |

**Żaden pojedynczy sygnał nie pokrywa własnych kosztów.** Najmocniejszy dochodzi
do 66%; brakuje mu 0,064 punktu procentowego na transakcję.

Górna granica dla złożenia wszystkich ośmiu, przy **założeniu ich niezależności**:
IC = 0,066, przewaga 0,212%, czyli **112% kosztu**. To jest granica nieosiągalna,
bo sygnały liczone z tej samej ceny są ze sobą silnie skorelowane — a i tak
oznaczałaby ledwie wyjście na zero.

To wyjaśnia wszystkie dotychczasowe wyniki naraz. Strategia nie „nie znalazła
jeszcze przewagi" — ona **strukturalnie nie ma z czego jej wziąć** przy tych
sygnałach i tych kosztach.

## Co realnie mogłoby to zmienić

W kolejności dźwigni, nie łatwości:

1. **Zlecenia z limitem zamiast rynkowych.** Symulacja zakłada prowizję taker
   (0,055%). Bybit liczy maker zwykle 0,02%, a przy wyższych progach mniej.
   Koszt rundy spadłby z 0,190% do ~0,120%, a najmocniejszy sygnał pokrywałby
   **105% zamiast 66%**. To jedyna dźwignia, która sama z siebie przestawia
   bilans — reszta wymaga znalezienia lepszych sygnałów.

   Cena: zlecenie z limitem nie zawsze się wypełnia. Trzeba wymodelować
   prawdopodobieństwo wypełnienia, inaczej backtest znowu zacznie kłamać —
   tym razem w drugą stronę.

2. **Sygnały o wyższym IC.** Mikrostruktura orderbooka (zbieramy od wczoraj,
   testowalna za kilka miesięcy) i news (czeka na klucz) to jedyne dwa źródła
   w projekcie, które nie są przetworzoną ceną.

3. **Rzadszy handel.** Koszt amortyzuje się na większym ruchu, ale świeca
   dobowa daje 1 460 obserwacji przez 4 lata — za mało na uczciwy walk-forward.

## Czego NIE zrobię

Nie będę dalej dobierał wag. Pomiar mówi, że wąskim gardłem nie jest sposób
ważenia — jest nim to, że **suma dostępnej informacji nie pokrywa kosztów
transakcyjnych**. Kolejne metody optymalizacji będą tylko coraz sprytniej
dopasowywać się do szumu.

---

# Sprostowanie: porównywałem liczby nierozróżnialne

Cztery kolejne dobowe przebiegi trenera — **ten sam kod, to samo ziarno losowe**,
różniące się wyłącznie przesunięciem okna o jeden dzień:

| Data | Wynik |
|---|---|
| 2026-08-28 | 52,5% |
| 2026-08-29 | 57,5% |
| 2026-08-30 | 60,0% |
| 2026-08-31 | 62,5% |

Dziesięć punktów w trzy dni, bez żadnej zmiany w kodzie.

## Przedział ufności

Przy 40 oknach przedział ufności 95% (Wilson) dla odsetka dodatnich okien ma
szerokość **około 30 punktów procentowych**:

| Wynik | Przedział 95% |
|---|---|
| 50,0% | 35,2% – 64,8% |
| 52,5% | 37,5% – 67,1% |
| 57,5% | 42,2% – 71,5% |
| 62,5% | 47,0% – 75,8% |

Te przedziały niemal całkowicie się pokrywają.

## Co to znaczy dla wszystkiego, co raportowałem wcześniej

Porównania między iteracjami — 55,2% wobec 57,9%, 52,5% wobec 55,0%, 50,0%
wobec 52,5% — **były porównaniami liczb statystycznie nierozróżnialnych**.
Przedstawiałem je jako poprawę albo pogorszenie. To było nadinterpretowanie.

Jedno stwierdzenie z tamtej serii się broni, ale słabiej, niż je podałem:
wynik spadał po **każdej** z czterech niezależnych poprawek poprawności. Każdy
pojedynczy spadek mieści się w szumie; konsekwentny kierunek czterech
niezależnych zmian jest słabą przesłanką, nie dowodem. Powinienem był tak to
napisać od razu.

Nie unieważnia to wniosku ilościowego z poprzedniej sekcji — ten opiera się na
pomiarze IC i kosztów, nie na odsetku okien, i pozostaje w mocy: żaden sygnał
nie pokrywa własnych kosztów.

## Ile okien trzeba

Żeby odróżnić 70% od 50% z pewnością 95%, przy wyniku 70% wystarczy n = 40
(dolna granica wypada na 54,6%). Próg jest więc osiągalny — ale **tylko wtedy,
gdy wynik naprawdę wyjdzie w okolicach 70%**. Przy wynikach 50–62% przedział
zawiera i 50%, i 70%, więc nie rozstrzyga niczego.

Zawężenie przedziału wymaga więcej okien, a tych jest tyle, ile pozwala
historia: 4 lata danych to twardy limit 40 okien niezachodzących na siebie.

## Wdrożone

Przedział ufności jest liczony przy każdym treningu, zapisywany
(migracja 0017) i pokazywany **wszędzie tam, gdzie pokazywany jest odsetek** —
w komunikacie trenera, w API i w zakładce „Stopień nauki", razem z jawnym
ostrzeżeniem, gdy przedział jest szerszy niż 20 punktów.

Sam odsetek, podany bez przedziału, zachęca do czytania różnicy dwóch punktów
jako sygnału.

---

# Losowe przeszukiwanie było gorsze od nierobienia go

Poprzednia sekcja kończy się zdaniem „nie będę dalej dobierał wag". Zostało
w mocy — ale okazało się, że problemem nie był *sposób* ważenia, tylko sam fakt,
że wagi są **wybierane**.

## Co pokazał licznik, którego nie czytałem

Trener wypisywał przy każdym przebiegu jedną linijkę, którą przez tydzień
traktowałem jak ciekawostkę:

```
Wagi z IC wybrane w 2/40 okien (reszta: losowanie albo założenia początkowe)
```

Wagi wyliczone ze współczynnika informacji — ten sam kandydat, który został
dodany jako „koniec szukania po omacku" — przegrywały na oknie treningowym
w **38 z 40 okien**. Przegrywały z najlepszym z sześćdziesięciu losowań.

To nie jest dowód, że losowania są lepsze. To definicja klątwy zwycięzcy:
maksimum z 60 próbek jest zawyżone o tyle, o ile próbek się wzięło, i całe
to zawyżenie znika poza oknem treningowym. Trener wybierał kandydata po
wielkości, na którą składał się głównie szum.

## Pomiar

Do trenera doszedł przełącznik `--wybor`, który zmienia wyłącznie to, **jak
powstaje kandydat**, a nie sposób jego oceny. Okna, progi wejścia, bramki,
koszty, filtr martwych sygnałów i ocena out-of-sample są identyczne we
wszystkich wariantach:

| wariant | kandydat na okno |
|---|---|
| `argmax` | najlepszy z 60 losowań, założeń początkowych i wag z IC — produkcja |
| `ic` | wyłącznie wagi wyliczone ze współczynnika informacji |
| `bazowe` | wyłącznie założenia początkowe |

Trzy przebiegi, świeca 4 h, 1400 dni, te same dane, tego samego dnia
(`--sucho`, więc żaden nie dotknął stanu nauki):

| wariant | okna | dodatnie | przedział 95% | mediana Sharpe | obsunięcie | pokonał kup-i-trzymaj |
|---|---|---|---|---|---|---|
| `bazowe` | 40 | 20 (50,0%) | 35,2%–64,8% | +0,008 | −0,89% | 21/40 (52,5%) |
| **`ic`** | **40** | **30 (75,0%)** | **59,8%–85,8%** | **+0,117** | −2,49% | 21/40 (52,5%) |
| `argmax` | 26 | 15 (57,7%) | 38,9%–74,5% | +0,066 | −1,60% | 14/26 (53,8%) |

## Drugi efekt: znikające okna

Warianty bez losowania dały **40 okien informatywnych, produkcja 26**. Różnica
nie jest kosmetyczna — to jedna trzecia dowodów.

Mechanizm jest konkretny i wynika wprost z tego, co maksymalizujemy. Miarą jest
Sharpe **na transakcję**. Układ wag, który prawie nie handluje, płaci mniej
prowizji i ma mniejszy mianownik, więc na oknie treningowym wypada świetnie
przy garstce transakcji. Losowanie systematycznie takie układy znajduje —
a poza oknem ta garstka to już tylko szum i okno wypada poniżej progu
12 transakcji, czyli wypada z oceny w ogóle.

Losowe przeszukiwanie nie tylko wybierało gorzej. **Wybierało tak, żeby wyniku
nie dało się zmierzyć.**

## Czego ten wynik NIE mówi

Przedział przy `ic` (59,8%–85,8%) leży powyżej rzutu monetą, ale kolumna
„pokonał kup-i-trzymaj" ani drgnęła: **21/40, czyli 52,5%, przedział
37,5%–67,1%**. Strategia jest częściej dodatnia, bo trzyma małą ekspozycję na
rynku, który przez większość okresu spadał — to nie to samo co przewaga.
Wniosek ilościowy z poprzedniej sekcji, że żaden pojedynczy sygnał nie pokrywa
własnych kosztów, pozostaje w mocy; zmieniło się to, że przestaliśmy do tych
sygnałów dokładać szum z losowania.

## Uboczna korzyść: licznik hipotez przestaje puchnąć

`argmax` dokładał 61 konfiguracji na okno, czyli ~2440 na przebieg, każdej doby.
Licznik doszedł do 11 460 i każdy przyszły wynik trzeba było czytać razem z nim.
`ic` dokłada jedną na okno — 40 na przebieg, sześćdziesiąt razy mniej.

## Test odporności: cztery przesunięcia okien

Poprzednia sekcja tego dokumentu obaliła serię porównań, bo różnice mieściły
się w szumie samego przesunięcia okna. Ten sam test zastosowany tutaj — cztery
przebiegi różniące się wyłącznie długością historii, czyli przesuwające
wszystkie granice okien:

| przesunięcie | `ic` — dodatnie | `argmax` — dodatnie | `ic` mediana Sharpe | `argmax` mediana Sharpe |
|---|---|---|---|---|
| 1400 dni | **30/40 (75,0%)** | 15/26 (57,7%) | +0,117 | +0,066 |
| 1397 dni | **28/40 (70,0%)** | 16/26 (61,5%) | +0,106 | +0,095 |
| 1394 dni | **27/40 (67,5%)** | 16/25 (64,0%) | +0,149 | +0,079 |
| 1391 dni | **29/40 (72,5%)** | 18/27 (66,7%) | +0,118 | **+0,163** |

Co z tego wynika i czego nie:

- **Liczba okien informatywnych: 40 wobec 25–27 w każdym z czterech przebiegów.**
  To jest najmocniejsza część wyniku, bo nie jest różnicą wielkości losowej —
  jest różnicą tego, ile dowodów w ogóle powstaje.
- **Odsetek dodatnich okien wyższy przy `ic` we wszystkich czterech przebiegach.**
  Rozrzut `ic` to 67,5–75,0%, `argmax` 57,7–66,7%. Przedziały ufności nadal się
  częściowo pokrywają, więc pojedyncze porównanie nic by nie znaczyło; znaczy
  konsekwencja kierunku przy czterech niezależnych przesunięciach.
- **Mediana Sharpe nie jest jednoznaczna: przy 1391 dniach `argmax` wypadł
  wyżej (+0,163 wobec +0,118).** Trzy na cztery to nie jest wynik, na którym
  można się oprzeć — i nie opieram się na nim.
- **Żaden wariant nie pokonuje kup-i-trzymaj.** `ic`: 52,5%, 50,0%, 52,5%,
  47,5%. `argmax`: 53,8%, 53,8%, 56,0%, 51,9%. Oba są rzutem monetą wobec
  odniesienia i to jest najważniejsze zdanie w całej tej sekcji.

## Wniosek

Zmiana jest uzasadniona jako **usunięcie szkodliwego kroku**, nie jako
znalezienie przewagi. Losowe przeszukiwanie kosztowało jedną trzecią okien
i sześćdziesiąt razy powiększało licznik hipotez, nie dając w zamian nic
mierzalnego. Strategia po tej zmianie nadal nie ma przewagi nad kupieniem
i trzymaniem — ma tylko o jedno źródło szumu mniej.

## Zastrzeżenie do statusu „validated"

Przy wariancie `ic` odsetek dodatnich okien przekracza próg 70% i trener
wystawia status **`validated` — „Kryteria spełnione"**. Czytanie tego jako
zielonego światła do handlu byłoby błędem, przed którym cały ten dokument
ostrzega: kryterium mierzy, jak często okno wychodzi na plus, a nie czy
strategia bije nierobienie niczego. Kolumna „pokonał kup-i-trzymaj" siedzi
na 50%.

Kryterium wymaga uzupełnienia o warunek przewagi nad odniesieniem, zanim
`validated` zacznie cokolwiek znaczyć. Do tego czasu tryb live pozostaje
zablokowany w API (`credentials.py:79`) niezależnie od statusu.

---

# Sprostowanie: miara, na której stoi cały ten dokument, była liczona przed prowizją

Powyższe sekcje porównują warianty doboru wag liczbami, które **nie zawierały
prowizji**. Wykrył to przypadek: dwa przebiegi różniące się wyłącznie stawką
taker — 0,100% wobec 0,250% — dały **identyczny odsetek dodatnich okien
i identyczną medianę Sharpe**, co przy dwukrotnej różnicy kosztu jest niemożliwe.

## Gdzie siedział błąd

`simulate_portfolio` liczyła prowizję poprawnie i odejmowała ją od **krzywej
kapitału**. Ale zwrot z pojedynczej transakcji zapisywany do `res.trades` był
liczony wyłącznie z cen:

```python
"ret": pos.side * (exit_px / pos.entry - 1)     # poślizg tak, prowizja NIE
```

A `metrics()` liczy Sharpe właśnie z tych zwrotów — i to Sharpe decyduje, czy
okno jest dodatnie, czyli o odsetku dodatnich okien, medianie i statusie modelu.
Prowizja wpływała więc wyłącznie na `max_dd` i `total_return`, czyli na dwie
liczby, których werdykt nie używa.

Kanarek jest prosty: na **płaskiej cenie** transakcja wychodziła na −0,080%,
czyli dokładnie dwa poślizgi po 0,04%. Ani jednej nogi prowizji.

Cały zestaw 121 testów przechodził. Żaden nie sprawdzał, czy koszt dociera do
miary — doszły dwa, które to pilnują, i oba padają na kodzie sprzed poprawki.

## Co się zmienia w liczbach

Ten sam wariant `ic`, te same okna, świeca 4 h, 1400 dni:

| | odsetek dodatnich | mediana Sharpe |
|---|---|---|
| przed poprawką (bez prowizji) | 30/40 = **75,0%** | +0,117 |
| po poprawce (z prowizją) | 26/40 = **65,0%** | +0,067 |

Dziesięć punktów procentowych i mediana zawyżona o 74%. Wniosek jakościowy
z porównania wariantów zostaje — `ic` nadal bije `argmax` i nadal daje 40 okien
zamiast 26 — ale **każda bezwzględna liczba z sekcji powyżej jest zawyżona**
i trzeba ją czytać przez tę tabelę.

## Trzy scenariusze kosztowe na giełdzie, na której faktycznie staniemy

Prowizje odczytane z konta przez `/v5/account/fee-rate` na `api-demo.bybit.eu`
(2026-09-04, jednakowe dla wszystkich 140 par): **maker 0,100%, taker 0,250%**.
Taker jest dwuipółkrotnie droższy niż na podmiocie globalnym.

| scenariusz | koszt rundy | dodatnie | przedział 95% | mediana Sharpe |
|---|---|---|---|---|
| spot globalny, zlecenia rynkowe | 0,280% | 26/40 (65,0%) | 49,5–77,9% | +0,067 |
| **spot unijny, zlecenia rynkowe** | **0,580%** | **17/40 (42,5%)** | 28,5–57,8% | **−0,011** |
| spot unijny, zlecenia z limitem | 0,200% | 26/40 (65,0%) | 49,5–77,9% | +0,098 |

Wniosek jest jednoznaczny i wykonalny: **na podmiocie unijnym zlecenia rynkowe
są wykluczone.** Przy 0,580% mediana Sharpe schodzi poniżej zera — strategia
przestaje istnieć. Stawka maker jest natomiast identyczna jak globalnie (0,100%),
więc egzekucja z limitem daje ten sam wynik co dotychczasowe założenie, a nawet
nieco wyższą medianę.

## Zastrzeżenie do scenariusza z limitem

Symulacja sprawdza wypełnienie limitu przez porównanie z zasięgiem następnej
świecy — świecy z **podmiotu globalnego**. Księga unijna jest dla BTCUSDC około
sześciokrotnie płytsza, a dla mniejszych par nieporównywalnie bardziej. Realny
odsetek wypełnień będzie tam niższy, a niewypełnione wyjście w spadającym rynku
kosztuje najwięcej. Liczba 65,0% z ostatniego wiersza jest więc **górnym
ograniczeniem**, nie prognozą.

Żaden z trzech scenariuszy nie pokonuje kup-i-trzymaj: **21/40, czyli 52,5%,
przedział 37,5–67,1%** — identycznie we wszystkich trzech.

---

# Perpetuały nie pomagają — a `koszty.py` twierdził, że są konieczne

Dokument `koszty.py` zawiera zdanie przyjmowane od tygodnia za pewnik:

> Strategia dwustronna wymaga więc kontraktów wieczystych. Tam krótka pozycja
> jest naturalna, a prowizja maker pięć razy niższa niż na spocie.

Rozumowanie było takie: na spocie nie da się grać na spadek bez pożyczki
marżowej, a 8 z 12 pozycji w dzienniku produkcyjnym to były shorty — czyli
transakcje niewykonalne. Perpetuały rozwiązują jedno i drugie: pozwalają
shortować i kosztują 0,040% rundy zamiast 0,200%.

Ponieważ podmiot unijny nie oferuje perpetuałów, wniosek brzmiał: albo strategia
zostaje kaleka, albo krypto przenosi się gdzie indziej. **To założenie nie
przeszło pomiaru.**

## Pomiar

Uniwersum zawężone do 10 par, które Bybit EU faktycznie oferuje. Cztery
przesunięcia okien, obie egzekucje z limitem, ta sama historia cen:

| przesunięcie | spot długi | perp z shortami | spot mediana | perp mediana |
|---|---|---|---|---|
| 1400 dni | **28/39 (71,8%)** | 26/40 (65,0%) | **+0,094** | +0,061 |
| 1397 dni | **26/39 (66,7%)** | 26/40 (65,0%) | **+0,153** | +0,032 |
| 1394 dni | **27/40 (67,5%)** | 25/40 (62,5%) | **+0,111** | +0,049 |
| 1391 dni | 28/40 (70,0%) | **30/40 (75,0%)** | +0,056 | **+0,068** |

Kierunek sprzyja spotowi w **3 z 4** przesunięć na obu miarach. Przedziały
ufności zachodzą na siebie prawie całkowicie, więc uczciwe zdanie brzmi:
**zakładana przewaga perpetuałów nie pojawia się w pomiarze** — a nie „spot
jest lepszy".

## Dlaczego to jest mocniejsze, niż wygląda

Wariant perp jest w tej symulacji **zawyżony na własną korzyść**: perpetuały
pobierają finansowanie co osiem godzin, a `simulate_portfolio` nalicza wyłącznie
prowizję (`fees_paid`). Finansowanie jest w projekcie mierzone jako *cecha*,
nie jako *koszt pozycji*.

Perp dostaje więc pięciokrotnie tańszą prowizję, prawo do shortowania i zerowy
koszt finansowania — i mimo tego przegrywa w trzech przesunięciach na cztery.
Oznacza to, że **krótka noga sygnału traci więcej, niż wynosi oszczędność na
prowizji**. Prawdziwy perp, płacący finansowanie, wypadłby jeszcze gorzej.

To luka w modelu, którą trzeba domknąć, zanim ktokolwiek wróci do tematu
perpetuałów — stawki finansowania są w bazie (39 tys. wierszy w `market`),
więc naliczanie ich jest wykonalne, a nie hipotetyczne.

## Zawężenie uniwersum do tego, co da się kupić

Przy okazji zmierzone: kalibracja na 24 instrumentach, z których 14 nie istnieje
na giełdzie, gdzie stoi konto, wobec kalibracji na 10 dostępnych.

| uniwersum | okna | dodatnie | mediana Sharpe |
|---|---|---|---|
| 24 pary (dotychczas) | 40 | 25 (62,5%) | +0,090 |
| **10 par dostępnych na EU** | 39 | **28 (71,8%)** | +0,094 |

Zawężenie nie szkodzi — pomaga, bo odpadają instrumenty najcieńsze. Trening idzie
więc od teraz po tym, co realnie da się kupić; kalibracja na instrumentach
niedostępnych mierzyła strategię niewykonalną, czyli dokładnie ten sam rodzaj
rozjazdu trening–produkcja, który ten dokument opisuje już trzeci raz.

## Co z tego wynika dla planu

**Pozostanie na Bybit EU ze spotem długim nie jest gałęzią kaleką.** Główny
argument za przeniesieniem krypta gdzie indziej — „tylko perpetuały dają tej
strategii szansę" — nie ma poparcia w pomiarze.

Nie zmienia się natomiast to, co blokuje naprawdę: **żaden wariant nie pokonuje
kup-i-trzymaj.** Spot 48,7 / 43,6 / 47,5 / 47,5%, perp 50,0 / 42,5 / 47,5 /
47,5%. Cztery przesunięcia, dwie egzekucje, osiem liczb, wszystkie poniżej 50%.


---

# Uniwersum przechodzi na pary USDC

Egzekucja pójdzie parami USDC, bo tam jest cała płynność podmiotu unijnego.
Kalibracja szła po parach USDT. Pytanie brzmiało, czy to formalność.

## Nie jest formalnością

Korelacja zwrotów 4 h między parą USDT i USDC tego samego aktywa wynosi
0,973–0,9997, więc szeregi wyglądają na ten sam. Zmierzona **mediana bazy**
mówi co innego:

| BTC | ETH | SOL | XRP | ADA | DOGE | SUI | ARB | LINK | NEAR |
|---|---|---|---|---|---|---|---|---|---|
| 0,053% | 0,054% | 0,058% | 0,053% | 0,061% | 0,064% | 0,065% | 0,078% | 0,096% | 0,163% |

Runda kosztowa z limitem to 0,200%. Baza jest więc rzędu **jednej czwartej
całego budżetu kosztowego** — tego samego rzędu co przewaga, której szukamy.
Trenowanie na jednym szeregu i handlowanie drugim wstrzykuje szum wielkości
mierzonego efektu.

## Pomiar

Zaciągnięte 351 tys. świec godzinowych i policzone ponad milion wartości cech
dla 10 par USDC. Te same aktywa, te same okna, ten sam cennik i egzekucja:

| przesunięcie | USDT | USDC | USDT mediana | USDC mediana |
|---|---|---|---|---|
| 1400 dni | 28/39 (71,8%) | **30/39 (76,9%)** | +0,094 | **+0,160** |
| 1397 dni | 26/39 (66,7%) | **30/39 (76,9%)** | **+0,153** | +0,111 |
| 1394 dni | 27/40 (67,5%) | **28/40 (70,0%)** | +0,111 | **+0,131** |
| 1391 dni | 28/40 (70,0%) | **30/40 (75,0%)** | +0,056 | **+0,129** |

Odsetek wyżej w 4 przesunięciach na 4, mediana w 3 na 4. Przedziały ufności
zachodzą na siebie, więc uczciwy wniosek brzmi: **uniwersum USDC nie jest
gorsze** — a jest tym, którym da się handlować. Sama wykonalność wystarczyłaby
za uzasadnienie; pomiar był po to, żeby nie zapłacić za nią wynikiem.

## Dwie rzeczy, które trzeba było naprawić po drodze

**Siła względna liczyła BTC dwa razy.** Po zarejestrowaniu par USDC ten sam
aktyw występował w bazie dwukrotnie, a ranking przekrojowy szedł po wszystkim,
co ma cechę `ret_mid`. Dziesięć z dwudziestu czterech aktywów liczyło się
podwójnie — także dla par USDT, czyli błąd dotykał wyniku, który już mieliśmy.
Przekrojowość liczy się teraz wyłącznie w obrębie uniwersum.

**Finansowanie byłoby po cichu urwane.** `funding_carry` to jeden z ośmiu
sygnałów i wymaga podpięcia pod perpetuał. Pary USDC go nie miały, więc trener
wykluczyłby cały sygnał jako martwy — utrata jednego z ośmiu sygnałów przemycona
jako zmiana waluty kwotowanej. Finansowanie jest własnością aktywa, nie waluty
kwotowanej, więc pary USDC dziedziczą je po perpetuale USDT tego samego aktywa.

Przy okazji wyszło, że proces `funding` **wywracał się od momentu rejestracji
par USDC**: wskazywał perpetuał doklejając `.P` do symbolu spotowego, więc pytał
o nieistniejący `BTCUSDC.P`, a wyjątek szedł w górę pętli i zatrzymywał
pobieranie stawek dla wszystkich pozostałych rynków. Widać to było wyłącznie
w logu, bo wywołanie jest osłonięte przez `|| true`.

## Czego to nie zmienia

Kolumna „pokonał kup-i-trzymaj": 48,7% / 43,6% / 50,0% / 47,5%. Bez zmian.
