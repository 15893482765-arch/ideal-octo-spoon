/** 路由守卫：无能力跳 /forbidden。 */
import { Navigate } from "react-router-dom";
import type { ReactNode } from "react";

import { useAuth } from "../../lib/auth";
import type { ModuleCode } from "../../lib/permissions";

export function RequireAbility({
  module,
  children,
}: {
  module: ModuleCode;
  children: ReactNode;
}) {
  const { hasAbility } = useAuth();
  if (!hasAbility(module)) {
    return <Navigate to="/forbidden" replace />;
  }
  return <>{children}</>;
}
