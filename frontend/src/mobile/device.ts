/** T28 相机 / 定位能力封装（Q165 拒绝时的替代提示；红线：定位仅用户主动打卡采集）。 */

export type PermissionState = "granted" | "denied" | "unavailable" | "unsupported";

export interface GeoFix {
  lng: number;
  lat: number;
  accuracyMeters: number;
  capturedAt: string;
}

export interface DeviceResult<T> {
  state: PermissionState;
  data?: T;
  /** 面向业务员的可操作替代提示（不暴露技术错误）。 */
  fallbackHint?: string;
}

/** 浏览器权限错误码归一化。 */
function classify(err: unknown): PermissionState {
  const code = (err as { code?: number; name?: string })?.code;
  const name = (err as { name?: string })?.name;
  if (code === 1 || name === "NotAllowedError" || name === "PermissionDeniedError") {
    return "denied";
  }
  if (code === 3 || name === "PositionUnavailableError") return "unavailable";
  return "unavailable";
}

/**
 * 主动获取一次精确定位（打卡按钮内调用）。
 * 绝不做 watchPosition / 后台持续定位。
 */
export function captureLocationOnce(
  opts: {
    geolocation?: Geolocation;
    now?: () => string;
  } = {},
): Promise<DeviceResult<GeoFix>> {
  const geo = opts.geolocation ?? (typeof navigator !== "undefined" ? navigator.geolocation : undefined);
  if (!geo) {
    return Promise.resolve({
      state: "unsupported",
      fallbackHint:
        "当前设备不支持定位。可先填写纸质巡访备注，回到有信号区域后补拍门头并由经理在修订中确认。",
    });
  }
  return new Promise((resolve) => {
    geo.getCurrentPosition(
      (pos) =>
        resolve({
          state: "granted",
          data: {
            lng: pos.coords.longitude,
            lat: pos.coords.latitude,
            accuracyMeters: pos.coords.accuracy,
            capturedAt: (opts.now ?? (() => new Date().toISOString()))(),
          },
        }),
      (err) => {
        const state = classify(err);
        resolve({
          state,
          fallbackHint:
            state === "denied"
              ? "定位权限被拒绝：请到「设置-浏览器-位置」允许定位后重试；本次可先保存草稿（不计为已打卡），恢复权限再提交。"
              : "暂时获取不到定位信号：请到店门口开阔处重试，或先保存草稿稍后再打卡。",
        });
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 },
    );
  });
}

export type CaptureKind = "storefront" | "display" | "freezer" | "replenishment";

/**
 * 调起相机/相册。返回的 File 交由 AttachmentStore 临时暂存（不落持久缓存）。
 * 不直接持有二进制，避免本模块耦合存储。
 */
export async function capturePhoto(
  input: HTMLInputElement | null,
  kind: CaptureKind,
): Promise<DeviceResult<{ file: File; kind: CaptureKind }>> {
  if (!input) {
    return {
      state: "unsupported",
      fallbackHint: "当前环境无法调起相机，请使用可拍照手机重新打开本页面。",
    };
  }
  // 接受：移动端优先调起后置相机，同时允许相册（弱网/无相机时的替代路径）。
  input.setAttribute("accept", "image/*");
  input.setAttribute("capture", "environment");

  const file = await pickFile(input);
  if (!file) {
    // 用户取消输入：视为拒绝/未授权，给出明确替代路径而非静默失败。
    return {
      state: "denied",
      fallbackHint:
        "未获取到照片。可点「拍照」重试，或从相册选择门头/陈列照片；无法拍照时可先保存草稿，稍后补拍。",
    };
  }
  if (!file.type.startsWith("image/")) {
    return {
      state: "unavailable",
      fallbackHint: "所选文件不是图片，请重新拍摄或选择 JPG/PNG 照片。",
    };
  }
  return { state: "granted", data: { file, kind } };
}

/** 以 Promise 方式读取一次 <input type=file> 选择。 */
function pickFile(input: HTMLInputElement): Promise<File | null> {
  return new Promise((resolve) => {
    const cleanup = () => input.removeEventListener("change", onChange);
    const onChange = () => {
      cleanup();
      resolve(input.files && input.files.length > 0 ? input.files[0] : null);
    };
    input.addEventListener("change", onChange);
    input.click();
  });
}
