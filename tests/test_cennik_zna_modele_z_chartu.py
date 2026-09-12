"""Model wpisany w chart MUSI mieć stawkę w `CENNIK`. Inaczej dziennik kłamie.

DLACZEGO TO JEST PUŁAPKA, A NIE DROBIAZG

`Dostawca.koszt` liczy tak:

    inp, out = self.CENNIK.get(model, self.CENNIK_DOMYSLNY)

`CENNIK_DOMYSLNY` u Anthropica to (5.0, 25.0) — czyli DOKŁADNIE STAWKA OPUSA.
Model spoza tablicy nie wywraca niczego: wycenia się po najdroższym cenniku
i zapisuje do `news.api_spend` kwotę, która nie ma związku z rachunkiem.

Skutek jest odwrotny do intuicji. Zejście z opusa na tańszy model po to, żeby
oszczędzić, przy braku wpisu w `CENNIK` daje:
  * u dostawcy — realnie taniej,
  * w dzienniku — nadal stawka opusa, czyli do 5x za dużo,
  * w bramce dobowej — potok zatrzymany po ułamku faktycznych wydatków,
  * w `/api/api-spend` — liczba, po której nikt nie pozna, że jest zmyślona.

Nic tego nie zgłasza. To jest ten sam kształt, co prowizja odejmowana nie od
tego, od czego trzeba: wszystko działa, tylko liczba jest nieprawdziwa.

Tablica `CENNIK` ma dziś trzy wpisy Anthropica i nie zawiera ani `claude-fable-5`,
ani datowanych identyfikatorów w rodzaju `claude-haiku-4-5-20251001` — a to są
dokładnie te wartości, które ktoś wpisze do chartu przy zmianie modelu.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from services.ingest.llm import DostawcaAnthropic, DostawcaGemini

KORZEN = Path(__file__).resolve().parents[1]
WARTOSCI = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8"))

# Pole w `values.yaml` -> klasa dostawcy, której cennik ma je znać.
MODELE = {
    "analysisModel": DostawcaAnthropic,
    "triageModel": DostawcaAnthropic,
    "analysisModelGemini": DostawcaGemini,
    "triageModelGemini": DostawcaGemini,
}


@pytest.mark.parametrize("pole,dostawca", MODELE.items())
def test_model_z_chartu_ma_stawke_w_CENNIKU(pole, dostawca):
    model = WARTOSCI["llm"][pole]
    assert model in dostawca.CENNIK, (
        f"values.yaml -> llm.{pole} = {model!r}, a {dostawca.__name__}.CENNIK "
        f"go nie zna. Wyceni się po CENNIK_DOMYSLNY = {dostawca.CENNIK_DOMYSLNY}, "
        f"czyli dziennik `news.api_spend` i bramka dobowa policzą kwotę, której "
        f"nikt nie zapłacił. Dopisz stawkę razem ze zmianą modelu.")


def test_CENNIK_DOMYSLNY_jest_NAJDROZSZY_ze_znanych():
    """Domyśl ma być górnym oszacowaniem, a nie środkiem.

    Skoro model spoza tablicy i tak wycenia się domyślnie, to ta wycena musi
    ZAWYŻAĆ. Zaniżająca kazałaby potokowi wydawać dalej przy wyczerpanym
    budżecie — bramka przestałaby wiązać dokładnie wtedy, gdy jest potrzebna.
    """
    for dostawca in (DostawcaAnthropic, DostawcaGemini):
        if not dostawca.CENNIK:
            continue
        najdrozszy_we = max(c[0] for c in dostawca.CENNIK.values())
        najdrozszy_wy = max(c[1] for c in dostawca.CENNIK.values())
        we, wy = dostawca.CENNIK_DOMYSLNY
        assert (we, wy) >= (najdrozszy_we, najdrozszy_wy), (
            f"{dostawca.__name__}.CENNIK_DOMYSLNY = {(we, wy)} jest tańszy niż "
            f"najdroższy znany model {(najdrozszy_we, najdrozszy_wy)} — nieznany "
            f"model wyceniłby się ZA NISKO i przepuściłby wydatek przez bramkę")


def test_chart_i_env_example_nie_rozjezdzaja_sie_co_do_nazw_zmiennych():
    """Zmienna wystawiona w charcie, a nieczytana przez kod, to martwy wpis;
    czytana, a niewystawiona — cichy powrót do domyślu w `llm.py`."""
    helper = (KORZEN / "charts/trader/templates/_helpers.tpl").read_text(encoding="utf-8")
    kod = (KORZEN / "services/ingest/llm.py").read_text(encoding="utf-8")
    for zmienna in ("ANALYSIS_MODEL", "TRIAGE_MODEL", "ANALYSIS_MODEL_GEMINI",
                    "TRIAGE_MODEL_GEMINI", "LLM_DAILY_USD", "LLM_DAILY_CALLS"):
        assert f"name: {zmienna}" in helper, f"{zmienna} nie jest wystawiana przez chart"
        assert f'"{zmienna}"' in kod, f"{zmienna} nie jest czytana przez llm.py"
