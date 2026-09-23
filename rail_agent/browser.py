from __future__ import annotations

import re
import time
import json
from pathlib import Path
from urllib.parse import urlparse, urljoin
from datetime import datetime, timedelta

from .config import SEATS, enough_tickets

QUERY_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
LOGIN_URL = "https://kyfw.12306.cn/otn/resources/login.html"


class ManualAction(RuntimeError):
    """Leave the visible browser open for the user; never retry a purchase."""


class RetryableQuery(RuntimeError):
    """A read-only query failed temporarily; no purchase was attempted."""


REDIRECTS = {301, 302, 303, 307, 308}


def is_query_endpoint(url):
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and parsed.hostname == 'kyfw.12306.cn'
            and re.fullmatch(r'/otn/leftTicket/query[A-Za-z]*', parsed.path) is not None)


def query_response_ready(response):
    # Follow the browser's redirect chain; do not launch a second API request.
    request = response.request
    belongs_to_query = False
    while request is not None:
        if request.method == 'GET' and is_query_endpoint(request.url):
            belongs_to_query = True
            break
        request = request.redirected_from
    if not belongs_to_query:
        return False
    if response.status in REDIRECTS:
        location = response.headers.get('location')
        # Wait for the final response only for redirects between query endpoints.
        return not location or not is_query_endpoint(urljoin(response.url, location))
    return True


def query_payload(response):
    if response.status in REDIRECTS:
        location = response.headers.get('location')
        if not location:
            raise RetryableQuery("查询重定向缺少目标地址")
        target = urljoin(response.url, location)
        if is_query_endpoint(target):
            raise RetryableQuery("查询接口跳转尚未完成")
        # Do not print redirect parameters; they may contain authentication data.
        raise ManualAction("余票请求被重定向到非查询页面，可能是登录或验证页；请查看网站提示")
    if response.status in (401, 403, 429):
        raise ManualAction(f"查询被拒绝或限流（HTTP {response.status}），请检查登录和网站提示")
    if response.status >= 500:
        raise RetryableQuery(f"查询服务暂时异常（HTTP {response.status}）")
    if response.status != 200:
        raise ManualAction(f"查询返回异常状态 HTTP {response.status}，请检查网站提示")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RetryableQuery("查询响应暂时无法解析") from exc
    if not isinstance(payload, dict):
        raise RetryableQuery("查询响应格式异常")
    messages = str(payload.get('messages', ''))
    if any(word in messages for word in ('登录', '验证码', '验证失败', '频繁', '频率', '访问异常', '未完成订单', '未支付订单')):
        raise ManualAction("查询响应提示登录、验证、限流或未完成订单问题，请查看网站提示")
    if payload.get('status') is not True:
        raise RetryableQuery("网站本次查询返回未成功")
    return payload


class Booker:
    def __init__(self, page, trip, state_dir, preview=False):
        self.page, self.trip = page, trip
        self.state_file = Path(state_dir) / "attempt.json"
        self.preview = preview
        self.started = False
        self.prepared_day = None
        self.query_stage = "尚未查询"
        self.browser_events = []
        page.on("close", lambda _: self.browser_events.append("page_closed"))
        page.on("crash", lambda _: self.browser_events.append("page_crashed"))
        page.context.on("close", lambda _: self.browser_events.append("context_closed"))
        if page.context.browser is not None:
            page.context.browser.on("disconnected", lambda _: self.browser_events.append("browser_disconnected"))

    def query_failure(self, exc):
        """Record lifecycle facts without page contents, cookies or exception traces."""
        closed = self.page.is_closed()
        crashed = "page_crashed" in self.browser_events
        target_closed = type(exc).__name__ == "TargetClosedError"
        browser = self.page.context.browser
        details = {
            "stage": self.query_stage, "error_type": type(exc).__name__,
            "page_closed": closed, "events": list(self.browser_events),
            "remaining_pages": len(self.page.context.pages),
            "browser_connected": browser.is_connected() if browser is not None else None,
        }
        diagnostic = self.state_file.parent / "query-error.json"
        try:
            diagnostic.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            diagnostic.write_text(json.dumps(details, ensure_ascii=False, indent=2))
            diagnostic.chmod(0o600)
            suffix = f" 诊断记录：{diagnostic.resolve()}"
        except OSError:
            suffix = " 诊断记录未能写入。"
        if crashed:
            reason = "浏览器报告页面崩溃"
        elif details["browser_connected"] is False:
            reason = "Playwright 与浏览器的连接已经断开，屏幕上仍有窗口也不代表控制连接正常"
        elif closed and details["remaining_pages"]:
            reason = "受控标签页已关闭，但该浏览器还有其他标签页打开"
        elif closed or target_closed:
            reason = "标签页、浏览器上下文已关闭，或浏览器连接已断开；仅凭此错误无法判断是谁关闭的"
        else:
            reason = f"页面操作失败（{type(exc).__name__}），浏览器页面尚未关闭"
        return ManualAction(f"查询停止在「{self.query_stage}」：{reason}。尚未执行预订。{suffix}")

    def checkpoint(self, stage):
        # Persist before any purchase action. A crash must not cause a repeat order.
        import json
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"stage": stage, "time": time.time()}))
        temporary.chmod(0o600)
        temporary.replace(self.state_file)

    def guard(self):
        host = urlparse(self.page.url).hostname or ""
        if host != "12306.cn" and not host.endswith(".12306.cn"):
            raise ManualAction("页面离开 12306，请人工检查")
        # The header's login link can be visible before async session hydration.
        # Public ticket queries do not require login; only an actual dialog blocks us.
        if self.page.locator('#J-login:visible, #nc_1_wrapper:visible').count():
            raise ManualAction("请在浏览器中完成登录或验证，然后重新运行")
        for message in ("操作频率过快", "访问异常"):
            if any(item.is_visible() for item in self.page.get_by_text(message, exact=False).all()):
                raise ManualAction(f"网站提示「{message}」，请人工处理")
        dialogs = self.page.locator('[role="dialog"]:visible, [role="alertdialog"]:visible, .dhtmlx_modal_box:visible, .dhx_modal_message:visible, .up-box:visible')
        for message in ("未完成订单", "未支付订单"):
            if any(item.is_visible() for item in dialogs.get_by_text(message, exact=False).all()):
                raise ManualAction(f"网站提示「{message}」，请人工处理")

    def station(self, kind, name):
        label = "出发站" if kind == "from" else "到达站"
        self.progress(f"填写{label}：{name}（定位输入框）")
        field = self.page.locator(f"#{kind}StationText")
        field.click(timeout=8000)
        self.progress(f"填写{label}：{name}（输入站名）")
        field.fill(name)
        # keydown opens the search panel; keyup filters its results. fill() emits
        # neither. End sends both real keyboard events without changing the name.
        field.press("End")
        # Use the official autocomplete so the hidden station code is populated.
        self.progress(f"填写{label}：{name}（等待候选车站，最多 8 秒）")
        # Select the clickable result ROW, not a label in the hot-city panel.
        # Hover sets the station plugin's active candidate; click commits it.
        option = self.page.locator('#panel_cities [id^="citem_"]:visible').filter(
            has=self.page.get_by_text(name, exact=True))
        option.first.wait_for(state="visible", timeout=8000)
        if option.count() != 1:
            raise ManualAction("车站匹配不唯一，请使用准确车站名")
        self.progress(f"填写{label}：{name}（选择候选车站）")
        expected_codes = self.page.evaluate("""name => (window.station_names || '').split('@')
            .map(entry => entry.split('|')).filter(parts => parts[1] === name)
            .map(parts => parts[2])""", name)
        if len(set(expected_codes)) != 1 or not expected_codes[0]:
            raise ManualAction(f"无法从网站车站表唯一核对{name}，停止填写")
        option.hover(timeout=8000)
        option.click(timeout=8000)
        self.page.wait_for_function("""({kind, name, code}) =>
            document.getElementById(kind + 'StationText').value === name &&
            document.getElementById(kind + 'Station').value === code""",
            arg={"kind": kind, "name": name, "code": expected_codes[0]}, timeout=8000)
        # The site clears uncommitted free text on blur. Verify it survives blur.
        field.press("Tab")
        if field.input_value() != name or self.page.locator(f"#{kind}Station").input_value() != expected_codes[0]:
            raise ManualAction(f"{name}未成功选中，车站代码核对失败")
        self.progress(f"已选中{label}：{name}（{expected_codes[0]}）")

    def query(self, day):
        self.query_stage = "加载查询页"
        try:
            return self._query(day)
        except (ManualAction, RetryableQuery):
            raise
        except Exception as exc:
            raise self.query_failure(exc) from exc

    def progress(self, stage):
        self.query_stage = stage
        print(stage, flush=True)

    def prepare_query(self, day):
        self.prepared_day = None
        self.page.goto(QUERY_URL, wait_until="domcontentloaded")
        self.progress("等待车站控件初始化")
        self.page.wait_for_function("""() => window.jQuery && window.jQuery.isReady
            && window.jQuery.stationFor12306 && typeof station_names === 'string'
            && station_names.length > 0
            && document.querySelector('#query_ticket')
            && !document.querySelector('#query_ticket').classList.contains('btn-disabled')""")
        self.guard()
        self.station("from", self.trip.origin)
        self.station("to", self.trip.destination)
        self.progress(f"填写出发日期：{day}")
        date_input = self.page.locator('input#train_date')
        date_input.evaluate("el => el.removeAttribute('readonly')")
        date_input.fill(day)
        date_input.dispatch_event("change")
        date_input.press("Tab")
        self.page.locator("#sf2" if self.trip.student else "#sf1").check()
        self.prepared_day = day

    def _query(self, day):
        if self.prepared_day != day:
            self.prepare_query(day)
        self.prepared_day = None
        date_input = self.page.locator('input#train_date')
        if (date_input.input_value() != day
                or self.page.locator('#fromStationText').input_value() != self.trip.origin
                or self.page.locator('#toStationText').input_value() != self.trip.destination
                or not self.page.locator('#sf2' if self.trip.student else '#sf1').is_checked()):
            raise ManualAction("查询表单被网页重置，未点击查询；请检查车站和可售日期")
        self.progress("点击查询，等待余票结果")
        # Await this specific query response, rather than accidentally reading stale rows.
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        try:
            with self.page.expect_response(query_response_ready) as response:
                self.page.locator("#query_ticket").click()
        except PlaywrightTimeout as exc:
            self.guard()
            raise RetryableQuery("等待余票查询响应超时") from exc
        result = response.value
        self.guard()
        payload = query_payload(result)
        self.page.wait_for_function("() => !document.querySelector('#query_ticket').classList.contains('btn-disabled')")
        self.guard()
        self.progress("按配置筛选车次、发车时间和席别")
        rows = self.page.locator('#queryLeftTable tr[id^="ticket_"]')
        if rows.count() == 0 and payload.get("data", {}).get("result"):
            raise ManualAction("查询有数据但页面行未找到，可能需要更新选择器")
        for row in rows.all():
            train = row.locator(".number").inner_text().strip()
            departure = row.locator(".start-t").inner_text().strip()
            if not self.trip.matches(train, departure):
                continue
            prefix = SEATS[self.trip.seat][0]
            cell = row.locator(f'[id^="{prefix}_"]')
            if cell.count() != 1:
                raise ManualAction("未找到指定席别列，停止以避免买错席别")
            if enough_tickets(cell.inner_text(), len(self.trip.passengers)):
                button = row.get_by_role("link", name="预订", exact=True)
                if button.count() == 1 and button.is_visible():
                    return button, train
        return None

    def handle_student_dialog(self):
        # The same 12306 dialog is used for child/disability tickets too: inspect
        # its question before choosing either action, never click a generic OK.
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        # createWin reparents the popup into body. The outer wrapper may have
        # no visible box even though its content and actions are on screen.
        message_node = self.page.locator('#dialog_xsertcj_msg:visible')
        self.order_progress("等待学生票提示正文")
        try:
            message_node.wait_for(state='visible', timeout=3000)
        except PlaywrightTimeout:
            return
        message = message_node.inner_text()
        if not re.search(r'购买学生票吗|是否.{0,8}购买学生票', message):
            raise ManualAction("出现未识别的票种确认弹窗，请人工处理；未点击确认")
        action = '#dialog_xsertcj_ok' if self.trip.student else '#dialog_xsertcj_cancel'
        self.order_progress("点击学生票确认" if self.trip.student else "点击学生票取消")
        button = self.page.locator(action + ':visible')
        button.wait_for(state='visible', timeout=8000)
        if button.count() != 1:
            raise ManualAction("学生票弹窗按钮不唯一，停止自动点击")
        button.scroll_into_view_if_needed()
        button.click(timeout=8000)
        self.order_progress("等待学生票提示关闭")
        message_node.wait_for(state='hidden', timeout=8000)
        print("已确认购买学生票。" if self.trip.student else "已取消学生票选项，按配置购买成人票。", flush=True)

    def set_passenger_checked(self, checkbox, checked):
        if checkbox.is_checked() == checked:
            return
        if not checkbox.is_enabled():
            raise ManualAction("乘车人复选框被网站禁用，请检查乘车人核验状态")
        if checkbox.is_visible():
            checkbox.set_checked(checked)
        else:
            # Styled checkboxes may hide the native input. Click its visible
            # label so the site's actual click/change handlers still execute.
            identifier = checkbox.get_attribute('id')
            label = self.page.locator(f'label[for={json.dumps(identifier)}]:visible') if identifier else None
            if label is None or label.count() != 1:
                label = checkbox.locator('xpath=following-sibling::label[1]')
            if label.count() != 1 or not label.is_visible():
                raise ManualAction("乘车人复选框隐藏且找不到唯一可点击标签，请检查页面")
            label.click()
        if checkbox.is_checked() != checked:
            raise ManualAction("网站未保留乘车人勾选状态，请检查弹窗或资格提示")

    def fill_passengers(self):
        self.order_progress("等待乘车人列表容器加载")
        passenger_list = self.page.locator("#normal_passenger_id")
        # A floated/contents list can have no bounding box while its children are visible.
        # Hidden native inputs can also have visible labels.
        passenger_list.wait_for(state="attached")
        self.order_progress("等待乘车人复选框加载")
        passenger_list.locator('input[type="checkbox"]').first.wait_for(state="attached")
        selected = []
        # Resolve all names before changing selection; allow only known display annotations,
        # not arbitrary substrings.
        for name in self.trip.passengers:
            self.order_progress("匹配配置中的乘车人")
            pattern = re.compile(r"^\s*" + re.escape(name) + r"\s*(?:\(\s*学生\s*\)|（\s*学生\s*）)?\s*$")
            checkbox = passenger_list.get_by_label(pattern)
            if checkbox.count() == 0:
                checkbox = passenger_list.locator('li').filter(has_text=pattern).locator('input[type="checkbox"]')
            if checkbox.count() != 1:
                raise ManualAction("找不到唯一的已保存乘车人（已兼容学生标记），请人工核对；不会自动新增乘车人")
            selected.append(checkbox)
        for checkbox in passenger_list.locator('input[type="checkbox"]:checked').all():
            self.set_passenger_checked(checkbox, False)
        for checkbox in selected:
            self.order_progress("勾选乘车人复选框")
            self.set_passenger_checked(checkbox, True)
            # The order row is rendered only after this modal is resolved.
            self.order_progress("处理学生票确认弹窗")
            self.handle_student_dialog()
        if passenger_list.locator('input[type="checkbox"]:checked').count() != len(selected):
            raise ManualAction("勾选的乘车人数与配置不符，停止提交")
        self.order_progress("核对乘车人订单行并设置票种和席别")
        seats = self.page.locator('#ticketInfo_id select[id^="seatType_"]')
        types = self.page.locator('#ticketInfo_id select[id^="ticketType_"]')
        if seats.count() != len(self.trip.passengers) or types.count() != len(self.trip.passengers):
            raise ManualAction("乘车人订单行数量不匹配")
        for ticket in types.all():
            ticket.select_option(label="学生票" if self.trip.student else "成人票")
            self.handle_student_dialog()
        # Changing ticket type can re-render the seat selector. Set seats last.
        for seat in seats.all():
            seat.select_option(SEATS[self.trip.seat][1])
        for ticket in types.all():
            if ticket.locator('option:checked').inner_text().strip() != ("学生票" if self.trip.student else "成人票"):
                raise ManualAction("确认弹窗后票种与配置不符，停止提交")
        print(f"已选择 {len(selected)} 名配置乘车人，并设置席别和票种。", flush=True)

    def order_progress(self, stage):
        self.order_stage = stage
        print(f"预订：{stage}", flush=True)

    def select_quiet_preference(self):
        if not self.trip.prefer_quiet:
            return
        self.order_progress("选择优先分配静音车厢")
        quiet = self.page.locator('#seat-jy')
        if quiet.count() == 0 or not quiet.is_visible():
            print("本次确认页未提供可见的静音车厢选项，继续按原席别预订。", flush=True)
            return
        if not quiet.is_enabled():
            raise ManualAction("静音车厢选项被禁用，请检查网站提示")
        quiet.check()
        # Official #seat-jy handler shows a dhtmlx notice. Acknowledge only the
        # quiet-carriage notice, not other warnings using the same dialog IDs.
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        message = self.page.locator('#content_defaultwarningAlert_hearder:visible')
        try:
            message.wait_for(state='visible', timeout=2000)
        except PlaywrightTimeout:
            pass
        else:
            text = message.inner_text()
            if '静音车厢' not in text or '保持安静' not in text:
                raise ManualAction("勾选静音车厢后出现其他提示，请人工核对")
            self.order_progress("确认静音车厢提示")
            self.page.locator('#qd_closeDefaultWarningWindowDialog_id:visible').click()
            message.wait_for(state='hidden')
        if not quiet.is_checked():
            raise ManualAction("静音车厢偏好未保持勾选，停止确认订单")
        print("已勾选优先分配静音车厢；最终以网站分配为准。", flush=True)

    def order(self, button, train, day):
        self.order_stage = "开始预订"
        try:
            return self._order(button, train, day)
        except ManualAction as exc:
            raise ManualAction(f"预订停止在「{self.order_stage}」：{exc}") from exc
        except Exception as exc:
            diagnostic = ""
            if "乘车人" in self.order_stage:
                try:
                    container = self.page.locator('#normal_passenger_id')
                    boxes = container.locator('input[type="checkbox"]')
                    diagnostic = (f" 列表容器数={container.count()}，复选框数={boxes.count()}，"
                                  f"可见复选框数={sum(box.is_visible() for box in boxes.all())}。")
                except Exception:
                    pass
            raise ManualAction(f"预订停止在「{self.order_stage}」（{type(exc).__name__}）。"
                               f"{diagnostic}"
                               "已保留尝试记录，不会自动重复下单；请检查浏览器和未完成订单。") from exc

    def _order(self, button, train, day):
        self.checkpoint("booking_started")
        self.order_progress("点击预订")
        button.click()
        self.order_progress("等待预订页面")
        self.page.wait_for_url("**/confirmPassenger/**", wait_until="domcontentloaded")
        self.guard()
        # Check train/date/stations on the order page before selecting passengers.
        self.order_progress("核对车次、日期和出发到达站")
        # Official passengerInfo JS renders ticketTitTemplateLong in ticket_tit_id.
        info = self.page.locator("#ticket_tit_id:visible, #train_info:visible").first
        info.wait_for(state="visible")
        text = info.inner_text()
        normalized = re.sub(r"\D", "", day)
        if (not re.search(r"(?<![A-Z0-9])" + re.escape(train) + r"(?![A-Z0-9])", text)
                or any(x not in text for x in (self.trip.origin, self.trip.destination))
                or normalized not in re.sub(r"\D", "", text)):
            raise ManualAction("订单页行程信息未通过核对，请手动检查")
        self.fill_passengers()
        self.guard()
        if self.preview:
            self.checkpoint("preview")
            return "预览已就绪：已填好乘车人和席别，未提交订单。请在浏览器中核对。"
        self.checkpoint("submit_started")
        self.order_progress("提交订单")
        self.page.locator("#submitOrder_id").click()
        self.order_progress("等待订单确认窗口")
        confirm = self.page.locator("#qr_submit_id")
        confirm.wait_for(state="visible")
        self.guard()
        if self.trip.prefer_seat:
            # Only click the requested seat letter; never infer coordinates.
            choices = self.page.locator('#id-seat-sel:visible').get_by_text(self.trip.prefer_seat, exact=True)
            selected = 0
            for choice in choices.all():
                if selected == len(self.trip.passengers):
                    break
                if choice.is_visible() and choice.is_enabled():
                    choice.click()
                    selected += 1
            print(f"已尝试选择 {selected} 个 {self.trip.prefer_seat} 座偏好；实际座位以网站分配为准。", flush=True)
        self.select_quiet_preference()
        self.order_progress("等待确认按钮可用")
        self.page.wait_for_function("""() => {
            const button = document.querySelector('#qr_submit_id');
            return button && !button.disabled && button.getAttribute('aria-disabled') !== 'true'
                && !button.classList.contains('btn92') && !button.classList.contains('btn-disabled');
        }""")
        self.checkpoint("confirm_started")
        self.order_progress("确认订单并等待待支付页面")
        confirm.click()  # Exactly once; timeout after this point is NOT a retry signal.
        try:
            self.page.get_by_role("link", name="网上支付", exact=True).wait_for(state="visible", timeout=120000)
        except Exception as exc:
            raise ManualAction("已尝试确认订单，结果尚未确定。请检查未完成订单，程序不会重试") from exc
        self.checkpoint("awaiting_payment")
        return "订单已进入待支付页面，请在浏览器中自行支付。"

    def run(self):
        if self.started or self.state_file.exists():
            raise ManualAction("存在购票尝试记录。请先检查未完成订单，确认后使用 clear-attempt 清除记录")
        self.started = True
        self.trip.validate()
        if self.trip.start_at:
            from .schedule import parse_start_at, wait_until, SHANGHAI
            target = parse_start_at(self.trip.start_at)
            print(f"定时查询：{target:%Y-%m-%d %H:%M:%S}（北京时间）。", flush=True)
            if target <= datetime.now(SHANGHAI):
                print("设定时间已到或已过，立即开始。", flush=True)
            else:
                # Pump browser events while waiting, so manual login can finish.
                pause = lambda seconds: self.page.wait_for_timeout(seconds * 1000)
                wait_until(target - timedelta(seconds=60), sleep=pause)
                self.progress("提前准备首个日期的查询表单")
                self.prepare_query(next(self.trip.dates()))
                print("表单已准备好，等待放票时间；请勿修改页面或关闭浏览器。", flush=True)
                wait_until(target, sleep=pause)
        # Waiting for release does not consume the ticket-monitoring budget.
        deadline = time.monotonic() + self.trip.timeout_minutes * 60
        failures = 0
        while time.monotonic() < deadline:
            for day in self.trip.dates():
                if time.monotonic() >= deadline:
                    break
                print(f"查询 {day} {self.trip.origin} → {self.trip.destination}", flush=True)
                try:
                    found = self.query(day)
                except RetryableQuery as exc:
                    failures += 1
                    found = None
                    print(f"{exc}；尚未预订，将继续查询。", flush=True)
                if found:
                    return self.order(*found, day)
                # Rate limit every query, including queries for different dates.
                delay = min(self.trip.poll_seconds, max(0, deadline - time.monotonic()))
                print(f"等待 {delay:g} 秒后进行下一次查询。", flush=True)
                time.sleep(delay)
        if failures:
            return f"监控时间已结束，未进入预订；期间有 {failures} 次查询失败，不能据此认定无票。"
        return "监控时间已结束，没有找到符合条件的余票。"
