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
