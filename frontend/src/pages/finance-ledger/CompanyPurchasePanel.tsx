/** 公司物品采购：录入（凭证必填）→ 待审批 → 审批通过自动进支出流水。 */
import { useState } from "react";

import { api } from "../../lib/api";
import { formatDate, label, money, statusTone, todayInTz } from "../../lib/format";
import { useApiMutation, useApiQuery } from "../../lib/useApi";
import { ErrorState, LoadingBlock } from "../../components/AsyncState";
import { EmptyState } from "../../components/EmptyState";
import { StatusBadge } from "../../components/StatusBadge";
import type { ToastTone } from "../../components/Toast";
import {
  PURCHASE_CATEGORIES,
  yuanToFen,
  type CompanyPurchase,
  type PurchaseCategory,
} from "./types";

const fieldClass =
  "w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-800 " +
  "outline-none transition focus:border-teal-600 focus:ring-2 focus:ring-teal-600/20";

function Field({ label: text, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-slate-600">
      <span>{text}</span>
      {children}
    </label>
  );
}

export function CompanyPurchasePanel({
  refreshKey,
  onChanged,
  onToast,
}: {
  refreshKey: number;
  onChanged: () => void;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const list = useApiQuery<CompanyPurchase[]>("/finance/company-purchases", {
    limit: 100,
    _r: refreshKey,
  });

  const [title, setTitle] = useState("");
  const [category, setCategory] = useState<PurchaseCategory>("OTHER");
  const [amount, setAmount] = useState("");
  const [incurredOn, setIncurredOn] = useState(todayInTz());
  const [remark, setRemark] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("");

  const submit = useApiMutation(async () => {
    if (!file) throw new Error("请先上传采购凭证（PDF / 图片）");
    const fen = yuanToFen(amount);
    if (fen === null || fen <= 0) throw new Error("金额需为大于 0 的元，最多两位小数");
    const voucher = await api.upload<{ file_id: string }>("/finance/vouchers", file);
    return api.post<CompanyPurchase>("/finance/company-purchases", {
      title: title.trim(),
      category,
      amount_fen: fen,
      incurred_on: incurredOn,
      voucher_file_id: voucher.file_id,
      remark: remark.trim() || null,
    });
  });

  const approve = useApiMutation((id: string) =>
    api.post<CompanyPurchase>(`/finance/company-purchases/${id}/approve`, {
      reason: "财务审批通过，计入支出流水",
    }),
  );

  const reject = useApiMutation((args: { id: string; reason: string }) =>
    api.post<CompanyPurchase>(`/finance/company-purchases/${args.id}/reject`, {
      reason: args.reason,
    }),
  );

  const handleSubmit = async () => {
    const outcome = await submit.runSafe(undefined);
    if (!outcome.ok) {
      onToast(outcome.error, "danger");
      return;
    }
    onToast("已提交，等待财务审批", "success");
    setTitle("");
    setAmount("");
    setRemark("");
    setFile(null);
    setIncurredOn(todayInTz());
    list.refetch();
  };

  const handleApprove = async (row: CompanyPurchase) => {
    if (!window.confirm(`确认审批「${row.title}」？通过后自动计入支出流水。`)) return;
    const outcome = await approve.runSafe(row.id);
    if (!outcome.ok) {
      onToast(outcome.error, "danger");
      return;
    }
    onToast("审批通过，已记入支出流水", "success");
    list.refetch();
    onChanged();
  };

  const handleReject = async (id: string) => {
    const reason = rejectReason.trim();
    if (!reason) {
      onToast("驳回必须填写原因", "danger");
      return;
    }
    const outcome = await reject.runSafe({ id, reason });
    if (!outcome.ok) {
      onToast(outcome.error, "danger");
      return;
    }
    onToast("已驳回该采购申请", "info");
    setRejectingId(null);
    setRejectReason("");
    list.refetch();
  };

  const rows = list.data ?? [];

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-700">公司物品采购</h2>
        <span className="text-xs text-slate-400">
          提交后为「待审批」，审批通过自动计入支出流水（只记账，不在线支付）
        </span>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
        <form
          className="space-y-3 rounded-lg border border-slate-200 bg-slate-50/60 p-4"
          onSubmit={(e) => {
            e.preventDefault();
            void handleSubmit();
          }}
        >
          <Field label="物品名称 *">
            <input
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              maxLength={128}
              placeholder="如 门店物料打印纸（10 箱）"
              className={fieldClass}
            />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="类别 *">
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value as PurchaseCategory)}
                className={fieldClass}
              >
                {PURCHASE_CATEGORIES.map(([value, text]) => (
                  <option key={value} value={value}>
                    {text}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="金额（元）*">
              <input
                required
                inputMode="decimal"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                onBlur={() => {
                  const fen = yuanToFen(amount);
                  if (fen !== null) setAmount((fen / 100).toFixed(2));
                }}
                placeholder="0.00"
                className={fieldClass}
              />
            </Field>
          </div>
          <Field label="发生日期 *">
            <input
              required
              type="date"
              value={incurredOn}
              onChange={(e) => setIncurredOn(e.target.value)}
              className={fieldClass}
            />
          </Field>
          <Field label="凭证（PDF / 图片）*">
            <input
              required
              type="file"
              accept="application/pdf,image/png,image/jpeg,image/webp"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="text-xs text-slate-600 file:mr-2 file:rounded file:border-0 file:bg-teal-700 file:px-3 file:py-1.5 file:text-xs file:text-white hover:file:bg-teal-800"
            />
          </Field>
          <Field label="备注">
            <textarea
              value={remark}
              onChange={(e) => setRemark(e.target.value)}
              rows={2}
              maxLength={500}
              placeholder="用途说明、领用部门等"
              className={fieldClass}
            />
          </Field>
          <button
            type="submit"
            disabled={submit.submitting}
            className="w-full rounded-md bg-teal-700 px-3 py-2 text-sm font-medium text-white transition hover:bg-teal-800 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {submit.submitting ? "提交中…" : "提交采购申请"}
          </button>
        </form>

        <div className="min-w-0">
          {list.loading ? <LoadingBlock text="加载采购记录…" /> : null}
          {list.error ? (
            <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
          ) : null}
          {!list.loading && !list.error ? (
            rows.length === 0 ? (
              <EmptyState
                title="暂无公司物品采购"
                description="录入物品名称、金额与凭证后提交，审批通过自动计入支出流水。"
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-200 text-left text-xs text-slate-500">
                      <th className="py-2 pr-3 font-medium">日期</th>
                      <th className="py-2 pr-3 font-medium">物品</th>
                      <th className="py-2 pr-3 font-medium">类别</th>
                      <th className="py-2 pr-3 text-right font-medium">金额</th>
                      <th className="py-2 pr-3 font-medium">状态</th>
                      <th className="py-2 font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.id} className="border-b border-slate-100 align-top">
                        <td className="py-2 pr-3 whitespace-nowrap text-slate-600">
                          {formatDate(row.incurred_on)}
                        </td>
                        <td className="py-2 pr-3 text-slate-800">
                          <div className="font-medium">{row.title}</div>
                          <div className="text-xs text-slate-400">
                            {row.expense_no}
                            {row.remark ? ` · ${row.remark}` : ""}
                          </div>
                        </td>
                        <td className="py-2 pr-3 whitespace-nowrap text-slate-600">
                          {label(row.category)}
                        </td>
                        <td className="py-2 pr-3 text-right tabular-nums text-slate-800">
                          {money(row.amount_fen)}
                        </td>
                        <td className="py-2 pr-3">
                          <StatusBadge tone={statusTone(row.status)} label={label(row.status)} />
                          {row.rejected_reason ? (
                            <div className="mt-1 text-xs text-rose-600">{row.rejected_reason}</div>
                          ) : null}
                        </td>
                        <td className="py-2">
                          {row.status === "PENDING_APPROVAL" ? (
                            rejectingId === row.id ? (
                              <div className="flex flex-col gap-1">
                                <input
                                  value={rejectReason}
                                  onChange={(e) => setRejectReason(e.target.value)}
                                  placeholder="驳回原因（必填）"
                                  className="w-36 rounded border border-slate-300 px-2 py-1 text-xs"
                                />
                                <div className="flex gap-1">
                                  <button
                                    type="button"
                                    onClick={() => void handleReject(row.id)}
                                    disabled={reject.submitting}
                                    className="rounded bg-rose-600 px-2 py-1 text-xs text-white hover:bg-rose-700 disabled:bg-slate-300"
                                  >
                                    确认驳回
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => {
                                      setRejectingId(null);
                                      setRejectReason("");
                                    }}
                                    className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                                  >
                                    取消
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <div className="flex gap-1">
                                <button
                                  type="button"
                                  onClick={() => void handleApprove(row)}
                                  disabled={approve.submitting}
                                  className="rounded bg-teal-700 px-2 py-1 text-xs text-white hover:bg-teal-800 disabled:bg-slate-300"
                                >
                                  审批通过
                                </button>
                                <button
                                  type="button"
                                  onClick={() => {
                                    setRejectingId(row.id);
                                    setRejectReason("");
                                  }}
                                  className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                                >
                                  驳回
                                </button>
                              </div>
                            )
                          ) : (
                            <span className="text-xs text-slate-400">—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : null}
        </div>
      </div>
    </section>
  );
}
