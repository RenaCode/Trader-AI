"""Czy stałe wstawiane do zapytań pasują do TYPÓW KOLUMN z migracji.

DLACZEGO OSOBNY PLIK

Próba pożarowa kill-switcha padła na produkcji z komunikatem:

    invalid input syntax for type bigint: "proba-pozarowa"

Kod ustawiał `released_by = 'proba-pozarowa'`, a ta kolumna jest typu bigint
z kluczem obcym do app."user". To nie jest literówka — to cała klasa błędów,
której NIC w tym repozytorium nie łapało:

  * testy jednostkowe podają atrapy zamiast bazy, więc typ kolumny w nich
    nie istnieje;
  * przegląd kodu widzi napis w miejscu, gdzie napis wygląda sensownie
    („zwalniający nazwany z imienia") — schemat jest w innym pliku;
  * Postgres zgłasza to dopiero przy WIĄZANIU PARAMETRU, czyli w chwili
    wykonania, na produkcji.

Ostatnia własność jest tu najgroźniejsza. Zapytanie, które się nie wykonało,
zostawia stan w połowie: próba pożarowa dojechała do zwolnienia zdarzenia,
które sama wywołała, i tam zginęła — zostawiając AKTYWNY kill-switch. Mechanizm,
którego jedynym zadaniem było udowodnić, że bezpiecznik działa, zatrzymał
handel na własne życzenie i nie posprzątał.

CO TEN PLIK SPRAWDZA

Buduje mapę (schemat, tabela) -> (kolumna -> typ) czytając db/migrations/*.sql,
a potem parsuje każdy `UPDATE` i `INSERT` w services/ i porównuje STAŁE podane
jako parametry z typem kolumny, do której trafiają.

CZEGO NIE SPRAWDZA, świadomie

Wartości przekazywane przez zmienne. Ich typ jest znany dopiero w czasie
wykonania, a zgadywanie go z nazwy dawałoby fałszywe alarmy — a test, który
krzyczy bez powodu, po tygodniu jest wyłączany. Sprawdzane są stałe, bo to one
są w kodzie widoczne i to na stałej ten błąd powstał.

Detektor ma własny kanarek (`test_wykrywacz_LAPIE_znany_blad`): karmiony
zapytaniem sprzed poprawki musi je zgłosić. Bez tego cisza tego pliku nie
znaczyłaby „czysto", tylko „parser nic nie zrozumiał".
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
MIGRACJE = KORZEN / "db" / "migrations"
ZRODLA = KORZEN / "services"


# ------------------------------------------------------- typy kolumn z migracji

# Rodzaj, a nie dokładny typ: wystarczy do rozstrzygnięcia, czy dana stała ma
# szansę w tę kolumnę wejść, a nie rozsypuje się przy każdym `numeric(28,12)`.
RODZAJE = {
    "liczba_calk": {"smallint", "integer", "int", "int2", "int4", "int8",
                    "bigint", "serial", "bigserial", "smallserial"},
    "liczba": {"numeric", "decimal", "real", "float", "float4", "float8",
               "double precision"},
    "tekst": {"text", "citext", "varchar", "character varying", "character",
              "char", "name", "uuid"},
    "bool": {"boolean", "bool"},
    "czas": {"timestamptz", "timestamp", "timestamp with time zone",
             "timestamp without time zone", "date", "time", "interval"},
    "json": {"json", "jsonb"},
    "bajty": {"bytea"},
}

# Typy własne (enumy `ref.engine`, `journal.action`, domeny) — poza sprawdzeniem.
# Enum przyjmuje napis, domena może stać nad czymkolwiek, więc twierdzenie o nich
# byłoby zgadywaniem. Milczenie tutaj jest uczciwsze niż fałszywy alarm.
NIESPRAWDZANE = "inne"

RE_KOMENTARZ = re.compile(r"--[^\n]*")
RE_CREATE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)\s*\(", re.I)
# Jedno `ALTER TABLE` bywa listą kilku klauzul po przecinku (0017 dodaje tak dwie
# kolumny naraz), więc nazwa tabeli jest szukana OSOBNO od klauzul. Wzorzec
# wymagający sąsiedztwa `ALTER TABLE ... ADD COLUMN` gubił drugą i każdą kolejną
# — i była to cicha strata: kolumna nieznana schematowi jest po prostu
# pomijana przy sprawdzaniu, więc nic by o tym nie powiedziało.
RE_ALTER = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.\"]+)([^;]*);", re.I)
RE_ADD_COL = re.compile(
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+([^;,]+)", re.I)
RE_ALTER_TYP = re.compile(
    r"ALTER\s+(?:COLUMN\s+)?(\w+)\s+(?:SET\s+DATA\s+)?TYPE\s+([^;,]+)", re.I)

NIE_KOLUMNA = ("primary", "unique", "foreign", "check", "constraint",
               "exclude", "like", "partition")
KONIEC_TYPU = re.compile(
    r"\s+(NOT\s+NULL|NULL|DEFAULT|PRIMARY|REFERENCES|UNIQUE|CHECK|GENERATED"
    r"|COLLATE|CONSTRAINT)\b", re.I)


def _bez_komentarzy(sql: str) -> str:
    return RE_KOMENTARZ.sub("", sql)


def _nazwa(surowa: str) -> tuple[str, str]:
    """`risk.killswitch_event` -> ('risk', 'killswitch_event')."""
    czesci = [c.strip().strip('"') for c in surowa.split(".")]
    return (czesci[0], czesci[1]) if len(czesci) > 1 else ("public", czesci[0])


def _rodzaj(typ: str) -> str:
    t = re.sub(r"\(.*?\)", "", typ).strip().lower()
    t = re.sub(r"\[\s*\]", "", t).strip()          # tablice -> rodzaj elementu
    for rodzaj, nazwy in RODZAJE.items():
        if t in nazwy:
            return rodzaj
    return NIESPRAWDZANE


def _tnij_po_przecinkach(cialo: str) -> list[str]:
    """Rozbija ciało CREATE TABLE po przecinkach NA POZIOMIE ZEROWYM.

    Zwykłe `split(",")` rozcięłoby `numeric(28,12)` i `CHECK (x IN ('a','b'))`.
    """
    out, biezacy, glebokosc, cudzyslow = [], [], 0, None
    for znak in cialo:
        if cudzyslow:
            if znak == cudzyslow:
                cudzyslow = None
        elif znak in "'\"":
            cudzyslow = znak
        elif znak == "(":
            glebokosc += 1
        elif znak == ")":
            glebokosc -= 1
        elif znak == "," and glebokosc == 0:
            out.append("".join(biezacy))
            biezacy = []
            continue
        biezacy.append(znak)
    if "".join(biezacy).strip():
        out.append("".join(biezacy))
    return out


def _przecinki_zerowego_poziomu(tekst: str) -> int:
    """Ile przecinków oddzielających pozycje listy — z pominięciem zagnieżdżeń."""
    ile, glebokosc, cudzyslow = 0, 0, None
    for znak in tekst:
        if cudzyslow:
            if znak == cudzyslow:
                cudzyslow = None
        elif znak in "'\"":
            cudzyslow = znak
        elif znak == "(":
            glebokosc += 1
        elif znak == ")":
            glebokosc -= 1
        elif znak == "," and glebokosc == 0:
            ile += 1
    return ile


def _cialo(sql: str, od: int) -> str:
    """Zawartość nawiasu otwartego na pozycji `od-1`, z zachowaniem zagnieżdżeń."""
    glebokosc, start = 1, od
    for i in range(od, len(sql)):
        if sql[i] == "(":
            glebokosc += 1
        elif sql[i] == ")":
            glebokosc -= 1
            if glebokosc == 0:
                return sql[start:i]
    return sql[start:]


def zbuduj_schemat() -> dict[tuple[str, str], dict[str, str]]:
    """Mapa (schemat, tabela) -> (kolumna -> typ), czytana z migracji po kolei."""
    schemat: dict[tuple[str, str], dict[str, str]] = {}
    for plik in sorted(MIGRACJE.glob("*.sql")):
        sql = _bez_komentarzy(plik.read_text(encoding="utf-8"))

        for m in RE_CREATE.finditer(sql):
            tabela = _nazwa(m.group(1))
            kolumny = schemat.setdefault(tabela, {})
            for pozycja in _tnij_po_przecinkach(_cialo(sql, m.end())):
                pozycja = pozycja.strip()
                if not pozycja or pozycja.split()[0].lower() in NIE_KOLUMNA:
                    continue
                nazwa_kol = pozycja.split()[0].strip('"')
                reszta = pozycja[len(pozycja.split()[0]):].strip()
                koniec = KONIEC_TYPU.search(reszta)
                kolumny[nazwa_kol] = (reszta[:koniec.start()] if koniec
                                      else reszta).strip()

        for alter in RE_ALTER.finditer(sql):
            kolumny = schemat.setdefault(_nazwa(alter.group(1)), {})
            for wzorzec in (RE_ADD_COL, RE_ALTER_TYP):
                for m in wzorzec.finditer(alter.group(2)):
                    koniec = KONIEC_TYPU.search(m.group(2))
                    kolumny[m.group(1)] = (
                        (m.group(2)[:koniec.start()] if koniec
                         else m.group(2)).strip())

    return schemat


SCHEMAT = zbuduj_schemat()


# --------------------------------------------------- gdzie trafia który parametr

RE_CEL = re.compile(
    r"\b(?:UPDATE\s+(?:ONLY\s+)?([\w.\"]+)\s+SET"
    r"|INSERT\s+INTO\s+([\w.\"]+)"
    r"|DELETE\s+FROM\s+([\w.\"]+))", re.I)
# Kolumna tuż przed `%s`: `released_by = %s`, `k.id = %s`, `engine::text = %s`.
RE_PRZED = re.compile(
    r"(?:[\w\"]+\.)?([\w\"]+)\s*(?:::\s*([\w.]+)\s*)?(?:=|<>|!=|>=|<=|>|<)\s*$")
RE_VALUES = re.compile(r"\bVALUES\s*\(", re.I)


def miejsca_parametrow(sql: str) -> list[tuple[tuple[str, str], str, str | None]]:
    """Dla kolejnych `%s` zwraca (tabela, kolumna, rzutowanie) albo None.

    Kolejność ma znaczenie: parametry są pozycyjne, więc i-te `%s` odpowiada
    i-temu elementowi krotki. Miejsca nierozpoznane (`LIMIT %s`, `interval %s`)
    zajmują swój indeks jako None — pominięcie ich rozjechałoby całą resztę.
    """
    cel = RE_CEL.search(sql)
    if not cel:
        return []
    tabela = _nazwa(cel.group(1) or cel.group(2) or cel.group(3))

    # INSERT: kolumny biorą się z listy przed VALUES, nie z tekstu przed `%s`.
    kolumny_insert: list[str] = []
    zakres_values: tuple[int, int] | None = None
    if cel.group(2):
        lista = re.match(r"\s*\(", sql[cel.end():])
        if lista:
            kolumny_insert = [k.strip().strip('"') for k in
                              _cialo(sql, cel.end() + lista.end()).split(",")]
        v = RE_VALUES.search(sql, cel.end())
        if v:
            tresc = _cialo(sql, v.end())
            zakres_values = (v.end(), v.end() + len(tresc))

    out: list[tuple[tuple[str, str], str, str | None] | None] = []
    for m in re.finditer(r"%s", sql):
        if zakres_values and zakres_values[0] <= m.start() < zakres_values[1]:
            # W VALUES kolumnę wyznacza POZYCJA, a nie sąsiedztwo w tekście.
            # Przecinki liczone NA POZIOMIE ZEROWYM: `coalesce(%s, 0)` ma
            # przecinek w środku i przesunąłby całą resztę o jedną kolumnę.
            indeks = _przecinki_zerowego_poziomu(
                sql[zakres_values[0]:m.start()])
            out.append((tabela, kolumny_insert[indeks], None)
                       if indeks < len(kolumny_insert) else None)
            continue
        przed = RE_PRZED.search(sql[:m.start()])
        out.append((tabela, przed.group(1).strip('"'), przed.group(2))
                   if przed else None)
    return out


# ------------------------------------------------------------- zgodność stałej

BOOLOWSKIE = {"t", "f", "true", "false", "yes", "no", "on", "off", "1", "0"}


def niezgodnosc(wartosc, rodzaj: str) -> str | None:
    """Opis niezgodności stałej z rodzajem kolumny albo None, gdy pasuje.

    Kryterium: czy Postgres PRZYJMIE tę wartość w to miejsce. Napis jest
    wysyłany jako `unknown` i rzutowany przez bazę, więc `"12"` w bigincie jest
    poprawne, a `"proba-pozarowa"` nie. Liczba jedzie z własnym typem, więc do
    kolumny tekstowej nie wejdzie wcale.
    """
    if wartosc is None or rodzaj in (NIESPRAWDZANE, "json", "bajty", "czas"):
        return None

    if rodzaj in ("liczba_calk", "liczba"):
        if isinstance(wartosc, bool):
            return f"wartość logiczna {wartosc!r} w kolumnie liczbowej"
        if isinstance(wartosc, str):
            try:
                int(wartosc) if rodzaj == "liczba_calk" else float(wartosc)
            except ValueError:
                return (f"napis {wartosc!r} w kolumnie liczbowej — Postgres "
                        f"odrzuci go przy wiązaniu parametru")
        elif isinstance(wartosc, float) and rodzaj == "liczba_calk":
            return f"liczba zmiennoprzecinkowa {wartosc!r} w kolumnie całkowitej"
        return None

    if rodzaj == "bool":
        if isinstance(wartosc, bool):
            return None
        if isinstance(wartosc, str) and wartosc.lower() in BOOLOWSKIE:
            return None
        return f"{wartosc!r} w kolumnie logicznej"

    if rodzaj == "tekst":
        if isinstance(wartosc, str):
            return None
        return (f"{type(wartosc).__name__} {wartosc!r} w kolumnie tekstowej — "
                f"jedzie z własnym typem, więc baza nie zrzutuje go po cichu")

    return None


def _stale_zapytania(drzewo: ast.AST):
    """Wyławia (zapytanie, krotka parametrów) z wywołań w rodzaju `execute(sql, args)`."""
    for wezel in ast.walk(drzewo):
        if not isinstance(wezel, ast.Call):
            continue
        for i, arg in enumerate(wezel.args):
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                continue
            sql = arg.value
            # Bez `\b` na końcu: granica słowa postawiona za `\w` wypadałaby
            # W ŚRODKU nazwy tabeli i nie pasowała nigdy, więc wzorzec cicho
            # pomijał wszystkie UPDATE-y. Złapał to dopiero kanarek — sam
            # detektor milczał tak samo, jakby wszystko było w porządku.
            if "%s" not in sql or not re.search(
                    r"\b(UPDATE|INSERT\s+INTO)\s+[\w\"]", sql, re.I):
                continue
            nastepny = wezel.args[i + 1] if i + 1 < len(wezel.args) else None
            if isinstance(nastepny, (ast.Tuple, ast.List)):
                yield sql, nastepny.elts, wezel.lineno
            break


def znajdz_niezgodnosci(sql: str, parametry, linia=0, plik="") -> list[str]:
    """Lista opisów niezgodności dla jednego zapytania. Serce detektora."""
    braki = []
    for indeks, (miejsce, param) in enumerate(
            zip(miejsca_parametrow(sql), parametry)):
        if miejsce is None:
            continue
        tabela, kolumna, rzutowanie = miejsce
        if not (isinstance(param, ast.Constant)):
            continue          # zmienna — typ znany dopiero w czasie wykonania
        typ = ("text" if rzutowanie else SCHEMAT.get(tabela, {}).get(kolumna))
        if typ is None:
            continue          # kolumna spoza tej tabeli (JOIN, alias) — nie zgaduję
        opis = niezgodnosc(param.value, _rodzaj(typ))
        if opis:
            braki.append(
                f"{plik}:{linia} parametr {indeks + 1} -> "
                f"{tabela[0]}.{tabela[1]}.{kolumna} ({typ}): {opis}")
    return braki


# ------------------------------------------------------------------- twierdzenia

def test_zaden_zapis_nie_wklada_stalej_niezgodnej_z_TYPEM_KOLUMNY():
    """Główne twierdzenie tego pliku.

    Zapytanie odrzucone przy wiązaniu parametru nie zostawia po sobie „nic" —
    zostawia stan w połowie. Tak padła próba pożarowa: wywołała kill-switcha,
    nie zdołała go zwolnić i zostawiła zatrzymany handel.
    """
    braki = []
    for plik in sorted(ZRODLA.rglob("*.py")):
        drzewo = ast.parse(plik.read_text(encoding="utf-8"), filename=str(plik))
        for sql, parametry, linia in _stale_zapytania(drzewo):
            braki += znajdz_niezgodnosci(
                sql, parametry, linia, str(plik.relative_to(KORZEN)))
    assert not braki, "stałe niezgodne z typem kolumny:\n  " + "\n  ".join(braki)


def test_wykrywacz_LAPIE_znany_blad():
    """KANAREK DETEKTORA.

    Cisza testu wyżej znaczy „czysto" tylko wtedy, gdy parser w ogóle rozumie
    zapytania. Tu jest podane dokładnie to zapytanie, które padło na produkcji
    9 września 2026 — musi zostać zgłoszone.
    """
    sql = ("UPDATE risk.killswitch_event SET released_at = now(),"
           " released_auto = false, released_by = %s, release_note = %s"
           " WHERE id = %s")
    parametry = [ast.Constant("proba-pozarowa"), ast.Constant("notatka"),
                 ast.Constant(1)]
    braki = znajdz_niezgodnosci(sql, parametry, 159, "proba_killswitcha.py")
    assert len(braki) == 1, f"detektor nie widzi znanego błędu: {braki}"
    assert "released_by" in braki[0] and "bigint" in braki[0]


def test_wykrywacz_NIE_krzyczy_na_zapisie_poprawnym():
    """Kanarek do kanarka. Detektor zgłaszający wszystko jest bezużyteczny
    tak samo jak detektor milczący — po tygodniu i tak zostanie wyłączony."""
    sql = ("UPDATE risk.killswitch_event SET released_at = now(),"
           " released_auto = false, release_note = %s WHERE id = %s")
    assert not znajdz_niezgodnosci(
        sql, [ast.Constant("PRÓBA POŻAROWA"), ast.Constant(7)])


def test_schemat_zostal_ODCZYTANY_a_nie_tylko_wczytany():
    """Gdyby parser migracji przestał cokolwiek rozumieć, oba testy wyżej
    przechodziłyby z braku danych. To sprawdzenie mierzy sam parser."""
    assert SCHEMAT.get(("risk", "killswitch_event"), {}).get("released_by") \
        == "bigint", "parser migracji nie widzi typu released_by"
    assert _rodzaj(SCHEMAT[("risk", "killswitch_event")]["release_note"]) == "tekst"
    assert _rodzaj(SCHEMAT[("risk", "killswitch_event")]["released_auto"]) == "bool"
    assert _rodzaj(SCHEMAT[("risk", "limit_def")]["value"]) == "liczba"
    assert len(SCHEMAT) > 30, f"z migracji wyszło tylko {len(SCHEMAT)} tabel"


def test_zapisy_w_kodzie_SA_w_ogole_znajdowane():
    """Drugie zabezpieczenie przed cichą pustką.

    Gdyby wyławianie wywołań albo kojarzenie `%s` z kolumną przestało działać,
    główny test byłby pętlą po zbiorze pustym i przechodziłby ZAWSZE. Awaria
    detektora wygląda dokładnie tak samo jak brak błędów — to ta sama własność,
    dla której w ogóle istnieje próba pożarowa.
    """
    zapytania = miejsca = znane = 0
    for plik in sorted(ZRODLA.rglob("*.py")):
        drzewo = ast.parse(plik.read_text(encoding="utf-8"), filename=str(plik))
        for sql, args, _ in _stale_zapytania(drzewo):
            zapytania += 1
            for m in miejsca_parametrow(sql):
                miejsca += 1
                if m and (SCHEMAT.get(m[0], {}).get(m[1]) or m[2]):
                    znane += 1
    assert zapytania >= 40, f"znaleziono tylko {zapytania} zapisów z parametrami"
    assert miejsca >= 250, f"rozpoznano tylko {miejsca} miejsc na parametry"
    # Nie 100%: `LIMIT %s`, `interval %s`, `ANY(%s)` i podzapytania sięgające
    # innej tabeli nie stoją przy żadnej kolumnie i bez pełnego parsera SQL nie
    # da się ich rozstrzygnąć. Dziś rozpoznanych jest ~96%; próg jest niżej,
    # żeby nie padać na jednym nowym `LIMIT %s`. Ale spadek poniżej niego
    # znaczy, że parser przestał rozumieć schemat albo zapytania — a wtedy
    # milczy o wszystkim, wyglądając dokładnie jak brak błędów.
    assert znane / miejsca > 0.90, (
        f"tylko {znane} z {miejsca} parametrów trafiło do kolumny znanej "
        f"z migracji — parser schematu albo zapytań przestał działać")


@pytest.mark.parametrize("wartosc,rodzaj,ma_zglosic", [
    ("proba-pozarowa", "liczba_calk", True),
    ("12", "liczba_calk", False),
    (12, "liczba_calk", False),
    (1.5, "liczba_calk", True),
    (True, "liczba_calk", True),
    (7, "tekst", True),
    ("tekst", "tekst", False),
    (None, "liczba_calk", False),
    ("nie-bool", "bool", True),
    (False, "bool", False),
])
def test_regula_zgodnosci(wartosc, rodzaj, ma_zglosic):
    """Sama reguła, bez parsowania — żeby awaria wskazywała, co pękło."""
    assert bool(niezgodnosc(wartosc, rodzaj)) is ma_zglosic
