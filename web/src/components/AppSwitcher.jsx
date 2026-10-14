/* Przelacznik aplikacji RenaCode (siatka 3x3 w pasku gornym, jak w Jira).
   Lista aplikacji zyje w `../aplikacje.js`. Wzorzec menu WAI-ARIA: przycisk
   z aria-haspopup/aria-expanded, po otwarciu fokus na pierwszej pozycji,
   strzalki/Home/End przesuwaja fokus, Escape zamyka i oddaje fokus
   przyciskowi, klikniecie poza panelem albo Tab poza niego - zamyka.
   Biezaca aplikacja nie jest linkiem (aria-current, aria-disabled). */
import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { APLIKACJE, BIEZACA } from '../aplikacje';
import Icon from './Icon';

export default function AppSwitcher() {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const btnRef = useRef(null);
  const menuRef = useRef(null);
  const tytulId = useId();
  const menuId = useId();

  const pozycje = () => Array.from(menuRef.current?.querySelectorAll('[role="menuitem"]') || []);
  const fokus = (i) => {
    const xs = pozycje();
    if (xs.length) xs[(i + xs.length) % xs.length].focus();
  };

  const zamknij = useCallback((oddajFokus) => {
    setOpen(false);
    if (oddajFokus) btnRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    fokus(0);
    const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onEsc = (e) => { if (e.key === 'Escape') zamknij(true); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onEsc);
    };
  }, [open, zamknij]);

  const onMenuKey = (e) => {
    const xs = pozycje();
    const i = xs.indexOf(document.activeElement);
    const ruch = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: xs.length - 1 }[e.key];
    if (ruch === undefined) return;
    e.preventDefault();
    fokus(ruch);
  };

  // Tab poza panel zamyka menu - inaczej zostaje otwarte pod fokusem gdzie indziej.
  const onBlur = (e) => {
    if (open && ref.current && e.relatedTarget && !ref.current.contains(e.relatedTarget)) setOpen(false);
  };

  return (
    <div className="apps-wrap" ref={ref} onBlur={onBlur}>
      <button ref={btnRef} type="button" className="apps-btn"
              aria-label="Przełącz aplikację" aria-haspopup="menu"
              aria-expanded={open} aria-controls={open ? menuId : undefined}
              title="Przełącz aplikację"
              onClick={() => setOpen((x) => !x)}>
        <Icon name="apps" size={18} />
      </button>

      {open && (
        <div className="apps-panel glass-card">
          <div className="apps-title" id={tytulId}>Aplikacje RenaCode</div>
          <div className="apps-list" role="menu" id={menuId} aria-labelledby={tytulId} ref={menuRef}
               onKeyDown={onMenuKey}>
            {APLIKACJE.map((a) => {
              const biezaca = a.klucz === BIEZACA;
              const tresc = (
                <>
                  <span className="apps-tile" aria-hidden="true" style={{ background: a.kolor }}>
                    {a.litera}
                  </span>
                  <span className="apps-text">
                    <span className="apps-name">
                      {a.nazwa}
                      {biezaca && <span className="badge info">bieżąca</span>}
                    </span>
                    <span className="apps-desc">{a.opis}</span>
                  </span>
                </>
              );
              return biezaca ? (
                <div key={a.klucz} role="menuitem" tabIndex={-1} className="apps-item current"
                     aria-current="page" aria-disabled="true">
                  {tresc}
                </div>
              ) : (
                <a key={a.klucz} role="menuitem" tabIndex={-1} className="apps-item" href={a.adres}>
                  {tresc}
                </a>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
