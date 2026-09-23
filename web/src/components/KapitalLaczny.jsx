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
  /* WALUTA PRZY KAZDEJ SERII, nie jedna na wykres — tak API ją oddaje
     (`kapital_laczny.py:174`) i tak musi ją czytać front. Krypto mierzy dziś
     w USDC, a equity w USD (migracje 0023 i 0047); dwie linie na jednej osi
     bez jednostki wyglądają na porównywalne rachunki i tak właśnie były
     czytane. Do 23.09.2026 `grep -rn "waluty" web/src/` nie dawał ani jednego
     trafienia. */
  const waluty = d?.waluty || {};
  const jednostki = [...new Set(silniki.map((e) => waluty[e]).filter(Boolean))];
  /* Oś dostaje jednostkę TYLKO wtedy, gdy jest jedna na cały wykres i znana
     dla każdej serii. Podpisanie osi jedną z dwóch walut byłoby gorsze niż
     brak podpisu: druga linia dostałaby cudzą jednostkę. */
  const jednostkaOsi = (silniki.length && jednostki.length === 1
    && silniki.every((e) => waluty[e])) ? jednostki[0] : null;
  const etykietaWaluty = (e) => waluty[e] || 'waluta nieznana';
  const series = [
    ...silniki.map((e) => ({ key: e, label: `${NAZWA[e] || e} · ${etykietaWaluty(e)}`,
                             color: KOLOR[e] || 'var(--text-dim)' })),
    { key: 'razem', label: NAZWA.razem, color: KOLOR.razem, gruba: true },
  ];
  return (
    <div className="glass-card">
      <div className="card-title">
        Kapitał łączny obu platform
        <span className="hint">tryb demo · {days} dni · siatka godzinowa</span>
      </div>
      {/* Wykres dostajac pusta serie pisze "Brak danych do wykresu" - i ma do
          tego prawo, bo nie wie, DLACZEGO jej nie ma. Wiedza o tym tutaj.
          Po nieudanym odczycie to zdanie bylo drugim, sprzecznym komunikatem
          pod komunikatem o awarii: raz "nie udalo sie pobrac", raz "nie ma
          danych". Przy danych z poprzedniego udanego odczytu wykres zostaje -
          stare punkty sa prawdziwe, trzeba tylko powiedziec, ze sa stare. */}
      {dane.error && (
        <div className="empty">Nie udało się pobrać: {dane.error}</div>
      )}
      {dane.error && !d?.punkty?.length ? null : (
        <MultiLineChart data={d?.punkty || []} series={series} height={240}
                        valueFmt={(v) => (jednostkaOsi ? `${fmtMoney(v)} ${jednostkaOsi}` : fmtMoney(v))}
                        labelFmt={(p) => fmtTime(p.t)} />
      )}
      <div style={{ display: 'grid', gap: 6, marginTop: 10 }}>
        {[...silniki, 'razem'].map((k) => (
          <div key={k} className="row-item" style={{ display: 'flex', justifyContent: 'space-between',
                                                     flexWrap: 'wrap', gap: 8 }}>
            <span className="row-title" style={{ color: KOLOR[k] }}>
              {NAZWA[k] || k}
              {k !== 'razem' && (
                <span className="hint" style={{ marginLeft: 6 }}>{etykietaWaluty(k)}</span>
              )}
            </span>
            <Zmiana z={d?.zmiany?.[k]} />
          </div>
        ))}
        {/* DLACZEGO "RAZEM" JEST PUSTE — na ekranie, nie w logu.
            Bez tego zdania wiersz "Razem" pokazywał samo `brak danych`
            (`Zmiana`), nie do odróżnienia od stanu "druga platforma jeszcze nie
            ruszyła", o którym ta sama karta zapewnia zdanie niżej. API liczy
            ten powód i oddaje go w `razem_powod` właśnie po to, żeby trafił
            przed oczy właściciela. */}
        {d?.razem_powod && (
          <div className="notice">
            <strong>Suma nie jest podana.</strong> {d.razem_powod}
          </div>
        )}
        {silniki.length < 2 && (
          <div className="hint">Druga platforma pojawi się na wykresie, gdy jej silnik zapisze pierwszy punkt kapitału.</div>
        )}
      </div>
    </div>
  );
}
