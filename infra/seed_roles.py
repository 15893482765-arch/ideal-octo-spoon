"""一次性脚本：为生产数据集补齐 6 类受控角色（PRD §2.2）。

背景：bootstrap_initial_admin 只创建 SYSTEM_ADMIN 角色；其余 5 类角色原本
只由 demo_account_seeder 在 DEMO 数据集里播种，而生产环境 DEMO_DATA_ENABLED=false，
导致 roles 表只有一条记录，新建账号的角色下拉只能选到「系统管理员」。

本脚本幂等：已存在则跳过。角色挂在现有 SYSTEM_ADMIN 所在的 (dataset_type, dataset_id)。
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.session import session_scope
from app.models import Role
from app.models.enums import RoleCode

NAMES = {
    RoleCode.SALES_REP.value: "业务员",
    RoleCode.REGIONAL_MANAGER.value: "片区经理",
    RoleCode.SUPERVISOR.value: "督导",
    RoleCode.FINANCE.value: "财务",
    RoleCode.SYSTEM_ADMIN.value: "系统管理员",
    RoleCode.SUPPLIER.value: "厂商",
}


def main() -> None:
    with session_scope() as s:
        base = s.execute(
            select(Role).where(Role.role_code == RoleCode.SYSTEM_ADMIN.value).limit(1)
        ).scalar_one_or_none()
        if base is None:
            raise SystemExit("未找到 SYSTEM_ADMIN 角色，请先完成首次引导")

        created: list[str] = []
        for rc in RoleCode:
            exists = s.execute(
                select(Role)
                .where(Role.role_code == rc.value)
                .where(Role.dataset_type == base.dataset_type)
                .where(Role.dataset_id == base.dataset_id)
                .limit(1)
            ).scalar_one_or_none()
            if exists is not None:
                continue
            s.add(
                Role(
                    code=rc.value,
                    name=NAMES[rc.value],
                    role_code=rc.value,
                    dataset_type=base.dataset_type,
                    dataset_id=base.dataset_id,
                )
            )
            created.append(rc.value)
        print("dataset:", base.dataset_type, base.dataset_id)
        print("created:", created or "(none, already complete)")

    # 结果回显
    with session_scope() as s:
        rows = s.execute(select(Role).order_by(Role.role_code)).scalars().all()
        for r in rows:
            print(f"  {r.role_code:18} {r.name}")


if __name__ == "__main__":
    main()
