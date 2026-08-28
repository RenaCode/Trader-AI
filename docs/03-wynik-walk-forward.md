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
