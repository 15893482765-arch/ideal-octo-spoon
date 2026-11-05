"""纸质合同正文模板（PRD §4.4 合同申请配套，T09 扩展）。

职责：
- 提供一页 A4 篇幅的《冰柜陈列合作协议》模板（``{{key}}`` 占位）；
- 创建/更新草稿时按合同字段渲染出正文，存入 ``contracts.body_text``；
- 商家店名、店长姓名、当前日期自动填入；字段缺失时留下划线，打印后可手填。

红线：本模块只做字符串渲染，不承载任何审批/放行裁决；正文内容属合同留痕，
不参与陈列达标、提成计算等业务判定。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

# 打印/手填占位：字段未填写时留下划线，纸质签署时可手写补齐
_BLANK = "＿＿＿＿＿＿"

CONTRACT_BODY_TEMPLATE = """冰柜陈列合作协议

合同编号：{{contract_no}}

甲方（供货方）：沁爽饮料有限公司
乙方（合作门店）：{{party_name}}
门店店长：{{store_manager_name}}

甲乙双方本着平等自愿、互惠互利的原则，就甲方冰柜在乙方门店的安置与
产品陈列合作事宜，达成如下协议：

一、合作内容
1. 甲方在乙方门店安置冰柜设备，用于甲方产品的陈列与冷藏销售。
2. 乙方负责冰柜的日常保管、清洁与正常用电，不得挪作他用或存放他方
   竞品。

二、陈列要求
1. 乙方保证甲方产品在冰柜内的陈列面占比不低于 {{display_threshold_pct}}%，
   并按甲方陈列标准摆放。
2. 甲方业务人员定期巡访核查陈列达标情况，乙方应予以配合。

三、费用与提成
1. 陈列补贴与提成按双方确认的提成规则（版本：{{commission_rule_version}}）
   执行；约定提成率为 {{commission_rate}}%。
2. 上述费用按规则约定周期结算。

四、合作期限
本协议自 {{effective_from}} 起至 {{expire_at}} 止。期满前双方可协商续约。

五、其他
1. 本协议未尽事宜，由双方友好协商解决。
2. 本协议一式两份，甲乙双方各执一份，自双方签字（盖章）之日起生效。


甲方（盖章）：沁爽饮料有限公司
甲方代表（签字）：＿＿＿＿＿＿

乙方（盖章）：{{party_name}}
门店店长（签字）：{{store_manager_name}}

签署日期：{{signed_date}}
"""


def _fmt_date(value: date | None) -> str:
    return value.isoformat() if value is not None else _BLANK


def _fmt_rate(value: Decimal | float | None) -> str:
    if value is None:
        return _BLANK
    text = f"{Decimal(str(value)).normalize():f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def render_contract_body(
    *,
    contract_no: str,
    party_name: str,
    store_manager_name: str | None,
    effective_from: date | None,
    expire_at: date | None,
    display_threshold_pct: int,
    commission_rate: Decimal | float | None,
    commission_rule_version: str | None,
    today: date | None = None,
) -> str:
    """按模板渲染一页 A4 合同正文。

    :param today: 签署日期预填值（默认取当前日期）；测试可注入固定日期。
    """
    signed = (today or date.today()).isoformat()
    values = {
        "contract_no": contract_no or _BLANK,
        "party_name": party_name or _BLANK,
        "store_manager_name": (store_manager_name or "").strip() or _BLANK,
        "effective_from": _fmt_date(effective_from),
        "expire_at": _fmt_date(expire_at),
        "display_threshold_pct": str(display_threshold_pct),
        "commission_rate": _fmt_rate(commission_rate),
        "commission_rule_version": (commission_rule_version or "").strip() or _BLANK,
        "signed_date": signed,
    }
    body = CONTRACT_BODY_TEMPLATE
    for key, value in values.items():
        body = body.replace("{{" + key + "}}", value)
    return body
