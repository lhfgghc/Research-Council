export type RunStatus =
  | "DRAFT"
  | "RUNNING"
  | "PAUSED"
  | "WAITING_USER"
  | "COMPLETED"
  | "COMPLETED_WITH_WARNINGS"
  | "BUDGET_EXHAUSTED"
  | "FAILED"
  | "CANCELLED";

export interface Skill {
  id: string;
  name: string;
  description: string;
  path: string;
}

export interface ModelProfile {
  id: string;
  name: string;
  model: string;
  base_url: string;
  api_format?: "openai" | "anthropic";
  api_key?: string;
  has_api_key?: boolean;
  reasoning_effort: string;
  context_window_tokens: number;
  max_input_tokens: number | null;
  input_price_per_million: number;
  output_price_per_million: number;
  strengths: string[];
}

export interface Settings {
  skills_dirs: string[];
  max_run_cost_usd: number;
  default_skills: string[];
  writing_skills: string[];
  request_timeout_seconds: number;
  context_compression_enabled: boolean;
  context_compression_trigger: number;
  context_compression_hard: number;
  model_profiles: ModelProfile[];
  role_models: Record<string, string[]>;
  discussion_models?: string[] | null;
  has_tavily_key?: boolean;
}

export interface RunBudgetPreview {
  core: number;
  team: number;
  team_models: number;
  debate: number;
  debate_models: number;
  panels: number;
  full: number;
  recommended: number;
  error: string;
}

export interface IdeaVersion {
  id: string;
  version: number;
  path: string;
  revision_path: string | null;
  created_at: string;
  manifest: Record<string, unknown>;
}

export interface Idea {
  id: string;
  project_id: string;
  run_id: string;
  title: string;
  slug: string;
  summary: string;
  status: string;
  current_version: number;
  parent_idea_id: string | null;
  created_at: string;
  updated_at: string;
  versions: IdeaVersion[];
  document?: string;
  revision_document?: string;
  current_path?: string;
  portfolio?: string;
}

export interface RunSummary {
  id: string;
  project_id: string;
  mode: "GENERATE" | "REVISE";
  status: RunStatus;
  phase: string;
  phase_label: string;
  budget_max: number;
  budget_used: number;
  candidate_count: number;
  idea_count: number;
  team_rounds: number;
  model: string;
  language: string;
  error: string | null;
  created_at: string;
  updated_at: string;
  ended_at: string | null;
  config: Record<string, unknown>;
  ideas: Idea[];
  call_count: number;
}

export interface LatestRun {
  id: string;
  status: RunStatus;
  phase: string;
  phase_label: string;
  created_at: string;
}

export interface Project {
  id: string;
  name: string;
  topic: string;
  venue: string;
  domain: string;
  constraints: string;
  language: string;
  skills: string[];
  created_at: string;
  updated_at: string;
  idea_count_actual: number;
  latest_run: LatestRun | null;
  archived?: boolean;
  archived_at?: string | null;
}

export interface ProjectDetail extends Project {
  runs: RunSummary[];
  ideas: Idea[];
}

export interface Message {
  id: number;
  role: string;
  role_label: string;
  message_type: string;
  content: string;
  structured?: Record<string, unknown>;
  scope?: string;
  created_at: string;
}

export interface Decision {
  id: number;
  title: string;
  content: string;
  rationale: string;
  created_at: string;
}

export interface RunDetail extends RunSummary {
  project: Project;
  messages: Message[];
  decisions: Decision[];
  state: Record<string, unknown>;
  pending_inputs?: Array<{ id: number; kind: string; content: string; created_at: string }>;
  budget_changes?: Array<{ id: number; previous_max: number; max: number; used: number; created_at: string }>;
  portfolio_path?: string;
  portfolio_content?: string;
  context?: {
    id: number;
    purpose: string;
    estimated_tokens: number;
    budget_tokens: number;
    ratio: number;
    compressed: number;
    strategy: string;
    summary_id: number | null;
    actual_prompt_tokens: number;
    created_at: string;
  } | null;
  context_stats?: {
    snapshots: number;
    compressions: number;
    peak_prompt_tokens: number;
  } | null;
  context_summaries?: Array<{
    id: number;
    scope: string;
    covered_start: number;
    covered_end: number;
    token_count: number;
    created_at: string;
  }>;
}

export interface ModelCall {  id: number;
  role: string;
  purpose: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  latency_ms: number;
  status: string;
  error: string | null;
  created_at: string;
}

export interface CallSummary {
  calls: ModelCall[];
  totals: {
    calls: number;
    prompt_tokens: number;
    completion_tokens: number;
    cost_usd: number;
  };
}

export interface FeedbackRecord {
  id: string;
  idea_id: string;
  base_version: number;
  instructions: string;
  created_at: string;
  title: string;
}

export interface Overview {
  projects: { total: number; active: number; archived: number };
  ideas: number;
  runs: { total: number; running: number; completed: number; failed: number };
  calls: { total: number; cost_usd: number; prompt_tokens: number; completion_tokens: number };
  recent_projects: Project[];
  model_team: {
    profiles: Array<{ id: string; name: string; model: string }>;
  };
}
