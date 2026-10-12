/** 应用外壳：顶栏 + 侧栏 + 主内容 + 响应式（移动端侧栏抽屉化）。 */
import { useState } from "react";
import type { ReactNode } from "react";

import { useIsMobile } from "../../lib/responsive";
import { SideNav } from "./SideNav";
import { TopBar } from "./TopBar";

export function AppLayout({ children }: { children: ReactNode }) {
  const mobile = useIsMobile();
  const [drawer, setDrawer] = useState(false);
  return (
    <div className="flex min-h-screen flex-col bg-slate-50" data-testid="app-layout">
      <TopBar />
      <div className="flex flex-1">
        {mobile ? (
          drawer ? (
            <div className="fixed inset-0 z-30 flex">
              <div
                className="absolute inset-0 bg-slate-900/40"
                onClick={() => setDrawer(false)}
              />
              <SideNav onNavigate={() => setDrawer(false)} />
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setDrawer(true)}
              className="m-2 h-9 self-start rounded-md border border-slate-300 bg-white px-3 text-sm"
            >
              菜单
            </button>
          )
        ) : (
          <SideNav />
        )}
        <main className="flex-1 overflow-x-auto p-4">{children}</main>
      </div>
    </div>
  );
}
