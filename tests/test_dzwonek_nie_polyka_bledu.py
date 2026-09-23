"""AWARIA LICZNIKA POWIADOMIEŃ NIE MOŻE WYGLĄDAĆ JAK „ZERO NIEPRZECZYTANYCH”.

CO BYŁO ZŁE

    const refreshCount = useCallback(async () => {
      try { setCount(await get('/api/notifications/unread-count')); } catch { /* cicho */ }
    }, []);

`count` startuje jako `{ unread: 0, critical: 0 }`, a plakietka rysowała się
wyłącznie przy `count.unread > 0`. Gdy endpoint zaczynał zwracać błąd (baza
niedostępna, 500), stan zostawał na zerze i dzwonek wyglądał IDENTYCZNIE jak
dzwonek bez powiadomień — w jedynym miejscu interfejsu, które ma krzyczeć
o kill-switchu i martwym silniku (`notify.event_type.always_on`, migracja
0007). Odświeżanie co 30 s niczego nie zmieniało: każdy kolejny błąd był
równie cichy.

Panel po otwarciu już to rozróżniał, ale plakietka jest tym, na co się patrzy,
a nie klika.

Odczyt, który nie doszedł, nie jest odczytem równym zeru — to ta sama zasada,
którą reszta tego interfejsu ma opisaną w `test_dane_bez_zer.py`. Dzwonek był
ostatnim miejscem, w którym nie obowiązywała.
"""
from __future__ import annotations

import re
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
UI = (KORZEN / "web/src/components/Bell.jsx").read_text(encoding="utf-8")
CSS = (KORZEN / "web/src/index.css").read_text(encoding="utf-8")


def _przycisk_dzwonka() -> str:
    """Treść `<button className="bell-btn" …>…</button>` — czyli to, co widać
    bez klikania. Cała wada mieści się w tym jednym fragmencie."""
    start = UI.index('className="bell-btn"')
    koniec = UI.index("</button>", start)
    return UI[start:koniec]


def test_blad_licznika_jest_ZAPAMIETANY_a_nie_polkniety():
    assert "countErr" in UI, (
        "błąd odczytu licznika nadal ginie w `catch { }` — a jedyne, co go "
        "odróżnia od zera, to właśnie on")
    assert not re.search(r"catch\s*\{\s*/\*\s*cicho\s*\*/\s*\}", UI), (
        "pusty `catch` przy odczycie licznika wciąż jest w kodzie")


def test_plakietka_POKAZUJE_SIE_przy_bledzie_odczytu():
    """Schowana plakietka znaczy „zero”, a zera tu nikt nie zmierzył."""
    przycisk = _przycisk_dzwonka()

    assert "countErr" in przycisk, (
        "plakietka nadal zależy wyłącznie od `count.unread`, więc awaria API "
        "rysuje dzwonek identycznie jak spokojna doba")


def test_stan_nieznany_wyglada_inaczej_niz_alarm_i_inaczej_niz_zero():
    """Trzy różne rzeczy, trzy różne wyglądy. Czerwień mówiłaby o zdarzeniu,
    o którym nic nie wiadomo; brak plakietki mówiłby, że nic się nie dzieje."""
    przycisk = _przycisk_dzwonka()

    assert "nieznany" in przycisk, "brak osobnego wyglądu dla stanu nieznanego"
    assert ".bell-badge.nieznany" in CSS, (
        "klasa plakietki stanu nieznanego nie ma stylu — plakietka byłaby "
        "nie do odróżnienia od zwykłego licznika")
    assert ".bell-badge.critical" in CSS, "styl alarmu krytycznego zniknął"


def test_udany_odczyt_KASUJE_stan_nieznany():
    """Plakietka „!”, która zostaje po powrocie API, nauczyłaby ją ignorować."""
    poczatek = UI.index("const refreshCount")
    # `useEffect` występuje też w imporcie na górze pliku, więc szukamy
    # WYWOŁANIA i dopiero od miejsca, w którym zaczyna się odświeżanie.
    odswiezanie = UI[poczatek:UI.index("useEffect(", poczatek)]

    assert re.search(r"setCountErr\(\s*''\s*\)", odswiezanie), (
        "udany odczyt nie kasuje zapamiętanego błędu")


def test_plakietka_licznika_dalej_dziala():
    """Kanarek: poprawka ma dołożyć stan nieznany, a nie zastąpić nim licznik."""
    przycisk = _przycisk_dzwonka()

    assert "count.unread > 0" in przycisk
    assert "count.critical > 0" in przycisk, (
        "wyróżnienie alarmu krytycznego zniknęło z plakietki")
    assert "99+" in przycisk
