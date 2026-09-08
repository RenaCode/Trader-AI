# Architektura bezpiecznika: kto pilnuje pilnującego

Wymaganie właściciela, 2026-09-07: *cała obsługa tradingu ma być autonomiczna,
nie chcę nic klikać o trzeciej w nocy; przycisk w interfejsie zostaje do
odwołania. Żadnych skryptów po VPS-ie, wszystko w kontenerach.*

Przy projektowaniu tego wyszła wada, której szukanie nie było przedmiotem
zlecenia.

---

## Wada: bezpiecznik żyje wewnątrz tego, co ma zatrzymać

`RiskKernel.check_account()` — jedyne miejsce, które ocenia limity całego konta
i uruchamia kill-switch — jest wołane **w pętli decyzyjnej silnika**, raz na
cykl, w procesie `trader-engine`.

Konsekwencja jest jednozdaniowa: **gdy silnik przestaje działać, przestaje też
działać ocena ryzyka.**

Nie chodzi o awarię, po której proces ginie — kontener wtedy wstaje i k8s to
pokazuje. Chodzi o proces, który **żyje i nic nie robi**:

| co się dzieje | pozycje | ocena limitów | kto zauważy |
|---|---|---|---|
| pod padł | zostają otwarte | **brak** | `zywotnosc`, po ≤25 min |
| pętla zawisła na zapytaniu | zostają otwarte | **brak** | `zywotnosc`, po ≤25 min |
| wyjątek w środku cyklu | zostają otwarte | **brak** | `zywotnosc`, po ≤25 min |

We wszystkich trzech przypadkach kapitał może obsuwać się dalej, bo pozycje są
otwarte, a **nikt nie liczy obsunięcia**. Czujka żywotności to wykryje i wyśle
alert — ale alert to nie jest zatrzymanie. Kill-switch, który nie może
zadziałać, gdy jego proces stoi, chroni dokładnie w tych sytuacjach, w których
nic mu nie zagraża.

To ten sam kształt, co błąd naprawiony wcześniej: *risk.check() wołane wyłącznie
przy próbie wejścia, więc obsunięcie podczas trzymania pozycji nie było oceniane
przez nikogo*. Wtedy poprawka przeniosła ocenę na każdy cykl. Została o jeden
poziom za nisko — nadal w tym samym procesie.

## Zasada: nadzorca nie może dzielić losu z nadzorowanym

Stąd trzy warstwy, gdzie każda pilnuje niższej, a łańcuch kończy się na czymś,
co gwarantuje platforma:

```
   ┌───────────────────────┐
   │  trader-engine        │  decyzje i pozycje
   │  Deployment           │  czyta kill-switch, NIE zwalnia go
   └──────────┬────────────┘
              │ risk.engine_heartbeat
   ┌──────────▼────────────┐
   │  trader-dozorca       │  OSOBNY POD, pętla 60 s
   │  Deployment           │  • ocenia limity konta niezależnie
   │                       │  • wyzwala kill-switch
   │                       │  • wznawia — wyłącznie na dowodzie
   │                       │  • bije własny heartbeat
   └──────────┬────────────┘
              │ risk.dozorca_heartbeat
   ┌──────────▼────────────┐
   │  trader-zywotnosc     │  CronJob co 10 min
   │  CronJob              │  pilnuje OBU heartbeatów
   └───────────────────────┘
              ▲
              └── harmonogram gwarantuje k8s, nie nasz kod
```

**Dlaczego dozorca to Deployment, a nie CronJob.** Najmniejszy krok CronJoba to
minuta, a realnie 10 minut, żeby nie zasypać klastra podami. Limit straty
dziennej 2% oceniany co 10 minut ma dziesięciominutowe okno ślepoty. Pętla
w Deploymencie chodzi co 60 s i kosztuje jeden mały pod.

**Dlaczego czujka żywotności zostaje CronJobem.** Ktoś musi pilnować dozorcy,
a łańcuch nie może się zapętlić. CronJob jest ostatnim ogniwem, bo jego
uruchomienie gwarantuje kubelet — nie nasz kod.

**Dlaczego silnik NADAL ocenia limity.** To nie jest duplikat, tylko obrona
w głąb. Silnik pyta „czy wolno mi teraz wejść" i musi znać odpowiedź w chwili
decyzji. Dozorca pyta „czy konto jest w stanie, który wymaga zatrzymania"
i pyta o to niezależnie od tego, czy silnik cokolwiek chce. Podwójne wyzwolenie
jest niemożliwe: `evaluate_account` przy aktywnym kill-switchu zwraca `block`
bez żądania kolejnego.

## Wznowienie: autonomia z granicą

„Kill-switch, który wygasa sam" jest sprzecznością — automatyczne wznowienie po
obsunięciu to najprostsza droga do powtórzenia tej samej straty. Ale kill-switch
wymagający człowieka o trzeciej w nocy nie jest autonomią.

Wyjście nie polega na wyborze jednej ze stron, tylko na zauważeniu, że **trzy
limity krytyczne znaczą trzy różne rzeczy**, a system traktował je identycznie:

| limit | co znaczy | reguła wznowienia |
|---|---|---|
| `max_daily_loss` | limit **dzienny** | `po_ustaniu` — mija razem z dobą, z definicji |
| `max_drawdown` | kondycja strategii | `po_odbudowie` — dopiero gdy kapitał wrócił |
| `min_equity` | granica kapitału właściciela | **`reczne`** — tego nie zwalnia automat |

Ostatni wiersz jest decyzją, nie zaniedbaniem. Przekroczenie progu kapitału
znaczy, że system złamał jedyną obietnicę, która była warunkiem wpłaty
(`docs/05`). Wznowienie tego automatem czyniłoby próg ozdobą.

Trzy zabezpieczenia odróżniają to od zwykłego poluzowania limitu:

1. **Histereza** — wznowienie przy progu ciaśniejszym niż zadziałanie (połowa
   limitu). System nie oscyluje wokół granicy.
2. **Karencja** — minimum 60 minut od zadziałania. Nie jest to warunek
   wznowienia, tylko warunek jego *rozważania*: sam upływ czasu niczego nie
   dowodzi.
3. **Licznik** — po `max_wznowien` (domyślnie 3) w ciągu doby limit przechodzi
   w tryb ręczny **sam z siebie**. To jest najważniejsze z trzech: system wraca
   do handlu, ale nie w kółko. Powtarzające się zatrzymania znaczą, że warunek
   wznowienia jest spełniany, a problem trwa — i wtedy człowiek jest naprawdę
   potrzebny.

Każde automatyczne zwolnienie idzie z `released_auto = true` i powiadomieniem.
System, który sam siebie zatrzymuje i sam wznawia w ciszy, jest nieodróżnialny
od systemu, który nie robi ani jednego, ani drugiego.

## Próba pożarowa jako zadanie, nie jako plik na dysku

Skrypt `.sh` leżący na serwerze rozjeżdża się z kodem, który testuje: obraz
idzie przez wdrożenie, plik nie idzie nigdzie. Po miesiącu nie wiadomo, czy
sprawdza wersję, która działa. Do tego próba uruchamiana ręcznie zostanie
uruchomiona raz.

`services/engine/proba_killswitcha.py` jedzie w tym samym obrazie co silnik
i biegnie jako CronJob raz w tygodniu. Podnosi próg kapitału powyżej bieżącego
stanu, czeka aż **silnik sam** wykryje przekroczenie, i sprawdza cztery ogniwa:
żądanie → wiersz w bazie → wstrzymanie wejść → powiadomienie. Próg wraca
w bloku `finally`, więc także po błędzie i po przerwaniu.

Próba używa progu kapitału, bo to jedyny limit krytyczny z regułą `reczne` —
dozorca go nie ruszy i nie zwolni zdarzenia w połowie pomiaru.

## Czego ta architektura nadal nie robi

- **Dozorca nie zamyka pozycji.** Zatrzymuje wejścia; wyjścia zostają w gestii
  silnika. Gdy silnik jest martwy, pozycje zostają otwarte i żaden z tych
  procesów ich nie zamknie. Awaryjne domknięcie z poziomu dozorcy to osobna
  decyzja — wymaga kluczy giełdy w kolejnym procesie, co poszerza powierzchnię
  ataku, więc nie jest oczywistą wygraną.
- **~~Uprawnienia nie są rozdzielone.~~** Rozdzielone od migracji 0034 — patrz
  niżej.
- **Jedna replika.** Dozorca ma `replicas: 1` i `strategy: Recreate`. Dwie
  repliki oceniałyby ten sam stan i mogłyby zwolnić to samo zdarzenie
  dwukrotnie. Cena: podczas wdrożenia jest kilkusekundowa przerwa w nadzorze.

## Kto czym może zwolnić kill-switcha (migracja 0034)

Do 0032 wszystkie procesy łączyły się do bazy rolą `trader_app`. Zdanie
„nadzorca nie może dzielić losu z nadzorowanym", na którym stoi cała ta
architektura, było więc konwencją w komentarzu — nadzorowany miał poświadczenia
nadzorcy. Teraz egzekwuje je `GRANT`:

| rola | `risk.killswitch_event` | `risk.limit_def` | handluje | klucze giełdy |
|---|---|---|---|---|
| `trader_silnik` | `SELECT`, `INSERT` | `SELECT` | tak | tak |
| `trader_dozorca` | `SELECT`, `INSERT`, `UPDATE` **tylko** `released_at`, `released_auto`, `release_note` | `SELECT` | **nie** | **nie** |
| `trader_czujka` | `SELECT` | `SELECT` | nie | tak (uzgodnienie) |
| `trader_proba` | `SELECT`, `UPDATE` trzech kolumn zwolnienia | `UPDATE` **tylko** `value`, `updated_at` | nie | nie |
| `trader_app` | `SELECT`, `UPDATE` `released_at`, `released_by`, `release_note` | `SELECT` | nie | tak |

Trzy rzeczy, które ta tabela mówi, a których nie mówi rysunek wyżej:

1. **Silnik nie zwolni zatrzymania, które go dotyczy.** Próba kończy się
   `InsufficientPrivilege`, czyli awarią widoczną w `kubectl get pods`, a nie
   cichym powodzeniem.
2. **Silnik nie podniesie sobie progu.** Zmiana limitu wyłącza bezpiecznik
   skuteczniej niż zwolnienie zdarzenia i, w odróżnieniu od niego, nie zostawia
   wiersza w dzienniku ani nie wysyła powiadomienia.
3. **Dozorca nie handluje.** Brak zapisu na `exec.*` znaczy, że błąd
   w nadzorcy nie może zamienić się w transakcję — mechanizm powołany do
   ograniczania ryzyka nie stanie się jego źródłem.

`released_auto` jest poza nadaniem dla API, a `released_by` poza nadaniem dla
dozorcy: dzięki temu pytanie „czy system wrócił do handlu sam, czy wrócił go
człowiek" jest rozstrzygalne z samej bazy, niezależnie od tego, co zapisał kod.
