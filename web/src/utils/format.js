const money = new Intl.NumberFormat('pl-PL', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const compact = new Intl.NumberFormat('pl-PL', { notation: 'compact', maximumFractionDigits: 1 });

export const fmtMoney = (v) => (v == null ? '—' : money.format(v));
export const fmtCompact = (v) => (v == null ? '—' : compact.format(v));
export const fmtPct = (v, d = 2) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`);
export const fmtNum = (v, d = 3) => (v == null ? '—' : Number(v).toFixed(d));

export const fmtTime = (iso) =>
  iso ? new Date(iso).toLocaleString('pl-PL', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';

export const fmtDate = (iso) =>
  iso ? new Date(iso).toLocaleDateString('pl-PL', { day: '2-digit', month: '2-digit' }) : '—';

export function fmtAgo(iso) {
  if (!iso) return '—';
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s} s temu`;
  if (s < 3600) return `${Math.floor(s / 60)} min temu`;
  if (s < 86400) return `${Math.floor(s / 3600)} godz. temu`;
  return `${Math.floor(s / 86400)} dni temu`;
}

export const fmtLag = (s) => (s == null ? '—' : s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`);

/* Etykiety po polsku - baza trzyma wartosci techniczne, UI pokazuje ludzkie. */
export const ACTION_PL = {
  enter_long: 'Wejście long', enter_short: 'Wejście short', scale_in: 'Dokupienie',
  scale_out: 'Redukcja', exit: 'Wyjście', hold: 'Bez zmian',
};
export const REGIME_PL = {
  trend_up: 'Trend wzrostowy', trend_down: 'Trend spadkowy', range: 'Konsolidacja',
  high_vol: 'Wysoka zmienność', news_shock: 'Szok newsowy',
};
export const VERDICT_PL = { allow: 'Dopuszczone', downsize: 'Zmniejszone', block: 'Zablokowane' };
export const DIRECTION_PL = { bullish: 'Pozytywny', bearish: 'Negatywny', neutral: 'Neutralny' };
export const STATE_PL = {
  warmup: 'Rozgrzewka', learning: 'Uczy się', validated: 'Zwalidowany',
  degraded: 'Degradacja', halted: 'Zatrzymany',
};
export const STATE_TONE = {
  warmup: 'info', learning: 'warn', validated: 'ok', degraded: 'bad', halted: 'bad',
};
