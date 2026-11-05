/** HTTP 客户端（T30 前端联调层）。
 *
 * 约定（与后端 T01/T03 一致）：
 * - 统一前缀 `/api`（Vite dev 经 proxy 转发到 :8000）；
 * - 鉴权：`Authorization: Bearer <access_token>`，token 由登录接口签发；
 * - 错误体为扁平结构 `{ code, message, request_id, retry_after_seconds? }`
 *   （登录限流 429 会带 retry_after_seconds）；
 * - 401 时用 refresh_token 静默续期一次，仍失败则清空凭据并广播登出。
 *
 * 红线：不在此处打印或持久化任何凭据内容以外的敏感数据（照片/合同正文等）。
 */

const ACCESS_KEY = "qs.access_token";
const REFRESH_KEY = "qs.refresh_token";

export interface ApiErrorBody {
  code?: string;
  message?: string;
  request_id?: string;
  retry_after_seconds?: number;
  detail?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly requestId?: string;
  readonly retryAfterSeconds?: number;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message ?? `请求失败（HTTP ${status}）`);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.requestId = body.request_id;
    this.retryAfterSeconds = body.retry_after_seconds;
  }
}

export function getAccessToken(): string | null {
  try {
    return localStorage.getItem(ACCESS_KEY);
  } catch {
    return null;
  }
}

export function getRefreshToken(): string | null {
  try {
    return localStorage.getItem(REFRESH_KEY);
  } catch {
    return null;
  }
}

export function setTokens(accessToken: string, refreshToken?: string | null): void {
  try {
    localStorage.setItem(ACCESS_KEY, accessToken);
    if (refreshToken) localStorage.setItem(REFRESH_KEY, refreshToken);
  } catch {
    /* 隐私模式等场景下忽略：退化为纯内存态（下次请求会 401） */
  }
}

export function clearTokens(): void {
  try {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  } catch {
    /* 忽略 */
  }
}

/** 登出广播：AuthProvider 订阅它以便清理用户态。 */
const UNAUTHORIZED_EVENT = "qs:unauthorized";
export function onUnauthorized(handler: () => void): () => void {
  window.addEventListener(UNAUTHORIZED_EVENT, handler);
  return () => window.removeEventListener(UNAUTHORIZED_EVENT, handler);
}
function emitUnauthorized(): void {
  window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
}

export type QueryValue = string | number | boolean | null | undefined;

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  /** 查询参数：undefined / null / "" 自动剔除 */
  query?: Record<string, QueryValue>;
  body?: unknown;
  /** multipart 上传时传 FormData，跳过 JSON 序列化 */
  formData?: FormData;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = path.startsWith("/api") ? path : `/api${path}`;
  if (!query) return url;
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    sp.append(k, String(v));
  }
  const qs = sp.toString();
  return qs ? `${url}?${qs}` : url;
}

async function parseBody(res: Response): Promise<ApiErrorBody> {
  try {
    const data = (await res.json()) as ApiErrorBody;
    return typeof data === "object" && data !== null ? data : {};
  } catch {
    return {};
  }
}

async function refreshOnce(): Promise<boolean> {
  const refresh = getRefreshToken();
  if (!refresh) return false;
  try {
    const res = await fetch(buildUrl("/api/auth/refresh"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { access_token?: string; refresh_token?: string };
    if (!data.access_token) return false;
    setTokens(data.access_token, data.refresh_token);
    return true;
  } catch {
    return false;
  }
}

/** 统一请求入口：自动注入 token、query、JSON、401 续期与错误体解析。 */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", query, body, formData, signal } = options;

  const send = async (): Promise<Response> => {
    const headers: Record<string, string> = {};
    const token = getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    let payload: BodyInit | undefined;
    if (formData) {
      payload = formData; // 由浏览器设置 multipart 边界
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    return fetch(buildUrl(path, query), { method, headers, body: payload, signal });
  };

  let res = await send();
  // 登录/刷新接口的 401 是业务结果（账号或密码错误、账号未激活/停用、被锁定），
  // 不能走"续期失败 → 会话失效"分支，否则后端的真实原因会被改写为「登录已失效」。
  const isCredentialEndpoint = /\/auth\/(login|refresh)$/.test(path);
  if (res.status === 401 && !isCredentialEndpoint) {
    const refreshed = await refreshOnce();
    if (refreshed) {
      res = await send();
    } else {
      clearTokens();
      emitUnauthorized();
      throw new ApiError(401, { code: "UNAUTHORIZED", message: "登录已失效，请重新登录" });
    }
  }

  if (!res.ok) {
    throw new ApiError(res.status, await parseBody(res));
  }

  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

export const api = {
  get: <T>(path: string, query?: Record<string, QueryValue>) => apiFetch<T>(path, { query }),
  post: <T>(path: string, body?: unknown, query?: Record<string, QueryValue>) =>
    apiFetch<T>(path, { method: "POST", body, query }),
  put: <T>(path: string, body?: unknown) => apiFetch<T>(path, { method: "PUT", body }),
  patch: <T>(path: string, body?: unknown) => apiFetch<T>(path, { method: "PATCH", body }),
  del: <T>(path: string, query?: Record<string, QueryValue>) =>
    apiFetch<T>(path, { method: "DELETE", query }),
  /** 上传：返回后端文件对象（attachment_id 等） */
  upload: <T>(path: string, file: File, query?: Record<string, QueryValue>) => {
    const fd = new FormData();
    fd.append("file", file);
    return apiFetch<T>(path, { method: "POST", formData: fd, query });
  },
};
