-- Pokrycie sygnałów: jaka część budżetu wag faktycznie stała za decyzją.
--
-- PO CO. Do tej pory na pytanie „ile decyzji zapadło na NIEPEŁNYM komplecie
-- sygnałów" nie dało się odpowiedzieć wprost. Dawało się to policzyć okrężnie —
-- porównując wiersze `journal.decision_contribution` z wektorem wag
-- `learn.model_version.params->'weights'` dla reżimu decyzji — i taki rachunek
-- na oknie 27.08–09.09.2026 pokazał, że 686 z 776 decyzji (88%) miało niepełny
-- komplet, przy średnim pokryciu 0,893. Liczba tej wagi nie może wymagać
-- trzyzłączeniowego zapytania i znajomości wewnętrznej semantyki wag.
--
-- CZEMU DOPIERO TERAZ TO BOLI. Wcześniej brakujący sygnał ściągał sam WYNIK ku
-- zeru (jego waga zostawała w mianowniku ze score zero), więc niepełne pokrycie
-- było widać pośrednio — jako słabsze wyniki. Od momentu, w którym `combine()`
-- renormalizuje wynik po wadze faktycznie obecnej, wynik ma tę samą skalę przy
-- pełnym i przy niepełnym komplecie. To jest poprawne — próg wejścia jest
-- centylem rozkładu z panelu treningowego i musi porównywać wielkości w tej
-- samej skali — ale znaczy, że informacja „ile sygnałów za tym stało" znika
-- z wyniku i musi być zapisana OSOBNO. Inaczej decyzja oparta na jednym
-- sygnale wygląda w dzienniku dokładnie tak samo jak decyzja oparta na ośmiu.
--
-- NULL znaczy „decyzja sprzed tej migracji", a nie „pełne pokrycie".
ALTER TABLE journal.decision
    ADD COLUMN IF NOT EXISTS pokrycie_sygnalow double precision
        CHECK (pokrycie_sygnalow IS NULL
               OR pokrycie_sygnalow BETWEEN 0 AND 1);

COMMENT ON COLUMN journal.decision.pokrycie_sygnalow IS
    'Udział sumy |wag| sygnałów, które się policzyły, w sumie |wag| kompletu. '
    '1,0 = komplet; 0,85 = 15% budżetu wagi nie miało czym się odezwać.';
