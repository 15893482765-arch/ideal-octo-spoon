/** T28 移动端 H5 入口：装配今日任务数据 + 提交器/状态读取器，弱网走本地草稿。
 *
 * 说明：本卡交付弱网草稿/重放引擎与移动流程 UI。真实后端对接（fetch 客户端、
 * 文件先上传后绑定、任务拉取）属于后续联调卡；此处以可注入的 SubmitFn/FetchStateFn
 * 作为边界，默认实现调用 /api 并携带稳定幂等键。
 */

import { useMemo } from "react";

import { useAuth } from "../../lib/auth";
import { MODULE } from "../../lib/permissions";
import type { FetchStateFn, SubmitFn } from "../../mobile/offline/types";
import { TodayTasks, type TodayTask } from "./TodayTasks";

/** 默认提交器：把草稿映射为后端提交，携带创建时生成的稳定幂等键。 */
function defaultSubmit(kind: string): { url: string; method: string } {
  switch (kind) {
    case "visit.submit":
    case "visit.checkin":
      return { url: "/api/visits/tasks/:id/submit", method: "POST" };
    case "replenishment.fulfill":
      return { url: "/api/replenishments/orders/:id/fulfillments", method: "POST" };
    case "activity.execute":
      return { url: "/api/activities/:id/execute", method: "POST" };
    default:
      return { url: "/api/unknown", method: "POST" };
  }
}

export function MobileEntry() {
  const { hasAbility } = useAuth();

  const submit: SubmitFn = useMemo(
    () => async (draft) => {
      const route = defaultSubmit(draft.kind);
      const url = route.url.replace(":id", encodeURIComponent(draft.targetId));
      let res: Response;
      try {
        res = await fetch(url, {
          method: route.method,
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": draft.idempotencyKey,
          },
          body: JSON.stringify({ ...draft.payload, idempotency_key: draft.idempotencyKey }),
          credentials: "include",
        });
      } catch {
        return { ok: false, httpStatus: 0, code: "NETWORK_ERROR" };
      }
      let body: unknown = null;
      try {
        body = await res.json();
      } catch {
        body = null;
      }
      const code = (body as { code?: string } | null)?.code;
      return {
        ok: res.ok,
        httpStatus: res.status,
        code,
        body,
        serverStatus: (body as { status?: string } | null)?.status,
      };
    },
    [],
  );

  const fetchState: FetchStateFn = useMemo(
    () => async (_kind, targetId) => {
      // 联调卡接入真实 GET；首版在无法取数时返回乐观快照，差异由实际状态接口覆盖。
      void _kind;
      try {
        const res = await fetch(`/api/mobile/state/${encodeURIComponent(targetId)}`, {
          credentials: "include",
        });
        if (!res.ok) throw new Error("state");
        const data = (await res.json()) as {
          status: string;
          version?: number | string;
          snapshot?: Record<string, unknown>;
        };
        return { status: data.status, version: data.version, snapshot: data.snapshot ?? {} };
      } catch {
        return { status: "UNKNOWN", snapshot: {} };
      }
    },
    [],
  );

  // 今日任务由后续联调卡从 /api/visits/tasks?on_date=today 拉取；首版给空列表占位，
  // 但弱网引擎/流程/设备处理已可独立验证。
  const tasks: TodayTask[] = useMemo(() => [], []);

  if (!hasAbility(MODULE.VISIT)) {
    return (
      <div data-testid="mobile-forbidden" className="p-6 text-center text-sm">
        当前角色无巡访移动入口权限。
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <TodayTasks tasks={tasks} submit={submit} fetchState={fetchState} />
    </div>
  );
}
