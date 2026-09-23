import tempfile
import unittest
from datetime import date
from unittest.mock import Mock, patch

from rail_agent.config import Trip, enough_tickets
from rail_agent.browser import Booker, ManualAction, RetryableQuery, query_payload


def trip(**changes):
    data = dict(origin="北京南", destination="上海虹桥", date_start="2026-10-01",
                date_end="2026-10-02", passengers=["张三"])
    data.update(changes)
    return Trip(**data)


class ConfigTests(unittest.TestCase):
    def test_quiet_preference_requires_explicit_boolean(self):
        self.assertFalse(trip().prefer_quiet)
        self.assertTrue(trip(prefer_quiet=True).validate(date(2026, 9, 22)).prefer_quiet)
        for invalid in ('true', 1, None):
            with self.assertRaises(ValueError):
                trip(prefer_quiet=invalid).validate(date(2026, 9, 22))

    def test_dates_and_filters(self):
        t = trip(trains=["G1"], time_start="08:00", time_end="12:00")
        t.validate(date(2026, 9, 22))
        self.assertEqual(list(t.dates()), ["2026-10-01", "2026-10-02"])
        self.assertTrue(t.matches("G1", "08:00"))
        self.assertTrue(t.matches("G1", "12:00"))
        self.assertFalse(t.matches("G2", "10:00"))
        self.assertFalse(t.matches("G1", "13:00"))

    def test_invalid(self):
        for changes in [dict(student="false"), dict(passengers=[]), dict(passengers=["甲", "甲"]),
                        dict(time_start="9:00"), dict(time_start="23:00", time_end="01:00"),
                        dict(date_end="2026-09-30"), dict(poll_seconds=1), dict(seat="无座")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                trip(**changes).validate(date(2026, 9, 22))

    def test_availability(self):
        for value in ["无", "候补", "--", "0", "1", "20张"]:
            self.assertFalse(enough_tickets(value, 2))
        self.assertTrue(enough_tickets("有", 5))
        self.assertTrue(enough_tickets("2", 2))


class BookingTests(unittest.TestCase):
    def test_temporary_query_failure_retries_after_configured_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            b = Booker(Mock(), trip(poll_seconds=15), directory)
            button = Mock()
            b.query = Mock(side_effect=[RetryableQuery('临时失败'), (button, 'G1')])
            b.order = Mock(return_value='订单流程结果')
            with patch.object(Trip, 'validate'), patch('rail_agent.browser.time.monotonic', return_value=0), \
                 patch('rail_agent.browser.time.sleep') as sleep:
                self.assertEqual(b.run(), '订单流程结果')
            sleep.assert_called_once_with(15)
            self.assertEqual(b.query.call_count, 2)
            b.order.assert_called_once()

    def test_manual_query_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            b = Booker(Mock(), trip(), directory)
            b.query = Mock(side_effect=ManualAction('需要登录'))
            with patch.object(Trip, 'validate'), patch('rail_agent.browser.time.sleep') as sleep:
                with self.assertRaises(ManualAction):
                    b.run()
            sleep.assert_not_called()
            b.query.assert_called_once()

    def test_query_response_failure_classification(self):
        response = Mock(status=200)
        response.json.return_value = {'status':False, 'messages':['系统繁忙']}
        with self.assertRaises(RetryableQuery):
            query_payload(response)
        response.json.return_value = {'status':False, 'messages':['请先登录']}
        with self.assertRaises(ManualAction):
            query_payload(response)
        response.status = 503
        with self.assertRaises(RetryableQuery):
            query_payload(response)
        response.status = 429
        with self.assertRaises(ManualAction):
            query_payload(response)

    def test_lookalike_domain_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Mock(url="https://12306.cn.evil.example/")
            with self.assertRaises(ManualAction):
                Booker(page, trip(), directory).guard()

    def test_wrong_train_does_not_select_passengers(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Mock()
            b = Booker(page, trip(), directory)
            b.guard = Mock()
            page.locator.return_value.first = page.locator.return_value
            page.locator.return_value.inner_text.return_value = "2026年10月01日 G10 北京南 上海虹桥"
            with self.assertRaises(ManualAction):
                b.order(Mock(), "G1", "2026-10-01")
            self.assertNotIn("#normal_passenger_id", [c.args[0] for c in page.locator.call_args_list])

    def test_persist_before_click_and_never_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            booker = Booker(Mock(), trip(), directory)
            button = Mock()
            def uncertain():
                self.assertTrue(booker.state_file.exists())
                raise TimeoutError()
            button.click.side_effect = uncertain
            with self.assertRaisesRegex(ManualAction, "点击预订.*TimeoutError"):
                booker.order(button, "G1", "2026-10-01")
            with self.assertRaises(ManualAction):
                Booker(Mock(), trip(), directory).run()
            button.click.assert_called_once()

    def test_no_stock_queries_are_spaced(self):
        with tempfile.TemporaryDirectory() as directory:
            b = Booker(Mock(), trip(), directory)
            b.query = Mock(return_value=None)
            b.order = Mock()
            with patch.object(Trip, "validate"), patch("rail_agent.browser.time.sleep") as sleep, \
                 patch("rail_agent.browser.time.monotonic", side_effect=[0, 0, 0, 0, 0, 0, 3601]):
                self.assertIn("没有找到", b.run())
            self.assertEqual(b.query.call_count, 2)
            self.assertEqual(sleep.call_count, 2)
            b.order.assert_not_called()

    def test_preview_cannot_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Mock()
            b = Booker(page, trip(date_end="2026-10-01"), directory, preview=True)
            b.guard = Mock()
            b.fill_passengers = Mock()
            element = page.locator.return_value
            element.first = element
            element.inner_text.return_value = "2026年10月01日 G1 北京南 上海虹桥"
            element.locator.return_value.all.return_value = []
            element.get_by_label.return_value.count.return_value = 1
            element.count.return_value = 1
            element.all.return_value = [Mock()]
            self.assertIn("未提交", b.order(Mock(), "G1", "2026-10-01"))
            self.assertNotIn("#submitOrder_id", [c.args[0] for c in page.locator.call_args_list])


if __name__ == "__main__":
    unittest.main()
