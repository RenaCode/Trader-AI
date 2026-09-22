"""Lista gield w UI i fabryka adapterow maja byc JEDNYM zrodlem prawdy."""
import pytest

from services.exchanges.fabryka import OBSLUGIWANE, zbuduj_adapter_gieldy


@pytest.mark.parametrize("kod", OBSLUGIWANE)
def test_kazda_obslugiwana_gielda_ma_adapter(kod):
    a = zbuduj_adapter_gieldy(kod, "k", "s", mode="demo", demo_host="https://x", live_host="https://y")
    assert a.venue_code == kod


def test_gielda_bez_adaptera_nie_jest_obslugiwana():
    assert "ibkr" not in OBSLUGIWANE
    with pytest.raises(KeyError):
        zbuduj_adapter_gieldy("ibkr", "k", "s", mode="demo", demo_host=None, live_host=None)


def test_lista_silnikow_daje_JEDEN_wpis_na_silnik_i_woli_gielde_z_adapterem():
    """22.09.2026: druga giełda equity w ref.venue dała DWIE zakładki „Giełda".
    Zapytanie ma grupować po silniku, a giełdę wybierać spośród obsługiwanych —
    atrapa bazy nie wykonuje DISTINCT ON, więc pilnujemy samego SQL-a."""
    import inspect

    from services.api import main

    zrodlo = inspect.getsource(main.engines)
    assert "DISTINCT ON (v.engine)" in zrodlo
    assert "OBSLUGIWANE" in zrodlo and "v.code = ANY(%s)" in zrodlo
