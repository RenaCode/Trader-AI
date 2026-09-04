-- 0021: pary USDC dziedziczą stawki finansowania po perpetuale tego samego aktywa
--
-- DLACZEGO
--
-- `funding_carry` to jeden z ośmiu sygnałów. Cecha liczy się ze stawek
-- finansowania kontraktu wieczystego podpiętego pod instrument spotowy przez
-- `meta.funding_from` (patrz services/ingest/funding.py). Nowo zarejestrowane
-- pary USDC tego podpięcia nie miały, więc cecha wychodziła pusta, a trener
-- wykluczyłby `funding_carry` jako sygnał martwy dla całego uniwersum USDC.
--
-- Utrata jednego z ośmiu sygnałów przy przejściu na USDC byłaby zmianą
-- strategii przemyconą jako zmiana waluty kwotowanej.
--
-- DLACZEGO WOLNO PODPIĄĆ PERPETUAŁ KWOTOWANY W USDT
--
-- Finansowanie jest własnością AKTYWA, nie waluty kwotowanej: opisuje, ile
-- płacą posiadacze pozycji długiej w bitcoinie, a nie w bitcoinie za dolara
-- z konkretnego emitenta. Perpetuał BTCUSDT.P jest najpłynniejszym miejscem,
-- gdzie ta stawka powstaje, i to jego historię mamy (39 tys. rekordów).
--
-- Zapisane wprost, bo to jest przybliżenie: gdyby kiedyś stawki na perpetuałach
-- kwotowanych w USDC istotnie rozjechały się z USDT, to podpięcie trzeba
-- przeliczyć, a nie odziedziczyć po cichu.

UPDATE ref.instrument u
   SET meta = u.meta || jsonb_build_object('funding_from', p.id,
                                           'funding_proxy', p.symbol)
  FROM ref.instrument p
 WHERE u.engine = 'crypto' AND u.quote_ccy = 'USDC' AND u.kind = 'spot'
   AND p.kind = 'perp' AND p.base_ccy = u.base_ccy AND p.quote_ccy = 'USDT';
