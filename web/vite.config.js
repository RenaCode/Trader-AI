import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    // Dzieki proxy front wola /api/... bez znajomosci hosta API - ta sama
    // sciezka dziala lokalnie i za Traefikiem na VPS.
    proxy: {
      '/api': { target: process.env.API_URL || 'http://127.0.0.1:8099', changeOrigin: true },
    },
  },
});
