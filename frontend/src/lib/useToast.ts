/** Toast 状态管理（配合 components/Toast 的 ToastStack 使用）。 */
import { useCallback, useState } from "react";

import type { ToastItem, ToastTone } from "../components/Toast";

let seq = 0;

export function useToast() {
  const [items, setItems] = useState<ToastItem[]>([]);

  const push = useCallback((message: string, tone: ToastTone = "info") => {
    seq += 1;
    setItems((prev) => [...prev, { id: `t-${seq}`, message, tone }]);
  }, []);

  const dismiss = useCallback((id: string) => {
    setItems((prev) => prev.filter((i) => i.id !== id));
  }, []);

  return { items, push, dismiss };
}
