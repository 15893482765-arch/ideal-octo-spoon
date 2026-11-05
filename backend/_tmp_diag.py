"""诊断：真实 LLM 下「最近5个月的收入和支出」为何没进 Text2SQL。"""

import traceback

from sqlalchemy import select

from app.core.permissions import ActorContext
from app.db.session import session_scope
from app.models import Role, User, UserRegionBrandAuth
from app.services import assistant_chat_service as svc
from app.services.ai_llm import get_chat_llm

QUESTION = "最近5个月的收入和支出"

with session_scope() as session:
    for role_code in ("SYSTEM_ADMIN",):
        role = session.scalars(select(Role).where(Role.role_code == role_code)).first()
        if role is None:
            print(f"缺少角色 {role_code}")
            continue
        user = session.scalars(select(User).where(User.role_id == role.id)).first()
        if user is None:
            print(f"缺少 {role_code} 账号")
            continue
        auths = list(
            session.scalars(
                select(UserRegionBrandAuth).where(UserRegionBrandAuth.user_id == user.id)
            )
        )
        actor = ActorContext(
            user_id=user.id,
            role_code=role.role_code,
            region_ids=frozenset(a.region_id for a in auths),
            brand_ids=frozenset(a.brand_id for a in auths),
        )
        print("用户", user.id, "dataset", user.dataset_type, user.dataset_id)
        print("should_try_text2sql =", svc._should_try_text2sql(QUESTION))
        print("match_rule =", svc._match_rule(QUESTION, svc._RULES))

        llm = get_chat_llm()
        print("llm =", llm)

        service = svc.AssistantChatService(session)
        try:
            data = service._chat_text2sql(
                llm, actor, QUESTION, session_id=None, history=[]
            )
            if data is None:
                print("_chat_text2sql 返回 None（规划失败/不可回答）")
            else:
                print("tool =", data.get("tool"), "status =", data.get("tool_status"))
                print("reply =", data.get("reply"))
                print("chart =", data.get("chart"))
                print("records =", data.get("records"))
        except Exception:  # noqa: BLE001
            print("异常：")
            traceback.print_exc()
