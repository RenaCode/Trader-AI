/* Wykresy SVG, bez zewnetrznych bibliotek - tak jak Dietetyk trzyma zero zaleznosci
   na wizualizacje. Wszystkie kolory ida z tokenow CSS, wiec zmiana motywu w jednym
   miejscu przenosi sie na wykresy. */
import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';

const PAD = { t: 12, r: 14, b: 24, l: 46 };

/* Lewy margines liczony z NAJDLUZSZEJ etykiety osi, a nie ze stalej.
   Przy skalowaniu viewBox do szerokosci kontenera (1:1 w pikselach) staly
   margines 46 px ucinal "102 500,00" do "500,00" - wczesniej tego nie bylo
   widac, bo SVG bylo powiekszane ~2.4x. 6.2 px na znak to ostrozne
   przyblizenie szerokosci cyfry w font-size 10 monospace. */
function axisPad(ticks, fmt) {
  const longest = ticks.reduce(
    (m, t) => Math.max(m, String(fmt ? fmt(t) : t.toFixed(1)).length), 0);
  return Math.max(PAD.l, Math.ceil(longest * 6.2) + 14);
}

/* Mierzy realna szerokosc kontenera i uzywa jej jako szerokosci viewBox.
   Bez tego SVG z viewBox="0 0 800 H" i sztywna wysokoscia renderuje sie w skali
   dopasowanej do WYSOKOSCI (domyslne preserveAspectRatio="xMidYMid meet"), czyli
   na 800 px wysrodkowanych - wykres zajmowal polowe karty, a reszta byla pusta.
   Mapowanie 1:1 daje przy okazji ostry tekst osi. */
function useWidth(fallback = 800) {
  const ref = useRef(null);
  const [w, setW] = useState(fallback);

  const measure = useCallback(() => {
    if (ref.current) {
      const next = ref.current.clientWidth;
      if (next > 0) setW(next);
    }
  }, []);

  useEffect(() => {
    measure();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }
    const ro = new ResizeObserver(measure);
    if (ref.current) ro.observe(ref.current);
    return () => ro.disconnect();
  }, [measure]);

  return [ref, w];
}

function niceTicks(min, max, count = 4) {
  if (!isFinite(min) || !isFinite(max) || min === max) return [min];
  const span = max - min;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(v);
  return out;
}

/* ------------------------------------------------------------------ AreaChart
   Krzywa kapitalu. Wypelnienie gradientem, hover pokazuje punkt i wartosc. */
export function AreaChart({ data, height = 220, valueKey = 'v', labelFmt, valueFmt, color }) {
  const gid = useId();
  const [hover, setHover] = useState(null);
  const [ref, w] = useWidth();
  const h = height;

  const { pts, min, max, ticks, padL } = useMemo(() => {
    if (!data?.length) return { pts: [], min: 0, max: 0, ticks: [], padL: PAD.l };
    const vals = data.map((d) => d[valueKey]);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.02 || 1;
    lo -= pad; hi += pad;
    const tk = niceTicks(lo, hi, 4);
    const padL = axisPad(tk, valueFmt);
    const iw = w - padL - PAD.r, ih = h - PAD.t - PAD.b;
    const pts = data.map((d, i) => [
      padL + (data.length === 1 ? iw / 2 : (i / (data.length - 1)) * iw),
      PAD.t + ih - ((d[valueKey] - lo) / (hi - lo)) * ih,
    ]);
    return { pts, min: lo, max: hi, ticks: tk, padL };
  }, [data, valueKey, h, w, valueFmt]);

  const ih = h - PAD.t - PAD.b;
  const stroke = color || 'var(--profit)';
  // Puste dane licza sie tutaj, a nie dopiero w JSX: pts[pts.length - 1] przy
  // pustej tablicy jest undefined i wysypuje cala aplikacje, nie tylko wykres.
  const hasPts = pts.length > 0;
  const line = hasPts
    ? pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ')
    : '';
  const area = hasPts
    ? `${line} L${pts[pts.length - 1][0].toFixed(1)},${PAD.t + ih} L${pts[0][0].toFixed(1)},${PAD.t + ih} Z`
    : '';

  const onMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * w;
    const iw = w - padL - PAD.r;
    const idx = Math.round(((x - padL) / iw) * (data.length - 1));
    if (idx >= 0 && idx < data.length) setHover(idx);
  };

  return (
    <div className="chart-wrap" ref={ref}>
      {!hasPts ? <div className="empty">Brak danych do wykresu</div> : (
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img"
           preserveAspectRatio="none"
           onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <defs>
          <linearGradient id={`g-${gid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.32" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>

        {ticks.map((tv) => {
          const y = PAD.t + ih - ((tv - min) / (max - min)) * ih;
          return (
            <g key={tv}>
              <line x1={padL} y1={y} x2={w - PAD.r} y2={y}
                    stroke="rgba(255,255,255,0.055)" strokeWidth="1" />
              <text x={padL - 8} y={y + 3.5} textAnchor="end"
                    fill="var(--text-dim)" fontSize="10" fontFamily="var(--font-mono)">
                {valueFmt ? valueFmt(tv) : tv.toFixed(0)}
              </text>
            </g>
          );
        })}

        <path d={area} fill={`url(#g-${gid})`} />
        <path d={line} fill="none" stroke={stroke} strokeWidth="2"
              strokeLinejoin="round" strokeLinecap="round" />

        {hover !== null && pts[hover] && (
          <g>
            <line x1={pts[hover][0]} y1={PAD.t} x2={pts[hover][0]} y2={PAD.t + ih}
                  stroke="rgba(255,255,255,0.2)" strokeDasharray="3 3" />
            <circle cx={pts[hover][0]} cy={pts[hover][1]} r="4" fill={stroke}
                    stroke="var(--bg-dark)" strokeWidth="2" />
          </g>
        )}
      </svg>)}
      <div className="chart-legend">
        {hover !== null && data[hover] ? (
          <>
            <span className="key mono" style={{ color: stroke }}>
              {valueFmt ? valueFmt(data[hover][valueKey]) : data[hover][valueKey]}
            </span>
            <span className="key">{labelFmt ? labelFmt(data[hover]) : ''}</span>
          </>
        ) : (
          <span className="key dim">Najedź na wykres, aby zobaczyć wartość</span>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ BarChart
   Metryki per okno walk-forward. Slupki dodatnie/ujemne wzgledem zera - od razu
   widac, ile okien OOS wyszlo na plus. */
export function BarChart({ data, height = 200, valueKey = 'v', labelKey = 'label', valueFmt, threshold = 0 }) {
  const [hover, setHover] = useState(null);
  const [ref, w] = useWidth();
  const h = height;
  if (!data?.length) return <div className="empty">Brak danych do wykresu</div>;

  const vals = data.map((d) => d[valueKey] ?? 0);
  const hi = Math.max(...vals, threshold, 0);
  const lo = Math.min(...vals, threshold, 0);
  const span = (hi - lo) || 1;
  const ticks = niceTicks(lo, hi, 4);
  const padL = axisPad(ticks, valueFmt);
  const iw = w - padL - PAD.r, ih = h - PAD.t - PAD.b;
  const bw = Math.max(3, (iw / data.length) * 0.66);
  const y0 = PAD.t + ih - ((0 - lo) / span) * ih;

  return (
    <div className="chart-wrap" ref={ref}>
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img"
           preserveAspectRatio="none">
        {ticks.map((tv) => {
          const y = PAD.t + ih - ((tv - lo) / span) * ih;
          return (
            <g key={tv}>
              <line x1={padL} y1={y} x2={w - PAD.r} y2={y} stroke="rgba(255,255,255,0.055)" />
              <text x={padL - 8} y={y + 3.5} textAnchor="end" fill="var(--text-dim)"
                    fontSize="10" fontFamily="var(--font-mono)">
                {valueFmt ? valueFmt(tv) : tv.toFixed(1)}
              </text>
            </g>
          );
        })}
        <line x1={padL} y1={y0} x2={w - PAD.r} y2={y0} stroke="rgba(255,255,255,0.22)" />

        {data.map((d, i) => {
          const v = d[valueKey] ?? 0;
          const x = padL + (i + 0.5) * (iw / data.length) - bw / 2;
          const y = PAD.t + ih - ((Math.max(v, 0) - lo) / span) * ih;
          const bh = Math.abs((v / span) * ih);
          return (
            <rect key={i} x={x} y={y} width={bw} height={Math.max(1.5, bh)} rx="2"
                  fill={v >= 0 ? 'var(--profit)' : 'var(--loss)'}
                  opacity={hover === null || hover === i ? 0.9 : 0.35}
                  onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} />
          );
        })}
      </svg>
      <div className="chart-legend">
        {hover !== null ? (
          <>
            <span className="key">{data[hover][labelKey]}</span>
            <span className="key mono" style={{ color: (data[hover][valueKey] ?? 0) >= 0 ? 'var(--profit)' : 'var(--loss)' }}>
              {valueFmt ? valueFmt(data[hover][valueKey]) : data[hover][valueKey]}
            </span>
            {data[hover].regime && <span className="key dim">reżim: {data[hover].regime}</span>}
          </>
        ) : (
          <>
            <span className="key"><i className="swatch" style={{ background: 'var(--profit)' }} /> okno dodatnie</span>
            <span className="key"><i className="swatch" style={{ background: 'var(--loss)' }} /> okno ujemne</span>
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ CalibrationChart
   Przekatna = model idealnie skalibrowany. Punkty pod przekatna = model jest
   zbyt pewny siebie; nad = zbyt ostrozny. */
export function CalibrationChart({ data, height = 300 }) {
  const [ref, w] = useWidth(300);
  if (!data?.length) return <div className="empty">Brak danych kalibracji</div>;

  // Obie osie sa w skali 0..1, wiec pole wykresu musi zostac kwadratem -
  // inaczej odchylenie od przekatnej przestaje byc czytelne wzrokowo.
  const P = 40;
  const side = Math.max(120, Math.min(w - P * 1.6, height - P * 1.5));
  const x0 = (w - side) / 2;
  const y0 = height - P;
  const sx = (v) => x0 + v * side;
  const sy = (v) => y0 - v * side;

  return (
    <div className="chart-wrap" ref={ref}>
      <svg viewBox={`0 0 ${w} ${height}`} width="100%" height={height} role="img"
           preserveAspectRatio="none">
        {[0, 0.25, 0.5, 0.75, 1].map((g) => (
          <g key={g}>
            <line x1={sx(0)} y1={sy(g)} x2={sx(1)} y2={sy(g)} stroke="rgba(255,255,255,0.05)" />
            <line x1={sx(g)} y1={sy(0)} x2={sx(g)} y2={sy(1)} stroke="rgba(255,255,255,0.05)" />
            <text x={sx(0) - 8} y={sy(g) + 3.5} textAnchor="end" fill="var(--text-dim)"
                  fontSize="10" fontFamily="var(--font-mono)">{g.toFixed(2)}</text>
          </g>
        ))}
        {/* Przekatna = model idealnie skalibrowany. */}
        <line x1={sx(0)} y1={sy(0)} x2={sx(1)} y2={sy(1)}
              stroke="var(--text-dim)" strokeDasharray="4 4" strokeWidth="1.2" />
        <path d={data.map((d, i) => `${i ? 'L' : 'M'}${sx(d.predicted)},${sy(d.observed)}`).join(' ')}
              fill="none" stroke="var(--color-primary)" strokeWidth="2" />
        {data.map((d, i) => (
          <circle key={i} cx={sx(d.predicted)} cy={sy(d.observed)}
                  r={Math.min(8, 3.5 + Math.log10(Math.max(d.n, 1)) * 1.7)}
                  fill="#c084fc" stroke="var(--bg-dark)" strokeWidth="1.5">
            <title>{`deklarowana ${(d.predicted * 100).toFixed(0)}% → rzeczywista ${(d.observed * 100).toFixed(0)}% (n=${d.n})`}</title>
          </circle>
        ))}
        <text x={w / 2} y={height - 8} textAnchor="middle" fill="var(--text-dim)" fontSize="11">
          deklarowana pewność
        </text>
        <text x={14} y={height / 2} textAnchor="middle" fill="var(--text-dim)" fontSize="11"
              transform={`rotate(-90 14 ${height / 2})`}>rzeczywista trafność</text>
      </svg>
    </div>
  );
}

/* ------------------------------------------------------------------ Sparkline */
export function Sparkline({ data, width = 110, height = 30, color = 'var(--profit)' }) {
  if (!data?.length) return null;
  const lo = Math.min(...data), hi = Math.max(...data);
  const span = (hi - lo) || 1;
  const d = data.map((v, i) =>
    `${i ? 'L' : 'M'}${((i / (data.length - 1)) * width).toFixed(1)},${(height - ((v - lo) / span) * height).toFixed(1)}`
  ).join(' ');
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <path d={d} fill="none" stroke={color} strokeWidth="1.6" strokeLinejoin="round" />
    </svg>
  );
}

/* ------------------------------------------------------------------ ContributionBars
   Rozbicie decyzji na wklady sygnalow/cech/newsow - "dlaczego" odczytane z bazy. */
export function ContributionBars({ items }) {
  if (!items?.length) return <div className="empty">Brak rozbicia dla tej decyzji</div>;
  const max = Math.max(...items.map((i) => Math.abs(i.contribution))) || 1;
  const kindColor = { signal: 'var(--color-primary)', feature: 'var(--color-blue)', news: 'var(--color-orange)' };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
      {items.map((it, i) => {
        const pct = (Math.abs(it.contribution) / max) * 50;
        const pos = it.contribution >= 0;
        return (
          <div key={i} style={{ display: 'grid', gridTemplateColumns: '150px 1fr 62px', gap: 10, alignItems: 'center' }}>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', overflow: 'hidden',
                           textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={it.label}>
              <i style={{ display: 'inline-block', width: 6, height: 6, borderRadius: 2,
                          background: kindColor[it.kind], marginRight: 6 }} />
              {it.label}
            </span>
            <div style={{ position: 'relative', height: 9, background: 'rgba(255,255,255,0.05)', borderRadius: 3 }}>
              <div style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1,
                            background: 'rgba(255,255,255,0.18)' }} />
              <div style={{
                position: 'absolute', top: 1, bottom: 1, borderRadius: 2,
                left: pos ? '50%' : `${50 - pct}%`, width: `${pct}%`,
                background: pos ? 'var(--profit)' : 'var(--loss)', opacity: 0.85,
              }} />
            </div>
            <span className="mono" style={{ fontSize: '0.73rem', textAlign: 'right',
                                            color: pos ? 'var(--profit)' : 'var(--loss)' }}>
              {it.contribution >= 0 ? '+' : ''}{it.contribution.toFixed(3)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
