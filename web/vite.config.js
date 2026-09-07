import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dzieki proxy front wola /api/... bez znajomosci hosta API - ta sama sciezka
// dziala lokalnie, w tescie dymnym i za Traefikiem na VPS.
//
// JEDEN OBIEKT DLA `server` I `preview`, nie dwa.
//
// Proxy bylo zdefiniowane wylacznie w `server`, czyli dzialalo tylko przy
// `vite dev`. Test dymny w CI celowo uzywa `vite preview` - zeby sprawdzac ten
// bundle, ktory pojdzie na serwer, a nie wersje deweloperska - wiec front wolal
// /api pod portem podgladu, gdzie nic nie odpowiada. Logowanie konczylo sie
// niczym, a dashboard nigdy sie nie renderowal.
//
// Nie bylo tego widac, bo wczesniejszy krok workflow padal na usunietym pliku
// (patrz komentarz w .github/workflows/docker-publish.yml) i do budowania frontu
// w ogole nie dochodzilo. Dwa bledy jeden pod drugim.
const proxy = {
  '/api': { target: process.env.API_URL || 'http://127.0.0.1:8099', changeOrigin: true },
};

export default defineConfig({
  plugins: [react()],
  server: { port: 5174, proxy },
  preview: { port: 4173, proxy },
});
