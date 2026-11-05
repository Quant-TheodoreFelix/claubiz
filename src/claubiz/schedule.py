import re
from dataclasses import dataclass
from datetime import datetime, timedelta

DAY_NAMES = "월화수목금토일"
WEEKDAYS = (0, 1, 2, 3, 4)
WEEKEND = (5, 6)
EVERY_RE = re.compile(r"^(?:매\s*)?(\d{1,3})\s*시간\s*(?:마다|간격)?$")
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
            return f"{self.every_hours}시간마다"
        if self.days == tuple(range(7)):
            prefix = "매일"
        elif self.days == WEEKDAYS:
            prefix = "평일"
        elif self.days == WEEKEND:
            prefix = "주말"
        else:
            prefix = "매주 " + ",".join(DAY_NAMES[d] for d in self.days)
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
        raise ScheduleError("다음 실행 시각을 계산할 수 없음")

    def nominal_gap(self) -> timedelta:
        if self.every_hours:
            return timedelta(hours=self.every_hours)
        per_week = len(self.days) * len(self.times)
        return timedelta(days=7) / max(per_week, 1)


def _parse_time(token: str) -> tuple[int, int]:
    match = CLOCK_RE.fullmatch(token)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        match = HOUR_RE.fullmatch(token)
        if not match:
            raise ScheduleError(f"시각 형식을 알 수 없음: {token} (예: 09:00, 오후 3시)")
        half, hour = match.group(1), int(match.group(2))
        minute = 30 if token.endswith("반") else int(match.group(3) or 0)
        if half and not 1 <= hour <= 12:
            raise ScheduleError(f"오전/오후 뒤에는 1~12시를 써야 함: {token}")
        if half == "오후" and hour != 12:
            hour += 12
        elif half == "오전" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"올바른 시각이 아님: {token}")
    return hour, minute


def _parse_days(token: str) -> tuple[int, ...] | None:
    if token == "매일":
        return tuple(range(7))
    if token == "평일":
        return WEEKDAYS
    if token == "주말":
        return WEEKEND
    cleaned = token.replace("요일", "").replace("매주", "")
    cleaned = re.sub(r"[,·/]", "", cleaned)
    if not cleaned or any(ch not in DAY_NAMES for ch in cleaned):
        return None
    return tuple(sorted({DAY_NAMES.index(ch) for ch in cleaned}))


def parse_schedule(text: str) -> Schedule:
    raw = " ".join(text.strip().split())
    if not raw:
        raise ScheduleError("일정을 입력해야 함 (예: 매일 09:00, 평일 08:30, 월,목 10:00, 6시간마다)")
    if len(raw) > 80:
        raise ScheduleError("일정은 80자 이하로 적어야 함")
    compact = raw.replace(" ", "")
    every = EVERY_RE.fullmatch(compact)
    if every:
        hours = int(every.group(1))
        if not 1 <= hours <= MAX_EVERY_HOURS:
            raise ScheduleError(f"반복 간격은 1~{MAX_EVERY_HOURS}시간이어야 함")
        return Schedule(every_hours=hours)
    normalized = re.sub(r"(오전|오후)\s+", r"\1", raw)
    normalized = re.sub(r"(\d)\s+시", r"\1시", normalized)
    normalized = re.sub(r"시\s+(\d)", r"시\1", normalized)
    normalized = re.sub(r"(\d)\s+분", r"\1분", normalized)
    tokens = [t for t in re.split(r"[\s,]+", normalized) if t]
    days: set[int] = set()
    times: list[tuple[int, int]] = []
    for token in tokens:
        if token == "매주":
            continue
        parsed_days = _parse_days(token)
        if parsed_days is not None:
            days.update(parsed_days)
            continue
        times.append(_parse_time(token))
    if not times:
        raise ScheduleError("실행 시각을 적어야 함 (예: 매일 09:00)")
    unique_times = tuple(sorted(set(times)))
    if len(unique_times) > MAX_TIMES:
        raise ScheduleError(f"하루 실행 시각은 최대 {MAX_TIMES}개")
    return Schedule(days=tuple(sorted(days)) or tuple(range(7)), times=unique_times)


def next_run(text: str, after: datetime) -> datetime:
    return parse_schedule(text).next_after(after)
