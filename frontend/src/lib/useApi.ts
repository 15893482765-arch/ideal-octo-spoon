/** 数据获取与提交 hooks（T30 前端联调层）。
 *
 * 统一的 loading / error / 重试语义，避免每个页面各写一套 useEffect。
 */

import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type QueryValue } from "./api";

export interface QueryState<T> {
  data: T | null;
  loading: boolean;
  error: string;
  /** HTTP 状态码（404/403 等可做差异化提示） */
  status: number | null;
  refetch: () => void;
}

function messageOf(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return fallback;
}

/** GET 查询：query 变化自动重取；enabled=false 时不发请求。 */
export function useApiQuery<T>(
  path: string,
  query?: Record<string, QueryValue>,
  options?: { enabled?: boolean },
): QueryState<T> {
  const enabled = options?.enabled ?? true;
  const key = JSON.stringify(query ?? {});
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState("");
  const [status, setStatus] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    let alive = true;
    setLoading(true);
    setError("");
    api
      .get<T>(path, query)
      .then((res) => {
        if (!alive) return;
        setData(res);
        setStatus(200);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setData(null);
        setError(messageOf(err, "加载失败"));
        setStatus(err instanceof ApiError ? err.status : null);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // query 以稳定序列化 key 作为依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, key, nonce, enabled]);

  const refetch = useCallback(() => setNonce((n) => n + 1), []);
  return { data, loading, error, status, refetch };
}

/** 单次写操作的结果：`error` 为**本次**失败的真实原因（后端 message）。 */
export interface MutationOutcome<TResult> {
  ok: boolean;
  data: TResult | null;
  error: string;
}

export interface MutationState<TArgs, TResult> {
  run: (args: TArgs) => Promise<TResult | null>;
  /** 与 `run` 等价，但把本次失败原因随结果一起返回。 */
  runSafe: (args: TArgs) => Promise<MutationOutcome<TResult>>;
  submitting: boolean;
  error: string;
  reset: () => void;
}

/** 写操作：统一 submitting/error；失败返回 null 不抛（由调用方看 error）。
 *
 * 注意：`error` 是 React state，在 `await run(...)` 之后读到的仍是**上一次渲染**的值
 * （通常是 ""），导致调用方只能提示笼统的"操作失败"。需要展示真实原因时请用
 * `runSafe`，它把本次的 message 直接放在返回值里。
 */
export function useApiMutation<TArgs, TResult>(
  fn: (args: TArgs) => Promise<TResult>,
): MutationState<TArgs, TResult> {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const runSafe = useCallback(
    async (args: TArgs): Promise<MutationOutcome<TResult>> => {
      setSubmitting(true);
      setError("");
      try {
        const data = await fn(args);
        return { ok: true, data, error: "" };
      } catch (err: unknown) {
        const message = messageOf(err, "操作失败");
        setError(message);
        return { ok: false, data: null, error: message };
      } finally {
        setSubmitting(false);
      }
    },
    [fn],
  );

  const run = useCallback(
    async (args: TArgs): Promise<TResult | null> => (await runSafe(args)).data,
    [runSafe],
  );

  return { run, runSafe, submitting, error, reset: () => setError("") };
}

/** 页面级写操作（无需参数）的简写。 */
export function useAction<TResult>(
  fn: () => Promise<TResult>,
): MutationState<void, TResult> {
  return useApiMutation<void, TResult>(fn);
}
