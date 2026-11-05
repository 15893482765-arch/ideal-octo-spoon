/** T28 离线队列 React Hook：把弱网引擎接到 H5 页面。 */

import { useCallback, useEffect, useRef, useState } from "react";

import { AttachmentStore } from "./offline/attachment-store";
import { DraftStore, type DraftStorage } from "./offline/draft-store";
import { SyncCoordinator } from "./offline/sync-coordinator";
import type {
  DraftDiff,
  FetchStateFn,
  OfflineDraft,
  SubmitFn,
} from "./offline/types";

export interface QueueState {
  drafts: OfflineDraft[];
  pendingCount: number;
  conflictCount: number;
  online: boolean;
}

export interface OfflineQueue extends QueueState {
  saveDraft: (d: OfflineDraft) => void;
  removeDraft: (id: string) => void;
  /** 自动通道：仅提交 low 且无冲突草稿。 */
  flush: () => Promise<void>;
  /** 用户在差异界面显式确认后提交单条（含高风险）。 */
  confirmAndReplay: (id: string) => Promise<void>;
  evaluate: (id: string) => Promise<DraftDiff | null>;
  attachments: AttachmentStore;
}

export function useOfflineQueue(deps: {
  submit: SubmitFn;
  fetchState: FetchStateFn;
  storage?: DraftStorage;
  isOnline?: () => boolean;
  now?: () => string;
}): OfflineQueue {
  const { submit, fetchState } = deps;
  const attachmentsRef = useRef<AttachmentStore | null>(null);
  if (!attachmentsRef.current) attachmentsRef.current = new AttachmentStore();

  const storeRef = useRef<DraftStore | null>(null);
  if (!storeRef.current) storeRef.current = new DraftStore(deps.storage);

  const coordRef = useRef<SyncCoordinator | null>(null);
  if (!coordRef.current) {
    coordRef.current = new SyncCoordinator(
      storeRef.current,
      attachmentsRef.current,
      submit,
      fetchState,
      { now: deps.now, revokeUrl: (u) => URL.revokeObjectURL(u) },
    );
  }

  const refresh = useCallback(() => {
    const all = storeRef.current!.list();
    setDrafts(all);
    setPendingCount(all.filter((d) => d.status !== "submitted").length);
    setConflictCount(all.filter((d) => d.status === "conflict").length);
  }, []);

  const [drafts, setDrafts] = useState<OfflineDraft[]>(() => storeRef.current!.list());
  const [pendingCount, setPendingCount] = useState<number>(
    () => storeRef.current!.pending().length,
  );
  const [conflictCount, setConflictCount] = useState<number>(
    () => storeRef.current!.list().filter((d) => d.status === "conflict").length,
  );
  const [online, setOnline] = useState<boolean>(
    () => (deps.isOnline ? deps.isOnline() : typeof navigator === "undefined" ? true : navigator.onLine),
  );

  const saveDraft = useCallback(
    (d: OfflineDraft) => {
      storeRef.current!.upsert(d);
      refresh();
    },
    [refresh],
  );

  const removeDraft = useCallback(
    (id: string) => {
      const d = storeRef.current!.get(id);
      if (d) {
        attachmentsRef.current!.releaseMany(
          d.attachments.map((a) => a.localRef),
          (u) => URL.revokeObjectURL(u),
        );
      }
      storeRef.current!.remove(id);
      refresh();
    },
    [refresh],
  );

  const flush = useCallback(async () => {
    await coordRef.current!.replayAll();
    refresh();
  }, [refresh]);

  const confirmAndReplay = useCallback(
    async (id: string) => {
      await coordRef.current!.replay(id, true);
      refresh();
    },
    [refresh],
  );

  const evaluate = useCallback(async (id: string) => {
    try {
      return await coordRef.current!.evaluate(id);
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    if (typeof window === "undefined" || !window.addEventListener) return;
    const up = () => {
      setOnline(true);
      void coordRef.current!.replayAll().then(refresh);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    drafts,
    pendingCount,
    conflictCount,
    online,
    saveDraft,
    removeDraft,
    flush,
    confirmAndReplay,
    evaluate,
    attachments: attachmentsRef.current,
  };
}
