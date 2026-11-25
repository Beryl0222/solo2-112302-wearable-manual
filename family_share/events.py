"""语料事件的轻量解析。

只识别文字里明确写出的内容；代词、昵称、未登记的称呼一律留给消歧层。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Mapping

from .model import Interval


WEEKDAY_INDEX = {
    "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6,
    "星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3, "星期五": 4, "星期六": 5, "星期日": 6,
}

MATERIAL_KEYWORDS = (
    ("daily_average", ("日均时长", "日均")),
    ("sleep_trend", ("睡眠趋势", "趋势")),
    ("weekly_summary", ("统计摘要", "摘要")),
    ("reminder_notice", ("提醒", "通知")),
)


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    order: int
    text: str
    written_at: datetime | None = None

    @property
    def body(self) -> str:
        return self.text.split("|", 1)[-1]

    @property
    def heading(self) -> str:
        return self.text.split("|", 1)[0]


def week_bounds(day: date) -> Interval:
    start = day - timedelta(days=day.weekday())
    return Interval(
        datetime.combine(start, time.min),
        datetime.combine(start + timedelta(days=7), time.min),
        label=f"本周（{start.isoformat()} 起七天）",
        precision="week",
    )


def day_bounds(day: date) -> Interval:
    return Interval(
        datetime.combine(day, time.min),
        datetime.combine(day + timedelta(days=1), time.min),
        label=f"{day.isoformat()} 当日",
        precision="day",
    )


def night_bounds(reference: date, *, which: str = "last") -> Interval:
    """“昨晚”或“今晚”覆盖的整晚。

    * 昨晚：前一日 18:00 至当日 12:00
    * 今晚：当日 18:00 至次日 12:00

    “今晚”是尚未结束的夜间，不能用它追溯隐藏昨夜的记录。
    """
    if which == "tonight":
        start = reference
        label = f"{reference} 夜间"
    else:
        start = reference - timedelta(days=1)
        label = f"{start} 夜间"
    return Interval(
        datetime.combine(start, time(18, 0)),
        datetime.combine(start + timedelta(days=1), time(12, 0)),
        label=label,
        precision="night",
    )


def open_bounds(label: str = "长期/不设边界") -> Interval:
    return Interval(None, None, label=label, precision="open")


def classify_material(text: str) -> str | None:
    for material, words in MATERIAL_KEYWORDS:
        if any(word in text for word in words):
            return material
    return None


def parse_coverage(text: str, reference: date, material: str | None = None) -> Interval | None:
    """识别资料本身覆盖的时段，而不是“什么时候可以读”。"""
    if any(term in text for term in ("本周", "这周", "这一周")):
        return week_bounds(reference)
    for term in WEEKDAY_INDEX:
        if term in text:
            day = _past_weekday(term, reference)
            return day_bounds(day)
    if "今晚" in text and "昨晚" not in text:
        return night_bounds(reference, which="tonight")
    if any(term in text for term in ("昨晚", "当天夜里", "夜里")):
        return night_bounds(reference)
    if "长期" in text:
        return open_bounds()
    if material == "sleep_trend":
        return week_bounds(reference)
    if material == "daily_average":
        return open_bounds()
    if material == "weekly_summary":
        return day_bounds(reference - timedelta(days=1))
    if material == "reminder_notice":
        return night_bounds(reference)
    return None


def _past_weekday(term: str, reference: date) -> date:
    target = WEEKDAY_INDEX[term]
    delta = reference.weekday() - target
    if delta <= 0:
        delta += 7
    return reference - timedelta(days=delta)


_CLOCK = re.compile(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})")


def _timestamp(day: date, clock: str) -> datetime:
    match = _CLOCK.fullmatch(clock)
    if not match:
        raise ValueError(f"无法解析时刻：{clock}")
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    if hour >= 24:
        return datetime.combine(day + timedelta(days=1), time(hour - 24, minute))
    return datetime.combine(day, time(hour, minute))


def parse_sleep_end(text: str, written_day: date) -> datetime | None:
    """解析“昨晚第一段睡眠 23:12 结束”。

    “昨晚”的钟点属于写出日期的前一天；没有钟点就不猜。
    """
    match = re.search(r"(?P<clock>\d{1,2}:\d{2})\s*结束", text)
    if not match:
        return None
    day = written_day
    if "昨晚" in text or "夜里" in text:
        day = written_day - timedelta(days=1)
    return _timestamp(day, match.group("clock"))


def parse_minutes_after(text: str) -> tuple[int | None, str | None]:
    match = re.search(r"距(?P<anchor>[^0-9，。；|]{1,24}?)\s*(?P<minutes>\d+)\s*分钟有提醒", text)
    if not match:
        return None, None
    return int(match.group("minutes")), match.group("anchor")


def is_owner_voice(heading: str) -> bool:
    return heading in ("苑云新文字", "佩戴者新文字", "佩戴者", "本人")


def is_relay(heading: str) -> bool:
    return heading in ("转述", "家人转述", "代传")


def event_day(event: Event, default: date) -> date:
    return event.written_at.date() if event.written_at else default


def build_events(corpus: Mapping, timestamps: Mapping[str, datetime] | None = None) -> dict[str, Event]:
    timestamps = timestamps or {}
    events = {}
    for order, record in enumerate(corpus["records"]):
        events[record["id"]] = Event(
            id=record["id"],
            kind=record["kind"],
            order=order,
            text=record["text"],
            written_at=timestamps.get(record["id"]),
        )
    return events
