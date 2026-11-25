"""已发出的通知回执。

回执本身是历史投递事实，永远保留，不因后续缩小授权而删除；但回执的
“再次打开正文”入口必须跟着当前授权和内容级排除走。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .model import Interval, ReceiptAccess, Segment


@dataclass(frozen=True)
class Receipt:
    id: str
    person_id: str
    material: str
    coverage: Interval
    delivered_at: datetime
    body: str
    detail_material: str | None = None
    detail_coverage: Interval | None = None
    delivery_record: str = ""

    def metadata_text(self) -> str:
        return (
            f"{self.id}：{self.delivered_at.date().isoformat()} 投递给 {self.person_id} 的"
            f"投递记录仍保留"
        )


def build_access(
    receipt: Receipt,
    *,
    material_visible: bool,
    allowed_segments: tuple[Segment, ...],
    detail_excluded: bool,
    reason_if_not: str = "",
) -> ReceiptAccess:
    """回执正文的再次读取裁定。

    * 回执对象始终存在。
    * 资料不在可见范围内：只给投递元数据。
    * 资料可见但摘要明细（提醒）被排除：给部分说明，不提供超出的正文。
    """
    if not material_visible:
        return ReceiptAccess(
            allowed=False,
            receipt=receipt,
            text=None,
            partial_text=receipt.metadata_text(),
            reason=reason_if_not or "当前授权已不含该资料，只保留投递事实",
        )
    allowed = [segment.interval for segment in allowed_segments]
    covered = any(interval.contains(receipt.coverage) for interval in allowed)
    if not covered:
        return ReceiptAccess(
            allowed=False,
            receipt=receipt,
            text=None,
            partial_text=receipt.metadata_text(),
            reason="摘要覆盖的时段已不在当前可见范围内，只保留投递事实",
        )
    if detail_excluded:
        return ReceiptAccess(
            allowed=False,
            receipt=receipt,
            text=None,
            partial_text=receipt.metadata_text() + "；摘要中的提醒明细已按排除范围不再提供",
            reason="摘要整体可见，但提醒明细落在排除范围内",
        )
    return ReceiptAccess(
        allowed=True,
        receipt=receipt,
        text=f"{receipt.id}：{receipt.body}",
        partial_text=None,
        reason="摘要及其明细仍完整落在当前授权范围内",
    )
