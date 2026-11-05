/** 片区管理（T02 / PRD §2.3、§4.1）。
 *
 * 片区是行级数据范围的最小单位：业务员、经理、督导、厂商账号的可视范围都按片区裁剪。
 * 角色边界（后端 `_require_region_manage` 二次鉴权，前端只做入口裁剪）：
 * - SYSTEM_ADMIN（settings）与 REGIONAL_MANAGER（region.manage）可新建片区；
 * - 其他角色既看不到入口，直接调接口也会被 403 拒绝。
 *
 * 主数据「只停用不删除」：本页只提供新增，不提供物理删除与就地改写。
 * 新建成功后账号建号表单的品牌/片区下拉即时可选。
 */
import { useState } from "react";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useAction, useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

interface RegionRow {
  id: string;
  code: string;
  name: string;
  is_active: boolean;
}

const BASE = "/admin/master-data";

export function Regions() {
  const { authenticated, hasAbility } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const regions = useApiQuery<RegionRow[]>(`${BASE}/regions`, undefined, {
    enabled: authenticated,
  });

  const [code, setCode] = useState("");
  const [name, setName] = useState("");

  const createMut = useAction(() =>
    api.post<RegionRow>(`${BASE}/regions`, { code: code.trim(), name: name.trim() }),
  );

  const columns: DataTableColumn<RegionRow>[] = [
    {
      key: "code",
      header: "片区编码",
      render: (r) => <span className="font-mono text-xs">{r.code}</span>,
    },
    { key: "name", header: "片区名称", render: (r) => r.name },
    {
      key: "is_active",
      header: "状态",
      render: (r) => (
        <StatusBadge
          tone={r.is_active ? "success" : "neutral"}
          label={r.is_active ? "启用" : "已停用"}
        />
      ),
    },
  ];

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="片区管理" description="片区新建与台账（行级数据范围的最小单位）" />
        <SessionNotice hint="片区管理需要真实登录后的系统管理员或片区经理会话（后端按 settings / region.manage 二次鉴权）。" />
      </div>
    );
  }

  if (!hasAbility("settings") && !hasAbility("region.manage")) {
    return (
      <div className="space-y-4">
        <PageHeader title="片区管理" description="片区新建与台账（行级数据范围的最小单位）" />
        <SessionNotice hint="当前角色无片区管理能力：仅系统管理员与片区经理可新建片区。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="片区管理"
        description="新建片区后，账号建号表单的品牌/片区授权下拉即可选择；主数据只停用不删除，本页不提供物理删除"
      />

      <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-700">新建片区</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            createMut.run().then((res) => {
              if (res) {
                push(`片区 ${res.code}（${res.name}）已创建`, "success");
                setCode("");
                setName("");
                regions.refetch();
              } else {
                push(createMut.error || "创建失败", "danger");
              }
            });
          }}
          className="space-y-3"
        >
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <label className="flex flex-col gap-1 text-xs text-slate-600">
              <span>片区编码 *（≤32 字符，全局唯一）</span>
              <input
                required
                maxLength={32}
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="如 HN-ZZ"
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-600">
              <span>片区名称 *（≤128 字符）</span>
              <input
                required
                maxLength={128}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="如 郑州片区"
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </label>
            <div className="flex items-end">
              <button
                type="submit"
                disabled={createMut.submitting || !code.trim() || !name.trim()}
                className="rounded-md bg-[var(--color-brand)] px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {createMut.submitting ? "创建中…" : "创建片区"}
              </button>
            </div>
          </div>
          {createMut.error ? (
            <div className="text-xs text-rose-600">{createMut.error}</div>
          ) : null}
          <p className="text-xs text-slate-400">
            编码重复会被拒绝（409），不会静默覆盖既有片区；如需调整名称请走主数据导入并保留批次痕迹。
          </p>
        </form>
      </section>

      <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-700">
            片区台账（{regions.data?.length ?? 0}）
          </h2>
          <button
            type="button"
            onClick={regions.refetch}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
          >
            刷新
          </button>
        </div>

        {regions.loading ? <LoadingBlock text="加载片区…" /> : null}
        {regions.error ? (
          <ErrorState message={regions.error} status={regions.status} onRetry={regions.refetch} />
        ) : null}
        {!regions.loading && !regions.error ? (
          <DataTable
            rows={regions.data ?? []}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="暂无片区：用上方表单创建第一个片区"
          />
        ) : null}
      </section>

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}
