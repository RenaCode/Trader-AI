-- 0019: termin ważności klucza API jako dana, nie jako notatka w kalendarzu
--
-- DLACZEGO
--
-- Bybit unieważnia klucze API po trzech miesiącach, jeśli nie są przypięte do
-- adresu IP — klucze z przypiętym IP są bezterminowe. Konto projektu ma klucz
-- bez przypięcia, więc wygasa 91 dni po utworzeniu, czyli w połowie
-- sześciomiesięcznej fazy demonstracyjnej.
--
-- Wygaśnięcie klucza w trakcie fazy demo nie objawia się niczym widocznym:
-- egzekucja po prostu przestaje działać, a odczyt danych rynkowych idzie
-- z hosta publicznego i nie wymaga klucza, więc dashboard nadal wygląda zdrowo.
--
-- Termin trzymamy więc przy giełdzie, nie w czyjejś głowie. API liczy go
-- z `created_at` poświadczenia i pokazuje w ustawieniach razem z liczbą
-- pozostałych dni.
--
-- CZEGO NIE DA SIĘ TU SPRAWDZIĆ
--
-- Podmiot unijny nie wystawia `/v5/user/query-api` (patrz 0018), więc faktycznej
-- daty wygaśnięcia ani przypięcia IP nie da się odczytać z giełdy. Liczba
-- poniżej jest założeniem z regulaminu, nie odczytem — i jest założeniem
-- OSTROŻNYM: klucz przypięty do IP nie wygaśnie, więc ostrzeżenie pojawi się
-- wtedy niepotrzebnie, a nie za późno.

UPDATE ref.venue
   SET meta = meta || jsonb_build_object('waznosc_klucza_dni', 91)
 WHERE code = 'bybit';
