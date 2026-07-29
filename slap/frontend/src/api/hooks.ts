// react-query hooks over the typed client (api/client.ts) — one hook per
// endpoint, plus mutations for every write action. Query keys are simple
// arrays so invalidateQueries can target either one page's data or (for
// actions with cross-page effects, e.g. tagging a reply) several at once.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiGet, apiPost } from './client';
import type {
  CampaignsResponse,
  CommandsResponse,
  EngagementResponse,
  HomeResponse,
  LinkedinRepliedBody,
  LogsResponse,
  NavResponse,
  OkResponse,
  PipelineResponse,
  ReachoutsResponse,
  ResendBody,
  SyncStatusResponse,
  TagReplyBody,
  TemplateFailuresResponse,
} from './types';

// --- read hooks -------------------------------------------------------------

export function useHome() {
  return useQuery({ queryKey: ['home'], queryFn: () => apiGet<HomeResponse>('/api/home') });
}

export function usePipeline() {
  return useQuery({ queryKey: ['pipeline'], queryFn: () => apiGet<PipelineResponse>('/api/pipeline') });
}

export function useEngagement(showHidden: boolean) {
  return useQuery({
    queryKey: ['engagement', showHidden],
    queryFn: () => apiGet<EngagementResponse>(`/api/engagement${showHidden ? '?show_hidden=1' : ''}`),
  });
}

export function useCampaigns() {
  return useQuery({ queryKey: ['campaigns'], queryFn: () => apiGet<CampaignsResponse>('/api/campaigns') });
}

export function useReachouts() {
  return useQuery({ queryKey: ['reachouts'], queryFn: () => apiGet<ReachoutsResponse>('/api/reachouts') });
}

export function useLogs(limit = 500) {
  return useQuery({ queryKey: ['logs', limit], queryFn: () => apiGet<LogsResponse>(`/api/logs?limit=${limit}`) });
}

export function useTemplateFailures() {
  return useQuery({
    queryKey: ['template-failures'],
    queryFn: () => apiGet<TemplateFailuresResponse>('/api/template-failures'),
  });
}

export function useSyncStatus() {
  return useQuery({
    queryKey: ['sync-status'],
    queryFn: () => apiGet<SyncStatusResponse>('/api/sync-status'),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

export function useNav() {
  return useQuery({
    queryKey: ['nav'],
    queryFn: () => apiGet<NavResponse>('/api/nav'),
    refetchInterval: 60_000,
  });
}

export function useCommands() {
  return useQuery({ queryKey: ['commands'], queryFn: () => apiGet<CommandsResponse>('/api/commands') });
}

// --- write hooks -------------------------------------------------------------
// Every mutation invalidates the queries its write action can affect —
// mirrors the same cache-invalidation-on-success discipline api.py's own
// comments call out (e.g. reply-tag mutations can move a recipient between
// several widgets: home's triage list, pipeline, reachouts, engagement).

function useInvalidateAfter(keys: string[][]) {
  const qc = useQueryClient();
  return () => keys.forEach((key) => qc.invalidateQueries({ queryKey: key }));
}

export function useTagReply(recipient: string) {
  const invalidate = useInvalidateAfter([['home'], ['pipeline'], ['reachouts'], ['engagement']]);
  return useMutation({
    mutationFn: (body: TagReplyBody) => apiPost<OkResponse>(`/api/reply/${encodeURIComponent(recipient)}/tag`, body),
    onSuccess: invalidate,
  });
}

export function useStopOutreach(recipient: string) {
  const invalidate = useInvalidateAfter([['pipeline'], ['reachouts'], ['home']]);
  return useMutation({
    mutationFn: () => apiPost<OkResponse>(`/api/reachouts/${encodeURIComponent(recipient)}/stop`, {}),
    onSuccess: invalidate,
  });
}

export function useResend(recipient: string) {
  const invalidate = useInvalidateAfter([['reachouts'], ['pipeline']]);
  return useMutation({
    mutationFn: (body: ResendBody) =>
      apiPost<OkResponse & { warning: string | null }>(`/api/reachouts/${encodeURIComponent(recipient)}/resend`, body),
    onSuccess: invalidate,
  });
}

export function useHideWarmButSilent(recipient: string, hide: boolean) {
  const invalidate = useInvalidateAfter([['engagement']]);
  return useMutation({
    mutationFn: () =>
      apiPost<OkResponse>(`/api/warm-but-silent/${encodeURIComponent(recipient)}/${hide ? 'hide' : 'unhide'}`, {}),
    onSuccess: invalidate,
  });
}

export function useLinkedinReplied(recipient: string) {
  const invalidate = useInvalidateAfter([['reachouts'], ['home'], ['pipeline']]);
  return useMutation({
    mutationFn: (body: LinkedinRepliedBody) =>
      apiPost<OkResponse>(`/api/reachouts/${encodeURIComponent(recipient)}/linkedin-replied`, body),
    onSuccess: invalidate,
  });
}

export function useFollowedUp(recipient: string) {
  const invalidate = useInvalidateAfter([['home'], ['pipeline'], ['reachouts']]);
  return useMutation({
    mutationFn: () => apiPost<OkResponse>(`/api/reachouts/${encodeURIComponent(recipient)}/followed-up`, {}),
    onSuccess: invalidate,
  });
}

// The Remind endpoint is being added on another track (see build brief) —
// POST optimistically and let the caller degrade gracefully on a 404.
export function useRemind(recipient: string) {
  return useMutation({
    mutationFn: (body: { note?: string; days?: number }) =>
      apiPost<OkResponse>(`/api/reachouts/${encodeURIComponent(recipient)}/remind`, body),
  });
}

export function useGmassRefresh() {
  const invalidate = useInvalidateAfter([['home'], ['pipeline'], ['engagement'], ['sync-status']]);
  return useMutation({
    mutationFn: () => apiPost<{ ok: boolean; reason?: string }>('/api/gmass/refresh', {}),
    onSuccess: invalidate,
  });
}
