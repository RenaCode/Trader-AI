/* Zakladka RYZYKO: limity, ich wykorzystanie, naruszenia, kill-switch. */
import React, { useState } from 'react';
import { useApi, post } from '../utils/api';
import { fmtNum, fmtTime, fmtAgo } from '../utils/format';

const LIMIT_PL = {
  max_daily_loss: 'Maks. strata dzienna',
  max_drawdown: 'Maks. obsunięcie kapitału',
  max_position_notional: 'Maks. wielkość pozycji',
  max_total_exposure: 'Maks. łączna ekspozycja',
  max_orders_per_hour: 'Maks. zleceń na godzinę',
  min_equity: 'Próg kapitału',
  reczny: 'Ręczne zatrzymanie',
};
/* Jednostke opisuje SILNIK, w polu `unit`. Poprzednia stala mapowala
   `quote_ccy` na 'USD' i byla nieprawda dla rachunku krypto rozliczanego
   w USDC - a jednostka podpisana zla waluta zmienia znaczenie kazdej liczby
   na tej zakladce. API nie podaje, KTORA to waluta, wiec tu tez jej nie
   zgadujemy: mowimy dokladnie tyle, ile wiemy. */
const JEDNOSTKA_PL = {
  pct: '%', pct_equity: '% kapitału', count: 'szt.',
  quote_ccy: 'w walucie rozliczenia',
};
const jednostka = (u) => (u ? JEDNOSTKA_PL[u] || u : '');
const ACTION_PL = { block: 'blokada', downsize: 'zmniejszenie', killswitch: 'kill-switch' };
// Regula wznowienia jest czescia definicji limitu. Bez niej interfejs mowi,
// ze system sie zatrzyma, i milczy o tym, czy sam wroci - a to pierwsze pytanie,
// jakie zadaje sie o autonomiczny bezpiecznik.
const WZNOWIENIE_PL = {
  reczne: { etykieta: 'tylko ręcznie', opis: 'granica kapitału — automat jej nie zwalnia' },
  po_ustaniu: { etykieta: 'sam, po ustaniu', opis: 'limit okresowy — mija razem z okresem' },
  po_odbudowie: { etykieta: 'sam, po odbudowie', opis: 'wznowienie poniżej połowy limitu' },
};

/* Historia kill-switcha jest ucinana limitem po stronie API (domyslnie 10).
   Front limitu nie podawal i szukal aktywnego bezpiecznika wsrod DZIESIECIU
   NAJNOWSZYCH zdarzen - starszy, wciaz trzymajacy silnik, wypadal poza okno
   i formularz zwolnienia znikal, choc bezpiecznik dalej dzialal. 100 to gorna
   granica przyjmowana przez API. */
const KS_LIMIT = 100;

function ZwolnijKillSwitch({ engine, zdarzenie, onZwolniony }) {
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  return (
    <div className="notice">
      <strong>Kill-switch aktywny.</strong> {zdarzenie.reason} — uruchomiony{' '}
      {fmtAgo(zdarzenie.triggered_at)}.
      Odblokowanie wymaga ręcznego potwierdzenia; automatyczne wznowienie po obsunięciu
      kapitału jest najprostszą drogą do powtórzenia tej samej straty.

      <div style={{ marginTop: 12 }}>
        <label className="field" style={{ marginBottom: 8 }}>
          <span className="field-label">
            Powód zwolnienia — co się zmieniło, żeby to się nie powtórzyło?
          </span>
          <input className="input-field" value={note} maxLength={500}
                 placeholder="np. przyczyną był błąd w danych świec, poprawiony w sha-…"
                 onChange={(e) => { setNote(e.target.value); setErr(null); }} />
        </label>
        <button className="btn" disabled={busy || note.trim().length < 10}
                onClick={async () => {
                  setBusy(true); setErr(null);
                  try {
                    await post(`/api/engines/${engine}/risk/killswitch/${zdarzenie.id}/release`,
                               { note: note.trim() });
                    setNote('');
                    onZwolniony?.();
                  } catch (e) {
                    setErr(e.message || 'Nie udało się zwolnić');
                  } finally { setBusy(false); }
                }}>
          {busy ? 'Zwalniam…' : 'Zwolnij kill-switch'}
        </button>
        {note.trim().length > 0 && note.trim().length < 10 && (
          <span className="hint" style={{ marginLeft: 10 }}>
            powód musi mieć co najmniej 10 znaków
          </span>
        )}
        {err && <div className="form-error" style={{ marginTop: 8, marginBottom: 0 }}>{err}</div>}
      </div>
    </div>
  );
}

/* RECZNY KILL-SWITCH - ta sama sciezka co automat (wiersz w
   risk.killswitch_event), z haslem jak kazda operacja wrazliwa. Formularz jest
   zwiniety za przyciskiem: zatrzymanie handlu nie moze byc jednym przypadkowym
   kliknieciem, a stale otwarte pole hasla kusiloby menedzera hasel. Tryb
   (demo/live) wybiera serwer z pulsu silnika. */
function WlaczKillSwitch({ engine, onWlaczony }) {
  const [otwarty, setOtwarty] = useState(false);
  const [powod, setPowod] = useState('');
  const [haslo, setHaslo] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const zamknij = () => { setOtwarty(false); setPowod(''); setHaslo(''); setErr(null); };

  if (!otwarty) {
    return (
      <div className="glass-card">
        <div className="card-title">
          Ręczny kill-switch
          <span className="hint">zatrzymuje otwieranie pozycji do ręcznego zwolnienia</span>
        </div>
        <button className="btn" onClick={() => setOtwarty(true)}>Zatrzymaj handel…</button>
      </div>
    );
  }

  return (
    <div className="notice">
      <strong>Zatrzymanie handlu ({engine}).</strong> Silnik przestanie otwierać pozycje przy
      najbliższym cyklu, tak jak po zadziałaniu limitu. Wróci dopiero po ręcznym zwolnieniu
      z powodem. Alarm pójdzie do panelu i przez bramkę (WhatsApp).
      <form style={{ marginTop: 12 }}
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true); setErr(null);
              try {
                await post(`/api/engines/${engine}/risk/killswitch`,
                           { powod: powod.trim(), haslo });
                zamknij();
                onWlaczony?.();
              } catch (ex) {
                setErr(ex.message || 'Nie udało się włączyć kill-switcha');
                setHaslo('');
              } finally { setBusy(false); }
            }}>
        <label className="field" style={{ marginBottom: 8 }}>
          <span className="field-label">Powód zatrzymania</span>
          <input className="input-field" value={powod} maxLength={500}
                 placeholder="np. podejrzany ruch na rachunku, sprawdzam przed dalszym handlem"
                 onChange={(e) => { setPowod(e.target.value); setErr(null); }} />
        </label>
        <label className="field" style={{ marginBottom: 8 }}>
          <span className="field-label">Hasło — potwierdzenie tożsamości</span>
          <input className="input-field" type="password" value={haslo}
                 autoComplete="current-password"
                 onChange={(e) => { setHaslo(e.target.value); setErr(null); }} />
        </label>
        <button className="btn" type="submit"
                disabled={busy || powod.trim().length < 10 || !haslo}>
          {busy ? 'Zatrzymuję…' : 'Włącz kill-switch'}
        </button>
        <button className="btn" type="button" style={{ marginLeft: 8 }}
                disabled={busy} onClick={zamknij}>
          Anuluj
        </button>
        {powod.trim().length > 0 && powod.trim().length < 10 && (
          <span className="hint" style={{ marginLeft: 10 }}>
            powód musi mieć co najmniej 10 znaków
          </span>
        )}
        {err && <div className="form-error" style={{ marginTop: 8, marginBottom: 0 }}>{err}</div>}
      </form>
    </div>
  );
}

export default function Risk({ engine }) {
  const limits = useApi(`/api/engines/${engine}/risk/limits`, [engine], { refreshMs: 30000 });
  const viol = useApi(`/api/engines/${engine}/risk/violations?limit=30`, [engine], { refreshMs: 30000 });
  const ks = useApi(`/api/engines/${engine}/risk/killswitch?limit=${KS_LIMIT}`, [engine], { refreshMs: 30000 });
  const doz = useApi(`/api/engines/${engine}/risk/dozorca`, [engine], { refreshMs: 30000 });

  const ksLista = ks.data || [];
  const active = ksLista.filter((k) => k.active);
  // Odpowiedz doklejona do limitu moze ukrywac starsze zdarzenia, w tym aktywne.
  const ksUciete = ksLista.length >= KS_LIMIT;

  /* Dozorca ma TRZY stany, nie dwa. Martwy dozorca, brak dozorcy i nieudany
     odczyt pulsu renderowaly sie dotad tym samym szarym napisem obok tytulu -
     a to sa trzy rozne rzeczy do zrobienia. */
  const dozLista = doz.data || [];
  const dozMartwe = dozLista.filter((d) => !d.zyje);
  // NAJSTARSZY puls, nie pierwszy po ORDER BY nazwa: zdanie mowi o WSZYSTKICH
  // dozorcach, wiec musi je opisywac ten, ktory bije najrzadziej.
  const dozNajstarszy = dozLista.length
    ? Math.max(...dozLista.map((d) => d.sekund_temu ?? 0))
    : null;
  const dozNieznany = !!doz.error || (doz.loading && !doz.data);

  return (
    <div className="grid" style={{ gap: 16 }}>
      {active.map((k) => (
        <ZwolnijKillSwitch key={k.id} engine={engine} zdarzenie={k}
                           onZwolniony={() => ks.reload?.()} />
      ))}

      {/* Tylko przy pewnym stanie: bez odczytu historii nie wiadomo, czy
          bezpiecznik juz trzyma silnik (API i tak odpowie 409). */}
      {ks.data && !ks.error && active.length === 0 && (
        <WlaczKillSwitch engine={engine} onWlaczony={() => ks.reload?.()} />
      )}

      {active.length === 0 && ksUciete && (
        <div className="notice">
          Historia kill-switcha została ucięta na {KS_LIMIT} zdarzeniach. W tym oknie
          nie ma aktywnego bezpiecznika, ale starszy, wciąż trzymający silnik, byłby
          stąd niewidoczny — jeśli silnik nie handluje, sprawdź to bezpośrednio w bazie.
        </div>
      )}

      {dozMartwe.length > 0 && (
        <div className="notice">
          <strong>Dozorca ryzyka nie bije.</strong>{' '}
          {dozMartwe.length === dozLista.length
            ? 'Żaden z procesów nadzoru nie zgłosił pulsu w ostatnich 3 minutach'
            : `${dozMartwe.length} z ${dozLista.length} procesów nadzoru nie zgłosiło pulsu`}
          {' '}({dozMartwe.map((d) => `${d.nazwa}: ${d.sekund_temu}s`).join(', ')}).
          Limity mogą być przekraczane bez zgłoszenia — to jest awaria nadzoru,
          a nie cisza spokojnego dnia.
        </div>
      )}

      {doz.error && (
        <div className="form-error" style={{ marginBottom: 0 }}>
          <strong>Nie wiadomo, czy dozorca ryzyka żyje:</strong> {doz.error}.
          Brak odczytu pulsu nie jest dowodem, że nadzór działa.
        </div>
      )}

      {!dozNieznany && dozLista.length === 0 && (
        <div className="notice">
          <strong>Żaden dozorca ryzyka nie zgłasza pulsu dla tego silnika.</strong>{' '}
          Limity poniżej są zdefiniowane, ale nikt ich niezależnie nie sprawdza.
        </div>
      )}

      <div className="glass-card">
        <div className="card-title">
          Limity ryzyka
          <span className="hint">egzekwowane w kodzie, nie w promptcie</span>
        </div>
        {limits.error && (
          <div className="form-error">
            <strong>Nie udało się odczytać limitów:</strong> {limits.error}.
            Nie wiadomo, czy silnik ma jakiekolwiek limity.
          </div>
        )}
        {limits.data?.length ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {limits.data.map((l) => {
              /* Pomiar istnieje tylko wtedy, gdy dozorca cokolwiek zapisal.
                 `utilization || 0` rysowalo limit NIGDY NIEZMIERZONY jako
                 zielony pasek na 0% - po awarii zapisu do risk.limit_usage
                 cala zakladka wygladala LEPIEJ niz przed nia. Znacznik czasu
                 pomiaru jest tu jedynym swiadkiem: API zwraca dzis `used`
                 i `utilization` jako 0.0 takze wtedy, gdy wiersza nie ma. */
              const mierzony = l.as_of != null;
              const wylaczony = l.enabled === false;
              const u = mierzony ? Math.min(1, l.utilization || 0) : 0;
              const tone = !mierzony || wylaczony
                ? 'var(--text-dim)'
                : u > 0.85 ? 'var(--loss)' : u > 0.6 ? 'var(--color-orange)' : 'var(--profit)';
              const jedn = jednostka(l.unit);
              return (
                <div key={l.id} style={{ opacity: wylaczony ? 0.55 : 1 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
                                marginBottom: 5, flexWrap: 'wrap', gap: 8 }}>
                    <span style={{ fontSize: '0.85rem', fontWeight: 600 }}>
                      {LIMIT_PL[l.name] || l.name}
                      {l.symbol && <span className="dim"> · {l.symbol}</span>}
                    </span>
                    <span style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                      <span className="mono" style={{ fontSize: '0.78rem', color: tone }}>
                        {mierzony ? fmtNum(l.used, 2) : '—'} / {fmtNum(l.threshold, 2)}
                        {jedn ? ` ${jedn}` : ''}
                      </span>
                      {wylaczony && <span className="badge warn">wyłączony</span>}
                      <span className="badge">{ACTION_PL[l.action] || l.action}</span>
                      {l.action === 'killswitch' && WZNOWIENIE_PL[l.wznowienie] && (
                        <span className="badge" title={WZNOWIENIE_PL[l.wznowienie].opis}>
                          {WZNOWIENIE_PL[l.wznowienie].etykieta}
                        </span>
                      )}
                    </span>
                  </div>
                  <div className="bar-track">
                    {mierzony && !wylaczony && (
                      <div className="bar-fill" style={{ width: `${u * 100}%`, background: tone }} />
                    )}
                  </div>
                  <div className="row-meta" style={{ marginTop: 4 }}>
                    {wylaczony ? (
                      <span className="dim">limit wyłączony — nie jest egzekwowany</span>
                    ) : mierzony ? (
                      <span className="dim" title={l.as_of}>pomiar {fmtAgo(l.as_of)}</span>
                    ) : (
                      <span className="dim">brak pomiaru — dozorca nie zapisał wykorzystania</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        ) : limits.data ? (
          <div className="empty">Brak zdefiniowanych limitów</div>
        ) : limits.error ? (
          <div className="empty">Stan limitów nieznany.</div>
        ) : <div className="empty">Wczytywanie…</div>}
      </div>

      <div className="grid grid-2">
        <div className="glass-card">
          <div className="card-title">
            Naruszenia limitów
            <span className="hint">{viol.data ? `${viol.data.length} zdarzeń` : '—'}</span>
          </div>
          {viol.error && (
            <div className="form-error">
              <strong>Nie udało się odczytać naruszeń:</strong> {viol.error}.
            </div>
          )}
          {viol.data?.length ? (
            <div className="row-list" style={{ maxHeight: 380 }}>
              {viol.data.map((v) => (
                <div key={v.id} className="row-item">
                  <div className="row-head">
                    <span className="row-title" style={{ fontSize: '0.85rem' }}>
                      {LIMIT_PL[v.limit] || v.limit}
                    </span>
                    <span className="badge bad">{ACTION_PL[v.action] || v.action}</span>
                  </div>
                  <div className="row-meta">
                    <span>{fmtTime(v.at)}</span>
                    <span className="mono">
                      zaobserwowano {fmtNum(v.observed, 2)} przy progu {fmtNum(v.threshold, 2)}
                      {jednostka(v.unit) ? ` ${jednostka(v.unit)}` : ''}
                    </span>
                    {v.decision_id && <span className="dim">decyzja #{v.decision_id}</span>}
                  </div>
                </div>
              ))}
            </div>
          ) : viol.data ? (
            <div className="empty">Brak naruszeń — limity nie były przekroczone</div>
          ) : viol.error ? (
            <div className="empty">Nie wiadomo, czy limity były przekraczane.</div>
          ) : <div className="empty">Wczytywanie…</div>}
        </div>

        <div className="glass-card">
          <div className="card-title">
            Historia kill-switcha
            <span className="hint">
              {doz.error ? 'puls dozorcy: nie wiadomo'
                : dozNieznany ? 'sprawdzam dozorcę…'
                : dozLista.length === 0 ? 'brak dozorcy'
                : dozMartwe.length ? `dozorca nie bije (${dozMartwe.length}/${dozLista.length})`
                : `dozorca żyje — najstarszy puls ${dozNajstarszy}s temu`}
            </span>
          </div>
          {ks.error && (
            <div className="form-error">
              <strong>Nie udało się odczytać historii kill-switcha:</strong> {ks.error}.
            </div>
          )}
          {ks.data?.length ? (
            <div className="row-list" style={{ maxHeight: 380 }}>
              {ks.data.map((k) => (
                <div key={k.id} className="row-item">
                  <div className="row-head">
                    <span className="row-title" style={{ fontSize: '0.85rem' }}>{k.reason}</span>
                    <span className={`badge ${k.active ? 'bad' : 'ok'}`}>
                      {k.active ? 'aktywny' : 'zwolniony'}
                    </span>
                  </div>
                  <div className="row-meta">
                    <span>
                      uruchomiony {fmtTime(k.triggered_at)}
                      {k.triggered_by ? ` — ręcznie przez ${k.triggered_by}` : ''}
                    </span>
                    {k.limit_name && <span className="dim">{LIMIT_PL[k.limit_name] || k.limit_name}</span>}
                    {k.released_at && (
                      <span>
                        zwolniony {fmtTime(k.released_at)}
                        {k.released_auto ? ' — automatycznie' : ` przez ${k.released_by || 'człowieka'}`}
                      </span>
                    )}
                  </div>
                  {k.release_note && (
                    <div className="row-meta">
                      <span className="dim" title="podstawa wznowienia">{k.release_note}</span>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ) : ks.data ? (
            <div className="empty">Kill-switch nigdy się nie uruchomił</div>
          ) : ks.error ? (
            <div className="empty">Nie wiadomo, czy kill-switch się uruchamiał.</div>
          ) : <div className="empty">Wczytywanie…</div>}
        </div>
      </div>
    </div>
  );
}
