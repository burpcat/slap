import { lazy, Suspense } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Route, Routes, useLocation } from 'react-router-dom';
import { ThemeProvider } from './theme/useTheme';
import { SplashGate } from './components/SplashGate';
import { Layout } from './components/Layout';
import { ErrorBoundary } from './components/ErrorBoundary';

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
const Lifecycle = lazy(() => import('./pages/Lifecycle'));
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

// The routed content, wrapped in an ErrorBoundary so a single page's render
// error shows a message in the content area instead of unmounting the whole
// app (header included). Keyed on the pathname so navigating away clears the
// error. Lives inside <BrowserRouter> so useLocation() is available.
function RoutedPages() {
  const { pathname } = useLocation();
  return (
    <ErrorBoundary resetKey={pathname}>
      <Suspense fallback={<p>Loading…</p>}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/campaigns" element={<Campaigns />} />
          <Route path="/engagement" element={<Engagement />} />
          <Route path="/pipeline" element={<Pipeline />} />
          <Route path="/reachouts" element={<Reachouts />} />
          <Route path="/lifecycle" element={<Lifecycle />} />
          <Route path="/commands" element={<Commands />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/template-failures" element={<TemplateFailures />} />
        </Routes>
      </Suspense>
    </ErrorBoundary>
  );
}

export default function App() {
  return (
    <SplashGate>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter basename="/">
            <Layout>
              <RoutedPages />
            </Layout>
          </BrowserRouter>
        </QueryClientProvider>
      </ThemeProvider>
    </SplashGate>
  );
}
