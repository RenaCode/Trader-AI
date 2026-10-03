# Trader-AI przed go-live

Stan na 2026-09-24. Dokument zywy — odpowiednik strony
https://claude.ai/code/artifact/2e0282c6-4ad2-42cf-b864-3ead3e76901a

Horyzont: rok. Decyzja wlasciciela z 2026-09-24: "lepiej zeby bylo dobrze niz szybko".

## Stan na dzis

Produkcja stoi na `sha-24443ed`, wszystkie pody `1/1`, drzewo czyste.
Pelny zestaw testow: 1912 przeszlo, 25 pominietych, 0 bledow.

### Co doszlo 2026-09-24 po poludniu

| co | stan |
| --- | --- |
| **Petla decyzyjna equity** (`workers.engine-equity`) | chodzi, `--bar 1h --kind stock --limit --egzekucja gielda --mode demo --wymagaj-modelu` |
| Bramka sesji (`services/engine/sesja.py`) | stan z `/v2/clock` Alpaki; „nie wiem" NIE jest otwarciem i konczy cykl bledem |
| **Dziennik jakosciowy** (`services/engine/postmortem.py`) | CronJob `50 7 * * *` Europe/Warsaw (wspolna `strefaCzasowa`), wlasna waska rola `trader_postmortem` (0054) |
| **Atrybucja newsow** (`decision_contribution kind='news'`) | zapisywana; jedna stala SQL wspolna z cecha `news_sent` |
| **Pelne CD** | push -> CI -> obraz w ghcr -> `update-git` podbija tag -> ArgoCD; `scripts/build-on-vps.sh` usuniety |
| **Plik blokady** (`services/api/constraints.txt`) | 42 pakiety przypiete ze zrzutu produkcji; `-c` wpiete w obraz |

EQUITY JESZCZE NIE HANDLUJE, I TO JEST ZAMIERZONE. `--wymagaj-modelu` wstrzymuje
zlecenia do czasu, az trener wystawi aktywna wersje `equity_signal_weighting_1h`
(cron 04:40 UTC, tor dodany w `trainerCron.equity`). Przy medianie OOS Sharpe
ponizej zera ten model moze nigdy nie przejsc walidacji — i wtedy petla bedzie
POPRAWNIE odmawiac handlu bez konca. To pomiar do przyjecia, nie usterka do naprawienia.

### Trzy wady znalezione tego dnia przez wlasny nadzor

| co | objaw | przyczyna |
| --- | --- | --- |
| `main` niespojny | 8 modulow testowych nie zbiera sie na czystym checkoucie | `ab71f7d` dodalo import `MINUT_TIMEFRAME` do `dozor.py`, ale mapa nigdy nie wyszla z czyjegos drzewa roboczego |
| `features-equity` w CrashLoopie | `unrecognized arguments: --okno-dni 7` | wpis w charcie uzywal flagi istniejacej tylko w niezacommitowanym kodzie; ArgoCD wdraza szablony natychmiast, bez podbicia tagu |
| **bezpiecznik equity bez ani jednego pomiaru** | `--co ryzyko`: „ANI JEDNEGO zapisanego pomiaru limitow konta w calej historii" | bramka sesji stala PRZED `RiskKernel`, wiec `check_account()` nie wykonal sie ani razu; podloga kapitalu nie byla liczona poza sesja, a pozycje zostaja otwarte przez noc i weekend |

Trzecia zostala zlapana przez czujke przy PIERWSZYM cyklu silnika equity
i naprawiona tego samego dnia (`24443ed`): bramki stoja teraz za pomiarem, bo
pomiar nie jest handlem.

Dwie pierwsze dzialaly po cichu przez kilka dni: **czujka zywotnosci rzucala
16 wyjatkow na przebieg i mimo to konczyla raportem**. Kazdy przebieg testow
przechodzil, bo kazdy szedl po drzewie roboczym, ktore akurat trzymalo
brakujacy plik.

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

## Brama -1 — czy ktokolwiek uslyszy alarm

**ZMIERZONE 2026-09-24:** `notify.delivery` ma 89 dostarczen i WSZYSTKIE rodzaju
`inapp`, przy 39 powiadomieniach `critical` w ostatniej dobie. Kanal wychodzacy
(`whatsapp`, `webhook`) nie byl skonfigurowany ANI RAZU w calej historii tabeli.

Znaczy to, ze `killswitch.triggered`, `engine.down` i `backup.missing` dotra do
czlowieka dopiero wtedy, gdy ten sam z siebie otworzy dashboard. O trzeciej
w nocy nie otworzy.

Ta brama stoi PRZED brama 0, bo dotyczy warunku wczesniejszego niz przewaga:
caly nadzor opisany w tym dokumencie jest wart tyle, ile droga, ktora jego
wynik pokonuje do odbiorcy. Bez niej pozostale czujki mierza poprawnie i mowia
do pustego pokoju.

- [ ] Skonfigurowac kanal `whatsapp` albo `webhook` w Ustawieniach i wlaczyc go
      dla zdarzen `always_on` o wadze `critical`
- [x] Czujka `dozor.py --co kanaly` (w `zywotnosc-cron`, co 10 min) zglasza brak
      takiego kanalu KODEM WYJSCIA, a nie powiadomieniem — bo powiadomienie
      szloby kanalem, ktorego brak wlasnie opisuje

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

- [x] **Zmierzyc spread na bybit.eu, nie na ksiedze globalnej** — ZROBIONE
      2026-09-24, ale wynik NIE jest jeszcze rozstrzygnieciem; patrz nizej.

### Spread na ksiedze unijnej — pierwszy odczyt (2026-09-24)

PRZYCZYNA BYLA W KODZIE, NIE W BRAKU NARZEDZIA. `BybitAdapter` bral arkusz
z `PUBLIC_HOST` (`api.bybit.com`) razem ze swiecami, bo "to on ma ksiege, na
ktorej model byl trenowany". Dla swiec jest to sluszne; dla arkusza bylo
odwrotne — jego jedyni odbiorcy to cecha `book_spread_bps` (ktorej nie czyta
zaden sygnal) i skladnik "spread" w `karta_rundy`, czyli RACHUNEK KOSZTU.
Od tej daty arkusz idzie z `self._host`, czyli z `ref.venue` — tej samej gieldy,
na ktora ida zlecenia, i automatycznie za trybem (demo/live).

Odczyt porownawczy, 12 probek w odstepie 2 s, mediana na symbol:

| symbol | ksiega globalna | ksiega unijna | krotnosc |
| --- | --- | --- | --- |
| BTCUSDC | 0,012 pb | 0,395 pb | 33,0x |
| ETHUSDC | 0,038 pb | 0,586 pb | 15,5x |
| SOLUSDC | 0,882 pb | 2,204 pb | 2,5x |
| DOGEUSDC | 8,643 pb | 18,892 pb | 2,2x |
| XRPUSDC | 2,378 pb | 4,419 pb | 1,9x |
| SUIUSDC | 2,097 pb | 2,621 pb | 1,3x |
| ADAUSDC | 8,478 pb | 4,238 pb | 0,5x |
| NEARUSDC | 79,954 pb | 20,438 pb | 0,3x |
| ARBUSDC | 5,413 pb | 0,471 pb | 0,1x |
| LINKUSDC | 26,067 pb | 2,851 pb | 0,1x |

**KROTNOSC MYLI I NIE JEST TU WLASCIWA MIARA.** Mediana krotnosci to 1,6x, ale
BTC ma 33x przy spreadzie 0,395 pb — czyli mnoznik ogromny na wielkosci, ktora
i tak jest bliska zeru. Znaczenie ma wartosc BEZWZGLEDNA wobec progu 13,76 pb.

Co z tego wynika, ostroznie:

  * mediana spreadu unijnego to **2,736 pb** na runde — przy przewadze
    13,76 pb zostaje okolo 11 pb zapasu, wiec obawa "5–17x szerzej, przewaga
    znika" NIE POTWIERDZILA SIE jako regula;
  * ale **DOGEUSDC (18,9 pb) i NEARUSDC (20,4 pb) same przekraczaja cala
    przewage** — na tych dwoch instrumentach strategia jest ujemna z samego
    spreadu, niezaleznie od sygnalu;
  * ksiega unijna bywa **wezsza** od globalnej (ARB, LINK, NEAR, ADA), co samo
    w sobie podwaza zalozenie "mniej plynny rynek = szerszy spread" i kaze
    podejrzewac, ze odczyty globalne dla malo plynnych par byly zaszumione.

CZEGO TEN ODCZYT NIE ROZSTRZYGA, i dlatego brama zostaje otwarta:

  * to 12 probek z JEDNEJ chwili, nie rozklad w czasie — nie ma p90 ani
    zachowania w godzinach, w ktorych naprawde skladamy zlecenia;
  * spread liczony przy pierwszym poziomie (`limit=1`) ignoruje GLEBOKOSC:
    przy naszej wielkosci zlecenia mozemy nie trafic w dotkniecie;
  * pomiar zrobiony z laptopa, nie z wezla produkcyjnego.

**Wlasciwy pomiar zbiera sie teraz sam**: `market_worker` zapisuje
`market.book_snapshot` z ksiegi unijnej, wiec za kilka dni `book_spread_bps`
da rozklad, mediane i p90 na tej ksiedze, na ktorej handlujemy — i dopiero
to domyka te brame. Wartosci sprzed 2026-09-24 pochodza z ksiegi globalnej
i nie wolno ich z tymi mieszac.

### Spread liczony Z BAZY, per silnik (2026-09-27)

Stala `trainerCron.spreadBps: 5.85` znikla z chartu. Trener liczy spread sam,
`services/engine/spread.py`, na TYM SAMYM uniwersum (`active AND tradeable`)
i w TYM SAMYM oknie (`--days`), ktore trenuje. Klucz zostaje w charcie jako
ZAKOMENTOWANE nadpisanie awaryjne — do policzenia wariantu, nie do codziennego
przebiegu. Przepisu na 5,85 nie dalo sie odtworzyc: liczba nie stoi w zadnym
zapytaniu ani w zadnym tescie, a najblizsza rekonstrukcja (wlasna ksiega,
24–25.09, srednia z median per instrument) daje 5,46 pb dla 1h i 5,69 pb dla 4h.

**Czym jest `book_spread_bps`, z dowodu w kodzie.** `OrderBook.spread` to
`asks[0].price - bids[0].price`, czyli PELNY spread; `market_worker` zapisuje go
bez przeliczania; `features.compute_orderbook` agreguje
`avg(spread / NULLIF(mid, 0)) * 10000`. Runda placi pol spreadu na wejsciu i pol
na wyjsciu, wiec „polowa na noge razy dwie nogi" z `karta_rundy` jest JUZ
W SRODKU tej cechy — mnozenie przez dwa zawyzaloby doplate dwukrotnie.

**Statystyka: SREDNIA, i to srednia ze srednich per instrument.** Koszt placi
kazda transakcja, nie kazda swieca, wiec szukana wielkosc to wartosc oczekiwana,
a jej estymatorem jest srednia. Zmierzone 2026-09-27, uniwersum decyzyjne
crypto, swieca 4h, 1 370 obserwacji, 10 par USDC:

| statystyka | wartosc |
| --- | --- |
| mediana po swiecach | 4,462 pb |
| q1 / q3 | 1,037 / 9,592 pb |
| **srednia ze srednich per instrument (uzywana)** | **8,618 pb** |
| p90 | 22,98 pb |
| p99 | 67,38 pb |
| skrajne instrumenty | BTCUSDC 0,084 pb, NEARUSDC 37,385 pb |

Mediana zanizylaby runde o czynnik 1,93 — w strone korzystna dla strategii.
Usredniamy najpierw po instrumencie, bo rozrzut MIEDZY instrumentami jest
o trzy rzedy wielkosci wiekszy niz w czasie (0,013 pb wobec 34,21 pb median),
wiec instrument o krotszym pokryciu wazylby proporcjonalnie do kompletnosci
swoich danych, a nie do tego, ile nim handlujemy.

**Czego ta liczba nadal nie jest.** Sredniej wazonej TRANSAKCJAMI. Kierunek
tego bledu jest zmierzony: 15 pozycji USDC z `exec.position` daje sredni spread
wazony faktycznym skladem **11,22 pb** wobec 8,618 pb przy wadze rownomiernej
(strategia weszla m.in. w ARB i NEAR — najszersze pary uniwersum). Przy n = 15
nie jest to estymator, ale dowodzi, ze 8,618 pb jest DOLNYM oszacowaniem.

**Ksiega nadal obca, i to ustala POMIAR, nie wpis w charcie.** Szereg dla par
`tradeable` zaczyna sie 2026-09-04, a wlasna ksiega od 2026-09-24, wiec wieksza
czesc okna pochodzi z hosta globalnego. `spread.zmierz_spread` sprawdza oba
warunki (data okna i `tradeable` wszystkich instrumentow) i sam ustawia flage —
karta ZOSTAJE NIEPELNA, a raport podaje krotnosc spreadu, przy ktorej przewaga
schodzi do zera.

**Equity: dane sa, pomiaru nie ma.** `book_spread_bps` istnieje dla 20 spolek
uniwersum (560 swiec 1h, 22–25.09), ale mediana po spolkach to 41 pb, a dla JNJ
479 pb (4,8% ceny). Zrodlem jest feed IEX — jedna gielda z kilkunastu — a
zlecenie Alpaca kieruje na caly rynek. Dowod, ze to artefakt, nie wymaga wiedzy
o cenniku: w 257 z 560 przypadkow kwotowanie jest SZERSZE niz caly zakres
high-low tej samej swiecy, mediana ilorazu 0,843; ta sama liczba dla crypto to
0,033. `zmierz_spread` odrzuca taki pomiar bramka spojnosci (prog 0,50), wiec
karta equity nadal konczy sie zdaniem „razem CO NAJMNIEJ ... — bez
niewypelnienie, spread, oplaty regulacyjne". Dzis odrzuca go jeszcze wczesniej:
28 swiec na instrument nie siega progu 30, wiec powodem jest POKRYCIE, a nie
spojnosc. Oba powody sa fail-closed. Zamknie to feed SIP albo kolumna oplat
z `exec.fill` po pierwszych sprzedazach na koncie paper.

**Wplyw na werdykt.** Sharpe/transakcje spada z +0,0952 (bez spreadu) przez
+0,0823 (stara stala 5,85 pb) do **+0,0762** przy zmierzonych 8,618 pb, a DSR
przy N = 57 z 0,9001 przez 0,7874 do **0,7149**. Bramka 0,95 nie byla
przechodzona wczesniej i nie jest teraz — zmiana nie przestawia werdyktu,
tylko przestaje opierac go na liczbie, ktorej nie dalo sie odtworzyc.
- [ ] Zmierzyc poslizg — dzis ZERO obserwacji, nie piec: z 49 wypelnien 31 ma
      cene zlecenia, a `limit_price != fill_price` nie zachodzi ani razu. Trzy
      wejscia maja kolumne zniszczona przez `zapisz_wejscie` sprzed poprawki;
      dwa wyjscia maja ja nietknieta i mimo to nie sa pomiarem — zlecenie z
      limitem wypelnia sie po cenie limitu albo lepiej, wiec pomiar wobec
      wlasnego limitu wykryje patologie, nie koszt
- [x] **Uruchomic `services/engine/wartosc_krotkiej.py` — URUCHOMIONE
      2026-09-27 na bazie produkcyjnej: KROTKA STRONA NIE MA PRZEWAGI.**
      Tor 4h, 1400 dni, finansowanie JUZ naliczane (od 2026-09-24, czyli luka
      z pomiaru 04.09 domknieta). Spot_eu +10,28% zwrotu i Sharpe +0,1036 przy
      1794 transakcjach; perp +1,85% i +0,0198 przy 1433 — odblokowanie
      krotkich to **-8,43 pp zwrotu i -0,0838 Sharpe'a**. Sama noga krotka:
      n=484, sredni zwrot -0,6036%, przedzial 95% [-1,3537%, +0,1073%], czyli
      OBEJMUJE ZERO. Niewrazliwe na zalozenie o posliznieciu 0-10 pb.
      NAJWAZNIEJSZE: -8,43 pp to w wiekszosci WEJSCIA DLUGIE, KTORYCH NIE BYLO
      (1794 -> 949 transakcji dlugich, "zablokowane przez ryzyko" 0 -> 4963),
      bo krotkie zjadaja ten sam budzet ekspozycji. 143 odrzucone sygnaly
      krotkie (76% z 188 ponad progiem) NIE SA wiec strata okazji. Szczegoly
      w `docs/03-wynik-walk-forward.md`, sekcja "Luka DOMKNIETA".
- [x] **Decyzja o perpetualach — ROZSTRZYGNIETA POMIAREM, nie preferencja:
      zostajemy na spocie.** Na bybit.eu perpetualow nie ma (spot 127,
      linear 0, inverse 0), a pomiar wyzej mowi, ze konto globalne po nie nie
      jest warte zakladania: krotka strona nie doklada przewagi i odbiera
      miejsce tej, ktora ja ma. Do decyzji wlasciciela zostaje wylacznie
      sytuacja, w ktorej limit ekspozycji rosnie tak, ze krotkie przestaja
      wypychac dlugie - i wtedy pomiar trzeba powtorzyc, bo caly ten wniosek
      jest przy `max_total_exposure` = 15%
- [x] **Rozstrzygnac los equity — ROZSTRZYGNIETE 2026-09-24: PRZEWAGI NIE MA.**
      Pelny walk-forward na 59 oknach, uniwersum 20 spolek, cennik Alpaki.
      Szczegoly nizej.

### Los equity — pomiar zamiast szacunku (2026-09-24)

Wczesniejsze liczby w tym punkcie (23,1% / 32,7% / 42,3% okien dodatnich) byly
szacunkiem przy CENNIKU BYBITA, bo innego wtedy nie bylo. Teraz jest pomiar
wlasnym cennikiem, na pelnej historii:

```
Sharpe/transakcje ze zlaczonej proby (2303 transakcje):  -0,0782
srednia po 59 oknach:                                    -0,1044
mediana okna:                                            -0,0942   [-0,2617; +0,0571]
okien ujemnych:                                          42/59 (71,2%)
profit factor zbiorczy:                                  0,7194
sredni zwrot z transakcji:                               -0,2237%, trafnych 43,6%
Stan: degraded — dodatnie 17/59 okien (29%), przedzial ufnosci 19%-41%
```

**KOSZT PROGOWY: -22,37 pb na runde, czyli UJEMNY.** To jest tu jedyna liczba,
ktora naprawde rozstrzyga. Prowizja Alpaki wynosi ZERO, a strategia i tak traci
22 pb na rundzie - equity nie przegrywa wiec z kosztami, tylko samo ze soba.
Skladniki nienaliczone (spread, niewypelnienie, oplaty regulacyjne) moga to
wylacznie poglebic. Obnizanie kosztu nie ma czego uratowac.

DECYZJA: `tradeable` dla equity ZOSTAJE `false`. Migracja 0047 ustawila te flage
swiadomie i postawila warunek: "wlaczenie do uniwersum decyzyjnego to osobna,
swiadoma migracja PO pierwszym walk-forwardzie". Walk-forward sie odbyl,
odpowiedz brzmi "nie", wiec migracji nie ma. Petla `engine-equity` chodzi dalej
i odmawia handlu przez `--wymagaj-modelu` - od teraz jest to odmowa oparta na
POMIARZE, a nie na braku modelu.

CO ZOSTAJE WLACZONE I DLACZEGO: potok danych equity (swiece, cechy, kapital,
puls). Kosztuje tyle co nic, a 2,5 roku historii godzinowej z policzonymi
cechami jest warunkiem KAZDEJ przyszlej hipotezy o tym rynku. Wylaczenie potoku
oznaczaloby, ze nastepne pytanie o equity znowu zaczyna sie od czekania.

### Trzy wady odslonete przy okazji tego pomiaru

| co | objaw | dlaczego bylo niewidoczne |
| --- | --- | --- |
| trener meldowal sukces na pustym uniwersum | Job ZIELONY, w panelu nie przybywa wersji modelu | `train()` robilo `return`, `main()` konczylo `return 0`; przyczyne widac bylo tylko w logu zadania, ktore meldowalo sukces |
| `tradeable = false` na wszystkich 21 spolkach | uniwersum decyzyjne equity = 0 | ustawione swiadomie w 0047; brakowalo kroku, ktory tamta migracja zapowiedziala |
| cechy equity liczone za 7 dni, nie za historie | 35-41 punktow panelu przy wymaganych 500 | swiec bylo 94 625 od 2024-04; `features-equity` ma okno 7 dni, co jest poprawne w biegu, ale nie po dolozeniu instrumentow |

Pierwsza jest naprawiona (`SystemExit(2)`, ten sam kod co przy zbyt krotkim
panelu - oba znacza "nie policzono niczego"). Trzecia domknieta jednorazowym
przeliczeniem wstecz: 723 147 wartosci cech, 0 bledow.

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
- [x] korekta o podzialy akcji u Alpaki — WYKONANA 2026-10-03: znacznik
      `swiece_skorygowane_o_podzialy` na 20/20 instrumentach equity (05:49 UTC,
      audyt 3). Opis operacji zostaje ponizej na wypadek kolejnego podzialu.
      Pierwotnie: MECHANIZM GOTOWY, OPERACJA DO
      WYKONANIA. Adapter pobiera od 2026-09-27 `adjustment=split`
      (`services/exchanges/alpaca.py:KOREKTA_SWIEC`; `all` odrzucone — wliczaloby
      dywidendy, ktorych zaden rachunek silnika nie przyjmuje). Zapisana historia
      jest nadal NIESKORYGOWANA: w `market.candle` leza trzy podzialy 10:1
      (NVDA 2024-06-10, AVGO 2024-07-15, NFLX 2025-11-17), a `ret_long` siega
      168 swiec, wiec jedno zdarzenie zatruwa okolo 670 swiec po sobie.
      Cztery kroki, W TEJ KOLEJNOSCI, z poda `trader-api` (tam SECRETS_KEY):

        1. `python services/ingest/korekta_podzialow.py --sucho`
           pomiar, nic nie zapisuje. Zestawienie z `--korekta raw` pokazuje,
           czy parametr w ogole dochodzi do dostawcy (musza wyjsc ROZNE liczby)
        2. `python services/ingest/korekta_podzialow.py --przepisz`
           przepisuje szereg 1h, jedna transakcja na instrument: albo caly
           szereg instrumentu, albo ani jeden jego wiersz. Znacznika NIE stawia
        3. `python services/ingest/features.py --bar 1h --engine equity
           --okno-dni 1400 --once`
           KROK OBOWIAZKOWY: trener czyta `feat.feature_value`, nie swiece,
           a `features-equity` chodzi z `--okno-dni 7` i okien z 2024 nie
           dotknie nigdy
        4. `python services/ingest/korekta_podzialow.py --znacznik`
           stawia znacznik, ale najpierw sprawdza, czy zapisane cechy zgadzaja
           sie ze swiecami (przelicza je w transakcji, ktora wycofuje).
           Rozjazd = odmowa

      Te cztery kroki sklada `scripts/korekta-podzialow-equity.sh` (od
      2026-10-03; `set -euo pipefail`). Bez argumentu wykonuje WYLACZNIE
      krok 1 i sprawdza jego warunki: `split` i `raw` daja rozne liczby,
      zadnego `NIEPELNE`, czynnik okolo 0,1 dla NVDA/AVGO/NFLX - inaczej
      przerywa. Kroki 2-4 dopiero z `--wykonaj`. Zaden szablon chartu go nie
      wola; dopoki znacznika nie ma, dozor po nocnym treningu zglasza tor
      equity jako niegotowy (kod 3) z tygodniowym przypomnieniem.

      Kazdy kolejny podzial uniewaznia cala zapisana historie (Alpaca koryguje
      wzgledem DZIS). Od 2026-09-27 nie jest to jednak cicha awaria:
      `backfill.py` porownuje przy kazdym przebiegu osiem ostatnich swiec
      z tym, co oddaje dostawca, i po wykryciu przeskalowania ZDEJMUJE znacznik,
      czyli przywraca bramke w `train.py`

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
- [ ] NIEAKTUALNE od migracji 0060 (stan 2026-10-03: 20 instrumentow equity
      `tradeable`, audyt 3). Pierwotnie:
      Uniwersum equity jest PUSTE Z DEFINICJI: wszystkie 23 instrumenty equity
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
