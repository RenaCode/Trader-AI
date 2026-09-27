"""Obie galezie `/state` oddaja TEN SAM zestaw kluczy, a panel czyta N, ktore bramkuje.

DLACZEGO TO ISTNIEJE

Dwie osobne wady, obie zmierzone 2026-09-27, obie o tym samym kształcie: panel
dowiadywal sie czegos innego niz robi system.

1. `services/api/routers/learning.py` ma w `state()` DWA `return` - jeden dla
   silnika ze stanem nauki, drugi dla silnika bez niego. Komentarz przy drugim
   mowi wprost, dlaczego musi miec KOMPLET kluczy z `null`: "w JS brakujacy
   klucz i klucz o wartosci `undefined` sa nieodroznialne, wiec front wypisywal
   doslownie 'undefined/undefined' - czyli brak pomiaru w przebraniu pomiaru".
   Regula byla zapisana w komentarzu i nic jej nie pilnowalo, wiec kazde nowe
   pole dodane tylko do jednej galezi przywracalo te wade.

2. Kafelek "Uczciwosc pomiaru" pokazywal `trials_run` (12 653 na produkcji)
   i konczyl zdaniem "Bez tej liczby deflated Sharpe jest nieobliczalny".
   Od 2026-09-27 to zdanie jest FALSZEM w obie strony: DSR jest liczony,
   i jawnie NIE z tego licznika - `reason` w bazie mowi doslownie "Licznik
   learn.hypothesis_trial NIE jest tu mianownikiem", bo jego kandydaci byli
   oceniani in-sample, a walk-forward juz za nich zaplacil. N bramkujace to
   `count(DISTINCT params)` po `learn.model_version` (58 dla toru 4h).

   Czytelnik panelu wyciagal wiec wniosek odwrotny do prawdy: ze system chroni
   sie przed selekcja liczba 12 653, podczas gdy chroni sie liczba 58.
"""
from __future__ import annotations

import re
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
ROUTER = KORZEN / "services" / "api" / "routers" / "learning.py"
PANEL = KORZEN / "web" / "src" / "components" / "Learning.jsx"


def _zestawy_kluczy() -> tuple[set[str], set[str]]:
    """Klucze obu `return`ow z `state()`. Bez atrapy - czytamy zrodlo."""
    zrodlo = ROUTER.read_text(encoding="utf-8")
    blok = zrodlo[zrodlo.index("async def state("):zrodlo.index('@router.get("/weights")')]
    i = blok.index("if not row:")
    j = blok.index("return {", i)
    bez_stanu = set(re.findall(r'"([a-z_]+)"\s*:', blok[j:blok.index("\n    }", j)]))
    j2 = blok.rindex("return {")
    ze_stanem = set(re.findall(r'"([a-z_]+)"\s*:', blok[j2:blok.index("\n    }", j2)]))
    return bez_stanu, ze_stanem


# Rownosci zbiorow kluczy NIE sprawdzam tutaj: robi to juz
# `test_dane_bez_zer.test_stan_nauki_bez_wiersza_oddaje_KOMPLET_kluczy`, i robi to
# LEPIEJ - wola endpoint dwa razy i porownuje zbiory, zamiast czytac zrodlo wzorcem.
# Druga kopia tej samej reguly rozjechalaby sie z pierwsza po cichu.


def test_state_oddaje_N_KTORE_BRAMKUJE_a_nie_tylko_licznik_hipotez():
    bez_stanu, _ = _zestawy_kluczy()
    assert "n_konfiguracji" in bez_stanu, (
        "`/state` nie oddaje `n_konfiguracji`, czyli liczby ROZROZNIALNYCH konfiguracji. "
        "Bez niej panel ma do pokazania wylacznie `trials_run`, ktory NIE jest mianownikiem "
        "poprawki na selekcje")
    assert "trials_run" in bez_stanu, (
        "`trials_run` ma ZOSTAC - jest uczciwym kontekstem, byle nie podawanym jako N")


def test_N_liczone_jest_TA_SAMA_wielkoscia_co_w_bramce():
    """Panel i bramka nie moga sie rozjechac, wiec licza to samo z tego samego zrodla."""
    router = ROUTER.read_text(encoding="utf-8")
    blok = router[router.index("async def state("):router.index('@router.get("/weights")')]
    assert "count(DISTINCT v.params)" in blok, (
        "N w API nie jest liczone jako `count(DISTINCT params)` po `learn.model_version`. "
        "Bramka w `train.n_konfiguracji` liczy dokladnie to - kazda inna wielkosc znaczy, "
        "ze panel pokazuje inna liczbe niz ta, ktora wstrzymuje promocje")
    assert "learn.model_version" in blok, "N musi pochodzic z `learn.model_version`"


def test_panel_pokazuje_N_BRAMKUJACE_jako_wartosc_glowna():
    panel = PANEL.read_text(encoding="utf-8")
    i = panel.index("Uczciwość pomiaru")
    kafelek = panel[i:i + 2200]
    # Kolejnosc ma znaczenie: wartosc glowna kafelka to ta, ktora czytelnik widzi pierwsza.
    assert kafelek.index("n_konfiguracji") < kafelek.index("trials_run"), (
        "`trials_run` stoi w kafelku PRZED `n_konfiguracji`, czyli jako wartosc glowna. "
        "To ta pierwsza liczba jest czytana jako miara uczciwosci pomiaru")


def test_panel_NIE_TWIERDZI_juz_ze_DSR_jest_bez_tego_licznika_nieobliczalny():
    panel = PANEL.read_text(encoding="utf-8")
    # Zdanie sprawdzane po TRESCI, nie po obecnosci slowa: chodzi o twierdzenie
    # "bez tej liczby DSR jest nieobliczalny", ktore od 2026-09-27 jest falszem.
    spojne = re.sub(r"\s+", " ", panel)
    assert "Bez tej liczby <em>deflated Sharpe</em> jest nieobliczalny" not in spojne, (
        "panel nadal twierdzi, ze bez licznika hipotez DSR jest nieobliczalny. DSR jest "
        "liczony i jawnie NIE z tego licznika - to zdanie mowi czytelnikowi odwrotnosc "
        "tego, co robi system")
    assert "to NIE jest N" in spojne, (
        "kafelek nie mowi wprost, ze licznik hipotez nie jest N. Sama zmiana kolejnosci "
        "nie wystarcza - czytelnik musi wiedzieć, ktora liczba bramkuje")
