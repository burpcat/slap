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
    rollupOptions: {
      output: {
        // Split vendored libs into their own long-cacheable chunks. `charts`
        // (chart.js + react-chartjs-2) is the heavy one and is only referenced
        // by the lazily-loaded Engagement route, so Rollup keeps it out of the
        // initial download and fetches it only when that page is opened.
        manualChunks: {
          'vendor-react': ['react', 'react-dom', 'react-router-dom'],
          'vendor-query': ['@tanstack/react-query'],
          'vendor-charts': ['chart.js', 'react-chartjs-2'],
          'vendor-floating': ['@floating-ui/react'],
        },
      },
    },
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
