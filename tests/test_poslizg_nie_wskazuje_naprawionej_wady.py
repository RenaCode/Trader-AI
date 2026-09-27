"""Kanarek: raport poslizgu nie moze wskazywac wady, ktorej JUZ NIE MA.

CO BYLO NIE TAK

Linia raportu dobowego (`dozor.py --co poslizg`) tlumaczyla zerowy poslizg
makera DWOMA przyczynami, z ktorych druga zostala usunieta 2026-09-26:

    "... a `loop.zapisz_wejscie` nadpisuje `exec.order.limit_price`
     cena wykonania. Stala pozostaje zalozeniem."

`loop.zapisz_wejscie` tego NIE ROBI od 2026-09-26 - galaz `order_id is not
None` rusza wylacznie `status`, `venue_order_id`, `updated_at` i `raw`,
a w kodzie stoi przy tym komentarz "`qty` I `limit_price` ZOSTAJA NIETKNIETE".

DLACZEGO TO NIE JEST CZEPIANIE SIE O TEKST

Ten komunikat jest JEDYNA rzecza, ktora czlowiek na temat poslizgu czyta -
raport nie wysyla powiadomienia i nie ma kodu wyjscia. Kierowal wiec
codziennie w strone naprawionej wady i ZASLANIAL wniosek, ktory z pomiaru
naprawde wynika: egzekucja sklada wylacznie `limit` z `PostOnly`, takie
zlecenie wypelnia sie po cenie limitu Z DEFINICJI, wiec poslizgu CENOWEGO
makera nie ma czego mierzyc - ani dzis, ani po tysiacu wypelnien. Koszt
makera siedzi CALY w niewypelnieniu.

Zmierzone na produkcji 2026-09-27: wszystkie 35 wypelnionych zlecen z limitem
(w tym cztery zlozone po poprawce z 2026-09-26) maja `exec.order.limit_price`
rowne `exec.fill.price` co do ostatniej cyfry - 0 wierszy roznych.

TEN TEST PADA NA KODZIE SPRZED POPRAWKI.
"""
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
POSLIZG = (KORZEN / "services/engine/poslizg.py").read_text(encoding="utf-8")
LOOP = (KORZEN / "services/engine/loop.py").read_text(encoding="utf-8")


def test_loop_NIE_nadpisuje_ceny_zlecenia():
    """Przeslanka calego tego pliku. Gdy ktos przywroci nadpisywanie, ten test
    upada PIERWSZY i mowi, ze komunikat nizej trzeba cofnac razem z nim."""
    assert "`qty` I `limit_price` ZOSTAJA NIETKNIETE" in LOOP


def test_raport_nie_oskarza_loop_zapisz_wejscie():
    """Wskazanie na naprawiona wade jest gorsze od milczenia: kieruje szukanie
    w miejsce, w ktorym nic nie ma."""
    assert "nadpisuje `exec.order.limit_price`" not in POSLIZG


def test_raport_mowi_ze_poslizgu_makera_NIE_MA_CZEGO_mierzyc():
    """Rozne zdania. "Stala pozostaje zalozeniem" znaczy "brakuje danych"
    i kaze czekac na wypelnienia. Prawda jest, ze czekanie nic nie da:
    PostOnly wypelnia sie po cenie limitu z definicji."""
    assert "Z DEFINICJI" in POSLIZG
    assert "NIEWYPEŁNIENIU" in POSLIZG


def test_raport_kieruje_do_wielkosci_ktora_DA_sie_zmierzyc():
    """Czytelnik ma wyjsc z tej linii wiedzac, gdzie patrzec dalej."""
    assert "wypełnialność z `exec.order`" in POSLIZG
