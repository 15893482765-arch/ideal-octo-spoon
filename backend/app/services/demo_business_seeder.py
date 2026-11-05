"""演示业务数据播种：把主数据串成一条可走通的业务链（DEMO 数据域）。

一次执行覆盖 12 个业务域，便于在界面上按业务流逐段查看：

| 段 | 覆盖状态 |
|---|---|
| 合同 | 草稿 / 待审批 / 已退回 / 生效（4 份） |
| 冰柜 | 在库 / 已预留（投放单）/ 已投放 / 待撤回 / 待验收 / 维修中 |
| 排班 | 本周已发布 / 下周草稿 |
| 巡访 | 待打卡 / 有效提报 / 异常待判定 |
| 补货 | 草稿 / 待确认 / 已确认 / 部分履约 / 已关闭 / 已取消 |
| 地推 | 草稿 / 已发布 / 已复盘（含样品台账） |
| 督导 | 计划 / 已跟访 / 整改闭环 |
| 采购 | 已确认 / 已收货（含供货确认） |
| 销售确认 | 近 3 个月已确认销量（报表与绩效的事实来源） |
| 绩效 | 规则版本 ACTIVE / 快照 DRAFT 与 REVIEWED |
| 风险 | RiskService 扫描产物 |
| 待办 | 各类 TaskItem 样本 |

口径与约束（重要）：

- 所有行落 **DEMO 数据域**（与演示账号同一 dataset），演示账号登录即可见；
  跨域混用（DEMO 行挂 PRODUCTION 上游）会被 `assert_same_dataset` 拒绝；
- **幂等**：每段以确定性编码/日期为锚点，已存在即跳过，可反复执行；
- **每段独立 SAVEPOINT**：单段失败不阻断其它段，返回值逐段汇报，便于定位；
- 采购链在服务层拒绝 DEMO 上游事实（PRD R33/R34），该段按服务落账口径
  直接写库（头金额 = 行金额之和），不复用被拒绝的 Service 路径。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import ActorContext
from app.models import (
    Activity,
    Brand,
    Contract,
    FileObject,
    FreezerAsset,
    Merchant,
    MerchantAssignment,
    PayrollStatement,
    PurchaseOrder,
    PurchaseOrderLine,
    Rectification,
    Region,
    ReplenishmentOrder,
    RiskItem,
    Role,
    SalesConfirmation,
    SchedulePlanBatch,
    Sku,
    SupervisionVisit,
    Supplier,
    SupplierAuthorization,
    SupplyConfirmation,
    User,
    UserRegionBrandAuth,
    VisitRecord,
    VisitTask,
)
from app.models.enums import (
    MerchantStatus,
    PurchaseOrderStatus,
    ReplenishmentStatus,
    VisitConclusion,
)
from app.schemas.contract import ContractDecisionRequest, ContractDraftRequest
from app.schemas.freezer import (
    FreezerDeployConfirmRequest,
    FreezerDeployRequest,
    FreezerPickupConfirmRequest,
    FreezerPurchaseRequest,
    FreezerRecallDecisionRequest,
    FreezerRecallRequestCreate,
    FreezerRepairRequest,
)
from app.schemas.visit import (
    VisitPhotoInput,
    VisitReplenishmentLineInput,
    VisitSalesLineInput,
    VisitSubmit,
)
from app.services import demo_dataset as ds
from app.services.activity_service import ActivityService
from app.services.contract_service import ContractService
from app.services.freezer_service import FreezerService
from app.services.master_data.fixture_loader import DemoFixtureLoader
from app.services.performance_snapshot_service import PerformanceSnapshotService
from app.services.replenishment_service import ReplenishmentService
from app.services.schedule_service import ScheduleService, monday_of
from app.services.supervision_service import SupervisionService
from app.services.task_service import (
    TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
    TASK_KIND_OTHER,
    TASK_KIND_VISIT_PLANNED,
    TaskService,
)
from app.services.visit_service import VisitService

TZ = ZoneInfo("Asia/Shanghai")

# 演示数据的确定性编码前缀（幂等锚点，勿随意改动）
CONTRACT_PREFIX = "HT-DEMO-"
FREEZER_PREFIX = "FZ-DEMO-"
REPL_PREFIX = "RD-DEMO-"
PO_PREFIX = "PO-DEMO-"
SUPPLIER_CODE = "SUP-DEMO-01"
RULE_CODE = "RULE-DEMO"
CAMPAIGN_TAG = "演示地推"

# 每段最多汇报的明细条数
_DETAIL_LIMIT = 6


class DemoBusinessSeeder:
    """把 DEMO 数据域串成完整业务流程（幂等，可重复执行）。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.brand: Brand | None = None
        self.regions: dict[str, Region] = {}
        self.users: dict[str, User] = {}
        self.role_of: dict[str, str] = {}
        self.auths: dict[UUID, list[UserRegionBrandAuth]] = {}
        self._sku_cache: list[Sku] = []

    # ------------------------------------------------------------------ 入口
    def seed(self) -> list[dict[str, str]]:
        self._load_context()
        self._ensure_base_data()
        steps: list[tuple[str, object]] = [
            ("矿泉水SKU", self._ensure_mineral_sku),
            ("合同", self._seed_contracts),
            ("冰柜", self._seed_freezers),
            ("排班", self._seed_schedules),
            ("巡访", self._seed_visits),
            ("补货", self._seed_replenishments),
            ("地推活动", self._seed_activities),
            ("督导跟访", self._seed_supervision),
            ("采购供货", self._seed_procurement),
            ("销售确认", self._seed_sales_confirmations),
            ("绩效快照", self._seed_performance),
            ("工资单", self._seed_payroll),
            ("风险扫描", self._seed_risks),
            ("待办中心", self._seed_tasks),
        ]
        results: list[dict[str, str]] = []
        for name, fn in steps:
            nested = self.session.begin_nested()
            try:
                detail = fn()  # type: ignore[operator]
                nested.commit()
                results.append({"phase": name, "status": "OK", "detail": detail})
            except Exception as exc:  # noqa: BLE001 —— 单段失败不影响其它段
                nested.rollback()
                results.append(
                    {
                        "phase": name,
                        "status": "FAIL",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
        return results

    # ------------------------------------------------------------ 上下文/工具
    def _ensure_base_data(self) -> None:
        """空库兜底：没有商家时先跑一次完整初始化夹具（主数据+商家+合同+冰柜）。

        正常演示库里 40 个商家已在（由 `db:demo-accounts` + 夹具导入建立），此处
        直接跳过；只有全新库才会用到，避免 `demo-business` 因缺商家而整体失败。
        """
        if self._exists(Merchant, Merchant.dataset_id == ds.DEMO_DATASET_UUID):
            return
        fixture = (
            Path(__file__).resolve().parents[3]
            / "沁爽饮料-初始化夹具与补充演示数据.xlsx"
        )
        if not fixture.is_file():
            raise RuntimeError(
                "演示库缺少商家且未找到初始化夹具工作簿，请先导入主数据与商家"
            )
        actor = self.users.get("zhaozhiqiang04") or next(iter(self.users.values()))
        DemoFixtureLoader(self.session).load(path=str(fixture), actor_user_id=actor.id)

    def _load_context(self) -> None:
        s = self.session
        self.brand = s.execute(
            select(Brand).where(Brand.code == ds.QINSHUANG_BRAND_CODE)
        ).scalars().first()
        if self.brand is None:
            raise RuntimeError("演示品牌不存在，请先执行 `python -m app.cli.db demo-accounts`")
        self.regions = {
            r.code: r
            for r in s.execute(
                select(Region).where(Region.dataset_id == ds.DEMO_DATASET_UUID)
            ).scalars()
        }
        self.users = {
            u.username: u
            for u in s.execute(
                select(User).where(User.dataset_id == ds.DEMO_DATASET_UUID)
            ).scalars()
        }
        roles = {r.id: r.role_code for r in s.execute(select(Role)).scalars()}
        self.role_of = {name: roles.get(u.role_id, "") for name, u in self.users.items()}
        self.auths = {}
        for auth in s.execute(select(UserRegionBrandAuth)).scalars():
            self.auths.setdefault(auth.user_id, []).append(auth)

    def _actor(self, username: str) -> ActorContext:
        user = self.users[username]
        auths = self.auths.get(user.id, [])
        return ActorContext(
            user_id=user.id,
            role_code=self.role_of[username],
            region_ids=frozenset(a.region_id for a in auths),
            brand_ids=frozenset(a.brand_id for a in auths),
        )

    def _role_user(self, role_code: str) -> User:
        for name, code in self.role_of.items():
            if code == role_code:
                return self.users[name]
        raise RuntimeError(f"演示账号缺少角色 {role_code}")

    def _ensure_mineral_sku(self) -> str:
        """幂等补建「沁爽矿泉水」QS-SKU-035。

        采购可选行原先只来自「已确认未满足的补货需求」，矿泉水没有补货单就永远
        选不到；这里补一条主数据，配合采购页的「手工加行」即可直接下采购。
        """
        existing = self.session.execute(
            select(Sku).where(Sku.code == ds.MINERAL_SKU_CODE).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return f"{ds.MINERAL_SKU_CODE} 已存在"
        self.session.add(
            Sku(
                code=ds.MINERAL_SKU_CODE,
                name=ds.MINERAL_SKU_NAME,
                brand_id=self.brand.id,
                capacity_ml=550,
                case_spec=Decimal("24"),
                suggested_price_fen=200,
                purchase_price_fen=120,
                spec_text="550ml/瓶×24瓶",
                series_name="饮用水系列",
                category_name="饮用水",
                dataset_type=ds.DEMO_DATASET_TYPE,
                dataset_id=ds.DEMO_DATASET_UUID,
            )
        )
        self.session.flush()
        self._sku_cache = []
        return f"新增 {ds.MINERAL_SKU_CODE} {ds.MINERAL_SKU_NAME}"

    def _skus(self, limit: int = 4) -> list[Sku]:
        if not self._sku_cache:
            self._sku_cache = list(
                self.session.execute(
                    select(Sku)
                    .where(Sku.dataset_id == ds.DEMO_DATASET_UUID)
                    .order_by(Sku.code)
                    .limit(12)
                ).scalars()
            )
        return self._sku_cache[:limit]

    def _merchant(self, code: str) -> Merchant:
        merchant = self.session.execute(
            select(Merchant).where(
                Merchant.code == code, Merchant.dataset_id == ds.DEMO_DATASET_UUID
            )
        ).scalars().first()
        if merchant is None:
            raise RuntimeError(f"演示商家 {code} 不存在")
        return merchant

    def _owner_of(self, merchant_id: UUID) -> UUID | None:
        """商家主责业务员（排班/巡访要求条目负责人 = 主责人）。"""
        row = self.session.execute(
            select(MerchantAssignment.owner_user_id)
            .where(MerchantAssignment.merchant_id == merchant_id)
            .order_by(MerchantAssignment.created_at.desc())
            .limit(1)
        ).first()
        return row[0] if row else None

    def _merchants_in(self, region_code: str, limit: int = 6) -> list[Merchant]:
        """按片区取在用商家（补货/排班的候选池，不受"是否本人主责"限制）。"""
        region = self.regions[region_code]
        return list(
            self.session.execute(
                select(Merchant)
                .where(
                    Merchant.region_id == region.id,
                    Merchant.dataset_id == ds.DEMO_DATASET_UUID,
                    Merchant.status == MerchantStatus.ACTIVE.value,
                )
                .order_by(Merchant.code)
                .limit(limit)
            ).scalars()
        )

    def _field_actor_for(self, merchant_id: UUID) -> ActorContext:
        """现场动作的操作人：优先商家主责业务员（行级可见性要求本人负责商家）。"""
        owner = self._owner_of(merchant_id)
        if owner is not None:
            try:
                return self._actor_of(owner)
            except RuntimeError:
                pass
        return self._actor("zhangwei01")

    def _actor_of(self, user_id: UUID) -> ActorContext:
        for name, user in self.users.items():
            if user.id == user_id:
                return self._actor(name)
        raise RuntimeError(f"账号 {user_id} 不在演示数据域")

    def _merchants_of(self, username: str, limit: int = 6) -> list[Merchant]:
        user = self.users[username]
        rows = self.session.execute(
            select(Merchant)
            .join(MerchantAssignment, MerchantAssignment.merchant_id == Merchant.id)
            .where(
                MerchantAssignment.owner_user_id == user.id,
                Merchant.dataset_id == ds.DEMO_DATASET_UUID,
                Merchant.status == MerchantStatus.ACTIVE.value,
            )
            .order_by(Merchant.code)
            .limit(limit)
        ).scalars()
        return list(rows)

    @staticmethod
    def _at(day: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=TZ).astimezone(UTC)

    def _exists(self, model: object, *conditions: object) -> bool:
        return (
            self.session.execute(select(model).where(*conditions).limit(1)).first()  # type: ignore[arg-type]
            is not None
        )

    # ---------------------------------------------------------------- 1 合同
    def _seed_contracts(self) -> str:
        if self._exists(Contract, Contract.contract_no.like(f"{CONTRACT_PREFIX}%")):
            return "已存在，跳过"

        rep = self._actor("zhangwei01")
        mgr = self._actor("gaojianmin01")
        svc = ContractService(self.session)

        tickets = [
            ("001", "SH103", "APPROVE"),
            ("002", "SH104", "APPROVE"),
            ("003", "SH105", "APPROVE"),
            ("004", "SH106", "APPROVE"),
            ("005", "SH101", "DRAFT"),
            ("006", "SH102", "SUBMIT"),
            ("007", "SH107", "RETURN"),
        ]
        made: list[str] = []
        for suffix, merchant_code, mode in tickets:
            merchant = self._merchant(merchant_code)
            contract = svc.create_draft(
                rep,
                ContractDraftRequest(
                    merchant_id=merchant.id,
                    contract_no=f"{CONTRACT_PREFIX}{suffix}",
                    title=f"陈列合作协议（{merchant.name}）",
                    party_name=merchant.name,
                    template_version="v1",
                    effective_from=date(2026, 1, 1),
                    expire_at=date(2026, 12, 31),
                    commission_rate=3.5,
                    display_threshold_pct=50,
                    commission_rule_version="comm-v2026-01",
                    reason="演示数据：合同起草",
                ),
            )
            if mode == "DRAFT":
                made.append(f"{contract.contract_no}:DRAFT")
                continue
            svc.submit(rep, contract.id, "提交合同审批")
            if mode == "SUBMIT":
                made.append(f"{contract.contract_no}:PENDING_APPROVAL")
                continue
            decision = "APPROVE" if mode == "APPROVE" else "RETURN"
            svc.decide(
                mgr,
                contract.id,
                ContractDecisionRequest(
                    decision=decision,
                    comment="同意签署" if decision == "APPROVE" else "请补充陈列面积与账期条款",
                ),
            )
            made.append(f"{contract.contract_no}:{contract.status}")
        return "；".join(made[:_DETAIL_LIMIT]) + f"（共 {len(made)} 份）"

    # ---------------------------------------------------------------- 2 冰柜
    def _seed_freezers(self) -> str:
        if self._exists(FreezerAsset, FreezerAsset.asset_code.like(f"{FREEZER_PREFIX}%")):
            return "已存在，跳过"
        assert self.brand is not None

        fin = self._actor("liumeling03")
        mgr = self._actor("gaojianmin01")
        svc = FreezerService(self.session)

        def purchase(code: str, price: int) -> FreezerAsset:
            return svc.purchase(
                fin,
                FreezerPurchaseRequest(
                    asset_code=code,
                    brand_id=self.brand.id,
                    model="QS-580L",
                    purchase_date=date(2026, 9, 20),
                    purchase_price_fen=price,
                    deposit_fen=50_000,
                    note="演示数据：财务购置入库",
                ),
            )

        def effective_contract(contract_no: str) -> Contract:
            return self.session.execute(
                select(Contract).where(Contract.contract_no == contract_no)
            ).scalars().one()

        made: list[str] = []

        # 在库池（可投放）
        for code in (f"{FREEZER_PREFIX}01", f"{FREEZER_PREFIX}02", f"{FREEZER_PREFIX}03"):
            purchase(code, 350_000)
        made.append("在库 3 台")

        # 已预留（投放单已建，待现场确认）
        reserved = purchase(f"{FREEZER_PREFIX}04", 360_000)
        svc.create_deployment(
            mgr,
            reserved.id,
            FreezerDeployRequest(
                contract_id=effective_contract(f"{CONTRACT_PREFIX}001").id,
                deposit_fen=50_000,
                note="演示数据：投放单（待现场确认）",
            ),
        )
        made.append("已预留 1 台")

        # 已投放（含押金收缴）
        deployed = purchase(f"{FREEZER_PREFIX}05", 360_000)
        contract_2 = effective_contract(f"{CONTRACT_PREFIX}002")
        svc.create_deployment(
            mgr, deployed.id, FreezerDeployRequest(contract_id=contract_2.id)
        )
        svc.confirm_deployment(
            self._field_actor_for(contract_2.merchant_id),
            deployed.id,
            FreezerDeployConfirmRequest(note="演示数据：现场投放完成"),
        )
        made.append("已投放 1 台")

        # 待撤回（商家主责业务员申请撤回 → 片区经理审批通过）
        recall = purchase(f"{FREEZER_PREFIX}06", 380_000)
        contract_3 = effective_contract(f"{CONTRACT_PREFIX}003")
        rep_recall = self._field_actor_for(contract_3.merchant_id)
        svc.create_deployment(
            mgr, recall.id, FreezerDeployRequest(contract_id=contract_3.id)
        )
        svc.confirm_deployment(
            rep_recall,
            recall.id,
            FreezerDeployConfirmRequest(note="演示数据：现场投放完成"),
        )
        svc.request_recall(
            rep_recall,
            recall.id,
            FreezerRecallRequestCreate(
                planned_pickup_date=date(2026, 10, 26),
                reason="商家门头改造，冰柜需撤场",
            ),
        )
        svc.decide_recall(
            mgr,
            recall.id,
            FreezerRecallDecisionRequest(
                decision="APPROVE", note="同意撤柜，按计划日期回收"
            ),
        )
        made.append("待撤回 1 台（含撤回申请/审批流水）")

        # 待验收（现场已回收）
        picked = purchase(f"{FREEZER_PREFIX}07", 380_000)
        contract_4 = effective_contract(f"{CONTRACT_PREFIX}004")
        rep_pickup = self._field_actor_for(contract_4.merchant_id)
        svc.create_deployment(
            mgr, picked.id, FreezerDeployRequest(contract_id=contract_4.id)
        )
        svc.confirm_deployment(
            rep_pickup,
            picked.id,
            FreezerDeployConfirmRequest(note="演示数据：现场投放完成"),
        )
        # 现场回收必须先进入「待撤回」：走撤回申请 → 审批 → 回收确认
        svc.request_recall(
            rep_pickup,
            picked.id,
            FreezerRecallRequestCreate(
                planned_pickup_date=date(2026, 10, 22),
                reason="合同到期不再续签，安排撤柜",
            ),
        )
        svc.decide_recall(
            mgr,
            picked.id,
            FreezerRecallDecisionRequest(decision="APPROVE", note="同意撤柜"),
        )
        svc.confirm_pickup(
            rep_pickup, picked.id, FreezerPickupConfirmRequest(note="演示数据：现场回收")
        )
        made.append("待验收 1 台")

        # 维修中
        repairing = purchase(f"{FREEZER_PREFIX}08", 340_000)
        svc.start_repair(
            mgr,
            repairing.id,
            FreezerRepairRequest(note="演示数据：压缩机制冷异常，送修"),
        )
        made.append("维修中 1 台")

        return f"共 8 台：{'；'.join(made)}"

    # ---------------------------------------------------------------- 3 排班
    def _seed_schedules(self) -> str:
        mgr = self._actor("gaojianmin01")
        region = self.regions["DEMO-JS"]
        this_monday = monday_of(date.today())
        next_monday = this_monday + timedelta(days=7)

        if self._exists(
            SchedulePlanBatch,
            SchedulePlanBatch.region_id == region.id,
            SchedulePlanBatch.week_start_date == this_monday,
        ):
            return "本周批次已存在，跳过"

        rep_name = "zhangwei01"
        merchants = self._merchants_in("DEMO-JS", limit=5)
        if not merchants:
            raise RuntimeError("DEMO-JS 没有在用商家，无法排班")

        def entries_for(week_start: date, take: int) -> list[dict]:
            out = []
            for idx, merchant in enumerate(merchants[:take]):
                owner = self._owner_of(merchant.id) or self.users[rep_name].id
                day = week_start + timedelta(days=idx)
                out.append(
                    {
                        "merchant_id": merchant.id,
                        "assigned_user_id": owner,
                        "planned_date": day,
                        "start_at": self._at(day, 9 + idx),
                        "end_at": self._at(day, 9 + idx, 30),
                        "priority": 2,
                    }
                )
            return out

        svc = ScheduleService(self.session)
        this_entries = entries_for(this_monday, 4)
        published = svc.create_draft(
            mgr,
            region_id=region.id,
            week_start=this_monday,
            entries=this_entries,
            idempotency_key="demo-schedule-this-week",
        )
        svc.publish(mgr, batch_id=published.id)

        next_entries = entries_for(next_monday, 3)
        draft = svc.create_draft(
            mgr,
            region_id=region.id,
            week_start=next_monday,
            entries=next_entries,
            idempotency_key="demo-schedule-next-week",
        )
        return (
            f"本周（{this_monday}）已发布 → 生成巡访待办 {len(this_entries)} 条；"
            f"下周（{next_monday}）草稿批次 {draft.id.hex[:8]}（{len(next_entries)} 条待发布）"
        )

    # ---------------------------------------------------------------- 4 巡访
    def _seed_visits(self) -> str:
        tasks = list(
            self.session.execute(
                select(VisitTask).order_by(VisitTask.planned_date).limit(8)
            ).scalars()
        )
        if not tasks:
            raise RuntimeError("没有可提报的巡访待办（请先执行排班段）")

        if self._exists(VisitRecord, VisitRecord.dataset_id == ds.DEMO_DATASET_UUID):
            return "巡访提报样本已存在，跳过"

        pending = [t for t in tasks if t.status == "PENDING"]
        if len(pending) < 2:
            raise RuntimeError("待打卡巡访任务不足 2 条（请先执行排班段并发布）")

        skus = self._skus(2)
        if not skus:
            raise RuntimeError("缺 SKU，无法造巡访销量行")

        svc = VisitService(self.session)
        made: list[str] = []

        # 4-1 有效提报（近点 + 照片 + 陈列达标 + 销量 + 补货需求）
        task_ok = pending[0]
        rep = self._actor_of(task_ok.assigned_user_id)
        merchant = self.session.get(Merchant, task_ok.merchant_id)
        assert merchant is not None
        lng, lat = self._merchant_point(merchant)
        photo = self._make_photo(task_ok.assigned_user_id)
        record = svc.submit(
            rep,
            task_ok.id,
            VisitSubmit(
                lng=lng,
                lat=lat + 0.0006,
                accuracy_meters=8,
                display_pct=65,
                conclusion=VisitConclusion.NEEDS_REPLENISHMENT,
                note="演示数据：门店陈列达标，冰柜库存偏低",
                photos=[VisitPhotoInput(file_id=photo.id, photo_kind="STOREFRONT")],
                sales_lines=[
                    VisitSalesLineInput(
                        sku_id=skus[0].id, reported_boxes=Decimal("12.5"), out_of_stock=False
                    )
                ],
                replenishment_lines=[
                    VisitReplenishmentLineInput(
                        sku_id=skus[0].id,
                        requested_boxes=Decimal("8"),
                        expected_delivery_date=date.today() + timedelta(days=3),
                    )
                ],
                idempotency_key="demo-visit-valid-01",
            ),
        )
        made.append(f"{merchant.name}:{record.validity}")

        # 4-2 异常提报（远离门店 → 待督导判定）
        task_bad = pending[1]
        merchant_bad = self.session.get(Merchant, task_bad.merchant_id)
        assert merchant_bad is not None
        lng_bad, lat_bad = self._merchant_point(merchant_bad)
        rep_bad = self._actor_of(task_bad.assigned_user_id)
        photo_bad = self._make_photo(task_bad.assigned_user_id)
        bad = svc.submit(
            rep_bad,
            task_bad.id,
            VisitSubmit(
                lng=lng_bad + 0.004,
                lat=lat_bad + 0.004,
                accuracy_meters=30,
                display_pct=40,
                conclusion=VisitConclusion.NORMAL,
                note="演示数据：定位偏离门店，待督导判定",
                photos=[VisitPhotoInput(file_id=photo_bad.id, photo_kind="STOREFRONT")],
                idempotency_key="demo-visit-abnormal-01",
            ),
        )
        made.append(f"{merchant_bad.name}:{bad.validity}")

        return f"提报 {len(made)} 条（{'；'.join(made)}）；其余 {len(pending) - 2} 条保持待打卡"

    @staticmethod
    def _merchant_point(merchant: Merchant) -> tuple[float, float]:
        """取商家坐标；无坐标时回退到上海市中心（演示数据）。"""
        try:
            from geoalchemy2.shape import to_shape

            if merchant.location is not None:
                point = to_shape(merchant.location)
                return float(point.x), float(point.y)
        except Exception:  # noqa: BLE001
            pass
        return 121.4737, 31.2304

    def _make_photo(self, owner_id: UUID) -> FileObject:
        from uuid import uuid4

        photo = FileObject(
            object_key=f"PHOTO/{uuid4().hex}/storefront.jpg",
            original_filename="storefront.jpg",
            content_type="image/jpeg",
            size_bytes=204_800,
            sha256=uuid4().hex * 2,
            biz_type="PHOTO",
            owner_user_id=owner_id,
            scan_status="CLEAN",
            dataset_type=ds.DEMO_DATASET_TYPE,
            dataset_id=ds.DEMO_DATASET_UUID,
        )
        self.session.add(photo)
        self.session.flush()
        return photo

    # ---------------------------------------------------------------- 5 补货
    def _seed_replenishments(self) -> str:
        if self._exists(
            ReplenishmentOrder, ReplenishmentOrder.code.like(f"{REPL_PREFIX}%")
        ):
            return "已存在，跳过"

        rep = self._actor("zhangwei01")
        mgr = self._actor("gaojianmin01")
        merchants = self._merchants_in("DEMO-JS", limit=6)
        skus = self._skus(4)
        if len(merchants) < 4 or len(skus) < 2:
            raise RuntimeError(
                f"商家或 SKU 不足（商家 {len(merchants)} / SKU {len(skus)}），无法造补货单"
            )

        svc = ReplenishmentService(self.session)

        def create(code: str, merchant: Merchant, sku: Sku, boxes: Decimal) -> ReplenishmentOrder:
            order = ReplenishmentOrder(
                code=code,
                merchant_id=merchant.id,
                sku_id=sku.id,
                requested_boxes=boxes,
                committed_boxes=Decimal("0"),
                fulfilled_boxes=Decimal("0"),
                request_date=date.today(),
                status=ReplenishmentStatus.DRAFT.value,
                dataset_type=ds.DEMO_DATASET_TYPE,
                dataset_id=ds.DEMO_DATASET_UUID,
            )
            self.session.add(order)
            self.session.flush()
            # DRAFT 的归属校验依赖提交人主责关系，提交后再走后续状态
            svc.submit_order(rep, order_id=order.id)
            return order

        # 5-1 待确认（SUBMITTED）
        create(f"{REPL_PREFIX}001", merchants[0], skus[0], Decimal("10"))

        # 5-2 已确认（CONFIRMED）
        confirmed = create(
            f"{REPL_PREFIX}002", merchants[1], skus[0], Decimal("12")
        )
        svc.confirm(
            mgr,
            order_id=confirmed.id,
            confirmed_boxes=Decimal("12"),
            promised_delivery_date=date.today() + timedelta(days=2),
        )

        # 5-3 部分履约（PARTIALLY_FULFILLED）
        partial = create(f"{REPL_PREFIX}003", merchants[2], skus[1], Decimal("20"))
        svc.confirm(
            mgr,
            order_id=partial.id,
            confirmed_boxes=Decimal("20"),
            promised_delivery_date=date.today() + timedelta(days=1),
        )
        svc.register_fulfillment(
            mgr,
            order_id=partial.id,
            delivered_boxes=Decimal("12"),
            delivered_on=date.today(),
            idempotency_key="demo-repl-fulfill-01",
            note="演示数据：分批到货",
        )

        # 5-4 已关闭（履约完成 → 关单）
        closed = create(f"{REPL_PREFIX}004", merchants[3], skus[1], Decimal("8"))
        svc.confirm(
            mgr,
            order_id=closed.id,
            confirmed_boxes=Decimal("8"),
            promised_delivery_date=date.today(),
        )
        svc.register_fulfillment(
            mgr,
            order_id=closed.id,
            delivered_boxes=Decimal("8"),
            delivered_on=date.today(),
            idempotency_key="demo-repl-fulfill-02",
        )
        svc.close_order(mgr, order_id=closed.id, note="演示数据：履约完成关单")

        # 5-5 已取消
        cancelled = create(f"{REPL_PREFIX}005", merchants[0], skus[1], Decimal("5"))
        svc.cancel_order(mgr, order_id=cancelled.id, reason="演示数据：厂商缺货，取消需求")

        return "5 张补货单：待确认 / 已确认 / 部分履约 / 已关闭 / 已取消"

    # ---------------------------------------------------------------- 6 地推
    def _seed_activities(self) -> str:
        if self._exists(Activity, Activity.name.like(f"{CAMPAIGN_TAG}%")):
            return "已存在，跳过"

        mgr = self._actor("gaojianmin01")
        region = self.regions["DEMO-JS"]
        rep = self.users["zhangwei01"]
        rep2 = self.users.get("yangfan06") or rep
        sku = self._skus(1)[0]
        sku.purchase_price_fen = sku.purchase_price_fen or 10_000
        day = date.today() + timedelta(days=2)
        svc = ActivityService(self.session)

        # 6-1 已复盘（含样品台账：领用 10、消耗 6、归还 3、缺失 1）
        reviewed = svc.create_draft(
            mgr,
            region_id=region.id,
            name=f"{CAMPAIGN_TAG}-国贸广场",
            start_at=self._at(day, 14),
            end_at=self._at(day, 17),
            location="国贸广场中庭",
            owner_user_id=rep.id,
            participant_user_ids=[rep.id, rep2.id],
            budget_fen=300_000,
            notes="演示数据：周末试饮地推",
            samples=[{"sku_id": sku.id, "boxes": Decimal("10"), "note": "演示领用"}],
        )
        svc.publish(mgr, activity_id=reviewed.id)
        svc.review(
            mgr,
            activity_id=reviewed.id,
            sample_results=[
                {"sku_id": sku.id, "stage": "CONSUMED", "boxes": Decimal("6")},
                {"sku_id": sku.id, "stage": "RETURNED", "boxes": Decimal("3")},
                {"sku_id": sku.id, "stage": "MISSING", "boxes": Decimal("1")},
            ],
        )

        # 6-2 已发布（进行中）
        published = svc.create_draft(
            mgr,
            region_id=region.id,
            name=f"{CAMPAIGN_TAG}-滨江步道",
            start_at=self._at(day + timedelta(days=3), 9),
            end_at=self._at(day + timedelta(days=3), 12),
            location="滨江步道入口",
            owner_user_id=rep.id,
            participant_user_ids=[rep.id],
            budget_fen=150_000,
            notes="演示数据：晨间派样",
        )
        svc.publish(mgr, activity_id=published.id)

        # 6-3 草稿（待发布）
        svc.create_draft(
            mgr,
            region_id=region.id,
            name=f"{CAMPAIGN_TAG}-社区巡展",
            start_at=self._at(day + timedelta(days=7), 9),
            end_at=self._at(day + timedelta(days=7), 18),
            location="锦绣社区广场",
            owner_user_id=rep.id,
            participant_user_ids=[rep.id, rep2.id],
            budget_fen=200_000,
            notes="演示数据：待发布",
        )
        return "3 场：已复盘（含样品损耗）/ 已发布 / 草稿"

    # ---------------------------------------------------------------- 7 督导
    def _seed_supervision(self) -> str:
        sup = self._actor("chenxiaomei02")
        rep = self._actor("zhangwei01")
        region = self.regions["DEMO-JS"]
        merchants = self._merchants_of("zhangwei01", limit=4)
        if len(merchants) < 2:
            raise RuntimeError("商家不足，无法造督导跟访")

        svc = SupervisionService(self.session)
        if self._exists(
            SupervisionVisit, SupervisionVisit.dataset_id == ds.DEMO_DATASET_UUID
        ):
            return "已存在，跳过"

        rep_user = self.users["zhangwei01"]
        # 7-1 已完成跟访 + 整改闭环
        day = date.today() - timedelta(days=1)
        visit = svc.plan_visit(
            sup,
            region_id=region.id,
            shadowed_user_id=rep_user.id,
            merchant_id=merchants[0].id,
            planned_date=day,
        )
        svc.submit_visit(
            sup,
            visit_id=visit.id,
            action_score=88,
            photo_count=3,
            visited_at=self._at(day, 10),
            display_issue="端架陈列面不足",
            note="演示数据：跟访记录",
            issues=[
                {
                    "category": "DISPLAY",
                    "description": "端架陈列面不足 2 层",
                    "owner_user_id": rep_user.id,
                    "due_date": day + timedelta(days=3),
                }
            ],
        )
        rect = self.session.execute(
            select(Rectification).where(Rectification.supervision_visit_id == visit.id)
        ).scalars().first()
        if rect is not None:
            svc.start_rectification(rep, rectification_id=rect.id)
            svc.submit_rectification(rep, rectification_id=rect.id, note="已补齐陈列面")
            svc.verify_rectification(
                sup, rectification_id=rect.id, passed=True, note="现场复核合格"
            )
            svc.close_rectification(sup, rectification_id=rect.id, note="闭环")

        # 7-2 仅计划（待跟访）
        svc.plan_visit(
            sup,
            region_id=region.id,
            shadowed_user_id=rep_user.id,
            merchant_id=merchants[1].id,
            planned_date=date.today() + timedelta(days=1),
        )
        return "2 条：1 条已跟访并整改闭环 / 1 条待跟访"

    # ---------------------------------------------------------------- 8 采购
    def _seed_procurement(self) -> str:
        if self._exists(Supplier, Supplier.code == SUPPLIER_CODE):
            return "已存在，跳过"
        assert self.brand is not None

        region = self.regions["DEMO-JS"]
        supplier_user = self.users.get("sunjianjun01")
        if supplier_user is None:
            raise RuntimeError("缺少厂商演示账号 sunjianjun01")

        supplier = Supplier(
            code=SUPPLIER_CODE,
            name="沁爽饮品供应有限公司",
            contact_name="张采购",
            contact_phone="13800001111",
            dataset_type=ds.DEMO_DATASET_TYPE,
            dataset_id=ds.DEMO_DATASET_UUID,
        )
        self.session.add(supplier)
        self.session.flush()
        self.session.add(
            SupplierAuthorization(
                supplier_id=supplier.id,
                user_id=supplier_user.id,
                brand_id=self.brand.id,
                region_id=region.id,
                dataset_type=ds.DEMO_DATASET_TYPE,
                dataset_id=ds.DEMO_DATASET_UUID,
            )
        )

        mgr = self.users["gaojianmin01"]
        skus = self._skus(2)

        def case_amount(qty: Decimal, sku: Sku, unit_price_fen: int) -> int:
            """箱数 × 箱规 × 瓶价：与服务端 line_amount 同口径。"""
            return int(
                (qty * sku.bottles_per_case * Decimal(unit_price_fen)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )

        def make_order(
            code: str, status: str, lines: list[tuple[Sku, Decimal, int]]
        ) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
            total = sum(case_amount(qty, sku, price) for sku, qty, price in lines)
            order = PurchaseOrder(
                code=code,
                supplier_id=supplier.id,
                brand_id=self.brand.id,
                region_id=region.id,
                expected_date=date.today() + timedelta(days=5),
                total_amount_fen=total,
                status=status,
                created_by_id_internal=mgr.id,
                confirmed_at=date.today(),
                confirmed_by_id=mgr.id,
                received_at=date.today() if status == PurchaseOrderStatus.RECEIVED.value else None,
                note="演示数据：采购单",
                dataset_type=ds.DEMO_DATASET_TYPE,
                dataset_id=ds.DEMO_DATASET_UUID,
            )
            self.session.add(order)
            self.session.flush()
            rows: list[PurchaseOrderLine] = []
            for sku, qty, price in lines:
                line = PurchaseOrderLine(
                    order_id=order.id,
                    sku_id=sku.id,
                    quantity_cases=qty,
                    tax_inclusive_unit_price_fen=price,
                    line_amount_fen=case_amount(qty, sku, price),
                    source_confirmed_boxes=qty,
                    dataset_type=ds.DEMO_DATASET_TYPE,
                    dataset_id=ds.DEMO_DATASET_UUID,
                )
                self.session.add(line)
                rows.append(line)
            self.session.flush()
            return order, rows

        # 8-1 厂商已确认供货
        confirmed, confirmed_lines = make_order(
            f"{PO_PREFIX}001",
            PurchaseOrderStatus.SUPPLIER_CONFIRMED.value,
            [
                (skus[0], Decimal("30"), skus[0].purchase_price_fen or 0),
                (skus[1], Decimal("20"), skus[1].purchase_price_fen or 0),
            ],
        )
        # 已确认的头单必须**所有行**都有确认记录，否则收货后缺行永远无法确认（Q190）
        for line, qty, eta_days in (
            (confirmed_lines[0], Decimal("30"), 3),
            (confirmed_lines[1], Decimal("20"), 4),
        ):
            self.session.add(
                SupplyConfirmation(
                    order_id=confirmed.id,
                    order_line_id=line.id,
                    supplier_id=supplier.id,
                    supplier_user_id=supplier_user.id,
                    status="CONFIRMED",
                    confirmed_quantity_cases=qty,
                    expected_delivery_date=date.today() + timedelta(days=eta_days),
                    note="演示数据：厂商按期供货",
                    dataset_type=ds.DEMO_DATASET_TYPE,
                    dataset_id=ds.DEMO_DATASET_UUID,
                )
            )

        # 8-2 待供货（厂商可逐行确认；演示"确认供货/无法供货"入口）
        make_order(
            f"{PO_PREFIX}002",
            PurchaseOrderStatus.CONFIRMED.value,
            [(skus[0], Decimal("10"), skus[0].purchase_price_fen or 0)],
        )
        return "2 张采购单：厂商已确认（可收货）/ 待供货（厂商待确认）"

    # ------------------------------------------------------------ 9 销售确认
    def _seed_sales_confirmations(self) -> str:
        confirmer = self.users["gaojianmin01"]
        count = 0
        if not self._exists(
            SalesConfirmation, SalesConfirmation.dataset_id == ds.DEMO_DATASET_UUID
        ):
            merchants = self._merchants_of("zhangwei01", limit=4)
            skus = self._skus(3)
            if not merchants or not skus:
                raise RuntimeError("商家或 SKU 不足，无法造销售确认")

            months = [(2026, 8), (2026, 9), (2026, 10)]
            for year, month in months:
                for idx, merchant in enumerate(merchants):
                    sku = skus[idx % len(skus)]
                    boxes = Decimal("10") + Decimal(idx * 3)
                    # T36：收入按建议零售价、成本按参考进价，毛利 = 收入 − 成本；
                    # 两个价格都是元/瓶，箱数需先乘箱规换算成瓶数
                    cases = boxes * sku.bottles_per_case
                    amount_fen = int(
                        (cases * sku.suggested_price_fen).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                    )
                    cost_fen = int(
                        (cases * (sku.purchase_price_fen or 0)).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                    )
                    self.session.add(
                        SalesConfirmation(
                            merchant_id=merchant.id,
                            sku_id=sku.id,
                            biz_date=date(year, month, min(20, 10 + idx)),
                            confirmed_boxes=boxes,
                            amount_fen=amount_fen,
                            cost_fen=cost_fen,
                            profit_fen=amount_fen - cost_fen,
                            confirmer_id=confirmer.id,
                            dataset_type=ds.DEMO_DATASET_TYPE,
                            dataset_id=ds.DEMO_DATASET_UUID,
                        )
                    )
                    count += 1
            self.session.flush()

        booked = self._book_sales_income()
        if count:
            return (
                f"{count} 条已确认销量（2026-08 ~ 2026-10，供报表与绩效取数），"
                f"同步登记 {booked} 笔销售收款流水"
            )
        return f"已存在，跳过（补登记 {booked} 笔销售收款流水）"

    def _book_sales_income(self) -> int:
        """为 DEMO 已确认销量补登记收入流水，保证总账收入与确认销售额同口径。

        T35 口径：收入只来自“销量确认即线下收款”。直插 SalesConfirmation 而
        不记账会让总账收入小于报表的确认销售额，因此这里按单据补记；
        幂等键 (SALES_CONFIRMATION, id, INCOME, SALES_REVENUE) 保证重跑不重复。
        """
        from app.models import FinanceTransaction
        from app.models.finance import SOURCE_INCOME_SALES
        from app.services.finance_ledger_service import TX_INCOME, FinanceLedgerService

        confirmations = list(
            self.session.execute(
                select(SalesConfirmation)
                .where(SalesConfirmation.dataset_id == ds.DEMO_DATASET_UUID)
                .order_by(SalesConfirmation.biz_date, SalesConfirmation.id)
            ).scalars()
        )
        booked_ids = set(
            self.session.execute(
                select(FinanceTransaction.related_object_id).where(
                    FinanceTransaction.dataset_id == ds.DEMO_DATASET_UUID,
                    FinanceTransaction.tx_type == TX_INCOME,
                    FinanceTransaction.source_kind == SOURCE_INCOME_SALES,
                    FinanceTransaction.related_object_type == "SALES_CONFIRMATION",
                )
            ).scalars()
        )
        actor = self._actor("gaojianmin01")
        service = FinanceLedgerService(self.session)
        booked = 0
        for confirmation in confirmations:
            if confirmation.id in booked_ids or confirmation.amount_fen == 0:
                continue
            service.record_income(
                actor,
                biz_date=confirmation.biz_date,
                amount_fen=confirmation.amount_fen,
                related_type="SALES_CONFIRMATION",
                related_id=confirmation.id,
                note="演示数据：销量确认收款",
                dataset_type=ds.DEMO_DATASET_TYPE,
                dataset_id=ds.DEMO_DATASET_UUID,
            )
            booked += 1
        return booked

    # ------------------------------------------------------------ 10 绩效快照
    def _seed_performance(self) -> str:
        mgr = self._actor("gaojianmin01")
        fin = self._actor("liumeling03")
        svc = PerformanceSnapshotService(self.session)

        rules = svc.list_rule_versions(mgr)
        if rules:
            return "规则/快照已存在，跳过"

        rule = svc.create_rule_version(
            mgr,
            params={
                "new_contract_award_fen": 5_000,
                "valid_visit_award_fen": 1_000,
                "activity_award_fen": 20_000,
                "sales_tiers": [(1_000_000, 200), (None, 300)],
            },
            effective_from=date(2026, 1, 1),
            name="演示绩效规则 v1",
            code=RULE_CODE,
        )
        svc.review_rule_version(fin, rule.id, note="演示数据：财务审核通过")

        rep_user = self.users["zhangwei01"]
        made: list[str] = []
        # 10-1 本月快照：保持 DRAFT（待经理确认，界面上可继续推进）
        this_month = f"{date.today().year}-{date.today().month:02d}"
        svc.freeze_month(mgr, user_id=rep_user.id, period_month=this_month)
        made.append(f"{this_month}:DRAFT")

        # 10-2 上月快照：走到 REVIEWED（工资/报表可用）
        first = date.today().replace(day=1)
        last_month_day = first - timedelta(days=1)
        last_month = f"{last_month_day.year}-{last_month_day.month:02d}"
        snapshot = svc.freeze_month(mgr, user_id=rep_user.id, period_month=last_month)
        svc.confirm_snapshot(mgr, snapshot.id)
        svc.review_snapshot(fin, snapshot.id)
        made.append(f"{last_month}:REVIEWED")
        return "规则 ACTIVE；快照：" + "、".join(made)

    # ---------------------------------------------------------------- 10.5 工资单
    # 演示底薪（分）：按角色定档，数值只为演示可查，不代表真实薪酬政策
    _PAYROLL_BASE_FEN = {
        "SALES_REP": 480_000,
        "SUPERVISOR": 650_000,
        "REGIONAL_MANAGER": 900_000,
        "FINANCE": 750_000,
    }

    def _seed_payroll(self) -> str:
        """近 6 个完整月（不含本月）的全员工资单，全部走完 生成→确认→复核→发放。

        经 PayrollService 受控入口逐月生成（绩效取自 REVIEWED 快照），保证
        数据与真实流转同构；已存在的 (员工, 月份) 跳过，可重复执行补齐。
        """
        from app.services.payroll_service import PayrollService

        mgr = self._actor("gaojianmin01")
        fin = self._actor("liumeling03")
        svc = PayrollService(self.session)

        # 最近 6 个完整月，倒序
        cursor = date.today().replace(day=1)
        months: list[str] = []
        for _ in range(6):
            prev = cursor - timedelta(days=1)
            months.append(f"{prev.year}-{prev.month:02d}")
            cursor = prev.replace(day=1)

        staff = [
            u
            for name, u in self.users.items()
            if self.role_of.get(name) in self._PAYROLL_BASE_FEN
        ]
        existing = {
            (str(st.user_id), st.period_month)
            for st in self.session.execute(select(PayrollStatement)).scalars()
        }

        created = 0
        for user in staff:
            base = self._PAYROLL_BASE_FEN[self.role_of[user.username]]
            for period in months:
                if (str(user.id), period) in existing:
                    continue
                st = svc.generate_statement(
                    fin,
                    user_id=user.id,
                    period_month=period,
                    base_salary_fen=base,
                )
                svc.confirm_statement(mgr, st.id)
                svc.review_statement(fin, st.id)
                year, month = int(period[:4]), int(period[5:])
                paid_on = (
                    date(year + 1, 1, 10) if month == 12 else date(year, month + 1, 10)
                )
                svc.mark_paid(fin, st.id, paid_on=paid_on)
                created += 1
        return (
            f"工资单：新增 {created} 张（{months[-1]} ~ {months[0]}，{len(staff)} 人），"
            f"已发放（次月 10 日发薪口径）"
        )

    # ---------------------------------------------------------------- 11 风险
    def _seed_risks(self) -> str:
        """风险池（工作台「风险提醒」与 RISK_HANDLING 待办的事实来源）。

        `RiskService.scan` 只接受 PRODUCTION 正式口径（DEMO 会被拒），而工作台
        `worklist` 又硬编码 `dataset_type == 'PRODUCTION'`；演示对象落在演示数据集，
        因此这里按同一表结构直接落风险行：`dataset_type` 取 PRODUCTION、`dataset_id`
        仍指向演示数据集（与合同表既有口径一致），行级裁剪按 dataset_id 反查商家范围，
        演示账号可正常看到。
        """
        iso = date.today().isocalendar()
        period = f"{iso.year}-W{iso.week:02d}"
        merchants = self._merchants_in("DEMO-JS", limit=3)
        contract = self.session.execute(
            select(Contract).where(Contract.contract_no == f"{CONTRACT_PREFIX}001")
        ).scalars().first()
        freezer = self.session.execute(
            select(FreezerAsset).where(FreezerAsset.asset_code == f"{FREEZER_PREFIX}08")
        ).scalars().first()

        targets: list[tuple[str, str, UUID | None, str, str]] = [
            ("RK-1", "MERCHANT", merchants[0].id if merchants else None, "NORMAL", "OPEN"),
            (
                "RK-3",
                "MERCHANT",
                merchants[1].id if len(merchants) > 1 else None,
                "HIGH",
                "OPEN",
            ),
            ("RK-6", "CONTRACT", contract.id if contract else None, "NORMAL", "OPEN"),
            ("RK-7", "FREEZER", freezer.id if freezer else None, "NORMAL", "ACKNOWLEDGED"),
        ]

        added = fixed = 0
        for rule, object_type, object_id, severity, status in targets:
            if object_id is None:
                continue
            row = self.session.execute(
                select(RiskItem).where(
                    RiskItem.object_type == object_type,
                    RiskItem.object_id == object_id,
                    RiskItem.rule_code == rule,
                    RiskItem.period == period,
                )
            ).scalars().first()
            if row is None:
                self.session.add(
                    RiskItem(
                        object_type=object_type,
                        object_id=object_id,
                        rule_code=rule,
                        period=period,
                        status=status,
                        severity=severity,
                        dataset_type=ds.PRODUCTION_DATASET_TYPE,
                        dataset_id=ds.DEMO_DATASET_UUID,
                    )
                )
                added += 1
            elif row.dataset_type != ds.PRODUCTION_DATASET_TYPE:
                row.dataset_type = ds.PRODUCTION_DATASET_TYPE
                fixed += 1
        self.session.flush()
        return f"风险池：新增 {added} 条、纠正口径 {fixed} 条（RK-1/RK-3/RK-6/RK-7）"

    # ---------------------------------------------------------------- 12 待办
    def _seed_tasks(self) -> str:
        tasks = TaskService(self.session)
        rep = self.users["zhangwei01"]
        mgr = self.users["gaojianmin01"]
        merchants = self._merchants_of("zhangwei01", limit=2)
        merchant_id = merchants[0].id if merchants else None

        created = 0
        samples = [
            (
                "演示待办：本周重点门店复访",
                rep.id,
                TASK_KIND_VISIT_PLANNED,
                "MERCHANT",
                merchant_id,
                "演示数据：巡访计划待执行",
            ),
            (
                "演示待办：合同到期续签评估",
                mgr.id,
                TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
                "CONTRACT",
                None,
                "演示数据：到期前 30 天续签评估",
            ),
            (
                "演示待办：季度陈列补贴核对",
                mgr.id,
                TASK_KIND_OTHER,
                "MERCHANT",
                merchant_id,
                "演示数据：补贴发放前核对",
            ),
        ]
        for title, assignee, kind, obj_type, obj_id, desc in samples:
            try:
                tasks.assign(
                    title=title,
                    assignee_id=assignee,
                    task_kind=kind,
                    related_object_type=obj_type,
                    related_object_id=obj_id,
                    description=desc,
                )
                created += 1
            except Exception:  # noqa: BLE001 —— 已存在同键 OPEN 待办即跳过
                continue
        return f"新增 {created} 条演示待办（业务动作自动生成的待办见各域）"
