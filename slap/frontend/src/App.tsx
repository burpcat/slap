import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { ThemeProvider } from './theme/useTheme';
import { SplashGate } from './components/SplashGate';
import { Layout } from './components/Layout';
import Home from './pages/Home';
import Campaigns from './pages/Campaigns';
import Engagement from './pages/Engagement';
import Pipeline from './pages/Pipeline';
import Reachouts from './pages/Reachouts';
import Commands from './pages/Commands';
import Logs from './pages/Logs';
import TemplateFailures from './pages/TemplateFailures';

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
          <BrowserRouter basename="/static/dist/">
            <Layout>
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
            </Layout>
          </BrowserRouter>
        </QueryClientProvider>
      </ThemeProvider>
    </SplashGate>
  );
}
