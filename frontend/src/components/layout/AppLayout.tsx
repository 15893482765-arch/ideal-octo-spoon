/** 应用外壳：顶栏 + 侧栏 + 主内容 + 响应式（移动端侧栏抽屉化）。
 *
 * UI 迭代：页底色改为浅灰、内容区统一内边距与最大宽度、侧栏抽屉加圆角与阴影。
 * state（drawer）与交互保持不变。
 */
import { useState } from "react";
import type { ReactNode } from "react";

import { useIsMobile } from "../../lib/responsive";
import { SideNav } from "./SideNav";
import { TopBar } from "./TopBar";

export function AppLayout({ children }: { children: ReactNode }) {
  const mobile = useIsMobile();
  const [drawer, setDrawer] = useState(false);
  return (
    <div className="flex min-h-screen flex-col bg-page" data-testid="app-layout">
      <TopBar />
      <div className="flex flex-1">
        {mobile ? (
          drawer ? (
            <div className="fixed inset-0 z-30 flex">
              <div
                className="absolute inset-0 bg-ink/35 backdrop-blur-[2px]"
                onClick={() => setDrawer(false)}
              />
              <div className="relative z-10 h-full overflow-hidden rounded-r-[20px] shadow-pop">
                <SideNav onNavigate={() => setDrawer(false)} />
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setDrawer(true)}
              className="qs-btn qs-btn-ghost m-3 h-9 self-start px-3"
            >
              菜单
            </button>
          )
        ) : (
          <SideNav />
        )}
        <main className="flex-1 overflow-x-auto px-4 py-5 lg:px-6 lg:py-6">
          <div className="mx-auto w-full max-w-[1440px]">{children}</div>
        </main>
      </div>
    </div>
  );
}
