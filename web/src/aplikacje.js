/* Aplikacje RenaCode w przelaczniku z paska gornego (AppSwitcher.jsx).
   Jedyne miejsce, ktore trzeba zmienic, gdy dojdzie albo zniknie aplikacja.

   Wszystkie stoja za tym samym portalem Authelii, wiec przejscie w tej samej
   karcie nie wymaga ponownego logowania. Adresy tylko https - trafiaja wprost
   do `href`. `litera` i `kolor` rysuja kafelek bez zadnych zasobow z zewnatrz.

   `tylkoWlasciciel: true` - pozycja administracyjna, widoczna wylacznie dla
   roli `owner`. Rola pochodzi z `/api/auth/me` i jest juz po obcieciu SSO
   (services/api/auth.py, `_rola_wedlug_grupy`): sesja z grupy podgladu ma
   `viewer` nawet przy koncie `owner` w bazie. To tylko ukrycie linku -
   dostep do samego Argo CD pilnuje Authelia. */
export const BIEZACA = 'trader';

export const APLIKACJE = [
  { klucz: 'trader',   nazwa: 'Trader',   opis: 'Handel i ryzyko',
    adres: 'https://trader.renacode.com/',   litera: 'T', kolor: '#7c3aed' },
  { klucz: 'kidwatch', nazwa: 'Kidwatch', opis: 'Czas ekranowy dzieci',
    adres: 'https://kidwatch.renacode.com/', litera: 'K', kolor: '#06b6d4' },
  { klucz: 'dietetyk', nazwa: 'Dietetyk', opis: 'Dieta i zdrowie',
    adres: 'https://dietetyk.renacode.com/', litera: 'D', kolor: '#10b981' },
  { klucz: 'argocd',   nazwa: 'Argo CD',  opis: 'Wdrożenia i klaster',
    adres: 'https://argocd.renacode.com/',   litera: 'A', kolor: '#ef7b4d',
    tylkoWlasciciel: true },
];

/* Lista dla biezacej sesji. Wszystko poza rola dokladnie 'owner' (viewer,
   brak sesji, nieznana wartosc) traci pozycje administracyjne. */
export function aplikacjeDla(rola) {
  return APLIKACJE.filter((a) => !a.tylkoWlasciciel || rola === 'owner');
}
