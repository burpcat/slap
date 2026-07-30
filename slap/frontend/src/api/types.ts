// Mirrors the JSON shapes returned by slap/api.py (which in turn mirrors the
// widget functions in slap/dashboard.py). Kept as plain interfaces, one per
// widget/response, rather than one giant blob — see each endpoint in api.py
// for the exact Python source of these fields.

export type CampaignColor = { light: string; dark: string };

// --- shared / cross-page pieces -------------------------------------------

export interface SentSplit {
  new: number;
  follow_up: number;
  total: number;
}

export interface TodayStrip {
  active_campaigns: string[];
  sent: SentSplit;
  daily_cap: number;
  cap_used_pct: number;
  replies_today: number;
  clicks_today: number;
}

export interface ThisWeek {
  range_start: string;
  range_end: string;
  sent: SentSplit;
  replies: number;
  clicks: number;
}

export interface TodaysRun {
  fired_at: string;
  sent: number | null;
  failed: number | null;
  still_queued: number | null;
  run_failed: boolean;
  error: string | null;
  retry_count: number | null;
}

export interface TodaysRuns {
  runs: TodaysRun[];
  earlier_count: number;
  current_queue_depth: number;
}

export interface FollowupsScheduledEntry {
  recipient: string;
  next_stage: number;
  fire_date: string;
}

export interface FollowupsScheduled {
  today: FollowupsScheduledEntry[];
  tomorrow: FollowupsScheduledEntry[];
}

export interface Pipeline {
  mid_sequence_by_stage: Record<string, string[]>;
  followups_scheduled: FollowupsScheduled;
}

export interface NextDrain {
  fire_window_start: string;
  fire_window_end: string;
  queue_depth: number;
}

export interface CompaniesContacted {
  all_time_count: number;
  this_week_count: number;
  top_companies: [string, number][];
  // Full (domain, count) roster for the front-page company word cloud — same
  // set as top_companies, just not truncated. TLD stripping is a display concern
  // (done in the UI); the domain is the real key here.
  all_companies: [string, number][];
}

export interface ActiveLead {
  recipient: string;
  campaign: string;
  persona: string;
  company: string;
  role: string;
  real_tagged_at: string;
}

export interface FollowUpReminder extends ActiveLead {
  days_since: number;
  last_interaction_at: string | null;
  // Computed cadence date for the next personal nudge: anchor (max of
  // real-tagged / last interaction) + a fixed gap. Derived, not stored — it
  // auto-recomputes when "Followed up" moves the anchor. ISO YYYY-MM-DD.
  next_follow_up_date: string;
}

export interface ContactContext {
  recipient: string;
  campaign: string;
  persona: string;
  status: string;
  first_sent_at: string | null;
  replied_at: string | null;
}

export interface DedupResult {
  hard_warning: ContactContext | null;
  soft_warning_domain: string | null;
  soft_warning_contacts: ContactContext[] | null;
}

export interface ReplyNeedingTriage {
  recipient: string;
  campaign: string;
  stage: number;
  timestamp: string;
  dedup_context: DedupResult;
}

export interface SyncResult {
  synced_at: string | null;
  new_replies: number;
  new_clicks: number;
  new_bounces: number;
  errors: string[];
}

export type CacheStatus = 'fresh' | 'stale_refreshing' | 'redis_unavailable';

export interface EngagementIntelligence {
  reply_rate_by_persona: Record<string, number>;
  reply_by_stage: Record<string, number>;
  click_by_stage: Record<string, number>;
  time_to_first_reply: {
    same_day: number;
    '1_2_days': number;
    '3_7_days': number;
    '8_plus_days': number;
  };
  has_data: boolean;
}

export interface BounceRow {
  recipient: string;
  campaign: string;
  last_event_at: string;
  category: string;
  reason: string;
}

export interface StoppedRosterRow {
  recipient: string;
  campaign: string;
  persona: string;
  company: string;
  stopped_at: string | null;
  scope: string;
}

// --- /api/home --------------------------------------------------------------

export interface HomeResponse {
  sync_result: SyncResult;
  replies: ReplyNeedingTriage[];
  cache_status: CacheStatus;
  today: TodayStrip;
  week: ThisWeek;
  runs: TodaysRuns;
  next_drain: NextDrain;
  follow_up_reminders: FollowUpReminder[];
  pipeline: Pipeline;
  companies: CompaniesContacted;
}

// --- /api/pipeline -----------------------------------------------------------

export interface PipelineResponse {
  today: TodayStrip;
  active_leads: ActiveLead[];
  follow_up_reminders: FollowUpReminder[];
  pipeline: Pipeline;
  companies: CompaniesContacted;
  bounces: BounceRow[];
  stopped_outreach: StoppedRosterRow[];
}

// --- /api/engagement ----------------------------------------------------------

export interface TrendPoint {
  date: string;
  new: number;
  follow_up: number;
  replies: number;
}

export interface BounceWeek {
  week_start: string;
  bounce: number;
  block: number;
}

export interface BounceReasonCount {
  reason: string;
  count: number;
}

export interface BounceBreakdown {
  by_category_over_time: BounceWeek[];
  top_reasons: BounceReasonCount[];
}

export interface WeeklyGoalProgress {
  target: number;
  actual: number;
  pct: number;
}

export interface EngagementAnalytics {
  trend: TrendPoint[];
  bounce_data: BounceBreakdown;
  reply_rate_by_persona: Record<string, number>;
  time_to_first_reply: EngagementIntelligence['time_to_first_reply'];
  weekly_goal: WeeklyGoalProgress | null;
}

export interface ClickDetail {
  url: string;
  stage: number;
  click_time: string | null;
}

export interface WarmButSilentRow {
  recipient: string;
  campaign: string;
  stages_clicked: number[];
  clicks: ClickDetail[];
}

export interface EngagementResponse {
  sync_result: SyncResult;
  cache_status: CacheStatus;
  engagement: EngagementIntelligence;
  warm_but_silent: WarmButSilentRow[];
  warm_but_silent_hidden_count: number;
  show_hidden: boolean;
  'engagement-analytics': EngagementAnalytics;
}

// --- /api/campaigns ------------------------------------------------------------

export interface CampaignSlice {
  campaign: string;
  color: CampaignColor;
  recipient_count: number;
  reply_count: number;
  click_count: number;
  active_lead_count: number;
}

export interface CampaignsResponse {
  campaigns: CampaignSlice[];
}

// --- /api/reachouts --------------------------------------------------------------

export interface StatusChip {
  color: 'good' | 'serious' | 'critical' | null;
  label: string;
}

export interface ReachoutRow {
  recipient: string;
  campaign: string;
  persona: string;
  status: string;
  engagement: 'replied' | 'clicked' | 'none';
  reply_tag: string | null;
  domain: string;
  company: string;
  name: string;
  req_id_present: boolean;
  date: string | null;
  ooo_resume_date: string | null;
  bounce_category: string | null;
  bounce_reason: string | null;
  corrected_from: string | null;
  already_corrected_to: { recipient: string; status: string }[];
  clicks: ClickDetail[];
  linkedin_replied: boolean;
  // Outreach halted because they replied on LinkedIn (status 'linkedin-gate') —
  // one-way, like `stopped`. Durable, append-only read (see dashboard.py).
  linkedin_gated: boolean;
  stopped: boolean;
  chip: StatusChip;
  date_local: string | null;
}

export interface ReachoutsResponse {
  rows: ReachoutRow[];
  total_count: number;
  campaign_colors: Record<string, CampaignColor>;
}

// --- /api/logs ------------------------------------------------------------------

export interface LogEvent {
  id: number;
  timestamp: string;
  type: string;
  recipient: string | null;
  campaign: string | null;
  stage: number | null;
  gmass_campaign_id: string | null;
  gmass_draft_id: string | null;
  meta: Record<string, unknown>;
  display: { label: string; chip: string; detail: string };
}

export interface LogsResponse {
  events: LogEvent[];
  total_count: number;
  limit: number;
  truncated: boolean;
  event_types: string[];
  logs: Record<string, string[]>;
}

// --- /api/template-failures -------------------------------------------------------

export interface TemplateFailure {
  campaign?: string;
  file?: string;
  error?: string;
  [key: string]: unknown;
}

export interface TemplateFailuresResponse {
  failures: TemplateFailure[];
  total_count: number;
}

// --- /api/sync-status --------------------------------------------------------------

export interface SyncStatusResponse {
  sync_result: SyncResult;
  cache_status: CacheStatus;
}

// --- /api/nav ------------------------------------------------------------------------

export interface NavResponse {
  template_failures_count: number;
  runner_staleness_warning: string | null;
}

// --- /api/commands ---------------------------------------------------------------------

export interface CommandArg {
  name: string;
  flags: string[];
  help: string;
  required: boolean;
  choices: string[] | null;
}

export interface CommandSpec {
  name: string;
  help: string;
  usage: string;
  args: CommandArg[];
  // Concrete example invocations, using the owner's real campaign names /
  // recipient where relevant (see _command_examples in slap/api.py).
  examples: string[];
}

export interface CommandsResponse {
  commands: CommandSpec[];
}

// --- write endpoints: request/response bodies ------------------------------------------

export type ReplyTag = 'real' | 'ooo' | 'not_interested' | 'unreal';

export interface TagReplyBody {
  tag: ReplyTag;
  resume_date?: string;
}

export interface ResendBody {
  corrected_email: string;
}

export interface LinkedinRepliedBody {
  replied: boolean;
}

export interface OkResponse {
  ok: true;
  [key: string]: unknown;
}

export interface ErrorResponse {
  error: string;
}
