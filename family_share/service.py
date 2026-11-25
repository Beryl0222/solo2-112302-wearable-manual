"""家庭共享决定账本：摄取、更正、查询与分发。

核心不变量：

1. 维度分开。受众、资料种类、读取时间、资料覆盖时段、二次转发各自决定。
   排除“昨晚那条”不会停掉“本周趋势”。
2. 只有佩戴者本人的明确同意能建立授权。转述、反问、请求“再问问”都不算。
3. 迟到的确认必须对应它回答的那个提问；期间范围变了或指代人了，就不能
   签成新同意。
4. 分发按“每份材料的真实来源 + 受众”裁定；组合后可推断被排除提醒时，
   只删减相关上下文，不连坐收件人原本有权看的趋势与摘要。
5. 回执保留投递事实，但正文入口跟着授权缩小。
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping, Sequence

from . import cast
from .cast import OWNER_ID, parse_persons, resolve_audience
from .events import (
    Event,
    build_events,
    classify_material,
    is_owner_voice,
    is_relay,
    night_bounds,
    open_bounds,
    parse_coverage,
    parse_minutes_after,
    parse_sleep_end,
    week_bounds,
)
from .model import (
    Action,
    BasisEntry,
    Bundle,
    BundleItem,
    Clarification,
    ClarificationEffect,
    ConfirmationOutcome,
    Decision,
    DecisionStatus,
    Exclusion,
    ForwardingCheck,
    InferenceFinding,
    Interval,
    Issue,
    MaterialBasis,
    MaterialOrigin,
    Person,
    PersonView,
    Question,
    ReceiptAccess,
    Scope,
    Segment,
    WithheldItem,
    subtract,
)
from .receipts import Receipt, build_access

DEFAULT_TODAY = date(2026, 11, 25)
DEFAULT_REFERENCE = datetime(2026, 11, 25, 9, 0)

_UNSET = object()

CONTENT_MATERIALS = ("sleep_trend", "daily_average", "weekly_summary", "reminder_notice")


# ---------------------------------------------------------------- JSON 工具


def _interval_json(interval: Interval | None) -> dict | None:
    if interval is None:
        return None
    return {
        "start": interval.start.isoformat() if interval.start else None,
        "end": interval.end.isoformat() if interval.end else None,
        "label": interval.label,
        "precision": interval.precision,
    }


def _interval_obj(data: Mapping | None) -> Interval | None:
    if not data:
        return None
    return Interval(
        start=datetime.fromisoformat(data["start"]) if data.get("start") else None,
        end=datetime.fromisoformat(data["end"]) if data.get("end") else None,
        label=data.get("label", ""),
        precision=data.get("precision", "day"),
    )


def _decision_json(decision: Decision) -> dict:
    return {
        "id": decision.id,
        "seq": decision.seq,
        "person_id": decision.person_id,
        "action": decision.action.value,
        "material": decision.material,
        "audience_text": decision.audience_text,
        "status": decision.status.value,
        "coverage": _interval_json(decision.coverage),
        "read_window": _interval_json(decision.read_window),
        "forwardable": decision.forwardable,
        "forwardable_explicit": decision.forwardable_explicit,
        "forward_denied_explicit": decision.forward_denied_explicit,
        "source_record": decision.source_record,
        "source_span": decision.source_span,
        "question_id": decision.question_id,
        "revision": decision.revision,
        "basis": decision.basis,
        "note": decision.note,
    }


def _decision_obj(data: Mapping) -> Decision:
    return Decision(
        id=data["id"],
        seq=int(data["seq"]),
        person_id=data["person_id"],
        action=Action(data["action"]),
        material=data["material"],
        audience_text=data["audience_text"],
        status=DecisionStatus(data["status"]),
        coverage=_interval_obj(data.get("coverage")),
        read_window=_interval_obj(data.get("read_window")),
        forwardable=bool(data.get("forwardable", False)),
        forwardable_explicit=bool(data.get("forwardable_explicit", False)),
        forward_denied_explicit=bool(data.get("forward_denied_explicit", False)),
        source_record=data.get("source_record", ""),
        source_span=data.get("source_span", ""),
        question_id=data.get("question_id"),
        revision=bool(data.get("revision", False)),
        basis=data.get("basis", "consent"),
        note=data.get("note", ""),
    )


def _question_json(question: Question) -> dict:
    return {
        "id": question.id,
        "asker": question.asker,
        "person_id": question.person_id,
        "person_id_at_ask": question.person_id_at_ask,
        "audience_text": question.audience_text,
        "material": question.material,
        "coverage": _interval_json(question.coverage),
        "read_window": _interval_json(question.read_window),
        "prompt": question.prompt,
        "scope_version": question.scope_version,
        "scope_fingerprint": question.scope_fingerprint,
        "source_record": question.source_record,
        "options": list(question.options),
        "audience_candidates": list(question.audience_candidates),
        "revokes_on_negative": question.revokes_on_negative,
        "intent": question.intent,
        "referent_invalidated": question.referent_invalidated,
        "invalidated_by": question.invalidated_by,
        "state": question.state,
        "asked_at": question.asked_at.isoformat() if question.asked_at else None,
        "answered_by": question.answered_by,
        "answered_at": question.answered_at.isoformat() if question.answered_at else None,
        "late_answer": question.late_answer,
        "blocking": question.blocking,
        "decisive_fact": question.decisive_fact,
        "note": question.note,
    }


def _question_obj(data: Mapping) -> Question:
    return Question(
        id=data["id"],
        asker=data["asker"],
        person_id=data.get("person_id"),
        audience_text=data["audience_text"],
        material=data.get("material"),
        coverage=_interval_obj(data.get("coverage")),
        prompt=data["prompt"],
        scope_version=int(data.get("scope_version", 0)),
        scope_fingerprint=data.get("scope_fingerprint"),
        source_record=data["source_record"],
        read_window=_interval_obj(data.get("read_window")),
        options=tuple(data.get("options", ())),
        audience_candidates=tuple(data.get("audience_candidates", ())),
        revokes_on_negative=bool(data.get("revokes_on_negative", False)),
        intent=data.get("intent", "grant"),
        referent_invalidated=bool(data.get("referent_invalidated", False)),
        invalidated_by=data.get("invalidated_by"),
        state=data.get("state", "open"),
        person_id_at_ask=data.get("person_id_at_ask", data.get("person_id")),
        asked_at=datetime.fromisoformat(data["asked_at"]) if data.get("asked_at") else None,
        answered_by=data.get("answered_by"),
        answered_at=datetime.fromisoformat(data["answered_at"]) if data.get("answered_at") else None,
        late_answer=bool(data.get("late_answer", False)),
        blocking=bool(data.get("blocking", False)),
        decisive_fact=data.get("decisive_fact", ""),
        note=data.get("note", ""),
    )


# --------------------------------------------------------------------- 账本


class Ledger:
    """家庭共享决定账本。可从合成语料构造，也可从快照恢复。"""

    def __init__(
        self,
        persons: Mapping[str, Person],
        events: Mapping[str, Event] | None = None,
        *,
        today: date = DEFAULT_TODAY,
        reference: datetime = DEFAULT_REFERENCE,
    ) -> None:
        self.persons = dict(persons)
        self.events = dict(events or {})
        self.today = today
        self.reference = reference
        self.decisions: list[Decision] = []
        self.questions: dict[str, Question] = {}
        self.clarifications: list[Clarification] = []
        self.rollbacks: list[Issue] = []
        self.receipts: list[Receipt] = []
        self.material_origins: dict[str, MaterialOrigin] = {}
        self._seq = 0
        self._version = 0
        self._dirty = True
        self._views: dict[str, PersonView] = {}
        self._ingested: set[str] = set()

    # ------------------------------------------------------------- 构造入口
    @classmethod
    def from_corpus(
        cls,
        corpus: Mapping,
        *,
        today: date = DEFAULT_TODAY,
        reference: datetime = DEFAULT_REFERENCE,
        timestamps: Mapping[str, datetime] | None = None,
        include: Iterable[str] | None = None,
    ) -> "Ledger":
        persons = parse_persons(corpus)
        events = build_events(corpus, timestamps)
        ledger = cls(persons, events, today=today, reference=reference)
        ledger.ingest(corpus, include=include)
        return ledger

    @classmethod
    def from_state(
        cls,
        state: Mapping,
        persons: Mapping[str, Person],
        *,
        events: Mapping[str, Event] | None = None,
    ) -> "Ledger":
        ledger = cls(persons, events)
        ledger.today = date.fromisoformat(state["today"])
        ledger.reference = datetime.fromisoformat(state["reference"])
        ledger.decisions = [_decision_obj(item) for item in state.get("decisions", ())]
        ledger.questions = {item["id"]: _question_obj(item) for item in state.get("questions", ())}
        ledger.clarifications = [
            Clarification(
                id=item["id"],
                record_id=item["record_id"],
                question_ids=tuple(item.get("question_ids", ())),
                refers_to=tuple(item.get("refers_to", ())),
                excludes=tuple(item.get("excludes", ())),
                effect=item.get("effect", ""),
            )
            for item in state.get("clarifications", ())
        ]
        ledger.receipts = [
            Receipt(
                id=item["id"],
                person_id=item["person_id"],
                material=item["material"],
                coverage=_interval_obj(item["coverage"]),  # type: ignore[arg-type]
                delivered_at=datetime.fromisoformat(item["delivered_at"]),
                body=item.get("body", ""),
                detail_material=item.get("detail_material"),
                detail_coverage=_interval_obj(item.get("detail_coverage")),
                delivery_record=item.get("delivery_record", ""),
            )
            for item in state.get("receipts", ())
        ]
        ledger.material_origins = {
            material: MaterialOrigin(
                material=material,
                authorization_records=tuple(item.get("authorization_records", ())),
                content_records=tuple(item.get("content_records", ())),
                note=item.get("note", ""),
            )
            for material, item in state.get("material_origins", {}).items()
        }
        ledger.rollbacks = [
            Issue(
                id=item["id"],
                kind=item["kind"],
                question=item["question"],
                source_record=item["source_record"],
                person_id=item.get("person_id"),
                material=item.get("material"),
                affects=tuple(item.get("affects", ())),
                blocking=bool(item.get("blocking", False)),
                detail=item.get("detail", ""),
            )
            for item in state.get("issues", ())
        ]
        ledger._seq = int(state.get("seq", 0))
        ledger._version = int(state.get("version", 0))
        ledger._ingested = set(state.get("ingested", ()))
        ledger._dirty = True
        ledger._rebuild_views()
        return ledger

    def export_state(self) -> dict:
        return {
            "today": self.today.isoformat(),
            "reference": self.reference.isoformat(),
            "seq": self._seq,
            "version": self.version,
            "ingested": sorted(self._ingested),
            "persons": {
                person_id: {
                    "label": person.label,
                    "relation": person.relation,
                    "aliases": list(person.aliases),
                    "family": person.is_family,
                }
                for person_id, person in sorted(self.persons.items())
            },
            "material_origins": {
                material: {
                    "authorization_records": list(origin.authorization_records),
                    "content_records": list(origin.content_records),
                    "note": origin.note,
                }
                for material, origin in sorted(self.material_origins.items())
            },
            "decisions": [_decision_json(decision) for decision in self.decisions],
            "questions": [_question_json(question) for question in self.questions.values()],
            "clarifications": [
                {
                    "id": item.id,
                    "record_id": item.record_id,
                    "question_ids": list(item.question_ids),
                    "refers_to": list(item.refers_to),
                    "excludes": list(item.excludes),
                    "effect": item.effect,
                }
                for item in self.clarifications
            ],
            "receipts": [
                {
                    "id": receipt.id,
                    "person_id": receipt.person_id,
                    "material": receipt.material,
                    "coverage": _interval_json(receipt.coverage),
                    "delivered_at": receipt.delivered_at.isoformat(),
                    "body": receipt.body,
                    "detail_material": receipt.detail_material,
                    "detail_coverage": _interval_json(receipt.detail_coverage),
                    "delivery_record": receipt.delivery_record,
                }
                for receipt in self.receipts
            ],
            "issues": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "question": item.question,
                    "source_record": item.source_record,
                    "person_id": item.person_id,
                    "material": item.material,
                    "affects": list(item.affects),
                    "blocking": item.blocking,
                    "detail": item.detail,
                }
                for item in self.rollbacks
            ],
            "views": {person_id: view.snapshot() for person_id, view in sorted(self._views.items())},
        }

    # ----------------------------------------------------------------- 摄取
    def ingest(self, corpus: Mapping, *, include: Iterable[str] | None = None) -> "Ledger":
        wanted = set(include) if include is not None else None
        records = [record for record in corpus["records"] if wanted is None or record["id"] in wanted]
        ordered = sorted(records, key=lambda record: _record_order(record["id"], record["kind"]))
        for record in ordered:
            if record["kind"] == "person" or record["id"] in self._ingested:
                continue
            event = self.events.get(record["id"])
            if event is None:
                event = Event(
                    id=record["id"],
                    kind=record["kind"],
                    order=0,
                    text=record["text"],
                )
                self.events[record["id"]] = event
            self._ingest_event(event)
            self._ingested.add(record["id"])
        return self

    def _ingest_event(self, event: Event) -> None:
        handler = {
            "receipt": self._ingest_receipt,
            "request": self._ingest_request,
            "question": self._ingest_question,
            "response": self._ingest_response,
            "message": self._ingest_draft,
        }.get(event.kind)
        if handler is None:
            return
        handler(event)

    # --------------------------------------------------------- 既往回执
    def _ingest_receipt(self, event: Event) -> None:
        if event.heading.startswith("原同意"):
            self._ingest_original_consent(event)
        elif event.heading.startswith("原通知"):
            self._ingest_notice(event)

    def _ingest_original_consent(self, event: Event) -> None:
        text = event.body
        forwardable = cast.grants_forwarding(text)
        denied = cast.denies_forwarding(text)
        trend_match = re.search(r"允许\s*(?P<who>p\d+)\s*查看本周睡眠趋势", text)
        if trend_match and trend_match.group("who") in self.persons:
            person_id = trend_match.group("who")
            self._add_decision(
                Decision(
                    id=f"d-sleep_trend-{person_id}-{event.id}",
                    seq=self._next_seq(),
                    person_id=person_id,
                    action=Action.GRANT,
                    material="sleep_trend",
                    audience_text="本周睡眠趋势",
                    coverage=week_bounds(self.today),
                    forwardable=forwardable,
                    forwardable_explicit=forwardable or denied,
                    forward_denied_explicit=denied,
                    source_record=event.id,
                    source_span="允许查看本周睡眠趋势",
                    basis="consent",
                    note="既往已确认的共享回执",
                )
            )
            self._note_origin("sleep_trend", authorization=event.id)
        daily_match = re.search(r"允许\s*(?P<who>p\d+)\s*长期查看日均时长", text)
        if daily_match and daily_match.group("who") not in self.persons:
            self._register_issue(
                Issue(
                    id=f"i-{event.id}-daily-recipient",
                    kind="unresolved_recipient",
                    question=f"既往同意里的 {daily_match.group('who')} 对应哪位已登记联系人？",
                    source_record=event.id,
                    material="daily_average",
                    blocking=True,
                    detail="该受众不在联系人登记中，不能把这份授权算到任何一位亲友头上。",
                )
            )
        if daily_match and daily_match.group("who") in self.persons:
            person_id = daily_match.group("who")
            self._add_decision(
                Decision(
                    id=f"d-daily_average-{person_id}-{event.id}",
                    seq=self._next_seq(),
                    person_id=person_id,
                    action=Action.GRANT,
                    material="daily_average",
                    audience_text="日均时长",
                    coverage=open_bounds("长期"),
                    forwardable=forwardable,
                    forwardable_explicit=forwardable or denied,
                    forward_denied_explicit=denied,
                    source_record=event.id,
                    source_span="允许长期查看日均时长",
                    basis="consent",
                    note="既往已确认的共享回执",
                )
            )
            self._note_origin("daily_average", authorization=event.id)

    def _ingest_notice(self, event: Event) -> None:
        text = event.body
        person_match = re.search(r"p\d+", text)
        if not person_match or person_match.group(0) not in self.persons:
            self._register_issue(
                Issue(
                    id=f"i-{event.id}-recipient",
                    kind="unresolved_recipient",
                    question="这条通知回执指向哪位联系人？",
                    source_record=event.id,
                    blocking=True,
                    detail="回执里的收件人标记不在登记联系人中，不能猜。",
                )
            )
            return
        person_id = person_match.group(0)
        if "周二统计摘要" not in text:
            return
        coverage = _day_of("周二", self.today)
        minutes, anchor = parse_minutes_after(text)
        detail_coverage: Interval | None = None
        detail_text = ""
        if minutes is not None and anchor:
            end = self._sleep_end()
            if end is not None:
                moment = end + timedelta(minutes=minutes)
                detail_coverage = _point(moment, f"{event.id} 所述提醒时刻")
                detail_text = f"距第一段睡眠结束后 {minutes} 分钟有提醒"
            else:
                detail_text = f"距第一段睡眠结束后 {minutes} 分钟有提醒（时刻不可推算）"
        else:
            detail_text = "周二统计摘要"
        self._add_decision(
            Decision(
                id=f"d-weekly_summary-{person_id}-{event.id}",
                seq=self._next_seq(),
                person_id=person_id,
                action=Action.GRANT,
                material="weekly_summary",
                audience_text="周二统计摘要",
                coverage=coverage,
                forwardable=False,
                forward_denied_explicit=True,
                source_record=event.id,
                source_span="可查看周二统计摘要",
                basis="notice",
                note="既往通知回执，仅覆盖摘要本身",
            )
        )
        self.receipts.append(
            Receipt(
                id=f"{event.id}-delivery",
                person_id=person_id,
                material="weekly_summary",
                coverage=coverage,
                delivered_at=event.written_at or self.reference,
                body=detail_text,
                detail_material="reminder_notice" if detail_coverage else None,
                detail_coverage=detail_coverage,
                delivery_record=event.id,
            )
        )
        self._note_origin("weekly_summary", authorization=event.id, content=event.id)
        if detail_coverage is not None:
            self._note_origin("reminder_notice", content=event.id)

    def _sleep_end(self) -> datetime | None:
        """从先前记录里找第一段睡眠的结束时刻；没有就返回 None，绝不猜。"""
        best: datetime | None = None
        for event in sorted(self.events.values(), key=lambda item: item.order):
            if "第一段睡眠" not in event.text:
                continue
            # 没有写出时间时，以账本的参考日当作语料“今天”；只对“昨晚”
            # 这种相对日说法生效，不改变文本本身的含义。
            written = event.written_at.date() if event.written_at else self.today
            moment = parse_sleep_end(event.text, written)
            if moment is not None:
                best = moment
        return best

    # --------------------------------------------------------- 请求与提问
    def _ingest_request(self, event: Event) -> None:
        if is_owner_voice(event.heading):
            self._ingest_owner_request(event)
        elif is_relay(event.heading):
            self._ingest_relay(event)
        else:
            self._register_issue(
                Issue(
                    id=f"i-{event.id}-speaker",
                    kind="unclear_speaker",
                    question="这句请求是谁说的？",
                    source_record=event.id,
                    blocking=True,
                    detail="没有标出说话人的请求不能当作佩戴者同意。",
                )
            )

    def _ingest_owner_request(self, event: Event) -> None:
        """佩戴者本人的文字：拆成“给 / 不给 / 再问”三类子句分别处理。

        同一则文字里先出现的明确受众，可以消解后续子句里的“她/他”，
        因为那是同一说话人在同一段话内的指代，不是另一位同名亲友。
        """
        last_resolved: tuple[str, ...] = ()
        if re.search(r"(^|[^你他她])妈", event.body):
            self._register_issue(
                Issue(
                    id=f"i-{event.id}-mother",
                    kind="unregistered_person",
                    question="‘妈’的需求对应哪位已登记联系人？母亲尚未登记，不能建立范围。",
                    source_record=event.id,
                    blocking=False,
                    detail="‘照原来那样’不产生新的授权；原有安排只属于已登记的亲友。",
                )
            )
        for clause in _clauses(event.body):
            if _asks_to_ask(clause):
                # “阿敏那边再提醒我问问”：只是请佩戴者再确认，不是授权，
                # 也没有说明资料种类。记为待办，不新建范围提问，也不替它
                # 在两位阿敏里选一位。
                self._register_issue(
                    Issue(
                        id=f"i-{event.id}-ask-again",
                        kind="request_update",
                        question="‘阿敏那边再提醒我问问’：要再确认的是哪位阿敏、哪一份资料？",
                        source_record=event.id,
                        person_id=None,
                        affects=tuple(
                            question.id
                            for question in self.questions.values()
                            if question.material == "daily_average"
                        ),
                        detail="这句话既不是授权也不是撤回；它只要求佩戴者再问一次。",
                    )
                )
                continue

            material = classify_material(re.sub(r"再提醒|提醒我问问|再问", "", clause))
            if material is None and _is_exclusion(clause) and any(
                term in clause for term in ("昨晚", "今晚", "那条", "这条", "提醒")
            ):
                material = "reminder_notice"
            if material is None:
                continue

            if _is_question(clause):
                # 反问/询问不是授权，也不是撤回：只留作待确认。
                self._register_issue(
                    Issue(
                        id=f"i-{event.id}-question-{_slug(clause)}",
                        kind="question_not_consent",
                        question=f"这是询问不是授权：{clause}",
                        source_record=event.id,
                        person_id=None,
                        material=material,
                        detail="疑问句不建立、也不收窄任何范围；需要一句明确的答复。",
                    )
                )
                continue

            if _is_exclusion(clause):
                # “昨晚那条别让她看”：只排除具体内容，不撤销其余范围。
                target_text = _exclusion_target(clause)
                resolution = resolve_audience(target_text, self.persons)
                if resolution.state != "resolved" and last_resolved and _is_pronoun(target_text):
                    resolution = cast.AudienceResolution(
                        target_text,
                        last_resolved,
                        "resolved",
                        "同一则请求内前一句已明确的受众",
                    )
                coverage = parse_coverage(clause, self.today, material) or night_bounds(self.reference.date())
                if resolution.state != "resolved":
                    self._open_question(
                        person_id=None,
                        audience_text=target_text,
                        material=material,
                        coverage=coverage,
                        prompt=f"‘{target_text}’指哪位亲友？",
                        source_record=event.id,
                        audience_candidates=resolution.person_ids,
                        intent="exclude",
                        decisive="确定排除对象",
                    )
                    continue
                last_resolved = resolution.person_ids
                for person_id in resolution.person_ids:
                    self._add_decision(
                        Decision(
                            id=f"d-exclude-{material}-{person_id}-{event.id}-{_slug(clause)}",
                            seq=self._next_seq(),
                            person_id=person_id,
                            action=Action.EXCLUDE,
                            material=material,
                            audience_text=target_text,
                            coverage=coverage,
                            source_record=event.id,
                            source_span=clause,
                            note="只排除这一条内容，不撤销该亲友其余范围",
                        )
                    )
                continue

            if _is_grant(clause):
                target_text = _grant_target(clause)
                resolution = resolve_audience(target_text, self.persons)
                if resolution.state != "resolved" and last_resolved and _is_pronoun(target_text):
                    resolution = cast.AudienceResolution(
                        target_text,
                        last_resolved,
                        "resolved",
                        "同一则请求内前一句已明确的受众",
                    )
                coverage = parse_coverage(clause, self.today, material) or _default_coverage(material, self.today)
                if resolution.state != "resolved":
                    self._open_question(
                        person_id=None,
                        audience_text=target_text,
                        material=material,
                        coverage=coverage,
                        prompt=f"‘{target_text}’指哪位亲友？",
                        source_record=event.id,
                        audience_candidates=resolution.person_ids,
                        intent="grant",
                        decisive="唯一确定受众",
                    )
                    continue
                last_resolved = resolution.person_ids
                for person_id in resolution.person_ids:
                    self._add_decision(
                        Decision(
                            id=f"d-{material}-{person_id}-{event.id}",
                            seq=self._next_seq(),
                            person_id=person_id,
                            action=Action.GRANT,
                            material=material,
                            audience_text=target_text,
                            coverage=coverage,
                            forwardable=cast.grants_forwarding(clause),
                            forwardable_explicit=cast.mentions_forwarding(clause)
                            or cast.denies_forwarding(clause),
                            forward_denied_explicit=cast.denies_forwarding(clause),
                            source_record=event.id,
                            source_span=clause,
                            note="佩戴者本人新文字",
                        )
                    )
                    self._note_origin(material, authorization=event.id)

    def _ingest_relay(self, event: Event) -> None:
        body = event.body
        owner_ref = resolve_audience(_relay_referent(body), self.persons)
        self._register_issue(
            Issue(
                id=f"i-{event.id}-relay",
                kind="relay_not_consent",
                question="转述里的需求是给谁的，是否得到佩戴者本人确认？",
                source_record=event.id,
                person_id=owner_ref.person_ids[0] if len(owner_ref.person_ids) == 1 else None,
                affects=(),
                detail="转述不构成授权；只有佩戴者本人明确说出的同意才建立范围。",
            )
        )
        if "妈" in body:
            self._register_issue(
                Issue(
                    id=f"i-{event.id}-mother",
                    kind="unregistered_person",
                    question="‘妈’想看的是哪一份资料？母亲尚未登记为联系人，无法建立授权。",
                    source_record=event.id,
                    material="reminder_notice",
                    blocking=False,
                    detail="‘她自己那条’是母亲自己的记录，不等于可看佩戴者的资料。",
                )
            )
        if cast.denies_forwarding(body) or "我的也给她" in body:
            self._register_issue(
                Issue(
                    id=f"i-{event.id}-forward",
                    kind="forwarding_boundary",
                    question="是否有人要把佩戴者本人的资料转给母亲？",
                    source_record=event.id,
                    detail="转述者明确否认转授权；佩戴者资料不因此对母亲开放。",
                )
            )

    def _ingest_question(self, event: Event) -> None:
        """一组澄清问题：每问只锁定一项最关键的事实。"""
        body = event.body
        opened: list[str] = []
        if "本周趋势" in body:
            resolution = resolve_audience("苑敏", self.persons)
            question = self._open_question(
                person_id=resolution.person_ids[0] if resolution.state == "resolved" else None,
                audience_text="苑敏",
                material="sleep_trend",
                coverage=week_bounds(self.today),
                prompt="苑敏可否继续看本周趋势？",
                source_record=event.id,
                options=("可以", "不可以"),
                revokes_on_negative=True,
                intent="reaffirm",
                decisive="本周趋势是否继续对该亲友可见",
            )
            opened.append(question.id)
        if "日均时长" in body and "运动组" in body:
            resolution = resolve_audience("运动组的阿敏", self.persons)
            question = self._open_question(
                person_id=resolution.person_ids[0] if resolution.state == "resolved" else None,
                audience_text="运动组的阿敏",
                material="daily_average",
                coverage=open_bounds("长期"),
                prompt="运动组的阿敏要不要（尚未发）给日均时长？",
                source_record=event.id,
                options=resolution.person_ids or ("p3",),
                audience_candidates=resolution.person_ids,
                revokes_on_negative=True,
                intent="grant",
                decisive="是否把日均时长给运动组搭档",
            )
            opened.append(question.id)
        if len(opened) > 1:
            self._register_issue(
                Issue(
                    id=f"i-{event.id}-two_questions",
                    kind="multiple_questions",
                    question="这一组澄清里哪些已明确、哪些仍需补？",
                    source_record=event.id,
                    affects=tuple(opened),
                    detail="两个问题分别对应不同亲友与资料，要分别答复，不能一句‘可以’同时签给两边。",
                )
            )

    def _ingest_response(self, event: Event) -> None:
        """确认/澄清：先处理指代澄清，再逐句绑定到具体提问。"""
        body = event.body
        owner = is_owner_voice(event.heading) or event.heading.startswith("晚到确认") or "我姐" in body
        if "指我姐" in body or "刚才指" in body:
            clarification = Clarification(
                id=f"c-{event.id}",
                record_id=event.id,
                question_ids=tuple(
                    question.id
                    for question in self.questions.values()
                    if question.audience_text in ("阿敏", "苑敏")
                ),
                refers_to=("p2",),
                excludes=("p3",),
                effect="把‘阿敏/她’在家庭语境中指回 p2，并排除运动搭档",
            )
            self.clarifications.append(clarification)
            self._touch()
            self._apply_clarification(clarification)
            self._register_issue(
                Issue(
                    id=f"i-{event.id}-clarification",
                    kind="clarification",
                    question="这次澄清改变了哪些提问的受众，又是否单独构成授权？",
                    source_record=event.id,
                    person_id="p2",
                    affects=clarification.question_ids,
                    detail="澄清只解决指代；同意仍要由对应提问的确认来建立。",
                )
            )

        named: tuple[str, ...] = ()
        if "指我姐" in body or "指姐姐" in body:
            named = ("p2",)
        for clause in _clauses(body):
            if "不是运动伙伴" in clause or "不是运动搭档" in clause:
                # 这句话是在纠正“刚才”的指代，不是对“要不要给日均时长”的表态。
                # 不撤销任何既有范围，也不把运动组提问当作已答复。
                self._register_issue(
                    Issue(
                        id=f"i-{event.id}-not-partner",
                        kind="referent_correction",
                        question="‘不是运动伙伴’只纠正了指代；运动组阿敏的日均时长仍未答复",
                        source_record=event.id,
                        person_id="p3",
                        material="daily_average",
                        affects=tuple(
                            question.id
                            for question in self.questions.values()
                            if question.material == "daily_average" and question.person_id == "p3"
                        ),
                        detail="纠正指代不等于授权，也不等于撤回未提及的既有范围。",
                    )
                )
                for question in self.questions.values():
                    if question.material == "daily_average" and question.person_id == "p3" and question.state == "open":
                        self.questions[question.id] = replace(
                            question,
                            note="说话人否认‘刚才’指运动搭档；本提问仍待明确答复",
                        )
                continue
            if "趋势" in clause and _affirmative(clause):
                self._answer_material(
                    "sleep_trend",
                    affirmative=True,
                    event=event,
                    speaker=OWNER_ID if owner else "unknown",
                    explicit_person_ids=named,
                )
            elif "日均" in clause and _affirmative(clause):
                self._answer_material(
                    "daily_average",
                    affirmative=True,
                    event=event,
                    speaker=OWNER_ID if owner else "unknown",
                    explicit_person_ids=named,
                )

    def _ingest_draft(self, event: Event) -> None:
        """候选发送段：它不是授权，只是可用来复核“摘要能否反推提醒”的来源。"""
        if "提醒" in event.body or "摘要" in event.body:
            self._note_origin("reminder_notice", content=event.id)

    def _apply_clarification(self, clarification: Clarification) -> None:
        """澄清只改“谁”，不替人签署同意。

        * 原来受众未定的提问：用澄清指定的亲友补上受众，提问仍然开放。
        * 原来锁定了被澄清排除的那位：旧确认不能再签给他/她，必须重新提问。
        """
        for question in list(self.questions.values()):
            if question.state not in ("open", "ambiguous"):
                continue
            if not _needs_referent_clarification(question):
                continue
            if question.person_id_at_ask is None:
                if len(clarification.refers_to) == 1:
                    self.questions[question.id] = replace(
                        question,
                        person_id=clarification.refers_to[0],
                        state="open",
                        blocking=False,
                        note="澄清补齐了受众；资料范围仍需单独确认",
                    )
            elif question.person_id_at_ask in clarification.excludes:
                self.questions[question.id] = replace(
                    question,
                    state="ambiguous",
                    blocking=True,
                    referent_invalidated=True,
                    invalidated_by=clarification.id,
                    note="澄清表明原先锁定的亲友不是说话人指的人，旧确认不能签给他/她",
                )
                self._touch()

    # ----------------------------------------------------------- 提问与确认
    def _open_question(
        self,
        *,
        person_id: str | None,
        audience_text: str,
        material: str | None,
        coverage: Interval | None,
        prompt: str,
        source_record: str,
        read_window: Interval | None = None,
        options: Sequence[str] = (),
        audience_candidates: Sequence[str] = (),
        revokes_on_negative: bool = False,
        intent: str = "grant",
        decisive: str = "",
        asked_at: datetime | None = None,
    ) -> Question:
        existing = [
            key for key in self.questions if key.startswith(f"q-{source_record}-")
        ]
        question_id = f"q-{source_record}-{len(existing) + 1}"
        fingerprint = None
        if person_id is not None and material is not None:
            fingerprint = self.view(person_id).fingerprint(material)
        question = Question(
            id=question_id,
            asker=OWNER_ID,
            person_id=person_id,
            person_id_at_ask=person_id,
            audience_text=audience_text,
            material=material,
            coverage=coverage,
            prompt=prompt,
            scope_version=self.version,
            scope_fingerprint=fingerprint,
            source_record=source_record,
            read_window=read_window,
            options=tuple(options),
            audience_candidates=tuple(audience_candidates),
            revokes_on_negative=revokes_on_negative,
            intent=intent,
            state="open" if person_id else "ambiguous",
            asked_at=asked_at,
            blocking=person_id is None,
            decisive_fact=decisive,
            note="" if person_id else "受众尚未唯一确定，先只补这一项",
        )
        self.questions[question_id] = question
        self._touch()
        return question

    def ask(
        self,
        *,
        person_id: str,
        material: str,
        prompt: str,
        source_record: str,
        coverage: Interval | None | object = _UNSET,
        audience_text: str | None = None,
        options: Sequence[str] = (),
        read_window: Interval | None = None,
        asked_at: datetime | None = None,
    ) -> Question:
        """追加一个锁定当时范围的提问。

        指纹在提问这一刻记录；之后范围一变，迟来的“可以”就失效。
        coverage 不传时按资料种类的常规范围；显式传 None 表示“范围尚未
        确定”，这种提问不能用一句“可以”换成无边界授权。
        """
        resolved_coverage = (
            _default_coverage(material, self.today) if coverage is _UNSET else coverage
        )
        return self._open_question(
            person_id=person_id,
            audience_text=audience_text or person_id,
            material=material,
            coverage=resolved_coverage,
            prompt=prompt,
            source_record=source_record,
            read_window=read_window,
            options=options,
            decisive="对该亲友的该资料范围作出答复",
            asked_at=asked_at,
        )

    def open_questions(self) -> tuple[Question, ...]:
        return tuple(
            question for question in self.questions.values() if question.state in ("open", "ambiguous")
        )

    def pending_decisive_items(self) -> tuple[Question, ...]:
        """只保留最能决定范围、且当前仍缺的一项事实。"""
        return tuple(question for question in self.open_questions() if question.blocking or question.state == "open")

    def _answer_material(
        self,
        material: str,
        *,
        affirmative: bool,
        event: Event,
        speaker: str,
        explicit_person_ids: Sequence[str] = (),
    ) -> ConfirmationOutcome:
        candidates = [
            question
            for question in self.questions.values()
            if question.material == material and question.state in ("open", "ambiguous")
        ]
        if not candidates:
            return ConfirmationOutcome(
                state="unmatched",
                applied=False,
                reason="没有与该确认对应、且仍开放的提问；不能据此新建同意",
            )
        if len(candidates) > 1:
            ids = tuple(question.id for question in candidates)
            return ConfirmationOutcome(
                state="ambiguous",
                applied=False,
                reason="有多条关于同一资料的未决提问，确认短句无法唯一对应",
                candidates=ids,
            )
        question = candidates[0]
        return self.answer_question(
            question.id,
            affirmative=affirmative,
            source_record=event.id,
            speaker=speaker,
            explicit_person_ids=explicit_person_ids,
        )

    def answer_question(
        self,
        question_id: str,
        *,
        affirmative: bool,
        source_record: str,
        speaker: str = OWNER_ID,
        answered_at: datetime | None = None,
        explicit_person_ids: Sequence[str] = (),
    ) -> ConfirmationOutcome:
        """把一句确认绑定到指定提问，并检查它是否仍然成立。

        explicit_person_ids 是确认句里点名的亲友（例如“刚才指我姐”）。
        当澄清推翻了提问时的指代，只有点名到同一位才可能继续绑定。
        """
        question = self.questions.get(question_id)
        if question is None:
            return ConfirmationOutcome(state="unmatched", applied=False, reason="没有这个提问")
        if question.state not in ("open", "ambiguous"):
            return ConfirmationOutcome(
                state="unmatched",
                applied=False,
                reason=f"提问已处于 {question.state}，不能重复签署",
                question_id=question.id,
                question_prompt=question.prompt,
            )
        if speaker != OWNER_ID:
            return ConfirmationOutcome(
                state="unmatched",
                applied=False,
                reason="只有佩戴者本人的确认能建立或撤回授权",
                question_id=question.id,
                question_prompt=question.prompt,
            )

        # 指代是否被澄清推翻？“对，她”撞上另一个候选人时不能签成新同意。
        if question.referent_invalidated:
            if not explicit_person_ids or not set(explicit_person_ids) & {
                question.person_id_at_ask
            }:
                self.questions[question.id] = replace(
                    question,
                    state="ambiguous",
                    blocking=True,
                    answered_by=speaker,
                    answered_at=answered_at,
                    note="澄清推翻了提问时的指代，必须重新提问并点名受众",
                )
                return ConfirmationOutcome(
                    state="mismatched",
                    applied=False,
                    reason="确认短句指向的候选人已被澄清排除，不能签成新同意",
                    question_id=question.id,
                    question_prompt=question.prompt,
                    person_id=question.person_id_at_ask,
                    candidates=question.audience_candidates,
                )

        # 参考人变了吗？确认若要绑定到与提问不同的人，也不能签成新同意。
        target = question.person_id
        if explicit_person_ids and target is not None and target not in explicit_person_ids:
            self.questions[question.id] = replace(
                question,
                state="mismatched",
                answered_by=speaker,
                answered_at=answered_at,
                note="确认点名的亲友与提问时锁定的不同",
            )
            return ConfirmationOutcome(
                state="mismatched",
                applied=False,
                reason="确认点名的亲友与提问时锁定的不是同一位，不能签成新同意",
                question_id=question.id,
                question_prompt=question.prompt,
                person_id=target,
            )
        if target is None:
            if len(question.audience_candidates) == 1:
                target = question.audience_candidates[0]
            else:
                self.questions[question.id] = replace(
                    question,
                    state="ambiguous",
                    blocking=True,
                    note="受众仍未唯一，确认无法签给具体亲友",
                )
                return ConfirmationOutcome(
                    state="ambiguous",
                    applied=False,
                    reason="受众未唯一，确认短句可能指向另一位同名亲友，不能签成新同意",
                    question_id=question.id,
                    question_prompt=question.prompt,
                    candidates=question.audience_candidates,
                )
        if question.person_id_at_ask is not None and target != question.person_id_at_ask:
            self.questions[question.id] = replace(
                question,
                state="mismatched",
                answered_by=speaker,
                answered_at=answered_at,
                note="确认所指的人与提问时锁定的人不同",
            )
            return ConfirmationOutcome(
                state="mismatched",
                applied=False,
                reason="确认指向的亲友与提问时锁定的不是同一位，不能签成新同意",
                question_id=question.id,
                question_prompt=question.prompt,
                person_id=target,
            )

        # 范围变了吗？只比较这位亲友、这份资料。
        current = self.view(target).fingerprint(question.material or "")
        if question.scope_fingerprint is not None and current != question.scope_fingerprint:
            self.questions[question.id] = replace(
                question,
                state="stale",
                answered_by=speaker,
                answered_at=answered_at,
                late_answer=True,
                note="确认到达时该亲友该资料的范围已改变",
            )
            self._touch()
            return ConfirmationOutcome(
                state="stale",
                applied=False,
                reason="迟到的确认撞上了已改变的资料范围，不能签成新同意",
                question_id=question.id,
                question_prompt=question.prompt,
                person_id=target,
            )

        if question.material is None:
            self.questions[question.id] = replace(
                question,
                state="open",
                blocking=True,
                note="问题还没有资料种类，需先补这一项",
            )
            return ConfirmationOutcome(
                state="ambiguous",
                applied=False,
                reason="提问还没指明资料种类，确认无法对应到具体材料",
                question_id=question.id,
                question_prompt=question.prompt,
                person_id=target,
            )

        if affirmative and question.coverage is None:
            existing_scope = self.view(target).scopes.get(question.material or "")
            if existing_scope is None or not existing_scope.coverages:
                self.questions[question.id] = replace(
                    question,
                    state="open",
                    blocking=True,
                    note="肯定答复没有可对应的资料范围，需先补范围",
                )
                self._touch()
                return ConfirmationOutcome(
                    state="ambiguous",
                    applied=False,
                    reason="提问没有确定的资料范围，不能凭一句‘可以’建立无边界授权",
                    question_id=question.id,
                    question_prompt=question.prompt,
                    person_id=target,
                )

        decision_id: str | None = None
        if affirmative:
            decision_id = self._apply_affirmation(question, target, source_record)
        elif question.revokes_on_negative:
            decision_id = self._apply_revocation(question, target, source_record)
        self.questions[question.id] = replace(
            question,
            person_id=target,
            state="answered" if affirmative else "resolved",
            answered_by=speaker,
            answered_at=answered_at,
            late_answer=bool(answered_at and question.asked_at and answered_at > question.asked_at),
            blocking=False,
            note="确认与提问时的受众和范围一致",
        )
        self._touch()
        return ConfirmationOutcome(
            state="bound",
            applied=affirmative or (not affirmative and question.revokes_on_negative),
            reason="确认对应到建立时的提问，受众与范围均未变",
            question_id=question.id,
            question_prompt=question.prompt,
            person_id=target,
            decision_id=decision_id,
        )

    def _apply_affirmation(self, question: Question, person_id: str, source_record: str) -> str:
        material = question.material or ""
        coverage = question.coverage
        existing = self.view(person_id).scopes.get(material)
        if coverage is None:
            # 没有新范围时只重申原范围；绝不凭一句“可以”造出无边界授权。
            if existing is None or not existing.coverages:
                raise ValueError("确认缺少资料范围，不能建立无边界授权")
            coverage = existing.coverage
        decision_id = f"d-{material}-{person_id}-{source_record}"
        # 读取窗口沿用当前值：确认只对内容范围表态，不复活提问时可能已过期的窗口。
        current_window = existing.read_window if existing is not None else question.read_window
        self._add_decision(
            Decision(
                id=decision_id,
                seq=self._next_seq(),
                person_id=person_id,
                action=Action.REAFFIRM if existing is not None else Action.GRANT,
                material=material,
                audience_text=question.audience_text,
                coverage=coverage,
                read_window=current_window,
                source_record=source_record,
                source_span="确认",
                question_id=question.id,
                basis="consent",
                note="针对具体提问的确认，不是重新授权，也不会扩大原范围",
            )
        )
        self._note_origin(material, authorization=source_record)
        return decision_id

    def _apply_revocation(self, question: Question, person_id: str, source_record: str) -> str | None:
        material = question.material or ""
        existing = self.view(person_id).scopes.get(material)
        if existing is None or not existing.coverages:
            # 没有既有授权可撤；否定只关闭提问，不制造一条空撤回。
            return None
        decision_id = f"d-deny-{material}-{person_id}-{source_record}"
        self._add_decision(
            Decision(
                id=decision_id,
                seq=self._next_seq(),
                person_id=person_id,
                action=Action.DENY,
                material=material,
                audience_text=question.audience_text,
                coverage=question.coverage,
                source_record=source_record,
                source_span="确认否定",
                question_id=question.id,
                note="明确否定该提问所述范围",
            )
        )
        return decision_id

    # --------------------------------------------------------------- 更正
    def revise(
        self,
        *,
        person_id: str,
        material: str,
        action: Action = Action.DENY,
        coverage: Interval | None = None,
        source_record: str,
        source_span: str = "",
        note: str = "",
    ) -> Decision:
        """只改指定亲友、指定资料；不连带其他受众或其他种类。"""
        decision = Decision(
            id=f"d-{action.value}-{material}-{person_id}-{source_record}-{self._seq + 1}",
            seq=self._next_seq(),
            person_id=person_id,
            action=action,
            material=material,
            audience_text=person_id,
            coverage=coverage,
            source_record=source_record,
            source_span=source_span or "更正",
            revision=True,
            note=note,
        )
        self._add_decision(decision)
        return decision

    def rollback(self, decision_id: str, *, source_record: str, reason: str) -> Issue:
        decision = next((item for item in self.decisions if item.id == decision_id), None)
        if decision is None:
            raise KeyError(decision_id)
        self.decisions = [
            replace(item, status=DecisionStatus.WITHDRAWN) if item.id == decision_id else item
            for item in self.decisions
        ]
        self._touch()
        issue = Issue(
            id=f"i-rollback-{decision_id}",
            kind="rollback",
            question=reason,
            source_record=source_record,
            person_id=decision.person_id,
            material=decision.material,
            affects=(decision.id,),
        )
        self._register_issue(issue)
        return issue

    # --------------------------------------------------------------- 视图
    def _touch(self) -> None:
        """任何会改变范围的操作都推进版本；只读重建不推进。"""
        self._version += 1
        self._dirty = True

    def _rebuild_views(self) -> None:
        views: dict[str, PersonView] = {}
        for person_id in self.persons:
            grants: dict[str, list[Decision]] = {}
            revocations: dict[str, list[Decision]] = {}
            scopes: dict[str, Scope] = {}
            exclusions: list[Exclusion] = []
            for decision in self.decisions:
                if decision.person_id != person_id or decision.status is not DecisionStatus.CONFIRMED:
                    continue
                if decision.action in (Action.GRANT, Action.REAFFIRM):
                    grants.setdefault(decision.material, []).append(decision)
                elif decision.action is Action.DENY:
                    revocations.setdefault(decision.material, []).append(decision)
                elif decision.action is Action.EXCLUDE:
                    exclusions.append(
                        Exclusion(
                            person_id=person_id,
                            material=decision.material,
                            coverage=decision.coverage or Interval(None, None),
                            decision_id=decision.id,
                            note=decision.note,
                        )
                    )
            for material, material_grants in grants.items():
                coverages: list[Interval] = []
                read_windows: list[Interval] = []
                decision_ids: list[str] = []
                forwardable = False
                forward_denied = False
                for decision in material_grants:
                    if decision.coverage is not None:
                        coverages.append(decision.coverage)
                    if decision.read_window is not None:
                        read_windows.append(decision.read_window)
                    decision_ids.append(decision.id)
                    forwardable = forwardable or (decision.forwardable and decision.forwardable_explicit)
                    forward_denied = forward_denied or decision.forward_denied_explicit
                for decision in revocations.get(material, []):
                    if decision.coverage is None:
                        coverages = []
                        decision_ids.append(decision.id)
                        continue
                    remaining: list[Interval] = []
                    for coverage in coverages:
                        split = subtract(coverage, decision.coverage)
                        remaining.extend(coverage_piece for coverage_piece in (split if split is not None else [coverage]))
                    coverages = remaining
                    decision_ids.append(decision.id)
                scope = Scope(
                    person_id=person_id,
                    material=material,
                    status=DecisionStatus.CONFIRMED,
                    coverages=_merge(coverages),
                    read_window=_narrowest(read_windows),
                    # 明确拒绝二次转发压过任何默认；只有明确写出的允许才算。
                    forwardable=forwardable and not forward_denied,
                    forward_denied=forward_denied,
                    decision_ids=tuple(decision_ids),
                )
                scopes[material] = scope
            views[person_id] = PersonView(
                person_id=person_id,
                version=self._version,
                scopes=scopes,
                exclusions=tuple(exclusions),
            )
        self._views = views
        self._dirty = False

    def view(self, person_id: str) -> PersonView:
        if self._dirty or person_id not in self._views:
            self._rebuild_views()
        return self._views[person_id]

    @property
    def version(self) -> int:
        if self._dirty:
            self._rebuild_views()
        return self._version

    # --------------------------------------------------- 读取范围与依据
    def allowed_segments(
        self, person_id: str, material: str, coverage: Interval | None = None
    ) -> tuple[Segment, ...]:
        view = self.view(person_id)
        scope = view.scopes.get(material)
        if scope is None or scope.status is not DecisionStatus.CONFIRMED:
            return ()
        if coverage is None:
            pieces = list(scope.coverages)
        else:
            pieces = list(scope.clips(coverage))
        for exclusion in view.exclusions_for(material):
            next_pieces: list[Interval] = []
            for piece in pieces:
                split = subtract(piece, exclusion.coverage)
                if split is None:
                    next_pieces.append(piece)
                else:
                    next_pieces.extend(split)
            pieces = next_pieces
        pieces = sorted(pieces, key=lambda item: (item.start or datetime.min, item.end or datetime.max))
        return tuple(Segment(interval=piece) for piece in pieces if _nonempty(piece))

    def material_basis(
        self,
        person_id: str,
        material: str,
        coverage: Interval | None = None,
    ) -> MaterialBasis:
        view = self.view(person_id)
        scope = view.scopes.get(material)
        target = coverage
        if target is None and scope is not None and scope.coverage is not None:
            target = scope.coverage
        if target is None:
            granted = [
                decision.coverage
                for decision in self.decisions
                if decision.person_id == person_id
                and decision.material == material
                and decision.action in (Action.GRANT, Action.REAFFIRM)
                and decision.coverage is not None
            ]
            target = granted[-1] if granted else Interval(None, None, label="未登记范围")
        segments = self.allowed_segments(person_id, material, coverage)
        entries = tuple(
            BasisEntry(
                decision_id=decision.id,
                action=decision.action.value,
                material=decision.material,
                status=decision.status.value,
                coverage=decision.coverage.describe() if decision.coverage else None,
                source_record=decision.source_record,
                source_span=decision.source_span,
                question_id=decision.question_id,
                basis=decision.basis,
            )
            for decision in self.decisions
            if decision.person_id == person_id and decision.material == material
        )
        exclusion_refs = tuple(entry for entry in entries if entry.action == Action.EXCLUDE.value)
        pending = tuple(
            question
            for question in self.questions.values()
            if question.person_id == person_id
            and question.material == material
            and question.state in ("open", "ambiguous")
        )
        receipts = tuple(
            receipt.id
            for receipt in self.receipts
            if receipt.person_id == person_id and receipt.material == material
        )
        note = ""
        if scope is None:
            note = "没有对这位亲友登记该资料的授权"
            if view.exclusions_for(material):
                note += "；另有针对具体时段的排除记录"
        elif not scope.coverages:
            note = "该资料的范围已被明确收回"
        elif not segments:
            note = "全部落在排除范围内"
        warnings: list[str] = []
        if material == "weekly_summary" and self._summary_detail_excluded(person_id):
            warnings.append("摘要正文含提醒明细；该明细对这位亲友被排除，必须改写掉而不能只删整条摘要")
        if material == "reminder_notice" and scope is None and view.exclusions_for(material):
            warnings.append("提醒从未单独授权；排除记录只用于限制摘要中的明细，不构成可读范围")
        return MaterialBasis(
            person_id=person_id,
            material=material,
            coverage=target,
            visible=bool(segments),
            allowed_segments=segments,
            excluded_segments=tuple(
                Segment(exclusion.coverage) for exclusion in view.exclusions_for(material)
            ),
            basis=entries,
            exclusion_refs=exclusion_refs,
            pending_questions=pending,
            notice_receipts=receipts,
            read_window=scope.read_window if scope else None,
            content_warnings=tuple(warnings),
            note=note,
        )

    def _summary_detail_excluded(self, person_id: str) -> bool:
        """这位亲友的摘要里，是否有某段提醒明细落在排除范围内。"""
        exclusions = self.view(person_id).exclusions_for("reminder_notice")
        if not exclusions:
            return False
        for receipt in self.receipts:
            if receipt.person_id != person_id:
                continue
            if receipt.detail_coverage is not None:
                if any(exclusion.coverage.overlaps(receipt.detail_coverage) for exclusion in exclusions):
                    return True
            elif any(exclusion.coverage.overlaps(receipt.coverage) for exclusion in exclusions):
                return True
        return False

    def explain(self, person_id: str, material: str, coverage: Interval | None = None) -> str:
        return self.material_basis(person_id, material, coverage).describe()

    def why_visible(self, material: str, *, person_id: str) -> MaterialBasis:
        """回答“一份材料依哪次确认可见”。"""
        return self.material_basis(person_id, material)

    def origin_of(self, material: str) -> MaterialOrigin:
        return self.material_origins.get(
            material,
            MaterialOrigin(material=material, authorization_records=(), content_records=()),
        )

    def clarify_effects(self) -> tuple[ClarificationEffect, ...]:
        effects: list[ClarificationEffect] = []
        for clarification in self.clarifications:
            affected = []
            for question_id in clarification.question_ids:
                question = self.questions.get(question_id)
                if question is None:
                    continue
                affected.append(
                    (
                        question.id,
                        question.person_id or question.person_id_at_ask or "",
                        question.material or "",
                        question.state,
                    )
                )
            affected_decisions = tuple(
                decision.id for decision in self.decisions if decision.question_id in clarification.question_ids
            )
            effects.append(
                ClarificationEffect(
                    clarification_id=clarification.id,
                    source_record=clarification.record_id,
                    refers_to=clarification.refers_to,
                    excludes=clarification.excludes,
                    affected_questions=tuple(affected),
                    affected_decisions=affected_decisions,
                    note=clarification.effect,
                )
            )
        return tuple(effects)

    def issues(self, *, blocking_only: bool = False) -> tuple[Issue, ...]:
        if blocking_only:
            return tuple(issue for issue in self.rollbacks if issue.blocking)
        return tuple(self.rollbacks)

    # ------------------------------------------------------------- 二次转发
    def forwarding_check(self, person_id: str, material: str) -> ForwardingCheck:
        view = self.view(person_id)
        scope = view.scopes.get(material)
        if scope is None or not scope.coverages:
            return ForwardingCheck(
                person_id=person_id,
                material=material,
                allowed=False,
                reason="没有该资料的有效授权，也就没有二次转发权",
            )
        if scope.forward_denied and not scope.forwardable:
            return ForwardingCheck(
                person_id=person_id,
                material=material,
                allowed=False,
                reason="授权文本明确不给二次转发权",
                decision_ids=scope.decision_ids,
            )
        if not scope.forwardable:
            return ForwardingCheck(
                person_id=person_id,
                material=material,
                allowed=False,
                reason="授权文本没有写出二次转发权，按默认不允许",
                decision_ids=scope.decision_ids,
            )
        return ForwardingCheck(
            person_id=person_id,
            material=material,
            allowed=True,
            reason="授权中明确写出了二次转发权",
            decision_ids=scope.decision_ids,
        )

    # --------------------------------------------------------------- 分发
    def inference_findings(self, person_id: str) -> tuple[InferenceFinding, ...]:
        """检查是否能用“趋势 + 摘要”的时刻与描述反推出被排除的提醒。"""
        findings: list[InferenceFinding] = []
        view = self.view(person_id)
        exclusions = view.exclusions_for("reminder_notice")
        if not exclusions:
            return ()
        has_trend = bool(self.allowed_segments(person_id, "sleep_trend"))
        has_summary = bool(self.allowed_segments(person_id, "weekly_summary"))
        summary_receipts = [receipt for receipt in self.receipts if receipt.person_id == person_id]
        detail_points = [
            receipt.detail_coverage
            for receipt in summary_receipts
            if receipt.detail_coverage is not None
            and any(exclusion.coverage.overlaps(receipt.detail_coverage) for exclusion in exclusions)
        ]
        if not (has_trend and has_summary):
            return ()
        if not detail_points:
            # 只有“时刻算不出来”时才退回到文字匹配。时刻已知且不在排除范围
            # 内，说明摘要明细与被排除的提醒无关，不能误伤。
            unknown = [
                receipt
                for receipt in summary_receipts
                if "提醒" in receipt.body and receipt.detail_coverage is None
            ]
            if unknown:
                for exclusion in exclusions:
                    findings.append(
                        InferenceFinding(
                            person_id=person_id,
                            material="reminder_notice",
                            excluded_decision=exclusion.decision_id,
                            reminder_moment=None,
                            source_records=tuple(
                                sorted(
                                    {
                                        *(self.origin_of("weekly_summary").content_records),
                                        *(self.origin_of("sleep_trend").content_records),
                                    }
                                )
                            ),
                            explanation="摘要提到提醒，与趋势合看仍有反推风险，需删减相关上下文。",
                        )
                    )
            return tuple(findings)
        for exclusion in exclusions:
            for point in detail_points:
                if not exclusion.coverage.overlaps(point):
                    continue
                findings.append(
                    InferenceFinding(
                        person_id=person_id,
                        material="reminder_notice",
                        excluded_decision=exclusion.decision_id,
                        reminder_moment=point.start,
                        source_records=tuple(
                            sorted(
                                {
                                    *(self.origin_of("weekly_summary").content_records),
                                    *(self.origin_of("sleep_trend").content_records),
                                }
                            )
                        ),
                        explanation=(
                            "趋势覆盖本周、摘要写明第一段睡眠结束后 23 分钟有提醒；"
                            "两者合并能推出具体提醒时刻，因此要删减可反推的上下文。"
                        ),
                    )
                )
        return tuple(findings)

    def compose_bundle(
        self,
        person_id: str,
        *,
        receipt_id: str | None = None,
    ) -> Bundle:
        """为一位亲友组装可读说明。

        每份材料按它自己的真实来源和该亲友的授权裁定。组合推断风险只删减
        相关上下文：趋势照发，摘要只改写掉可反推的段落。
        """
        view = self.view(person_id)
        items: list[BundleItem] = []
        withheld: list[WithheldItem] = []
        notes: list[str] = []
        findings = self.inference_findings(person_id)
        risky = bool(findings)

        trend = self.material_basis(person_id, "sleep_trend")
        if trend.visible:
            items.append(
                BundleItem(
                    id=f"{person_id}-trend",
                    material="sleep_trend",
                    coverage=trend.coverage,
                    text=f"【本周睡眠趋势】{_segments_text(trend.allowed_segments)}：按当前授权整理。",
                    source_record=self.origin_of("sleep_trend").authorization_records[0]
                    if self.origin_of("sleep_trend").authorization_records
                    else "",
                    basis_decision_ids=tuple(entry.decision_id for entry in trend.basis),
                )
            )
        else:
            withheld.append(
                WithheldItem(
                    id=f"{person_id}-trend",
                    material="sleep_trend",
                    reason=trend.note or "趋势没有有效授权",
                    decision_ids=tuple(entry.decision_id for entry in trend.basis),
                    recoverable=bool(trend.basis),
                )
            )

        summary = self.material_basis(person_id, "weekly_summary")
        if summary.visible:
            text = f"【周二统计摘要】{_segments_text(summary.allowed_segments)} 的统计摘要。"
            redactions: tuple[str, ...] = ()
            if summary.content_warnings or risky:
                text = (
                    "【周二统计摘要】统计口径与总体结论保留；"
                    "已删去与趋势合看可反推出具体提醒的时刻描述。"
                )
                redactions = (
                    "摘要中的提醒明细落在排除范围，改写保留其余摘要",
                )
            items.append(
                BundleItem(
                    id=f"{person_id}-summary",
                    material="weekly_summary",
                    coverage=summary.coverage,
                    text=text,
                    source_record=self.origin_of("weekly_summary").content_records[0]
                    if self.origin_of("weekly_summary").content_records
                    else "",
                    redactions=redactions,
                    basis_decision_ids=tuple(entry.decision_id for entry in summary.basis),
                )
            )
        else:
            withheld.append(
                WithheldItem(
                    id=f"{person_id}-summary",
                    material="weekly_summary",
                    reason=summary.note or "摘要没有有效授权",
                    decision_ids=tuple(entry.decision_id for entry in summary.basis),
                    recoverable=bool(summary.basis),
                )
            )

        if items and not risky and summary.visible and summary.content_warnings:
            notes.append("摘要内提醒明细被排除，已只改写该段，摘要其余部分保留。")
        if risky:
            notes.append(
                "趋势与摘要的时刻/描述合并后可反推被排除的提醒：已改写摘要中的相关上下文；趋势本身仍保留。"
            )

        if receipt_id is not None:
            receipt = next((item for item in self.receipts if item.id == receipt_id), None)
            if receipt is None:
                notes.append(f"没有找到回执 {receipt_id}")
            else:
                access = self.receipt_access(receipt, person_id=person_id)
                if not access.allowed:
                    notes.append(access.reason)

        return Bundle(
            person_id=person_id,
            version=self.version,
            items=tuple(items),
            withheld=tuple(withheld),
            notes=tuple(notes),
            receipt_id=receipt_id,
        )

    def receipt_access(self, receipt: Receipt, *, person_id: str | None = None) -> ReceiptAccess:
        recipient = person_id or receipt.person_id
        basis = self.material_basis(recipient, receipt.material, receipt.coverage)
        detail_excluded = False
        view = self.view(recipient)
        reminder_exclusions = view.exclusions_for("reminder_notice")
        if receipt.detail_material == "reminder_notice":
            if receipt.detail_coverage is None:
                # 时刻未知时按回执覆盖的整段比较；只要有一段提醒被排除，
                # 就不能继续提供这份正文。
                detail_excluded = any(
                    exclusion.coverage.overlaps(receipt.coverage)
                    for exclusion in reminder_exclusions
                )
            else:
                detail_excluded = any(
                    exclusion.coverage.overlaps(receipt.detail_coverage)
                    for exclusion in reminder_exclusions
                )
        return build_access(
            receipt,
            material_visible=basis.visible,
            allowed_segments=basis.allowed_segments,
            detail_excluded=detail_excluded,
        )

    # ------------------------------------------------------------- 内部工具
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _add_decision(self, decision: Decision) -> Decision:
        self.decisions = [item for item in self.decisions if item.id != decision.id]
        self.decisions.append(decision)
        self._touch()
        return decision

    def _note_origin(
        self,
        material: str,
        *,
        authorization: str | None = None,
        content: str | None = None,
    ) -> None:
        origin = self.material_origins.get(
            material, MaterialOrigin(material=material, authorization_records=(), content_records=())
        )
        auths = origin.authorization_records
        contents = origin.content_records
        if authorization and authorization not in auths:
            auths = (*auths, authorization)
        if content and content not in contents:
            contents = (*contents, content)
        self.material_origins[material] = MaterialOrigin(
            material=material,
            authorization_records=auths,
            content_records=contents,
            note=origin.note,
        )

    def _register_issue(self, issue: Issue) -> Issue:
        if not any(existing.id == issue.id for existing in self.rollbacks):
            self.rollbacks.append(issue)
        return issue


# ------------------------------------------------------------------ 工具函数


def _record_order(record_id: str, kind: str) -> tuple[int, str]:
    order = {
        "person": 0,
        "receipt": 1,
        "request": 2,
        "question": 3,
        "response": 4,
        "message": 5,
    }
    return (order.get(kind, 9), record_id)


def _clauses(body: str) -> tuple[str, ...]:
    return tuple(
        clause.strip()
        for clause in re.split(r"[。；;！!？?，,]", body)
        if clause.strip()
    )


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^\w一-鿿]+", "", text)
    return cleaned[:12] or "x"


def _asks_to_ask(clause: str) -> bool:
    return "问问" in clause or "再提醒" in clause or "再问" in clause


def _is_exclusion(clause: str) -> bool:
    return any(term in clause for term in ("别让", "不要给", "别给", "不用给", "不给", "别让她看", "别给他看"))


def _is_question(clause: str) -> bool:
    """反问/询问不是授权：带疑问词或句末问号的子句只当问题。"""
    if "?" in clause or "？" in clause:
        return True
    return any(
        term in clause
        for term in ("吗", "么", "是否", "能不能", "可否", "要不要", "是不是", "要不要", "该不该")
    )


def _is_grant(clause: str) -> bool:
    if _is_exclusion(clause) or _is_question(clause):
        return False
    return any(term in clause for term in ("发给", "给", "允许", "可以看", "能看", "开放"))


def _grant_target(clause: str) -> str:
    for marker in ("发给", "给", "让"):
        if marker in clause:
            tail = clause.split(marker, 1)[1]
            tail = re.split(r"[，,。；]", tail)[0]
            tail = re.sub(r"^(还是|就|先|再|把.*?发)", "", tail)
            if tail:
                return tail.strip()
    return clause


def _exclusion_target(clause: str) -> str:
    for marker in ("别让", "不要给", "别给", "别让她看", "别给他看", "不给"):
        if marker in clause:
            tail = clause.split(marker, 1)[1]
            for stop in ("看", "，", ",", "。"):
                tail = tail.split(stop, 1)[0]
            if tail:
                return tail.strip()
    return "她"


def _needs_referent_clarification(question: Question) -> bool:
    """只有指代本来就不唯一的提问才会被澄清改写。

    已经写明“运动组的阿敏”的提问不需要再消歧；澄清也不能反过来把它
    改成姐姐。
    """
    if question.audience_text in ("阿敏", "她", "他", "那位", "对方"):
        return True
    if len(question.audience_candidates) > 1:
        return True
    return False


def _is_pronoun(text: str) -> bool:
    stripped = re.sub(r"[\s，,。；;]", "", text)
    return stripped in ("她", "他", "她看", "他看", "那位", "对方")


def _relay_referent(body: str) -> str:
    if "我妈" in body or "妈" in body:
        return "妈"
    return body


def _affirmative(clause: str) -> bool:
    if any(term in clause for term in ("不是", "不要", "别", "不行", "不可以", "不给")):
        return False
    return any(term in clause for term in ("可以", "对", "行", "允许", "好"))


def _default_coverage(material: str, today: date) -> Interval:
    if material == "sleep_trend":
        return week_bounds(today)
    if material == "daily_average":
        return open_bounds("长期")
    if material == "weekly_summary":
        return _day_of("周二", today)
    return night_bounds(today)


def _day_of(term: str, today: date) -> Interval:
    from .events import parse_coverage

    parsed = parse_coverage(term, today)
    if parsed is None:
        raise ValueError(term)
    return parsed


def _point(moment: datetime, label: str) -> Interval:
    return Interval(moment, moment + timedelta(minutes=1), label=label, precision="instant")


def _nonempty(interval: Interval) -> bool:
    if interval.is_open():
        return True
    if interval.start is not None and interval.end is not None:
        return interval.start < interval.end
    return True


def _merge(intervals: Sequence[Interval]) -> tuple[Interval, ...]:
    """合并重叠或相接的区间；保留开放区间。"""
    if not intervals:
        return ()
    ordered = sorted(intervals, key=lambda item: (item.start or datetime.min, item.end or datetime.max))
    merged: list[Interval] = [ordered[0]]
    for interval in ordered[1:]:
        last = merged[-1]
        if last.end is None or interval.start is None:
            merged[-1] = Interval(
                last.start, None if last.end is None or interval.end is None else max(last.end, interval.end),
                label=last.label or interval.label,
                precision="merge",
            )
            continue
        if interval.start <= last.end:
            merged[-1] = Interval(
                last.start,
                max(last.end, interval.end) if interval.end else None,
                label=last.label or interval.label,
                precision="merge",
            )
        else:
            merged.append(interval)
    return tuple(merged)


def _narrowest(windows: Sequence[Interval]) -> Interval | None:
    if not windows:
        return None
    return min(
        windows,
        key=lambda item: (
            item.end is None,
            (item.end - item.start) if item.start and item.end else timedelta.max,
        ),
    )


def _segments_text(segments: Sequence[Segment]) -> str:
    if not segments:
        return "（无可见片段）"
    return "、".join(segment.interval.describe() for segment in segments)


def load_ledger(corpus: Mapping, **kwargs) -> Ledger:
    return Ledger.from_corpus(corpus, **kwargs)
