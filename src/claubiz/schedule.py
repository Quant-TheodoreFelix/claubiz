import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .i18n import language, tr

DAY_NAMES = "월화수목금토일"
EN_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
EN_DAY_WORDS = {
    **{name: index for index, name in enumerate(EN_DAYS)},
    "tues": 1, "weds": 2, "thur": 3, "thurs": 3,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "mondays": 0, "tuesdays": 1, "wednesdays": 2, "thursdays": 3, "fridays": 4, "saturdays": 5, "sundays": 6,
}
EN_ALL_DAYS = frozenset({"daily", "everyday"})
EN_WEEKDAYS = frozenset({"weekday", "weekdays"})
EN_WEEKEND = frozenset({"weekend", "weekends"})
EN_FILLER = frozenset({"every", "at", "on", "and"})
WEEKDAYS = (0, 1, 2, 3, 4)
WEEKEND = (5, 6)
EVERY_RE = re.compile(r"^(?:매\s*)?(\d{1,3})\s*시간\s*(?:마다|간격)?$")
EN_EVERY_RE = re.compile(r"^(?:every)?(\d{1,3})(?:h|hr|hrs|hour|hours)$")
AMPM_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?(am|pm)$")
CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
HOUR_RE = re.compile(r"^(오전|오후)?(\d{1,2})시(?:(\d{1,2})분|반)?$")
MAX_TIMES = 6
MAX_EVERY_HOURS = 168


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    days: tuple[int, ...] = ()
    times: tuple[tuple[int, int], ...] = ()
    every_hours: int = 0

    def describe(self) -> str:
        if self.every_hours:
            return tr("{hours}시간마다", hours=self.every_hours)
        if self.days == tuple(range(7)):
            prefix = tr("매일")
        elif self.days == WEEKDAYS:
            prefix = tr("평일")
        elif self.days == WEEKEND:
            prefix = tr("주말")
        else:
            names = DAY_NAMES if language() == "ko" else EN_DAYS
            prefix = tr("매주 {days}", days=",".join(names[d] for d in self.days))
        return f"{prefix} " + ", ".join(f"{h:02d}:{m:02d}" for h, m in self.times)

    def next_after(self, moment: datetime) -> datetime:
        base = moment.replace(second=0, microsecond=0)
        if self.every_hours:
            return base + timedelta(hours=self.every_hours)
        for offset in range(9):
            day = base.date() + timedelta(days=offset)
            if day.weekday() not in self.days:
                continue
            for hour, minute in self.times:
                candidate = datetime(day.year, day.month, day.day, hour, minute)
                if candidate > moment:
                    return candidate
        raise ScheduleError(tr("다음 실행 시각을 계산할 수 없음"))

    def nominal_gap(self) -> timedelta:
        if self.every_hours:
            return timedelta(hours=self.every_hours)
        per_week = len(self.days) * len(self.times)
        return timedelta(days=7) / max(per_week, 1)


def _parse_time(token: str) -> tuple[int, int]:
    match = CLOCK_RE.fullmatch(token)
    ampm = AMPM_RE.fullmatch(token.lower())
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    elif ampm:
        hour, minute = int(ampm.group(1)), int(ampm.group(2) or 0)
        if not 1 <= hour <= 12:
            raise ScheduleError(tr("am/pm 앞에는 1~12시를 써야 함: {token}", token=token))
        if ampm.group(3) == "pm" and hour != 12:
            hour += 12
        elif ampm.group(3) == "am" and hour == 12:
            hour = 0
    else:
        match = HOUR_RE.fullmatch(token)
        if not match:
            raise ScheduleError(tr("시각 형식을 알 수 없음: {token} (예: 09:00, 오후 3시)", token=token))
        half, hour = match.group(1), int(match.group(2))
        minute = 30 if token.endswith("반") else int(match.group(3) or 0)
        if half and not 1 <= hour <= 12:
            raise ScheduleError(tr("오전/오후 뒤에는 1~12시를 써야 함: {token}", token=token))
        if half == "오후" and hour != 12:
            hour += 12
        elif half == "오전" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(tr("올바른 시각이 아님: {token}", token=token))
    return hour, minute


def _parse_english_days(token: str) -> tuple[int, ...] | None:
    word = token.lower()
    if word in EN_ALL_DAYS:
        return tuple(range(7))
    if word in EN_WEEKDAYS:
        return WEEKDAYS
    if word in EN_WEEKEND:
        return WEEKEND
    if word in EN_DAY_WORDS:
        return (EN_DAY_WORDS[word],)
    return None


def _parse_days(token: str) -> tuple[int, ...] | None:
    if token == "매일":
        return tuple(range(7))
    if token == "평일":
        return WEEKDAYS
    if token == "주말":
        return WEEKEND
    english = _parse_english_days(token)
    if english is not None:
        return english
    cleaned = token.replace("요일", "").replace("매주", "")
    cleaned = re.sub(r"[,·/]", "", cleaned)
    if not cleaned or any(ch not in DAY_NAMES for ch in cleaned):
        return None
    return tuple(sorted({DAY_NAMES.index(ch) for ch in cleaned}))


def parse_schedule(text: str) -> Schedule:
    raw = " ".join(text.strip().split())
    if not raw:
        raise ScheduleError(tr("일정을 입력해야 함 (예: 매일 09:00, 평일 08:30, 월,목 10:00, 6시간마다)"))
    if len(raw) > 80:
        raise ScheduleError(tr("일정은 80자 이하로 적어야 함"))
    compact = raw.replace(" ", "")
    every = EVERY_RE.fullmatch(compact) or EN_EVERY_RE.fullmatch(compact.lower())
    if every:
        hours = int(every.group(1))
        if not 1 <= hours <= MAX_EVERY_HOURS:
            raise ScheduleError(tr("반복 간격은 1~{limit}시간이어야 함", limit=MAX_EVERY_HOURS))
        return Schedule(every_hours=hours)
    normalized = re.sub(r"(오전|오후)\s+", r"\1", raw)
    normalized = re.sub(r"(\d)\s+시", r"\1시", normalized)
    normalized = re.sub(r"시\s+(\d)", r"시\1", normalized)
    normalized = re.sub(r"(\d)\s+분", r"\1분", normalized)
    normalized = re.sub(r"(\d)\s+(am|pm)\b", r"\1\2", normalized, flags=re.IGNORECASE)
    tokens = [t for t in re.split(r"[\s,]+", normalized) if t]
    days: set[int] = set()
    times: list[tuple[int, int]] = []
    weekly = False
    for token in tokens:
        if token == "매주" or token.lower() == "weekly":
            weekly = True
            continue
        if token.lower() in EN_FILLER:
            continue
        parsed_days = _parse_days(token)
        if parsed_days is not None:
            days.update(parsed_days)
            continue
        times.append(_parse_time(token))
    if weekly and not days:
        raise ScheduleError(tr("매주 실행하려면 요일을 함께 적어야 함 (예: 매주 월 09:00)"))
    if not times:
        raise ScheduleError(tr("실행 시각을 적어야 함 (예: 매일 09:00)"))
    unique_times = tuple(sorted(set(times)))
    if len(unique_times) > MAX_TIMES:
        raise ScheduleError(tr("하루 실행 시각은 최대 {limit}개", limit=MAX_TIMES))
    return Schedule(days=tuple(sorted(days)) or tuple(range(7)), times=unique_times)


def next_run(text: str, after: datetime) -> datetime:
    return parse_schedule(text).next_after(after)
