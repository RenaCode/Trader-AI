-- Plan moze zostac ODRZUCONY, zanim w ogole dotknie gieldy.
--
-- CHECK dopuszczal cztery stany: pending, triggered, cancelled, expired.
-- Wpiecie egzekucji dolozylo piaty przypadek faktyczny - plan, ktorego reguly
-- gieldy nie przepuszczaja (ilosc po zaokragleniu ponizej minimum, brak reguly
-- kroku, wartosc ponizej minOrderAmt). Kod zapisywal go jako 'rejected'
-- i wywracal sie na tym ograniczeniu.
--
-- SKUTEK BYL GORSZY NIZ SAM BLAD. Odmowa zlozenia jest sciezka NORMALNA, nie
-- awaryjna, wiec wywracala cala transakcje cyklu: decyzje byly juz zapisane,
-- ale ZADEN plan tego cyklu nie szedl na gielde i nie zostawal po tym slad.
-- Dokladnie ta cisza, przed ktora ostrzega docstring funkcji skladajacej.
--
-- DLACZEGO OSOBNY STAN, A NIE 'cancelled'. Anulowanie znaczy "wycofalismy
-- zlecenie" - robi to kill-switch. Odrzucenie znaczy "nigdy sie nie
-- zakwalifikowalo". Zlanie ich w jedno odbieraloby mozliwosc odpowiedzi na
-- pytanie "czy bezpiecznik zadzialal, czy tylko nie umielismy zlozyc zlecenia"
-- z samej tabeli planow.
ALTER TABLE journal.planned_order DROP CONSTRAINT IF EXISTS planned_order_status_check;
ALTER TABLE journal.planned_order ADD CONSTRAINT planned_order_status_check
    CHECK (status IN ('pending', 'triggered', 'cancelled', 'expired', 'rejected'));
