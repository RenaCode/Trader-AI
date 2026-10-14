/* Ikony nawigacji i paska gornego - liniowe SVG rysowane kolorem tekstu
   (`currentColor`), wiec stan aktywny/hover ustawia sie samym CSS.
   Zero zaleznosci, tak jak wykresy. Ikona zawsze idzie obok podpisu albo
   z `aria-label` na przycisku - sama w sobie jest dla czytnika niewidoczna. */
import React from 'react';

const PATHS = {
  home: <><path d="M4 10.5 12 4l8 6.5" /><path d="M6 9.5V20h12V9.5" /><path d="M10 20v-5h4v5" /></>,
  learning: <><path d="M4 19h16" /><path d="M5 15l4-4 3 3 7-7" /><path d="M15 7h4v4" /></>,
  moves: <><path d="M4 8h13" /><path d="m14 5 3 3-3 3" /><path d="M20 16H7" /><path d="m10 13-3 3 3 3" /></>,
  sources: <><path d="M5 5h11a3 3 0 0 1 3 3v11H8a3 3 0 0 1-3-3Z" /><path d="M9 9h6" /><path d="M9 13h6" /></>,
  risk: <><path d="M12 3 5 6v5.5c0 4.2 3 7.9 7 9.5 4-1.6 7-5.3 7-9.5V6Z" /><path d="M12 8.5v4" /><path d="M12 15.5h.01" /></>,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" /></>,
  profile: <><circle cx="12" cy="8" r="4" /><path d="M4.5 20.5c1.4-3.6 4.2-5.5 7.5-5.5s6.1 1.9 7.5 5.5" /></>,
  bell: <><path d="M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15Z" /><path d="M10 20.5a2.2 2.2 0 0 0 4 0" /></>,
  logout: <><path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4" /><path d="M10 16l-4-4 4-4" /><path d="M6 12h10" /></>,
  equity: <><path d="M4 18 9 12l3.5 3L20 7" /><path d="M15 7h5v5" /></>,
  /* Siatka 3x3 przelacznika aplikacji - kropki wypelnione, nie obrys. */
  apps: <>{[5, 12, 19].flatMap((y) => [5, 12, 19].map((x) => (
    <circle key={`${x}-${y}`} cx={x} cy={y} r="1.9" fill="currentColor" stroke="none" />)))}</>,
};

export default function Icon({ name, size = 18 }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"
         aria-hidden="true" focusable="false">
      {PATHS[name]}
    </svg>
  );
}
