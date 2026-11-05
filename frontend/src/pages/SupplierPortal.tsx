/** 厂商门户（PRD §2.2/§2.3/§3.2 `/supplier`、§4.9、R18）。
 *
 * 范围（PRD §2.3「仅能/不能」清单）：
 * - 仅可见授权品牌×片区的采购单（服务端行级裁剪 R18，前端不做过滤、隐藏不作鉴权）；
 * - 按行确认/驳回供货数量与预计到货日（复用采购页的 SupplyTab，走同一受控端点）；
 * - 确认 ≠ 付款 ≠ 入库（Q100）；同账号同行重复确认幂等（Q101）。
 * 对账/结算属演进方向（「先只读供货确认」），本页不含。
 *
 * 可用前提：`SUPPLIER_PORTAL_ENABLED=true` + 已配置品牌×片区授权 + 账号已激活，
 * 否则登录即被拒（401 SUPPLIER_PORTAL_DISABLED）；未配置授权时本页明确提示而非静默空白。
 */
import { useAuth } from "../lib/auth";
import { useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { SessionNotice } from "../components/AsyncState";
import { PageHeader } from "../components/PageHeader";
import { ToastStack } from "../components/Toast";
import { SupplyTab } from "./Procurement";

interface SupplierOptionListOut {
  items: Array<{ id: string; code: string; name: string }>;
}

export function SupplierPortal() {
  const { authenticated } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  // 授权主体（厂商身份下服务端只返回自己授权的供应商；空 = 未配置授权）
  const suppliers = useApiQuery<SupplierOptionListOut>("/procurement/suppliers", undefined, {
    enabled: authenticated,
  });
  const names = (suppliers.data?.items ?? []).map((s) => s.name);

  return (
    <div className="space-y-4">
      <PageHeader
        title="厂商门户"
        description="查看授权品牌×片区的采购单，按行确认或驳回供货数量与预计到货日"
      />

      {!authenticated ? (
        <SessionNotice hint="请使用已激活的厂商账号登录；门户未开启或账号未激活时登录会被拒绝。" />
      ) : (
        <>
          <p className="rounded-md border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
            当前授权主体：
            {suppliers.loading
              ? "加载中…"
              : names.length > 0
                ? names.join("、")
                : "尚未配置品牌×片区授权，暂无可操作数据——请联系经销商管理员配置授权。"}
          </p>
          <SupplyTab isSupplier onToast={push} />
        </>
      )}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}
