-- 0028: limity ryzyka wiązane z kapitałem, plus próg kapitału
--
-- POWÓD
--
-- Właściciel zapowiedział wpłatę 10 000 USD z warunkiem „rachunek nie ma wyjść
-- na minus". Sprawdzenie tego warunku odsłoniło coś, czego pytanie nie
-- dotyczyło: trzy z pięciu limitów były zapisane jako KWOTY wyliczone dla
-- rachunku 100 000 USD (`risk_rules.START_EQUITY`).
--
--     limit                      USD    % ze 100 000    % z 10 000
--     max_position_notional    5 000            5,0%         50,0%
--     max_total_exposure      15 000           15,0%        150,0%
--     budżet na pozycję        1 500            1,5%         15,0%
--
-- Na rachunku 10 000 limit łącznej ekspozycji to 150% kapitału. Na spocie bez
-- dźwigni nie da się tego przekroczyć, więc limit, który trzyma całą
-- konstrukcję, NIGDY BY SIĘ NIE AKTYWOWAŁ - a cały profil ryzyka byłby
-- dziesięciokrotnie wyższy niż ten, na którym mierzono wynik walk-forward.
--
-- Nie objawiłoby się to awarią. System handlowałby dalej i raportował zielone
-- statusy.
--
-- WARTOŚCI DOBRANE TAK, ŻEBY DZIŚ NIC SIĘ NIE ZMIENIŁO
--
-- 5% ze 100 000 to 5 000. 15% ze 100 000 to 15 000. Rachunek demonstracyjny ma
-- dokładnie 100 000, więc zmiana jednostki nie ma prawa zmienić zmierzonego
-- wyniku - i to jest sprawdzalne ponownym przebiegiem walk-forward.
--
-- PRÓG KAPITAŁU jest limitem NOWYM, nie przemianowanym.
--
-- Obsunięcie liczy się od SZCZYTU, a szczyt rośnie razem z rachunkiem. Przy
-- progu 10% podłoga to 0,9 x szczyt - dobre po wzroście, ale na starcie, gdy
-- szczyt równa się wpłacie, dopuszcza stratę 10% wpłaconych pieniędzy.
-- Próg kapitału mówi wprost „poniżej tej kwoty nie schodzimy", w jednostkach,
-- w których się wpłaca. Dlatego jest KWOTOWY i taki zostaje: próg wyrażony
-- procentem szczytu byłby dokładnie limitem obsunięcia, czyli duplikatem
-- wyglądającym na dodatkowe zabezpieczenie.
--
-- 95 000 to 5% poniżej kapitału startowego - ciaśniej niż limit obsunięcia
-- (10%), więc to on staje się wiążący. Zmierzone na 39 oknach: najgorszy punkt
-- ciągłej ścieżki kapitału to 99,97% wpłaty, a najgorsze obsunięcie wewnątrz
-- okna 2,85%. Próg 5% nie zadziałałby ani razu - jest siatką, nie kagańcem.

BEGIN;

UPDATE risk.limit_def SET unit = 'pct_equity', value = 5
 WHERE name = 'max_position_notional' AND unit = 'quote_ccy' AND value = 5000;

UPDATE risk.limit_def SET unit = 'pct_equity', value = 15
 WHERE name = 'max_total_exposure' AND unit = 'quote_ccy' AND value = 15000;

INSERT INTO risk.limit_def (engine, mode, name, scope, value, unit, action) VALUES
    ('crypto','demo','min_equity','engine',  95000, 'quote_ccy', 'killswitch'),
    ('equity','demo','min_equity','engine',  95000, 'quote_ccy', 'killswitch')
ON CONFLICT (engine, mode, name, scope, instrument_id) DO NOTHING;

COMMENT ON COLUMN risk.limit_def.unit IS
'Jednostka wartości limitu:
  pct        - procent (strata dzienna, obsunięcie od szczytu)
  pct_equity - procent KAPITAŁU; przelicza się w chwili oceny, więc ten sam
               zapis znaczy to samo na rachunku 100 000 i na 10 000
  quote_ccy  - kwota w walucie kwotowanej; do limitów, które z natury są kwotą
               (próg kapitału, minimum giełdowe)
  count      - liczba sztuk';

COMMIT;
