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
- [x] 123 testy jednostkowe (reguły ryzyka, portfel, koszty, TOTP, kryptografia)
- [x] Ustawienia, powiadomienia (w aplikacji / Telegram / webhook), profil konta
- [x] Prowizja w mierze, a nie tylko w krzywej kapitału — odsetek dodatnich
      okien i mediana Sharpe były liczone **przed kosztami** (patrz `docs/03`)
- [x] Konto demo Bybit **EU** zweryfikowane; cennik odczytany z giełdy
- [x] Skaner RSS — 10 kanałów sprawdzonych i zbierających (235 wpisów w pierwszym
      cyklu, zero błędów); artykuły zapisują się **bez** klucza
- [ ] Analiza Claude — **czeka na klucz API Anthropic**; do tego czasu
      `news_shock` jest wykluczany z kalibracji jako sygnał martwy
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
                                          └──► Anthropic API (claude-opus-5)
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
| `features` | cechy z realnych świec, co 5 min | — |
| `engine` | pętla decyzyjna: reżim → sygnały → risk kernel → dziennik | cechy |
| `trainer` | walk-forward, raz na dobę, po nim **dozór** | cechy |
| `news` | RSS → zapis; z kluczem także filtr → analiza, co 5 min | — |

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

> **Obrazy budowane są obecnie na VPS**, nie pobierane z ghcr.io — repozytorium
> jest prywatne, więc pakiety też, a klaster nie ma tokenu z `read:packages`.
> Patrz `scripts/build-on-vps.sh`; ścieżka powrotu do pełnego GitOps-a jest
> opisana w `charts/trader/values.yaml`.

## Uwierzytelnianie

| Warstwa | Rozwiązanie |
|---|---|
| Hasło | Argon2id, 64 MiB / t=3 / p=4 (OWASP wymaga minimum 19 MiB / t=2 / p=1) |
| Drugi składnik | TOTP (RFC 6238) + 10 jednorazowych kodów zapasowych |
| Sesja | 256 bitów z CSPRNG; w bazie wyłącznie SHA-256; cookie `HttpOnly` + `Secure` + `SameSite=Strict` |
| Session fixation | Nowa sesja przy logowaniu i po potwierdzeniu drugiego składnika |
| CSRF | Double-submit cookie na metodach zmieniających stan |
| Blokada | 15 minut po pięciu nieudanych próbach; stała odpowiedź i stałe opóźnienie dla nieistniejącego loginu |
| Audyt | Każda próba logowania, zmiana hasła i operacja na 2FA w `app.audit_log` |

Konto zakładane poza repozytorium:

```bash
kubectl exec <pod-api> -- sh -c 'printf %s "<hasło>" | python scripts/create_admin.py admin --stdin'
```

## Testy

```bash
BASE_URL=https://trader.renacode.com TEST_PASSWORD=... node tests/smoke.spec.mjs
```

Test dymny przechodzi pełny przepływ (odmowa bez sesji → logowanie → każda
zakładka → oba silniki → wylogowanie) i pilnuje, że każdy wykres renderuje się
w skali 1:1 względem swojego `viewBox`. Powstał, bo trzy regresje interfejsu
przeszły przez `npm run build` bez ostrzeżenia — build sprawdza kompilację,
nie to, czy cokolwiek widać.
