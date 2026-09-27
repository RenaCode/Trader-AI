"""Atrapa kursora, która ZNA OGRANICZENIA BAZY — a nie tylko zapamiętuje SQL.

DLACZEGO TO JEST OSOBNY MODUŁ, A NIE KLASA W JEDNYM PLIKU TESTOWYM

Atrapa, która przyjmuje każdy `execute`, czyni każde naruszenie schematu
niewykrywalnym Z DEFINICJI: nie ma stanu, na którym mogłoby się wywrócić.
Testy przechodzą, a instrukcja, którą utrwalają, jest odrzucana przez bazę
dopiero na produkcji — czyli w jedynym miejscu, gdzie kosztuje.

W tym projekcie zdarzyło się to dwa razy tego samego dnia i oba razy znalazł to
audyt czytający kod, a nie pakiet testów:

  * `UPDATE journal.planned_order SET status='rejected'` — `CHECK` dopuszczał
    wtedy tylko pending/triggered/cancelled/expired. Odmowa złożenia jest
    ścieżką NORMALNĄ, więc wywracała całą transakcję cyklu: decyzje zapisane,
    ale ŻADEN plan nie szedł na giełdę. Dokładnie ta cisza, przed którą
    ostrzega docstring funkcji składającej.
  * drugi `INSERT INTO exec.order` dla tego samego planu — `UNIQUE (mode,
    client_order_id)`. Objaw nie wygląda na awarię, tylko na strategię, która
    weszła mocniej, niż zakładał risk kernel.

Obie klasy błędów mają wspólną cechę: kod i schemat wyglądają poprawnie
Z OSOBNA. Widać je dopiero, gdy ktoś zestawi je ze sobą — i to robi ta atrapa.

CZEGO TA ATRAPA NIE ROBI, ŚWIADOMIE

To NIE jest emulator Postgresa. Nie wykonuje `WHERE`, nie zna typów, nie
sprawdza kluczy obcych i nie oddaje wierszy, których nie podłożono ręcznie.
Zna dokładnie tyle, ile potrzeba, żeby dwie powyższe klasy błędów przestały
być niewidoczne: `CHECK ... IN (...)`, `UNIQUE`, `NOT NULL` i istnienie
kolumny — i tylko dla tabel, których dotykają testy egzekucji.

OGRANICZENIA CZYTAMY Z MIGRACJI, nie przepisujemy ich tutaj. Kopia schematu
w pliku testowym rozjechałaby się z bazą przy pierwszej migracji, a rozjazd
byłby niewidoczny — czyli powielilibyśmy ten sam błąd o piętro wyżej.
"""
from __future__ import annotations

import re
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
MIGRACJE = sorted((KORZEN / "db/migrations").glob("*.sql"))

# Tabele, dla ktorych atrapa pilnuje ograniczen. Instrukcje dotyczace innych
# tabel przechodza bez sprawdzenia - i to jest jawne, a nie milczace: test
# `test_atrapa_zna_ograniczenia_tabel_egzekucji` pilnuje tej listy.
TABELE = ("journal.planned_order", "exec.order", "exec.fill", "exec.position",
          # Dziennik likwidacji po kill-switchu (0065). Jest tu, bo to na jego
          # `CHECK (stan IN ...)` stoi rozroznienie szesciu wynikow likwidacji,
          # a `UNIQUE (killswitch_event_id, position_id)` JEST mechanizmem
          # idempotencji - literowka w nazwie stanu wywracalaby cykl silnika
          # dokladnie w chwili, w ktorej bezpiecznik ma zamykac pozycje.
          "exec.likwidacja")


class NaruszenieBazy(AssertionError):
    """Instrukcja, ktora prawdziwa baza by odrzucila.

    Dziedziczy z AssertionError celowo: to nie jest blad atrapy, tylko
    NIEZDANY TEST - kod zapisuje cos, czego schemat nie dopuszcza.
    """


# ----------------------------------------------------------------- schemat

def _blok_nawiasow(tekst: str, od: int) -> str:
    """Zawartosc nawiasu zaczynajacego sie na pozycji `od` (bez nawiasow)."""
    glebokosc, i, poczatek = 0, od, od + 1
    while i < len(tekst):
        if tekst[i] == "(":
            glebokosc += 1
        elif tekst[i] == ")":
            glebokosc -= 1
            if glebokosc == 0:
                return tekst[poczatek:i]
        i += 1
    raise ValueError("niedomkniety nawias w migracji")


def _podziel_po_przecinkach(tekst: str) -> list[str]:
    """Podzial po przecinkach NAJWYZSZEGO POZIOMU.

    Naiwne `split(",")` rozcina `numeric(28,12)` i `IN ('a','b')` - czyli
    dokladnie te miejsca, o ktore chodzi.
    """
    czesci, biezaca, glebokosc, apostrof = [], [], 0, False
    for znak in tekst:
        if apostrof:
            biezaca.append(znak)
            if znak == "'":
                apostrof = False
            continue
        if znak == "'":
            apostrof = True
            biezaca.append(znak)
        elif znak == "(":
            glebokosc += 1
            biezaca.append(znak)
        elif znak == ")":
            glebokosc -= 1
            biezaca.append(znak)
        elif znak == "," and glebokosc == 0:
            czesci.append("".join(biezaca).strip())
            biezaca = []
        else:
            biezaca.append(znak)
    if "".join(biezaca).strip():
        czesci.append("".join(biezaca).strip())
    return czesci


def _bez_komentarzy(sql: str) -> str:
    return "\n".join(re.sub(r"--.*$", "", w) for w in sql.splitlines())


def _wczytaj_schemat() -> tuple[dict, dict, dict, dict]:
    """Kolumny, CHECK-i, UNIQUE-i i NOT NULL-e z WSZYSTKICH migracji po kolei.

    Kolejnosc ma znaczenie: pozniejsza migracja nadpisuje wczesniejsza, wiec
    `ALTER TABLE ... ADD CONSTRAINT ... CHECK` z 0032 zastepuje zbior z 0005.
    Gdyby atrapa czytala samo 0005, utrwalalaby stan sprzed poprawki - czyli
    sama stalaby sie tym, przed czym ma chronic.
    """
    kolumny: dict[str, set[str]] = {}
    checki: dict[tuple[str, str], set[str]] = {}
    unikalne: dict[str, list[tuple[str, ...]]] = {}
    wymagane: dict[str, set[str]] = {}

    for plik in MIGRACJE:
        tresc = _bez_komentarzy(plik.read_text(encoding="utf-8"))

        for m in re.finditer(r"CREATE TABLE\s+(\w+\.\w+)\s*\(", tresc):
            tabela = m.group(1)
            cialo = _blok_nawiasow(tresc, m.end() - 1)
            kolumny.setdefault(tabela, set())
            unikalne.setdefault(tabela, [])
            wymagane.setdefault(tabela, set())
            for pozycja in _podziel_po_przecinkach(cialo):
                gora = pozycja.upper()
                if gora.startswith(("UNIQUE", "PRIMARY KEY")):
                    wnetrze = pozycja[pozycja.index("("):]
                    pola = tuple(p.strip() for p in
                                 _podziel_po_przecinkach(wnetrze[1:-1]))
                    unikalne[tabela].append(pola)
                    continue
                if gora.startswith(("CONSTRAINT", "CHECK", "FOREIGN KEY")):
                    continue
                nazwa = pozycja.split()[0]
                kolumny[tabela].add(nazwa)
                if "PRIMARY KEY" in gora:
                    unikalne[tabela].append((nazwa,))
                if "NOT NULL" in gora:
                    wymagane[tabela].add(nazwa)
                w = re.search(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)\s*\)", pozycja)
                if w:
                    checki[(tabela, w.group(1))] = set(
                        re.findall(r"'([^']*)'", w.group(2)))

        for m in re.finditer(
                r"ALTER TABLE\s+(?:IF EXISTS\s+)?(\w+\.\w+)\s+ADD (?:COLUMN\s+)?"
                r"(?:CONSTRAINT\s+\w+\s+)?(.*?);", tresc, re.S):
            tabela, reszta = m.group(1), m.group(2).strip()
            w = re.search(r"CHECK\s*\(\s*(\w+)\s+IN\s*\((.*?)\)\s*\)", reszta, re.S)
            if w:
                checki[(tabela, w.group(1))] = set(
                    re.findall(r"'([^']*)'", w.group(2)))
            elif reszta.upper().startswith("UNIQUE"):
                wnetrze = reszta[reszta.index("("):]
                unikalne.setdefault(tabela, []).append(
                    tuple(p.strip() for p in _podziel_po_przecinkach(
                        wnetrze[1:wnetrze.rindex(")")])))
            elif tabela in kolumny and re.match(r"^\w+\s+\w", reszta):
                kolumny[tabela].add(reszta.split()[0])

        for m in re.finditer(r"ALTER TABLE\s+(?:IF EXISTS\s+)?(\w+\.\w+)\s+"
                             r"DROP (?:COLUMN\s+)?(?:IF EXISTS\s+)?(\w+)", tresc):
            if m.group(1) in kolumny:
                kolumny[m.group(1)].discard(m.group(2))

    return kolumny, checki, unikalne, wymagane


KOLUMNY, CHECKI, UNIKALNE, WYMAGANE = _wczytaj_schemat()


# ---------------------------------------------------------------- instrukcje

class _Nieznane:
    """Wartosc, ktorej atrapa nie potrafi ustalic (np. `qty - %s`, `now()`).

    NIE UDAJEMY, ze ja znamy. Zgadniecie tutaj zamienialoby atrape w zrodlo
    falszywych alarmow, a falszywy alarm w tescie konczy sie jego wylaczeniem.
    """

    def __repr__(self):
        return "<nieznane>"


NIEZNANE = _Nieznane()


def _wartosc(wyrazenie: str, sql_przed: str, params):
    """Wartosc przypisania: literal, zbindowany parametr albo NIEZNANE."""
    w = wyrazenie.strip()
    if re.fullmatch(r"'[^']*'", w):
        return w[1:-1]
    if w.upper() == "NULL":
        return None
    if w == "%s":
        if params is None:
            return NIEZNANE
        i = sql_przed.count("%s")
        return params[i] if i < len(params) else NIEZNANE
    return NIEZNANE


def przypisania(sql: str, params) -> tuple[str | None, list[tuple[str, object]]]:
    """(tabela, [(kolumna, wartosc)]) dla INSERT-a albo UPDATE-a.

    Parametry sa POZYCYJNE, wiec wiazanie idzie po liczbie `%s` PRZED danym
    wyrazeniem - takze tych z klauzuli SET przy UPDATE. Liczenie ich osobno dla
    SET i WHERE rozjechaloby sie przy pierwszym `WHERE id=%s`.
    """
    plaski = " ".join(sql.split())

    m = re.match(r"INSERT INTO\s+(\w+\.\w+)\s*\(", plaski)
    if m:
        tabela = m.group(1)
        nazwy = [n.strip() for n in
                 _podziel_po_przecinkach(_blok_nawiasow(plaski, m.end() - 1))]
        v = plaski.upper().index(" VALUES", m.end())
        start = plaski.index("(", v)
        surowe = _podziel_po_przecinkach(_blok_nawiasow(plaski, start))
        out, kursor = [], start
        for nazwa, wyr in zip(nazwy, surowe):
            kursor = plaski.index(wyr, kursor)
            out.append((nazwa, _wartosc(wyr, plaski[:kursor], params)))
            kursor += len(wyr)
        return tabela, out

    m = re.match(r"UPDATE\s+(\w+\.\w+)\s+SET\s+", plaski, re.I)
    if not m:
        return None, []
    tabela = m.group(1)
    koniec = plaski.upper().find(" WHERE ")
    sekcja = plaski[m.end():] if koniec < 0 else plaski[m.end():koniec]
    out, kursor = [], m.end()
    for przypisanie in _podziel_po_przecinkach(sekcja):
        if "=" not in przypisanie:
            continue
        nazwa, wyr = przypisanie.split("=", 1)
        kursor = plaski.index(wyr.strip(), kursor) if wyr.strip() else kursor
        out.append((nazwa.strip(), _wartosc(wyr, plaski[:kursor], params)))
        kursor += len(wyr.strip())
    return tabela, out


class Kursor:
    """Atrapa kursora z ograniczeniami. Zapamiętuje SQL i sprawdza schemat.

    `wiersze` to odpowiedzi na kolejne `fetchone`/`fetchall` — atrapa NIE
    wykonuje `WHERE` i niczego nie wnioskuje z zapisanych wierszy.
    """

    def __init__(self, wiersze=None, *, sprawdzaj: bool = True):
        self.sql = []
        self._wiersze = list(wiersze or [])
        self._sprawdzaj = sprawdzaj
        self._klucze: set[tuple] = set()

    # ---- interfejs kursora
    def execute(self, sql, params=None):
        if self._sprawdzaj:
            self._sprawdz(sql, params)
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._wiersze.pop(0) if self._wiersze else None

    def fetchall(self):
        w, self._wiersze = self._wiersze, []
        return w

    def zawiera(self, fragment):
        return any(fragment in s for s, _ in self.sql)

    # ---- ograniczenia
    def _sprawdz(self, sql, params):
        tabela, pola = przypisania(sql, params)
        if tabela not in TABELE:
            return
        wstawienie = " ".join(sql.split()).upper().startswith("INSERT")
        znane = KOLUMNY.get(tabela, set())

        for nazwa, wartosc in pola:
            if znane and nazwa not in znane:
                raise NaruszenieBazy(
                    f"{tabela} nie ma kolumny {nazwa!r} — instrukcja nie "
                    f"wykonałaby się na prawdziwej bazie")
            if wartosc is NIEZNANE:
                continue
            if wartosc is None and nazwa in WYMAGANE.get(tabela, set()):
                raise NaruszenieBazy(
                    f"{tabela}.{nazwa} jest NOT NULL, a zapisywany jest NULL")
            dopuszczalne = CHECKI.get((tabela, nazwa))
            if dopuszczalne is not None and isinstance(wartosc, str) \
                    and wartosc not in dopuszczalne:
                raise NaruszenieBazy(
                    f"{tabela}.{nazwa} = {wartosc!r} łamie CHECK "
                    f"(dopuszczalne: {sorted(dopuszczalne)})")

        if not wstawienie:
            return
        wiersz = {n: w for n, w in pola}
        for klucz in UNIKALNE.get(tabela, []):
            if any(k not in wiersz or wiersz[k] is NIEZNANE for k in klucz):
                continue          # nie znamy calego klucza — nie zgadujemy
            odcisk = (tabela, klucz, tuple(str(wiersz[k]) for k in klucz))
            if odcisk in self._klucze:
                raise NaruszenieBazy(
                    f"{tabela}: drugi wiersz o tym samym "
                    f"UNIQUE {klucz} = {odcisk[2]} — prawdziwa baza odrzuciłaby "
                    f"ten INSERT i wywróciłaby całą transakcję cyklu")
            self._klucze.add(odcisk)
