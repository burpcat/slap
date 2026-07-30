import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Served from Flask at /static/dist/ (slap/dashboard.py's default static
// handling — see slap/frontend/README context in the build brief). The dev
// server proxies /api to the Flask app so `npm run dev` can be used against
// a locally running `python slap.py dashboard`.
export default defineConfig({
  plugins: [react()],
  base: '/static/dist/',
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5050',
        changeOrigin: true,
      },
    },
  },
});
