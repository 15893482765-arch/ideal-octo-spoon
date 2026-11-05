/** 登录页（PRD §4.1）：真实接入后端 T03 鉴权（`POST /api/auth/login`）。
 *
 * - 无注册入口；账号由系统管理员创建并下发一次性激活凭据；
 * - 首登强制改密：`must_change_password=true` 时先进入改密视图；
 * - 429 限流会带 `retry_after_seconds`，页面按后端提示展示；
 * - 开发态（DEV）提供「快速登录」：选择角色即用对应演示账号走真实登录接口。
 *
 * UI 迭代：整页左右分栏——左侧为半模糊货架实景 + 紫色蒙版 + 大字号标语，
 * 右侧为登录表单。校验、提交与跳转逻辑与改造前一致。
 */
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import loginBg from "../assets/login-bg.jpg";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { RoleCode } from "../lib/permissions";

/** 开发态快速登录：角色 → 演示账号（真实登录，产生真会话）。 */
const DEV_ACCOUNTS: ReadonlyArray<{
  key: RoleCode;
  label: string;
  account: string;
  person: string;
}> = [
  { key: "REGIONAL_MANAGER", label: "经理", account: "gaojianmin01", person: "高建民" },
  { key: "SALES_REP", label: "业务员", account: "zhangwei01", person: "张伟" },
  { key: "FINANCE", label: "财务", account: "liumeling03", person: "刘美玲" },
  { key: "SUPERVISOR", label: "管理员", account: "chenxiaomei02", person: "陈晓梅" },
  { key: "SUPPLIER", label: "厂商", account: "wangjianjian", person: "王建建" },
];

const DEV_PASSWORD = "Qs@123456";

export function Login() {
  const { loginWithCredentials, changePassword, activate, authenticated, mustChangePassword } =
    useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/workspace";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [devRole, setDevRole] = useState<RoleCode>("REGIONAL_MANAGER");
  // 首次激活：新账号 status=PENDING_ACTIVATION，登录会被拒绝，必须先消化一次性凭据
  const [mode, setMode] = useState<"login" | "activate">("login");
  const [activationToken, setActivationToken] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (authenticated && !mustChangePassword) navigate(from, { replace: true });
  }, [authenticated, mustChangePassword, navigate, from]);

  function describe(err: unknown): string {
    if (err instanceof ApiError) {
      if (err.status === 429) {
        const wait = err.retryAfterSeconds ? `，请 ${err.retryAfterSeconds} 秒后重试` : "";
        return `登录尝试过于频繁${wait}`;
      }
      return err.message;
    }
    return err instanceof Error ? err.message : "请求失败";
  }

  async function onLogin(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await loginWithCredentials(username.trim(), password);
    } catch (err) {
      setError(describe(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function onChangePassword(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致");
      return;
    }
    setSubmitting(true);
    try {
      await changePassword(password, newPassword);
    } catch (err) {
      setError(describe(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function onActivate(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setNotice("");
    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致");
      return;
    }
    setSubmitting(true);
    try {
      await activate(activationToken.trim(), newPassword);
      setActivationToken("");
      setNewPassword("");
      setConfirmPassword("");
      setMode("login");
      setNotice("激活成功，请用账号与新密码登录");
    } catch (err) {
      setError(describe(err));
    } finally {
      setSubmitting(false);
    }
  }

  /** 开发态快速登录：选中角色对应的演示账号，走真实登录接口。 */
  async function onDevEnter() {
    const target = DEV_ACCOUNTS.find((a) => a.key === devRole);
    if (!target) return;
    setError("");
    setNotice("");
    setSubmitting(true);
    try {
      await loginWithCredentials(target.account, DEV_PASSWORD);
    } catch (err) {
      setError(describe(err));
    } finally {
      setSubmitting(false);
    }
  }

  const changing = authenticated && mustChangePassword;
  const current = DEV_ACCOUNTS.find((a) => a.key === devRole);

  return (
    <div className="min-h-screen w-full bg-surface lg:grid lg:grid-cols-[1.08fr_1fr]">
      {/* ---------- 左：货架实景 + 标语 ---------- */}
      <aside className="relative isolate flex min-h-[420px] overflow-hidden lg:min-h-screen">
        <img
          src={loginBg}
          alt=""
          aria-hidden="true"
          className="absolute inset-0 h-full w-full scale-110 object-cover blur-[3px]"
        />
        {/* 紫色蒙版：保证白字可读，同时保持整体主色调不变 */}
        <div
          aria-hidden="true"
          className="absolute inset-0 bg-gradient-to-br from-[#1f0b5e]/92 via-[#4a22c9]/78 to-[#7c5cfc]/66"
        />
        <div
          aria-hidden="true"
          className="absolute inset-0 bg-[radial-gradient(110%_70%_at_15%_0%,rgba(255,255,255,0.26),transparent_58%)]"
        />
        {/* 左侧竖光条，增加纵深 */}
        <div
          aria-hidden="true"
          className="absolute top-0 bottom-0 left-0 w-[3px] bg-gradient-to-b from-transparent via-[#c9b4ff] to-transparent opacity-70"
        />

        {/* 接缝羽化：右缘渐隐入白底，去掉生硬的分界线（桌面） */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 right-0 hidden w-32 bg-gradient-to-l from-surface via-surface/70 to-transparent lg:block"
        />
        {/* 接缝羽化：小屏上下布局时改为底缘渐隐 */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 bottom-0 h-20 bg-gradient-to-t from-surface to-transparent lg:hidden"
        />

        <div className="relative flex h-full w-full flex-col justify-between gap-8 p-8 sm:p-10 lg:p-14">
          {/* 品牌 */}
          <div className="flex items-center gap-3">
            <span className="grid h-10 w-10 place-items-center rounded-[12px] bg-white text-[17px] font-bold text-brand-strong shadow-[0_14px_30px_-14px_rgba(0,0,0,0.7)]">
              沁
            </span>
            <div className="leading-tight">
              <div className="text-[15px] font-semibold tracking-tight text-white sm:text-[16px]">
                沁爽饮料分销管理平台
              </div>
              <div className="mt-0.5 text-[10px] font-medium tracking-[0.22em] text-white/50 uppercase">
                Beverage Distribution Suite
              </div>
            </div>
          </div>

          {/* 标语主体 */}
          <div className="max-w-[34ch]">
            <span className="inline-flex items-center gap-2 rounded-full bg-white/15 px-3.5 py-1.5 text-[12px] font-medium tracking-wide text-white/95 ring-1 ring-white/20 backdrop-blur-sm">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-300 shadow-[0_0_10px_2px_rgba(110,231,183,0.7)]" />
              B2B 饮料分销 · 全链路数字化
            </span>

            <h2 className="mt-6 text-[32px] leading-[1.06] font-extrabold tracking-[-0.03em] text-white drop-shadow-[0_10px_30px_rgba(0,0,0,0.45)] sm:text-[52px] lg:text-[60px]">
              <span className="block whitespace-nowrap">每一瓶的动销，</span>
              <span className="block bg-gradient-to-r from-white via-[#e6d9ff] to-[#ab8bff] bg-clip-text text-transparent">
                都算得清。
              </span>
            </h2>

            <p className="mt-5 max-w-[32ch] text-[15px] leading-7 font-medium text-white/80">
              从货架陈列到财务总账，商家、冰柜、巡访、收支
              在同一条链路上对齐。
            </p>

            {/* 数据点：把"文字偏少"补足 */}
            <div className="mt-7 flex flex-wrap items-end gap-x-8 gap-y-4">
              {[
                { n: "40+", t: "在册商家" },
                { n: "5", t: "大片区" },
                { n: "1s", t: "一句话出图" },
              ].map((item) => (
                <div key={item.t}>
                  <div className="text-[26px] leading-none font-bold tracking-tight text-white">
                    {item.n}
                  </div>
                  <div className="mt-1.5 text-[11px] tracking-wide text-white/55">{item.t}</div>
                </div>
              ))}
            </div>
          </div>

          {/* 能力点 */}
          <div className="grid max-w-[460px] grid-cols-3 gap-2.5">
            {[
              { k: "商家建档", v: "归属与等级" },
              { k: "冰柜资产", v: "投放与回收" },
              { k: "AI 问数", v: "问句即图表" },
            ].map((item) => (
              <div
                key={item.k}
                className="rounded-[12px] bg-white/12 px-3.5 py-3 ring-1 ring-white/15 backdrop-blur-sm"
              >
                <div className="text-[12px] leading-4 font-semibold text-white">{item.k}</div>
                <div className="mt-1 text-[11px] text-white/55">{item.v}</div>
              </div>
            ))}
          </div>
        </div>
      </aside>

      {/* ---------- 右：登录表单 ---------- */}
      <main className="flex items-center justify-center px-5 py-10 sm:px-8 lg:px-12">
        <div className="w-full max-w-[380px]">
          <form
            onSubmit={changing ? onChangePassword : mode === "activate" ? onActivate : onLogin}
            className="w-full"
          >
            <p className="text-[12px] font-medium tracking-wide text-brand-strong">
              {changing ? "首次登录" : mode === "activate" ? "账号激活" : "欢迎回来"}
            </p>
            <h1 className="qs-page-title mt-1 text-[26px]">
              {changing ? "设置新密码" : mode === "activate" ? "激活账号" : "登录"}
            </h1>
            <p className="qs-hint mt-1.5">
              无注册入口；账号由系统管理员创建并下发激活凭据。
            </p>

            {changing ? (
              <>
                <p className="mt-4 rounded-[12px] bg-amber-50 px-3 py-2 text-[12px] leading-5 text-amber-800">
                  首次登录/重置后需修改密码后才能使用其他模块。
                </p>
                <Field label="新密码（≥8 位）">
                  <input
                    type="password"
                    required
                    minLength={8}
                    autoComplete="new-password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    className="qs-input mt-1.5"
                  />
                </Field>
                <Field label="确认新密码">
                  <input
                    type="password"
                    required
                    minLength={8}
                    autoComplete="new-password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    className="qs-input mt-1.5"
                  />
                </Field>
              </>
            ) : mode === "activate" ? (
              <>
                <p className="mt-4 rounded-[12px] bg-sky-50 px-3 py-2 text-[12px] leading-5 text-sky-800">
                  新账号第一次使用：粘贴管理员给你的「一次性激活凭据」，并设置自己的密码。
                  激活前用账号+密码登录会提示「账号尚未激活」。
                </p>
                <Field label="一次性激活凭据">
                  <textarea
                    required
                    rows={3}
                    value={activationToken}
                    onChange={(e) => setActivationToken(e.target.value)}
                    placeholder="粘贴管理员下发的一长串凭据"
                    className="qs-input mt-1.5 font-mono text-[12px]"
                  />
                </Field>
                <Field label="设置密码（≥8 位）">
                  <input
                    type="password"
                    required
                    minLength={8}
                    autoComplete="new-password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    className="qs-input mt-1.5"
                  />
                </Field>
                <Field label="确认密码">
                  <input
                    type="password"
                    required
                    minLength={8}
                    autoComplete="new-password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    className="qs-input mt-1.5"
                  />
                </Field>
              </>
            ) : (
              <>
                <Field label="账号 / 工号 / 手机号">
                  <input
                    required
                    autoComplete="username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    className="qs-input mt-1.5"
                  />
                </Field>
                <Field label="密码">
                  <input
                    type="password"
                    required
                    minLength={8}
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="qs-input mt-1.5"
                  />
                </Field>
              </>
            )}

            {notice ? (
              <p className="mt-4 rounded-[12px] bg-emerald-50 px-3 py-2 text-[12px] leading-5 text-emerald-700">
                {notice}
              </p>
            ) : null}

            {error ? (
              <p
                role="alert"
                className="mt-4 rounded-[12px] bg-rose-50 px-3 py-2 text-[12px] leading-5 text-rose-700"
              >
                {error}
              </p>
            ) : null}

            <button
              type="submit"
              disabled={submitting}
              className="qs-btn qs-btn-primary mt-6 h-11 w-full text-[14px]"
            >
              {submitting
                ? "提交中…"
                : changing
                  ? "修改密码并进入"
                  : mode === "activate"
                    ? "激活账号"
                    : "登录"}
            </button>

            {!changing ? (
              <button
                type="button"
                onClick={() => {
                  setError("");
                  setNotice("");
                  setMode(mode === "activate" ? "login" : "activate");
                }}
                className="qs-btn qs-btn-text mt-2 w-full text-[12px]"
              >
                {mode === "activate" ? "已有密码，返回登录" : "新账号首次使用？用激活凭据激活"}
              </button>
            ) : null}

            <p className="qs-hint mt-5 leading-5">
              下一步：账号由管理员创建后，用一次性激活凭据自设密码，再用账号+新密码登录。
            </p>
          </form>

          {import.meta.env.DEV && !changing ? (
            <div className="mt-6 rounded-[14px] bg-page p-4">
              <p className="qs-hint">
                开发态快速登录（走真实登录接口，产生真实会话）：
              </p>
              <div className="mt-2 flex gap-2">
                <select
                  aria-label="选择演示账号"
                  value={devRole}
                  onChange={(e) => setDevRole(e.target.value as RoleCode)}
                  className="qs-input flex-1"
                >
                  {DEV_ACCOUNTS.map((a) => (
                    <option key={a.key} value={a.key}>
                      {a.label} · {a.person}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={onDevEnter}
                  disabled={submitting}
                  className="qs-btn qs-btn-ghost bg-surface"
                >
                  登录
                </button>
              </div>
              {current ? (
                <p className="qs-hint mt-2">
                  账号 {current.account} · 密码 {DEV_PASSWORD}
                  {current.key === "SUPERVISOR" ? "（该账号实际角色为督导）" : ""}
                </p>
              ) : null}
            </div>
          ) : null}

          <p className="qs-hint mt-6 text-center">© 2026 沁爽分销</p>
        </div>
      </main>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="mt-4 block text-[12px] font-medium text-ink-2">
      {label}
      {children}
    </label>
  );
}
