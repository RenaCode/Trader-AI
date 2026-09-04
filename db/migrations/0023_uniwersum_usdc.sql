-- 0023: uniwersum decyzyjne przechodzi z par USDT na USDC
--
-- DLACZEGO
--
-- Giełda, na której stoi konto, ma 4 pary *USDT i 113 par *USDC (0018). Cała
-- jej płynność jest w USDC, więc egzekucja pójdzie parami USDC — a kalibracja
-- szła dotąd po parach USDT. Trenowanie na jednym szeregu i handlowanie drugim
-- nie jest formalnością: zmierzona mediana bazy między nimi to 0,05–0,16%,
-- czyli jedna czwarta rundy kosztowej i ten sam rząd wielkości co przewaga,
-- której szukamy.
--
-- POMIAR, KTÓRY TO POPRZEDZIŁ
--
-- Te same 10 aktywów, te same okna, ten sam cennik i egzekucja, cztery
-- przesunięcia okien. Uniwersum USDC wobec USDT:
--
--     przesunięcie   USDT            USDC            USDT med.  USDC med.
--     1400 dni       28/39 (71,8%)   30/39 (76,9%)   +0,094     +0,160
--     1397 dni       26/39 (66,7%)   30/39 (76,9%)   +0,153     +0,111
--     1394 dni       27/40 (67,5%)   28/40 (70,0%)   +0,111     +0,131
--     1391 dni       28/40 (70,0%)   30/40 (75,0%)   +0,056     +0,129
--
-- USDC wyżej odsetkiem w 4 przesunięciach na 4 i medianą w 3 na 4. Przedziały
-- ufności zachodzą na siebie, więc uczciwy wniosek brzmi: uniwersum USDC NIE
-- JEST GORSZE — a jest tym, którym da się handlować. Sama wykonalność
-- wystarczyłaby za uzasadnienie; pomiar był po to, żeby nie zapłacić za nią
-- wynikiem.
--
-- CO ZOSTAJE
--
-- Pary USDT zostają `active`: mają głębszą historię (NEARUSDT 1638 dni wobec
-- 743 dla NEARUSDC), zbieramy dla nich świece dalej i to na nich stoi cały
-- dotychczasowy pomiar. Wypadają wyłącznie z uniwersum decyzyjnego.

UPDATE ref.instrument
   SET tradeable = (quote_ccy = 'USDC')
 WHERE engine = 'crypto' AND kind = 'spot' AND active;
