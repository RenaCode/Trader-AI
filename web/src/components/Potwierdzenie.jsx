/* OKNO POTWIERDZENIA operacji nieodwracalnej (audyt 2026-10-16).

   „Usuń" przy kluczu giełdy, kluczu usługi i portfelu wysyłało DELETE po jednym
   kliknięciu - chybiony klik zdejmował klucz, na którym silnik handluje. Okno
   NAZYWA usuwany obiekt, bo „Na pewno?" bez nazwy potwierdza się odruchowo.

   Własne okno, NIE `window.confirm()`: natywne okno blokuje kartę, nie da się
   go ostylować ani sprawdzić w stendzie, a przeglądarka pozwala je wyłączyć
   jednym zaznaczeniem („nie pokazuj więcej okien z tej strony") - po czym
   `confirm()` zwraca false albo true bez pytania.

   PORTAL DO <body>: `.glass-card` ma `backdrop-filter`, a to robi z karty blok
   zawierający dla `position: fixed` - okno wyrenderowane w karcie zakrywałoby
   tylko kartę. Fokus startuje na „Anuluj": Enter po otwarciu nie usuwa. */
import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';

export default function Potwierdzenie({
  tytul, children, etykieta = 'Usuń', zajete = false, onPotwierdz, onAnuluj,
}) {
  const anuluj = useRef(null);

  useEffect(() => {
    anuluj.current?.focus();
    const klawisz = (e) => { if (e.key === 'Escape' && !zajete) onAnuluj(); };
    document.addEventListener('keydown', klawisz);
    return () => document.removeEventListener('keydown', klawisz);
  }, [onAnuluj, zajete]);

  return createPortal(
    <div className="potwierdzenie-tlo" onMouseDown={(e) => {
      if (e.target === e.currentTarget && !zajete) onAnuluj();
    }}>
      <div className="glass-card potwierdzenie" role="alertdialog" aria-modal="true"
           aria-labelledby="potwierdzenie-tytul" aria-describedby="potwierdzenie-tresc">
        <div className="card-title" id="potwierdzenie-tytul">{tytul}</div>
        <div className="row-body" id="potwierdzenie-tresc">{children}</div>
        <div className="potwierdzenie-przyciski">
          <button ref={anuluj} className="btn" disabled={zajete} onClick={onAnuluj}>
            Anuluj
          </button>
          <button className="btn btn-danger" disabled={zajete} onClick={onPotwierdz}>
            {zajete ? 'Usuwanie…' : etykieta}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
