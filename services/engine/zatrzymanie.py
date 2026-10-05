"""Łagodne zatrzymanie pętli silnika i dozorcy (audyt 04.10, T-9, pakiet P-T4).

Do 2026-10-05 `loop.py` i `dozorca.py` chodziły jako PID 1 (`exec python`) bez
żadnej obsługi SIGTERM-a. CPython kończy wtedy proces natychmiast, a kubelet
czeka domyślne 30 s i dobija SIGKILL-em — każdy sync Argo i każdy restart hosta
mógł przeciąć cykl w środku transakcji (decyzja zapisana, zlecenie nie; albo
odwrotnie).

SIGTERM USTAWIA ZDARZENIE, NIE PRZERYWA CYKLU. Cykl w toku dobiega końca
i domyka transakcję; pętla kończy się kodem 0 na najbliższej granicy:
przed następnym cyklem albo w trakcie czekania między cyklami. Dlatego
`terminationGracePeriodSeconds` w chartcie musi być dłuższy niż najdłuższy
cykl (`charts/trader/values.yaml`, `engine`, `dozorca`).

To NIE jest wzorzec z `proba_killswitcha.py` (`Task.cancel()`): tam zadanie
ma się przerwać natychmiast, a `finally` sprząta. Tu przerwanie cyklu jest
właśnie tym, czego chcemy uniknąć.
"""
from __future__ import annotations

import asyncio
import signal


class Zatrzymanie:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.sygnal: str | None = None

    def zarejestruj(self) -> "Zatrzymanie":
        """Podpina SIGTERM i SIGINT pod zdarzenie. Wołać z działającej pętli."""
        petla = asyncio.get_running_loop()
        for s in (signal.SIGTERM, signal.SIGINT):
            petla.add_signal_handler(s, self._ustaw, s.name)
        return self

    def _ustaw(self, nazwa: str) -> None:
        if self.sygnal is None:
            self.sygnal = nazwa
            print(f"{nazwa} — kończę między cyklami (cykl w toku dobiegnie końca)",
                  flush=True)
        self._stop.set()

    def ustaw(self) -> None:
        """Zatrzymanie bez sygnału (testy, wywołania wewnętrzne)."""
        self._ustaw("stop")

    @property
    def ustawione(self) -> bool:
        return self._stop.is_set()

    async def czekaj(self, sekundy: float) -> bool:
        """Śpi do `sekundy` albo do zatrzymania. True = trzeba kończyć.

        Sen idzie przez `asyncio.sleep`, nie przez `wait_for(timeout=)`, żeby
        testy podmieniające `asyncio.sleep` dalej przyspieszały pętlę.
        """
        if self._stop.is_set():
            return True
        sen = asyncio.ensure_future(asyncio.sleep(sekundy))
        stop = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({sen, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for z in (sen, stop):
                z.cancel()
        return self._stop.is_set()
