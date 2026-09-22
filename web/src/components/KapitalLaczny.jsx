/* Kapital OBU platform na jednym wykresie, z suma i zmianami.

   Siatka czasu jest wspolna (godzinowa, liczona po stronie API), wiec suma
   w kazdym punkcie to kapital obu rachunkow W TEJ SAMEJ CHWILI - a nie suma
   dwoch roznych ostatnich wpisow. Silnik bez danych (jeszcze nie ruszyl) ma
   w serii dziure, nie zero. */
import React from 'react';
import { useApi } from '../utils/api';
import { fmtMoney, fmtPct, fmtTime } from '../utils/format';
import { MultiLineChart } from '../charts';

const NAZWA = { crypto: 'Krypto (Bybit)', equity: 'Giełda (Alpaca)', razem: 'Razem' };
const KOLOR = { crypto: 'var(--color-orange)', equity: 'var(--color-blue)', razem: 'var(--profit)' };

/* `null >= 0` jest w JS PRAWDA, wiec brak wartosci renderowal sie jako zielone
   "+—": kolor zysku i znak plus przy pomiarze, ktorego nie ma. Brak liczby ma
   byc szary i bez znaku. */
function Delta({ v, pct, etykieta }) {
  if (v == null) {
    return <span className="dim" style={{ marginLeft: 8 }}>{etykieta} —</span>;
  }
  return (
    <span style={{ color: v >= 0 ? 'var(--profit)' : 'var(--loss)', marginLeft: 8 }}>
      {etykieta} {v >= 0 ? '+' : ''}{fmtMoney(v)} ({fmtPct(pct)})
    </span>
  );
}

function Zmiana({ z }) {
  if (!z || z.teraz == null) return <span className="hint">brak danych</span>;
  return (
    <span className="mono" style={{ fontSize: '0.82rem' }}>
      {fmtMoney(z.teraz)}
      <Delta etykieta="24 h" v={z.doba} pct={z.doba_pct} />
      <Delta etykieta="okno" v={z.od_startu} pct={z.od_startu_pct} />
    </span>
  );
}

export default function KapitalLaczny({ days = 45 }) {
  const dane = useApi(`/api/kapital/laczny?days=${days}`, [days], { refreshMs: 60000 });
  const d = dane.data;
  const silniki = d?.silniki || [];
  const series = [
    ...silniki.map((e) => ({ key: e, label: NAZWA[e] || e, color: KOLOR[e] || 'var(--text-dim)' })),
    { key: 'razem', label: NAZWA.razem, color: KOLOR.razem, gruba: true },
  ];
  return (
    <div className="glass-card">
      <div className="card-title">
        Kapitał łączny obu platform
        <span className="hint">tryb demo · {days} dni · siatka godzinowa</span>
      </div>
      {dane.error && <div className="empty">Nie udało się pobrać: {dane.error}</div>}
      <MultiLineChart data={d?.punkty || []} series={series} height={240}
                      valueFmt={(v) => fmtMoney(v)} labelFmt={(p) => fmtTime(p.t)} />
      <div style={{ display: 'grid', gap: 6, marginTop: 10 }}>
        {[...silniki, 'razem'].map((k) => (
          <div key={k} className="row-item" style={{ display: 'flex', justifyContent: 'space-between',
                                                     flexWrap: 'wrap', gap: 8 }}>
            <span className="row-title" style={{ color: KOLOR[k] }}>{NAZWA[k] || k}</span>
            <Zmiana z={d?.zmiany?.[k]} />
          </div>
        ))}
        {silniki.length < 2 && (
          <div className="hint">Druga platforma pojawi się na wykresie, gdy jej silnik zapisze pierwszy punkt kapitału.</div>
        )}
      </div>
    </div>
  );
}
