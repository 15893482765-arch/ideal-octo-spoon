/** 响应式断点 hook（T06 / PRD §3.1 桌面优先 / 移动端 H5 优先）。
 *
 * 简化设计：使用 window.matchMedia 检测宽度，避免引入额外依赖；
 * 默认断点：>= 1024px 桌面；< 1024px 移动端（侧栏收起）。
 */

import { useEffect, useState } from "react";

const MOBILE_MAX = 1023;

export function useIsMobile(): boolean {
  const [mobile, setMobile] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.innerWidth <= MOBILE_MAX;
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    const mq = window.matchMedia(`(max-width: ${MOBILE_MAX}px)`);
    const handler = (e: MediaQueryListEvent) => setMobile(e.matches);
    setMobile(mq.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return mobile;
}
