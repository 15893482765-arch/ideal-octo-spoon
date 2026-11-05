/** AuthContext（T03 真实登录接入 + T06 角色能力）。
 *
 * - 真实登录：`POST /api/auth/login` → 保存 access/refresh token → `GET /api/auth/me` 取权威用户字段；
 * - 会话恢复：刷新页面后用已有 token 调 `/me`，401 时静默失效并回到登录页；
 * - 无注册入口（后端不提供 register）；
 * - `login(user)` 保留为开发/测试注入（无 token，不产生真实会话）；
 * - `?role=` 开发直入仅用于本地体验，不可替代真实登录。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  api,
  clearTokens,
  getAccessToken,
  onUnauthorized,
  setTokens,
} from "./api";
import {
  ROLE_ABILITIES,
  hasAbility as hasAbilityFn,
  type ModuleCode,
  type RoleCode,
} from "./permissions";

export interface AuthUser {
  id: string;
  username: string;
  displayName: string;
  role: RoleCode;
}

interface MeResponse {
  id: string;
  username: string;
  display_name: string;
  role_code: string;
  role_name?: string;
  must_change_password: boolean;
}

interface LoginOut {
  access_token: string;
  refresh_token?: string;
  token_type?: string;
  must_change_password?: boolean;
}

export interface AuthContextValue {
  user: AuthUser | null;
  /** 是否持有真实后端会话（决定业务请求是否可行）。 */
  authenticated: boolean;
  /** 会话恢复是否已完成（避免刷新时闪烁）。 */
  ready: boolean;
  mustChangePassword: boolean;
  loginError: string | null;
  /** 真实登录；失败抛 ApiError，由页面展示 message。 */
  loginWithCredentials: (
    username: string,
    password: string,
  ) => Promise<{ mustChangePassword: boolean }>;
  changePassword: (oldPassword: string, newPassword: string) => Promise<void>;
  /** 首次激活：用管理员下发的一次性凭据 + 自设新密码把账号置为 ACTIVE。 */
  activate: (activationToken: string, newPassword: string) => Promise<void>;
  /** 开发/测试注入（无 token）。 */
  login: (user: AuthUser) => void;
  logout: () => Promise<void>;
  hasAbility: (m: ModuleCode) => boolean;
}

const Ctx = createContext<AuthContextValue | null>(null);

function meToUser(me: MeResponse): AuthUser {
  return {
    id: me.id,
    username: me.username,
    displayName: me.display_name,
    role: me.role_code as RoleCode,
  };
}

export function AuthProvider(props: { children: ReactNode; initial?: AuthUser | null }) {
  const { initial = null } = props;
  const [user, setUser] = useState<AuthUser | null>(initial);
  const [authenticated, setAuthenticated] = useState(false);
  const [mustChangePassword, setMustChangePassword] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  // 无 token 时无需异步恢复，直接就绪
  const [ready, setReady] = useState(() => !getAccessToken());

  // 会话恢复：仅当存在 token 时才请求 /me（测试环境无 token，不发请求）
  useEffect(() => {
    if (!getAccessToken()) return;
    let alive = true;
    (async () => {
      try {
        const me = await api.get<MeResponse>("/auth/me");
        if (!alive) return;
        setUser(meToUser(me));
        setMustChangePassword(Boolean(me.must_change_password));
        setAuthenticated(true);
      } catch {
        if (!alive) return;
        clearTokens();
        setUser(null);
        setAuthenticated(false);
      } finally {
        if (alive) setReady(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // token 失效广播（refresh 亦失败）
  useEffect(
    () =>
      onUnauthorized(() => {
        setUser(null);
        setAuthenticated(false);
        setMustChangePassword(false);
      }),
    [],
  );

  const loginWithCredentials = useCallback(async (username: string, password: string) => {
    setLoginError(null);
    try {
      const out = await api.post<LoginOut>("/auth/login", { username, password });
      setTokens(out.access_token, out.refresh_token);
      const me = await api.get<MeResponse>("/auth/me");
      setUser(meToUser(me));
      const must = Boolean(out.must_change_password ?? me.must_change_password);
      setMustChangePassword(must);
      setAuthenticated(true);
      return { mustChangePassword: must };
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : "登录失败");
      throw err;
    }
  }, []);

  const changePassword = useCallback(async (oldPassword: string, newPassword: string) => {
    await api.post<void>("/auth/change-password", {
      old_password: oldPassword,
      new_password: newPassword,
    });
    setMustChangePassword(false);
  }, []);

  const activate = useCallback(
    async (activationToken: string, newPassword: string) => {
      await api.post<void>("/auth/activate", {
        activation_token: activationToken,
        new_password: newPassword,
      });
    },
    [],
  );

  const login = useCallback((u: AuthUser) => {
    setUser(u);
    setAuthenticated(false); // 开发注入不产生真实会话
  }, []);

  const logout = useCallback(async () => {
    try {
      if (getAccessToken()) await api.post<void>("/auth/logout");
    } catch {
      /* 后端不可达也要保证本地登出 */
    }
    clearTokens();
    setUser(null);
    setAuthenticated(false);
    setMustChangePassword(false);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      authenticated,
      ready,
      mustChangePassword,
      loginError,
      loginWithCredentials,
      changePassword,
      activate,
      login,
      logout,
      hasAbility: (m) => hasAbilityFn(user?.role ?? null, m),
    }),
    [
      user,
      authenticated,
      ready,
      mustChangePassword,
      loginError,
      loginWithCredentials,
      changePassword,
      activate,
      login,
      logout,
    ],
  );
  return <Ctx.Provider value={value}>{props.children}</Ctx.Provider>;
}

export function useAuth(): AuthContextValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth 必须在 <AuthProvider> 内使用");
  return v;
}

/** 工具：从 URL ?role= 解析开发态 mock 登录（仅供 Q028 测试与 dev 体验）。 */
export function devRoleFromUrl(search: string): AuthUser | null {
  const params = new URLSearchParams(search);
  const role = params.get("role") as RoleCode | null;
  if (!role || !(role in ROLE_ABILITIES)) return null;
  return {
    id: `dev-${role.toLowerCase()}`,
    username: `dev_${role.toLowerCase()}`,
    displayName: `开发账号 ${role}`,
    role,
  };
}
