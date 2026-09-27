"""Zadanie zapisujące nie może chodzić na roli tylko do odczytu.

Migracja 0034 rozdzieliła role bazodanowe. Zaraz po jej wdrożeniu odświeżanie
statusu instrumentów zaczęło kończyć się `permission denied for table
instrument` — bo siedziało jako krok w czujce żywotności, która z założenia
chodzi na roli bez prawa zapisu. Błąd był ZAMASKOWANY przez `|| true`, więc
zadanie dalej kończyło się powodzeniem, a kolumna statusu zamarzła na ostatniej
znanej wartości i od tej chwili codziennie zapewniała, że wszystko handluje.

Wykryte dopiero z logu poda, 10 godzin później. Ten test wiąże jedno z drugim:
skrypt, który pisze do bazy, musi dostać rolę z prawem zapisu.
"""
import re
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
SZABLONY = KORZEN / "charts/trader/templates"

# Skrypty, ktore ZAPISUJA do bazy. Lista jest jawna, bo automatyczne zgadywanie
# po tresci pliku (czy jest tam UPDATE) dawaloby falszywe trafienia na
# zapytaniach w komentarzach i w dokumentacji.
ZAPISUJACE = ("services/ingest/instrumenty.py",)


# KOMENTARZ NIE JEST WYWOLANIEM.
#
# Skanowanie po surowej tresci szablonu liczylo WZMIANKE o skrypcie
# w uzasadnieniu jak jego uruchomienie - a te szablony sa uzasadnieniami gesto
# pokryte. 2026-09-27 zapalilo to `test_skrypt_zapisujacy_...` na zmianie, ktora
# nie dodala ani jednego wywolania, tylko komentarz tlumaczacy, dlaczego dla toru
# equity odswiezacza NIE MA.
#
# Zdjecie komentarzy nie moze niczego ukryc, i to jest tu warunek konieczny:
# wiersz za `#` nie jest przez powloke wykonywany, a komentarz Helma nie trafia
# nawet do manifestu. Przeciwna kolejnosc bledu (test uspokojony komentarzem)
# jest wiec niemozliwa, a ta jedna byla realna.
_KOMENTARZ_HELMA = re.compile(r"\{\{-?/\*.*?\*/-?\}\}", re.S)


def bez_komentarzy(tekst: str) -> str:
    tekst = _KOMENTARZ_HELMA.sub("", tekst)
    return "\n".join(w.split("#")[0] for w in tekst.splitlines())


def zadania_z_rolami() -> dict[str, tuple[str, str]]:
    """Nazwa pliku -> (polecenia kontenera, rola bazodanowa).

    Tresc jest OCZYSZCZONA Z KOMENTARZY - patrz `bez_komentarzy` wyzej.
    """
    wynik = {}
    for plik in SZABLONY.glob("*.yaml"):
        tresc = bez_komentarzy(plik.read_text(encoding="utf-8"))
        role = re.findall(r'"trader\.rolaDbEnv"\s*\(dict\s+"ctx"\s+\S+\s+"rola"\s+"(\w+)"', tresc)
        if role:
            wynik[plik.name] = (tresc, role)
    return wynik


def test_skrypt_zapisujacy_NIE_chodzi_na_roli_tylko_do_odczytu():
    """Rola `czujka` ma na wszystkich schematach wyłącznie SELECT (migracja
    0034). Zadanie, które pod nią uruchamia skrypt zapisujący, nie zadziała —
    a przy `|| true` nie powie o tym ani słowa."""
    for nazwa, (tresc, role) in zadania_z_rolami().items():
        for skrypt in ZAPISUJACE:
            if skrypt in tresc:
                assert "czujka" not in role, (
                    f"{nazwa} uruchamia {skrypt}, który ZAPISUJE do bazy, "
                    f"na rolach {role} — rola `czujka` nie ma prawa zapisu")


def test_odswiezanie_instrumentow_W_OGOLE_jest_uruchamiane():
    """Druga granica: samo usunięcie kroku z czujki „naprawiłoby" uprawnienia
    i zostawiło mechanizm wykrywania delistingu martwym. Kolumna statusu
    zamarłaby tak samo, tylko po cichu i na zawsze."""
    gdzies = [n for n, (t, _) in zadania_z_rolami().items()
              if "services/ingest/instrumenty.py" in t]
    assert gdzies, ("nic nie odświeża statusu instrumentów — kolumna zamarznie "
                    "na ostatniej wartości i będzie wyglądać jak potwierdzenie")


def test_odswiezanie_chodzi_CZESCIEJ_niz_prog_czujki():
    """Próg czujki to `PRZEGLAD_STATUSU_H` godzin. Gdy odświeżanie chodzi raz na
    dobę, JEDNA nieudana próba od razu przekracza próg i budzi człowieka.
    Odstęp musi zostawiać zapas na co najmniej jedno nieudane podejście."""
    from services.engine.dozor import PRZEGLAD_STATUSU_H

    values = (KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8")
    blok = re.search(r"^instrumentyCron:.*?^\s*schedule:\s*\"([^\"]+)\"",
                     values, re.M | re.S)
    assert blok, "brak instrumentyCron.schedule w values.yaml"
    pole_godzin = blok.group(1).split()[1]
    assert pole_godzin.startswith("*/"), (
        f"harmonogram {blok.group(1)!r} nie jest cykliczny w godzinach — "
        f"przy przebiegu dobowym jedna awaria przekracza próg {PRZEGLAD_STATUSU_H} h")
    co_godzin = int(pole_godzin[2:])
    assert co_godzin * 2 <= PRZEGLAD_STATUSU_H, (
        f"co {co_godzin} h przy progu {PRZEGLAD_STATUSU_H} h nie zostawia zapasu "
        f"na nieudane podejście")
