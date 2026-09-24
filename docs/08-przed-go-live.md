# Trader-AI przed go-live

Stan na 2026-09-24. Dokument zywy — odpowiednik strony
https://claude.ai/code/artifact/2e0282c6-4ad2-42cf-b864-3ead3e76901a

Horyzont: rok. Decyzja wlasciciela z 2026-09-24: "lepiej zeby bylo dobrze niz szybko".

## Stan na dzis

Produkcja stoi na `sha-c758da2`, wszystkie workery `1/1`, drzewo czyste.
Pelny zestaw testow: 1861 przeszlo, 25 pominietych, 0 bledow.

Dwie rzeczy naprawione w tej turze byly awariami produkcji, nie hipotezami:

| co | objaw | przyczyna |
| --- | --- | --- |
| `main` niespojny | 8 modulow testowych nie zbiera sie na czystym checkoucie | `ab71f7d` dodalo import `MINUT_TIMEFRAME` do `dozor.py`, ale mapa nigdy nie wyszla z czyjegos drzewa roboczego |
| `features-equity` w CrashLoopie | `unrecognized arguments: --okno-dni 7` | wpis w charcie uzywal flagi istniejacej tylko w niezacommitowanym kodzie; ArgoCD wdraza szablony natychmiast, bez podbicia tagu |

Pierwsza z nich dzialala po cichu przez kilka dni: **czujka zywotnosci rzucala
16 wyjatkow na przebieg i mimo to konczyla raportem**. Kazdy przebieg testow
przechodzil, bo kazdy szedl po drzewie roboczym, ktore akurat trzymalo
brakujacy plik.

Po naprawie `features-equity` liczy 6 849 wartosci cech na cykl, 0 bledow;
czujka zywotnosci ma 0 tracebackow; worker newsow na nowej, odcietej od kluczy
gieldy roli produkuje 36 analiz na dobe przy zerowej zaleglosci.

## Brama 0 — czy jest po co isc

Cala zmierzona przewaga to **13,76 punktu bazowego na runde**. Sama prowizja
spotu to 20 pb — wiecej niz caly zapas. Ta brama rozstrzyga wszystkie
pozostale: jesli jest zamknieta, reszta listy nie ma znaczenia.

Sharpe wychodzi z +0,0452, po korekcie odchylenia populacyjnego na probkowe
+0,0440, i dalej zalezy wylacznie od spreadu, ktorego nikt nie zmierzyl tam,
gdzie skladamy zlecenia.

| spread na runde | Sharpe |
| --- | --- |
| 0 pb | +0,0440 |
| 2 pb | +0,0386 |
| 5 pb | +0,0288 |
| 10 pb | +0,0124 |
| 13,76 pb | 0,0000 |

Tabela jest optymistyczna, nie ostrozna: 13,76 pb to prog dla miary zbiorczej,
a miara po oknach jest na koszt wrazliwsza (0,0065 wobec 0,0047 Sharpe'a na
punkt bazowy), wiec jej prog lezy nizej.

### Krotnosc zabojcza — pytanie, ktore rozstrzyga

Spready zmierzono na ksiedze GLOBALNEJ, a zlecenia ida na bybit.eu o okolo
0,1% tego wolumenu. Pytanie nie brzmi "ile wynosi spread", tylko "ile razy
szerszy jest u nas".

| podstawa | spread | krotnosc do zera |
| --- | --- | --- |
| rowna waga po 8 aktywach | 0,802 pb | 17,2x |
| mediana uniwersum | 0,982 pb | 14,0x |
| najszerszy aktyw (BNB) | 1,304 pb | 10,6x |
| p90 DOGE | 2,461 pb | 5,6x |
| p90 SUI | 2,728 pb | 5,0x |

Wniosek nie brzmi "przewagi nie ma". Brzmi: **o tym, czy jest, decyduje
wielkosc, ktorej nikt nie zmierzyl, i decyduje w zakresie calkowicie realnym
dla rynku o 0,1% wolumenu.** Jeden odczyt arkusza z hosta unijnego zamyka sprawe.

### Co jeszcze musi sie wydarzyc w tej bramie

- [ ] Zmierzyc spread na bybit.eu, nie na ksiedze globalnej — jedyny pomiar
      mogacy samodzielnie uniewaznic projekt
- [ ] Zmierzyc poslizg — dzis ZERO obserwacji, nie piec: z 49 wypelnien 31 ma
      cene zlecenia, a `limit_price != fill_price` nie zachodzi ani razu. Trzy
      wejscia maja kolumne zniszczona przez `zapisz_wejscie` sprzed poprawki;
      dwa wyjscia maja ja nietknieta i mimo to nie sa pomiarem — zlecenie z
      limitem wypelnia sie po cenie limitu albo lepiej, wiec pomiar wobec
      wlasnego limitu wykryje patologie, nie koszt
- [ ] Uruchomic `services/engine/wartosc_krotkiej.py` na bazie produkcyjnej —
      75% sygnalow ponad progiem to shorty, nieuzyteczne na spocie. Narzedzie
      liczy GORNE OGRANICZENIE, nie wartosc oczekiwana
- [ ] Decyzja wlasciciela, nie inzynierska: na bybit.eu perpetualow nie ma
      (spot 127, linear 0, inverse 0). Konto globalne, inna gielda, albo
      zostajemy na spocie
- [ ] Rozstrzygnac los equity: 23,1% okien dodatnich przy koszcie Bybita,
      32,7% przy polowie, 42,3% przy zerze — mediana OOS Sharpe wciaz ujemna.
      Koszt nie jest tu waskim gardlem, bo nie ma przewagi, ktora koszt by zjadl

## Brama 1 — uczciwosc pomiaru

Model uczy sie na ksiedze o okolo 1000x wiekszym wolumenie niz ta, w ktorej
sklada zlecenia. To rozjazd trening-produkcja co do tozsamosci rynku, nie
problem kosztow — i jest wazniejszy niz ktorykolwiek pojedynczy blad nizej.

Zmierzone 2026-09-07 na 1600 swiecach 4h: mediana rozjazdu 5,9 pb, p90 28,4 pb,
a w **16,8% swiec rozjazd przekracza cala runde kosztowa** (dla NEARUSDC w
polowie swiec). Cene limitu liczymy z ksiegi globalnej, zlecenie laduje w
unijnej. `fetch_funding` tez idzie jawnie na host globalny, wiec cecha, na
ktorej model sie uczy, pochodzi z innej ksiegi niz rynek, na ktorym handlujemy.

Skutek dziala w jedna strone: symulacja uznaje limit za wypelniony, gdy swieca
dotknela ceny, a produkcja sklada PostOnly — zlecenie przecinajace spread
zostaje ODRZUCONE, nie wypelnione takerem. Konwersja w produkcji jest wiec
nizsza niz w symulacji, na obu nogach.

- [ ] `train.py --kind` — symulacja liczy cennikiem Bybit spot niezaleznie od
      instrumentu. 19 punktow procentowych roznicy miedzy skrajnymi kosztami;
      przy strategii blisko progu ten dlug sam decydowalby o werdykcie
- [ ] `krotka_pozycja` dla `stock` i `etf` stoi na `True` (`koszty.py:98-99`).
      Dzis przed shortami na Alpace chroni nas wylacznie niewdrozony silnik —
      zabezpieczenie przez przypadek. Zmiana w kodzie, nie migracja: tabeli
      cennika w bazie nie ma
- [ ] `adjustment: "raw"` u Alpaki — trzy podzialy 10:1, okolo 670 swiec w
      cieniu jednego zdarzenia. Kazdy kolejny split wymusza powtorzenie backfillu

Po stronie pozytywnej: karta kosztow rozroznia teraz stan pochodzenia kazdego
skladnika. Spread z obcej ksiegi wchodzi jako PODLOGA, nie jako pomiar, a karta
zostaje niepelna — gdyby wszedl jako pomiar, oglosilaby rachunek zamknietym
przy nadal nieznanym skladniku, czyli zrobilaby dokladnie to, przed czym ma
bronic. Nieznane to dzis dokladnie dwa skladniki: niewypelnienie i spread.

## Brama 2 — dane

### Ujemny spread: rozstrzygniete

Wszystkie 23 wiersze z ujemnym spreadem w `market.book_snapshot` leza
**wylacznie na equity i wylacznie o 20:00 UTC** — w migawce zamkniecia sesji.
Ani jednego na krypto, ani jednego o innej godzinie. To nie jest blad zapisu
rozrzucony losowo ani awaria zbierania: to jedna chwila na dobe, w ktorej
ksiega jest skrzyzowana albo nieswieza.

Co czyni to grozniejszym, niz wyglada: przy 0,14-0,42% wierszy na instrument
MEDIANA POZOSTAJE ZDROWA — NVDA ma mediane +0,888 pb przy sredniej -19,637 pb.
Wady nie widac w zadnym podsumowaniu podajacym mediane, a wszystkie nasze
tabele podaja mediane.

- [ ] Odfiltrowac migawke zamkniecia sesji albo odrzucac skrzyzowana ksiege
      przy zapisie (`services/ingest/`)
- [ ] Sprawdzic, czy te wiersze weszly do cech, na ktorych trenowal sie model equity

### Pozostale pozycje

- [ ] `naprawa-B1-zasiew-demo.sql` — 25 920 swiec i 709 629 wierszy cech
- [ ] `naprawa-B4-news-sent.sql` — 747 680 z 749 591 wierszy bez pokrycia
- [ ] Uniwersum equity jest PUSTE Z DEFINICJI: wszystkie 23 instrumenty equity
      maja `tradeable = false`. Silnik liczy cechy i nie moze otworzyc zadnej
      pozycji. To swiadome, ale musi byc swiadome takze za rok — inaczej ktos
      bedzie szukal przyczyny braku transakcji w strategii
- [ ] `market.funding_rate` ma 102 845 wierszy na produkcji i zero lokalnie —
      dlatego wyceny krotkiej strony nie dalo sie policzyc poza produkcja

## Brama 3 — bezpieczenstwo i eksploatacja

- [ ] HASLO ZE ZNAKIEM SPECJALNYM ROZCINA LANCUCH POLACZENIA. Naprawa nie moze
      isc w szablon: `postgresql://user:$(ROLA_DB_PASSWORD)@...` sklada sie
      podstawieniem Kubernetesa, gdzie kodowanie procentowe jest niemozliwe.
      Trzeba przekazywac parametry polaczenia osobno. Cztery role dzialaja dzis
      przez przypadek — ich hasla po prostu nie trafily na `/`. Objaw jest
      mylacy: komunikat mowi o nierozwiazanym hoscie i porcie, a wina lezy w hasle
- [ ] ZMIANA SEKRETU NIE ZMIENIA HASLA W BAZIE — ustawia je migracja, ktora
      chodzi w kontenerze startowym API. Restart samego workera niczego nie
      naprawia. Warto to zapisac w procedurze, bo kosztowalo dwa nieudane starty
- [ ] Rotacja hasla ArgoCD i skasowanie `argocd-initial-admin-secret`
- [ ] `can_withdraw` NIGDY NIE SPRAWDZONE WOBEC GIELDY — `permissions` puste,
      kolumna z wartoscia domyslna. Przed prawdziwymi pieniedzmi to jest
      pytanie, czy klucz moze wyprowadzic srodki
- [ ] Bezpiecznik potoku newsow trzyma stan w atrybutach klasy — restart poda
      kasuje szesciogodzinna blokade
- [ ] `--engine` z domyslna wartoscia `"crypto"` w dwoch miejscach
      (`features.py`, `loop.py`)

Zamkniete w tej turze: skaner wiadomosci dostal wlasna role bazodanowa i NIE
WIDZI JUZ KLUCZY GIELDY. Proces parsujacy cudzy XML nie ma dostepu do sekretow,
ktorymi sklada sie zlecenia. Potok dziala dalej — 36 analiz na dobe, zerowa zaleglosc.

## Brama 4 — zgodnosc ksiag

- [ ] POZYCJE KSIEGOWANE BRUTTO TO WADA SYSTEMOWA OBJAWIAJACA SIE LOSOWO.
      Bybit pobiera prowizje kupna w kupionym aktywie, a `exec.position.qty`
      zapisywalo wartosc przed prowizja — wyjscie prosilo o wiecej, niz lezy na
      rachunku. Pozycja SOLUSDC odbila sie od gieldy cztery razy. Pozycje BTC i
      ETH z tego samego cyklu sprzedaly sie tylko dlatego, ze na koncie lezaly
      resztki z wczesniejszych rund. Wada milknie, gdy jest z czego doplacic —
      dlatego wyglada na incydent, a jest regula
- [ ] UZGODNIENIE MA DZIS LUKE POMIAROWA I SAMO TO MOWI: host demo nie wystawia
      sald, wiec porownania sald nie bylo. Zmierzono pytanie wezsze — zgodnosc
      z rejestrem transakcji. Niesprawdzone zostaja: reczne zamkniecie w panelu,
      przelewy i kazdy rozjazd wielkosci prowizji. Przed live musi byc
      zamkniete, nie opisane
- [ ] DZWIGNIA JEST SPRAWDZANA WYLACZNIE PRZY WEJSCIU. `evaluate_account`,
      ktora dozorca wola co 60 s, nie patrzy na nia wcale. Na shorcie to boli,
      bo dzwignia rosnie sama: notional rosnie z cena, kapital maleje, oba w te
      sama strone — czyli dokladnie wtedy, gdy nikt nie probuje wchodzic. To co
      do ksztaltu ta sama wada, dla ktorej w ogole powstalo `evaluate_account`.
      Wymaga rozstrzygniecia wlasnosci `stan_kapitalu.py`
- [ ] Gdyby perpetualy: pozycja perp nie jest saldem monety, a
      `/v5/position/list` w adapterze nie istnieje — uzgodnienie porownywaloby
      pozycje z pustka. Albo cisza "zgodne", albo alarm pozycja-widmo przy
      kazdej otwartej pozycji

Rozjazd ksiag na wyjsciach jest policzony: ETHUSDC 41,56 pb, BTCUSDC 5,34 pb,
mediana 23,45 pb. Rozjazd na wyjsciu ETH przekracza dwukrotnie cala runde
kosztowa spotu.

Zamkniete w tej turze: limit `max_leverage` z migracja 0053, pomiarem do
`risk.limit_usage` i regula fail-closed — instrument pozwalajacy na short bez
tego limitu dostaje `block`, a nie ostrzezenie. Regula blokujaca idzie przed
obcinajaca, bo w pierwszej wersji stala za `max_position_notional`, ktory
konczy sie `downsize` i zwraca — blokada byla wiec nieosiagalna dla zlecen
obcietych, czyli dla najwiekszych.

## Brama 5 — czy nadzor w ogole patrzy

Trzy udokumentowane przypadki, w ktorych nadzor swiecil na zielono, nie
mierzac tego, co mial mierzyc:

| przypadek | co raportowal | co naprawde robil |
| --- | --- | --- |
| proba pozarowa | "ZALICZONA" w 8 przebiegach | nie zmierzyla ogniwa 3 ani razu; raport drukowal werdykt bezwarunkowo |
| kanarek sciezki | zielony | swiecil zielono takze przy literowce w sciezce |
| czujka zywotnosci | wynik koncowy, `zgodny` | rzucala 16 wyjatkow na przebieg i konczyla raportem |

Trzeci przypadek to znalezisko z 2026-09-24 i jest juz naprawiony — po
wdrozeniu `sha-c758da2` czujka ma zero tracebackow. Ale wazniejsze jest to, co
go laczy z dwoma poprzednimi.

- [ ] Przejrzec pozostale sciezki nadzoru pod katem tego samego ksztaltu: czy
      werdykt moze sie wypisac, gdy pomiaru nie bylo
- [ ] Kazdy detektor sprawdzic wstrzyknieciem znanego zlego przypadku, zanim
      uzna sie jego cisze za dowod
- [ ] Testy krytycznych modulow uruchamiac na czystym checkoucie, nie na
      drzewie roboczym — osiem modulow padalo od tygodnia i zaden przebieg tego
      nie pokazal

## Wzorzec i kolejnosc

Wiekszosc pozycji na tej liscie ma jeden ksztalt: COS WYGLADA NA SPRAWDZONE,
BO KIEDYS BYLO. Ta sama funkcja w szesciu kopiach poprawiona w pieciu. Plik
testowy, ktory nigdy nie wszedl do lancucha uruchomien. Test, ktorego opis
mowil o jednym, a wywolanie robilo drugie. Zalozenie o braku dzwigni wpisane w
uzasadnienie limitu zamiast w jego warunek. Komentarz broniacy rozumowania,
ktore wygaslo. Karta kosztow oglaszajaca rachunek zamknietym przy nieznanym
skladniku.

Dotyczy to takze narzedzi uzytych w tym audycie — detektory "tylko do odczytu"
musialy zostac sprawdzone wstrzyknieciem zlego przypadku, zanim ich cisza
cokolwiek znaczyla, a jeden z nich zapalal sie na zdaniu w komentarzu
tlumaczacym ryzyko, czyli karal za opisanie problemu.

**To jest ryzyko numer jeden przed live, wieksze niz ktorykolwiek pojedynczy
blad z tej listy.**

### Trzy rzeczy w tej kolejnosci

1. ODCZYT ARKUSZA Z HOSTA UNIJNEGO. Jeden pomiar rozstrzyga, czy przewaga
   istnieje. Wszystko inne jest przygotowaniem do decyzji, ktorej ten pomiar
   moze nie zostawic do podjecia.
2. DECYZJA O KROTKIEJ STRONIE I O GIELDZIE. 75% sygnalow jest dzis nieuzyteczne,
   a perpetualow na naszym koncie nie ma. To decyzja wlasciciela, nie zadanie
   inzynierskie — i blokuje wycene tego, co odrzucamy.
3. DOMKNIECIE UZGODNIENIA SALD. Dopoki host demo nie wystawia sald, nie wiemy,
   czy ksiegi sie zgadzaja. Przed prawdziwymi pieniedzmi to warunek wejscia,
   nie usprawnienie.

Horyzont roczny jest tu zaleta, nie ograniczeniem: wiekszosc tych pozycji to
pomiary, ktore musza sie nazbierac — poslizg wymaga nowych wypelnien, a tych
nie da sie przyspieszyc.
