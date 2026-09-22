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

function Zmiana({ z }) {
  if (!z || z.teraz == null) return <span className="hint">brak danych</span>;
  const tone = (v) => (v >= 0 ? 'var(--profit)' : 'var(--loss)');
  return (
    <span className="mono" style={{ fontSize: '0.82rem' }}>
      {fmtMoney(z.teraz)}
      <span style={{ color: tone(z.doba), marginLeft: 8 }}>24 h {z.doba >= 0 ? '+' : ''}{fmtMoney(z.doba)} ({fmtPct(z.doba_pct)})</span>
      <span style={{ color: tone(z.od_startu), marginLeft: 8 }}>okno {z.od_startu >= 0 ? '+' : ''}{fmtMoney(z.od_startu)} ({fmtPct(z.od_startu_pct)})</span>
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
