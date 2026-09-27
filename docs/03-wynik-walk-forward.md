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

### Drugie uzupełnienie: deflowany Sharpe wszedł do bramki (2026-09-26)

Warunek `pct >= 70 and median > 0,05 and bije_odniesienie` nie uwzględniał ani
**liczby przetestowanych konfiguracji**, ani rozkładu zwrotów, ani kosztów, których
symulacja nie nalicza. Teraz DSR jest warunkiem bramki, **fail-closed**:
nieobliczalny DSR wstrzymuje promocję.

#### Co jest N — i dlaczego NIE `learn.hypothesis_trial`

Licznik `hypothesis_trial` istniał od migracji 0006 właśnie po to, żeby DSR dało się
policzyć, i **nie jest właściwym N**. Nie dlatego, że rośnie kalendarzem — to jest
argument za słaby: 143 przebiegi po najwyżej 40 okien dają sufit 5 720 wierszy
z wariantu `ic`, a licznik toru 4h ma 12 613, więc co najmniej 6 893 wiersze
pochodzą z realnego przeszukiwania `argmax`.

Właściwy powód jest mocniejszy: **kandydaci `argmax` byli oceniani in-sample**
(`ocen(..., train_slices, ...)`), a raportowany Sharpe pochodzi z okna testowego,
którego selekcja nigdy nie widziała. Walk-forward już za nich zapłacił — tym, że
wynik liczy się poza oknem wyboru. Doliczenie ich do poprzeczki deflacji odejmowałoby
klątwę zwycięzcy, którą ten protokół właśnie usuwa: **ta sama kara dwa razy**.

N to liczba **rozróżnialnych konfiguracji, których wynik OOS ktoś zobaczył**:
`count(DISTINCT params)` po `learn.model_version`. Zmierzone 2026-09-26:

```
crypto_signal_weighting_4h:  143 wersje | 57 roznych params | 2026-08-27..2026-09-26
calosc projektu:             180 wersji | 86 roznych params | 5 modeli
hypothesis_trial (tor 4h):   12 613     | oos_sharpe wypelnione: 0
```

##### „Których wynik ktoś zobaczył" jest tu WARUNKIEM, nie ozdobą (2026-09-27)

Do 2026-09-27 zapytanie liczyło **każdy** wiersz `learn.model_version`, także wersje,
które zostały w statusie `training` — sieroty po przebiegach zabitych i po `--sucho`.
Taki przebieg nigdy nie doszedł do wyniku, więc selekcja nie mogła się na nim oprzeć:
nikt go nie zobaczył i nikt na jego podstawie niczego nie wybrał. Rozbicie 59 różnych
`params` toru 4h, zmierzone tego dnia:

```
28  ma zapisany median OOS Sharpe                      -> proba
24  skonczone (active/retired), bez wiersza w dzienniku -> proba (`--sucho`)
 7  wylacznie `training`, nigdy nie doszly do wyniku     -> NIE proba
```

Siedem `params` z `training` nie powtarza się w żadnym innym statusie, więc filtr
naprawdę je usuwa. N toru 4h spada z **59 na 52** (toru 1h: 27 na 26), poprzeczka
selekcji z **+0,0622 na +0,0609**, a DSR rośnie z **0,7064 na 0,7232**. Kierunek błędu
był więc **na niekorzyść strategii** — nie przepuszczał niczego, co nie powinno przejść,
ale podawał człowiekowi jako pomiar liczbę, która pomiarem nie była. Werdykt się nie
zmienia: 0,7232 wobec progu 0,95.

**Wersja bieżącego przebiegu wchodzi do N osobno.** `train()` zakłada swój wiersz
(status domyślny `training`) **zanim** dojdzie do bramki, więc sam filtr statusu gubiłby
dokładnie tę próbę, która jest przedmiotem selekcji — a pierwszy przebieg nowego toru
dostawałby N = 0, czyli DSR **nieobliczalny** i promocję wstrzymaną przez filtr, a nie
przez wielokrotność prób (`crypto_signal_weighting_5m` i `equity_signal_weighting_1h`
stoją dziś dokładnie w tym stanie: po jednej wersji, obie `training`).

**Rozrzut pod `V[{SR_n}]` pochodzi z WĘŻSZEJ populacji niż N** — z 28 konfiguracji
z zapisanym Sharpe'em OOS, nie z 52. Przyjęcie sd tych 28 za sd całej populacji N jest
**założeniem, nie pomiarem**, i od tej samej daty stoi jawnie w `reason` stanu nauki.

#### `V[{SR_n}]` — od 2026-09-27 MIERZONE, a nie podstawiane

Do tej daty `deflowany_sharpe` podstawiał w miejsce rozrzutu Sharpe'ów **między
próbami** wariancję samego estymatora pod H₀: `sd = σ/√(T−1)`. Przy T = 1462 to
**0,0264**. Założenie mówi „konfiguracje różnią się wyłącznie błędem pomiaru",
a różnią się więcej — więc poprzeczka selekcji była zaniżona, a DSR zawyżony.

`learn.hypothesis_trial.oos_sharpe` istniał od migracji 0006 i był pusty
w **16 050 wierszach z 16 050** (zmierzone 2026-09-27; miesiąc wcześniej 0 z 12 613).
Teraz `train.py` zapisuje tam Sharpe OOS kandydata **wybranego w każdym oknie** —
jedynego, dla którego symulacja poza oknem wyboru w ogóle się liczy. Odrzuceni
kandydaci (60 losowań + założenia + IC na okno) **nie mają wyniku OOS i nigdy go
nie mieli**: `ocen(cand, train_slices, cache_tr)` liczy ich in-sample. Policzenie im
OOS to inny protokół, nie inny zapis.

Skala jest znana z góry, bo ta sama wielkość leży już w `learn.wf_metric`:
rozrzut Sharpe'ów OOS okno po oknie to **sd = 0,2627** (2 353 okna toru 4h;
tor 1h 0,2589, model bez horyzontu 0,2600). Dziesięć razy więcej niż podstawienie.

| Sharpe | poprzeczka z H₀ (0,0264) | DSR | poprzeczka ze zmierzonego (0,2627) | DSR |
| --- | --- | --- | --- | --- |
| +0,0952 (bez spreadu) | +0,0614 | 0,9001 | +0,6111 | **0,0000** |
| +0,0823 (spread 5,85 pb) | +0,0613 | 0,7874 | +0,6111 | **0,0000** |
| +0,0762 (spread 8,618 pb, pomiar) | +0,0612 | 0,7149 | +0,6111 | **0,0000** |

Kierunek jest więc taki, jaki musi być: **poprzeczka w górę, DSR w dół.** Odwrotny
wynik byłby objawem błędu, nie dobrą wiadomością.

**Dwa zastrzeżenia, oba nazwane w kodzie** (`train.odchylenie_prob_z_bazy`):
rozrzut mierzymy na ZWYCIĘZCACH okien, czyli w próbie wybranej po górnym ogonie —
to go ZANIŻA, więc nadal działa na korzyść strategii. Jednocześnie kolejne okna to
inny rynek, więc w rozrzut wchodzi zmienność okien, nie tylko konfiguracji — a to
go PODNOSI. Który składnik przeważa, nie jest zmierzone.

**Próg próby jest jawny.** Poniżej `MIN_PROB_ROZRZUTU = 20` konfiguracji z policzonym
OOS zostaje dzisiejsze podstawienie z H₀, a opis werdyktu mówi wprost, którą drogą
poszedł. Odchylenie z trzech liczb wchodzi do poprzeczki liniowo i skakałoby o
kilkadziesiąt procent między przebiegami różniącymi się jednym oknem — „pomiar",
który tak się zachowuje, przenosi lukę w miejsce, w którym nikt jej nie szuka.

**`n_windows` był wpisywany jako 1 i nigdy nie aktualizowany.** Przy `oos_sharpe`
równym NULL dwie kolumny tego samego wiersza mówiły co innego. Nowe wiersze dostają
0, historyczne poprawia migracja 0061, a znaczenie obu kolumn stoi teraz w
`COMMENT ON COLUMN`, nie tylko w komentarzu w kodzie.

**57 jest DOLNĄ GRANICĄ.** Do 26.09 `params` nie zapisywało `kind` ani `limit`, więc
dwa przebiegi różniące się wyłącznie cennikiem (`spot` taker 0,100% wobec `spot_eu`
0,250%) zlewały się w jedną konfigurację. Od teraz zapisuje; historycznych wierszy to
nie naprawia. Zaniżone N podnosi DSR, więc błąd tej liczby działa **na korzyść
strategii**.

#### Wynik przy zmierzonym N

T = 1462, γ₃ = 0, γ₄ = 3:

| N | poprzeczka selekcji | DSR przy SR +0,0952 | DSR po spreadzie 5,85 pb (SR +0,0823) |
|---|---|---|---|
| 17 | +0,0478 | 0,9646 przechodzi | **0,9058 nie** |
| 22 | +0,0508 | 0,9547 przechodzi | **0,8852 nie** |
| **57** (zmierzone) | +0,0609 | **0,9049 nie** | **0,7934 nie** |
| 86 | +0,0648 | 0,8768 nie | 0,7478 nie |
| 12 613 | +0,1025 | 0,3907 nie | 0,2207 nie |

Tabela policzona `services/engine/statystyka.py`. Druga, niezależna implementacja
(napisana od zera) daje te same liczby z rozbieżnością poniżej 0,004 na całym
zakresie N — czyli zgodność, która nie zmienia ani jednego werdyktu.

Dwa wnioski:

1. **Przy zmierzonym N werdykt nie jest na granicy** — 0,9040, i spada dalej, bo 57 to
   dolna granica. Nie trzeba więc rozstrzygać sporu o księgowanie hipotez, żeby
   odpowiedzieć na pytanie „czy przewaga przeżywa poprawkę na selekcję". Nie przeżywa.
2. **Po doliczeniu zmierzonego spreadu DSR nie przechodzi przy ŻADNYM N, także przy
   najłagodniejszym.** Rozstrzyga więc **koszt**, nie selekcja — i to zdejmuje z bramki
   ciężar bycia precyzyjną w N.

#### DSR liczy się PO koszcie, z tej samej symulacji, z której bierze się koszt

To był najważniejszy element tej zmiany. `Karta.doplata_poza_symulacja` sumuje
wyłącznie te składniki, które **mają wartość, a symulacja ich nie naliczyła** — dziś
spread (gdy podano pomiar) i niewypełnienie przy limicie. Odjęcie całego `policzone`
liczyłoby prowizję dwa razy; odjęcie zera twierdziłoby, że kosztu poza symulacją nie
ma. Rozróżnienie stoi w polu `Skladnik.w_symulacji`, a nie w dopasowywaniu nazw
tekstem — przemianowanie składnika zmieniałoby inaczej werdykt bramki bez żadnego
objawu.

#### Trzy zastrzeżenia, bez których ta liczba byłaby ładniejsza niż jest

1. `V[{SR_n}]` z pracy Baileya i Lópeza de Prado — rozrzut Sharpe'ów **między
   próbami** — nie jest mierzalny: `learn.hypothesis_trial.oos_sharpe` jest wypełnione
   w **0 z 12 613** wierszy. Wstawiamy wariancję samego estymatora pod hipotezą zerową,
   a to **zawyża DSR**.
2. Transakcje krypto nie są niezależne, więc efektywne `T` jest mniejsze niż licznik
   transakcji — co również zawyża DSR.
3. N = 57 jest dolną granicą (patrz wyżej) — co znów zawyża DSR.

Wszystkie trzy błędy idą w tę samą stronę i wszystkie na korzyść strategii. Liczby
w tabeli są więc **górnym** oszacowaniem, a nie pomiarem zamkniętym.

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

## Werdykt promocji jest rzutem monetą o położenie okna (2026-09-27)

`--days` liczy się od `now()`, więc granice okien przesuwają się z zegarem. Pięć
przebiegów toru 4h różniących się **tylko** przesunięciem o jeden dzień
(`--sucho`, poza tym identyczne nastawy):

| `--days` | status | DSR | Sharpe/transakcję | okien dodatnich |
|---|---|---|---|---|
| 1400 | **validated** | 0,9524 | +0,1016 | 27/38 (71%) |
| 1399 | **validated** | 0,9732 | +0,1078 | 28/38 (74%) |
| 1398 | learning | 0,9342 | +0,0975 | 31/40 (78%) |
| 1397 | learning | 0,9362 | +0,0978 | 28/39 (72%) |
| 1396 | **validated** | 0,9790 | +0,1095 | 29/39 (74%) |

Trzy razy `validated`, dwa razy `learning`. **Poprzeczka selekcji jest we
wszystkich pięciu identyczna (+0,0611)**, więc nie chodzi o liczbę prób — cała
zmiana siedzi w Sharpe'ie, który chodzi w przedziale 0,0120, czyli sd 0,0056.
To **0,22 błędu standardowego** samego Sharpe'a (1/√1500 = 0,0258).

Dwie rzeczy są tu warte osobnego zdania:

1. **Bramka rozstrzyga różnice pięć razy drobniejsze od szumu własnego
   pomiaru**, a jej wyjście jest binarne i czyta się jak ustalenie o strategii.
2. **Kryteria przeczą sobie w kierunku:** przesunięcie 1398 ma najlepszy odsetek
   okien z całej piątki (78%) i mimo to nie przechodzi, bo DSR wypada 0,9342.

Nie jest to wada DSR — DSR poprawnie mówi, że przewaga jest nieodróżnialna od
progu. Wadą było podawanie werdyktu bez tej informacji. Od 2026-09-27 `reason`
zawiera zdanie **ODPORNOŚĆ WERDYKTU**: ile Sharpe'a brakuje (albo zostaje) do
progu, wyrażone w błędach standardowych, a poniżej jednego błędu — wprost, że
werdykt nie jest odporny, wraz z tą tabelą. Liczbę oblicza
`statystyka.sharpe_dla_dsr`, przez bisekcję po przesunięciu próby: przesunięcie
zmienia **tylko** średnią, bo odchylenie, skośność i kurtoza są momentami
centralnymi, więc nie ma tu drugiej kopii wzoru Baileya.

Błąd 1/√T jest **zaniżony** i tak trzeba go czytać: to błąd Sharpe'a przy
zwrotach niezależnych, a te nie są niezależne. Prawdziwy błąd jest większy,
czyli werdykt jest **mniej** odporny, niż mówi ta liczba.

## Luka DOMKNIĘTA, pytanie ROZSTRZYGNIĘTE (2026-09-27)

Finansowanie jest naliczane od 2026-09-24 (`koszty.py`, w zwrocie z transakcji,
nie tylko w krzywej kapitału). `services/engine/wartosc_krotkiej.py` puszczony
tego dnia na torze 4h, 1400 dni, model 144 (próg wspólny 0,2796 — produkcja nie
przetrenowała się jeszcze po zmianie progów per reżim):

| | zwrot z okna | Sharpe | transakcji | pominiętych shortów |
|---|---|---|---|---|
| **spot_eu** (produkcja) | **+10,28%** | **+0,1036** | 1794 | 3141 |
| perp (krótkie dozwolone) | +1,85% | +0,0198 | 1433 | 0 |

Odblokowanie krótkiej strony to więc **−8,43 pp zwrotu i −0,0838 Sharpe'a**.
Sama noga krótka: n=484, średni zwrot **−0,6036%**, przedział 95%
**[−1,3537%, +0,1073%]** — obejmuje zero, czyli przewagi **nie ma wykazanej**,
a oszacowanie punktowe jest ujemne. Trafność 57,6% przy ujemnej średniej, czyli
wiele małych wygranych i kilka dużych przegranych. Finansowanie −363,16 na nodze
krótkiej wobec +327,92 na długiej. Wynik jest **niewrażliwy na założenie
o poślizgu** w całym zakresie 0–10 pb (Δ zwrotu stoi na −8,43 pp przy każdym).

### Dwa efekty, nie jeden — i to jest najważniejsze zastrzeżenie

Różnica −8,43 pp **nie jest stratą nogi krótkiej**. Noga długa spada z 1794
transakcji na 949, a licznik „zablokowane przez ryzyko" idzie z **0 na 4963**:
krótkie zjadają ten sam budżet ekspozycji (`max_total_exposure` 15%), więc
wypychają wejścia długie. Większość ubytku to **wejścia długie, których nie
było**, a nie pieniądze przegrane na shortach.

Wniosek jest przez to mocniejszy, nie słabszy: przy dzisiejszym limicie
ekspozycji krótka strona nie dokłada przewagi i odbiera miejsce tej, która ją ma.

### Skutek dla 76% odrzucanych sygnałów

Produkcja, cała historia od 2026-08-27 (1869 decyzji): 37 wejść długich, 8
krótkich (sprzed bramki z migracji 0040) i **143 sygnały krótkie odrzucone** jako
`short_niewykonalny` — czyli 76% z 188 sygnałów ponad progiem. **To nie jest
strata okazji.** Sygnał, którego realizacja nie ma wykazanej wartości, nie jest
okazją, a próba jego przechwycenia kosztuje miejsce pod sygnały długie.
„Odblokujmy shorty" jest odrzucone pomiarem, w obie strony.

Czego te liczby nie zawierają: bazy kontraktu nad spotem, likwidacji, oraz tego,
że stawka finansowania pochodzi z perpetuała *USDT (przybliżenie, migracja 0021).
Uniwersum pozostaje spotowe — wariant perp stosuje cennik perpetuała do szeregu
cen pary spotowej.

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


---

# Dwa horyzonty odpowiadają inaczej — i to jest wynik

Cechy istniały wyłącznie dla świecy 4-godzinnej: 3,4 mln wartości, pełne cztery
lata. Horyzont 1h nie miał ich wcale. Policzone (4,17 mln wartości) i po raz
pierwszy przepuszczone przez walk-forward — na kodzie **po** naprawie prowizji,
bo poprzedni wybór horyzontu zapadał, gdy miara nie zawierała kosztów.

| przesunięcie | dodatnie 4h | dodatnie 1h | mediana 4h | mediana 1h | kup-i-trzymaj 4h | kup-i-trzymaj 1h |
|---|---|---|---|---|---|---|
| 1400 dni | 29/39 (74,4%) | 38/87 (43,7%) | +0,176 | −0,027 | 48,7% | **56,3%** |
| 1397 dni | 30/39 (76,9%) | 42/88 (47,7%) | +0,121 | −0,032 | 41,0% | **51,1%** |
| 1394 dni | 28/40 (70,0%) | 45/88 (51,1%) | +0,125 | +0,012 | 47,5% | **50,0%** |
| 1391 dni | 29/40 (72,5%) | 44/87 (50,6%) | +0,133 | +0,001 | 50,0% | **52,9%** |

## Dwie miary wskazują dwa różne horyzonty

Świeca 4-godzinna wygrywa **odsetkiem dodatnich okien** (70–77% wobec 44–51%)
i **medianą Sharpe** (+0,12…+0,18 wobec −0,03…+0,01), i to bezdyskusyjnie.

Świeca godzinowa wygrywa **pokonaniem kup-i-trzymaj** — w czterech przesunięciach
na cztery, 50,0–56,3% wobec 41,0–50,0%. To jedyna miara, która przez cały dzień
pomiarów nie ruszyła się z okolic 50%, i jedyna, która mówi o przewadze.

## Dlaczego to nie jest sprzeczność

Odsetek dodatnich okien **nagradza nieuczestniczenie**. Strategia na świecy 4h
trzyma małą ekspozycję: rzadko traci, więc ma dużo okien dodatnich — i rzadko
wyprzedza rynek. Strategia na świecy 1h handluje częściej, więc mocniej podąża
za rynkiem: w okresie spadków daje mniej okien dodatnich, ale częściej wypada
lepiej niż trzymanie.

Ten dokument ostrzegał przed tym rozróżnieniem od kilku sekcji. To jest pierwszy
przypadek, w którym obie miary wskazują **przeciwne** rozstrzygnięcia — czyli
pierwszy, w którym ostrzeżenie ma konsekwencję, a nie tylko rację.

## Czego to NIE dowodzi

Przedziały ufności dla pokonania kup-i-trzymaj na świecy 1h — 45,9–66,3%,
40,9–61,3%, 39,8–60,2%, 42,5–63,0% — **wszystkie zawierają 50%**. Przewagi nie
ma. Jest natomiast dwukrotnie więcej dowodów: 87–88 okien zamiast 39–40, czyli
przedział węższy o jedną trzecią. Gdyby przewaga istniała, ten horyzont ma
szansę ją pokazać; świeca 4h przy 40 oknach nie ma.

Mediana Sharpe na świecy 1h jest przy tym zerowa albo ujemna, co jest dokładnie
tym, co przewidywała tabela kosztów w `horizons.py`: przy świecy godzinowej
koszt zjada 45–97% typowego ruchu wobec 22–48% przy czterogodzinnej.

## Wdrożone

Oba horyzonty są trenowane co noc, każdy z własnym modelem, własnym licznikiem
hipotez i własnym stanem nauki (migracja 0025). Silnik handluje wyłącznie
horyzontem domyślnym; 1h jest torem porównawczym i nie podnosi alarmu.

Okna 1h przesuwają się o 14 dni zamiast 30, więc ten tor zyskuje nowe okno
testowe dwa razy częściej — jest w tym sensie „szybszy" i to on pierwszy pokaże
zmianę, jeśli jakaś nadejdzie.


---

# Sprostowanie: „trzykrotnie" dotyczyło innego porównania

Commit naprawiający prowizję w pętli decyzyjnej stwierdza, że krzywa kapitału
mierzyła strategię *tańszą trzykrotnie od wykonalnej*. To zdanie jest prawdziwe
tylko przy jednym z dwóch możliwych porównań i nie powinno stać bez tego
zastrzeżenia.

Policzone na faktycznych wypełnieniach (18 wypełnień, obrót 14 731):

| model kosztu | runda | koszt na tym obrocie |
|---|---|---|
| stała z pętli: 0,055% + poślizg 0,04% | 0,190% | 13,99 |
| przyjęty: spot unijny, zlecenia z limitem | 0,200% | 14,73 |
| gdyby zlecenia **rynkowe** na tej giełdzie | 0,580% | 42,72 |

**Trzykrotność dotyczy trzeciego wiersza wobec pierwszego** — czyli tego, ile
kosztowałaby egzekucja rynkowa, którą pętla symulowała. Wobec modelu, który
faktycznie przyjęliśmy, różnica wynosi **5%**, a błąd zapisany w krzywej to
**0,74** na rachunku stu tysięcy.

Wyszło tak, bo dwa błędy częściowo się zniosły: zaniżona stawka (0,055%
zamiast 0,100%) i doliczony poślizg 0,04%, którego przy zleceniu z limitem
nie ma. Suma wypadła blisko poprawnej — przypadkiem, nie z projektu.

## Co z tego wynika praktycznie

**Krzywej kapitału nie trzeba resetować.** Rozważałem to, bo mieszanie dwóch
modeli kosztu czyni ją nieinterpretowalną — ale przy różnicy 0,74 nie ma czego
mieszać.

Wniosek o samym błędzie zostaje w mocy i jest poważny niezależnie od skali:
produkcja liczyła koszt z własnej stałej, a nie z `koszty.py`, przez tydzień po
tym, jak ten sam błąd naprawiono w symulacji. Gdyby stawki rozjechały się
bardziej — a rozjadą się przy każdej zmianie cennika giełdy — nikt by tego nie
zauważył. Dlatego naprawą jest jedno źródło prawdy plus test, który pada, gdy
wróci własna stała, a nie sama korekta liczby.


---

# Mikrostruktura: pierwszy odczyt i to, czym on nie jest

Cechy z arkusza zleceń — nierównowaga i spread — są liczone od 2026-08-27.
Sygnał na nich oparty **celowo nie istnieje**: kalibrowanie go na dwóch
tygodniach dodałoby stopień swobody i nic nie powiedziało o rynku. Ale
**mierzenie to nie budowanie**: współczynnik informacji niczego nie wybiera,
nie zmienia wag i nie wchodzi do licznika hipotez.

Diagnostyka sygnałów raportuje teraz osobno cechy, których żaden sygnał nie
konsumuje. Pierwszy odczyt wyglądał sensacyjnie:

| cecha | 1 św. | 3 św. | 6 św. | 12 św. | n |
|---|---|---|---|---|---|
| `book_imbalance` | +0,0135 | +0,0172 | +0,0410 | +0,0161 | 1 616 |
| `book_spread_bps` | +0,0644 | +0,0798 | +0,1349 | **+0,1532** | 1 616 |

+0,1532 to **trzy i pół raza więcej niż najlepszy sygnał w projekcie**
(`volume_breakout`, −0,0455). Gdyby to była prawda, byłby to najważniejszy
wynik od początku prac.

## Nie jest

Naiwny błąd standardowy IC wynosi 1/√n, czyli ±0,0249 przy 1 616 obserwacjach.
Ale **zwrot z dwunastu świec liczony przy każdej świecy nakłada się na jedenaście
sąsiednich pomiarów** — te obserwacje nie są niezależne. Efektywna liczba
obserwacji to około n/k, więc przy horyzoncie 12 świec nie 1 616, tylko ~135,
a błąd standardowy nie ±0,025, tylko **±0,098**.

Po poprawce `book_spread_bps` wyraża się w błędach standardowych tak:

| | 1 św. | 3 św. | 6 św. | 12 św. |
|---|---|---|---|---|
| `book_spread_bps` | 2,6 | 1,8 | 2,1 | **1,6** |
| `book_imbalance` | 0,5 | 0,4 | 0,6 | 0,2 |

Czyli krąży wokół progu dwóch błędów standardowych, a przy najdłuższym
horyzoncie — tam, gdzie surowa liczba wyglądała najlepiej — jest **najsłabszy**.
To jest podręcznikowy kształt wyniku, który wygląda na odkrycie, dopóki nie
policzy się błędu poprawnie.

## Co ta poprawka pokazała przy okazji

Ten sam rachunek zastosowany do sygnałów, które już mamy:

| sygnał | 1 św. | 3 św. | 6 św. | 12 św. |
|---|---|---|---|---|
| `mean_reversion_rsi` | **6,9** | **6,6** | **4,1** | **3,1** |
| `momentum_multi` | **6,6** | **5,7** | 2,2 | 1,3 |
| `range_extreme` | **6,3** | **5,6** | **3,6** | 1,9 |
| `trend_follow` | **3,4** | **6,0** | **4,5** | **3,3** |
| `volume_breakout` | **4,0** | **3,8** | 2,3 | 0,9 |
| `cross_section` | **3,0** | 2,4 | 2,1 | 1,3 |
| `funding_carry` | 0,4 | 1,4 | 0,2 | 0,9 |

Dwie rzeczy, których wcześniej nie było widać:

- **`funding_carry` jest nieodróżnialny od zera na każdym horyzoncie.** Sygnał
  ożył dopiero po migracji 0021 (wcześniej pary USDC nie miały podpiętego
  finansowania) i pierwszy pomiar mówi, że nie niesie informacji.
- **`volume_breakout` i `momentum_multi` tracą istotność przy dłuższych
  horyzontach**, mimo że ich surowe IC pozostaje duże. Ich wartość jest krótka.

## Czego to nie zmienia

Nic tutaj nie jest przewagą. Najsilniejszy sygnał, `mean_reversion_rsi`, ma IC
+0,034 — przy rundzie kosztowej 0,200% to nadal za mało, żeby pokryć koszty.
Poprawka na nakładanie się obserwacji nie odkryła nowej informacji; **odebrała
pozorną**.


---

# Czy sygnał w ogóle wyczuwa czas — pierwszy test permutacyjny

Projekt mierzył dotąd dwie rzeczy i **żadna nie odpowiadała na to pytanie**:

- **Odsetek dodatnich okien** premiuje nieuczestniczenie. Strategia trzymająca
  małą ekspozycję na spadającym rynku ma dużo okien dodatnich, bo rzadko traci —
  i rzadko cokolwiek wyczuwa.
- **Pokonanie kup-i-trzymaj** porównuje z pełną ekspozycją. Strategia
  o dwuprocentowej ekspozycji pobije trzymanie na każdym rynku spadającym
  i przegra na każdym rosnącym, niezależnie od tego, czy jej wejścia są trafione,
  czy losowe.

Brakowało benchmarku z **tą samą ekspozycją i losowym czasem**.

## Konstrukcja

Dla każdego okna testowego bierzemy faktyczne transakcje i zachowujemy z nich
**wszystko poza momentem wejścia**: liczbę, długość każdej z nich, instrument,
okno i model kosztów. Moment wejścia losujemy — 200 układów na okno. Potem
sprawdzamy, na którym percentylu rozkładu losowego wypada wynik faktyczny.

To nie jest kolejna hipoteza. Model zerowy nie jest kandydatem do wyboru:
niczego nie optymalizuje, nie zmienia wag i nie zapisuje nic do
`learn.hypothesis_trial`.

## Wynik

```
Okien: 33
Średni percentyl wyniku faktycznego wśród losowych: 43,3
   (oczekiwane 50, błąd std. ±5,0, odchylenie −1,33)
Okien powyżej mediany losowej: 14/33 (42%)
   (oczekiwane 16,5, odchylenie −0,87)
```

**Nieodróżnialne od losowego.** Oba odchylenia mieszczą się poniżej dwóch
błędów standardowych, więc nie da się stwierdzić ani trafności, ani szkodliwości
wyboru momentu.

Ale kierunek jest jednoznaczny w obu miarach i wskazuje **poniżej** losowego —
a to znaczy, że nie ma podstaw twierdzić, że sygnał wnosi cokolwiek w wymiarze
czasu.

## Co to znaczy dla odsetka 74%

Świeca 4h daje 74,4% dodatnich okien i to jest najwyższa liczba w projekcie.
Ten test mówi, skąd ona pochodzi: **z profilu ekspozycji, nie z trafności
wejść.** Strategia rzadko traci, bo rzadko jest na rynku — a gdy już wchodzi,
robi to nie lepiej niż rzut monetą.

Trzy niezależne pomiary układają się teraz w jedno zdanie:

| pytanie | miara | odpowiedź |
|---|---|---|
| czy sygnały niosą informację? | IC | +0,034 najsilniejszy, przy koszcie 0,200% |
| czy strategia bije nierobienie niczego? | kup-i-trzymaj | 43–50%, czyli nie |
| czy moment wejścia coś znaczy? | test permutacyjny | 43,3 percentyla, czyli nie |

## Jak liczona jest istotność

Przy braku trafności percentyl każdego okna ma rozkład **jednostajny** na
(0, 100): średnia 50, odchylenie 100/√12 ≈ 28,9. Błąd standardowy średniej
z 33 okien to 28,9/√33 ≈ 5,0.

Narzędzie liczy to samo i wypisuje werdykt, zamiast zostawiać liczbę czytelnikowi.
Bez tego „43,3" zachęca do wniosku „gorzej niż losowo", który przy trzydziestu
kilku oknach nie jest uprawniony — ten sam błąd, który tego samego dnia zamienił
mikrostrukturę w pozorne odkrycie.


---

# Nowy sygnał nie mógł wejść do modelu przez dwadzieścia miesięcy

Znalezione przy przygotowaniu kanału newsowego, zanim klucz Anthropic
w ogóle dotarł.

## Mechanizm

Wagi produkcyjne to **mediana po wszystkich oknach** walk-forward. Sygnał
uznany za martwy na danym oknie (`martwe_sygnaly` — niezerowy wynik rzadziej
niż w 1% świec) jest z kalibracji wykluczany i dostaje w tym oknie **zero**.

To zero trafiało do mediany na równi z wagami zmierzonymi. Ale ono nie jest
pomiarem „waga powinna być zerowa" — jest **brakiem pomiaru**. Wliczanie go
do mediany to uśrednianie braków danych jako zer.

## Skutek, policzalny co do dnia

`news_shock` bez klucza jest martwy we wszystkich oknach. Po wpięciu klucza
ożywa najpierw w jednym oknie, potem w kolejnych — w tempie jednego okna na
30 dni. Żeby **mediana** z 39 okien przestała być zerem, musiałby żyć w 20
z nich:

```
20 okien × 30 dni = 600 dni ≈ 19,7 miesiąca
```

Faza demonstracyjna trwa sześć miesięcy. **Sygnał, którego dane zaczęły
istnieć dziś, nie mógł wejść do modelu nigdy w horyzoncie tego projektu** —
niezależnie od tego, ile informacji niesie.

Dotyczyło to każdego przyszłego sygnału na nowych danych, nie tylko newsów:
mikrostruktura arkusza trafiłaby dokładnie na to samo.

## Poprawka

Mediana liczona **wyłącznie po oknach, w których sygnał żył**. Okna, gdzie był
martwy, nie głosują — bo nie mają czym.

To wprowadza drugi problem: sygnał żywy w jednym oknie dostałby wagę z jednego
losowania. Stąd próg `MIN_OKIEN_ZYWEGO = 5` — pięć niezależnych okresów
testowych, czyli około pięciu miesięcy przy kroku 30 dni. Mieści się w fazie
demonstracyjnej i jest wystarczająco dużo, żeby mediana nie była pojedynczym
pomiarem.

| próg | historia potrzebna |
|---|---|
| mediana po wszystkich oknach (przed) | 600 dni ≈ 19,7 mies. |
| 5 żywych okien (po) | 150 dni ≈ 4,9 mies. |

## Wpływ na dzisiejszy wynik: żaden

28/39 dodatnich okien (71,8%) wobec 29/39 (74,4%) w przebiegu nocnym, mediana
Sharpe +0,126 wobec +0,100 — różnice w granicach zmierzonego szumu. Dziś
jedynym martwym sygnałem jest `news_shock`, martwy we **wszystkich** oknach,
więc poniżej progu pięciu i tak dostaje zero.

Zmiana nie poprawia wyniku. Odblokowuje możliwość, żeby nowy sygnał kiedykolwiek
go poprawił.

---

# Warunek „lepiej niż kupić i trzymać" mierzył znak rynku, nie strategię

Znalezione 2026-09-07 przy przeglądzie nocnego przebiegu.

## Obserwacja

Warunek `bije_odniesienie` jest jedynym niespełnionym kryterium przejścia na
live: odsetek dodatnich okien (74,4%) i mediana Sharpe (+0,100) są powyżej
progów, a strategia bije kup-i-trzymaj w 17 oknach na 39 — czyli poniżej połowy.

Rozbicie tych 17 okien według kierunku rynku daje wynik, który nie zostawia
miejsca na interpretację:

| horyzont | okien | kup-i-trzymaj ujemny | strategia wyżej | kup-i-trzymaj dodatni | strategia wyżej |
|---|---|---|---|---|---|
| 4h | 39 | 17 | **17** | 22 | **0** |
| 1h | 88 | 48 | **48** | 40 | **0** |

**Sto dwadzieścia siedem okien, zero wyjątków.** Wynik porównania równał się
znakowi rynku. Warunek nie zawierał ani jednego bitu informacji o strategii.

## Dlaczego tak być musiało

Limit `max_total_exposure` to 15 000 przy kapitale 100 000, czyli **15%**.
Budżet na pozycję to ten limit podzielony przez liczbę instrumentów, a udział
budżetu to `min(1, |score| * 2)` — więc pojedyncza pozycja waży 0,75–1,5%
kapitału, a cały portfel nigdy więcej niż 15%.

Kup-i-trzymaj angażuje 100% przez całe okno.

Przy takiej różnicy:

- rosnącego rynku **nie da się pobić** — siedmiokrotnie mniejsza ekspozycja
  musiałaby nadrobić siedmiokrotnie lepszym timingiem w każdym oknie;
- ze spadającym rynkiem **nie da się przegrać** — nie ma czym stracić.

Warunek był więc nieosiągalny przez konstrukcję na rynku, który w badanym
okresie rósł w 22 oknach na 39. Gdyby krypto w tych czterech latach spadało,
przeszedłby sam z siebie — przy strategii bez jakiejkolwiek przewagi.

To ten sam kształt błędu, co prowizja liczona poza miarą: liczba wyglądała na
pomiar umiejętności, a mierzyła coś innego, i nie objawiało się to awarią.

## Poprawka

Odniesienie skalowane **średnią ekspozycją okna**:

```
warunek:  zwrot_strategii > ekspozycja × zwrot_kup_i_trzymaj
```

Zwrot z konta jest w wielkości pozycji liniowy po obu stronach, więc to
dokładnie `zwrot/ekspozycja > kup_i_trzymaj` — „czy złoty zaangażowany przez
strategię zarobił więcej niż złoty włożony w koszyk". **Zmniejszeniem pozycji
nie da się tego warunku przejść**: skraca obie strony jednakowo.

Symulacja liczyła dotąd wyłącznie ekspozycję **szczytową**. Szczyt mówi, jak źle
mogło być w najgorszym momencie; ile kapitału faktycznie pracowało — nie mówi
nic. `PortfolioResult.avg_exposure` liczy średnią po **wszystkich** świecach
okna, także tych bez pozycji.

Stara liczba jest nadal raportowana, bo opisuje realny zwrot z konta. Jest tylko
opisana tym, czym jest: funkcją znaku rynku.

## Czego ta poprawka NIE rozstrzyga

Osobne pytanie, którego projekt dotąd nie zadał: **czy 15% ekspozycji to
właściwa liczba.** Suma zwrotów strategii przez 39 okien to +9,99% wobec
+100,47% dla kup-i-trzymaj — około 3% rocznie, poniżej stopy wolnej od ryzyka.
Przy dodatniej przewadze na jednostkę kapitału to jest pytanie o wielkość
pozycji, nie o sygnały. Ale odpowiedź wymaga pomiaru, nie decyzji — bo
ekspozycja wchodzi też do obsunięcia i do limitu straty dziennej.

---

# Pomiar po poprawce: przewaga jest, kapitału nie ma

Pierwszy przebieg po naprawie warunku odniesienia, 2026-09-07, horyzont 4 h,
`spot_eu` ze zleceniami z limitem, 39 okien.

## Liczby

| | |
|---|---|
| średnia ekspozycja | **1,28% kapitału** |
| mediana ekspozycji | 1,00% |
| maksimum | 3,50% |
| okien z ekspozycją poniżej 1% | 19 z 39 |
| limit łącznej ekspozycji | 15 000 z 100 000, czyli **15%** |
| suma zwrotów z okien | +10,07% przez 3,2 roku (**~3% rocznie**) |
| suma kup-i-trzymaj | +99,67% |
| mediana zwrotu na jednostkę ekspozycji | **+0,15 na okno** |
| pokonane odniesienie przy tej samej ekspozycji | **30/39 (76,9%)**, przedział 61,7%–87,4% |
| pokonane odniesienie zaangażowane w 100% | 17/39 (43,6%) — funkcja znaku rynku |
| status | **`validated`** |

## Co z tego wynika

Dwie rzeczy naraz, i trzeba je czytać razem.

**Strategia ma przewagę.** Mediana zwrotu na jednostkę zaangażowanego kapitału
to +0,15 na okno — złotówka włożona przez strategię pracuje wyraźnie lepiej niż
złotówka w koszyku. Dolna granica przedziału ufności (61,7%) jest powyżej 50%,
więc to nie jest rzut monetą.

**Strategia nie angażuje kapitału.** 1,28% średnio, przy własnym limicie 15% —
używa **jednej dwunastej swojego budżetu ryzyka**. Wynik w złotówkach jest
w efekcie bliski zeru: 3% rocznie to poniżej stopy wolnej od ryzyka.

## Dziura w kryteriach, którą to odsłoniło

Kryteria przejścia na live mierzą **umiejętność**. Nie mierzą, czy
zaangażowanych złotówek jest na tyle dużo, żeby wynik cokolwiek znaczył.

Dopóki warunek odniesienia był nieosiągalny przez konstrukcję, nikt nie mógł tej
dziury zobaczyć — status nigdy nie dochodził do `validated`. Jeden błąd pomiaru
zasłaniał drugi brak.

Statusu nie zmieniono: to decyzja o polityce przejścia na live, a nie o pomiarze.
Zmieniono **komunikat** — skala wyniku idzie teraz razem ze statusem, z tego
samego powodu, dla którego przedział ufności idzie razem z odsetkiem.
„Kryteria spełnione" bez liczby 1,28% obok czyta się jak zielone światło do
handlu prawdziwymi pieniędzmi.

## Pytanie otwarte

Dlaczego ekspozycja jest dwunastokrotnie niższa od limitu. Kandydaci, w kolejności
łatwości sprawdzenia:

1. **Próg wejścia** (`MIN_CONFIDENCE = 0,25`, plus percentylowa bramka liczona na
   oknie treningowym) przepuszcza za mało świec — 30–70 transakcji na okno przy
   10 instrumentach i 180 świecach.
2. **`udzial_budzetu(score) = min(1, |score| × 2)`** — przy typowym score bliskim
   progu udział wynosi ~0,5, czyli połowę i tak małego budżetu.
3. **Horyzont 4 h** zamyka pozycje szybko, więc kapitał pracuje krótko; średnia
   po wszystkich świecach spada nawet przy sensownej wielkości pojedynczej pozycji.

Każdy z nich jest sprawdzalny osobno przebiegiem `--sucho`. Żadnego nie wolno
zmienić „bo wygląda na za ostry" — podniesienie ekspozycji podnosi też obsunięcie
i limit straty dziennej, a te są mierzone tą samą symulacją.

---

# Podniesienie ekspozycji nie kupuje zwrotu. Kupuje obsunięcie

Hipoteza z poprzedniej sekcji brzmiała: budżet na pozycję dzieli limit przez
**10 instrumentów**, zakładając, że zapełnią się wszystkie, a zapełnia się 2,57 —
więc budżet jest liczony pod portfel, który nie powstaje. Wygląda na darmowe
czterokrotne podniesienie zwrotu przy tym samym twardym limicie.

**Jest odwrotnie.** Zmierzone 2026-09-07, ten sam przebieg, zmieniony wyłącznie
dzielnik budżetu (`--zakladane-pozycje`, wymaga `--sucho`):

| dzielnik | ekspozycja | pozycji w rynku | suma zwrotów | mediana Sharpe | dodatnie okna | obsunięcie | status |
|---|---|---|---|---|---|---|---|
| **10** (produkcja) | 1,29% | 2,57 | +9,6% | +0,111 | 30/39 (77%) | **−2,85%** | `validated` |
| 6 | 1,92% | 2,47 | +11,0% | +0,086 | 28/39 (72%) | −4,21% | `validated` |
| 3 | 3,01% | 1,92 | **+6,0%** | +0,042 | 23/38 (61%) | −4,50% | `learning` |

## Co się psuje

**Większe pozycje wypychają się nawzajem z portfela.** Przy dzielniku 3 budżet
na pozycję to 5% kapitału, więc trzy pozycje wypełniają limit łącznej ekspozycji
w całości. Dalsze wejścia są blokowane — w oknach widać `odrzuconych wejść: 17`.
Liczba pozycji jednocześnie **spada** z 2,57 do 1,92, mimo że kapitału jest
więcej.

A to właśnie rozproszenie na kilka instrumentów zamienia mały, zaszumiony zysk
na transakcję w **konsekwentnie dodatnie okno**. Skoncentrowany portfel ma tę
samą przewagę na jednostkę kapitału, ale wariancję na tyle większą, że odsetek
dodatnich okien spada z 77% do 61%, a status wraca do `learning`.

## Czego ten pomiar NIE mówi

Że dzielnik 6 jest lepszy od 10. Różnica zwrotu (+11,0% wobec +9,6% przez
3,2 roku) mieści się w szumie między przebiegami — ten sam wariant uruchomiony
godzinę wcześniej dał 28/39 i +10,07% zamiast 30/39 i +9,6%. Różnica obsunięcia
(−4,21% wobec −2,85%) jest za to duża i idzie konsekwentnie w jedną stronę.

Wniosek uczciwy brzmi więc: **podniesienie ekspozycji kupuje mierzalne obsunięcie
za zwrot nieodróżnialny od szumu.**

## Dokąd to przekierowuje pracę

Ekspozycja 1,29% nie jest błędem konfiguracji. Jest tym, na co pozwala gęstość
sygnału. Wąskim gardłem nie jest wielkość pozycji — jest to, że strategia
**stoi poza rynkiem przez 57% czasu i znajduje 2,5 okazji jednocześnie na
dziesięć możliwych**.

Żeby wynik zaczął cokolwiek znaczyć w złotówkach, potrzeba więcej sygnału, nie
większych pozycji. W kolejności, w jakiej da się to sprawdzić:

1. **Analiza wiadomości** — od 2026-09-07 działa na produkcji i jest dokładnie
   tym: nowym źródłem wejść. Wchodzi do wag po pięciu żywych oknach.
2. **Szersze uniwersum** — 10 instrumentów to sufit liczby jednoczesnych pozycji.
   Podmiot unijny ma 113 par USDC; próg `MIN_INSTRUMENTS` to 8.
3. **Krótszy horyzont** — 1 h daje więcej świec decyzyjnych, ale przy koszcie
   rundy 0,200% amplituda świecy przestaje go pokrywać (patrz sekcja o wyborze
   horyzontu wyżej).

Pierwsza z nich już biegnie i nic nie kosztuje. Druga jest sprawdzalna jednym
przebiegiem `--sucho --instrumenty`.

---

# Dwa dobre uniwersa dają jedno złe. Wynik, którego nie zakładała żadna hipoteza

Poprzednia sekcja przekierowała pracę na gęstość sygnału: strategia stoi poza
rynkiem 57% czasu i znajduje 2,5 okazji na 10 możliwych, więc naturalnym ruchem
jest **więcej instrumentów**. Baza ma historię szerszą, niż handlujemy — 24 pary
USDT zarejestrowane w 0020, z pełnymi cechami.

Zmierzone 2026-09-07, horyzont 4 h, `spot_eu` ze zleceniami z limitem:

| wariant | instr. | ekspozycja | w rynku | pozycji | zwrot | mediana Sharpe | dodatnie okna | status |
|---|---|---|---|---|---|---|---|---|
| **produkcja** | 10 | 1,29% | 43% | 2,57 | **+9,6%** | +0,111 | 30/39 (77%) | `validated` |
| budżet ÷6 | 10 | 1,92% | 42% | 2,47 | +11,0% | +0,086 | 28/39 (72%) | `validated` |
| budżet ÷3 | 10 | 3,01% | 43% | 1,92 | +6,0% | +0,042 | 23/38 (61%) | `learning` |
| **tylko 14 nowych** | 14 | 1,10% | 44% | 2,98 | +4,1% | **+0,129** | 24/39 (62%) | `learning` |
| 24, budżet ÷23 | 24 | 1,28% | 59% | 4,47 | +3,7% | +0,038 | 22/40 (55%) | `learning` |
| 24, budżet ÷10 | 24 | **2,53%** | 58% | 3,99 | **+1,7%** | +0,020 | 21/40 (52%) | `learning` |

## Co zadziałało dokładnie tak, jak miało

Szersze uniwersum **rozwiązuje problem gęstości sygnału**. Czas w rynku rośnie
z 43% do 59%, liczba pozycji jednocześnie z 2,57 do 4,47. Przy zachowanej
wielkości pozycji ekspozycja podwaja się: 1,29% → 2,53%.

Uwaga metodologiczna do wiersza „24, budżet ÷23": tam ekspozycja **nie drgnęła**,
bo `position_budget` dzieli limit przez liczbę instrumentów — pozycje zmalały
dokładnie o tyle, o ile przybyło okazji. Ten przebieg zmieniał więc dwie rzeczy
naraz i sam z siebie niczego nie rozstrzyga. Rozdziela je dopiero wiersz
„budżet ÷10".

## Czego nie zakładała żadna hipoteza

**Każda z dwóch grup osobno ma przewagę. Ich suma jej nie ma.**

```
10 par USDC        mediana Sharpe +0,111   dodatnie 77%
14 par USDT        mediana Sharpe +0,129   dodatnie 62%
24 razem           mediana Sharpe +0,020   dodatnie 52%
```

To nie jest własność dołożonych aktywów — osobno mają **najwyższą jakość na
transakcję ze wszystkich sześciu przebiegów**. Coś psuje się dopiero przy
połączeniu.

## Kandydaci, w kolejności prawdopodobieństwa

1. **Wagi kalibrowane wspólnie.** Trener dopasowuje jeden wektor wag na regim
   dla całego uniwersum. Dwie grupy o różnej charakterystyce dostają kompromis,
   który jest gorszy dla obu niż wagi dopasowane osobno. To tłumaczyłoby wynik
   bez odwoływania się do czegokolwiek innego — i jest sprawdzalne.
2. **`rel_strength` jest cechą przekrojową.** Ranking liczy się po uniwersum
   decyzyjnym, więc zmiana uniwersum zmienia wartość tej cechy dla **każdego**
   instrumentu.

   Sprawdzone 2026-09-07, po podejrzeniu, że wartości historyczne pochodzą
   z rankingu po 24 instrumentach. **Podejrzenie było błędne.** Krok wartości
   zdradza liczność zbioru: `percent_rank` po *n* elementach daje wartości co
   `2/(n-1)`. Zmierzone na dwóch losowych świecach: 0,2222 (= 2/9, dziesięć
   instrumentów) i 0,2500 (= 2/8, dziewięć — NEARUSDC nie był jeszcze notowany).
   Ranking zawsze szedł po zbiorze `tradeable`.

   Mylące było `count(distinct instrument_id) = 34`: liczy instrumenty, które
   **mają wiersze**, a nie te, po których liczono ranking. Dwadzieścia cztery
   z nich to martwe pozostałości sprzed migracji 0023.

   **Ale zastrzeżenie do wierszy z szerszym uniwersum zostaje, i teraz jest
   precyzyjne.** Przebiegi na 24 instrumentach czytały dla czternastu z nich
   `rel_strength` policzone przed 0023, dla **innego zbioru**. Przed każdym
   kolejnym porównaniem uniwersum trzeba więc najpierw przeliczyć tę cechę dla
   zbioru kandydującego:

   ```bash
   python services/ingest/features.py --bar 4h --backfill --days 1400 \
       --tylko-przekroj --instrumenty <lista kandydująca>
   ```

   Bez tego porównanie idzie na siedmiu sygnałach zamiast ośmiu, czyli mierzy
   inną strategię, a nie inne uniwersum.
3. **Korelacja.** Pozycje w 24 skorelowanych aktywach to mniej niezależnych
   zakładów, niż sugeruje ich liczba. Zastrzeżenie o efektywnej liczbie
   obserwacji, opisane wyżej przy transakcjach, dotyczy tu wprost ekspozycji.

## Wniosek operacyjny

Uniwersum zostaje przy dziesięciu parach USDC. Trzy kierunki sprawdzone,
wszystkie gorsze od stanu obecnego:

- większe pozycje → obsunięcie rośnie szybciej niż zwrot,
- szersze uniwersum → więcej okazji, mniej przewagi,
- jedno i drugie naraz → najgorszy wynik z sześciu.

Konfiguracja produkcyjna jest **zmierzonym optimum lokalnym**, a nie wartością
domyślną, której nikt nie sprawdził. Następne pytanie nie brzmi „ile
instrumentów", tylko „czy wagi mają być wspólne dla całego uniwersum" — i to
jest hipoteza dla osobnego przebiegu, nie dla zmiany w produkcji.

---

# Cztery hipotezy o „dwóch dobrych uniwersach", cztery obalone

Poprzednia sekcja zostawiła zagadkę: dziesiątka ma przewagę, czternastka ma
przewagę, a suma jej nie ma. Zmierzone teraz na **czystych danych** — po tym,
jak cecha przekrojowa dostała odcisk uniwersum (`docs/06`, migracja 0030),
więc każdy zbiór czyta wartości policzone dla siebie:

| uniwersum | dodatnie okna | mediana Sharpe | zwrot | transakcji/okno | rozrzut Sharpe |
|---|---|---|---|---|---|
| **10 (produkcja)** | 30/39 (77%) | **+0,111** | +9,6% | 34 | **0,239** |
| **14 osobno** | 23/39 (59%) | **+0,112** | +3,8% | 45 | 0,290 |
| **24 razem** | 20/40 (50%) | **+0,011** | +2,0% | 77 | 0,312 |

Jakość **na transakcję** obu grup jest praktycznie identyczna. Połączenie ją
zabija. Poniżej cztery wyjaśnienia, które sprawdziłem i które nie przeszły.

## 1. Skażona cecha przekrojowa — NIE

Podejrzenie: przebiegi na 24 czytały `rel_strength` policzone dla innego zbioru.
To była prawda, więc naprawiłem tożsamość cechy i policzyłem ją osobno dla
każdego uniwersum. Wynik dla 24: 20/40 wobec 21/40 przed poprawką, mediana
+0,011 wobec +0,020. **Bez zmiany.**

## 2. Wspólny wektor wag — NIE

Podejrzenie: trener dopasowuje jeden wektor wag na reżim dla całego uniwersum,
więc dwie grupy o różnej charakterystyce dostają kompromis gorszy dla obu.

Współczynnik informacji policzony osobno dla każdej grupy (`diagnose_signals.py
--instrumenty`, horyzont 3 świec):

| sygnał | 10 | 14 | znak |
|---|---|---|---|
| mean_reversion_rsi | +0,0329 | +0,0399 | zgodny |
| range_extreme | +0,0173 | +0,0218 | zgodny |
| trend_follow | −0,0143 | −0,0281 | zgodny |
| momentum_multi | −0,0160 | −0,0251 | zgodny |
| volume_breakout | −0,0590 | −0,0457 | zgodny |
| cross_section | −0,0037 | −0,0164 | zgodny |
| funding_carry | +0,0116 | +0,0070 | zgodny |

**Wszystkie siedem ma ten sam znak w obu grupach**, a czternastka ma je nawet
silniejsze. Obie grupy chcą tego samego kierunku, więc kompromis nie ma na czym
powstać.

## 3. Wypychanie z portfela — NIE

Podejrzenie: kandydaci są sortowani po sile sygnału, a czternastka ma sygnały
mocniejsze — więc mogła wypchnąć dziesiątkę z portfela o wspólnym kapitale.

Udział w transakcjach przy 24 instrumentach: sześć największych po 4–5% przy
równym udziale 4,2%. **Żaden instrument nie przekracza dwukrotności równego
udziału.** Portfel jest rozłożony równo.

## 4. Zwykła dywersyfikacja — NIE, i to jest wskazówka

Więcej instrumentów powinno **zmniejszyć** rozrzut wyników między oknami.
Zmierzone jest odwrotnie:

```
10 instrumentów  →  34 transakcje/okno  →  rozrzut Sharpe 0,239
14 instrumentów  →  45 transakcji/okno  →  rozrzut Sharpe 0,290
24 instrumenty   →  77 transakcji/okno  →  rozrzut Sharpe 0,312
```

Rozrzut rośnie razem z liczbą transakcji. **Dokładane transakcje nie są
niezależnymi zakładami** — to w dużej mierze ten sam zakład powtórzony.
Zastrzeżenie o efektywnej liczbie obserwacji, które w tym dokumencie towarzyszy
transakcjom od początku, dotyczy tu wprost jakości okien.

## Co z tego zostaje

Efekt jest **realny i powtarzalny**, a cztery najbardziej naturalne wyjaśnienia
zostały wykluczone pomiarem. To zawęża mechanizm do jednego miejsca: sposobu,
w jaki zwroty z pojedynczych transakcji składają się na wynik OKNA.

`Sharpe/transakcję` w oknie to `średnia / odchylenie` po wszystkich transakcjach
tego okna. Grupy o różnej zmienności, wrzucone do jednej takiej statystyki, dają
wynik, który nie jest średnią ich wyników. Następny pomiar musi więc rozbić
transakcje w przebiegu łączonym **na grupy pochodzenia** i policzyć średnią oraz
odchylenie osobno dla każdej — dopiero to powie, czy grupy się znoszą, czy jedna
zawyża mianownik drugiej.

Do tego czasu wniosek operacyjny zostaje bez zmian: **uniwersum zostaje przy
dziesięciu parach USDC.** Nie dlatego, że rozumiemy dlaczego — dlatego, że
zmierzone alternatywy są gorsze.

---

# Rozstrzygnięcie: nie miara, tylko jedna grupa traci przewagę w towarzystwie

Poprzednia sekcja zawęziła mechanizm do sposobu, w jaki zwroty z transakcji
składają się na wynik okna, i postawiła pytanie rozstrzygające: czy przy
połączeniu załamuje się **miara**, czy **strategia**.

Pomiar liczy `Sharpe/transakcję` osobno dla każdej grupy, **w tym samym oknie
i tym samym przebiegu** (grupowanie po walucie kwotowanej — dokładnie ta sama
linia, która dzieli uniwersum decyzyjne od reszty historii):

```
przebieg na 24 instrumentach, budżet jak w produkcji
    USDC (10):  mediana Sharpe  +0.111   przez 37 okien
    USDT (14):  mediana Sharpe  −0.015   przez 40 okien
    łącznie:    mediana Sharpe  +0.011
```

Dla porównania, te same grupy uruchomione **osobno**:

```
    USDC (10) osobno:  +0.111
    USDT (14) osobno:  +0.112
```

## Co to znaczy

**Miara jest w porządku.** Dziesiątka zachowuje w przebiegu łączonym dokładnie
ten sam wynik co samodzielnie — +0,111 wobec +0,111. Gdyby problemem było
wrzucanie grup o różnej zmienności do jednej statystyki, ucierpiałyby obie.

**Czternastka traci całą przewagę wyłącznie w towarzystwie**: +0,112 samodzielnie,
−0,015 obok dziesiątki. Te same instrumenty, te same świece, ta sama historia.

Połączenie nie uśrednia więc dwóch dobrych wyników w mierny. Zostawia jeden
nietknięty i **zabija drugi**.

## Konsekwencja dla wniosku operacyjnego

Zmienia się jego treść, nie sam wniosek. Do tej pory brzmiał „uniwersum zostaje
przy dziesiątce, bo alternatywy są gorsze". Teraz wiadomo, dlaczego są gorsze:
czternastka dołożona do dziesiątki **nie jest utraconą szansą, tylko strumieniem
transakcji o ujemnej przewadze**. Tłumaczy to też wszystko pozostałe — mediana
łączna ląduje między +0,111 a −0,015, a rozrzut rośnie, bo dokładany strumień
nie niesie przewagi, za to niesie zmienność.

## Pytanie, które z tego zostaje

Dlaczego czternastka traci przewagę tylko w towarzystwie. Sygnały mają w obu
grupach **ten sam znak** (sekcja wyżej), więc to nie jest sprzeczność kierunku.
Ale zgodność znaków nie znaczy zgodności **wielkości**: optymalne proporcje wag
mogą się różnić mocno przy identycznych kierunkach, a wagi są dopasowywane
wspólnie dla całego uniwersum.

Co ważne, czternastka wnosi **więcej obserwacji** do tego dopasowania niż
dziesiątka (113 tys. wobec 77 tys.), więc to ona dominuje wektor wag — i mimo to
ona traci. Wskazuje to, że dziesiątka jest po prostu **odporna na złe wagi**,
a czternastka nie. To jest hipoteza sprawdzalna: wystarczy zmierzyć, jak wynik
każdej grupy reaguje na wagi dopasowane do tej drugiej.

## Eksperyment: pozycjonowanie tłumu (2026-09-08)

**Pytanie właściciela:** czy kopiować ruchy najlepszych traderów crypto?

Kopiowania nie da się zrobić i nie warto — `/v5/copytrading/*` zwraca 404 na
wszystkich hostach, wewnętrzny ranking Bybita odpowiada 403, podmiot unijny nie
ma perpetuali w ogóle, a czołówka rankingu to prawy ogon rozkładu, zdominowany
przez szczęście pomnożone przez dźwignię. Mierzalna jest natomiast
**zagregowana** informacja o ustawieniu tłumu, bez wybierania zwycięzców.

Zebrano 779 844 godzin z 24 rynków (listopad 2022 – wrzesień 2026): udział kont
długich i otwarte pozycje na perpetualach. Z tego trzy cechy i jeden sygnał
kontrariański, wymagający DWÓCH warunków naraz: skrajnego wychylenia tłumu
**i** rosnących otwartych pozycji.

### Wynik — 40 okien walk-forward, oba przebiegi w trybie suchym

| | bez sygnału | z sygnałem | różnica |
|---|---|---|---|
| Okna dodatnie | 25/40 (62,5%) | 25/40 (62,5%) | **bez zmian** |
| Mediana Sharpe (OOS) | +0,070 | +0,121 | +0,051 |
| Suma zwrotów z 40 okien | +7,6% | +6,6% | −1,0 pp |
| Średnia ekspozycja | 1,22% | 1,01% | −0,21 pp |
| Najgorsze obsunięcie | −2,83% | −2,75% | +0,08 pp |

### Odczytanie, które nie da się nadużyć

Mediana Sharpe'a rośnie o 73% i **jest to liczba myląca, jeśli podać ją samą**.
Sygnał obniża ekspozycję o 17% i to wyjaśnia obie zmiany naraz: mniej
zaangażowanego kapitału daje lepszy wynik na jednostkę ryzyka i jednocześnie
mniejszy zwrot łączny.

Po znormalizowaniu na ekspozycję (zwrot na jednostkę zaangażowanego kapitału)
przewaga wynosi **+4,9%** — przy **identycznej** liczbie okien dodatnich (25/40
w obu przebiegach). Sygnał nie zmienia więc tego, KTÓRE okna są zyskowne;
zmienia wyłącznie, ile kapitału jest w rynku.

### Werdykt: zostaje za bramką

To nie jest dowód przewagi. Cztery i pół procent na jednostkę ekspozycji przy
niezmienionej trafności okien nie odróżnia się od szumu, a sygnał, który
głównie tłumi ekspozycję, jest **decyzją o apetycie na ryzyko, a nie zmierzoną
przewagą**. Włączenie go byłoby zmianą profilu ryzyka pod pozorem poprawy
wyniku.

Sygnał zostaje w `EKSPERYMENTALNE`: liczy się wyłącznie w przebiegach z jawnie
ustawionym `SYGNALY_EKSPERYMENTALNE` i nie ma wagi domyślnej w żadnym reżimie.

**Co zmieniłoby ten werdykt:** wzrost liczby okien dodatnich, a nie samego
Sharpe'a. Dane zbierają się dalej, więc pomiar da się powtórzyć na dłuższej
historii bez żadnej dodatkowej pracy.
