from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# DOM column prefix and order-form option value. No implicit seat upgrades.
SEATS = {"商务座": ("SWZ", "9"), "一等座": ("ZY", "M"), "二等座": ("ZE", "O")}


@dataclass(frozen=True)
class Trip:
    origin: str
    destination: str
    date_start: str
    date_end: str
    passengers: list[str]
    time_start: str = "00:00"
    time_end: str = "23:59"
    trains: list[str] = field(default_factory=list)
    seat: str = "二等座"
    student: bool = False
    prefer_f: bool = True
    prefer_quiet: bool = False
    poll_seconds: int = 30
    timeout_minutes: int = 60

    def validate(self, today=None):
        today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
        start, end = date.fromisoformat(self.date_start), date.fromisoformat(self.date_end)
        if not today <= start <= end or (end - start).days > 14:
            raise ValueError("日期必须从今天或以后开始，结束日期不能早于开始，跨度不超过 15 天")
        for value in (self.time_start, self.time_end):
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                raise ValueError("发车时间必须为 HH:MM")
        if self.time_start > self.time_end:
            raise ValueError("每天的时间段不能跨午夜；请拆分任务")
        if not self.origin.strip() or not self.destination.strip() or self.origin == self.destination:
            raise ValueError("请填写不同的出发站和到达站全名")
        if self.seat not in SEATS:
            raise ValueError("席别支持：商务座、一等座、二等座")
        if type(self.student) is not bool or type(self.prefer_f) is not bool:
            raise ValueError("student / prefer_f 必须是 JSON 布尔值")
        if type(self.prefer_quiet) is not bool:
            raise ValueError("prefer_quiet 必须是 JSON 布尔值")
        if not isinstance(self.passengers, list) or not 1 <= len(self.passengers) <= 5:
            raise ValueError("请指定 1 至 5 名已有乘车人")
        if any(not isinstance(p, str) or not p.strip() for p in self.passengers) or len(set(self.passengers)) != len(self.passengers):
            raise ValueError("乘车人姓名不能为空或重复；同名乘车人需手动处理")
        if not isinstance(self.trains, list) or any(not re.fullmatch(r"[A-Z]?\d+", t) for t in self.trains):
            raise ValueError("车次应为 G123 等大写车次号列表")
        if type(self.poll_seconds) is not int or self.poll_seconds < 15:
            raise ValueError("查询间隔至少 15 秒")
        if type(self.timeout_minutes) is not int or not 1 <= self.timeout_minutes <= 1440:
            raise ValueError("监控时长应为 1 至 1440 分钟")
        return self

    def dates(self):
        current = date.fromisoformat(self.date_start)
        while current <= date.fromisoformat(self.date_end):
            yield current.isoformat()
            current += timedelta(days=1)

    def matches(self, train, departure):
        return (not self.trains or train in self.trains) and self.time_start <= departure <= self.time_end


def enough_tickets(value, count):
    value = value.strip()
    return value == "有" or (value.isdecimal() and int(value) >= count)
