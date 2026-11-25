"""联系人登记、称谓与受众消歧。

关键约束：
* 同名不等于同一人。“阿敏”在家庭语境是 p2，在运动组语境是 p3。
* 沉默、反问、转述、请求“再问问”都不构成授权；只有佩戴者本人明确说出的
  同意才算。
* “妈”在当前登记里没有对应联系人，不能把母亲的需求安到姐姐身上。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .model import AudienceResolution, Person


PERSON_LINE = re.compile(r"^(?P<label>[^|]+)\|(?P<name>[^，。]+)，(?P<rest>.+)$")

OWNER_ID = "p1"

UNREGISTERED_REFERENCES = {
    "妈": "母亲没有登记为联系人",
    "母亲": "母亲没有登记为联系人",
    "妈妈": "母亲没有登记为联系人",
    "爸": "父亲没有登记为联系人",
    "父亲": "父亲没有登记为联系人",
}


@dataclass(frozen=True)
class AliasEntry:
    surface: str
    person_id: str
    context: str  # family | activity | neutral
    kind: str = "name"  # name | kin | nickname | role


def parse_persons(corpus: Mapping) -> dict[str, Person]:
    persons: dict[str, Person] = {}
    for record in corpus["records"]:
        if record["kind"] != "person":
            continue
        match = PERSON_LINE.match(record["text"])
        if not match:
            raise ValueError(f"联系人记录格式无法解析：{record['id']}")
        label = match.group("label")
        name = match.group("name")
        rest = match.group("rest")
        nicknames = re.findall(r"(?:昵称|同样叫|也叫)(阿敏|[一-龥]{1,3})", rest)
        persons[record["id"]] = Person(
            id=record["id"],
            label=label,
            relation=rest,
            aliases=tuple(dict.fromkeys([name, *nicknames])),
            is_owner=label == "佩戴者",
            is_family="家庭" in rest and "家庭以外" not in rest,
            registered=True,
            source_record=record["id"],
        )
    return persons


def build_alias_table(persons: Mapping[str, Person]) -> tuple[AliasEntry, ...]:
    entries: list[AliasEntry] = []
    if OWNER_ID in persons:
        entries.append(AliasEntry("我", OWNER_ID, "neutral", "kin"))
        entries.append(AliasEntry("我自己的", OWNER_ID, "neutral", "kin"))
    for person in persons.values():
        for name in person.aliases:
            context = "neutral"
            if name == "阿敏":
                context = "family" if person.is_family else "activity"
            entries.append(AliasEntry(name, person.id, context, "name"))
        if person.is_family and person.id != OWNER_ID:
            for kin in ("我姐", "姐姐", "妹", "我妹"):
                entries.append(AliasEntry(kin, person.id, "family", "kin"))
        if not person.is_family:
            for role in ("运动伙伴", "运动搭档", "搭档", "队友"):
                entries.append(AliasEntry(role, person.id, "activity", "role"))
    return tuple(entries)


CONTEXT_HINTS = {
    "family": ("我姐", "姐姐", "家里", "妈", "母亲", "家人", "家庭"),
    "activity": ("运动", "搭档", "跑步", "球", "运动组", "队友", "伙伴"),
}


def detect_context(text: str) -> str:
    contexts = {
        name for name, hints in CONTEXT_HINTS.items() if any(hint in text for hint in hints)
    }
    if len(contexts) == 1:
        return next(iter(contexts))
    return "neutral"


def unregistered_reference(text: str) -> str | None:
    for surface, reason in UNREGISTERED_REFERENCES.items():
        if re.search(rf"(^|[^你他她]){surface}(说|想|要|看|的|问|$)", text):
            return reason
    return None


def resolve_audience(
    text: str,
    persons: Mapping[str, Person],
    *,
    speaker: str = OWNER_ID,
    table: Iterable[AliasEntry] | None = None,
) -> AudienceResolution:
    """把一个受众短语解析成候选联系人；不唯一时不取第一个候选。"""
    compact = re.sub(r"\s+", "", text)
    table = tuple(table if table is not None else build_alias_table(persons))

    reason = unregistered_reference(compact)
    if reason:
        return AudienceResolution(text, (), "unresolved", reason)

    # 全名最先于其余称谓判定。
    for person in persons.values():
        for name in person.aliases:
            if len(name) >= 2 and name in compact and name != "阿敏":
                return AudienceResolution(text, (person.id,), "resolved", f"全名 {name}")

    if any(term in compact for term in ("我姐", "姐姐")):
        sisters = [
            person.id
            for person in persons.values()
            if person.is_family and person.id != speaker
        ]
        if len(sisters) == 1:
            return AudienceResolution(text, (sisters[0],), "resolved", "称谓‘我姐’与登记的家庭关系")
        if not sisters:
            return AudienceResolution(text, (), "unresolved", "没有登记姐姐")
        return AudienceResolution(text, tuple(sorted(sisters)), "ambiguous", "有多位家庭联系人")

    if any(term in compact for term in ("运动组", "运动伙伴", "运动搭档", "搭档", "队友")):
        partners = tuple(sorted(person.id for person in persons.values() if not person.is_family and person.id != speaker))
        if len(partners) == 1:
            return AudienceResolution(text, partners, "resolved", "运动语境限定的联系人")
        if not partners:
            return AudienceResolution(text, (), "unresolved", "没有登记运动搭档")
        return AudienceResolution(text, partners, "ambiguous", "有多位运动组联系人")

    if "阿敏" in compact:
        candidates = tuple(
            sorted(
                person.id
                for person in persons.values()
                if "阿敏" in person.aliases and person.id != speaker
            )
        )
        if len(candidates) == 1:
            return AudienceResolution(text, candidates, "resolved", "唯一昵称")
        return AudienceResolution(
            text,
            candidates,
            "ambiguous",
            "家庭与运动组各有一位‘阿敏’，单凭昵称无法确定",
        )

    if re.search(r"(^|[^你他她])她", compact) or re.search(r"(^|[^你他她])他", compact):
        return AudienceResolution(text, (), "unresolved", "代词缺少明确指代")

    if ("我的" in compact or "我自己" in compact) and speaker == OWNER_ID:
        return AudienceResolution(text, (OWNER_ID,), "resolved", "说话者本人")

    if "大家" in compact or "家里" in compact:
        family = tuple(sorted(person.id for person in persons.values() if person.is_family))
        return AudienceResolution(text, family, "ambiguous", "泛称未逐一指名")

    return AudienceResolution(text, (), "unresolved", "没有可解析的受众")


def mentions_forwarding(text: str) -> bool:
    return any(term in text for term in ("转发", "给别人", "也给她", "也给他", "转给"))


def denies_forwarding(text: str) -> bool:
    """明确否认/拒绝二次转发。"""
    return any(
        term in text
        for term in (
            "不给二次转发",
            "不能转发",
            "不许转发",
            "别转发",
            "不要转发",
            "没说可以",
            "我可没说",
        )
    )


def grants_forwarding(text: str) -> bool:
    """只有明确写出的二次转发授权才算数。"""
    if denies_forwarding(text):
        return False
    return any(term in text for term in ("允许二次转发", "可以再次转发", "可再次转发"))


def deny_forward(text: str) -> bool:
    return denies_forwarding(text)


def negated(text: str, term: str) -> bool:
    """判断 term 在文本中是否出现在否定语境附近。"""
    for match in re.finditer(re.escape(term), text):
        window = text[max(0, match.start() - 4): match.end() + 2]
        if any(marker in window for marker in ("不", "别", "未", "没")):
            return True
    return False
