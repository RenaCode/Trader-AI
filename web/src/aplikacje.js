/* Aplikacje RenaCode w przelaczniku z paska gornego (AppSwitcher.jsx).
   Jedyne miejsce, ktore trzeba zmienic, gdy dojdzie albo zniknie aplikacja.

   Wszystkie stoja za tym samym portalem Authelii, wiec przejscie w tej samej
   karcie nie wymaga ponownego logowania. Adresy tylko https - trafiaja wprost
   do `href`. `litera` i `kolor` rysuja kafelek bez zadnych zasobow z zewnatrz. */
export const BIEZACA = 'trader';

export const APLIKACJE = [
  { klucz: 'trader',   nazwa: 'Trader',   opis: 'Handel i ryzyko',
    adres: 'https://trader.renacode.com/',   litera: 'T', kolor: '#7c3aed' },
  { klucz: 'kidwatch', nazwa: 'Kidwatch', opis: 'Czas ekranowy dzieci',
    adres: 'https://kidwatch.renacode.com/', litera: 'K', kolor: '#06b6d4' },
  { klucz: 'dietetyk', nazwa: 'Dietetyk', opis: 'Dieta i zdrowie',
    adres: 'https://dietetyk.renacode.com/', litera: 'D', kolor: '#10b981' },
];
