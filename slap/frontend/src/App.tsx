import { lazy, Suspense } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { ThemeProvider } from './theme/useTheme';
import { SplashGate } from './components/SplashGate';
import { Layout } from './components/Layout';

// Route-level code splitting: each page is its own async chunk, so heavy,
// page-specific deps aren't in the initial bundle. Notably chart.js /
// react-chartjs-2 (only Engagement uses them) now load on demand when that
// route is opened, keeping the first paint small. The shell (Layout, theme,
// splash) stays eager since it renders immediately on every route.
const Home = lazy(() => import('./pages/Home'));
const Campaigns = lazy(() => import('./pages/Campaigns'));
const Engagement = lazy(() => import('./pages/Engagement'));
const Pipeline = lazy(() => import('./pages/Pipeline'));
const Reachouts = lazy(() => import('./pages/Reachouts'));
const Commands = lazy(() => import('./pages/Commands'));
const Logs = lazy(() => import('./pages/Logs'));
const TemplateFailures = lazy(() => import('./pages/TemplateFailures'));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
});

export default function App() {
  return (
    <SplashGate>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter basename="/">
            <Layout>
              <Suspense fallback={<p>Loading…</p>}>
                <Routes>
                  <Route path="/" element={<Home />} />
                  <Route path="/campaigns" element={<Campaigns />} />
                  <Route path="/engagement" element={<Engagement />} />
                  <Route path="/pipeline" element={<Pipeline />} />
                  <Route path="/reachouts" element={<Reachouts />} />
                  <Route path="/commands" element={<Commands />} />
                  <Route path="/logs" element={<Logs />} />
                  <Route path="/template-failures" element={<TemplateFailures />} />
                </Routes>
              </Suspense>
            </Layout>
          </BrowserRouter>
        </QueryClientProvider>
      </ThemeProvider>
    </SplashGate>
  );
}
