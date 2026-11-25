"""家庭共享决定的核心数据模型。

模型刻意把五件事拆开保存，避免一则口语请求把不同维度混成一个“开/关”：

* 受众（谁）
* 资料种类（哪一份内容）
* 读取时间（什么时候可以读）
* 资料本身覆盖的时段（内容描述的是哪段时间）
* 能否再次转发（二次传播权）

所有记录都是合成演练材料。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Mapping


class Action(str, Enum):
    GRANT = "grant"          # 允许查看/领取
    DENY = "deny"            # 明确不给，或收回既有范围
    EXCLUDE = "exclude"      # 从已允许的范围里排除具体内容
    DEFER = "defer"          # 先搁置，等当事人补充
    REAFFIRM = "reaffirm"    # 对既有范围的再次确认，不扩大范围


class DecisionStatus(str, Enum):
    CONFIRMED = "confirmed"
    PENDING = "pending"
    WITHDRAWN = "withdrawn"


class SpeechAct(str, Enum):
    FIRST_PERSON_REQUEST = "first_person_request"
    RELAY = "relay"
    QUESTION = "question"
    CONFIRMATION = "confirmation"
    BOUNDARY = "boundary"
    RECEIPT = "receipt"
    DRAFT = "draft"


MATERIAL_LABELS: dict[str, str] = {
    "sleep_trend": "本周睡眠趋势",
    "daily_average": "日均时长",
    "weekly_summary": "周二统计摘要",
    "reminder_notice": "提醒通知",
}

# 只有明确写出的二次转发授权才会打开；默认始终为 False。
DEFAULT_FORWARDABLE = False

_ONE_MINUTE = timedelta(minutes=1)


def material_label(material: str) -> str:
    return MATERIAL_LABELS.get(material, material)


@dataclass(frozen=True)
class Person:
    id: str
    label: str
    relation: str
    aliases: tuple[str, ...] = ()
    is_owner: bool = False
    is_family: bool = False
    registered: bool = True
    source_record: str = ""

    @property
    def display(self) -> str:
        if not self.registered:
            return f"{self.id}（未登记联系人）"
        return f"{self.id}（{self.relation}）"


@dataclass(frozen=True)
class Interval:
    """半开时间区间 [start, end)；None 表示该侧不设边界。"""

    start: datetime | None
    end: datetime | None
    label: str = ""
    precision: str = "day"

    def is_open(self) -> bool:
        return self.start is None and self.end is None

    def is_point(self) -> bool:
        return self.start is not None and self.end is not None and self.end - self.start <= _ONE_MINUTE

    def contains(self, other: "Interval | None") -> bool:
        if other is None:
            return True
        if self.start is not None and (other.start is None or other.start < self.start):
            return False
        if self.end is not None and (other.end is None or other.end > self.end):
            return False
        return True

    def overlaps(self, other: "Interval | None") -> bool:
        if other is None:
            return True
        if self.end is not None and other.start is not None and other.start >= self.end:
            return False
        if other.end is not None and self.start is not None and other.end <= self.start:
            return False
        return True

    def key(self) -> list[str | None]:
        return [
            self.start.isoformat() if self.start else None,
            self.end.isoformat() if self.end else None,
            self.precision,
        ]

    def describe(self) -> str:
        if self.label:
            return self.label
        if self.is_open():
            return "长期/不设边界"
        start = self.start.date().isoformat() if self.start else "…"
        end = self.end.date().isoformat() if self.end else "…"
        return f"{start} 至 {end}"


def intersect(left: Interval, right: Interval) -> Interval | None:
    if left.is_point() and right.contains(left):
        return left
    if right.is_point() and left.contains(right):
        return right
    start = left.start if right.start is None else right.start if left.start is None else max(left.start, right.start)
    end = left.end if right.end is None else right.end if left.end is None else min(left.end, right.end)
    if start is not None and end is not None:
        if start > end:
            return None
        if start == end and not (left.is_point() and right.is_point()):
            return None
    return Interval(start, end, label=f"{left.describe()} ∩ {right.describe()}", precision="clip")


def subtract(interval: Interval, cut: Interval) -> tuple[Interval, ...] | None:
    """从 interval 中剪掉 cut；完全不相交时返回 None。"""
    if not interval.overlaps(cut):
        return None
    parts: list[Interval] = []
    if interval.start is not None and cut.start is not None and cut.start > interval.start:
        end = cut.start if interval.end is None else min(cut.start, interval.end)
        if end > interval.start:
            parts.append(Interval(interval.start, end, label=interval.describe(), precision="clip"))
    if interval.end is not None and cut.end is not None and cut.end < interval.end:
        start = cut.end if interval.start is None else max(cut.end, interval.start)
        if start < interval.end:
            parts.append(Interval(start, interval.end, label=interval.describe(), precision="clip"))
    if not parts:
        return ()
    return tuple(parts)


@dataclass(frozen=True)
class Exclusion:
    """针对某位亲友的内容级排除；不撤销该亲友其余的合法范围。"""

    person_id: str
    material: str
    coverage: Interval
    decision_id: str
    note: str = ""


@dataclass(frozen=True)
class Scope:
    person_id: str
    material: str
    status: DecisionStatus
    coverages: tuple[Interval, ...] = ()
    read_window: Interval | None = None
    forwardable: bool = DEFAULT_FORWARDABLE
    forward_denied: bool = False
    decision_ids: tuple[str, ...] = ()

    @property
    def coverage(self) -> Interval | None:
        """可用范围的包络；具体可用片段见 coverages。"""
        if not self.coverages:
            return None
        start = min((c.start for c in self.coverages if c.start is not None), default=None)
        end = max((c.end for c in self.coverages if c.end is not None), default=None)
        if start is None and end is None and not any(c.is_open() for c in self.coverages):
            return None
        return Interval(start, end, precision="bound")

    def allows(self, coverage: Interval | None = None) -> bool:
        if self.status is not DecisionStatus.CONFIRMED:
            return False
        if coverage is None:
            return bool(self.coverages)
        return any(c.contains(coverage) for c in self.coverages)

    def clips(self, coverage: Interval) -> tuple[Interval, ...]:
        pieces = []
        for own in self.coverages:
            part = intersect(own, coverage)
            if part is not None:
                pieces.append(part)
        return tuple(pieces)


@dataclass(frozen=True)
class PersonView:
    person_id: str
    version: int
    scopes: Mapping[str, Scope]
    exclusions: tuple[Exclusion, ...]

    def exclusions_for(self, material: str) -> tuple[Exclusion, ...]:
        return tuple(exclusion for exclusion in self.exclusions if exclusion.material == material)

    def allows(self, material: str, coverage: Interval | None = None) -> bool:
        scope = self.scopes.get(material)
        return bool(scope and scope.allows(coverage))

    def allows_content(self, content_material: str, coverage: Interval | None) -> bool:
        """内容级的额外过滤：查看某份摘要时，其中被排除的子内容要剔除。"""
        for exclusion in self.exclusions:
            if exclusion.material == content_material and exclusion.coverage.overlaps(coverage):
                return False
        return True

    def fingerprint(self, material: str) -> str:
        """某位亲友在某份资料上的范围指纹。

        提问时记录指纹；确认到达时若指纹已变，迟到的“可以”不能签成新同意。
        无关资料、无关受众的变动不会改变这里的结果。
        """
        scope = self.scopes.get(material)
        payload = {
            "person": self.person_id,
            "material": material,
            "scope": None
            if scope is None
            else {
                "status": scope.status.value,
                "coverages": sorted(c.key() for c in scope.coverages),
                # 读取时间是与资料覆盖时段相互独立的维度；读取窗口的调整
                # 不追溯作废对内容范围的确认。
                "forwardable": scope.forwardable,
                "forward_denied": scope.forward_denied,
            },
            # 只计入同一份资料上的排除。排除的是提醒，不应让“趋势可以”这句
            # 确认失效；反之，趋势本身的范围一变，旧确认就不能再利用。
            "exclusions": sorted(
                [exclusion.coverage.key() for exclusion in self.exclusions if exclusion.material == material],
                key=lambda item: json.dumps(item, ensure_ascii=False),
            ),
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return digest[:16]

    def snapshot(self) -> dict:
        return {
            "version": self.version,
            "scopes": {
                material: {
                    "status": scope.status.value,
                    "coverages": [c.describe() for c in scope.coverages],
                    "read_window": scope.read_window.describe() if scope.read_window else None,
                    "forwardable": scope.forwardable,
                    "forward_denied": scope.forward_denied,
                }
                for material, scope in sorted(self.scopes.items())
            },
            "exclusions": [
                {
                    "material": exclusion.material,
                    "coverage": exclusion.coverage.describe(),
                    "decision": exclusion.decision_id,
                }
                for exclusion in self.exclusions
            ],
        }


@dataclass(frozen=True)
class Decision:
    id: str
    seq: int
    person_id: str
    action: Action
    material: str
    audience_text: str
    status: DecisionStatus = DecisionStatus.CONFIRMED
    coverage: Interval | None = None
    read_window: Interval | None = None
    forwardable: bool = DEFAULT_FORWARDABLE
    forwardable_explicit: bool = False
    forward_denied_explicit: bool = False
    source_record: str = ""
    source_span: str = ""
    question_id: str | None = None
    revision: bool = False
    basis: str = "consent"  # consent | notice | clarification
    note: str = ""

    def source_ref(self) -> str:
        if self.source_span:
            return f"{self.source_record}「{self.source_span}」"
        return self.source_record


@dataclass(frozen=True)
class Question:
    id: str
    asker: str
    person_id: str | None
    audience_text: str
    material: str | None
    coverage: Interval | None
    prompt: str
    scope_version: int
    scope_fingerprint: str | None
    source_record: str
    person_id_at_ask: str | None = None
    read_window: Interval | None = None
    options: tuple[str, ...] = ()
    audience_candidates: tuple[str, ...] = ()
    revokes_on_negative: bool = False
    intent: str = "grant"  # grant | reaffirm | exclude | disclose
    referent_invalidated: bool = False
    invalidated_by: str | None = None
    state: str = "open"  # open | answered | resolved | stale | ambiguous | withdrawn
    asked_at: datetime | None = None
    answered_by: str | None = None
    answered_at: datetime | None = None
    late_answer: bool = False
    blocking: bool = False
    decisive_fact: str = ""
    note: str = ""


@dataclass(frozen=True)
class Clarification:
    """一次澄清把某个代词指回哪位亲友，并排除哪些候选。

    澄清只解决“谁”，不自动构成授权；是否授权仍要另行对应提问与范围。
    """

    id: str
    record_id: str
    question_ids: tuple[str, ...]
    refers_to: tuple[str, ...]
    excludes: tuple[str, ...]
    effect: str = ""


@dataclass(frozen=True)
class Issue:
    id: str
    kind: str
    question: str
    source_record: str
    person_id: str | None = None
    material: str | None = None
    affects: tuple[str, ...] = ()
    blocking: bool = False
    detail: str = ""


@dataclass(frozen=True)
class ConfirmationOutcome:
    state: str  # bound | stale | ambiguous | mismatched | unmatched
    applied: bool
    reason: str
    question_id: str | None = None
    question_prompt: str | None = None
    person_id: str | None = None
    decision_id: str | None = None
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class AudienceResolution:
    raw: str
    person_ids: tuple[str, ...]
    state: str  # resolved | ambiguous | unresolved
    basis: str = ""


@dataclass(frozen=True)
class Segment:
    interval: Interval

    def describe(self) -> str:
        return self.interval.describe()


@dataclass(frozen=True)
class BasisEntry:
    decision_id: str
    action: str
    material: str
    status: str
    coverage: str | None
    source_record: str
    source_span: str
    question_id: str | None = None
    basis: str = "consent"

    def as_text(self) -> str:
        origin = f"{self.source_record}「{self.source_span}」"
        if self.question_id:
            origin += f"（回答提问 {self.question_id}）"
        state = "有效" if self.status == DecisionStatus.CONFIRMED.value else "已撤回"
        return f"{self.source_record}：{self.action} {material_label(self.material)}，{state}，依据 {origin}"


@dataclass(frozen=True)
class MaterialBasis:
    person_id: str
    material: str
    coverage: Interval
    visible: bool
    allowed_segments: tuple[Segment, ...]
    excluded_segments: tuple[Segment, ...]
    basis: tuple[BasisEntry, ...]
    exclusion_refs: tuple[BasisEntry, ...]
    pending_questions: tuple[Question, ...]
    notice_receipts: tuple[str, ...]
    read_window: Interval | None = None
    content_warnings: tuple[str, ...] = ()
    note: str = ""

    def describe(self) -> str:
        window = self.read_window
        window_text = ""
        if window is not None and not window.is_open():
            window_text = f"，读取窗口：{window.describe()}"
        if not self.visible:
            reason = self.note or "没有有效授权或已被排除"
            return (
                f"{self.person_id} 不能查看{material_label(self.material)}"
                f"（{self.coverage.describe()}{window_text}）：{reason}"
            )
        segments = "、".join(segment.interval.describe() for segment in self.allowed_segments) or "（无片段）"
        basis = "；".join(entry.as_text() for entry in self.basis) or "本人"
        return f"{self.person_id} 可查看{material_label(self.material)}（{segments}{window_text}），依据：{basis}"

    def visible_segment_ids(self) -> tuple[str, ...]:
        return tuple(segment.interval.label or segment.interval.describe() for segment in self.allowed_segments)


@dataclass(frozen=True)
class MaterialOrigin:
    material: str
    authorization_records: tuple[str, ...]
    content_records: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class InferenceFinding:
    person_id: str
    material: str
    excluded_decision: str
    reminder_moment: datetime | None
    source_records: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class ClarificationEffect:
    clarification_id: str
    source_record: str
    refers_to: tuple[str, ...]
    excludes: tuple[str, ...]
    affected_questions: tuple[tuple[str, str, str, str], ...]  # (id, person, material, state)
    affected_decisions: tuple[str, ...]
    note: str = ""

    @property
    def affected_persons(self) -> tuple[str, ...]:
        people = set(self.refers_to)
        for _, person_id, _, _ in self.affected_questions:
            if person_id:
                people.add(person_id)
        return tuple(sorted(people))


@dataclass(frozen=True)
class BundleItem:
    id: str
    material: str
    coverage: Interval
    text: str
    source_record: str
    redactions: tuple[str, ...] = ()
    basis_decision_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WithheldItem:
    id: str
    material: str
    reason: str
    decision_ids: tuple[str, ...] = ()
    recoverable: bool = True  # 是否只是被排除/缩小，而非从未授权


@dataclass(frozen=True)
class Bundle:
    person_id: str
    version: int
    items: tuple[BundleItem, ...]
    withheld: tuple[WithheldItem, ...]
    notes: tuple[str, ...] = ()
    receipt_id: str | None = None

    def text(self) -> str:
        return "\n".join(item.text for item in self.items)

    def material_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)


@dataclass(frozen=True)
class ReceiptAccess:
    allowed: bool
    receipt: object | None
    text: str | None
    reason: str = ""
    partial_text: str | None = None


@dataclass(frozen=True)
class ForwardingCheck:
    person_id: str
    material: str
    allowed: bool
    reason: str
    decision_ids: tuple[str, ...] = ()


def unique_sorted(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))
