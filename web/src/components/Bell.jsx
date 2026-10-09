/* Dzwonek powiadomien w naglowku. */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { get, post } from '../utils/api';
import { fmtAgo } from '../utils/format';
import Icon from './Icon';

const TONE = { critical: 'bad', warning: 'warn', info: 'info' };

export default function Bell() {
  const [open, setOpen] = useState(false);
  const [count, setCount] = useState({ unread: 0, critical: 0 });
  /* ODCZYT, KTORY NIE DOSZEDL, NIE JEST ODCZYTEM ROWNYM ZERU.
     `count` startuje jako zero, a plakietka rysowala sie wylacznie przy
     `unread > 0`. Gdy endpoint zaczynal zwracac blad (baza niedostepna, 500),
     stan zostawal na zerze i dzwonek wygladal IDENTYCZNIE jak dzwonek bez
     powiadomien - w jedynym miejscu interfejsu, ktore ma krzyczec o
     kill-switchu i martwym silniku (`notify.event_type.always_on`, migracja
     0007). Odswiezanie co 30 s niczego nie zmienialo: kazdy kolejny blad byl
     rownie cichy. Panel po otwarciu to rozroznial, ale plakietka jest tym,
     na co sie patrzy, a nie klika. */
  const [countErr, setCountErr] = useState('');
  const [items, setItems] = useState(null);
  const ref = useRef(null);

  const refreshCount = useCallback(async () => {
    try {
      setCount(await get('/api/notifications/unread-count'));
      setCountErr('');
    } catch (e) {
      setCountErr(e?.message || 'Nie udało się odczytać licznika powiadomień');
    }
  }, []);

  useEffect(() => {
    refreshCount();
    const id = setInterval(refreshCount, 30000);
    return () => clearInterval(id);
  }, [refreshCount]);

  // Zamkniecie po kliknieciu poza panelem - bez tego panel zostaje otwarty
  // i zaslania nawigacje.
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onEsc = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onEsc);
    };
  }, [open]);

  const [error, setError] = useState('');
  const [listError, setListError] = useState('');

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next) {
      // Pusta lista po awarii klamala: panel mowil "Brak powiadomien", a
      // plakietka obok swiecila liczba nieprzeczytanych z osobnego zapytania.
      setListError('');
      setItems(null);
      try {
        setItems(await get('/api/notifications?limit=25'));
      } catch (e) {
        setListError(e?.message || 'Nie udało się pobrać powiadomień');
      }
    }
  };

  const markAll = async () => {
    setError('');
    try {
      await post('/api/notifications/read', { ids: null });
    } catch (e) {
      // Blad ma byc WIDOCZNY. Wczesniej wyjatek przerywal te funkcje przed
      // odswiezeniem licznika i przycisk wygladal na zepsuty bez slowa.
      setError(e?.message || 'Nie udało się oznaczyć powiadomień');
      return;
    }
    setItems((xs) => (xs || []).map((x) => ({ ...x, read: true })));
    await refreshCount();
  };

  return (
    <div className="bell-wrap" ref={ref}>
      <button className="bell-btn" onClick={toggle}
              aria-label={countErr ? 'Powiadomienia — licznik nieznany' : 'Powiadomienia'}
              aria-expanded={open}>
        <Icon name="bell" size={18} />
        {countErr ? (
          /* Plakietka stanu NIEZNANEGO zamiast jej schowania. Schowana znaczy
             "zero", a zera tu nikt nie zmierzył. */
          <span className="bell-badge nieznany" title={`Licznik powiadomień nieznany: ${countErr}`}>
            !
          </span>
        ) : count.unread > 0 && (
          <span className={`bell-badge ${count.critical > 0 ? 'critical' : ''}`}>
            {count.unread > 99 ? '99+' : count.unread}
          </span>
        )}
      </button>

      {open && (
        <div className="bell-panel glass-card">
          <div className="card-title" style={{ marginBottom: 10 }}>
            Powiadomienia
            {count.unread > 0 && (
              <button className="btn-link" style={{ width: 'auto', margin: 0, padding: '4px 8px' }}
                      onClick={markAll}>Oznacz wszystkie</button>
            )}
          </div>

          {countErr && (
            <div className="notice">
              Licznik nieprzeczytanych jest nieznany: {countErr}. Plakietka „!" przy
              dzwonku znaczy brak odczytu, a nie brak powiadomień.
            </div>
          )}
          {error && <div className="empty" style={{ color: 'var(--loss)' }}>{error}</div>}
          {listError ? (
            <div className="notice">
              Nie udało się wczytać powiadomień: {listError}
              {count.unread > 0 && (
                <><br />Licznik pokazuje {count.unread} nieprzeczytanych z wcześniejszego odczytu.</>
              )}
            </div>
          ) : items === null ? (
            <div className="empty loading-pulse">Wczytywanie…</div>
          ) : items.length === 0 ? (
            <div className="empty">Brak powiadomień</div>
          ) : (
            <div className="row-list no-fade" style={{ maxHeight: 420 }}>
              {items.map((n) => (
                <div key={n.id} className="row-item"
                     style={{ opacity: n.read ? 0.55 : 1,
                              borderLeft: `3px solid var(--${n.severity === 'critical' ? 'loss'
                                : n.severity === 'warning' ? 'color-orange' : 'color-blue'})` }}>
                  <div className="row-head">
                    <span className="row-title" style={{ fontSize: '0.85rem' }}>{n.title}</span>
                    <span className={`badge ${TONE[n.severity]}`}>{n.event_label}</span>
                  </div>
                  {n.body && <div className="row-body">{n.body}</div>}
                  <div className="row-meta">
                    <span>{fmtAgo(n.created_at)}</span>
                    {n.engine && <span>{n.engine}</span>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
