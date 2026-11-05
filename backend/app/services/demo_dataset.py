"""演示数据集固定标识（T07 / PRD §6 R18、§8、§10.2）。

夹具工作簿以字符串 ``DEMO-QINSHUANG-2026Q3`` 标记 dataset_id，而库内
``dataset_id`` 列为 UUID。为保证：

1. 每次导入同一演示数据集的行落在**同一** UUID（关联链数据域一致、可复现）；
2. 不依赖随机值（便于 SeedCheck 与测试断言）；

这里用固定命名空间 + 字符串 ID 派生 UUIDv5（确定性），与 T06 占位 UUID(int=0)
不同，后者只用于空批次自检。
"""

from __future__ import annotations

import uuid
from uuid import UUID

# 与 app/models/seed.py 的 DEMO_DATASET_ID_DEFAULT 保持一致
DEMO_DATASET_KEY = "DEMO-QINSHUANG-2026Q3"

# 平台固定命名空间（任意确定 UUID；仅用于把演示字符串 ID 派生为稳定 UUID）
_DEMO_NAMESPACE = UUID("6f2f1c94-7d3a-4b6e-9a1f-2c8d5e0b3f10")


def demo_dataset_uuid(key: str = DEMO_DATASET_KEY) -> UUID:
    """字符串数据集标识 → 确定性 UUIDv5。"""
    return uuid.uuid5(_DEMO_NAMESPACE, key)


DEMO_DATASET_UUID: UUID = demo_dataset_uuid()
DEMO_DATASET_TYPE = "DEMO"
PRODUCTION_DATASET_TYPE = "PRODUCTION"

# 沁爽品牌在夹具中的稳定编码
QINSHUANG_BRAND_CODE = "QINSHUANG"
QINSHUANG_BRAND_NAME = "沁爽 QINSHUANG"

# 演示用「沁爽矿泉水」：编码排在既有 34 个 SKU 之后（QS-SKU-035），
# 因此 seeder 里 `_skus(n)` 按 code 升序取前 N 个时不会被挤进补货/采购演示链路。
MINERAL_SKU_CODE = "QS-SKU-035"
MINERAL_SKU_NAME = "沁爽矿泉水"
