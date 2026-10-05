{{/*
BRAMKA MIGRACJI - kontener startowy `czekaj-na-migracje`.

Jedna definicja dla workerow (workers.yaml - tam pelne uzasadnienie: dlaczego
tylko czyta i dlaczego bierze WYLACZNIE role swojego procesu) i dla trenera
(trainer-cron.yaml, poprawka A z 2026-10-05). Dwie kopie tego skryptu
rozjechalyby sie przy pierwszej zmianie limitu albo numeru NADANIE, a rozjazd
bylby widoczny dopiero w dniu, w ktorym jedna z nich przestanie czekac.

Wolanie: include "trader.bramkaMigracji" (dict "ctx" $ "rola" "<rola procesu>")
| nindent <wciecie listy initContainers>. Rola MUSI byc ta sama, co kontenera
roboczego - pilnuja tego tests/test_bramka_migracji.py
i tests/test_chart_zadania_cykliczne.py.
*/}}
{{- define "trader.bramkaMigracji" -}}
{{- $ctx := .ctx -}}
- name: czekaj-na-migracje
  image: "{{ $ctx.Values.api.image.repository }}:{{ $ctx.Values.api.image.tag }}"
  imagePullPolicy: {{ $ctx.Values.api.image.pullPolicy }}
  {{- include "trader.kontenerBezUprawnien" $ctx | nindent 2 }}
  env:
    {{/*
    TA SAMA ROLA, CO WORKER - i nic poza nia. Ani helpera od hasel
    wszystkich rol, ani hasla wlasciciela, ani hasla roli API.
    Pilnuje tego tests/test_bramka_migracji.py.
    */}}
    {{- include "trader.rolaDbEnv" (dict "ctx" $ctx "rola" .rola) | nindent 4 }}
    - name: PYTHONUNBUFFERED
      value: "1"
  command:
    - python
    - -c
    - |
      import os, pathlib, re, sys, time
      import psycopg

      # LIMIT CZASU - 600 s, i to jest liczba dobrana, nie zaokraglona.
      #
      # Gorna granica bierze sie z czujki pulsu: prog martwego workera
      # ma podloge 900 s (dozor.py, MIN_PROG_S), wiec czekanie ponizej
      # 600 s nie wywola falszywego alarmu, a wyczerpanie limitu
      # przeciwnie - pod pada, puls milknie i czujka zglasza to sama,
      # bez drugiego mechanizmu powiadamiania w tym pliku.
      #
      # Dolna granica: 120 s juz raz zabraklo o 10 s, bo start po
      # awarii trwa dluzej niz start czysty - baza moze wlasnie
      # odtwarzac sie po restarcie, a migrator stosowac kilka plikow
      # po kolei.
      #
      # Przekroczenie NIE JEST nieodwracalne: kubelet ponawia kontener
      # startowy z narastajacym odstepem, wiec za krotki limit kosztuje
      # widoczny CrashLoopBackOff, a brak limitu - niewidoczne czekanie
      # bez konca. `Recreate` znaczy, ze stary pod juz nie dziala, wiec
      # czekanie w nieskonczonosc byloby cisza, nie bezpieczenstwem.
      LIMIT_S = 600
      OKRES_S = 5

      # Numer migracji, ktora nadaje rolom procesow SELECT na
      # public.schema_migrations. Nie jest progiem ani nastawa - jest
      # nazwa jednego, niezmiennego pliku. Zgodnosc z rzeczywistoscia
      # pilnuje tests/test_bramka_migracji.py.
      NADANIE = 50

      def numer(tekst):
          m = re.match(r"(\d+)", tekst)
          return int(m.group(1)) if m else -1

      pliki = sorted(pathlib.Path("db/migrations").glob("*.sql"))
      if not pliki:
          sys.exit("Brak db/migrations w obrazie - bramka nie ma czego porownac.")
      wymagana = max(numer(p.stem) for p in pliki)

      url = os.environ.get("DATABASE_URL")
      if not url:
          sys.exit("DATABASE_URL nie jest ustawiony - bramka nie ma o co zapytac.")

      koniec = time.monotonic() + LIMIT_S
      powod = "brak polaczenia z baza"
      while time.monotonic() < koniec:
          try:
              with psycopg.connect(url, connect_timeout=5) as conn:
                  wolno = conn.execute(
                      "SELECT has_table_privilege(current_user,"
                      " 'public.schema_migrations', 'SELECT')").fetchone()[0]
                  if not wolno:
                      # Pytanie o prawo idzie PRZED zapytaniem, bo
                      # odroznia dwa stany nie do odroznienia po samym
                      # bledzie "permission denied":
                      #
                      #  * migracja NADANIE jeszcze nie zostala
                      #    zastosowana, ale jest w tym obrazie - czyli
                      #    baza nie dogonila kodu. Czekamy, o to chodzi.
                      #
                      #  * tego obrazu migracja NADANIE w ogole nie
                      #    obejmuje - czyli szablon chartu wyprzedza
                      #    obraz (ArgoCD synchronizuje `charts/` od razu,
                      #    a tag podbija sie osobno). Bramka nie ma
                      #    wtedy JAK zmierzyc czegokolwiek, a jej
                      #    zamkniecie poloczyloby dziewiec workerow -
                      #    petle decyzyjna i dozorce ryzyka - z powodu
                      #    kolejnosci wdrozenia, a nie rozjazdu schematu.
                      #    Przepuszczamy TEN SAM kod, ktory chodzil
                      #    minute temu, i mowimy o tym glosno.
                      if wymagana < NADANIE:
                          print("USTEPUJE: obraz nie zawiera migracji %d,"
                                " wiec bramka nie odczyta wersji schematu."
                                " Szablon chartu wyprzedza obraz - podbij"
                                " api.image.tag." % NADANIE, flush=True)
                          sys.exit(0)
                      powod = ("rola nie ma jeszcze SELECT na"
                               " public.schema_migrations (migracja %d"
                               " niezastosowana)" % NADANIE)
                  else:
                      numery = [numer(r[0]) for r in conn.execute(
                          "SELECT version FROM public.schema_migrations")]
                      zastosowana = max(numery) if numery else -1
                      if zastosowana >= wymagana:
                          print("Baza na %d, obraz wymaga %d - startuje."
                                % (zastosowana, wymagana), flush=True)
                          sys.exit(0)
                      powod = ("baza na %d, obraz wymaga %d"
                               % (zastosowana, wymagana))
          except Exception as blad:
              powod = "%s: %s" % (type(blad).__name__, blad)
          print("Czekam - " + powod, flush=True)
          time.sleep(OKRES_S)

      # Blad, nie `exit 0`. Pod, ktory wstal po cichu na starym
      # schemacie, jest dokladnie ta awaria, ktorej ten kontener ma
      # zapobiec - tyle ze niewidoczna.
      sys.exit("Migracje nie dogonily obrazu w %d s (%s)."
               " Pod NIE wstanie na starym schemacie." % (LIMIT_S, powod))
{{- end }}
