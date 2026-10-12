# 认证与权限（T03）

> 本文件描述 T03 落地的账号生命周期、JWT 凭证、6 类角色权限矩阵与行级范围；
> 依赖数据模型详见 `data-model.md`，**不重复**其中的表结构。

## 1. 账号生命周期（PRD §2.1）

```
[ 无账号 ] → 系统管理员 / 部署期 bootstrap → PENDING_ACTIVATION
                                                  │
                                  (一次性凭据)   │
                                                  ▼
                                                ACTIVE
                                                  │
                              ┌───────────────────┼───────────────────┐
                              │                   │                   │
                       (管理员停用)            (管理员改密)         (角色变更)
                              ▼                   │                   │
                          INACTIVE                │                   │
                              │                   ▼                   ▼
                              │            password_version++  password_version++
                              │            (踢所有旧 token)   (踢所有旧 token)
                              ▼
                          历史保留
```

- **无注册入口**：`POST /api/auth/register` 一律 404/405；唯一可创建账号的入口是
  `POST /api/admin/users`（仅 SYSTEM_ADMIN）。
- **系统管理员不能设置/查询初始密码**：建号时系统生成一次性 `ACTIVATION` 凭据，
  仅在创建响应 `one_time_credential` 字段中返回，**不在**列表/导出/审计中回显。
- **首次激活强制改密**：`User.must_change_password=True`，激活路径 `POST /api/auth/activate`
  同时消费凭据、设置新密码、生成新 `password_version`。
- **停用立即失效**：`User.status=INACTIVE` 且 `password_version` 提升，使所有旧 token 失效；
  历史 `auth_events` / `audit_logs` 保留。

### 1.1 首次系统管理员（Bootstrap）

- 配置：`INITIAL_ADMIN_BOOTSTRAP=true` + `INITIAL_ADMIN_USERNAME` / `INITIAL_ADMIN_PASSWORD` /
  `INITIAL_ADMIN_DISPLAY_NAME` 三个环境变量（其中密码 `SecretStr`）。
- 触发：应用 `lifespan` 启动时调用 `bootstrap_initial_admin`；**仅当数据库内没有
  `SYSTEM_ADMIN` ACTIVE 用户**时执行一次。
- 收尾：运维在首登后立即从部署环境移除 `INITIAL_ADMIN_PASSWORD`，强制后续使用"修改密码"流程。

## 2. 凭证与令牌

| 凭据 | 用途 | 寿命 | 存储 |
| --- | --- | --- | --- |
| `ACTIVATION` token | 一次性激活 | 默认 7 天 | 内存 `TokenRevocationStore`（jti 黑名单） |
| `PASSWORD_RESET` token | 一次性重置 | 默认 24 小时 | 同上 |
| `ACCESS` token (HS256 JWT) | API 鉴权 | 默认 900s | 仅验证签名 + 撤销名单 |
| `REFRESH` token (HS256 JWT) | 换取新 access | 默认 30 天 | 同 access；一次性消费（消费即撤销 jti） |

### 2.1 撤销机制

```text
T 时刻签发 token(pv=1, jti=J)
   │
   ├─ 显式登出：revoke_jti(J)
   ├─ 改密：bump_user_version(uid, pv+1)  → 旧 pv 全部失效
   ├─ 角色变更：bump_user_version(uid, pv+1)
   ├─ 停用：status=INACTIVE + bump_user_version
   └─ 一次性凭据消费：revoke_jti(J)
```

`token_revocation.is_revoked(jti, user_id, pv)` 在 `decode_token` 中强制校验。
T03 阶段撤销名单与登录失败计数器在**进程内**（`LoginRateLimiter`、`TokenRevocationStore`）；
T04 迁到 Redis，接口与调用方不变。

## 3. 6 类角色 × 模块权限矩阵（PRD §2.2/§2.4）

代码内唯一锚：`app/core/permissions.py:RoleCode` + `RoleProfile.abilities`。

| 模块编号 | SALES_REP | REGIONAL_MANAGER | SUPERVISOR | FINANCE | SYSTEM_ADMIN | SUPPLIER |
| --- | --- | --- | --- | --- | --- | --- |
| `auth.login` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `dashboard.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `merchant.*` | ✓ | ✓ | ✓（只读） | | ✓ | |
| `contract.*` | ✓ | ✓ | ✓（只读） | | ✓ | |
| `freezer.*` | ✓ | ✓ | ✓ | | ✓ | |
| `visit.*` | ✓ | ✓ | ✓ | | ✓ | |
| `replenishment.*` | ✓ | ✓ | ✓ | ✓ | ✓ | |
| `sales.confirm` | | ✓ | | | ✓ | |
| `schedule.*` | ✓ | ✓ | ✓ | | ✓ | |
| `activity.*` | ✓ | ✓ | ✓ | | ✓ | |
| `procurement.*` | | ✓ | | ✓ | ✓ | ✓（仅确认供货） |
| `finance.*` | | | | ✓ | ✓ | |
| `performance.*` | | ✓ | | ✓ | ✓ | |
| `payroll.view` | | | | ✓ | ✓ | |
| **`payroll.disburse`** | | | | **✓** | **✗**（红线） | |
| `user.admin` | | | | | ✓ | |
| `audit.*` | | | | | ✓ | |
| `settings.*` | | | | | ✓ | |
| `supplier.confirm_supply` | | | | | | ✓（受 `SUPPLIER_PORTAL_ENABLED` 约束） |

**红线已落代码**：
- `MODULE_PAYROLL_DISBURSE` 不在 SYSTEM_ADMIN.abilities；
- 厂商对 `SENSITIVE_FIELDS_SUPPLIER`（`merchant_contact_phone`、`merchant_location_wkt`、
  `payroll_*_fen`、`performance_*_fen`、`phone` 等）一律返回 `null`，由
  `PermissionService.redact_payload()` 在响应序列化前完成。

## 4. 行级范围（PRD §2.2/§2.4）

`ActorContext` 字段全部由 `current_actor` 依赖从 JWT + `user_region_brand_auths` 表注入；
**禁止**路由参数或模型字段覆盖。

| row_scope | 说明 |
| --- | --- |
| `self` | 业务员：只看到"自己负责"商家（Service 层用 `merchant.owner_user_id` 切片） |
| `region` | 片区经理/督导：限本片区 (`actor.region_ids ∩ candidate`) |
| `global` | 财务/系统管理员：全量 |
| `brand_region` | 厂商：限授权 `(brand, region)` 交集，且**不直接返回商家 ID 列表** |
| `global_readonly` | 督导扩展使用（T08 落地） |

## 5. 端点清单

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/auth/login` | 公开 | 账号+密码登录 |
| POST | `/api/auth/logout` | Bearer access | access 撤销 jti |
| POST | `/api/auth/refresh` | Bearer refresh | 一次性换取新 access + refresh |
| POST | `/api/auth/activate` | 公开（持 ACTIVATION） | 一次性凭据 + 改密 |
| POST | `/api/auth/change-password` | Bearer access | 改密并踢旧 token |
| POST | `/api/auth/password-reset/consume` | 公开（持 PASSWORD_RESET） | 管理员重置后用户激活 |
| GET  | `/api/auth/me` | Bearer access | 当前用户信息（角色+must_change_password） |
| POST | `/api/admin/users` | `user.admin` | 建号（返回一次性凭据） |
| GET  | `/api/admin/users` | `user.admin` | 账号列表（脱敏：仅状态/角色名） |
| POST | `/api/admin/users/{id}/deactivate` | `user.admin` | 停用（含原因）；禁止自裁 |
| POST | `/api/admin/users/{id}/reactivate` | `user.admin` | 重新激活并返回新一次性凭据 |
| POST | `/api/admin/users/{id}/change-role` | `user.admin` | 改角色并踢旧 token；禁止自裁 |

## 6. 错误码到 HTTP 状态

| 错误族 | HTTP | 例子 |
| --- | --- | --- |
| `AuthError` (子类) | 401 | `INVALID_CREDENTIALS`、`ACCOUNT_LOCKED`、`ACCOUNT_INACTIVE`、`ACCOUNT_PENDING_ACTIVATION`、`INVALID_ACTIVATION_TOKEN`、`INVALID_RESET_TOKEN`、`SUPPLIER_PORTAL_DISABLED`、`MISSING_BEARER`、`INVALID_ACCESS_TOKEN`、`INVALID_REFRESH_TOKEN` |
| `PermissionDeniedError` | 403 | `PERMISSION_DENIED`、`CANNOT_DEACTIVATE_SELF`、`CANNOT_CHANGE_OWN_ROLE` |
| `DomainError` (其他) | 422 | `USERNAME_TAKEN`、`ROLE_NOT_FOUND`、`USER_NOT_FOUND`、`USERNAME_TOO_SHORT`、`BOOTSTRAP_FAILED`、`NOT_IMPLEMENTED_IN_T03` |
| 未捕获异常 | 500 | 不暴露堆栈/SQL/密钥 |

## 7. 登录失败限流

- 连续 `max_login_failures`（默认 5）次错误密码 → 账号锁定 `login_lockout_seconds`（默认 900s）。
- 锁定期间正确密码也返回 `ACCOUNT_LOCKED`；`details.remaining_seconds` 给前端做倒计时。
- 限流状态在内存；T04 迁 Redis。

## 8. 审计

`auth_events` 表记录以下事件，写入时**不**含明文密码/token 原文：

| 事件 | 触发 | payload 示例 |
| --- | --- | --- |
| `LOGIN_SUCCESS` | 登录成功 | `{username, role}` |
| `LOGIN_FAILED` | 登录失败 | `{username, reason}`（reason ∈ `no_such_user / bad_password / inactive / pending_activation / locked / supplier_portal_disabled`） |
| `ACTIVATION_CONSUMED` | 首次激活成功 | `{username}` |
| `PASSWORD_CHANGED` | 改密 | `{username}` |
| `PASSWORD_RESET_ISSUED` | 管理员发重置凭据 | `{target_username, action: create_account / reactivate}` |
| `PASSWORD_RESET_CONSUMED` | 用户消费重置凭据 | `{username}` |
| `TOKEN_REVOKED` | 角色变更/停用触发踢 token | `{action, username, new_role?}` |
| `LOGOUT` | 用户主动登出 | `{jti}` |
| `ACCOUNT_LOCKED` | 登录失败达上限 | `{username}` |

通用业务审计（合同/商家/巡访等）由 T04 `AuditService` 接管。
