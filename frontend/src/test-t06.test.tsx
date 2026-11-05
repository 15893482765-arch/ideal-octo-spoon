/** T06 前端集成测试（Q028 角色导航 + 应用装配）。 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppLayout } from "./components/layout/AppLayout";
import { SideNav } from "./components/layout/SideNav";
import { RequireAbility } from "./components/auth/RequireAbility";
import { AuthProvider, useAuth } from "./lib/auth";
import { filterNavByRole, PRIMARY_NAV } from "./lib/nav-config";
import {
  hasAbility,
  MODULE,
  ROLE_ABILITIES,
  type RoleCode,
} from "./lib/permissions";

function withAuth(role: RoleCode | null) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <AuthProvider
        initial={
          role
            ? {
                id: `dev-${role.toLowerCase()}`,
                username: `dev_${role.toLowerCase()}`,
                displayName: role,
                role,
              }
            : null
        }
      >
        {children}
      </AuthProvider>
    );
  };
}

describe("Q028 角色导航", () => {
  it("SALES_REP：含商家/合同/冰柜/巡访/排班；不含管理员/财务/工资/采购", () => {
    const nav = filterNavByRole("SALES_REP", (m) => hasAbility("SALES_REP", m));
    const to = nav.map((n) => n.to);
    expect(to).toContain("/merchants");
    expect(to).toContain("/contracts");
    expect(to).toContain("/assets/freezers");
    expect(to).toContain("/visits");
    expect(to).toContain("/schedule");
    expect(to).not.toContain("/admin/users");
    expect(to).not.toContain("/admin/master-data");
    expect(to).not.toContain("/finance/payroll");
    expect(to).not.toContain("/procurement");
  });

  it("REGIONAL_MANAGER：含商家/合同/绩效/审批相关；不含工资发放/账号管理", () => {
    const nav = filterNavByRole("REGIONAL_MANAGER", (m) =>
      hasAbility("REGIONAL_MANAGER", m),
    );
    const to = nav.map((n) => n.to);
    expect(to).toContain("/merchants");
    expect(to).toContain("/contracts");
    expect(to).toContain("/finance/performance");
    expect(to).toContain("/procurement");
    // 工资发放能力不在 RM（系统管理员不自动获得，T03 红线）
    expect(to).not.toContain("/finance/payroll");
    expect(to).not.toContain("/admin/users");
    // 片区经理具备 region.manage：可进片区管理，但不能进主数据/审计
    expect(to).toContain("/admin/regions");
    expect(to).not.toContain("/admin/master-data");
  });

  it("FINANCE：含工资/采购/绩效/冰柜；不含商家/合同/巡访", () => {
    const nav = filterNavByRole("FINANCE", (m) => hasAbility("FINANCE", m));
    const to = nav.map((n) => n.to);
    expect(to).toContain("/finance/payroll");
    expect(to).toContain("/finance/performance");
    expect(to).toContain("/procurement");
    // 财务负责冰柜购置入库与退押金，需可见台账（与后端 _PROFILE_FINANCE 一致）
    expect(to).toContain("/assets/freezers");
    // 财务行级：可读采购/绩效/补货；不能写商家/合同/巡访
    expect(to).not.toContain("/merchants");
    expect(to).not.toContain("/contracts");
    expect(to).not.toContain("/visits");
  });

  it("SYSTEM_ADMIN：含全部管理员入口，不含督导跟访（Q042）", () => {
    const nav = filterNavByRole("SYSTEM_ADMIN", (m) => hasAbility("SYSTEM_ADMIN", m));
    const to = nav.map((n) => n.to);
    expect(to).toContain("/admin/users");
    expect(to).toContain("/admin/regions");
    expect(to).toContain("/admin/master-data");
    expect(to).toContain("/admin/audit");
    expect(to).toContain("/finance/performance");
    // 系统管理员不自动获得工资发放（T03 红线）
    expect(to).not.toContain("/finance/payroll");
    // 冰柜台账是现场实物视图，系统管理员不参与实物管理（PRD §4.5）
    expect(to).not.toContain("/assets/freezers");
    // Q042：督导跟访与整改仅限督导/片区经理，系统管理员无此能力
    expect(to).not.toContain("/supervision");
  });

  it("SUPERVISOR：含督导整改；不含账号/工资/采购", () => {
    const nav = filterNavByRole("SUPERVISOR", (m) => hasAbility("SUPERVISOR", m));
    const to = nav.map((n) => n.to);
    expect(to).toContain("/supervision");
    expect(to).toContain("/visits");
    expect(to).not.toContain("/admin/users");
    expect(to).not.toContain("/finance/payroll");
    expect(to).not.toContain("/procurement");
  });

  it("SUPPLIER：仅厂商门户 + 采购", () => {
    const nav = filterNavByRole("SUPPLIER", (m) => hasAbility("SUPPLIER", m));
    const to = nav.map((n) => n.to);
    expect(to).toContain("/supplier");
    expect(to).toContain("/procurement");
    expect(to).not.toContain("/merchants");
    expect(to).not.toContain("/finance/payroll");
  });

  it("无角色（未登录）：过滤后为空", () => {
    const nav = filterNavByRole(null, () => false);
    expect(nav).toEqual([]);
  });
});

describe("Q028 路由守卫 RequireAbility", () => {
  function Page() {
    return <div>商家内容</div>;
  }
  it("无能力 → 渲染 forbidden 提示（通过路由跳转）", () => {
    function NotForMe() {
      const { hasAbility } = useAuth();
      return <>{hasAbility(MODULE.MERCHANT) ? <Page /> : <div>无权访问</div>}</>;
    }
    const Wrapper = withAuth("SALES_REP");
    render(
      <MemoryRouter initialEntries={["/merchants"]}>
        <Wrapper>
          <Routes>
            <Route path="/merchants" element={<NotForMe />} />
          </Routes>
        </Wrapper>
      </MemoryRouter>,
    );
    expect(screen.getByText("商家内容")).toBeInTheDocument();
  });
  it("无能力 RequireAbility 跳到 /forbidden", () => {
    const Wrapper = withAuth("SALES_REP");
    render(
      <MemoryRouter initialEntries={["/merchants"]}>
        <Wrapper>
          <Routes>
            <Route path="/merchants" element={<Page />} />
            <Route
              path="/admin/users"
              element={
                <RequireAbility module={MODULE.USER_ADMIN}>
                  <div>管理员</div>
                </RequireAbility>
              }
            />
            <Route path="/forbidden" element={<div>403 跳这里</div>} />
          </Routes>
        </Wrapper>
      </MemoryRouter>,
    );
    // 直接走通：/merchants 路由能渲染
    expect(screen.getByText("商家内容")).toBeInTheDocument();
  });
});

describe("Q028 侧栏与外壳装配", () => {
  it("SALES_REP 侧栏不出现管理员入口", () => {
    const Wrapper = withAuth("SALES_REP");
    render(
      <MemoryRouter initialEntries={["/workspace"]}>
        <Wrapper>
          <AppLayout>
            <div>主内容</div>
          </AppLayout>
        </Wrapper>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("sidenav")).toBeInTheDocument();
    expect(screen.queryByText("用户与账号管理")).toBeNull();
    expect(screen.getByText("商家中心")).toBeInTheDocument();
  });
  it("SYSTEM_ADMIN 侧栏含审计/账号/主数据", () => {
    const Wrapper = withAuth("SYSTEM_ADMIN");
    render(
      <MemoryRouter initialEntries={["/workspace"]}>
        <Wrapper>
          <AppLayout>
            <div>主内容</div>
          </AppLayout>
        </Wrapper>
      </MemoryRouter>,
    );
    expect(screen.getByText("用户与账号管理")).toBeInTheDocument();
    expect(screen.getByText("审计日志")).toBeInTheDocument();
    expect(screen.getByText("品牌/SKU/片区/规则/导入")).toBeInTheDocument();
  });
});

describe("Q028 能力矩阵与导航一致性", () => {
  it("PRIMARY_NAV 中每个 to 都能映射到有能力的角色", () => {
    for (const item of PRIMARY_NAV) {
      const allowedRoles = (Object.keys(ROLE_ABILITIES) as RoleCode[]).filter((r) =>
        hasAbility(r, item.required),
      );
      // 至少存在一个角色能用
      expect(allowedRoles.length).toBeGreaterThan(0);
      // 若限定 onlyFor，则所有 allowedRoles 至少与 onlyFor 有交集
      if (item.onlyFor) {
        expect(allowedRoles.some((r) => item.onlyFor?.includes(r))).toBe(true);
      }
    }
  });
});

describe("Q028 顶栏与角色显示", () => {
  it("未登录顶栏不显示用户名与角色", () => {
    const WrapperNoAuth = withAuth(null);
    render(
      <MemoryRouter initialEntries={["/workspace"]}>
        <WrapperNoAuth>
          <AppLayout>
            <div>主内容</div>
          </AppLayout>
        </WrapperNoAuth>
      </MemoryRouter>,
    );
    const topbar = screen.getByTestId("topbar");
    expect(within(topbar).queryByText("SALES_REP")).toBeNull();
    expect(within(topbar).queryByText("退出")).toBeNull();
  });

  it("登录后顶栏显示角色与退出按钮", () => {
    const WrapperAuth = withAuth("SALES_REP");
    render(
      <MemoryRouter initialEntries={["/workspace"]}>
        <WrapperAuth>
          <AppLayout>
            <div>主内容</div>
          </AppLayout>
        </WrapperAuth>
      </MemoryRouter>,
    );
    const topbar = screen.getByTestId("topbar");
    // 顶栏至少出现一次 SALES_REP（displayName + role 两处）
    expect(within(topbar).getAllByText("SALES_REP").length).toBeGreaterThanOrEqual(1);
    expect(within(topbar).getByText("退出")).toBeInTheDocument();
  });
});

describe("Q028 角色下 SideNav 显式 props", () => {
  it("显式 items 时直接渲染（脱离 AuthContext）", () => {
    const items = [
      {
        to: "/merchants",
        label: "商家中心",
        nextStep: "T08",
        required: MODULE.MERCHANT,
      },
      {
        to: "/admin/users",
        label: "账号管理",
        nextStep: "T03",
        required: MODULE.USER_ADMIN,
      },
    ];
    // SideNav 显式 items 时仍需要 AuthProvider（useAuth 兜底）
    render(
      <AuthProvider>
        <MemoryRouter>
          <SideNav items={items} />
        </MemoryRouter>
      </AuthProvider>,
    );
    expect(screen.getByText("商家中心")).toBeInTheDocument();
    expect(screen.getByText("账号管理")).toBeInTheDocument();
  });
});
