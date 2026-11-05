/** 定位采集（T36）：把「当前定位无法获取」翻译成可执行的原因与处置建议。
 *
 * 浏览器定位失败的常见根因（按现场频率排序）：
 * 1. 非安全上下文：用 `http://192.168.x.x` 打开后台，Chrome/Safari 直接拒绝定位；
 * 2. 权限被拒：曾点过“阻止”，后续调用静默失败（需要到站点设置里改回允许）；
 * 3. 位置不可用：室内/无 GPS/系统定位开关关闭，返回 code 2；
 * 4. 超时：默认 10s 太短，室内冷启动经常超时，返回 code 3。
 *
 * 只做一次性采集，绝不 watchPosition / 后台持续定位。
 */

export type LocateFailureReason =
  | "insecure"
  | "unsupported"
  | "denied"
  | "unavailable"
  | "timeout"
  | "unknown";

export interface LocateFailure {
  reason: LocateFailureReason;
  /** 面向业务员的一句话结论 */
  message: string;
  /** 具体怎么办 */
  hint: string;
}

export interface LocateSuccess {
  lng: number;
  lat: number;
  accuracy: number;
}

export type LocateResult =
  | { ok: true; data: LocateSuccess }
  | { ok: false; failure: LocateFailure };

const FAILURE_TEXT: Record<LocateFailureReason, { message: string; hint: string }> = {
  insecure: {
    message: "当前页面不是 HTTPS，浏览器禁止定位",
    hint: "请用 https:// 域名打开（或用手机浏览器访问正式域名）；局域网 IP 的 http 地址无法定位。",
  },
  unsupported: {
    message: "当前设备/浏览器不支持定位",
    hint: "请改用手机浏览器或 App 端打卡；也可先手填经纬度并写明原因。",
  },
  denied: {
    message: "定位权限被拒绝",
    hint: "点地址栏左侧的站点图标 → 位置 → 允许，然后重试；手机上还需打开系统「定位服务」。",
  },
  unavailable: {
    message: "暂时取不到位置（信号弱）",
    hint: "走到店门口或靠窗处重试；室内负一楼、电梯口常取不到，可稍等再点一次。",
  },
  timeout: {
    message: "定位超时",
    hint: "首次定位较慢，请重试；仍超时可手填经纬度并在备注说明设备情况。",
  },
  unknown: {
    message: "定位失败",
    hint: "请重试，或手填经纬度并写明原因。",
  },
};

function failure(reason: LocateFailureReason): LocateFailure {
  return { reason, ...FAILURE_TEXT[reason] };
}

/** 浏览器是否处于安全上下文（HTTPS 或 localhost）。 */
export function isSecureContext(): boolean {
  if (typeof window === "undefined") return false;
  return window.isSecureContext !== false;
}

/**
 * 主动采集一次定位。
 * @param timeoutMs 超时时间，默认 20s（比浏览器默认 10s 更宽容）
 */
export function locateOnce(timeoutMs = 20_000): Promise<LocateResult> {
  if (typeof navigator === "undefined" || !("geolocation" in navigator)) {
    return Promise.resolve({ ok: false, failure: failure("unsupported") });
  }
  if (!isSecureContext()) {
    return Promise.resolve({ ok: false, failure: failure("insecure") });
  }

  return new Promise((resolve) => {
    let settled = false;
    const done = (result: LocateResult) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };

    try {
      navigator.geolocation.getCurrentPosition(
        (pos) =>
          done({
            ok: true,
            data: {
              lng: pos.coords.longitude,
              lat: pos.coords.latitude,
              accuracy: Math.round(pos.coords.accuracy),
            },
          }),
        (err) => {
          if (err.code === err.PERMISSION_DENIED) return done({ ok: false, failure: failure("denied") });
          if (err.code === err.POSITION_UNAVAILABLE) {
            return done({ ok: false, failure: failure("unavailable") });
          }
          if (err.code === err.TIMEOUT) return done({ ok: false, failure: failure("timeout") });
          return done({ ok: false, failure: failure("unknown") });
        },
        { enableHighAccuracy: true, timeout: timeoutMs, maximumAge: 0 },
      );
    } catch {
      // 少数浏览器在权限策略拦截时直接抛异常
      done({ ok: false, failure: failure("denied") });
    }

    // 双保险：个别 WebView 既不回调 success 也不回调 error
    setTimeout(() => done({ ok: false, failure: failure("timeout") }), timeoutMs + 2_000);
  });
}
