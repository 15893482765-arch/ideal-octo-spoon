/** 采购与供货（T18 前端联调 / PRD §4.9 / §5.1 / R17 / R18）。
 *
 * 接入后端：
 * - `GET /api/procurement/suggestions?brand_id=&region_id=&supplier_id=`（采购建议，不落库）；
 * - `POST /api/procurement/orders`（建单，**请求体不含总额**：金额一律服务端按行重算）；
 * - `GET /api/procurement/orders`（采购单列表；厂商只返回其授权品牌×片区的单）；
 * - `POST /api/procurement/orders/{id}/receive`（内部收货闭环）；
 * - `POST /api/procurement/orders/{id}/lines/{line_id}/supply-confirm|supply-reject`（厂商按行确认/拒绝）；
 * - `GET /api/procurement/orders/{id}/supply-confirmations`（逐行确认轨迹，厂商只见自己的）；
 * - `GET /api/procurement/suppliers` / `sku-options`（下拉）。
 *
 * 红线：建议只读“已确认且未满足”的补货需求（R8/R17）；总额服务端重算，前端不传（Q096）；
 * 厂商≠付款≠入库（Q100）；厂商行级范围=授权品牌×片区（Q098），越权 403。页面只做展示与
 * 受控交互，前端隐藏不作为鉴权。
 */
import { useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { MODULE } from "../lib/permissions";
import { boxes, formatDate, label, money, statusTone } from "../lib/format";
import { useApiMutation, useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { FilterBar } from "../components/FilterBar";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

const PAGE_SIZE = 20;

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

interface SuggestionLine {
  sku_id: string;
  sku_code: string;
  brand_id: string;
  region_id: string;
  confirmed_unmet_boxes: string;
  open_purchase_boxes: string;
  suggested_purchase_boxes: string;
  inventory_available: boolean;
  inventory_status: string;
  /** T36：超期未收货的在途量（不参与扣减） */
  overdue_purchase_boxes?: string;
  /** 已被在途采购完全覆盖：仍在列表里解释原因，但不可勾选下单 */
  covered_by_open_purchase?: boolean;
  /** T32：系统现存箱数（入库 − 出库），负值提示账实待核对 */
  available_boxes?: string;
}

/** 采购建议口径诊断（空态解释：为什么这里没有建议）。 */
interface SuggestionDiagnostics {
  dataset_type: string | null;
  dataset_id: string | null;
  confirmed_sku_count: number;
  confirmed_unmet_boxes: string;
  open_purchase_boxes: string;
  overdue_purchase_boxes: string;
  suggested_purchase_boxes: string;
  available_inventory_boxes?: string;
  covered_sku_count: number;
  overdue_grace_days: number;
}

interface OrderLine {
  id: string;
  sku_id: string;
  /** 商品编码/名称随单返回（后端内联），不再依赖二次映射 */
  sku_code: string;
  sku_name: string;
  quantity_cases: string;
  tax_inclusive_unit_price_fen: number;
  line_amount_fen: number;
  source_confirmed_boxes: string;
}

interface SkuOption {
  id: string;
  code: string;
  name: string;
  brand_id: string;
  /** 参考进价（分/瓶） */
  purchase_price_fen?: number | null;
  /** 箱规（瓶/箱）：数量按箱、单价按瓶，换算靠它 */
  case_spec?: string | number | null;
}

/** 手工加的采购行（不属于采购建议，直接进建单草稿）。 */
interface ManualLine {
  sku_id: string;
  sku_code: string;
  sku_name: string;
  qty: string;
  priceYuan: string;
}

interface OrderView {
  id: string;
  code: string;
  supplier_id: string;
  brand_id: string;
  region_id: string;
  expected_date: string | null;
  total_amount_fen: number;
  status: string;
  confirmed_at: string | null;
  received_at: string | null;
  note: string | null;
  lines: OrderLine[];
}

interface SupplyConfirmation {
  id: string;
  order_id: string;
  order_line_id: string;
  supplier_id: string;
  supplier_user_id: string;
  status: string;
  confirmed_quantity_cases: string | null;
  expected_delivery_date: string | null;
  proof_file_id: string | null;
  reject_reason: string | null;
  note: string | null;
}

interface OptionList<T = OptionItem> {
  items: T[];
}

interface DraftLine {
  sku_id: string;
  sku_code: string;
  suggested: number;
  qty: string;
  priceYuan: string;
  checked: boolean;
}

function yuanToFen(input: string): number {
  const n = Number(input);
  if (Number.isNaN(n) || n < 0) return -1;
  return Math.round(n * 100);
}

export function Procurement() {
  const { user, hasAbility } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const role = user?.role;
  /** 厂商账号（Q098）：只能确认/拒绝供货，不能建单、不能收货。 */
  const isSupplier = role === "SUPPLIER";
  /** SETTINGS 角色可见「供应商管理」tab（创建/启停业务档案）。 */
  const canManageSuppliers = hasAbility(MODULE.SETTINGS);

  const [tab, setTab] = useState<"suggestions" | "orders" | "supply" | "inventory" | "suppliers">(
    isSupplier ? "supply" : "suggestions",
  );

  useEffect(() => {
    setTab(isSupplier ? "supply" : "suggestions");
  }, [isSupplier]);

  const tabs = isSupplier
    ? ([["supply", "供货确认"]] as const)
    : ([
        ["suggestions", "采购建议"],
        ["orders", "采购订单"],
        ["supply", "供货确认（只读）"],
        ["inventory", "库存汇总"],
        ...(canManageSuppliers ? [["suppliers", "供应商"]] as const : []),
      ] as const);

  return (
    <div className="space-y-4">
      <PageHeader
        title="采购与供货"
        description="已确认补货需求 → 采购建议 → 内部建单（金额服务端重算）→ 厂商按行确认/拒绝 → 内部收货"
      />

      {!user ? (
        <SessionNotice hint="当前是开发注入账号，请从登录页用真实账号登录后再查看业务数据。" />
      ) : (
        <>
          <div className="flex gap-1 rounded-md border border-slate-200 bg-white p-1">
            {tabs.map(([key, text]) => (
              <button
                key={key}
                type="button"
                onClick={() => setTab(key)}
                className={`rounded px-3 py-1.5 text-sm ${
                  tab === key ? "bg-slate-100 font-medium text-slate-800" : "text-slate-600"
                }`}
              >
                {text}
              </button>
            ))}
          </div>

          {tab === "suggestions" ? <SuggestionsTab onToast={push} /> : null}
          {tab === "orders" ? <OrdersTab onToast={push} /> : null}
          {tab === "supply" ? (
            <SupplyTab isSupplier={isSupplier} onToast={push} />
          ) : null}
          {tab === "inventory" ? <InventoryTab /> : null}
          {tab === "suppliers" ? <SupplierManagementTab onToast={push} /> : null}
        </>
      )}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}

function Field({ label: text, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-slate-600">
      <span>{text}</span>
      {children}
    </label>
  );
}

function Info({ label: text, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{text}</div>
      <div className="mt-0.5 text-slate-800">{value}</div>
    </div>
  );
}

function useCatalog() {
  const brands = useApiQuery<OptionList>("/master-data/brands");
  const regions = useApiQuery<OptionList>("/master-data/regions");
  return {
    brands: useMemo(() => brands.data?.items ?? [], [brands.data]),
    regions: useMemo(() => regions.data?.items ?? [], [regions.data]),
    loading: brands.loading || regions.loading,
  };
}

/** 在途超期宽限天数（与后端 build_suggestions 默认值一致）。 */
const DEFAULT_OVERDUE_GRACE_DAYS = 3;

/** 空态标题：把"看不到建议"拆成三种可执行原因。 */
function emptyTitle(diag: SuggestionDiagnostics | undefined | null): string {
  if (!diag) return "暂无采购建议";
  if (diag.confirmed_sku_count === 0) return "本品牌 × 片区没有已确认的补货需求";
  if (Number(diag.suggested_purchase_boxes) <= 0) return "已确认需求已被在途采购或系统库存覆盖";
  return "暂无采购建议";
}

/** 空态说明：优先解释"为什么空"，再给下一步动作。 */
function emptyDescription(
  diag: SuggestionDiagnostics | undefined | null,
  onlyActionable: boolean,
): string {
  const manual =
    "也可以用下方「手工加行」直接采购任意商品（如沁爽矿泉水这类没有补货需求的商品）。";
  if (!diag) return `当片区存在「已确认且未满足」的补货需求时，这里会给出建议采购量；${manual}`;
  if (diag.confirmed_sku_count === 0) {
    return (
      `当前数据域（${diag.dataset_type ?? "—"}）内没有「已确认且未满足」的补货需求：` +
      `巡访提报的补货单需经理在「补货中心」确认为 CONFIRMED 后才会进入建议。${manual}`
    );
  }
  if (onlyActionable && diag.covered_sku_count > 0) {
    return `全部 ${diag.covered_sku_count} 个 SKU 的需求都已被在途采购覆盖；取消勾选「仅看仍需采购」即可查看这些行与被覆盖的箱数。`;
  }
  return (
    `已确认未满足 ${diag.confirmed_unmet_boxes} 箱、有效在途 ${diag.open_purchase_boxes} 箱、` +
    `系统库存 ${diag.available_inventory_boxes ?? 0} 箱，相抵后无可采购量。` +
    `请跟进在途采购单收货/关闭，或核对库存台账后再查看。`
  );
}

/** 单价：留空传 null，由服务端按 SKU 参考进价自动带出（T36）。 */
function priceOrNull(raw: string): number | null {
  const text = raw.trim();
  if (text === "") return null;
  const fen = yuanToFen(text);
  return fen < 0 ? null : fen;
}

/** 商品展示文案：优先用接口随单返回的「编码 · 名称」。 */
function skuLabel(code?: string | null, name?: string | null): string {
  const c = (code ?? "").trim();
  const n = (name ?? "").trim();
  if (c && n) return `${c} · ${n}`;
  if (n) return n;
  if (c) return c;
  return "未知商品";
}

/** SKU id → 名称映射（仅采购建议行需要：它只返回 sku_code，没有名称）。 */
function useSkuNames(brandId: string) {
  const skus = useApiQuery<OptionList<{ id: string; code: string; name: string; brand_id: string }>>(
    "/procurement/sku-options",
    { brand_id: brandId },
    { enabled: Boolean(brandId) },
  );
  return useMemo(() => {
    const map = new Map((skus.data?.items ?? []).map((s) => [s.id, s.name]));
    return (id: string) => map.get(id) || "未知商品";
  }, [skus.data]);
}

// ============================ 采购建议 ============================

function SuggestionsTab({
  onToast,
}: {
  onToast: (message: string, tone?: "info" | "success" | "warning" | "danger") => void;
}) {
  const { brands, regions } = useCatalog();
  const [brandId, setBrandId] = useState("");
  const [regionId, setRegionId] = useState("");
  const [supplierId, setSupplierId] = useState("");
  const [expectedDate, setExpectedDate] = useState("");
  const [note, setNote] = useState("");
  /** 建单草稿：按 SKU 记录勾选/箱数/含税单价（DataTable 的 render 只给行，不给下标）。 */
  const [drafts, setDrafts] = useState<Record<string, Omit<DraftLine, "sku_id" | "sku_code" | "suggested">>>(
    {},
  );

  useEffect(() => {
    if (!brandId && brands.length > 0) setBrandId(brands[0].id);
  }, [brands, brandId]);
  useEffect(() => {
    if (!regionId && regions.length > 0) setRegionId(regions[0].id);
  }, [regions, regionId]);

  const suppliers = useApiQuery<OptionList>(
    "/procurement/suppliers",
    { brand_id: brandId, region_id: regionId },
    { enabled: Boolean(brandId && regionId) },
  );
  const skuName = useSkuNames(brandId);

  /** 默认展示全部（含已被在途覆盖的行）：既能解释"为什么没有建议"，也不会重复下单。 */
  const [onlyActionable, setOnlyActionable] = useState(false);

  const suggestions = useApiQuery<SuggestionLine[]>(
    "/procurement/suggestions",
    {
      brand_id: brandId,
      region_id: regionId,
      supplier_id: supplierId,
      include_covered: !onlyActionable,
    },
    { enabled: Boolean(brandId && regionId) },
  );

  const diagnostics = useApiQuery<SuggestionDiagnostics>(
    "/procurement/suggestions/diagnostics",
    { brand_id: brandId, region_id: regionId, supplier_id: supplierId },
    { enabled: Boolean(brandId && regionId) },
  );

  // 手工加行与建议行共用同一份 SKU 目录（含参考进价，用于自动带出含税单价）
  const skuCatalog = useApiQuery<OptionList<SkuOption>>(
    "/procurement/sku-options",
    { brand_id: brandId },
    { enabled: Boolean(brandId) },
  );
  const skuOptions = useMemo(
    () => skuCatalog.data?.items ?? [],
    [skuCatalog.data],
  );
  /** SKU → 参考进价（元文本）；缺进价时返回空串，仍需人工填写。 */
  const refPriceYuan = useMemo(() => {
    const map = new Map<string, string>();
    for (const s of skuOptions) {
      if (s.purchase_price_fen != null) {
        map.set(s.id, (s.purchase_price_fen / 100).toFixed(2));
      }
    }
    return map;
  }, [skuOptions]);

  /** SKU → 箱规（瓶/箱）；未维护按 1 计，与服务端 bottles_per_case 同口径。 */
  const caseSpecMap = useMemo(() => {
    const map = new Map<string, number>();
    for (const s of skuOptions) {
      const spec = Number(s.case_spec ?? 0);
      map.set(s.id, spec > 0 ? spec : 1);
    }
    return map;
  }, [skuOptions]);
  /** 箱规文本，用于“≈ 元/箱”换算提示。 */
  function caseSpecOf(skuId: string): number {
    return caseSpecMap.get(skuId) ?? 1;
  }

  useEffect(() => {
    setDrafts((prev) => {
      const next: typeof prev = {};
      for (const s of suggestions.data ?? []) {
        const actionable =
          Number(s.suggested_purchase_boxes) > 0 && !s.covered_by_open_purchase;
        next[s.sku_id] = prev[s.sku_id] ?? {
          qty: s.suggested_purchase_boxes,
          // T36：含税单价默认按主数据参考进价自动生成，不再要求人工算价
          priceYuan: refPriceYuan.get(s.sku_id) ?? "",
          checked: actionable,
        };
      }
      return next;
    });
  }, [suggestions.data, refPriceYuan]);

  const createMut = useApiMutation(
    (p: {
      supplier_id: string;
      brand_id: string;
      region_id: string;
      expected_date: string | null;
      note: string | null;
      lines: {
        sku_id: string;
        quantity_cases: number;
        tax_inclusive_unit_price_fen: number | null;
      }[];
    }) => api.post<OrderView>("/procurement/orders", p),
  );

  // 手工加行：采购可选项不再只受「已确认补货需求」限制（矿泉水等无需求商品也能直接买）
  const [manualSkuId, setManualSkuId] = useState("");
  const [manualQty, setManualQty] = useState("1");
  const [manualPrice, setManualPrice] = useState("");
  const [manualLines, setManualLines] = useState<ManualLine[]>([]);

  const suggestionRows = suggestions.data ?? [];
  const picked = suggestionRows.filter((s) => {
    const d = drafts[s.sku_id];
    // 已被在途采购覆盖的行只做解释，不能进采购单（避免重复下单）
    return !s.covered_by_open_purchase && d?.checked && Number(d.qty) > 0;
  });
  /** 预估单价：留空时按参考进价估算，避免预览总额显示 0。 */
  function effectivePriceFen(skuId: string, raw: string): number {
    if (raw.trim() === "") {
      const ref = refPriceYuan.get(skuId);
      return ref ? Math.max(yuanToFen(ref), 0) : 0;
    }
    return Math.max(yuanToFen(raw), 0);
  }

  /** 行金额（分）：箱数 × 箱规 × 瓶单价，与服务端重算口径一致。 */
  function lineAmountFen(skuId: string, qty: string, priceYuan: string): number {
    const unit = effectivePriceFen(skuId, priceYuan);
    return Math.round(Number(qty) * caseSpecOf(skuId) * unit);
  }

  const pickedFen = picked.reduce((sum, s) => {
    const d = drafts[s.sku_id];
    return sum + lineAmountFen(s.sku_id, d.qty, d.priceYuan);
  }, 0);
  const manualFen = manualLines.reduce(
    (sum, l) => sum + lineAmountFen(l.sku_id, l.qty, l.priceYuan),
    0,
  );
  const totalFen = pickedFen + manualFen;
  /** T36：单价可留空——留空由服务端按 SKU 参考进价自动带出；只拦非法负数。 */
  const priceInvalid =
    picked.some((s) => {
      const raw = drafts[s.sku_id].priceYuan.trim();
      return raw !== "" && yuanToFen(raw) < 0;
    }) || manualLines.some((l) => l.priceYuan.trim() !== "" && yuanToFen(l.priceYuan) < 0);
  const priceMissingCount =
    picked.filter((s) => drafts[s.sku_id].priceYuan.trim() === "").length +
    manualLines.filter((l) => l.priceYuan.trim() === "").length;

  function addManualLine() {
    const hit = skuOptions.find((s) => s.id === manualSkuId);
    if (!hit) {
      onToast("请选择要采购的商品", "danger");
      return;
    }
    if (Number(manualQty) <= 0) {
      onToast("采购箱数必须大于 0", "danger");
      return;
    }
    if (manualPrice.trim() !== "" && yuanToFen(manualPrice) < 0) {
      onToast("含税单价不能为负数（单位：元）；留空按参考进价自动生成", "danger");
      return;
    }
    if (
      manualLines.some((l) => l.sku_id === hit.id) ||
      picked.some((s) => s.sku_id === hit.id)
    ) {
      onToast(`${hit.code} 已在本次采购单中，不能重复添加`, "warning");
      return;
    }
    setManualLines((prev) => [
      ...prev,
      {
        sku_id: hit.id,
        sku_code: hit.code,
        sku_name: hit.name,
        qty: manualQty,
        priceYuan: manualPrice,
      },
    ]);
    setManualSkuId("");
    setManualQty("1");
    setManualPrice("");
  }

  async function submit() {
    if (!supplierId) {
      onToast("请选择供应商", "danger");
      return;
    }
    if (picked.length === 0 && manualLines.length === 0) {
      onToast("请至少勾选一条建议行，或用「手工加行」添加商品", "danger");
      return;
    }
    if (priceInvalid) {
      onToast("含税单价不能为负数（单位：元）", "danger");
      return;
    }
    const res = await createMut.run({
      supplier_id: supplierId,
      brand_id: brandId,
      region_id: regionId,
      expected_date: expectedDate || null,
      note: note.trim() || null,
      lines: [
        ...picked.map((s) => ({
          sku_id: s.sku_id,
          quantity_cases: Number(drafts[s.sku_id].qty),
          tax_inclusive_unit_price_fen: priceOrNull(drafts[s.sku_id].priceYuan),
        })),
        ...manualLines.map((l) => ({
          sku_id: l.sku_id,
          quantity_cases: Number(l.qty),
          tax_inclusive_unit_price_fen: priceOrNull(l.priceYuan),
        })),
      ],
    });
    if (res) {
      onToast(`采购单 ${res.code} 已创建并确认（总额 ${money(res.total_amount_fen)} 由服务端重算）`, "success");
      setDrafts({});
      setManualLines([]);
      suggestions.refetch();
    } else {
      onToast(
        createMut.error ||
          "建单失败：演示（DEMO）上游事实不得进入正式采购单，请确认账号与供应商的数据范围",
        "danger",
      );
    }
  }

  /** 一键全选/清空建议行：勾选即视为确认本次要采购。 */
  function toggleAll(next: boolean) {
    setDrafts((prev) => {
      const out: typeof prev = { ...prev };
      for (const s of suggestionRows) {
        out[s.sku_id] = {
          ...(out[s.sku_id] ?? {
            qty: s.suggested_purchase_boxes,
            priceYuan: refPriceYuan.get(s.sku_id) ?? "",
            checked: true,
          }),
          checked: next,
        };
      }
      return out;
    });
  }

  /** 按主数据参考进价填充所有空单价，免人工算价。 */
  function fillRefPrices() {
    setDrafts((prev) => {
      const out: typeof prev = { ...prev };
      for (const s of suggestionRows) {
        const current = out[s.sku_id];
        if (!current || current.priceYuan.trim() !== "") continue;
        out[s.sku_id] = {
          ...current,
          priceYuan: refPriceYuan.get(s.sku_id) ?? "",
        };
      }
      return out;
    });
    setManualLines((prev) =>
      prev.map((l) =>
        l.priceYuan.trim() === ""
          ? { ...l, priceYuan: refPriceYuan.get(l.sku_id) ?? "" }
          : l,
      ),
    );
  }

  return (
    <div className="space-y-4">
      <FilterBar
        onApply={() => undefined}
        onReset={() => {
          setDrafts({});
        }}
      >
        <Field label="品牌 *">
          <select
            value={brandId}
            onChange={(e) => {
              setBrandId(e.target.value);
              setSupplierId("");
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            {brands.map((b) => (
              <option key={b.id} value={b.id}>
                {b.code} · {b.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="片区 *">
          <select
            value={regionId}
            onChange={(e) => {
              setRegionId(e.target.value);
              setSupplierId("");
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            {regions.map((r) => (
              <option key={r.id} value={r.id}>
                {r.code} · {r.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="在途口径限定供应商">
          <select
            value={supplierId}
            onChange={(e) => setSupplierId(e.target.value)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">不限（汇总该品牌×片区全部在途）</option>
            {(suppliers.data?.items ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.code} · {s.name}
              </option>
            ))}
          </select>
        </Field>
      </FilterBar>

      <p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
        建议口径：建议采购 = 已确认未满足需求 − 有效在途 − 系统库存（采购收货入库、补货送达出库的
        台账余额，R8/R17）；已被在途覆盖的 SKU 会保留并使用「已在途」标记、不可勾选下单；超过{" "}
        {DEFAULT_OVERDUE_GRACE_DAYS} 天预计到货日仍未收货的在途单不再压制建议。含税单价留空即按主数据
        参考进价（元/瓶）自动生成。
      </p>

      {diagnostics.data ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
          <span className="font-medium text-slate-700">口径诊断</span>
          <span>已确认未满足 {boxes(diagnostics.data.confirmed_unmet_boxes)} 箱</span>
          <span>有效在途 {boxes(diagnostics.data.open_purchase_boxes)} 箱</span>
          <span>
            系统库存 {boxes(diagnostics.data.available_inventory_boxes ?? 0)} 箱
          </span>
          {Number(diagnostics.data.overdue_purchase_boxes) > 0 ? (
            <span className="text-amber-700">
              超期在途 {boxes(diagnostics.data.overdue_purchase_boxes)} 箱（建议跟进厂商或关闭采购单）
            </span>
          ) : null}
          <span>建议采购 {boxes(diagnostics.data.suggested_purchase_boxes)} 箱</span>
          {diagnostics.data.covered_sku_count > 0 ? (
            <span>
              {diagnostics.data.covered_sku_count} 个 SKU 已被在途覆盖
              <span className="text-slate-400">
                （先到「采购订单」收货或关闭对应单据，才会重新进入建议）
              </span>
            </span>
          ) : null}
          <span className="text-slate-400">数据域 {diagnostics.data.dataset_type ?? "—"}</span>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => toggleAll(true)}
          disabled={suggestionRows.length === 0}
          className="rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          全选建议行
        </button>
        <button
          type="button"
          onClick={() => toggleAll(false)}
          disabled={suggestionRows.length === 0}
          className="rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          清空勾选
        </button>
        <button
          type="button"
          onClick={fillRefPrices}
          className="rounded-md border border-[var(--color-brand)] bg-white px-2.5 py-1 text-xs text-[var(--color-brand)] hover:bg-teal-50"
        >
          按参考进价填充单价
        </button>
        <label className="flex items-center gap-1 text-xs text-slate-600">
          <input
            type="checkbox"
            checked={onlyActionable}
            onChange={(e) => setOnlyActionable(e.target.checked)}
          />
          仅看仍需采购（隐藏已在途）
        </label>
        <span className="text-xs text-slate-500">
          已确认 {picked.length} 行
          {priceMissingCount > 0
            ? ` · 其中 ${priceMissingCount} 行单价留空（提交时按参考进价生成）`
            : " · 单价已按参考进价带出"}
        </span>
      </div>

      {suggestions.loading ? <LoadingBlock text="计算采购建议…" /> : null}
      {suggestions.error ? (
        <ErrorState
          message={suggestions.error}
          status={suggestions.status}
          onRetry={suggestions.refetch}
        />
      ) : null}

      {!suggestions.loading && !suggestions.error ? (
        <>
          {(suggestions.data ?? []).length === 0 ? (
            <EmptyState
              title={emptyTitle(diagnostics.data)}
              description={emptyDescription(diagnostics.data, onlyActionable)}
            />
          ) : (
            <DataTable
              rows={suggestionRows}
              columns={[
                {
                  key: "pick",
                  header: "",
                  render: (s) => (
                    <input
                      type="checkbox"
                      disabled={Boolean(s.covered_by_open_purchase)}
                      title={
                        s.covered_by_open_purchase
                          ? "该 SKU 的已确认需求已被在途采购覆盖，无需重复下单"
                          : undefined
                      }
                      checked={
                        !s.covered_by_open_purchase &&
                        Boolean(drafts[s.sku_id]?.checked)
                      }
                      onChange={(e) =>
                        setDrafts((prev) => ({
                          ...prev,
                          [s.sku_id]: {
                            ...(prev[s.sku_id] ?? { qty: "0", priceYuan: "", checked: false }),
                            checked: e.target.checked,
                          },
                        }))
                      }
                    />
                  ),
                },
                {
                  key: "sku",
                  header: "SKU",
                  render: (s) => `${s.sku_code} · ${skuName(s.sku_id)}`,
                },
                {
                  key: "unmet",
                  header: "已确认未满足",
                  align: "right",
                  render: (s) => (
                    <span className="font-mono">{boxes(s.confirmed_unmet_boxes)}</span>
                  ),
                },
                {
                  key: "on_the_way",
                  header: "在途采购",
                  align: "right",
                  render: (s) => (
                    <span className="font-mono">
                      {boxes(s.open_purchase_boxes)}
                      {Number(s.overdue_purchase_boxes ?? 0) > 0 ? (
                        <span
                          className="ml-1 text-[11px] text-amber-700"
                          title="超过预计到货日仍未收货，已不再压制建议，请跟进厂商或关闭该采购单"
                        >
                          （超期 {boxes(s.overdue_purchase_boxes ?? "0")}）
                        </span>
                      ) : null}
                    </span>
                  ),
                },
                {
                  key: "suggested",
                  header: "建议采购",
                  align: "right",
                  render: (s) =>
                    s.covered_by_open_purchase ? (
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">
                        已在途
                      </span>
                    ) : (
                      <span className="font-mono">{boxes(s.suggested_purchase_boxes)}</span>
                    ),
                },
                {
                  key: "qty",
                  header: "本次采购箱数",
                  align: "right",
                  render: (s) => (
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      disabled={Boolean(s.covered_by_open_purchase)}
                      value={drafts[s.sku_id]?.qty ?? ""}
                      onChange={(e) =>
                        setDrafts((prev) => ({
                          ...prev,
                          [s.sku_id]: {
                            ...(prev[s.sku_id] ?? { qty: "", priceYuan: "", checked: true }),
                            qty: e.target.value,
                          },
                        }))
                      }
                      className="w-24 rounded-md border border-slate-300 px-2 py-1 text-right text-sm"
                    />
                  ),
                },
                {
                  key: "price",
                  header: "含税单价（元/瓶，留空=参考进价）",
                  align: "right",
                  render: (s) => {
                    const unitFen = effectivePriceFen(
                      s.sku_id,
                      drafts[s.sku_id]?.priceYuan ?? "",
                    );
                    return (
                      <div className="flex flex-col items-end gap-0.5">
                        <input
                          type="number"
                          min="0"
                          step="0.01"
                          disabled={Boolean(s.covered_by_open_purchase)}
                          placeholder={refPriceYuan.get(s.sku_id) ?? "需手填"}
                          value={drafts[s.sku_id]?.priceYuan ?? ""}
                          onChange={(e) =>
                            setDrafts((prev) => ({
                              ...prev,
                              [s.sku_id]: {
                                ...(prev[s.sku_id] ?? { qty: "", priceYuan: "", checked: true }),
                                priceYuan: e.target.value,
                              },
                            }))
                          }
                          className="w-28 rounded-md border border-slate-300 px-2 py-1 text-right text-sm"
                        />
                        <span className="text-[11px] text-slate-400">
                          ≈ {money(Math.round(unitFen * caseSpecOf(s.sku_id)))}/箱
                        </span>
                      </div>
                    );
                  },
                },
                {
                  key: "inventory",
                  header: "系统库存",
                  align: "right",
                  render: (s) => {
                    if (!s.inventory_available) {
                      return (
                        <span className="text-xs text-amber-700">
                          {s.inventory_status === "UNAVAILABLE" ? "台账不可读" : s.inventory_status}
                        </span>
                      );
                    }
                    const balance = Number(s.available_boxes ?? 0);
                    if (balance > 0) {
                      return (
                        <span className="font-mono text-xs text-emerald-700">
                          {boxes(balance)} 箱
                        </span>
                      );
                    }
                    if (balance < 0) {
                      return (
                        <span
                          className="font-mono text-xs text-rose-600"
                          title="出库多于入库，台账缺历史入库数据，需账实核对"
                        >
                          {boxes(balance)} 箱
                        </span>
                      );
                    }
                    return <span className="font-mono text-xs text-slate-400">0 箱</span>;
                  },
                },
              ]}
              rowKey={(s) => s.sku_id}
              emptyText="暂无建议行"
            />
          )}

          <div className="rounded-md border border-slate-200 bg-white p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-xs font-medium text-slate-600">
                手工加行（不依赖补货需求，可直接采购任意商品）
              </div>
              <span className="text-[11px] text-slate-400">
                建议行 {picked.length} 条 · 手工行 {manualLines.length} 条
              </span>
            </div>
            <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-4">
              <Field label="商品">
                <select
                  value={manualSkuId}
                  onChange={(e) => {
                    setManualSkuId(e.target.value);
                    const hit = skuOptions.find((s) => s.id === e.target.value);
                    // 参考进价自动预填，避免手输错价（仍可修改）
                    setManualPrice(
                      hit?.purchase_price_fen != null
                        ? (hit.purchase_price_fen / 100).toFixed(2)
                        : "",
                    );
                  }}
                  className="w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                >
                  <option value="">请选择商品</option>
                  {skuOptions.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.code} · {s.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="箱数">
                <input
                  type="number"
                  min="0.01"
                  step="0.01"
                  value={manualQty}
                  onChange={(e) => setManualQty(e.target.value)}
                  className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </Field>
              <Field label="含税单价（元/瓶，留空=参考进价）">
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  placeholder="自动按参考进价"
                  value={manualPrice}
                  onChange={(e) => setManualPrice(e.target.value)}
                  className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </Field>
              <div className="flex items-end">
                <button
                  type="button"
                  onClick={addManualLine}
                  className="w-full rounded-md border border-[var(--color-brand)] px-3 py-1.5 text-sm text-[var(--color-brand)] hover:bg-teal-50"
                >
                  + 加入本次采购
                </button>
              </div>
            </div>

            {manualLines.length > 0 ? (
              <ul className="mt-2 divide-y divide-slate-100 rounded-md border border-slate-200">
                {manualLines.map((l) => (
                  <li
                    key={l.sku_id}
                    className="flex flex-wrap items-center justify-between gap-2 px-3 py-1.5 text-xs"
                  >
                    <span className="text-slate-700">
                      <span className="font-mono text-slate-500">{l.sku_code}</span> ·{" "}
                      {l.sku_name}
                    </span>
                    <span className="text-slate-500">
                      {boxes(l.qty)} 箱 × {boxes(caseSpecOf(l.sku_id))} 瓶/箱 ×{" "}
                      {money(effectivePriceFen(l.sku_id, l.priceYuan))} ={" "}
                      <span className="font-mono text-slate-700">
                        {money(lineAmountFen(l.sku_id, l.qty, l.priceYuan))}
                      </span>
                    </span>
                    <button
                      type="button"
                      onClick={() =>
                        setManualLines((prev) => prev.filter((x) => x.sku_id !== l.sku_id))
                      }
                      className="text-rose-600 hover:underline"
                    >
                      移除
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>

          <div className="grid grid-cols-1 gap-3 rounded-md border border-slate-200 bg-white p-3 md:grid-cols-4">
              <Field label="供应商 *">
                <select
                  value={supplierId}
                  onChange={(e) => setSupplierId(e.target.value)}
                  className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                >
                  <option value="">请选择</option>
                  {(suppliers.data?.items ?? []).map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.code} · {s.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="预计到货日">
                <input
                  type="date"
                  value={expectedDate}
                  onChange={(e) => setExpectedDate(e.target.value)}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </Field>
              <Field label="备注">
                <input
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </Field>
              <div className="flex flex-col justify-end gap-1">
                <div className="text-xs text-slate-500">
                  已选 {picked.length + manualLines.length} 行 · 预估总额 {money(totalFen)}（以服务端重算为准）
                  {priceMissingCount > 0
                    ? ` · ${priceMissingCount} 行单价将由参考进价自动生成`
                    : ""}
                </div>
                <button
                  type="button"
                  disabled={createMut.submitting}
                  onClick={submit}
                  className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {createMut.submitting ? "提交中…" : "确认并生成采购单"}
                </button>
              </div>
            </div>

          {(suppliers.data?.items ?? []).length === 0 ? (
            <p className="text-xs text-amber-700">
              暂无可选供应商。请先前往「供应商」tab 创建业务档案（仅 SETTINGS 角色可见），再重新选择品牌×片区。
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

// ============================ 采购订单 ============================

function useOrderCatalogs() {
  const { brands, regions } = useCatalog();
  const suppliers = useApiQuery<OptionList>("/procurement/suppliers");
  const maps = useMemo(() => {
    const supplierMap = new Map((suppliers.data?.items ?? []).map((s) => [s.id, s.name]));
    const brandMap = new Map(brands.map((b) => [b.id, b.name]));
    const regionMap = new Map(regions.map((r) => [r.id, r.name]));
    return { supplierMap, brandMap, regionMap };
  }, [suppliers.data, brands, regions]);
  return maps;
}

function OrdersTab({
  onToast,
}: {
  onToast: (message: string, tone?: "info" | "success" | "warning" | "danger") => void;
}) {
  const { brands, regions } = useCatalog();
  const { supplierMap, brandMap, regionMap } = useOrderCatalogs();
  const [brandId, setBrandId] = useState("");
  const [regionId, setRegionId] = useState("");
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const orders = useApiQuery<OrderView[]>(
    "/procurement/orders",
    { brand_id: brandId, region_id: regionId },
    { enabled: true },
  );

  const receiveMut = useApiMutation((id: string) =>
    api.post<OrderView>(`/procurement/orders/${id}/receive`),
  );

  const rows = orders.data ?? [];
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const pageRows = rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const columns: DataTableColumn<OrderView>[] = [
    {
      key: "code",
      header: "采购单号",
      render: (o) => (
        <button
          type="button"
          onClick={() => setSelectedId(o.id)}
          className="font-mono text-xs text-[var(--color-brand)] hover:underline"
        >
          {o.code}
        </button>
      ),
    },
    {
      key: "supplier",
      header: "供应商",
      render: (o) => supplierMap.get(o.supplier_id) ?? `${o.supplier_id.slice(0, 8)}…`,
    },
    { key: "brand", header: "品牌", render: (o) => brandMap.get(o.brand_id) ?? "—" },
    { key: "region", header: "片区", render: (o) => regionMap.get(o.region_id) ?? "—" },
    { key: "expected", header: "预计到货", render: (o) => formatDate(o.expected_date) },
    {
      key: "lines",
      header: "行数",
      align: "right",
      render: (o) => o.lines.length,
    },
    {
      key: "total",
      header: "总额（服务端重算）",
      align: "right",
      render: (o) => <span className="font-mono">{money(o.total_amount_fen)}</span>,
    },
    {
      key: "status",
      header: "状态",
      render: (o) => <StatusBadge tone={statusTone(o.status)} label={label(o.status)} />,
    },
    {
      key: "actions",
      header: "操作",
      render: (o) =>
        o.status === "SUPPLIER_CONFIRMED" ? (
          <button
            type="button"
            disabled={receiveMut.submitting}
            onClick={async () => {
              const res = await receiveMut.run(o.id);
              if (res) {
                onToast("已确认收货（仅状态闭环，不入库、不付款）", "success");
                orders.refetch();
              } else {
                onToast(receiveMut.error || "收货失败", "danger");
              }
            }}
            className="rounded-md bg-[var(--color-brand)] px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
          >
            确认收货
          </button>
        ) : (
          <span className="text-xs text-slate-400">—</span>
        ),
    },
  ];

  const selected = rows.find((o) => o.id === selectedId) ?? null;

  return (
    <div className="space-y-4">
      <FilterBar
        onApply={() => setPage(1)}
        onReset={() => {
          setBrandId("");
          setRegionId("");
          setPage(1);
        }}
      >
        <Field label="品牌">
          <select
            value={brandId}
            onChange={(e) => {
              setBrandId(e.target.value);
              setPage(1);
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {brands.map((b) => (
              <option key={b.id} value={b.id}>
                {b.code} · {b.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="片区">
          <select
            value={regionId}
            onChange={(e) => {
              setRegionId(e.target.value);
              setPage(1);
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {regions.map((r) => (
              <option key={r.id} value={r.id}>
                {r.code} · {r.name}
              </option>
            ))}
          </select>
        </Field>
        <span className="text-xs text-slate-500">
          品牌与片区需同时选择才会按组合过滤（避免半截条件被拒）
        </span>
      </FilterBar>

      {orders.loading ? <LoadingBlock text="加载采购单…" /> : null}
      {orders.error ? (
        <ErrorState message={orders.error} status={orders.status} onRetry={orders.refetch} />
      ) : null}

      {!orders.loading && !orders.error ? (
        <>
          <div className="text-xs text-slate-500">共 {rows.length} 张采购单（客户端分页，每页 {PAGE_SIZE} 张）</div>
          <DataTable
            rows={pageRows}
            columns={columns}
            rowKey={(o) => o.id}
            emptyText="暂无采购单（可从「采购建议」生成）"
          />
          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      ) : null}

      {selectedId ? (
        <OrderPanel
          mode="internal"
          order={selected}
          supplierName={selected ? (supplierMap.get(selected.supplier_id) ?? "—") : "—"}
          brandName={selected ? (brandMap.get(selected.brand_id) ?? "—") : "—"}
          regionName={selected ? (regionMap.get(selected.region_id) ?? "—") : "—"}
          onClose={() => setSelectedId(null)}
          onToast={onToast}
          onChanged={() => orders.refetch()}
        />
      ) : null}
    </div>
  );
}

// ============================ 库存汇总（T32 收发存台账） ============================

interface InventorySummaryItem {
  sku_id: string;
  sku_code: string;
  sku_name: string;
  received_cases: string;
  delivered_cases: string;
  balance_cases: string;
}

function InventoryTab() {
  const summary = useApiQuery<{ items: InventorySummaryItem[] }>(
    "/procurement/inventory/summary",
    undefined,
    { enabled: true },
  );
  const items = summary.data?.items ?? [];

  return (
    <div className="space-y-4">
      <p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
        收发存台账：采购单收货后按厂商确认供货量入库；补货「登记送达」后按实送箱数出库；
        现存 = 累计入库 − 累计出库。台账由收货/送达动作自动记账，重复操作幂等不重复计。
      </p>

      {summary.loading ? <LoadingBlock text="汇总库存台账…" /> : null}
      {summary.error ? (
        <ErrorState
          message={summary.error}
          status={summary.status}
          onRetry={summary.refetch}
        />
      ) : null}

      {!summary.loading && !summary.error ? (
        items.length === 0 ? (
          <EmptyState
            title="暂无库存流水"
            description="采购单「确认收货」后自动入库；补货单「登记送达」后自动出库。"
          />
        ) : (
          <DataTable
            rows={items}
            columns={[
              { key: "code", header: "SKU 编码", render: (i) => i.sku_code },
              { key: "name", header: "饮料名称", render: (i) => i.sku_name },
              {
                key: "received",
                header: "累计入库（箱）",
                render: (i) => boxes(i.received_cases),
              },
              {
                key: "delivered",
                header: "累计出库/送达（箱）",
                render: (i) => boxes(i.delivered_cases),
              },
              {
                key: "balance",
                header: "现存（箱）",
                render: (i) => (
                  <span className="font-semibold text-slate-800">
                    {boxes(i.balance_cases)}
                  </span>
                ),
              },
            ]}
            rowKey={(i) => i.sku_id}
            emptyText="暂无数据"
          />
        )
      ) : null}
    </div>
  );
}

// ============================ 供货确认 ============================

/** 供货确认工作台：采购页 tab 与厂商门户页（/supplier）共用。 */
export function SupplyTab({
  isSupplier,
  onToast,
}: {
  isSupplier: boolean;
  onToast: (message: string, tone?: "info" | "success" | "warning" | "danger") => void;
}) {
  const { supplierMap, brandMap, regionMap } = useOrderCatalogs();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [page, setPage] = useState(1);

  const orders = useApiQuery<OrderView[]>("/procurement/orders", undefined, { enabled: true });
  const rows = orders.data ?? [];
  const pending = rows.filter((o) => o.status === "CONFIRMED");
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const pageRows = rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const selected = rows.find((o) => o.id === selectedId) ?? null;

  return (
    <div className="space-y-4">
      <p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
        {isSupplier
          ? "按行确认可供货量与预计到货日（可拒绝并说明原因）。确认不等于付款、不等于入库（Q100）；同一行同一厂商账号重复确认按幂等处理（Q101）。"
          : "本页只读展示厂商确认轨迹：逐行确认/拒绝由厂商账号在门户执行，内部不做代填。"}
      </p>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="rounded-md border border-slate-200 bg-white p-3">
          <div className="text-xs text-slate-500">可见采购单</div>
          <div className="mt-1 text-lg font-semibold text-slate-800">{rows.length}</div>
        </div>
        <div className="rounded-md border border-slate-200 bg-white p-3">
          <div className="text-xs text-slate-500">待厂商确认（CONFIRMED）</div>
          <div className="mt-1 text-lg font-semibold text-slate-800">{pending.length}</div>
        </div>
        <div className="rounded-md border border-slate-200 bg-white p-3">
          <div className="text-xs text-slate-500">厂商已确认</div>
          <div className="mt-1 text-lg font-semibold text-slate-800">
            {rows.filter((o) => o.status === "SUPPLIER_CONFIRMED").length}
          </div>
        </div>
        <div className="rounded-md border border-slate-200 bg-white p-3">
          <div className="text-xs text-slate-500">已收货</div>
          <div className="mt-1 text-lg font-semibold text-slate-800">
            {rows.filter((o) => o.status === "RECEIVED").length}
          </div>
        </div>
      </div>

      {orders.loading ? <LoadingBlock text="加载采购单…" /> : null}
      {orders.error ? (
        <ErrorState message={orders.error} status={orders.status} onRetry={orders.refetch} />
      ) : null}

      {!orders.loading && !orders.error ? (
        rows.length === 0 ? (
          <EmptyState
            title="暂无可确认的采购单"
            description={
              isSupplier
                ? "仅显示与你授权品牌×片区匹配的采购单；内部建单后才会出现在这里。"
                : "暂无采购单。"
            }
          />
        ) : (
          <>
            <DataTable
              rows={pageRows}
              columns={[
                {
                  key: "code",
                  header: "采购单号",
                  render: (o) => (
                    <button
                      type="button"
                      onClick={() => setSelectedId(o.id)}
                      className="font-mono text-xs text-[var(--color-brand)] hover:underline"
                    >
                      {o.code}
                    </button>
                  ),
                },
                {
                  key: "supplier",
                  header: "供应商",
                  render: (o) => supplierMap.get(o.supplier_id) ?? `${o.supplier_id.slice(0, 8)}…`,
                },
                { key: "brand", header: "品牌", render: (o) => brandMap.get(o.brand_id) ?? "—" },
                { key: "region", header: "片区", render: (o) => regionMap.get(o.region_id) ?? "—" },
                { key: "expected", header: "预计到货", render: (o) => formatDate(o.expected_date) },
                {
                  key: "status",
                  header: "状态",
                  render: (o) => <StatusBadge tone={statusTone(o.status)} label={label(o.status)} />,
                },
              ]}
              rowKey={(o) => o.id}
              emptyText="暂无数据"
            />
            <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
          </>
        )
      ) : null}

      {selectedId ? (
        <OrderPanel
          mode={isSupplier ? "supplier" : "internal"}
          order={selected}
          supplierName={selected ? (supplierMap.get(selected.supplier_id) ?? "—") : "—"}
          brandName={selected ? (brandMap.get(selected.brand_id) ?? "—") : "—"}
          regionName={selected ? (regionMap.get(selected.region_id) ?? "—") : "—"}
          onClose={() => setSelectedId(null)}
          onToast={onToast}
          onChanged={() => orders.refetch()}
        />
      ) : null}
    </div>
  );
}

// ============================ 采购单明细（内部 / 厂商共用） ============================

function OrderPanel({
  mode,
  order,
  supplierName,
  brandName,
  regionName,
  onClose,
  onToast,
  onChanged,
}: {
  mode: "internal" | "supplier";
  order: OrderView | null;
  supplierName: string;
  brandName: string;
  regionName: string;
  onClose: () => void;
  onToast: (message: string, tone?: "info" | "success" | "warning" | "danger") => void;
  onChanged: () => void;
}) {
  const orderId = order?.id ?? "";

  const confirmations = useApiQuery<SupplyConfirmation[]>(
    `/procurement/orders/${orderId}/supply-confirmations`,
    undefined,
    { enabled: Boolean(orderId) },
  );

  const [drafts, setDrafts] = useState<
    Record<string, { qty: string; eta: string; note: string; reason: string; rejecting: boolean }>
  >({});

  const confirmMut = useApiMutation(
    (p: {
      lineId: string;
      qty: number;
      eta: string;
      note: string | null;
    }) =>
      api.post<SupplyConfirmation>(
        `/procurement/orders/${orderId}/lines/${p.lineId}/supply-confirm`,
        {
          confirmed_quantity_cases: p.qty,
          expected_delivery_date: p.eta,
          note: p.note,
        },
      ),
  );
  const rejectMut = useApiMutation((p: { lineId: string; reason: string }) =>
    api.post<SupplyConfirmation>(
      `/procurement/orders/${orderId}/lines/${p.lineId}/supply-reject`,
      { reason: p.reason },
    ),
  );

  const byLine = useMemo(() => {
    const map = new Map<string, SupplyConfirmation>();
    for (const c of confirmations.data ?? []) map.set(c.order_line_id, c);
    return map;
  }, [confirmations.data]);

  // 可确认供货的单状态（与后端 confirm_supply 允许集合一致；SUPPLIER_CONFIRMED
  // 仅用于缺行补录，正常不会出现未确认行）
  const canSupply = Boolean(
    order &&
      ["CONFIRMED", "SUPPLY_REJECTED", "SUPPLIER_CONFIRMED"].includes(order.status),
  );

  const busy = confirmMut.submitting || rejectMut.submitting;
  const actionError = confirmMut.error || rejectMut.error;

  function draftOf(lineId: string) {
    return (
      drafts[lineId] ?? { qty: "", eta: "", note: "", reason: "", rejecting: false }
    );
  }

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-700">采购单明细</h2>
          {order ? (
            <div className="mt-0.5 font-mono text-xs text-slate-500">{order.code}</div>
          ) : null}
        </div>
        <button type="button" onClick={onClose} className="text-xs text-slate-500 hover:underline">
          关闭
        </button>
      </div>

      {!order ? (
        <EmptyState title="请选择采购单" description="从列表点击采购单号查看行明细与供货轨迹。" />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Info label="供应商" value={supplierName} />
            <Info label="品牌" value={brandName} />
            <Info label="片区" value={regionName} />
            <Info
              label="状态"
              value={<StatusBadge tone={statusTone(order.status)} label={label(order.status)} />}
            />
            <Info label="预计到货日" value={formatDate(order.expected_date)} />
            <Info label="总额（服务端重算）" value={money(order.total_amount_fen)} />
            <Info label="确认时间" value={formatDate(order.confirmed_at)} />
            <Info label="收货时间" value={formatDate(order.received_at)} />
            <Info label="备注" value={order.note ?? "—"} />
          </div>

          {mode === "supplier" && !canSupply ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              该采购单当前为「{label(order.status)}」，供货确认已截止（已收货/已取消的单不能再确认）。
              已确认记录仍可在下方查看；如有缺行，请联系片区经理核对。
            </div>
          ) : null}

          <div>
            <div className="text-xs font-medium text-slate-600">采购行（{order.lines.length} 行）</div>
            <div className="mt-2 space-y-2">
              {order.lines.map((ln) => {
                const c = byLine.get(ln.id);
                const d = draftOf(ln.id);
                const confirmed = c?.status === "CONFIRMED";
                const rejected = c?.status === "REJECTED";
                return (
                  <div key={ln.id} className="rounded-md border border-slate-200 p-3">
                    <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-5">
                      <Info label="商品" value={skuLabel(ln.sku_code, ln.sku_name)} />
                      <Info label="采购箱数" value={boxes(ln.quantity_cases)} />
                      <Info
                        label="含税单价（元/瓶）"
                        value={money(ln.tax_inclusive_unit_price_fen)}
                      />
                      <Info label="行金额" value={money(ln.line_amount_fen)} />
                      <Info
                        label="供货确认"
                        value={
                          c ? (
                            <StatusBadge
                              tone={c.status === "CONFIRMED" ? "success" : "danger"}
                              label={label(c.status)}
                            />
                          ) : (
                            <span className="text-slate-400">
                              {canSupply ? "待确认" : "未确认（已截止）"}
                            </span>
                          )
                        }
                      />
                    </div>
                    {confirmed ? (
                      <div className="mt-2 text-xs text-emerald-700">
                        确认供货 {boxes(c?.confirmed_quantity_cases)} 箱 · 预计到货{" "}
                        {formatDate(c?.expected_delivery_date)}
                        {c?.note ? ` · 备注：${c.note}` : ""}
                      </div>
                    ) : null}
                    {rejected ? (
                      <div className="mt-2 text-xs text-rose-600">
                        已拒绝 · 原因：{c?.reject_reason ?? "—"}
                      </div>
                    ) : null}

                    {mode === "supplier" && !c && canSupply ? (
                      <div className="mt-3 flex flex-wrap items-end gap-2">
                        <Field label="确认供货箱数 *">
                          <input
                            type="number"
                            min="0.01"
                            step="0.01"
                            value={d.qty}
                            onChange={(e) =>
                              setDrafts((prev) => ({
                                ...prev,
                                [ln.id]: { ...d, qty: e.target.value },
                              }))
                            }
                            className="w-28 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                          />
                        </Field>
                        <Field label="预计到货日 *">
                          <input
                            type="date"
                            value={d.eta}
                            onChange={(e) =>
                              setDrafts((prev) => ({ ...prev, [ln.id]: { ...d, eta: e.target.value } }))
                            }
                            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                          />
                        </Field>
                        <Field label="备注">
                          <input
                            value={d.note}
                            onChange={(e) =>
                              setDrafts((prev) => ({ ...prev, [ln.id]: { ...d, note: e.target.value } }))
                            }
                            className="w-40 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                          />
                        </Field>
                        <button
                          type="button"
                          disabled={busy || !d.qty || !d.eta}
                          onClick={async () => {
                            const res = await confirmMut.run({
                              lineId: ln.id,
                              qty: Number(d.qty),
                              eta: d.eta,
                              note: d.note.trim() || null,
                            });
                            if (res) {
                              onToast("已确认该行供货（不产生付款、不入库）", "success");
                              confirmations.refetch();
                              onChanged();
                            } else {
                              onToast(confirmMut.error || "确认失败（检查门户开关与品牌×片区授权）", "danger");
                            }
                          }}
                          className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                        >
                          确认供货
                        </button>
                        {d.rejecting ? (
                          <>
                            <Field label="拒绝原因 *">
                              <input
                                value={d.reason}
                                onChange={(e) =>
                                  setDrafts((prev) => ({
                                    ...prev,
                                    [ln.id]: { ...d, reason: e.target.value },
                                  }))
                                }
                                className="w-48 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                              />
                            </Field>
                            <button
                              type="button"
                              disabled={busy || !d.reason.trim()}
                              onClick={async () => {
                                const res = await rejectMut.run({
                                  lineId: ln.id,
                                  reason: d.reason.trim(),
                                });
                                if (res) {
                                  onToast("已拒绝该行供货（原因已留痕）", "success");
                                  confirmations.refetch();
                                  onChanged();
                                } else {
                                  onToast(rejectMut.error || "拒绝失败", "danger");
                                }
                              }}
                              className="rounded-md border border-rose-300 bg-white px-3 py-1.5 text-sm text-rose-700 disabled:opacity-50"
                            >
                              提交拒绝
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            onClick={() =>
                              setDrafts((prev) => ({
                                ...prev,
                                [ln.id]: { ...d, rejecting: true },
                              }))
                            }
                            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600"
                          >
                            无法供货
                          </button>
                        )}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>

          {actionError ? <div className="text-xs text-rose-600">{actionError}</div> : null}

          <div>
            <div className="text-xs font-medium text-slate-600">
              供货确认轨迹（{confirmations.data?.length ?? 0} 条；厂商账号只见自己的记录）
            </div>
            <div className="mt-2">
              <DataTable
                rows={confirmations.data ?? []}
                columns={[
                  {
                    key: "line",
                    header: "采购行",
                    render: (c) => {
                      const line = order.lines.find((l) => l.id === c.order_line_id);
                      return skuLabel(line?.sku_code, line?.sku_name);
                    },
                  },
                  {
                    key: "status",
                    header: "结果",
                    render: (c) => (
                      <StatusBadge
                        tone={c.status === "CONFIRMED" ? "success" : "danger"}
                        label={label(c.status)}
                      />
                    ),
                  },
                  {
                    key: "qty",
                    header: "确认箱数",
                    align: "right",
                    render: (c) => (c.confirmed_quantity_cases ? boxes(c.confirmed_quantity_cases) : "—"),
                  },
                  {
                    key: "eta",
                    header: "预计到货",
                    render: (c) => formatDate(c.expected_delivery_date),
                  },
                  {
                    key: "reason",
                    header: "拒绝原因 / 备注",
                    render: (c) => c.reject_reason ?? c.note ?? "—",
                  },
                ]}
                rowKey={(c) => c.id}
                emptyText="暂无供货确认记录"
              />
            </div>
          </div>
        </>
      )}
    </section>
  );
}

// ============================ 供应商管理 ============================

interface SupplierRow {
  id: string;
  code: string;
  name: string;
  contact_name: string | null;
  contact_phone: string | null;
  active: boolean;
}

function SupplierManagementTab({
  onToast,
}: {
  onToast: (message: string, tone?: "info" | "success" | "warning" | "danger") => void;
}) {
  const suppliers = useApiQuery<SupplierRow[]>("/procurement/suppliers/management", undefined, {
    enabled: true,
  });

  const createMut = useApiMutation(
    (p: { code: string; name: string; contact_name?: string; contact_phone?: string }) =>
      api.post<SupplierRow>("/procurement/suppliers", p),
  );

  const toggleMut = useApiMutation((id: string) =>
    api.post<SupplierRow>(`/procurement/suppliers/${id}/toggle-active`),
  );

  const rows = suppliers.data ?? [];

  // 新建表单状态
  const [showForm, setShowForm] = useState(false);
  const [formCode, setFormCode] = useState("");
  const [formName, setFormName] = useState("");
  const [formContactName, setFormContactName] = useState("");
  const [formContactPhone, setFormContactPhone] = useState("");

  const handleCreate = async () => {
    if (!formCode.trim() || !formName.trim()) {
      onToast("编码和名称不能为空", "warning");
      return;
    }
    const res = await createMut.run({
      code: formCode.trim(),
      name: formName.trim(),
      contact_name: formContactName.trim() || undefined,
      contact_phone: formContactPhone.trim() || undefined,
    });
    if (res) {
      onToast(`供应商 ${res.code} 创建成功`, "success");
      suppliers.refetch();
      setShowForm(false);
      setFormCode("");
      setFormName("");
      setFormContactName("");
      setFormContactPhone("");
    } else {
      onToast(createMut.error || "创建失败", "danger");
    }
  };

  const handleToggle = async (row: SupplierRow) => {
    const res = await toggleMut.run(row.id);
    if (res) {
      onToast(
        res.active ? `供应商 ${res.code} 已启用` : `供应商 ${res.code} 已停用`,
        res.active ? "success" : "warning",
      );
      suppliers.refetch();
    } else {
      onToast(toggleMut.error || "操作失败", "danger");
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-600">
          供应商管理：创建、启用/停用业务档案。新建采购单前确保至少有一个在职供应商。
          停用不影响历史订单，但新订单将无法选择已停用供应商。
        </p>
        <button
          type="button"
          onClick={() => setShowForm((v) => !v)}
          className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
        >
          {showForm ? "取消" : "+ 新建供应商"}
        </button>
      </div>

      {showForm && (
        <div className="rounded-md border border-slate-200 bg-slate-50 p-4 space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="编码 *">
              <input
                value={formCode}
                onChange={(e) => setFormCode(e.target.value)}
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                placeholder="如 SUP-001"
              />
            </Field>
            <Field label="名称 *">
              <input
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                placeholder="供应商公司名称"
              />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="联系人（可选）">
              <input
                value={formContactName}
                onChange={(e) => setFormContactName(e.target.value)}
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                placeholder="姓名"
              />
            </Field>
            <Field label="联系电话（可选）">
              <input
                value={formContactPhone}
                onChange={(e) => setFormContactPhone(e.target.value)}
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                placeholder="手机号或座机"
              />
            </Field>
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setShowForm(false)}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleCreate}
              disabled={createMut.submitting}
              className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {createMut.submitting ? "创建中…" : "确认创建"}
            </button>
          </div>
        </div>
      )}

      {suppliers.loading ? (
        <LoadingBlock text="加载供应商台账…" />
      ) : suppliers.error ? (
        <div className="rounded-md border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
          加载失败：{suppliers.error}
          {suppliers.status === 403 && (
            <div className="mt-1 text-xs opacity-75">当前账号缺少「设置」模块权限，无法访问供应商管理。</div>
          )}
          {suppliers.status === 401 && (
            <div className="mt-1 text-xs opacity-75">登录已失效，请重新登录后再试。</div>
          )}
          <button
            type="button"
            onClick={suppliers.refetch}
            className="mt-3 rounded-md border border-rose-300 bg-white px-3 py-1.5 text-xs text-rose-700 hover:bg-rose-100"
          >
            重试
          </button>
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          title="暂无供应商"
          description="点击右上角「新建供应商」创建第一个业务档案，之后方可在建单时选择。"
        />
      ) : (
          <DataTable
            rows={rows}
            columns={[
              { key: "code", header: "编码", render: (r) => r.code },
              { key: "name", header: "名称", render: (r) => r.name },
              {
                key: "contact",
                header: "联系人",
                render: (r) => r.contact_name ?? "—",
              },
              {
                key: "phone",
                header: "电话",
                render: (r) => r.contact_phone ?? "—",
              },
              {
                key: "active",
                header: "状态",
                render: (r) => (
                  <span
                    className={
                      r.active
                        ? "text-emerald-600 font-medium"
                        : "text-slate-400"
                    }
                  >
                    {r.active ? "在职" : "停用"}
                  </span>
                ),
              },
              {
                key: "actions",
                header: "操作",
                render: (r) => (
                  <button
                    type="button"
                    onClick={() => handleToggle(r)}
                    disabled={toggleMut.submitting}
                    className={
                      r.active
                        ? "rounded-md border border-rose-300 bg-white px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                        : "rounded-md border border-emerald-300 bg-white px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-50 disabled:opacity-50"
                    }
                  >
                    {r.active ? "停用" : "启用"}
                  </button>
                ),
              },
            ]}
            rowKey={(r) => r.id}
            emptyText="暂无数据"
          />
        )
      }
    </div>
  );
}
