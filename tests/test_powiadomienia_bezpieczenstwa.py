"""Zdarzenia bezpieczeństwa muszą być zgłaszane — i nie mogą blokować.

DLACZEGO TO JEST OSOBNY PLIK

Przegląd wszystkich zadeklarowanych alarmów (2026-09-06) pokazał, że jedenaście
z trzynastu typów zdarzeń nie było wysyłanych przez nikogo. Dwa z nich —
`auth.new_login` i `credential.changed` — są oznaczone jako `always_on`, czyli
projektant uznał, że użytkownik nie może ich wyłączyć. Mimo to nikt ich nigdy
nie podłączył.

System trzyma zaszyfrowane klucze API giełdy. Logowanie z nieznanego adresu
i dodanie klucza to dwa zdarzenia, których przeoczenie kosztuje najwięcej.
"""
import asyncio
import inspect
import re
from pathlib import Path

import pytest

from services.api.notifications import powiadom_o_bezpieczenstwie

AUTH = Path("services/api/routers/auth.py").read_text()
CRED = Path("services/api/routers/credentials.py").read_text()


def test_awaria_kanalu_nie_przerywa_operacji():
    """Mechanizm ostrzegania, który potrafi zablokować chronioną operację, jest
    gorszy niż jego brak: zamienia awarię kanału w awarię systemu.

    Bez bazy `notify()` rzuci wyjątkiem — i to jest tutaj treścią testu.
    """
    # asyncio.run zamiast markera pytest-asyncio: jeden test nie jest powodem,
    # zeby dokladac zaleznosc do zestawu testowego.
    asyncio.run(powiadom_o_bezpieczenstwie(1, "auth.new_login", "test", "test"))


def test_logowanie_zglasza_nowe_urzadzenie():
    assert "_zglos_nowe_urzadzenie(row[\"id\"], request)" in AUTH


def test_nowe_urzadzenie_nie_widzi_samego_siebie():
    """`audit()` zapisuje wpis PRZED sprawdzeniem, więc bez odcięcia świeżego
    wpisu każde logowanie znajdowałoby siebie jako wcześniejsze i nic nie
    zgłaszało — alarm, który nigdy nie zadziała, wygląda identycznie jak brak
    zagrożeń."""
    assert "at < now() - interval '1 second'" in AUTH


def test_zmiana_hasla_i_dodanie_klucza_sa_zglaszane():
    assert '"credential.changed", "Hasło zostało zmienione"' in AUTH
    assert '"credential.changed", "Dodano klucz API giełdy"' in CRED


def test_zgloszenia_ida_przez_wariant_niepodnoszacy_wyjatku():
    """W ścieżce żądania użytkownika wolno wołać wyłącznie wariant, który
    połyka błąd kanału."""
    for zrodlo, nazwa in ((AUTH, "auth.py"), (CRED, "credentials.py")):
        for linia in zrodlo.splitlines():
            l = linia.strip()
            if l.startswith("await notify("):
                pytest.fail(f"{nazwa}: bezpośrednie notify() w ścieżce żądania")


def test_helper_polyka_wyjatek_ale_go_nie_ukrywa():
    # Od 2026-10-03 wysylka idzie w tle (`create_task`), a wyjatek polyka
    # korutyna zadania - patrz `test_bezpieczenstwo_kont.py`.
    from services.api.notifications import _powiadom_bez_wyjatku
    zrodlo = inspect.getsource(_powiadom_bez_wyjatku)
    assert "except Exception" in zrodlo
    assert "print(" in zrodlo, "błąd kanału znika bez śladu w logu"


# --- regresja: wykrywanie nowego urzadzenia wywrocilo logowanie -------------
def test_wykrywanie_nowego_urzadzenia_jest_oslonione_w_calosci():
    """Osłonięcie samego `notify()` NIE WYSTARCZYŁO.

    Pierwsza wersja wołała `_zglos_nowe_urzadzenie()` wprost, ufając, że
    `powiadom_o_bezpieczenstwie` w środku połknie błąd. Ale ten wariant chroni
    wyłącznie ostatni krok — zapytanie, które go karmi, było nagie. Literówka
    w nazwie kolumny (`created_at` zamiast `at`) wywróciła logowanie błędem 500
    i nikt tego nie zauważył, bo test dymny w tym czasie nie działał.

    To dokładnie ta awaria, przed którą ostrzega komentarz przy helperze:
    mechanizm ostrzegania zablokował chronioną operację.
    """
    i = AUTH.index("await _zglos_nowe_urzadzenie(")
    kontekst = AUTH[max(0, i - 700):i]
    assert "try:" in kontekst, \
        "wywołanie wykrywania nie jest osłonięte — błąd w nim wywróci logowanie"


def test_zapytanie_o_audyt_uzywa_istniejacej_kolumny_czasu():
    """`app.audit_log` ma kolumnę `at`, nie `created_at` (migracja 0007)."""
    ddl = Path("db/migrations/0007_risk_and_app.sql").read_text()
    tabela = ddl[ddl.index("CREATE TABLE app.audit_log"):]
    tabela = tabela[:tabela.index(");")]
    assert "created_at" not in tabela and re.search(r"^\s+at\s+timestamptz", tabela, re.M)

    zapytanie = AUTH[AUTH.index("FROM app.audit_log"):]
    zapytanie = zapytanie[:zapytanie.index("LIMIT 1")]
    assert "created_at" not in zapytanie, \
        "zapytanie o app.audit_log używa nieistniejącej kolumny created_at"
