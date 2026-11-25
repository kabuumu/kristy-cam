import { usageLogger, type UsageLogEvent } from "../utils/usageLogger";
import type {
  Capabilities,
  ExportOptions,
  JobAccepted,
  JobState,
  MediaAssetImportResult,
  MediaAssetKind,
  MediaSelectResult,
  ProjectBundle,
  ProjectInput,
  ProjectSummary,
} from "../types/studio";

export class ApiError extends Error {
  status: number;
  detail: string;
  payload: unknown;

  constructor(status: number, detail: string, payload?: unknown) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.payload = payload;
  }
}

export function getSessionToken(): string | null {
  const meta = document.querySelector('meta[name="session-token"]');
  const token = meta?.getAttribute("content")?.trim() ?? "";
  if (!token || token === "__SESSION_TOKEN__") {
    return null;
  }
  return token;
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const startedAt = performance.now();
  const headers = new Headers(options.headers);
  const token = getSessionToken();
  if (token) {
    headers.set("X-Session-Token", token);
  }
  if (
    options.body &&
    !(options.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(path, {
      ...options,
      headers,
    });
  } catch (err) {
    const durationMs = performance.now() - startedAt;
    if (!path.startsWith("/api/logs/events")) {
      usageLogger.logError(
        `API network failure: ${method} ${path}`,
        "api.network_error",
        err,
        {
          target: `${method} ${path}`,
          durationMs,
        }
      );
    }
    throw err;
  }

  const durationMs = performance.now() - startedAt;
  const contentType = response.headers.get("content-type") ?? "";
  let payload: unknown = null;
  if (contentType.includes("application/json")) {
    payload = await response.json();
  } else {
    const text = await response.text();
    payload = text || null;
  }

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    if (payload && typeof payload === "object" && "detail" in payload) {
      const rawDetail = (payload as { detail: unknown }).detail;
      detail =
        typeof rawDetail === "string"
          ? rawDetail
          : JSON.stringify(rawDetail);
    } else if (typeof payload === "string" && payload.trim()) {
      detail = payload;
    }
    const apiErr = new ApiError(response.status, detail, payload);
    if (!path.startsWith("/api/logs/events")) {
      usageLogger.logError(
        `API error ${response.status}: ${method} ${path}`,
        "api.error",
        apiErr,
        {
          target: `${method} ${path}`,
          statusCode: response.status,
          durationMs,
          context: { detail },
        }
      );
    }
    throw apiErr;
  }

  return payload as T;
}

export const apiClient = {
  health(): Promise<{
    status: string;
    app?: string;
    session_token?: string;
  }> {
    return request("/api/health", { method: "GET" });
  },

  capabilities(): Promise<Capabilities> {
    return request("/api/capabilities", { method: "GET" });
  },

  selectMedia(options?: {
    path?: string;
    file?: File;
    copyToProject?: boolean;
  }): Promise<MediaSelectResult> {
    if (options?.file) {
      const form = new FormData();
      form.append("file", options.file);
      if (options.copyToProject !== undefined) {
        form.append("copy_to_project", String(options.copyToProject));
      }
      return request("/api/media/select", {
        method: "POST",
        body: form,
      });
    }
    const body: Record<string, unknown> = {};
    if (options?.path) {
      body.path = options.path;
    }
    if (options?.copyToProject !== undefined) {
      body.copy_to_project = options.copyToProject;
    }
    return request("/api/media/select", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  async listProjects(): Promise<ProjectSummary[]> {
    const data = await request<ProjectSummary[] | { projects: ProjectSummary[] }>(
      "/api/projects",
      { method: "GET" }
    );
    return Array.isArray(data) ? data : data.projects ?? [];
  },

  createProject(params: {
    media_id: string;
    title: string;
    copy_to_project?: boolean;
  }): Promise<{
    project?: ProjectInput;
    project_id?: string;
    job_id?: string;
  }> {
    return request("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        media_id: params.media_id,
        title: params.title,
        copy_source: params.copy_to_project ?? false,
        copy_to_project: params.copy_to_project ?? false,
      }),
    });
  },

  getProject(projectId: string): Promise<ProjectBundle> {
    return request(`/api/projects/${encodeURIComponent(projectId)}`, {
      method: "GET",
    });
  },

  saveProject(
    projectId: string,
    project: ProjectInput,
    expectedRevision: number
  ): Promise<ProjectBundle> {
    return request(`/api/projects/${encodeURIComponent(projectId)}`, {
      method: "PUT",
      body: JSON.stringify({
        project,
        expected_revision: expectedRevision,
      }),
    });
  },

  relinkSource(
    projectId: string,
    options?: { path?: string; file?: File }
  ): Promise<ProjectBundle & { job_id?: string }> {
    if (options?.file) {
      const form = new FormData();
      form.append("file", options.file);
      return request(
        `/api/projects/${encodeURIComponent(projectId)}/source/relink`,
        {
          method: "POST",
          body: form,
        }
      );
    }
    return request(
      `/api/projects/${encodeURIComponent(projectId)}/source/relink`,
      {
        method: "POST",
        body: JSON.stringify(options?.path ? { path: options.path } : {}),
      }
    );
  },

  importProjectAsset(
    projectId: string,
    options?: {
      path?: string;
      file?: File;
      kind?: MediaAssetKind;
      name?: string;
      copyToProject?: boolean;
    }
  ): Promise<MediaAssetImportResult> {
    if (options?.file) {
      const form = new FormData();
      form.append("file", options.file);
      if (options.kind) {
        form.append("kind", options.kind);
      }
      if (options.name) {
        form.append("name", options.name);
      }
      if (options.copyToProject !== undefined) {
        form.append("copy_to_project", String(options.copyToProject));
      }
      return request(`/api/projects/${encodeURIComponent(projectId)}/assets`, {
        method: "POST",
        body: form,
      });
    }
    const body: Record<string, unknown> = {};
    if (options?.path) body.path = options.path;
    if (options?.kind) body.kind = options.kind;
    if (options?.name) body.name = options.name;
    if (options?.copyToProject !== undefined) {
      body.copy_to_project = options.copyToProject;
    }
    return request(`/api/projects/${encodeURIComponent(projectId)}/assets`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  deleteProjectAsset(projectId: string, assetId: string): Promise<ProjectBundle> {
    return request(
      `/api/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(assetId)}`,
      { method: "DELETE" }
    );
  },

  synthesizeTts(projectId: string, segId: string): Promise<JobAccepted> {
    return request(
      `/api/projects/${encodeURIComponent(projectId)}/segments/${encodeURIComponent(segId)}/tts`,
      { method: "POST" }
    );
  },

  synthesizeProjectTts(
    projectId: string,
    clipIds?: string[]
  ): Promise<JobAccepted & { target_clip_ids?: string[] }> {
    return request(`/api/projects/${encodeURIComponent(projectId)}/tts`, {
      method: "POST",
      body: clipIds ? JSON.stringify({ clip_ids: clipIds }) : undefined,
    });
  },

  renderPreview(projectId: string): Promise<JobAccepted> {
    return request(
      `/api/projects/${encodeURIComponent(projectId)}/preview`,
      { method: "POST" }
    );
  },

  exportProject(projectId: string, options?: ExportOptions): Promise<JobAccepted> {
    return request(`/api/projects/${encodeURIComponent(projectId)}/export`, {
      method: "POST",
      body: options ? JSON.stringify(options) : undefined,
    });
  },

  renameProject(projectId: string, title: string): Promise<ProjectBundle> {
    return request(`/api/projects/${encodeURIComponent(projectId)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
  },

  duplicateProject(projectId: string, title?: string): Promise<ProjectBundle> {
    return request(`/api/projects/${encodeURIComponent(projectId)}/duplicate`, {
      method: "POST",
      body: title ? JSON.stringify({ title }) : undefined,
    });
  },

  deleteProject(projectId: string): Promise<{ deleted: boolean; project_id: string }> {
    return request(`/api/projects/${encodeURIComponent(projectId)}`, {
      method: "DELETE",
    });
  },

  subtitlesSrtUrl(projectId: string): string {
    return `/api/projects/${encodeURIComponent(projectId)}/subtitles.srt`;
  },

  assetThumbnailUrl(projectId: string, assetId: string): string {
    return `/api/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(assetId)}/thumbnail`;
  },

  getAssetWaveform(
    projectId: string,
    assetId: string,
    buckets = 120
  ): Promise<{ asset_id: string; peaks: number[] }> {
    return request(
      `/api/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(assetId)}/waveform?buckets=${buckets}`,
      { method: "GET" }
    );
  },

  getMediaWaveform(
    projectId: string,
    artifactId: string,
    buckets = 80
  ): Promise<{ artifact_id: string; peaks: number[] }> {
    return request(
      `/api/projects/${encodeURIComponent(projectId)}/media/${encodeURIComponent(artifactId)}/waveform?buckets=${buckets}`,
      { method: "GET" }
    );
  },

  getJob(jobId: string): Promise<JobState> {
    return request(`/api/jobs/${encodeURIComponent(jobId)}`, {
      method: "GET",
    });
  },

  cancelJob(jobId: string): Promise<JobState> {
    return request(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
    });
  },

  mediaUrl(projectId: string, artifactId: string): string {
    return `/api/projects/${encodeURIComponent(projectId)}/media/${encodeURIComponent(artifactId)}`;
  },

  async getPronunciations(): Promise<Record<string, string>> {
    const data = await request<
      Record<string, string> | { pronunciations: Record<string, string> }
    >("/api/pronunciations", { method: "GET" });
    if (
      data &&
      typeof data === "object" &&
      "pronunciations" in data &&
      typeof data.pronunciations === "object"
    ) {
      return data.pronunciations as Record<string, string>;
    }
    return data as Record<string, string>;
  },

  savePronunciations(
    pronunciations: Record<string, string>
  ): Promise<Record<string, string>> {
    return request("/api/pronunciations", {
      method: "PUT",
      body: JSON.stringify({ pronunciations }),
    });
  },

  getLogs(options?: {
    limit?: number;
    category?: string;
    projectId?: string;
  }): Promise<{
    events: UsageLogEvent[];
    count: number;
    usage_log_path: string;
    debug_log_path: string;
  }> {
    const params = new URLSearchParams();
    if (options?.limit) params.set("limit", String(options.limit));
    if (options?.category) params.set("category", options.category);
    if (options?.projectId) params.set("project_id", options.projectId);
    const qs = params.toString();
    return request(`/api/logs${qs ? `?${qs}` : ""}`, { method: "GET" });
  },
};
