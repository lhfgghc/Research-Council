import type {
  CallSummary,
  FeedbackRecord,
  Idea,
  Project,
  ProjectDetail,
  RunDetail,
  RunBudgetPreview,
  RunSummary,
  Settings,
  Skill,
} from "./types";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(options?.headers || {}),
    },
  });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      if (Array.isArray(payload.detail)) {
        message = payload.detail.map((item: { msg?: string }) => item.msg).filter(Boolean).join("，") || message;
      } else {
        message = payload.detail || payload.error || message;
      }
    } catch {
      // Keep the status-based message when the response is not JSON.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

function jsonBody(payload: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(payload) };
}

export const api = {
  health: () =>
    request<{ status: string; model: string; api_configured: boolean; configured_models: number }>("/api/health"),

  getSettings: () => request<Settings>("/api/settings"),

  updateSettings: (payload: Record<string, unknown>) =>
    request<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(payload) }),

  revealModelKey: (id: string) =>
    request<{ api_key: string }>(`/api/settings/models/${encodeURIComponent(id)}/key`, { cache: "no-store" }),

  testModel: (id: string) =>
    request<{ ok: boolean; model: string }>(`/api/settings/models/${encodeURIComponent(id)}/test`, { method: "POST" }),

  listSkills: () => request<{ skills: Skill[]; directories: string[] }>("/api/skills"),

  listProjects: () => request<Project[]>("/api/projects"),

  getOverview: () => request<import("./types").Overview>("/api/overview"),

  createProject: (payload: Record<string, unknown>) =>
    request<Project>("/api/projects", jsonBody(payload)),

  archiveProject: (projectId: string) =>
    request<Project>(`/api/projects/${projectId}/archive`, { method: "POST" }),

  unarchiveProject: (projectId: string) =>
    request<Project>(`/api/projects/${projectId}/unarchive`, { method: "POST" }),

  getProject: (projectId: string) => request<ProjectDetail>(`/api/projects/${projectId}`),

  createRun: (projectId: string, payload: Record<string, unknown>) =>
    request<RunSummary>(`/api/projects/${projectId}/runs`, jsonBody(payload)),

  previewRunBudget: (payload: Record<string, unknown>, signal: AbortSignal) =>
    request<RunBudgetPreview>("/api/runs/budget-preview", { ...jsonBody(payload), signal }),

  getRun: (runId: string) => request<RunDetail>(`/api/runs/${runId}`),

  pauseRun: (runId: string) =>
    request<{ status: string }>(`/api/runs/${runId}/pause`, { method: "POST" }),

  resumeRun: (runId: string) =>
    request<{ status: string }>(`/api/runs/${runId}/resume`, { method: "POST" }),

  cancelRun: (runId: string) =>
    request<{ status: string }>(`/api/runs/${runId}/cancel`, { method: "POST" }),

  updateBudget: (runId: string, maxCalls: number) =>
    request<{ budget_used: number; budget_max: number; status: string; resumed: boolean }>(
      `/api/runs/${runId}/budget`,
      jsonBody({ max_calls: maxCalls }),
    ),

  addRunInput: (runId: string, payload: { kind: string; content: string }) =>
    request<{ id: number; status: string; note?: string }>(
      `/api/runs/${runId}/inputs`,
      jsonBody(payload),
    ),

  getCalls: (runId: string) => request<CallSummary>(`/api/runs/${runId}/calls`),

  getIdea: (ideaId: string) => request<Idea>(`/api/ideas/${ideaId}`),

  getIdeaVersion: (ideaId: string, version: number) =>
    request<{
      version: number;
      path: string;
      document: string;
      revision_path: string | null;
      revision_document: string;
      manifest: Record<string, unknown>;
    }>(`/api/ideas/${ideaId}/versions/${version}`),

  submitFeedback: (ideaId: string, form: FormData) =>
    request<{ feedback_id: string; run: RunSummary }>(`/api/ideas/${ideaId}/feedback`, {
      method: "POST",
      body: form,
    }),

  listFeedback: (projectId: string) =>
    request<FeedbackRecord[]>(`/api/projects/${projectId}/feedback`),
};
