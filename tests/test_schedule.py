import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from rail_agent.browser import Booker
from rail_agent.config import Trip
from rail_agent.schedule import SHANGHAI, parse_start_at, wait_until


class ScheduleTests(unittest.TestCase):
    def test_time_only_means_today_in_shanghai(self):
        now = datetime(2026, 9, 23, 15, tzinfo=SHANGHAI)
        self.assertEqual(parse_start_at('16:30', now), now.replace(hour=16, minute=30))
        self.assertEqual(parse_start_at('2026-09-23T08:30:00+00:00', now), now.replace(hour=16, minute=30))

    def test_invalid_schedule(self):
        for value in ('25:00', 'tomorrow', '2026-09-23', 123):
            with self.assertRaises(ValueError):
                parse_start_at(value)

    def test_wait_does_not_fire_early(self):
        clock = [datetime(2026, 9, 23, 16, 29, tzinfo=SHANGHAI)]
        target = clock[0] + timedelta(seconds=60)
        sleeps = []
        def advance(seconds):
            sleeps.append(seconds)
            clock[0] += timedelta(seconds=seconds)
        wait_until(target, sleep=advance, now=lambda: clock[0])
        self.assertGreaterEqual(clock[0], target)
        self.assertLessEqual(max(sleeps), 30)
        wait_until(target - timedelta(seconds=1), sleep=lambda _: self.fail('past target must not sleep'), now=lambda: clock[0])

    def test_monitor_budget_starts_after_scheduled_wait(self):
        trip = Trip(origin='北京南', destination='上海虹桥', date_start='2099-10-01',
                    date_end='2099-10-01', passengers=['测试'], start_at='2099-09-23 16:30:00')
        events = []
        with tempfile.TemporaryDirectory() as directory:
            booker = Booker(Mock(), trip, directory)
            booker.prepare_query = Mock(side_effect=lambda _: events.append('prepare'))
            booker.query = Mock(return_value=(Mock(), 'G1'))
            booker.order = Mock(return_value='done')
            with patch('rail_agent.schedule.wait_until', side_effect=lambda *a, **kw: events.append('wait')), \
                 patch('rail_agent.browser.time.monotonic', side_effect=lambda: events.append('budget') or 0):
                self.assertEqual(booker.run(), 'done')
            self.assertEqual(events[:4], ['wait', 'prepare', 'wait', 'budget'])
            booker.prepare_query.assert_called_once_with('2099-10-01')
            booker.query.assert_called_once()
