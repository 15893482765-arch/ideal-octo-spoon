/** 应用根组件：路由 + 角色守卫 + 应用外壳（T06）。 */
import { useEffect } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useSearchParams } from "react-router-dom";

import { AppLayout } from "./components/layout/AppLayout";
import { RequireAbility } from "./components/auth/RequireAbility";
import { RequireAuth } from "./components/auth/RequireAuth";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { AuthProvider, devRoleFromUrl, useAuth } from "./lib/auth";
import { MODULE } from "./lib/permissions";
import { Forbidden, NotFound } from "./pages/StatusPages";
import { Login } from "./pages/Login";
import { Placeholder } from "./pages/Placeholder";
import { Workspace } from "./pages/Workspace";

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
            <Route
              path="/*"
              element={
                <RequireAuth>
                  <AppLayout>
                    <ErrorBoundary>
                      <Routes>
                        <Route path="/" element={<Navigate to="/workspace" replace />} />
                        <Route
                          path="/workspace"
                          element={
                            <RequireAbility module={MODULE.DASHBOARD}>
                              <Workspace />
                            </RequireAbility>
                          }
                        />
                        <Route path="/merchants" element={<Placeholder title="商家中心" nextStep="T08 商家中心" />} />
                        <Route path="/contracts" element={<Placeholder title="合同中心" nextStep="T09 合同与审批" />} />
                        <Route path="/assets/freezers" element={<Placeholder title="冰柜资产台账" nextStep="T10 冰柜资产" />} />
                        <Route path="/visits" element={<Placeholder title="巡访与打卡" nextStep="T11 巡访与排班" />} />
                        <Route path="/schedule" element={<Placeholder title="周排班与地图" nextStep="T13 周排班" />} />
                        <Route path="/replenishments" element={<Placeholder title="补货中心" nextStep="T15 补货与核销" />} />
                        <Route path="/campaigns" element={<Placeholder title="地推活动" nextStep="T16 活动与兑付" />} />
                        <Route path="/supervision" element={<Placeholder title="督导跟访与整改" nextStep="T11 督导整改" />} />
                        <Route path="/procurement" element={<Placeholder title="采购与供货" nextStep="T14 采购与供货" />} />
                        <Route path="/finance/performance" element={<Placeholder title="绩效核算" nextStep="T19 绩效汇总" />} />
                        <Route path="/finance/payroll" element={<Placeholder title="工资发放" nextStep="T19 工资发放" />} />
                        <Route path="/reports" element={<Placeholder title="经营报表" nextStep="T20 经营报表" />} />
                        <Route path="/admin/users" element={<Placeholder title="用户与账号管理" nextStep="T07 主数据" />} />
                        <Route path="/admin/master-data" element={<Placeholder title="品牌/SKU/片区/规则/导入" nextStep="T07 主数据" />} />
                        <Route path="/admin/audit" element={<Placeholder title="审计日志" nextStep="T18 审计/导出" />} />
                        <Route path="/supplier" element={<Placeholder title="厂商门户" nextStep="T12 厂商门户" />} />
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
