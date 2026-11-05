/** 应用根组件：路由 + 角色守卫 + 应用外壳（T06）。
 *
 * 路由表唯一来源为 `lib/nav-config.ts` 的 `PAGE_ROUTES`（PRD §3.2 页面路由）：
 * - 已实现页面走 `IMPLEMENTED_PAGES`（当前仅今日工作台，带能力守卫）；
 * - 其余路径按同一张表渲染统一占位页，标题与「下一步」提示不再在组件里重复硬编码。
 */
import { useEffect } from "react";
import type { ReactNode } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useParams,
  useSearchParams,
} from "react-router-dom";

import { AppLayout } from "./components/layout/AppLayout";
import { RequireAbility } from "./components/auth/RequireAbility";
import { RequireAuth } from "./components/auth/RequireAuth";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { AuthProvider, devRoleFromUrl, useAuth } from "./lib/auth";
import { PAGE_ROUTES } from "./lib/nav-config";
import { Forbidden, NotFound } from "./pages/StatusPages";
import { Activities } from "./pages/Activities";
import { AdminUsers } from "./pages/AdminUsers";
import { AssistantPage } from "./pages/Assistant";
import { AuditLog } from "./pages/AuditLog";
import { ContractDetailPage, Contracts } from "./pages/Contracts";
import { FreezerDetailPage, Freezers } from "./pages/Freezers";
import { Login } from "./pages/Login";
import { MasterData } from "./pages/MasterData";
import { Regions } from "./pages/Regions";
import { Merchants } from "./pages/Merchants";
import { MobileEntry } from "./pages/mobile/MobileEntry";
import { Performance } from "./pages/Performance";
import { Placeholder } from "./pages/Placeholder";
import { FinanceLedger } from "./pages/FinanceLedger";
import { Payroll } from "./pages/Payroll";
import { Profit } from "./pages/Profit";
import { Reports } from "./pages/Reports";
import { Procurement } from "./pages/Procurement";
import { Replenishments } from "./pages/Replenishments";
import { Risks } from "./pages/Risks";
import { Schedule } from "./pages/Schedule";
import { Supervision } from "./pages/Supervision";
import { SupplierPortal } from "./pages/SupplierPortal";
import { VisitRecordPage, Visits } from "./pages/Visits";
import { Workspace } from "./pages/Workspace";

/** 已实现页面：按路径注册；未注册的路径渲染占位页。 */
const IMPLEMENTED_PAGES: Record<string, ReactNode> = {
  "/workspace": <Workspace />,
  "/merchants": <Merchants />,
  // PRD §3.2 详情路由：不单独成页，转为商家中心的详情面板深链
  "/merchants/:id": <MerchantDeepLink />,
  "/contracts": <Contracts />,
  "/contracts/:id": <ContractDetailPage />,
  "/assets/freezers": <Freezers />,
  "/assets/freezers/:id": <FreezerDetailPage />,
  "/visits": <Visits />,
  "/visits/:id": <VisitRecordPage />,
  "/schedule": <Schedule />,
  "/replenishments": <Replenishments />,
  "/campaigns": <Activities />,
  "/supervision": <Supervision />,
  "/procurement": <Procurement />,
  // PRD §3.2：厂商门户（功能开关开启才可访问，onlyFor SUPPLIER）
  "/supplier": <SupplierPortal />,
  "/finance/performance": <Performance />,
  // T20 工资发放：财务复核/发放（经理确认在工资单状态里体现）
  "/finance/payroll": <Payroll />,
  // T35 财务总账：收入/支出/净利润、公司物品采购与流水明细
  "/finance/ledger": <FinanceLedger />,
  "/finance/profit": <Profit />,
  "/reports": <Reports />,
  // AI 助手：历史会话列表 + 对话抽屉（T23 会话持久化）
  "/assistant": <AssistantPage />,
  // 工作台风险卡/风险区块的深链落点（T27 deep_link → /risks?...）
  "/risks": <Risks />,
  "/admin/users": <AdminUsers />,
  "/admin/regions": <Regions />,
  "/admin/master-data": <MasterData />,
  "/admin/audit": <AuditLog />,
};

/** 旧深链别名 → 现有页面（保留 query，避免丢失 period 等筛选条件）。 */
function LegacyRedirect({ to }: { to: string }) {
  const { search } = useLocation();
  return <Navigate to={`${to}${search}`} replace />;
}

/** 商家档案深链 `/merchants/:id` → 商家中心并展开该商家（T08 详情以页内面板呈现，无独立页）。 */
function MerchantDeepLink() {
  const { id } = useParams();
  const { search } = useLocation();
  const next = new URLSearchParams(search);
  if (id) next.set("merchant_id", id);
  const qs = next.toString();
  return <Navigate to={qs ? `/merchants?${qs}` : "/merchants"} replace />;
}

/** 风险详情深链 `/risks/:id` → 风险清单并高亮该条（NotificationService 的 deep_link 形态）。 */
function RiskDeepLink() {
  const { riskId } = useParams();
  const { search } = useLocation();
  const next = new URLSearchParams(search);
  if (riskId) next.set("risk_id", riskId);
  const qs = next.toString();
  return <Navigate to={qs ? `/risks?${qs}` : "/risks"} replace />;
}

function DevAutoLogin() {
  const { login, user } = useAuth();
  const [params] = useSearchParams();
  const location = useLocation();
  useEffect(() => {
    if (user) return;
    const dev = devRoleFromUrl(location.search);
    if (dev) login(dev);
  }, [user, params, login, location.search]);
  return null;
}

export default function App() {
  return (
    <ErrorBoundary>
      <AuthProvider>
        <BrowserRouter>
          <DevAutoLogin />
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/forbidden" element={<Forbidden />} />
            {/* T28 移动端 H5：独立全屏路由，不套桌面侧栏外壳 */}
            <Route
              path="/m/today"
              element={
                <RequireAuth>
                  <MobileEntry />
                </RequireAuth>
              }
            />
            <Route
              path="/*"
              element={
                <RequireAuth>
                  <AppLayout>
                    <ErrorBoundary>
                      <Routes>
                        <Route path="/" element={<Navigate to="/workspace" replace />} />
                        {/* T27 深链兼容：后端 WorkbenchService / NotificationService 输出的路径
                            与前端路由表并不同名，统一落到现有页面，避免点深链掉进 404；
                            query（period 等）原样保留。 */}
                        <Route path="/dashboard" element={<LegacyRedirect to="/workspace" />} />
                        <Route path="/tasks" element={<LegacyRedirect to="/workspace" />} />
                        <Route path="/tasks/*" element={<LegacyRedirect to="/workspace" />} />
                        <Route path="/approvals" element={<LegacyRedirect to="/workspace" />} />
                        <Route path="/approvals/*" element={<LegacyRedirect to="/workspace" />} />
                        <Route path="/risks/:riskId" element={<RiskDeepLink />} />
                        <Route path="/visits/new" element={<LegacyRedirect to="/visits" />} />
                        <Route path="/replenishment" element={<LegacyRedirect to="/replenishments" />} />
                        <Route path="/payroll" element={<LegacyRedirect to="/finance/payroll" />} />
                        <Route path="/procurement/confirm" element={<LegacyRedirect to="/procurement" />} />
                        <Route path="/supervision/issues" element={<LegacyRedirect to="/supervision" />} />
                        {PAGE_ROUTES.map((r) => {
                          const page = IMPLEMENTED_PAGES[r.to];
                          return (
                            <Route
                              key={r.to}
                              path={r.to}
                              element={
                                page ? (
                                  <RequireAbility module={r.required}>{page}</RequireAbility>
                                ) : (
                                  <Placeholder title={r.label} nextStep={r.nextStep} />
                                )
                              }
                            />
                          );
                        })}
                        <Route path="*" element={<NotFound />} />
                      </Routes>
                    </ErrorBoundary>
                  </AppLayout>
                </RequireAuth>
              }
            />
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </ErrorBoundary>
  );
}
