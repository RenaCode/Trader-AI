-- Katalog zdarzeń obiecywał PSI powyżej 0,25. Ani miara nie jest PSI, ani próg
-- nie wynosi 0,25.
--
-- CO NAPRAWDĘ LICZY `train.py`:
--
--     d = |średnia_nowa - średnia_stara| / sd_stare + |sd_nowe / sd_stare - 1|
--
-- czyli standaryzowane przesunięcie średniej (d Cohena) powiększone o względną
-- zmianę rozrzutu. PSI to suma po kubełkach (p-q)*ln(p/q) — inna konstrukcja
-- i inna skala. Próg 0,25 pochodzi z literatury Siddiqiego o PSI i przeniesiony
-- na tę miarę NIE ZNACZY NIC, a wygląda na uzasadniony.
--
-- Skutek był konkretny i mylący w obie strony naraz: dashboard malował na
-- czerwono od 0,25, czujka odzywa się od 1,0. Właściciel widział alarmujący
-- kolor przy stanie, który system uznaje za normalny — i przy okazji uczył się
-- ignorować ten wykres.
--
-- Progi w treści zdarzenia są teraz TE SAME, które stosuje `dozor.py`
-- (PSI_PROG = 1.0 dla pojedynczego odczytu, DRYF_TRWALY_PROG = 0.5 dla stanu
-- utrzymującego się przez trzy przebiegi). Wyprowadzone ze skali Cohena, bo
-- taka jest natura tej miary: 0,2 mały, 0,5 średni, 0,8 duży.
--
-- NAZWA KOLUMNY `psi` ZOSTAJE i jest to świadoma decyzja: zmiana nazwy kolumny
-- w `learn.drift_metric` oraz `learn.model_version.max_psi` dotknęłaby zapisów
-- historycznych, API i wykresów naraz, a nie naprawia niczego, czego nie
-- naprawia prawdziwy opis. Miejsce, w którym nazwa mogłaby wprowadzić w błąd,
-- mówi teraz wprost, czym ta liczba jest.

UPDATE notify.event_type
   SET description = 'Rozkład cech odjechał od poprzedniego okresu. Miara to '
                     'wielkość efektu (d Cohena + względna zmiana rozrzutu), '
                     'NIE jest to PSI. Alarm od 1,0 przy pojedynczym odczycie; '
                     '0,5 utrzymujące się przez trzy przebiegi też jest '
                     'zgłaszane. Skuteczność modelu może spadać.'
 WHERE code = 'learning.drift_high';
