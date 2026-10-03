# Trader-AI

Autonomiczny system tradingowy z własnym dashboardem, uruchamiany na VPS RenaCode.
**Dwa niezależne silniki** — krypto (Bybit EU) i giełda tradycyjna (Interactive Brokers) —
z osobnymi modelami, osobnym ryzykiem i osobnymi zakładkami w UI.

> **Faza demo.** System handluje wyłącznie na kontach demonstracyjnych. Przejście na
> live wymaga spełnienia kryteriów opisanych w `docs/00-research.md` §9.2 i osobnego,
> jawnego potwierdzenia w interfejsie. Projekt nie zakłada rentowności — sześć miesięcy
> demo to test tej hipotezy, a wynik negatywny jest realnym rezultatem.

## Dokumentacja

| Dokument | Zawartość |
|---|---|
| [`docs/00-research.md`](docs/00-research.md) | Wybór giełd, architektura, warstwa uczenia, ryzyko, plan wdrożenia |
| [`docs/01-data-architecture.md`](docs/01-data-architecture.md) | Wybór bazy, bitemporalność, feature store, rejestr modeli, retencja |
| [`docs/05-ochrona-kapitalu.md`](docs/05-ochrona-kapitalu.md) | **Czy rachunek może wyjść na minus** — go/no-go przed wpłatą realnych środków |
| [`docs/06-architektura-bezpiecznika.md`](docs/06-architektura-bezpiecznika.md) | **Kto pilnuje pilnującego** — dlaczego dozorca ryzyka jest osobnym podem |
| [`docs/07-czego-brakuje.md`](docs/07-czego-brakuje.md) | **Przegląd luk** wobec działających botów — co sprawdzone w kodzie, nie w dokumentacji |

## Stan projektu

- [x] Research i architektura
- [x] Schemat bazy (11 schematów, 18 migracji, hypertables + pgvector)
- [x] API odczytu (FastAPI) i dashboard (React + Vite)
- [x] Chart Helm, obrazy Docker, CI
- [x] Wdrożenie na `trader.renacode.com` (TLS, Traefik, ArgoCD)
- [x] Uwierzytelnianie: hasło Argon2id + TOTP + kody zapasowe, sesje serwerowe, CSRF
- [x] Test dymny całej aplikacji w CI
- [x] Szyfrowane klucze API giełd w UI
- [x] Adapter Bybit + 4 lata historii: 24 instrumenty, 810 tys. świec godzinowych,
      39 tys. stawek finansowania, 2,1 mln wartości cech
- [x] Silnik cech, sygnałów i risk kernel — pętla decyzyjna na realnych danych
- [x] Trener walk-forward z licznikiem hipotez; horyzont 4 h wybrany pomiarem
      stosunku kosztu do amplitudy świecy (patrz `docs/03`)
- [x] Symulacja **portfelowa** przez ten sam risk kernel co produkcja
- [x] Rola bazy bez uprawnień superużytkownika + kontrola uprawnień
- [x] Kopie zapasowe (weryfikowane), rotacja `SECRETS_KEY`, limit wydatków na API
- [x] 3085 testów jednostkowych (reguły ryzyka, portfel, koszty, TOTP, kryptografia),
      zmierzone 03.10.2026 — polecenie i sposób sprawdzenia w sekcji [Testy](#testy)
- [x] Ustawienia, powiadomienia (w aplikacji / WhatsApp / webhook), profil konta
- [x] Prowizja w mierze, a nie tylko w krzywej kapitału — odsetek dodatnich
      okien i mediana Sharpe były liczone **przed kosztami** (patrz `docs/03`)
- [x] Konto demo Bybit **EU** zweryfikowane; cennik odczytany z giełdy
- [x] Skaner RSS — 10 kanałów sprawdzonych i zbierających (235 wpisów w pierwszym
      cyklu, zero błędów); artykuły zapisują się **bez** klucza
- [x] Porównanie z kup-i-trzymaj **przy tej samej ekspozycji** — warunek
      w starej postaci mierzył znak rynku, nie strategię (patrz `docs/03`)
- [x] Analiza wiadomości **działa na produkcji** (Gemini, ~0,5 USD/dobę)
- [x] **Ekspozycja rozłożona na czynniki** — 1,29% = 43% świec w rynku × 2,57
      pozycji × 1,16% na pozycję. Podniesienie jej zmierzone i **odrzucone**:
      kupuje obsunięcie za zwrot nieodróżnialny od szumu (patrz `docs/03`)
- [x] Limity ryzyka wyrażone **ułamkiem kapitału**, nie kwotą — te same trzy
      limity na rachunku 10 000 znaczyły 50%, 150% i 15% zamiast 5%, 15% i 1,5%
- [x] **Próg kapitału** (`min_equity`) — twarda podłoga kwotowa, osobna od
      obsunięcia liczonego od szczytu
- [ ] **Wąskie gardło to gęstość sygnału, nie wielkość pozycji.** Strategia stoi
      poza rynkiem 57% czasu i znajduje 2,5 okazji na 10 możliwych; wynik ~3%
      rocznie. Kryteria mierzą przewagę na jednostkę kapitału, nie skalę
- [x] Dwa dostawcy analizy wiadomości: Anthropic (docelowy) i Google Gemini
      (zapasowy). Wybiera się sam po tym, który klucz jest ustawiony
- [ ] Analiza wiadomości włączona na produkcji; do tego czasu `news_shock`
      jest wykluczany z kalibracji jako sygnał martwy
- [ ] Egzekucja zleceń — **zablokowana wąskim gardłem podmiotu unijnego**
      (patrz niżej), nie brakiem klucza
- [ ] Adapter IBKR + silnik equity

## Ograniczenia podmiotu unijnego

Po MiCA konto stoi na `bybit.eu` — to **inna giełda niż bybit.com**, nie inna
domena tej samej. Zmierzone 2026-09-04:

| | bybit.com | bybit.eu |
|---|---|---|
| pary spot / w tym `*USDT` | 541 / 399 | 140 / **4** |
| kontrakty wieczyste | 859 | **0** |
| prowizja spot maker / taker | 0,100% / 0,100% | 0,100% / **0,250%** |

Trzy konsekwencje, każda twarda:

1. **Zlecenia rynkowe są wykluczone.** Runda kosztuje tam 0,580% zamiast 0,280%,
   przy czym mediana Sharpe schodzi poniżej zera. Trener modeluje więc
   `--kind spot_eu --limit` (stawka maker jest identyczna jak globalnie).
2. **Strategia dwustronna jest niewykonalna.** Wymaga kontraktów wieczystych
   (`services/engine/koszty.py`), a podmiot unijny ich nie oferuje.
3. **Uniwersum wymaga przebazowania na USDC.** Płynność unijna siedzi na parach
   USDC; z 24 aktywów bazowych sensowny obrót ma tam 10, przy progu
   `MIN_INSTRUMENTS = 8`.

Dane publiczne — świece, arkusz zleceń, finansowanie — pochodzą nadal z hosta
globalnego, bo to jego księga stoi za czterema latami historii.

## Agenci

Cztery definicje w `.claude/agents/`, każda z dyscypliną wypracowaną przez ten
projekt wpisaną wprost w instrukcje:

| Agent | Rola | Czego nie robi |
|---|---|---|
| `strateg` | hipotezy z góry określonym warunkiem obalenia | nie mierzy sam |
| `trener` | walk-forward, ≥4 przesunięcia okien, tryb suchy | nie decyduje o wdrożeniu |
| `analityk` | audyt pomiaru: czy mierzymy to, co się wydaje | nie proponuje sygnałów |
| `koordynator` | kolejność, decyzja o wdrożeniu, raport | nie mierzy i nie proponuje |

Definicje żyją w repozytorium. Claude Code szuka ich w `.claude/agents/`
**katalogu sesji**, więc jeśli uruchamiasz sesję piętro wyżej:

```bash
scripts/sync-agents.sh            # kopiuje do ~/workspace/.claude/agents
scripts/sync-agents.sh --sprawdz  # wykrywa rozjazd, kod wyjścia 1
```

Sesja uruchomiona z katalogu projektu nie potrzebuje niczego. Dowiązanie
symboliczne zostało odrzucone: nie wiadomo, czy skaner za nim podąża, a
„prawdopodobnie działa" to zła podstawa dla czegoś, czego awaria objawia się
ciszą.

Kolejność nie jest dowolna: **analityk idzie PO trenerze**. Najgroźniejszy błąd
w historii projektu — prowizja liczona poza miarą, wynik zawyżony o 74% — był
widoczny dopiero w liczbach: dwa przebiegi o dwukrotnie różnym koszcie dały
identyczny wynik. Bez pomiaru nie było czego zauważyć.

## Kontrola po zmianach w silniku

```bash
# Wykonuje wszystkie zapytania pętli decyzyjnej BEZ ZAPISU i kończy kodem 1,
# gdy którekolwiek zawiedzie. Wyłapuje rozjazd między kodem a schematem bazy,
# którego przenośne testy nie widzą.
kubectl exec deploy/trader-api -c api -- python services/engine/loop.py --sprawdz

# Uprawnienia roli aplikacji: sprawdza obie strony — że rola potrafi to, czego
# potrzebuje, i że NIE potrafi tego, przed czym chroni.
kubectl exec deploy/trader-api -c api -- python db/check_privileges.py
```

Kontrola pętli działa też automatycznie: proces silnika uruchamia ją przed
wejściem w pętlę i nie startuje, gdy zawiedzie.

## Architektura w skrócie

```
web (nginx + React)  ──► api (FastAPI) ──┬──► PostgreSQL 17 + TimescaleDB + pgvector
   trader.renacode.com                    ├──► Redis
                                          └──► API modelu (Anthropic albo Gemini)
       engine-crypto ─┐
       engine-equity ─┴──► wspólny rdzeń: dane, newsy, cechy, risk kernel, audyt
```

Trzy decyzje, które niosą resztę projektu:

1. **Bitemporalność.** Każdy fakt ma `event_time` (kiedy zaszło) i `ingested_at` (kiedy
   system się dowiedział). Odczyt do treningu idzie wyłącznie przez `feat.as_of()`, która
   filtruje po obu. Bez tego model uczyłby się na newsach z przyszłości.
2. **Dziennik decyzji jest append-only** (wymuszone triggerem) i zapisywany *przed*
   poznaniem wyniku, razem ze snapshotem cech. To jednocześnie odpowiedź na „dlaczego"
   w UI i dataset wolny od leakage'u.
3. **Metryki tylko out-of-sample**, z licznikiem przetestowanych konfiguracji. Nie ma
   gdzie wpisać „wyniku backtestu", bo świetny backtest jest sygnałem przeuczenia.

## Uruchomienie lokalne

```bash
cp .env.example .env
# wygeneruj klucz szyfrowania sekretów:
python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"   # -> SECRETS_KEY

docker compose up -d              # PostgreSQL + TimescaleDB + pgvector, Redis
python -m venv .venv && .venv/bin/pip install -r services/api/requirements.txt

set -a; . ./.env; set +a
.venv/bin/python db/migrate.py            # migracje

.venv/bin/uvicorn services.api.main:app --port 8099 &
cd web && npm install && npm run dev      # http://localhost:5174
```

**Dane demonstracyjne zostały usunięte** — system pracuje wyłącznie na realnych
świecach z giełdy. `purge_demo.py`
kasuje wszystko, co wygenerował, zostawiając realne dane rynkowe.

## Procesy tła

| Proces | Co robi | Zależność |
|---|---|---|
| `market` | świece 1 m i snapshoty orderbooka, co 60 s | — |
| `history` | świece 1 h i stawki finansowania, co 20 min | — |
| `features` | cechy na obu horyzontach (`4h,1h`), co 15 min | historia |
| `engine` | pętla decyzyjna: reżim → sygnały → risk kernel → dziennik | cechy |
| `trainer` | walk-forward, raz na dobę, po nim **dozór**; **CronJob, nie worker** | cechy |

Każdy z tych procesów ma **pętlę we własnym module**, a nie w komendzie powłoki.
`sh -c "while true; do ... || true; sleep N; done"` połykał każdy niezerowy wynik:
awaria nie restartowała kontenera i nie podnosiła licznika restartów, więc pod
stał „Running" i mógł nie robić nic od dni. Pętla w module łączy się z bazą **na
każdy cykl** (jedno połączenie na cały proces po restarcie bazy zrywa się raz
i już się nie naprawia) i **kończy proces po pięciu awariach pod rząd** — wpis
w logu nie jest widocznością, restart poda jest.

Trener jest wyjątkiem i pętli nie dostaje: trwa dziesiątki minut i musi dobiec
końca niezależnie od wdrożeń, dlatego jest zadaniem cyklicznym
(`templates/trainer-cron.yaml`).
| `news` | RSS → zapis; z kluczem także filtr → analiza, co 5 min | klucz dostawcy |
| `dozorca` | **ocena limitów konta NIEZALEŻNIE od silnika**, wyzwolenie i wznowienie kill-switcha, co 60 s | — |

## Niezmiennik danych rynkowych

**Do bazy trafiają wyłącznie świece ZAMKNIĘTE, a zapis je poprawia.**

`event_time` świecy to jej **początek**, więc warunek `event_time < now`
przepuszczał świecę trwającą — o 09:01 świeca 09:00 lądowała w bazie z zakresem
jednej minuty. Ponieważ wznawianie zaczynało od najnowszej posiadanej świecy,
a zapis pomijał konflikt, taka świeca nie była już nigdy odpytywana.

Zmierzone 2026-09-06 na BTCUSDC, 55 godzin z kompletem świec minutowych:
zamknięcie zgodne z faktycznym w **9 przypadkach**, zapisany zakres high-low
równy **39%** prawdziwego. Każda cecha z nich policzona — ATR, położenie
w zakresie, reżim zmienności — dziedziczyła to zaniżenie.

Trzy reguły, które to trzymają:

| reguła | gdzie |
|---|---|
| tylko świece domknięte: `event_time + step <= now` | `backfill.py`, `market_worker.py` |
| zapis **poprawia** istniejące, gdy wartości się różnią | `batched_insert`, `insert_values` |
| zakładka trzech świec przy wznawianiu | `backfill.py` |

Razem sprawiają, że potok **leczy się sam**: błędna świeca zostaje nadpisana
przy następnym przebiegu, a cechy z niej policzone — przy następnym liczeniu.
Tryb `backfill.py --napraw` pobiera zadane okno od nowa, gdy zakładka nie sięga
wystarczająco daleko wstecz.

## Egzekucja papierowa

Silnik nie składa zleceń na giełdzie — prowadzi **własny rachunek papierowy**
po realnych cenach. Zlecenia są jawnie oznaczone (`execution: internal_paper`,
`venue_fill_id: paper-*`), a koszt liczony z `services/engine/koszty.py` tym
samym wywołaniem co w symulacji walk-forward.

**Zlecenie z limitem jest planem, nie faktem.** Decyzja na zamknięciu świecy T
zapisuje `journal.planned_order` z ceną limitu i terminem ważności do końca
świecy T+1. Na początku kolejnego cyklu plan jest rozstrzygany regułą
`portfolio._wypelnione` — tą samą, której używa symulacja:

```
kupno   wypełnia się, gdy low  ≤ limit
sprzedaż                gdy high ≥ limit
```

Niewypełnienie jest **wynikiem, nie błędem**. Przy wyjściu oznacza, że pozycja
zostaje otwarta — najdroższy przypadek w całym modelu, bo dotyczy rynku, który
ucieka przeciwko nam.

Do 2026-09-06 pętla zapisywała wypełnienie od razu, przyjmując, że limit się
wypełnił. Błąd był systematyczny w jedną stronę: zlecenia, które się nie
wypełniają, to zwykle te, przy których rynek uciekł w naszą stronę, czyli
najbardziej zyskowne.

## Dostawca analizy wiadomości

Skaner wiadomości potrzebuje modelu do dwóch rzeczy: **triażu** (czy nagłówek
w ogóle może ruszyć ceną — model tani) i **analizy** (kierunek, siła, horyzont,
nowość — model mocniejszy). Dostawcę wybiera `services/ingest/llm.py` po tym,
który klucz jest ustawiony:

| kolejność | sekret w UI / zmienna | triaż | analiza |
|---|---|---|---|
| 1 | `anthropic_api_key` / `ANTHROPIC_API_KEY` | claude-haiku-4-5 | claude-opus-5 |
| 2 | `gemini_api_key` / `GEMINI_API_KEY` | gemini-3.5-flash-lite | gemini-3.5-flash |

Anthropic wygrywa, gdy ustawione są oba — jest dostawcą docelowym, a Gemini
wszedł jako ścieżka na czas oczekiwania na klucz. Klucz z bazy (wpisany
w Ustawieniach) bije zmienną środowiskową **tego samego** dostawcy; między
dostawcami rozstrzyga kolejność z tabeli, nie źródło klucza.

Zmierzony koszt analizy przez Gemini: **około 0,54 USD na dobę** przy 128
artykułach, z czego triaż to kilka procent — reszta idzie na artykuły, które
przez triaż przeszły. Limit dobowy (`LLM_DAILY_USD`, domyślnie 5 USD) zatrzymuje
potok twardo.

> **Zmiana dostawcy w trakcie nie jest neutralna.** Oceny dwóch różnych modeli
> nie są tym samym pomiarem, a cecha `news_sent` sklejona z obu ma w środku
> uskok, którego kalibracja wagi nie zakłada. Model zapisuje się przy każdym
> wierszu (`news.article_analysis.model`), a zmiana trybu jest wykrzyczana
> w logu — ale to jest wykrywalność, nie rozwiązanie.

**Kiedy to wpłynie na model.** Nigdy wstecz. Analizy powstają od dnia wpięcia
klucza, więc `news_shock` jest martwy we wszystkich oknach historycznych. Wchodzi
do wag produkcyjnych dopiero po `MIN_OKIEN_ZYWEGO = 5` oknach, w których żyje —
przy kroku 30 dni to około pięciu miesięcy od dziś.

## Kill-switch: próba pożarowa

Kill-switch jest jedynym mechanizmem w tym systemie, którego awaria **nie
objawia się niczym**. Silnik po jego zadziałaniu wygląda dokładnie tak samo jak
silnik bez sygnału do wejścia: zero decyzji `enter`, żaden błąd w logu,
heartbeat bije dalej.

Łańcuch ma pięć ogniw i testy jednostkowe pokrywają tylko pierwsze:

| ogniwo | co się dzieje | gdzie sprawdzane |
|---|---|---|
| 1 | limit przekroczony → werdykt żąda zatrzymania | `tests/test_killswitch.py` |
| 2 | żądanie → wiersz w `risk.killswitch_event` | próba pożarowa |
| 3 | aktywny → wejścia wstrzymane, **wyjścia nadal działają** | oba |
| 4 | dozór → powiadomienie krytyczne | próba pożarowa |
| 5 | człowiek → zwolnienie z powodem i śladem w audycie | interfejs |

```bash
# Podnosi próg kapitału powyżej bieżącego stanu, czeka aż silnik SAM wykryje
# przekroczenie, sprawdza wszystkie ogniwa i przywraca próg.
ssh root@185.201.112.104 'bash -s' < scripts/proba-killswitcha.sh
```

Próg wraca przez `trap EXIT`, więc także po przerwaniu i po błędzie —
zostawiony podniesiony zatrzymałby silnik na stałe, czyli spowodowałby
dokładnie tę awarię, przed którą kill-switch ma chronić. Skrypt odmawia
startu, gdy jakiś kill-switch jest już aktywny: nie odróżniłby wtedy swojego
zdarzenia od zastanego.

**Brak powiadomienia to NIEZALICZONA próba**, nie drobiazg. Cichy kill-switch
jest gorszy niż jego brak, bo daje złudzenie kontroli.

Ogniwa 5 nie było do 2026-09-07: kolumny `released_at`, `released_by`
i `release_note` istniały od migracji 0007, ale router ryzyka miał wyłącznie
`GET`. Kill-switch działał, zatrzymywał silnik i nie dawało się go zwolnić
inaczej niż ręcznym SQL-em — a powiadomienie odsyłało do zakładki, która
pokazywała komunikat „odblokowanie wymaga ręcznego potwierdzenia" i nie dawała
czym potwierdzić.

## Dozór stanu nauki

Po każdym nocnym treningu uruchamia się `services/engine/dozor.py`. Odzywa się
**wyłącznie wtedy, gdy coś się zmieniło** — powiadomienie, które przychodzi
zawsze, przestaje cokolwiek znaczyć.

Próg jest wzięty z pomiaru, nie z przeczucia. Cztery przebiegi tego samego kodu,
różniące się przesunięciem okna o dzień, dały 52,5% → 62,5%: dobowa różnica
odsetka **nie jest zdarzeniem**. Zdarzeniem jest wyjście poza przedział ufności
poprzedniego przebiegu.

Przechodzi: zmiana statusu · odsetek poza przedziałem (w obie strony — poprawa
też bywa błędem) · ubytek okien informatywnych o ćwierć · zmiana znaku mediany
Sharpe · rosnący dryf powyżej progu · **awaria trenera**.

Ten ostatni warunek jest osobny, bo przebieg zakończony błędem nie dopisuje
wiersza do dziennika: bez niego dozór porównałby dwa stare wpisy, nie zobaczył
różnicy i zamilkł — awaria wyglądałaby jak spokój.

```bash
kubectl exec deploy/trader-api -c api -- python services/engine/dozor.py --sucho
```

## Baza danych

| Schemat | Zawartość |
|---|---|
| `ref` | Giełdy, instrumenty, kalendarz sesji, akcje korporacyjne |
| `market` | Świece (hypertable), orderbook, funding. Godzinówki pobierane wprost
z giełdy; wyższe interwały liczone w locie — agregaty ciągłe usunięte w 0016 jako
drugie źródło tej samej prawdy |
| `news` | Kanały RSS, artykuły, analiza Claude, embeddingi (pgvector + HNSW) |
| `feat` | Wersjonowane definicje cech, wartości, reżim, funkcja `as_of()` |
| `sig` | Definicje i wartości sygnałów |
| `journal` | **Append-only** dziennik decyzji, rozbicie na wkłady, zlecenia planowane |
| `exec` | Zlecenia, wypełnienia, pozycje, krzywa kapitału |
| `learn` | Rejestr modeli, walk-forward, kalibracja, dryf, licznik hipotez |
| `risk` | Limity, wykorzystanie, naruszenia, kill-switch, heartbeat |
| `app` | Użytkownicy, TOTP, WebAuthn, sesje, zaszyfrowane klucze API, audyt |

Migracje są **niemutowalne** — runner porównuje sumy kontrolne i odmawia startu, jeśli
zastosowany plik został zmieniony. Poprawki idą jako nowy plik.

## Wdrożenie

GitOps: push do `main` → GitHub Actions buduje obrazy do `ghcr.io` i podbija tagi
w `charts/trader/values.yaml` → ArgoCD na VPS synchronizuje klaster.

Sekrety **nie** trafiają do repozytorium — tworzone raz, ręcznie:

```bash
kubectl create secret generic trader-secret \
  --from-literal=POSTGRES_PASSWORD='...' \
  --from-literal=SECRETS_KEY='...' \
  --from-literal=ANTHROPIC_API_KEY='...'
```

TLS: cert-manager (`letsencrypt-prod`) + Traefik, tak jak reszta usług na VPS.
Kontener nginx serwuje czysty HTTP — certyfikat żyje wyłącznie na krawędzi.

> **Obrazy buduje CI i pobiera je klaster z ghcr.io.** Push do `main` uruchamia
> testy, build obu obrazów i — po ich powodzeniu — commit podbijający
> `api.image.tag` / `web.image.tag`, który ArgoCD synchronizuje. Żaden krok nie
> wymaga wejścia na serwer.
>
> Repozytorium jest prywatne, więc pakiety też. Klaster uwierzytelnia się
> sekretem `ghcr-pull`, który powstaje POZA repozytorium — token potrzebuje
> wyłącznie `read:packages`:
>
> ```bash
> kubectl create secret docker-registry ghcr-pull \
>   --docker-server=ghcr.io \
>   --docker-username=<login-github> \
>   --docker-password=<token-z-read:packages>
> ```
>
> Bez tego sekretu każde wdrożenie nowego commita kończy się
> `ImagePullBackOff` — obraz istnieje wtedy wyłącznie w rejestrze.

## Uwierzytelnianie

| Warstwa | Rozwiązanie |
|---|---|
| Hasło | Argon2id, 64 MiB / t=3 / p=4 (OWASP wymaga minimum 19 MiB / t=2 / p=1) |
| Drugi składnik | TOTP (RFC 6238) + 10 jednorazowych kodów zapasowych; włączenie wymaga hasła, wyłączenie hasła i kodu (TOTP lub zapasowego) |
| Sesja | 256 bitów z CSPRNG; w bazie wyłącznie SHA-256; cookie `HttpOnly` + `Secure` + `SameSite=Strict`; wygasa po 12 h albo po 30 min bezczynności |
| Session fixation | Nowa sesja przy logowaniu i po potwierdzeniu drugiego składnika |
| CSRF | Double-submit cookie na metodach zmieniających stan |
| Blokada | 15 minut po pięciu nieudanych próbach — wspólny licznik dla logowania, drugiego składnika i ponownego podania hasła (`/password`, `/totp/*`, `revoke-others`); identyczna odpowiedź i ta sama weryfikacja Argon2 (atrapa) dla nieistniejącego loginu |
| Webhook | tylko publiczne adresy po rozwiązaniu DNS, połączenie przypięte do sprawdzonego IP |
| Audyt | Każda próba logowania, zmiana hasła i operacja na 2FA w `app.audit_log` |

Konto zakładane poza repozytorium:

```bash
kubectl exec <pod-api> -- sh -c 'printf %s "<hasło>" | python scripts/create_admin.py admin --stdin'
```

## Testy

### Testy jednostkowe

```bash
pip install -r services/api/requirements.txt -r requirements-dev.txt
python -B -m pytest
```

**Potrzebne są OBA pliki zależności.** `requirements-dev.txt` zawiera wyłącznie
to, czego obraz produkcyjny nie ma (sam `pytest`); testy importują `services/*`,
a te importują `psycopg`, `fastapi`, `httpx`, `yaml`, `pyotp` i `cryptography`.
Instalacja samego pliku deweloperskiego daje 59 błędów kolekcji, a komunikat
o brakującym module nie mówi, którego pliku brakuje.

`-B` wyłącza zapis bajtkodu i nie jest ozdobą: przy sprawdzaniu, czy test
naprawdę pilnuje tego, co obiecuje (wstawienie wady i sprawdzenie, że test
pada), mutacja pliku o niezmienionym rozmiarze potrafi trafić w stary `.pyc`
i dać wynik nie z tego kodu. Dokładnie te dwa polecenia uruchamia CI
(`.github/workflows/tests.yml`).

**Żaden z tych testów nie dotyka bazy ani giełdy.** Testy wymagające bazy
z zastosowanymi migracjami pomijają się same i mówią o tym wprost — żeby je
uruchomić, podaj `TEST_DB_URL` wskazujący na **jednorazową** bazę:

```bash
TEST_DB_URL=postgresql://... python -B -m pytest tests/test_role_bazodanowe.py
```

Liczba testów podana wyżej w [Stanie projektu](#stan-projektu) jest pilnowana testem
(`tests/test_dokumentacja_testow.py`): napis, który odjedzie od pomiaru, zapala
zestaw, zamiast cicho zostać nieprawdą — tak jak stało się z „123 testy
jednostkowe" przy faktycznych 1999.

### Test dymny interfejsu

```bash
BASE_URL=https://trader.renacode.com TEST_PASSWORD=... node tests/smoke.spec.mjs
```

Test dymny przechodzi pełny przepływ (odmowa bez sesji → logowanie → każda
zakładka → oba silniki → wylogowanie) i pilnuje, że każdy wykres renderuje się
w skali 1:1 względem swojego `viewBox`. Powstał, bo trzy regresje interfejsu
przeszły przez `npm run build` bez ostrzeżenia — build sprawdza kompilację,
nie to, czy cokolwiek widać.
