{{- define "trader.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "trader.fullname" -}}
{{- printf "%s" (default .Chart.Name .Values.nameOverride) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "trader.labels" -}}
app.kubernetes.io/name: {{ include "trader.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/*
HASLO NIGDY W URL-U - IDZIE W `PGPASSWORD` (2026-10-03, docs/08 brama 3, F-9.4).

URL skladal sie podstawieniem Kubernetesa (`user:$(HASLO)@host`), gdzie
kodowanie procentowe jest niemozliwe: haslo ze znakiem `/`, `@`, `?` albo `#`
rozcinalo lancuch, a komunikat mowil o nierozwiazanym hoscie. libpq bierze
haslo z `PGPASSWORD`, gdy w lancuchu go nie ma - wiec kod (psycopg, pula,
psql) zostaje bez zmian. Pilnuje tego tests/test_haslo_poza_url.py.
*/}}

{{/* Połączenie WŁAŚCICIELA schematów. Wyłącznie do migracji i konserwacji. */}}
{{- define "trader.databaseUrl" -}}
postgresql://{{ .Values.db.username }}@{{ include "trader.fullname" . }}-db:5432/{{ .Values.db.database }}
{{- end -}}

{{/*
Połączenie APLIKACJI i procesów roboczych — rola o minimalnych uprawnieniach.

Nie jest właścicielem żadnej tabeli, więc nie może wyłączyć wyzwalacza
chroniącego niezmienność dziennika decyzji ani zmienić struktury bazy —
niezależnie od tego, co zdołałby wykonać błąd w kodzie albo wstrzyknięcie SQL.
*/}}
{{- define "trader.appDatabaseUrl" -}}
postgresql://{{ .Values.db.appUsername }}@{{ include "trader.fullname" . }}-db:5432/{{ .Values.db.database }}
{{- end -}}

{{/* Haslo roli aplikacji dla libpq - obok `appDatabaseUrl`, ktory hasla nie ma. */}}
{{- define "trader.appPgPasswordEnv" -}}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef: { name: {{ .Values.secretName }}, key: APP_DB_PASSWORD }
{{- end -}}

{{/* Zmienna z hasłem roli aplikacji — wstrzykiwana wszędzie, gdzie użyto appDatabaseUrl. */}}
{{- define "trader.appDbPasswordEnv" -}}
- name: APP_DB_PASSWORD
  valueFrom:
    secretKeyRef: { name: {{ .Values.secretName }}, key: APP_DB_PASSWORD }
{{- end -}}

{{/*
POŚWIADCZENIA PER PROCES (migracja 0034).

Do tej pory silnik, dozorca ryzyka, czujka żywotności i próba pożarowa łączyły
się do bazy JEDNĄ rolą `trader_app`. Nic nie WYMUSZAŁO, że silnik nie zwolni
sobie kill-switcha — nie robił tego, bo nie miał takiego kodu, a nie dlatego,
że nie mógł.

Rozdział uprawnień jest wart tyle, ile rozdział poświadczeń: gdyby wszystkie
role miały to samo hasło, proces o węższych nadaniach po prostu połączyłby się
tą rolą, która ma szersze. Dlatego każda rola ma własny klucz w sekrecie.

Wywołanie:  {{`{{- include "trader.rolaDbEnv" (dict "ctx" $ "rola" "silnik") | nindent 12 }}`}}

Nazwa roli MUSI istnieć w .Values.db.role — literówka ma zatrzymać `helm
template`, a nie po cichu wygenerować pusty login, na którym pod wstanie
i połączy się jako nikt.
*/}}
{{- define "trader.rolaDbEnv" -}}
{{- $ := .ctx -}}
{{- $r := index $.Values.db.role .rola -}}
{{- if not $r }}{{ fail (printf "Nie ma roli bazodanowej %q w .Values.db.role" .rola) }}{{ end }}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef: { name: {{ $.Values.secretName }}, key: {{ $r.secretKey }} }
- name: DATABASE_URL
  value: "postgresql://{{ $r.user }}@{{ include "trader.fullname" $ }}-db:5432/{{ $.Values.db.database }}"
{{- end -}}

{{/*
Hasła WSZYSTKICH ról procesów — wyłącznie dla kontenera migracji.

Migrator chodzi rolą właściciela i ustawia nimi hasła pozostałych ról (patrz
db/migrate.py). Jest jedynym miejscem, które widzi je wszystkie naraz; gdyby
robił to ktoś inny, hasło roli dozorcy musiałoby trafić do poda, który nie jest
dozorcą.
*/}}
{{- define "trader.rolaHaslaEnv" -}}
{{- range $nazwa, $r := .Values.db.role }}
- name: {{ $r.secretKey }}
  valueFrom:
    {{/*
    `optional: true` - I TO JEST WYBOR MIEDZY DWOMA AWARIAMI, nie poluzowanie.

    Bez tego klucz nieobecny w sekrecie daje `CreateContainerConfigError`
    kontenerowi, ktory to czyta. Czyta to kontener STARTOWY poda API, wiec
    brak JEDNEGO hasla - np. przy dodaniu nowej roli - zatrzymuje caly pod
    API. Kladzie to dashboard, czyli JEDYNE miejsce, w ktorym widac stan
    systemu, i robi to z powodu niezwiazanego z niczym, co API robi.

    Z `optional` brak hasla przestaje dotyczyc API: migrator wypisuje wprost
    "X nie ustawione - rola zostaje bez hasla i jej proces sie nie zaloguje"
    (db/migrate.py), a czerwony robi sie ten pod albo Job, KTORY TEJ ROLI
    UZYWA. Awaria zostaje widoczna i trafia tam, gdzie jej przyczyna.

    GWARANCJA BEZPIECZENSTWA ZOSTAJE NIETKNIETA: rola bez hasla nie zaloguje
    sie w ogole, wiec zaden proces nie zaczyna dzialac na cudzych
    poswiadczeniach. Zmienia sie wylacznie to, KTORY pod pada.
    */}}
    secretKeyRef: { name: {{ $.Values.secretName }}, key: {{ $r.secretKey }}, optional: true }
{{- end }}
{{- end -}}

{{/*
Modele i limity dostawcy analizy — JEDNA DEFINICJA DLA WSZYSTKICH PODÓW.

DLACZEGO WSPÓLNY HELPER, A NIE DWIE LISTY

`/api/api-spend` pokazuje limit dobowy, wołając `limit_dobowy_usd()` w procesie
API — czyli czyta WŁASNE środowisko, nie środowisko workera, który ten limit
faktycznie egzekwuje. Dwie listy zmiennych znaczyłyby więc interfejs mówiący
jedną liczbę i bramkę zatrzymującą wydatki na drugiej, bez żadnego sygnału, że
się rozjechały. Ten sam kształt rozjazdu (workers.features kontra
trainerCron.horyzonty) kosztował już 21 godzin martwych cech 2026-09-04.

`required` zamiast `default`: wartość pusta ma wywrócić `helm template`, a nie
po cichu zejść na domyśl wpisany w `services/ingest/llm.py`. Domyśl w kodzie
jest zabezpieczeniem na wypadek braku zmiennej, a nie deklaracją tego, czym
system pracuje — przez cały czas życia projektu ANALYSIS_MODEL nie istniał
nigdzie indziej i dlatego każda analiza szła najdroższym modelem.
*/}}
{{/*
KLUCZE DOSTAWCOW ANALIZY ZE SRODOWISKA - sciezka zapasowa obok widoku
`app.secret_llm` (`news_worker._klucz`: baza ma pierwszenstwo). JEDNA definicja
dla workerow i post-mortemu (audyt 2026-10-10, N2-3): post-mortem nie mial tych
wpisow, wiec przy kluczu Gemini trzymanym tylko w sekrecie k8s budowal samego
Anthropica i przy pustym koncie padal zamiast zejsc na Gemini jak skaner
wiadomosci. Obie `optional`: brak klucza w sekrecie nie moze zatrzymac poda.
*/}}
{{- define "trader.llmKluczeEnv" -}}
- name: ANTHROPIC_API_KEY
  valueFrom:
    secretKeyRef: { name: {{ .Values.secretName }}, key: ANTHROPIC_API_KEY, optional: true }
- name: GEMINI_API_KEY
  valueFrom:
    secretKeyRef: { name: {{ .Values.secretName }}, key: GEMINI_API_KEY, optional: true }
{{- end -}}

{{- define "trader.llmEnv" -}}
- name: ANALYSIS_MODEL
  value: {{ required "llm.analysisModel musi byc ustawiony wprost" .Values.llm.analysisModel | quote }}
- name: TRIAGE_MODEL
  value: {{ required "llm.triageModel musi byc ustawiony wprost" .Values.llm.triageModel | quote }}
- name: ANALYSIS_MODEL_GEMINI
  value: {{ required "llm.analysisModelGemini musi byc ustawiony wprost" .Values.llm.analysisModelGemini | quote }}
- name: TRIAGE_MODEL_GEMINI
  value: {{ required "llm.triageModelGemini musi byc ustawiony wprost" .Values.llm.triageModelGemini | quote }}
- name: LLM_DAILY_USD
  value: {{ required "llm.dailyUsd musi byc ustawiony wprost" .Values.llm.dailyUsd | quote }}
- name: LLM_DAILY_CALLS
  value: {{ required "llm.dailyCalls musi byc ustawiony wprost" .Values.llm.dailyCalls | quote }}
{{- end -}}

{{/*
KONTEKST BEZPIECZEŃSTWA PODÓW Z OBRAZEM API (2026-10-02).

Obraz API ustawia `USER trader` (uid 10001, docker/api.Dockerfile), ale chart
niczego nie wymuszał: brak `runAsNonRoot`, brak `allowPrivilegeEscalation:
false`, pełny domyślny zestaw capabilities. Te pody trzymają `SECRETS_KEY`
i - w `engine` - odszyfrowane klucze giełdy, a `news` przetwarza treść od
obcych (RSS, odpowiedzi modeli). Ucieczka z procesu nie powinna dostawać
w prezencie roota ani `setuid`.

`runAsUser` JEST WPISANY LICZBOWO, i to nie jest powtórzenie Dockerfile'a:
`USER trader` to nazwa, a kubelet z `runAsNonRoot: true` i nienumerycznym
użytkownikiem obrazu odmawia startu („cannot verify user is non-root").
Bez tej linii poprawka zatrzymałaby API i wszystkie workery naraz.
Pilnuje tego tests/test_chart_kontekst_bezpieczenstwa.py.

`readOnlyRootFilesystem` CELOWO NIE: nikt nie zmierzył, czy żaden proces nie
pisze poza /tmp, a ten chart synchronizuje się na produkcję od razu.
*/}}
{{- define "trader.podBezUprawnien" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "trader.kontenerBezUprawnien" -}}
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
{{- end -}}

{{/*
To samo co `trader.kontenerBezUprawnien` plus ROOT FILESYSTEM TYLKO DO ODCZYTU
(audyt Fable U-8). Dla workerow: jedynym zapisem procesu jest plik zywotnosci
`/tmp/trader-zywy` (puls.zyje), wiec /tmp dostaje `emptyDir`. Sprawdzone
03.10 obrazem z `--read-only --tmpfs /tmp`. Od 04.10 (T-13) takze API i front;
Joby zostaja na wersji bez tej flagi - nikt nie zmierzyl, czy nie pisza poza /tmp.
*/}}
{{- define "trader.kontenerTylkoOdczyt" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
{{- end -}}

{{/*
OBRAZ BAZY - repozytorium:tag@digest (2026-10-03).

`pg17` jest tagiem RUCHOMYM, a `pullPolicy: IfNotPresent` znaczy, ze kazdy
wezel trzyma ten obraz, ktory pobral PIERWSZY. Nowy wezel albo wyczyszczony
cache pobralby inna wersje TimescaleDB pod ta sama nazwa - a zrzut z kopii
pasuje tylko do wersji rozszerzenia, z ktorej powstal (backup.yaml).

Digest jest WYMAGANY: pusty ma zatrzymac `helm template`, a nie po cichu
zejsc na sam tag. Ta sama zasada co w `trader.llmEnv`.
*/}}
{{- define "trader.obrazBazy" -}}
{{ .Values.db.image.repository }}:{{ .Values.db.image.tag }}@{{ required "db.image.digest musi byc ustawiony (przypiecie obrazu bazy)" .Values.db.image.digest }}
{{- end -}}

{{/*
ETYKIETA PODOW CRONJOBOW (2026-10-03) - po niej templates/networkpolicy.yaml
wpuszcza Joby do bazy.

Pody Jobow nie mialy zadnej wlasnej etykiety (tylko `job-name` z unikalnym
sufiksem), wiec nie dalo sie ich wskazac w polityce inaczej niz "kazdy pod
w przestrzeni nazw" - czyli razem z kidwatchem i dietetykiem. Klucz ten sam co
`trader.renacode.com/component: worker` na Deploymentach workerow.
*/}}
{{/*
BRAMKA POWIADOMIEN RENACODE (2026-10-04) - adres i klucz wysylkowy dla
`notify()`, ktory przekazuje tam kazdy alarm krytyczny (services/api/bramka.py).

Dostaja je procesy, ktore wolaja `notify()`: API, workery (w tym oba dozorcy
ryzyka) i CronJoby czujek, instrumentow, postmortemu i trenera. Workery
wszystkie naraz, z tego samego powodu co `trader.llmEnv`: lista per worker
bylaby druga lista do utrzymania. Do bramki i tak dochodza tylko ci z listy
`networkPolicy.klienci` w chartcie bramki (renacode-infra).

KLUCZ `optional: true`. Brak klucza w Sekrecie = alarmy zostaja w panelu
(linia w logu), a nie pod w CreateContainerConfigError. Klucz to
`BRAMKA_KLUCZ_TRADER` z `bramka/bramka-klucze`, skopiowany do Sekretu tradera
jako `BRAMKA_KLUCZ` - nigdy do repozytorium.
*/}}
{{- define "trader.bramkaEnv" -}}
- name: BRAMKA_URL
  value: {{ required "bramkaPowiadomien.url musi byc ustawiony wprost" .Values.bramkaPowiadomien.url | quote }}
- name: BRAMKA_KLUCZ
  valueFrom:
    secretKeyRef: { name: {{ .Values.secretName }}, key: BRAMKA_KLUCZ, optional: true }
{{- end -}}

{{- define "trader.etykietyZadania" -}}
trader.renacode.com/component: job
{{- end -}}

{{/*
REGULY EGRESS - WSZYSTKO POZA `networkPolicy.egress.siecDomowa` (2026-10-03).

Wezel ma trase do sieci prywatnych przez tunel - bez tej polityki kazdy pod
dochodzil do urzadzen w tych sieciach. Trzy reguly, suma (OR):

  1. DNS do CoreDNS (UDP i TCP 53),
  2. dowolny pod w klastrze (`namespaceSelector: {}`) - ruch wewnatrz
     klastra bez zmian; kube-router liczy egress PO DNAT kube-proxy, wiec
     ClusterIP jest juz adresem poda,
  3. kazdy adres IPv4 poza `networkPolicy.egress.siecDomowa` - internet oraz
     adres WEZLA, na ktory DNAT-uje sie ClusterIP API serwera (10.43.0.1:443
     -> :6443) i kubelet (:10250).

ipBlock w kube-routerze dotyczy KAZDEGO adresu docelowego, takze adresow
podow (ipset hash:net, wyjatki jako wpisy `nomatch`, /0 rozbijane na dwa /1 -
pkg/controllers/netpol/policy.go, evalIPBlockPeer). Regula 2 zostaje mimo to:
wedlug specyfikacji NetworkPolicy ipBlock nie musi obejmowac podow, wiec ruch
w klastrze nie powinien od tego zalezec.
*/}}
{{/*
REGULY EGRESS BEZ INTERNETU - tylko DNS i klaster (audyt Fable U-16).
Pierwsze dwie reguly `trader.regulyEgress`; dla podow, ktore nie lacza sie
z zadnym API zewnetrznym (`networkPolicy.egress.bezInternetu`).
*/}}
{{- define "trader.regulyEgressKlaster" -}}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
  ports:
    - { protocol: UDP, port: 53 }
    - { protocol: TCP, port: 53 }
- to:
    - namespaceSelector: {}
{{- end -}}

{{- define "trader.regulyEgress" -}}
{{ include "trader.regulyEgressKlaster" . }}
- to:
    - ipBlock:
        cidr: 0.0.0.0/0
        except: {{- toYaml .Values.networkPolicy.egress.siecDomowa | nindent 10 }}
{{- end -}}
