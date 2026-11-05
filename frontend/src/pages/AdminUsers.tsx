/** 用户与账号管理（T03 前端联调 / PRD §2.1、§2.2、§2.4）。

接入后端：
- `GET /api/admin/users`（账号列表，含品牌/片区授权信息）；
- `GET /api/admin/users/roles`（角色下拉源）；
- `GET /api/admin/users/suppliers`（厂商账号创建时供应商下拉）；
- `POST /api/admin/users`（建号，可携带 supplier_id + brand_ids + region_ids）；
- `POST /api/admin/users/{id}/deactivate`（需填写原因，幂等）；
- `POST /api/admin/users/{id}/reactivate`（重新签发一次性激活凭据）；
- `POST /api/admin/users/{id}/change-role`（角色变更后旧 token 立即失效）。

红线（全部由后端 UserAdminService 裁决，前端只做提示与防误操作）：
- 无注册入口，账号只能由系统管理员创建/停用/重置；
- 不存在「初始密码」：明文一次性 Activation 凭据只在创建/重新激活的响应里出现一次；
- 管理员不能停用或降权自己（CANNOT_DEACTIVATE_SELF / CANNOT_CHANGE_OWN_ROLE）。
*/
import { useState } from "react";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { label, statusTone } from "../lib/format";
import { useApiMutation, useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";
import type { ToastTone } from "../components/Toast";

interface UserRow {
  id: string;
  username: string;
  display_name: string;
  role_code: string;
  status: string;
  must_change_password: boolean;
  is_locked: boolean;
  supplier_ids: string[];
  brand_codes: string[];
  region_codes: string[];
}

interface RoleRow {
  id: string;
  code: string;
  name: string;
  role_code: string;
}

interface SupplierRow {
  id: string;
  code: string;
  name: string;
  active: boolean;
}

interface CreatedAccount {
  user_id: string;
  username: string;
  role_code: string;
  one_time_credential: string;
}

interface Credential {
  username: string;
  credential: string;
  action: "建号" | "重新激活";
}

type RowAction = { userId: string; action: "deactivate" | "role" } | null;

export function AdminUsers() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const { hasAbility } = useAuth();
  const users = useApiQuery<UserRow[]>("/admin/users", undefined, {
    enabled: authenticated,
  });
  const roles = useApiQuery<RoleRow[]>("/admin/users/roles", undefined, {
    enabled: authenticated,
  });
  const suppliers = useApiQuery<SupplierRow[]>("/admin/users/suppliers", undefined, {
    enabled: authenticated,
  });
  const brands = useApiQuery<{ id: string; code: string; name: string }[]>(
    "/admin/master-data/brands",
    undefined,
    { enabled: authenticated },
  );
  const regions = useApiQuery<{ id: string; code: string; name: string }[]>(
    "/admin/master-data/regions",
    undefined,
    { enabled: authenticated },
  );
  const canManageRegion = hasAbility("region.manage");

  const [credential, setCredential] = useState<Credential | null>(null);
  const [rowAction, setRowAction] = useState<RowAction>(null);
  const [deactivateReason, setDeactivateReason] = useState("");
  const [newRoleId, setNewRoleId] = useState("");
  const [reactivateTarget, setReactivateTarget] = useState<UserRow | null>(null);

  const deactivateMut = useApiMutation((p: { id: string; reason: string }) =>
    api.post<void>(`/admin/users/${p.id}/deactivate`, { reason: p.reason }),
  );
  const reactivateMut = useApiMutation((id: string) =>
    api.post<CreatedAccount>(`/admin/users/${id}/reactivate`),
  );
  const changeRoleMut = useApiMutation((p: { id: string; roleId: string }) =>
    api.post<void>(`/admin/users/${p.id}/change-role`, { role_id: p.roleId }),
  );

  function roleNameOf(roleCode: string): string {
    const hit = roles.data?.find((r) => r.role_code === roleCode);
    return hit ? hit.name : label(roleCode);
  }

  function isSelf(row: UserRow): boolean {
    return Boolean(user && row.id === user.id);
  }

  const columns: DataTableColumn<UserRow>[] = [
    {
      key: "username",
      header: "账号",
      render: (r) => (
        <div>
          <span className="font-mono text-sm">{r.username}</span>
          {isSelf(r) ? (
            <span className="ml-2 text-xs text-slate-400">（我）</span>
          ) : null}
        </div>
      ),
    },
    { key: "display_name", header: "姓名", render: (r) => r.display_name },
    { key: "role_code", header: "角色", render: (r) => roleNameOf(r.role_code) },
    {
      key: "region_codes",
      header: "片区",
      render: (r) =>
        r.region_codes.length > 0 ? (
          <span className="text-xs">{r.region_codes.join(" / ")}</span>
        ) : (
          <span className="text-xs text-slate-400">—</span>
        ),
    },
    {
      key: "brand_codes",
      header: "品牌",
      render: (r) =>
        r.brand_codes.length > 0 ? (
          <span className="text-xs">{r.brand_codes.join(" / ")}</span>
        ) : (
          <span className="text-xs text-slate-400">—</span>
        ),
    },
    {
      key: "supplier_ids",
      header: "供应商",
      render: (r) =>
        r.supplier_ids.length > 0 ? (
          <span className="text-xs font-mono">{r.supplier_ids.join(" / ")}</span>
        ) : (
          <span className="text-xs text-slate-400">—</span>
        ),
    },
    {
      key: "status",
      header: "状态",
      render: (r) => (
        <div className="flex flex-wrap items-center gap-1">
          <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />
          {r.is_locked && r.status !== "INACTIVE" ? (
            <StatusBadge tone="danger" label="已锁定" />
          ) : null}
        </div>
      ),
    },
    {
      key: "must_change_password",
      header: "首登改密",
      render: (r) =>
        r.must_change_password ? (
          <StatusBadge tone="warning" label="需改密" />
        ) : (
          <span className="text-xs text-slate-400">—</span>
        ),
    },
    {
      key: "actions",
      header: "操作",
      render: (r) => (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={isSelf(r)}
            title={isSelf(r) ? "管理员不能修改自己的角色（后端同样拒绝）" : undefined}
            onClick={() => {
              setRowAction({ userId: r.id, action: "role" });
              setNewRoleId(
                roles.data?.find((role) => role.role_code === r.role_code)?.id ?? "",
              );
              setDeactivateReason("");
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40"
          >
            变更角色
          </button>
          {r.status === "INACTIVE" ? (
            <button
              type="button"
              onClick={() => setReactivateTarget(r)}
              className="rounded-md border border-emerald-300 bg-white px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-50"
            >
              重新激活
            </button>
          ) : (
            <button
              type="button"
              disabled={isSelf(r)}
              title={isSelf(r) ? "管理员不能停用自己的账号（后端同样拒绝）" : undefined}
              onClick={() => {
                setRowAction({ userId: r.id, action: "deactivate" });
                setDeactivateReason("");
              }}
              className="rounded-md border border-rose-300 bg-white px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-40"
            >
              停用
            </button>
          )}
        </div>
      ),
    },
  ];

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="用户与账号管理" description="建号、停用、启用与角色变更" />
        <SessionNotice hint="账号管理需要真实登录后的系统管理员会话（后端按 user.admin 能力二次鉴权）。" />
      </div>
    );
  }

  const activeRow =
    rowAction !== null ? (users.data ?? []).find((u) => u.id === rowAction.userId) ?? null : null;

  return (
    <div className="space-y-4">
      <PageHeader
        title="用户与账号管理"
        description="无注册入口：账号由系统管理员创建并下发一次性激活凭据；支持厂商账号（供应商+品牌+片区）与内部账号（品牌+片区）"
      />

      {credential ? (
        <CredentialPanel
          credential={credential}
          onClose={() => setCredential(null)}
          onToast={push}
        />
      ) : null}

      <CreateUserForm
        roles={roles.data ?? []}
        suppliers={suppliers.data ?? []}
        brands={brands.data ?? []}
        regions={regions.data ?? []}
        canManageRegion={canManageRegion}
        onCreated={(created) => {
          setCredential({
            username: created.username,
            credential: created.one_time_credential,
            action: "建号",
          });
          push(`账号 ${created.username} 已创建，请立即下发一次性激活凭据`, "success");
          users.refetch();
        }}
        onToast={push}
      />

      {users.loading ? <LoadingBlock text="加载账号列表…" /> : null}
      {users.error ? (
        <ErrorState message={users.error} status={users.status} onRetry={users.refetch} />
      ) : null}
      {roles.error ? (
        <ErrorState message={roles.error} status={roles.status} onRetry={roles.refetch} />
      ) : null}

      {!users.loading && !users.error ? (
        <>
          <div className="text-xs text-slate-500">
            共 {(users.data ?? []).length} 个账号（含待激活 / 已停用）
          </div>
          <DataTable
            rows={users.data ?? []}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="暂无账号（用上方表单创建第一个账号）"
          />

          {rowAction && activeRow && rowAction.action === "deactivate" ? (
            <div className="space-y-2 rounded-md border border-rose-200 bg-rose-50 p-3">
              <div className="text-xs font-medium text-rose-800">
                停用账号 {activeRow.username}（{activeRow.display_name}）
              </div>
              <label className="flex flex-col gap-1 text-xs text-slate-600">
                <span>停用原因 *（写入审计事件）</span>
                <input
                  value={deactivateReason}
                  onChange={(e) => setDeactivateReason(e.target.value)}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </label>
              <p className="text-xs text-rose-700">
                停用会立即吊销该账号的全部 token；重新启用需重新签发一次性激活凭据。
              </p>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={deactivateMut.submitting || !deactivateReason.trim()}
                  onClick={() => {
                    deactivateMut
                      .run({ id: activeRow.id, reason: deactivateReason.trim() })
                      .then((res) => {
                        if (res !== null) {
                          push(`账号 ${activeRow.username} 已停用`, "success");
                          setRowAction(null);
                          setDeactivateReason("");
                          users.refetch();
                        } else {
                          push(deactivateMut.error || "停用失败", "danger");
                        }
                      });
                  }}
                  className="rounded-md bg-rose-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {deactivateMut.submitting ? "停用中…" : "确认停用"}
                </button>
                <button
                  type="button"
                  onClick={() => setRowAction(null)}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600"
                >
                  取消
                </button>
              </div>
              {deactivateMut.error ? (
                <div className="text-xs text-rose-700">{deactivateMut.error}</div>
              ) : null}
            </div>
          ) : null}

          {rowAction && activeRow && rowAction.action === "role" ? (
            <div className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3">
              <div className="text-xs font-medium text-slate-700">
                变更 {activeRow.username} 的角色（当前：{roleNameOf(activeRow.role_code)}）
              </div>
              <label className="flex flex-col gap-1 text-xs text-slate-600">
                <span>新角色 *</span>
                <select
                  value={newRoleId}
                  onChange={(e) => setNewRoleId(e.target.value)}
                  className="w-64 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                >
                  <option value="">请选择</option>
                  {(roles.data ?? []).map((role) => (
                    <option key={role.id} value={role.id}>
                      {role.name}（{label(role.role_code)}）
                    </option>
                  ))}
                </select>
              </label>
              <p className="text-xs text-slate-500">
                角色变更后该账号的旧 token 立即失效，需重新登录。
              </p>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={changeRoleMut.submitting || !newRoleId}
                  onClick={() => {
                    changeRoleMut
                      .run({ id: activeRow.id, roleId: newRoleId })
                      .then((res) => {
                        if (res !== null) {
                          push(`账号 ${activeRow.username} 的角色已更新`, "success");
                          setRowAction(null);
                          setNewRoleId("");
                          users.refetch();
                        } else {
                          push(changeRoleMut.error || "角色变更失败", "danger");
                        }
                      });
                  }}
                  className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {changeRoleMut.submitting ? "提交中…" : "确认变更"}
                </button>
                <button
                  type="button"
                  onClick={() => setRowAction(null)}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600"
                >
                  取消
                </button>
              </div>
              {changeRoleMut.error ? (
                <div className="text-xs text-rose-600">{changeRoleMut.error}</div>
              ) : null}
            </div>
          ) : null}
        </>
      ) : null}

      <ConfirmDialog
        open={reactivateTarget !== null}
        title="重新激活并签发一次性凭据？"
        description={
          reactivateTarget
            ? `账号 ${reactivateTarget.username} 将回到「待激活」并强制首次改密，旧 token 立即失效；系统会返回新的激活凭据，且只展示这一次。`
            : ""
        }
        confirmText="重新激活"
        onClose={() => setReactivateTarget(null)}
        onConfirm={() => {
          const target = reactivateTarget;
          setReactivateTarget(null);
          if (!target) return;
          reactivateMut.run(target.id).then((res) => {
            if (res) {
              setCredential({
                username: res.username,
                credential: res.one_time_credential,
                action: "重新激活",
              });
              push(`账号 ${res.username} 已重新激活，请立即下发一次性凭据`, "success");
              users.refetch();
            } else {
              push(reactivateMut.error || "重新激活失败", "danger");
            }
          });
        }}
      />

      <p className="text-xs text-slate-400">
        系统不保存也不展示任何"初始密码"：建号与重新激活返回的是一次性激活凭据，明文仅在响应中出现一次，
        后续任何接口都无法再次获取（PRD §2.1）；全部账号操作都会写入审计事件。
      </p>

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}

/** 一次性凭据面板：醒目提示只展示一次，关闭即丢弃。 */
function CredentialPanel({
  credential,
  onClose,
  onToast,
}: {
  credential: Credential;
  onClose: () => void;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  async function copy() {
    try {
      await navigator.clipboard.writeText(credential.credential);
      onToast("激活凭据已复制到剪贴板", "success");
    } catch {
      onToast("复制失败，请手动选中后复制", "warning");
    }
  }

  return (
    <section className="space-y-2 rounded-md border border-amber-300 bg-amber-50 p-3">
      <div className="text-sm font-semibold text-amber-900">
        {credential.action}成功 · 一次性激活凭据（仅展示一次）
      </div>
      <div className="text-xs text-amber-800">
        账号：<span className="font-mono">{credential.username}</span>
        ，请立即复制并安全下发给本人；关闭本提示后无法再次获取，只能重新激活换发。
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <code className="max-w-full flex-1 overflow-x-auto rounded border border-amber-300 bg-white px-2 py-1 font-mono text-xs break-all">
          {credential.credential}
        </code>
        <button
          type="button"
          onClick={copy}
          className="rounded-md bg-amber-600 px-3 py-1.5 text-sm font-medium text-white"
        >
          复制
        </button>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-amber-400 bg-white px-3 py-1.5 text-sm text-amber-800"
        >
          我已记录，关闭
        </button>
      </div>
    </section>
  );
}

// ============================== 建号（含厂商账号支持） ==============================

function CreateUserForm({
  roles,
  suppliers,
  brands,
  regions,
  canManageRegion,
  onCreated,
  onToast,
}: {
  roles: RoleRow[];
  suppliers: SupplierRow[];
  brands: { id: string; code: string; name: string }[];
  regions: { id: string; code: string; name: string }[];
  canManageRegion: boolean;
  onCreated: (created: CreatedAccount) => void;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [phone, setPhone] = useState("");
  const [roleId, setRoleId] = useState("");
  // 厂商专属字段
  const [supplierId, setSupplierId] = useState("");
  const [selectedBrandIds, setSelectedBrandIds] = useState<string[]>([]);
  const [selectedRegionIds, setSelectedRegionIds] = useState<string[]>([]);

  const createMut = useApiMutation((payload: Record<string, unknown>) =>
    api.post<CreatedAccount>("/admin/users", payload),
  );

  const selectedRole = roles.find((r) => r.id === roleId);
  const isSupplierRole = selectedRole?.role_code === "SUPPLIER";

  function toggleArray(arr: string[], val: string): string[] {
    return arr.includes(val) ? arr.filter((v) => v !== val) : [...arr, val];
  }

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
      >
        {open ? "收起新建" : "新建账号"}
      </button>

      {open ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const payload: Record<string, unknown> = {
              username: username.trim(),
              display_name: displayName.trim(),
              phone: phone.trim() || undefined,
              role_id: roleId,
            };
            // 厂商账号：必填供应商；内部账号：可选传品牌+片区
            if (isSupplierRole) {
              payload["supplier_id"] = supplierId || null;
              payload["brand_ids"] = selectedBrandIds;
              payload["region_ids"] = selectedRegionIds;
            } else if (selectedBrandIds.length > 0 || selectedRegionIds.length > 0) {
              payload["brand_ids"] = selectedBrandIds;
              payload["region_ids"] = selectedRegionIds;
            }
            createMut
              .run(payload)
              .then((res) => {
                if (res) {
                  onCreated(res);
                  setUsername("");
                  setDisplayName("");
                  setPhone("");
                  setRoleId("");
                  setSupplierId("");
                  setSelectedBrandIds([]);
                  setSelectedRegionIds([]);
                  setOpen(false);
                } else {
                  onToast(createMut.error || "建号失败", "danger");
                }
              });
          }}
          className="space-y-3"
        >
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
            <label className="flex flex-col gap-1 text-xs text-slate-600">
              <span>账号 *（≥3 字符，登录用）</span>
              <input
                required
                minLength={3}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-600">
              <span>姓名 *</span>
              <input
                required
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-600">
              <span>手机号（可选）</span>
              <input
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-600">
              <span>角色 *</span>
              <select
                required
                value={roleId}
                onChange={(e) => setRoleId(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
              >
                <option value="">请选择</option>
                {roles.map((role) => (
                  <option key={role.id} value={role.id}>
                    {role.name}（{label(role.role_code)}）
                  </option>
                ))}
              </select>
            </label>
          </div>

          {/* 授权设置：厂商账号需选供应商+品牌+片区；内部账号可选品牌+片区 */}
          <div className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3">
            <div className="text-xs font-medium text-slate-700">授权范围</div>

            {isSupplierRole ? (
              <label className="flex flex-col gap-1 text-xs text-slate-700">
                <span>供应商 *</span>
                <select
                  value={supplierId}
                  onChange={(e) => setSupplierId(e.target.value)}
                  className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                >
                  <option value="">请选择</option>
                  {suppliers.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.code} — {s.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}

            {/* 品牌多选 */}
            <label className="flex flex-col gap-1 text-xs text-slate-700">
              <span>
                授权品牌（多选{isSupplierRole ? "，必选" : "，空则全品牌可见"}）
                {isSupplierRole ? <span className="text-rose-600"> *</span> : null}
              </span>
              <div className="max-h-40 overflow-y-auto rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm">
                {brands.length === 0 ? (
                  <span className="text-slate-400">暂无品牌（请先导入主数据）</span>
                ) : (
                  brands.map((b) => (
                    <label key={b.id} className="flex items-center gap-2 py-0.5">
                      <input
                        type="checkbox"
                        checked={selectedBrandIds.includes(b.id)}
                        onChange={() => setSelectedBrandIds((prev) => toggleArray(prev, b.id))}
                      />
                      <span className="font-mono text-xs">{b.code}</span>
                      <span className="text-xs text-slate-500">{b.name}</span>
                    </label>
                  ))
                )}
              </div>
            </label>

            {/* 片区多选 */}
            <label className="flex flex-col gap-1 text-xs text-slate-700">
              <span>
                授权片区（多选{isSupplierRole ? "，必选" : "，空则全部"}）
                {isSupplierRole ? <span className="text-rose-600"> *</span> : null}
                {canManageRegion ? (
                  <span className="ml-1 text-slate-400">（新增片区请前往「片区管理」页）</span>
                ) : (
                  <span className="ml-1 text-orange-600">（需联系系统管理员或片区经理新增片区）</span>
                )}
              </span>
              <div className="max-h-40 overflow-y-auto rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm">
                {regions.length === 0 ? (
                  <span className="text-slate-400">暂无片区（SYSTEM_ADMIN 或片区经理可从主数据页面新增）</span>
                ) : (
                  regions.map((r) => (
                    <label key={r.id} className="flex items-center gap-2 py-0.5">
                      <input
                        type="checkbox"
                        checked={selectedRegionIds.includes(r.id)}
                        onChange={() => setSelectedRegionIds((prev) => toggleArray(prev, r.id))}
                      />
                      <span className="font-mono text-xs">{r.code}</span>
                      <span className="text-xs text-slate-500">{r.name}</span>
                    </label>
                  ))
                )}
              </div>
            </label>
          </div>

          <p className="text-xs text-slate-400">
            管理员不设置初始密码：建号后账号处于「待激活」，系统返回一次性激活凭据，首次登录强制改密。
          </p>
          {createMut.error ? <div className="text-xs text-rose-600">{createMut.error}</div> : null}

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={
                createMut.submitting ||
                !roleId ||
                (isSupplierRole &&
                  (!supplierId || selectedBrandIds.length === 0 || selectedRegionIds.length === 0))
              }
              className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {createMut.submitting ? "创建中…" : "创建账号"}
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600"
            >
              取消
            </button>
          </div>
        </form>
      ) : null}
    </section>
  );
}
