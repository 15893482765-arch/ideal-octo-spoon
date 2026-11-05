/** 财务总账（T35）：类型与展示常量。
 *
 * 金额一律整数分，展示时统一用 `fenToYuan` / `money`；
 * 后端只记流水（收入/支出/调整），本页不做任何口径重算。
 */
import type { StatusTone } from "../../components/StatusBadge";

export type Granularity = "month" | "year";

export interface LedgerBucket {
  period: string;
  income_fen: number;
  expense_fen: number;
  net_fen: number;
}

export interface LedgerSummary {
  granularity: Granularity;
  year: number;
  month: number | null;
  income_fen: number;
  expense_fen: number;
  net_fen: number;
  buckets: LedgerBucket[];
}

export type TxType = "INCOME" | "EXPENSE" | "ADJUSTMENT";

export interface LedgerTransaction {
  id: string;
  biz_date: string;
  tx_type: TxType;
  source_kind: string;
  source_label: string;
  amount_fen: number;
  related_object_type: string;
  related_object_id: string;
  note: string | null;
  bookkeeper_id: string;
  bookkeeper_name: string | null;
  created_at: string;
}

export type PurchaseCategory = "PROCUREMENT" | "FREEZER_PURCHASE" | "OTHER";

export interface CompanyPurchase {
  id: string;
  expense_no: string;
  title: string;
  category: PurchaseCategory;
  amount_fen: number;
  incurred_on: string;
  status: string;
  voucher_file_id: string;
  remark: string | null;
  approved_at: string | null;
  rejected_reason: string | null;
  created_at: string;
}

export interface TxFilters {
  tx_type: string;
  source_kind: string;
  date_from: string;
  date_to: string;
}

export const EMPTY_TX_FILTERS: TxFilters = {
  tx_type: "",
  source_kind: "",
  date_from: "",
  date_to: "",
};

export const TX_TYPE_LABELS: Record<string, string> = {
  INCOME: "收入",
  EXPENSE: "支出",
  ADJUSTMENT: "调整",
};

export const TX_TYPE_TONES: Record<string, StatusTone> = {
  INCOME: "success",
  EXPENSE: "danger",
  ADJUSTMENT: "warning",
};

/** 流水来源（与后端 SOURCE_KIND_LABELS 对齐，用于筛选下拉）。 */
export const SOURCE_OPTIONS: ReadonlyArray<[string, string]> = [
  ["SALES_REVENUE", "销售收款"],
  ["PROCUREMENT", "采购进货"],
  ["PAYROLL", "工资发放"],
  ["ASSET", "资产购置"],
  ["COMPANY_PURCHASE", "公司物品采购"],
  ["OPEX", "运营费用"],
  ["OTHER", "其他支出"],
];

/** 公司物品采购类别（复用后端费用类型）。 */
export const PURCHASE_CATEGORIES: ReadonlyArray<[PurchaseCategory, string]> = [
  ["PROCUREMENT", "采购物品"],
  ["FREEZER_PURCHASE", "冰柜购置"],
  ["OTHER", "其他支出"],
];

export const RELATION_LABELS: Record<string, string> = {
  SALES_CONFIRMATION: "销量确认单",
  PURCHASE_ORDER: "采购单",
  PAYROLL_STATEMENT: "工资单",
  EXPENSE: "费用单",
  FREEZER_ASSET: "冰柜资产",
};

/** 元字符串 → 整数分（失焦时调用；非法输入返回 null）。 */
export function yuanToFen(value: string): number | null {
  const text = value.trim();
  if (!text) return null;
  if (!/^\d+(\.\d{0,2})?$/.test(text)) return null;
  return Math.round(Number(text) * 100);
}
