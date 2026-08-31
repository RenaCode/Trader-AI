-- 0017: przedział ufności dla odsetka dodatnich okien
--
-- DLACZEGO
--
-- Cztery kolejne dobowe przebiegi trenera, ten sam kod i to samo ziarno losowe,
-- różniące się wyłącznie przesunięciem okna o jeden dzień, dały:
--
--     52,5%  →  57,5%  →  60,0%  →  62,5%
--
-- Dziesięć punktów w trzy dni. Przy 40 oknach przedział ufności 95% dla odsetka
-- ma szerokość około 30 punktów procentowych, więc wszystkie te wartości są
-- statystycznie nierozróżnialne — podobnie jak większość porównań między
-- iteracjami, które wcześniej raportowałem jako poprawę albo pogorszenie.
--
-- Sam odsetek, podany bez przedziału, zachęca do czytania różnicy dwóch punktów
-- jako sygnału. Trzymamy więc granice obok wyniku i pokazujemy je wszędzie tam,
-- gdzie pokazujemy wynik.

ALTER TABLE learn.learning_state
    ADD COLUMN positive_ci_low  double precision,
    ADD COLUMN positive_ci_high double precision;

COMMENT ON COLUMN learn.learning_state.positive_ci_low IS
    'Dolna granica przedziału ufności 95% (Wilson) dla odsetka dodatnich okien.';
COMMENT ON COLUMN learn.learning_state.positive_ci_high IS
    'Górna granica przedziału ufności 95% (Wilson) dla odsetka dodatnich okien.';
