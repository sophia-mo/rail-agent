"""One-shot start time in Asia/Shanghai; no background scheduler installation."""
from datetime import datetime
from zoneinfo import ZoneInfo
import re
import time

SHANGHAI = ZoneInfo('Asia/Shanghai')


def parse_start_at(value, now=None):
    now = now or datetime.now(SHANGHAI)
    if not isinstance(value, str):
        raise ValueError('start_at 必须是时间字符串或 null')
    if re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?', value):
        value = now.astimezone(SHANGHAI).date().isoformat() + 'T' + value
    elif not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', value):
        raise ValueError('start_at 使用 HH:MM[:SS] 或 YYYY-MM-DD HH:MM:SS')
    try:
        target = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('start_at 不是有效的日期时间') from exc
    if target.tzinfo is None:
        target = target.replace(tzinfo=SHANGHAI)
    return target.astimezone(SHANGHAI)


def wait_until(target, sleep=time.sleep, now=None):
    now = now or (lambda: datetime.now(SHANGHAI))
    next_report = 0
    while True:
        current = now()
        remaining = (target - current).total_seconds()
        if remaining <= 0:
            return
        if current.timestamp() >= next_report:
            print(f'定时等待：距离 {target:%Y-%m-%d %H:%M:%S}（北京时间）还有 {remaining:.1f} 秒。', flush=True)
            next_report = current.timestamp() + 30
        sleep(min(remaining, 30 if remaining > 5 else 0.05))
