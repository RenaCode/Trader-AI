# RAPORT: czym zastąpić `health-check.sh` — kontener w klastrze zamiast basha z crona

Data: 2026-09-08. Status: ZAKOŃCZONY.
Oznaczenia: **[Z]** = zweryfikowane, jest źródło. **[O]** = moja ocena, nie zmierzone.
"nie ustaliłem" = brak wiarygodnej liczby — świadomie NIE szacuję.

---

## 1. REKOMENDACJA (jeden akapit)

**Nie wymieniaj logiki — wymień opakowanie.** Zabierz obecne 106 linii basha z hosta,
zbuduj z nich obraz, wdroż jako `CronJob` w klastrze przez ten sam chart ArgoCD co reszta,
daj mu własny `Secret` z kluczem Mailgun i własne `ServiceAccount` + RBAC, dopisz testy
jednostkowe do funkcji progów i tłumienia, i rozszerz o punkty 6–10 (Joby z CronJobów,
restarty, PVC, certyfikaty, stan aplikacji ArgoCD) — to wszystko jest jednym `kubectl get`
każde. Steady-state koszt pamięci: **zero** (CronJob nie trzyma poda między uruchomieniami),
przy 3,4 GB wolnego na cztery projekty to jest argument nie do pobicia. Do tego dołóż dwie
rzeczy spoza klastra, bo żaden kontener w k3s nie odpowie na pytanie „czy k3s żyje": darmowy
**healthchecks.io** jako dead man's switch (CronJob pinguje po każdym udanym przebiegu; brak
pinga = mail) i zewnętrzny monitor HTTPS na `trader.renacode.com` i pozostałe adresy.
**kube-prometheus-stack odradzam jako pierwszy krok**, choć — wbrew swojej reputacji — mieści
się w budżecie: zmierzone `<512 Mi` na klastrze 1 węzeł / ~30 podów po agresywnym przycięciu
metryk. Odradzam nie z powodu RAM-u, tylko dlatego, że dokłada 6–8 ruchomych części, CRD i
operatora po to, by odpowiedzieć na pytania, na które `kubectl` odpowiada jednym poleceniem.
Wróć do niego dopiero wtedy, gdy właściciel naprawdę zechce **panel i historię**, a nie
tylko alarmy — i wtedy świadomie zapłać ~512 Mi (15% wolnej pamięci), wiedząc, że
**skrócenie retencji tego rachunku NIE zmniejszy**, bo retencja kosztuje dysk, a nie RAM.

---

## 2. PEŁEN ZAKRES DO POKRYCIA (12 punktów, wg doprecyzowania właściciela)

| # | Co | Dziś w skrypcie |
|---|---|---|
| 1 | Dysk hosta | TAK (próg 80%) |
| 2 | Dostępna pamięć hosta | TAK (próg 15%) |
| 3 | Load average / CPU hosta | NIE |
| 4 | Węzeł k3s `Ready` | TAK |
| 5 | Pody trwale złe (CrashLoop, ImagePull, OOMKilled, Evicted, Error), wszystkie ns | TAK |
| 6 | **Pody z Jobów/CronJobów, które padły** | NIE ← realny incydent, 3 h niezauważone |
| 7 | Rosnąca liczba restartów kontenerów | NIE |
| 8 | PVC bliskie zapełnienia | NIE |
| 9 | Wygasające certyfikaty (cert-manager) | NIE |
| 10 | ArgoCD: nieudany sync / aplikacja zdegradowana | NIE |
| 11 | HTTPS z zewnątrz + ważność certyfikatu | NIE |
| 12 | Kto pilnuje pilnującego (spoza klastra) | NIE |

---

## 3. USTALENIE WSTĘPNE: argocd-notifications ODPADA jako zamiennik [Z]

Dwa niezależne dowody:

1. **Dokumentacja triggerów**: warunek triggera operuje **wyłącznie na obiekcie `app`**
   (Application/ApplicationSet). Dostępne helpery: `time`, `strings`, `sync`, `repo`.
   Brak dostępu do Node, Pod, Job, do metryk hosta.
   <https://argo-cd.readthedocs.io/en/stable/operator-manual/notifications/triggers/>
2. **Pomiar koordynatora na żywym klastrze**: drzewo zasobów aplikacji `trader` to
   10 Deployment, 4 Service, 4 CronJob, 2 PVC, 2 Ingress, 1 Middleware — **podów tam nie ma**.
   Joby tworzone przez CronJob nie pochodzą z manifestu, więc są niewidoczne. Dowód:
   3 zadania `trader-zywotnosc` = `Failed` przez ponad 3 h, a `application/trader` przez
   cały ten czas `Healthy / Synced`.

Pokrycie: punkty 1–9 i 11–12 — **żadnego**. Punkt 10 — jedyny, który obsługuje, i obsługuje
go dobrze.

**Tłumienie**: jedyny mechanizm to pole `oncePer` (np. `oncePer: app.status.sync.revision`).
**Brak cooldownu czasowego, brak repeat-interval** [Z, dokumentacja j.w.].

→ **Osobna, opcjonalna rekomendacja**: włączyć `argocd-notifications-cm` wyłącznie dla
punktu 10 (`on-sync-failed`, `on-health-degraded`). Koszt pamięci: **0** — kontroler już
działa od 75 dni i tylko konsumuje pamięć bez pożytku. To jest darmowy zysk, ale to jest
INNY alert, nie zamiennik skryptu.

---

## 4. ZMIERZONE / UDOKUMENTOWANE ZUŻYCIE RAM

### Punkt odniesienia — ile już zjada sam k3s [Z]
Dokumentacja k3s, "Resource Profiling", serwer jednowęzłowy z workloadem, 95. percentyl:
**1596 MB** (kine/SQLite) lub 1606 MB (embedded etcd) na Intel 8375C. Agent: 275 M.
<https://docs.k3s.io/reference/resource-profiling>
→ Z 4,5 GB zajętych ~1,6 GB to sam k3s. To nie jest do odzyskania.

### Prometheus / kolektor
| Źródło | Warunki | RAM |
|---|---|---|
| Microsoft, benchmark ama-metrics, tabela "replica" [Z] | 3 węzły, 40 podów, 11 344 próbek/min, cele domyślne | **148 Mi** |
| ta sama tabela, DaemonSet [Z] | 3 węzły, 40 podów | 581 Mi łącznie, **189 Mi/pod** |
| ta sama tabela [Z] | 340 węzłów, 13 000 podów | 1,70 GB |

<https://learn.microsoft.com/en-us/azure/azure-monitor/containers/prometheus-metrics-scrape-scale>
Zastrzeżenie: to kolektor Azure (OTel+Prometheus), nie czysty `prometheus-server`. Rząd
wielkości uważam za przenośny; twardej liczby dla czystego Prometheusa przy 34 podach
**nie ustaliłem**.

### Cały kube-prometheus-stack po przycięciu — najbliższy analog naszej sytuacji [Z]
Blog clux.dev, "Running a Minimal Prometheus": **`<512Mi` łącznie, uśrednione przez tydzień**,
przy **`<0.01` rdzenia**, na klastrze **jednowęzłowym z ~30 podami** i **7 000 aktywnych serii**.
<https://clux.dev/post/2024-09-07-prometheus-minified/>
Zastosowane cięcia: odrzucenie 97% metryk kubeleta przez metric relabeling, odrzucenie
histogramów (`'^.*bucket.*'`), usunięcie Thanosa i prometheus-adapter, interwały scrape i
evaluation podniesione do 60 s, agresywne kasowanie etykiet w kube-state-metrics i
node-exporter.
Zastrzeżenie: to pomiar z bloga, nie z dokumentacji producenta. Autor podaje wartość
zagregowaną dla stosu, **bez rozbicia na komponenty**. Nie ustaliłem, czy Grafana była w tej
sumie.

Dla kontrastu — deklaracje „z pudełka": różne materiały mówią o 4–8 GB RAM dla Prometheusa
na klastrze 1–5 węzłów [Z, ale to poradniki, nie benchmark]. **Rozjazd 8–16× między
„domyślnie" a „po przycięciu" jest tu najważniejszą liczbą w całym raporcie.**

### Pozostałe komponenty
| Komponent | Liczba | Źródło / status |
|---|---|---|
| kube-state-metrics | **250 MiB, 0,1 rdzenia** — oficjalna "Resource recommendation", benchmark na klastrze 100-węzłowym | [Z] README kube-state-metrics. Przy 34 podach realnie znacznie mniej, ale **zmierzonej liczby dla tej skali nie ustaliłem** |
| node-exporter | chart: `resources: {}`. Zakomentowany przykład: **requests 30Mi / limits 50Mi** | [Z] <https://github.com/prometheus-community/helm-charts/blob/main/charts/prometheus-node-exporter/values.yaml> |
| node-exporter (inne wdrożenia) | openstack-helm: requests **128Mi**, limits **1024Mi** | [Z]. Rozrzut zależy od włączonych kolektorów; kolektor `systemd` bywa pamięciożerny (issue node_exporter#1108) |
| metrics-server | **200 MiB + 100m** dla ≤100 węzłów; +2 MiB/węzeł powyżej | [Z] dokumentacja metrics-server. **Ten koszt jest już zapłacony** |
| Grafana | oficjalny floor: **512 MB + 1 rdzeń**; tier "small": 2 rdzenie / 2–4 GB | [Z] dokumentacja Grafany. Realne zużycie na biegu jałowym jest niższe, ale **nie ustaliłem zmierzonej wartości** |
| Grafana Alloy | przykład w chartcie: requests **128Mi** / limits 256Mi; inny przykład: **500Mi**; raport użytkownika na klastrze 1-węzłowym: **~700 MB** | [Z] <https://github.com/grafana/k8s-monitoring-helm/issues/541> |
| Uptime Kuma | **~100 MB** na biegu jałowym (v2.x); poradniki: 50–200 MB, komfortowo 256–512 MB | [Z, ale źródła to issue i blogi] issue louislam/uptime-kuma#5884. Znane narastanie pamięci: issue #7537 |
| blackbox-exporter | przykład chartu: requests **128Mi** / limits 512Mi | [Z]. Realnego RSS **nie ustaliłem** |
| botkube | `resources: {}` — brak domyślnych | [Z] <https://github.com/kubeshop/botkube/blob/main/helm/botkube/values.yaml> |
| Alertmanager | **nie ustaliłem** | — |
| prometheus-operator | raport w issue helm-charts#314 mówi o ~420–440 MiB — to zgłoszenie, nie benchmark. **Uznaję za nieustalone** | — |
| VictoriaMetrics (vmsingle/vmagent/vmalert) | chart nie ustawia domyślnych `resources` (`{}`). Przykład vmalert w dokumentacji: 128Mi | [Z] values.yaml victoria-metrics-k8s-stack. **Zmierzonej wartości dla 1 węzła / 34 podów nie ustaliłem** |
| Własny CronJob z bashem + kubectl | steady-state **0 Mi** (pod nie istnieje między przebiegami); szczyt w trakcie ~20 s przebiegu — **nie ustaliłem** ([O]: kubectl to statyczny binarny Go, rząd 40–80 MB RSS) | [O] |

---

## 5. KLUCZOWA OBSERWACJA: retencja to dysk, nie RAM [Z]

Koordynator zasugerował skrócenie retencji jako sposób na oszczędność pamięci.
**To nie zadziała.** Zużycie RAM Prometheusa jest funkcją **liczby aktywnych serii
czasowych i częstotliwości scrape'u**, nie długości retencji; zmiana retencji z 15 na
90 dni zwiększa zużycie DYSKU, nie pamięci.
Dźwignia na RAM to **redukcja liczby serii** (metric relabeling / dropping) — dokładnie to,
czym clux zszedł do `<512Mi`. Dźwignia na dysk to retencja: 100 000 serii, scrape 30 s,
15 dni ≈ 9,7 GB (przy 27% z 98 GB zajętych — do udźwignięcia, ale to +10% dysku, a dysk
jest jednym z monitorowanych progów; ironia warta odnotowania).
Źródła: materiały o wysokim zużyciu Prometheusa; regułę „~3 KB RAM na aktywną serię"
podaję jako [O], bo nie pochodzi z oficjalnej dokumentacji.

---

## 6. PORÓWNANIE OPCJI

Legenda pokrycia: ✅ pełne · ⚠️ częściowe / wymaga dokładki · ❌ brak

| | A. Własny kontener (CronJob) | B. kube-prometheus-stack, BEZ Grafany | C. kube-prometheus-stack + Grafana | D. VictoriaMetrics k8s stack | E. Grafana Alloy | F. event-exporter / botkube | G. Uptime Kuma | H. healthchecks.io | I. argocd-notifications |
|---|---|---|---|---|---|---|---|---|---|
| **RAM (steady)** | **0 Mi** [O: szczyt nieustalony] | **<512 Mi po przycięciu** [Z, analog 1 węzeł/30 podów] | B + Grafana (floor 512 MB wg docs; realnie niżej, nie ustaliłem) | nie ustaliłem | 128–700 MB [Z] | botkube: nie ustaliłem; e-e: nie ustaliłem | ~100 MB idle [Z] | 0 (SaaS) | **0** (już działa) |
| 1. dysk hosta | ✅ hostPath `/` + `df`, albo kubelet `/stats/summary` | ✅ node-exporter, `NodeFilesystemAlmostOutOfSpace` | ✅ | ✅ (ten sam node-exporter) | ⚠️ zbiera, nie alarmuje | ❌ | ❌ | ❌ | ❌ |
| 2. pamięć hosta | ✅ `/proc/meminfo` w kontenerze **pokazuje pamięć HOSTA** [Z] | ✅ node-exporter | ✅ | ✅ | ⚠️ | ❌ | ❌ | ❌ | ❌ |
| 3. load average | ✅ `/proc/loadavg` (host, tak samo) | ✅ `node_load1/5/15` | ✅ | ✅ | ⚠️ | ❌ | ❌ | ❌ | ❌ |
| 4. node Ready | ✅ `kubectl get node` | ✅ `KubeNodeNotReady`, `for: 15m` [Z] | ✅ | ✅ | ⚠️ | ⚠️ | ❌ | ❌ | ❌ |
| 5. złe pody, wszystkie ns | ✅ `kubectl get pods -A` | ✅ `KubePodNotReady` `for:15m`, `KubePodCrashLooping` `for:15m`, `KubeContainerWaiting` `for:1h` [Z] | ✅ | ✅ | ⚠️ | ✅ (zdarzenia) | ❌ | ❌ | ⚠️ tylko przez health Deploymentu |
| 6. **padłe Joby/CronJoby** | ✅ `kubectl get jobs -A` | ✅ **`KubeJobFailed`**, `expr: kube_job_failed > 0`, `for: 15m`, etykiety `namespace`,`job_name`, **wszystkie ns** [Z] | ✅ | ✅ | ⚠️ | ✅ | ❌ | ❌ | ❌ **dziura z dzisiaj** |
| 7. rosnące restarty | ✅ pole `restartCount` + plik stanu | ✅ `kube_pod_container_status_restarts_total`, `rate()` | ✅ | ✅ | ⚠️ | ⚠️ | ❌ | ❌ | ❌ |
| 8. PVC bliskie pełna | ✅ kubelet `/stats/summary` lub `/metrics` | ✅ **`KubePersistentVolumeFillingUp`** (`kubelet_volume_stats_available_bytes`) [Z] | ✅ | ✅ | ⚠️ | ❌ | ❌ | ❌ | ❌ |
| 9. certyfikaty | ⚠️ `kubectl get certificate -A` → `status.notAfter` / `renewalTime` [O, do sprawdzenia 2 min] | ✅ `certmanager_certificate_expiration_timestamp_seconds` (port 9402) [Z] | ✅ | ✅ | ⚠️ | ❌ | ✅ dla cert TLS z zewnątrz | ❌ | ❌ |
| 10. ArgoCD sync/health | ✅ `kubectl get applications -A` | ⚠️ wymaga ServiceMonitora na argocd-metrics | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ❌ | ❌ | ✅ **jedyna mocna strona** |
| 11. HTTPS z zewnątrz | ⚠️ `curl` z wnętrza klastra ≠ z zewnątrz | ⚠️ wymaga blackbox-exportera, i tak z wnętrza | ⚠️ | ⚠️ | ❌ | ❌ | ✅ jeśli poza klastrem | ❌ | ❌ |
| 12. **pilnowanie pilnującego** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ jeśli poza klastrem | ✅ **dead man's switch** [Z] | ❌ |
| **E-mail natywnie** | ✅ (własny kod) | ✅ Alertmanager `email_configs` (`smarthost`, `auth_username`, `auth_password` → Mailgun SMTP) [Z] | ✅ | ✅ (Alertmanager) | ❌ | ❌ **botkube nie ma sinka SMTP** [Z]; event-exporter też nie znalazłem | ✅ | ✅ | ✅ |
| **Ruchome części** | 1 (obraz + CronJob + Secret + RBAC) | 5–6 (operator, CRD, Prometheus, Alertmanager, KSM, node-exporter) | 6–7 | 8 (vmsingle, vmagent, vmalert, alertmanager, grafana, KSM, node-exporter, operator) [Z] | 1 + backend | 1 + relay webhook→mail | 1 (ale na innym hoście) | 0 | 0 |

### Dlaczego niektóre opcje odpadają twardo
- **Grafana Alloy** — **nie ewaluuje reguł alertowych.** Jest kolektorem; reguły ocenia
  Prometheus, Mimir Ruler, Loki Ruler albo Grafana Alerting. Komponent
  `mimir.rules.kubernetes` służy do *wgrywania* reguł do rulera Mimira, nie do ich
  wykonywania. [Z, dokumentacja Alloy] → sam z siebie nie jest rozwiązaniem alertowym,
  a kosztuje 128–700 MB. Odpada.
- **botkube** — brak sinka e-mail/SMTP (Slack, Mattermost, Discord, Elasticsearch, Webhook)
  [Z, values.yaml]. Przy wymaganiu „e-mail" wymaga dołożenia relaya webhook→mail, czyli
  kolejnej ruchomej części z kluczem Mailgun. Odpada.
- **kubernetes-event-exporter** — również nie znalazłem sinka SMTP (webhook, Teams, Syslog,
  BigQuery, OpsGenie, Elasticsearch, Loki, Kafka, AWS/GCP). Ten sam problem. Dodatkowo
  widzi tylko *zdarzenia*, więc nie pokrywa punktów 1–3, 8, 9, 11, 12. Odpada.
- **Uptime Kuma wewnątrz klastra** — sprzeczność wewnętrzna: umiera razem z tym, co ma
  pilnować. Ma sens **wyłącznie na innym hoście**. Wtedy pokrywa 11 i 12 za ~100 MB
  cudzej pamięci.

---

## 7. TŁUMIENIE POWTÓRZEŃ — to była realna wada, więc osobno

### Alertmanager (Prometheus i VictoriaMetrics) [Z]
Domyślne wartości: `group_wait: 30s`, `group_interval: 5m`, **`repeat_interval: 4h`**.
Deduplikacja automatyczna: identyczny zestaw etykiet = jeden alert.
<https://prometheus.io/docs/alerting/latest/configuration/>

**Czy tłumienie przeżywa zmianę nazw podów przy wdrożeniu?** Zależy od alertu — i to jest
sedno:

| Alert | Etykiety wyjściowe | Przeżywa redeploy? |
|---|---|---|
| `NodeFilesystemAlmostOutOfSpace` | `instance`, `mountpoint` | ✅ TAK |
| `KubeNodeNotReady` | `node`, `cluster` | ✅ TAK |
| `KubeJobFailed` | `namespace`, `job_name` | ✅ TAK — **brak etykiety `pod`** |
| `KubePodNotReady` | `namespace`, `pod`, `job` | ❌ NIE — `pod` w zestawie |
| `KubePodCrashLooping` | `namespace`, `pod`, `container` | ❌ NIE |

Czyli sam Alertmanager **nie leczy** dokładnie tej wady, którą właściciel dziś naprawiał —
dla alertów per-pod nowa nazwa poda to nowy alert i nowy cooldown. Leczy ją co innego:
pole **`for:`** w regule. `KubePodNotReady` ma `for: 15m`, `KubePodCrashLooping` `for: 15m`,
`KubeContainerWaiting` `for: 1h` [Z, `kubernetesControlPlane-prometheusRule.yaml`].
Alert musi utrzymać się kwadrans, zanim wystrzeli — to jest dokładnie odpowiednik drugiej
dzisiejszej poprawki („nie alarmuj na stanach przejściowych przy wdrożeniu").

**Wniosek uczciwy: kube-prometheus rozwiązuje wadę „stany przejściowe" wzorcowo (`for:`),
a wadę „cooldown resetuje się przy zmianie nazwy poda" rozwiązuje tylko częściowo —
dokładnie tak samo dobrze jak Twoja dzisiejsza poprawka, ani lepiej.**

### argocd-notifications [Z]
Tylko `oncePer`. Brak cooldownu czasowego. Najsłabsze z trzech.

### Własny kontener
Tyle, ile zaprogramujesz — a jest już zaprogramowane i dziś naprawione. Klucz tłumienia bez
nazw podów + plik stanu. Uwaga projektowa: przy przejściu na CronJob **plik stanu musi
przeżyć restart poda** → mały PVC (np. 64 Mi) albo ConfigMap aktualizowany przez to samo
ServiceAccount. To jedyna nowa ruchoma część, jaką ta opcja dokłada.

---

## 8. HOST Z WNĘTRZA KONTENERA — czy to w ogóle działa bez node-exportera? TAK [Z]

To jest ustalenie, które przesądza o opłacalności opcji A.

- **Pamięć hosta (punkt 2)**: `/proc/meminfo` wewnątrz kontenera pokazuje **pamięć HOSTA**,
  nie limit cgroup. To jest powszechnie opisywane jako wada („`free` i `top` w kontenerze
  kłamią"), rozwiązywana przez LXCFS. Tutaj jest **zaletą**: kontener czyta pamięć węzła
  za darmo, bez node-exportera. Źródła: opis LXCFS (Alibaba Cloud), fabiokung.com
  „Memory inside Linux containers", ops.tips.
  To samo dotyczy `/proc/loadavg` → **punkt 3 gratis**.
- **Dysk hosta (punkt 1)**: dwie drogi, obie bez node-exportera:
  1. `hostPath` na `/` w trybie `readOnly` + `df` — działa, ale wymaga hostPath (uprawnienie,
     które warto ograniczyć);
  2. **kubelet Summary API**: `kubectl get --raw "/api/v1/nodes/<node>/proxy/stats/summary"`
     → `node.fs.availableBytes` / `node.fs.capacityBytes` oraz osobno `imageFs`.
     [Z] <https://kubernetes.io/docs/reference/instrumentation/node-metrics> i
     `k8s.io/kubelet/pkg/apis/stats/v1alpha1`. Wymaga RBAC `nodes/proxy` `get`.
     **Ta sama odpowiedź daje punkt 8 (PVC)** — Summary API zawiera statystyki wolumenów.
- **metrics-server nie wystarczy**: wystawia wyłącznie CPU i pamięć (NodeMetrics/PodMetrics),
  **nie wystawia dysku**. Dokumentacja wprost ostrzega: „not meant for non-autoscaling
  purposes", nie jest „an accurate source of resource usage metrics", do monitoringu
  odsyła do Prometheusa. [Z] <https://kubernetes-sigs.github.io/metrics-server/>
  → metrics-server może posłużyć jako sanity-check punktu 2, ale nie jako podstawa, i nie
  da punktu 1.

**Konsekwencja: node-exporter (30–128 Mi) jest potrzebny TYLKO w opcjach B/C/D. Opcja A
dostaje punkty 1, 2, 3 i 8 za zero dodatkowej pamięci.**

---

## 9. ALARM vs ALARM + PANEL — wycena osobna

**To są dwa różne produkty i różnica jest większa, niż wygląda.**

### Wariant „tylko alarmy" (cisza do awarii, potem mail)
| Rozwiązanie | RAM |
|---|---|
| A. Własny CronJob | **0 Mi steady** |
| B. kube-prometheus-stack, `grafana.enabled: false` [Z, jedna flaga] | **<512 Mi** minus Grafana — nie ustaliłem, ile z tego `<512Mi` brała Grafana |

### Wariant „alarmy + panel + historia"
| Rozwiązanie | RAM |
|---|---|
| C. kube-prometheus-stack + Grafana | **<512 Mi łącznie** wg pomiaru clux (1 węzeł, ~30 podów, 7k serii) [Z] — o ile Grafana była w tej sumie; jeśli nie, dodaj floor 512 MB z dokumentacji Grafany [Z], realnie mniej [O, nie ustaliłem] |
| A+ | Panel? Brak. CronJob nie ma UI ani historii. |

**Odpowiedź na pytanie „czy panel kosztuje mało":**
Przy `<512 Mi` za CAŁY przycięty stos to jest **~15% z 3,4 GB wolnego** — mieści się, nic
nie trzeba przycinać. Przy stosie NIEprzyciętym (4–8 GB dla samego Prometheusa wg
poradników) **nie mieści się w ogóle** i trzeba by przyciąć któryś z czterech projektów.
Cała różnica leży w tym, czy ktoś usiądzie i zrobi metric relabeling. **To jest praca
jednorazowa, ale nietrywialna i łatwa do zepsucia przy aktualizacji chartu.**

**Ostrzeżenie o retencji**: skrócenie retencji do kilku dni **nie zmniejszy RAM-u**
(sekcja 5). Zmniejszy dysk. Do pytania „czy pamięć rośnie od tygodnia" wystarczy 7–14 dni
retencji i to jest rozsądne, ale nie jako oszczędność pamięci — jako oszczędność dysku.

### Moja ocena, czego NIE warto kupować [O]
- **Panelu i historii metryk dla czterech małych projektów na jednym VPS-ie.** Pytanie
  „czy pamięć rośnie od tygodnia" da się odpowiedzieć przez ten sam CronJob dopisujący
  jedną linię do pliku CSV na małym PVC — koszt: kilobajty. Panel jest przyjemny, ale jego
  realną ceną nie jest 512 Mi, tylko utrzymanie: 6–7 komponentów, CRD, operator,
  aktualizacje chartu, ryzyko regresji przy każdym `helm upgrade`.
- **blackbox-exporter wewnątrz klastra do punktu 11.** Sonda z wnętrza k3s nie odpowiada
  na pytanie „czy użytkownik z internetu widzi `trader.renacode.com`" i umiera razem z
  klastrem. To pozorne pokrycie. Punkt 11 ma sens wyłącznie z zewnątrz.

---

## 10. KTO PILNUJE PILNUJĄCEGO (punkt 12) — tanio

Trzy warianty, uporządkowane od najtańszego:

1. **healthchecks.io, plan darmowy "Hobbyist"** [Z]: **20 checków, 3 członków zespołu,
   3 miesiące historii, powiadomienia e-mail**. Model dead man's switch: CronJob pinguje
   URL po każdym udanym przebiegu; brak pinga w terminie → mail. <https://healthchecks.io/pricing/>
   Koszt lokalny: **0 Mi**, jedna linia `curl` na końcu skryptu. Wykrywa: padnięty k3s,
   padnięty węzeł, padnięty CronJob, zapętlony monitoring. **Najlepszy stosunek zysku do
   ceny w całym raporcie.** Serwis jest open source — można go później przenieść do siebie.
2. **Zewnętrzny monitor HTTPS** (punkt 11). UptimeRobot plan darmowy: 50 monitorów,
   interwał 5 min, powiadomienia e-mail, 3 miesiące danych [Z]. **UWAGA — dyskwalifikacja:
   od października 2024 plan darmowy jest ograniczony do użytku osobistego,
   niekomercyjnego**; „business use, commercial projects, client work, or any
   revenue-generating application" jest wprost zabroniony [Z]. Przy `trader.renacode.com`
   i stronie firmowej to jest naruszenie regulaminu → **odradzam**. Alternatywy: płatny
   plan, inny dostawca z komercyjnym darmowym tierem, albo **Uptime Kuma (~100 MB) na
   dowolnym innym hoście** — laptop nie wystarczy, ale najtańszy VPS/Raspberry tak.
3. **Cron na innym hoście** robiący `curl` po adresach + ping do healthchecks.io.
   Najprostsze, zero nowego oprogramowania, ale wymaga drugiego hosta tak samo jak (2).

**Bez drugiego miejsca (SaaS albo drugi host) punktu 12 nie da się rozwiązać.** To nie jest
kwestia wyboru narzędzia, tylko topologii.

---

## 11. UZASADNIENIE — dlaczego nie pozostałe

**Dlaczego nie kube-prometheus-stack (B/C).** Nie dlatego, że nie mieści się w pamięci —
mieści się (`<512 Mi` po przycięciu, zmierzone na bliźniaczym klastrze). Dlatego, że
kupujesz silnik szeregów czasowych, operator, cztery CRD i regułę alertową po to, by
odpowiedzieć na pytania, na które `kubectl get pods -A` odpowiada bezpośrednio. Punkty
1–10 ten stos pokrywa wzorcowo i ma lepsze reguły niż napiszesz sam (`for: 15m`,
`KubeJobFailed` bez etykiety `pod`, `KubePersistentVolumeFillingUp`) — ale te `<512 Mi`
są warunkowe: wymagają ręcznego metric relabelingu, który trzeba utrzymać przez każdą
aktualizację chartu. Bez tego stos rośnie wielokrotnie. Przy czterech projektach na jednym
VPS-ie to jest zobowiązanie, nie jednorazowy koszt. **Wracaj do tej opcji, gdy panel stanie
się realną potrzebą, a nie „byłoby fajnie".**

**Dlaczego nie VictoriaMetrics (D).** Ma opinię oszczędniejszej i prawdopodobnie nią jest
[O], ale: chart `victoria-metrics-k8s-stack` domyślnie włącza **osiem** komponentów —
tyle samo ruchomych części co kube-prometheus-stack, a nie mniej [Z]. Domyślne `resources`
są puste, więc chart nie chroni Cię przed niczym. **Nie ustaliłem zmierzonego zużycia RAM
dla klastra 1 węzeł / 34 pody** — a bez tej liczby nie mam podstawy, by polecić VM zamiast
Prometheusa, dla którego liczbę mam. Rekomendowanie VM byłoby tu wiarą, nie pomiarem.

**Dlaczego nie Alloy (E).** Nie umie ewaluować reguł alertowych [Z]. Kosztuje 128–700 MB
i nadal potrzebuje backendu, który zrobi alerty. Najgorszy stosunek pamięci do funkcji
w zestawieniu.

**Dlaczego nie botkube / kubernetes-event-exporter (F).** Brak sinka e-mail [Z dla botkube,
nie znalazłem dla event-exportera]. Wymagają relaya webhook→mail — czyli dokładnie tej
zależności od cudzego procesu, którą chcemy usunąć. Do tego widzą wyłącznie zdarzenia:
zero pokrycia dla dysku, pamięci, load average, PVC, certyfikatów i punktów 11–12.

**Dlaczego nie argocd-notifications (I) jako zamiennik.** Model danych to wyłącznie obiekt
Application [Z]. Nie widzi podów (potwierdzone pomiarem: drzewo zasobów `trader` zawiera
Deploymenty i CronJoby, nie Pody i nie Joby), nie widzi hosta, a incydent z dzisiaj —
trzy `Failed` Joby przy `Healthy/Synced` przez 3 h — jest twardym dowodem, że nawet punkt 6
przepuszcza. Tłumienie ma najsłabsze z całej stawki (`oncePer`, bez cooldownu czasowego).
**Ale**: włącz go osobno dla punktu 10, bo kosztuje zero i dziś stoi bezużyteczny.

**Dlaczego nie Uptime Kuma w klastrze (G).** Umiera razem z tym, co ma pilnować. Poza
klastrem — dobra, tania opcja na punkty 11 i 12.

**Czy w ogóle warto wymieniać działający skrypt?** Logika: **nie warto**, jest poprawna
i właśnie została naprawiona w dwóch realnych miejscach. Opakowanie: **warto i to pilnie**,
bo cztery wady są prawdziwe i żadna nie wymaga zmiany logiki:
1. bash na hoście → nie odtworzysz go po przeinstalowaniu VPS-a;
2. poza GitOps → nie ma go w repo, nie ma historii zmian, nie ma review;
3. brak testów → progi i tłumienie to czysta arytmetyka, idealna do testów;
4. zależność od poda Dietetyka → monitoring ginie razem z monitorowanym.
Punkt 4 rozwiązuje własny `Secret` z kluczem Mailgun w namespace monitoringu — klucz nadal
nie leży na hoście, a monitoring przestaje zależeć od cudzej aplikacji.

---

## 12. CO KONKRETNIE ZROBIĆ (kolejność)

**Etap 1 — teraz, koszt 0 Mi:**
1. Obraz: `alpine` + `kubectl` + obecny skrypt. Chart w repo, wdrażany ArgoCD.
2. `CronJob` co 15 min, `concurrencyPolicy: Forbid`, własne `ServiceAccount` +
   `ClusterRole` (`get,list` na `nodes`, `nodes/proxy`, `pods`, `jobs`, `persistentvolumeclaims`,
   `certificates.cert-manager.io`, `applications.argoproj.io`).
3. Własny `Secret` z kluczem Mailgun → koniec z `kubectl exec` do cudzego poda.
4. Mały PVC (64 Mi) na plik stanu tłumienia — musi przeżyć restart poda.
5. Punkty 1–3 z `/proc/meminfo` i `/proc/loadavg` (host, za darmo), punkt 1 i 8 z kubelet
   Summary API.
6. Dopisz punkty 6, 7, 9, 10 — każdy to jeden `kubectl get`.
7. Testy jednostkowe progów i klucza tłumienia (dziś nie ma żadnych).
8. `curl` do healthchecks.io na końcu udanego przebiegu → punkt 12 za darmo.

**Etap 1b — koszt 0 Mi, osobno:** wypełnij `argocd-notifications-cm` triggerami
`on-sync-failed` i `on-health-degraded` → punkt 10 podwójnie zabezpieczony.

**Etap 2 — tylko jeśli właściciel zechce panelu:** kube-prometheus-stack z
`grafana.enabled` wg decyzji, retencja 7–14 dni, obowiązkowo metric relabeling wg przepisu
clux (odrzucenie kubeleta i histogramów). Budżet: `<512 Mi`. Przed wdrożeniem
**zmierz na miejscu** — moja liczba pochodzi z analogicznego, ale cudzego klastra.

---

## 13. CZEGO NIE USTALIŁEM (świadomie, zamiast szacować)
- Zużycie RAM Alertmanagera.
- Wiarygodne zużycie RAM prometheus-operatora (jest tylko raport z issue: ~420–440 MiB).
- Zużycie RAM VictoriaMetrics (vmsingle/vmagent/vmalert) na klastrze 1 węzeł / 34 pody.
- Zużycie RAM kube-state-metrics przy 34 podach (mam tylko rekomendację 250 MiB
  z benchmarku 100-węzłowego).
- Realny RSS Grafany na biegu jałowym (mam tylko oficjalny „floor" 512 MB).
- Czy `<512 Mi` z pomiaru clux obejmowało Grafanę.
- Realny RSS blackbox-exportera.
- Szczytowe zużycie RAM przez kontener `kubectl` w trakcie ~20-sekundowego przebiegu.
- Czy `kubernetes-event-exporter` ma sink SMTP (nie znalazłem — nie znaczy, że nie ma).
- Czy `Certificate.status.notAfter` w cert-managerze jest zawsze wypełnione (punkt 9 w opcji A)
  — do sprawdzenia jednym `kubectl get certificate -A -o yaml`.
