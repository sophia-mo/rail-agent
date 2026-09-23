"""Browser regressions using a local fixture; no account, network or orders."""
import importlib.util
import tempfile
import json
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch, Mock

from rail_agent.browser import Booker, ManualAction, QUERY_URL
from rail_agent.config import Trip


FIXTURE = """<!doctype html><meta charset="utf-8">
<a id="login_user">登录</a><a>未完成订单</a>
<input id="fromStationText"><input id="fromStation" type="hidden">
<input id="toStationText"><input id="toStation" type="hidden">
<div id="form_cities" style="display:none"><div id="panel_cities"></div></div>
<div id="form_cities2" style="display:none">热门站点：北京 上海 天津</div>
<input id="train_date" readonly value="2026-09-22">
<input id="sf1" name="sf" type="radio" checked>
<input id="sf2" name="sf" type="radio">
<a id="query_ticket" href="#" class="btn-disabled">查询</a>
<table><tbody id="queryLeftTable"></tbody></table>
<script>
window.jQuery = {isReady:false};
window.observed = null;
// keydown opens the search panel; keyup fills it even when it is hidden.
setTimeout(() => {
  window.jQuery = {isReady:true, stationFor12306:{}};
  window.station_names = '@bjn|北京南|VNP@shhq|上海虹桥|AOH@jnb|济宁北|MIK@jn|济宁|JIK';
  for (const kind of ['from', 'to']) {
    const input = document.getElementById(kind+'StationText');
    input.addEventListener('focus', () => {
      document.getElementById('form_cities2').style.display = 'block';
    });
    input.addEventListener('keydown', () => {
      document.getElementById('form_cities2').style.display = 'none';
      document.getElementById('form_cities').style.display = 'block';
    });
    input.addEventListener('keyup', () => {
      const panel = document.getElementById('panel_cities');
      panel.replaceChildren();
      const name = input.value;
      const codes = {'北京南':'VNP', '上海虹桥':'AOH', '济宁北':'MIK', '济宁':'JIK'};
      if (!codes[name]) return;
      // Exact result is second for 济宁北; a blind first-result click is wrong.
      const names = name === '济宁北' ? ['济宁', '济宁北'] : [name];
      let active = names[0];
      names.forEach((candidate, index) => {
        const item = document.createElement('div');
        item.id = 'citem_' + index;
        item.className = 'cityline';
        item.innerHTML = '<span class="ralign">' + candidate + '</span><span>pinyin</span>';
        item.onmouseover = () => { active = candidate; };
        item.onclick = () => {
          input.value = active;
          document.getElementById(kind+'Station').value = codes[active];
          window.stationClicks = (window.stationClicks || []).concat(active);
          document.getElementById('form_cities').style.display = 'none';
        };
        panel.append(item);
      });
    });
    input.addEventListener('blur', () => {
      if (!document.getElementById(kind+'Station').value) input.value = '';
    });
  }
  document.getElementById('query_ticket').classList.remove('btn-disabled');
}, 100);
document.getElementById('query_ticket').onclick = async e => {
  e.preventDefault();
  const read = id => document.getElementById(id).value;
  window.observed = {origin:read('fromStationText'), destination:read('toStationText'),
    fromCode:read('fromStation'), toCode:read('toStation'), date:read('train_date'),
    student:document.getElementById('sf2').checked};
  document.getElementById('query_ticket').classList.add('btn-disabled');
  await (await fetch('/otn/leftTicket/query?fixture=1')).json();
  document.getElementById('queryLeftTable').innerHTML = `<tr id="ticket_1">
    <td><a class="number">G103</a><strong class="start-t">09:00</strong></td>
    <td id="ZE_1">有</td><td><a href="#">预订</a></td></tr>`;
  document.getElementById('query_ticket').classList.remove('btn-disabled');
};
</script>"""


@unittest.skipUnless(importlib.util.find_spec("playwright"), "需要安装 Playwright")
class QueryBrowserTests(unittest.TestCase):
    @contextmanager
    def query_redirect_server(self, target):
        # Playwright routing does not reliably intercept redirected requests;
        # use a loopback server so the entire redirect stays off the Internet.
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith('/otn/leftTicket/query?'):
                    self.send_response(302)
                    self.send_header('Location', target)
                    self.end_headers()
                    return
                data = (b'{"status":true,"data":{"result":["fixture"]}}'
                        if self.path.startswith('/otn/leftTicket/queryG') else FIXTURE.encode())
                self.send_response(200)
                self.send_header('Content-Type', 'application/json' if self.path.startswith('/otn/leftTicket/queryG') else 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        from rail_agent.browser import is_query_endpoint
        self.page.unroute('**/*')
        self.booker.guard = Mock()
        try:
            with patch('rail_agent.browser.QUERY_URL', base + '/otn/leftTicket/init'), patch(
                'rail_agent.browser.is_query_endpoint', side_effect=lambda url: is_query_endpoint(url.replace(base, 'https://kyfw.12306.cn'))):
                yield
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def passenger_page(self, suffix='(学生)', duplicate=False):
        from dataclasses import replace
        self.booker.trip = replace(self.trip, passengers=['陈默'])
        self.page.unroute('**/*')
        extra = '<li><input type="checkbox" id="duplicate"><label for="duplicate">陈默</label></li>' if duplicate else ''
        html = '''<meta charset="utf-8"><div id="ticket_tit_id">2026年10月01日 G103 北京南 上海虹桥</div>
        <ul id="normal_passenger_id">
          <li><input type="checkbox" id="wanted"><label for="wanted">陈默SUFFIX</label></li>
          <li><input type="checkbox" id="other"><label for="other">陈默然</label></li>EXTRA
        </ul><div id="ticketInfo_id"></div>
        <a id="reserve" href="#">预订</a><button id="submitOrder_id">提交订单</button>
        <button id="qr_submit_id" style="display:none">确认</button>
        <a id="payment" href="#" style="display:none">网上支付</a>
        <script>
        window.submits=0; window.confirms=0; window.payments=0;
        document.querySelectorAll('#normal_passenger_id input').forEach(box => {
          box.onchange = () => {
            document.getElementById('ticketInfo_id').innerHTML = Array.from(
              document.querySelectorAll('#normal_passenger_id input:checked')).map((_,i) =>
                `<select id="seatType_${i}"><option value="O">二等座（¥321.0元）</option></select>
                 <select id="ticketType_${i}"><option>成人票</option><option>学生票</option></select>`).join('');
          };
        });
        document.getElementById('submitOrder_id').onclick=()=>{
          window.submits++;document.getElementById('qr_submit_id').style.display='block';};
        document.getElementById('qr_submit_id').onclick=()=>{
          window.confirms++;document.getElementById('payment').style.display='block';};
        document.getElementById('payment').onclick=()=>{window.payments++;};
        </script>'''.replace('SUFFIX', suffix).replace('EXTRA', extra)
        self.page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
        self.page.goto('https://kyfw.12306.cn/otn/confirmPassenger/initDc')

    def test_student_annotation_selected_then_order_submitted(self):
        self.passenger_page()
        self.booker.preview = False
        result = self.booker.order(self.page.locator('#reserve'), 'G103', '2026-10-01')
        self.assertIn('待支付', result)
        self.assertTrue(self.page.locator('#wanted').is_checked())
        self.assertFalse(self.page.locator('#other').is_checked())
        self.assertEqual(self.page.locator('#ticketType_0').input_value(), '学生票')
        self.assertEqual(self.page.evaluate('[submits,confirms,payments]'), [1,1,0])

    def test_quiet_preference_and_notice_before_final_confirmation(self):
        from dataclasses import replace
        self.passenger_page()
        self.booker.trip = replace(self.booker.trip, prefer_quiet=True)
        self.page.evaluate('''() => {
          document.body.insertAdjacentHTML('beforeend', `<input type="checkbox" id="seat-jy" style="display:none">
            <div id="quietNotice" style="display:none"><div id="content_defaultwarningAlert_hearder">静音车厢内须保持安静</div>
            <button id="qd_closeDefaultWarningWindowDialog_id">确定</button></div>`);
          const submit=document.getElementById('submitOrder_id');
          const original=submit.onclick;
          submit.onclick=()=>{
            original(); document.getElementById('seat-jy').style.display='block';
            document.getElementById('qr_submit_id').className='btn92';
          };
          document.getElementById('seat-jy').onclick=()=>{
            document.getElementById('quietNotice').style.display='block';};
          document.getElementById('qd_closeDefaultWarningWindowDialog_id').onclick=()=>{
            document.getElementById('quietNotice').style.display='none';
            setTimeout(()=>{document.getElementById('qr_submit_id').className='btn92s';},200);
          };
          const confirm=document.getElementById('qr_submit_id');
          const originalConfirm=confirm.onclick;
          confirm.onclick=()=>{
            if(confirm.className!=='btn92s') return;
            window.quietAtConfirm=document.getElementById('seat-jy').checked;
            originalConfirm();
          };
        }''')
        self.booker.preview = False
        self.booker.order(self.page.locator('#reserve'), 'G103', '2026-10-01')
        self.assertTrue(self.page.evaluate('quietAtConfirm'))
        self.assertEqual(self.page.evaluate('[confirms,payments]'), [1,0])

    def test_quiet_preference_can_be_disabled(self):
        from dataclasses import replace
        self.passenger_page()
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<input id=seat-jy type=checkbox>')")
        self.booker.trip = replace(self.booker.trip, prefer_quiet=False)
        self.booker.select_quiet_preference()
        self.assertFalse(self.page.locator('#seat-jy').is_checked())

    def test_quiet_preference_does_not_accept_unrelated_notice(self):
        from dataclasses import replace
        self.passenger_page()
        self.booker.trip = replace(self.booker.trip, prefer_quiet=True)
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<input id=seat-jy type=checkbox><div id=content_defaultwarningAlert_hearder>订单异常</div>')")
        with self.assertRaises(ManualAction):
            self.booker.select_quiet_preference()

    def test_chinese_annotation_preview_does_not_submit(self):
        from dataclasses import replace
        self.passenger_page(suffix='（学生）')
        self.booker.trip = replace(self.booker.trip, student=False)
        self.booker.order(self.page.locator('#reserve'), 'G103', '2026-10-01')
        self.assertTrue(self.page.locator('#wanted').is_checked())
        self.assertEqual(self.page.locator('#ticketType_0').input_value(), '成人票')
        self.assertEqual(self.page.evaluate('[submits,confirms,payments]'), [0,0,0])

    def test_duplicate_passenger_name_is_not_selected(self):
        self.passenger_page(duplicate=True)
        with self.assertRaises(ManualAction):
            self.booker.fill_passengers()
        self.assertFalse(self.page.locator('#wanted').is_checked())
        self.assertEqual(self.page.evaluate('submits'), 0)

    def test_list_without_bounding_box_can_select_visible_children(self):
        self.passenger_page()
        self.page.locator('#normal_passenger_id').evaluate("el => {el.style.height='0px'; el.style.padding='0'; el.style.overflow='visible'; el.style.marginBottom='80px';}")
        self.assertFalse(self.page.locator('#normal_passenger_id').is_visible())
        self.assertTrue(self.page.locator('#wanted').is_visible())
        self.booker.fill_passengers()
        self.assertTrue(self.page.locator('#wanted').is_checked())

    def test_hidden_native_checkbox_is_selected_through_visible_label(self):
        self.passenger_page()
        self.enable_student_confirmation()
        self.page.locator('#wanted').evaluate("el => el.style.display='none'")
        self.booker.fill_passengers()
        self.assertTrue(self.page.locator('#wanted').is_checked())
        self.assertEqual(self.page.evaluate('studentConfirmed'), 2)

    def enable_student_confirmation(self):
        self.page.evaluate('''() => {
          document.body.insertAdjacentHTML('beforeend', `<div id="dialog_xsertcj" style="display:none;position:fixed;inset:0;background:white;z-index:1000">
            <div id="dialog_xsertcj_msg">您是要购买学生票吗？</div>
            <button id="dialog_xsertcj_ok">确认</button>
            <button id="dialog_xsertcj_cancel">取消</button></div>`);
          window.studentConfirmed=0; window.studentCancelled=0;
          const modal=document.getElementById('dialog_xsertcj');
          const checkbox=document.getElementById('wanted');
          const render=checkbox.onchange;
          let done;
          const open=callback=>{done=callback;setTimeout(()=>{modal.style.display='block';},50);};
          checkbox.onchange=()=>open(student=>{
            render();
            const type=document.getElementById('ticketType_0');
            type.value=student?'学生票':'成人票';
            type.onchange=()=>{if(type.value==='学生票') open(yes=>{type.value=yes?'学生票':'成人票';});};
          });
          document.getElementById('dialog_xsertcj_ok').onclick=()=>{
            window.studentConfirmed++;modal.style.display='none';done(true);};
          document.getElementById('dialog_xsertcj_cancel').onclick=()=>{
            window.studentCancelled++;modal.style.display='none';done(false);};
        }''')

    def test_student_modal_after_check_and_ticket_change(self):
        self.passenger_page()
        self.assertEqual(self.page.locator('#train_info').count(), 0)
        self.assertTrue(self.page.locator('#ticket_tit_id').is_visible())
        self.enable_student_confirmation()
        self.booker.preview = False
        self.booker.order(self.page.locator('#reserve'), 'G103', '2026-10-01')
        self.assertEqual(self.page.evaluate('[studentConfirmed,studentCancelled,submits,payments]'), [2,0,1,0])

    def test_adult_config_cancels_student_question(self):
        from dataclasses import replace
        self.passenger_page()
        self.enable_student_confirmation()
        self.booker.trip = replace(self.booker.trip, student=False)
        self.booker.fill_passengers()
        self.assertEqual(self.page.locator('#ticketType_0').input_value(), '成人票')
        self.assertEqual(self.page.evaluate('[studentConfirmed,studentCancelled,submits]'), [0,1,0])

    def test_unrelated_ticket_dialog_is_not_confirmed(self):
        self.passenger_page()
        self.enable_student_confirmation()
        self.page.locator('#dialog_xsertcj_msg').evaluate("el => el.textContent='您是要购买儿童票吗？'")
        with self.assertRaises(ManualAction):
            self.booker.fill_passengers()
        self.assertEqual(self.page.evaluate('[studentConfirmed,studentCancelled,submits]'), [0,0,0])

    def test_student_dialog_with_zero_height_wrapper_is_confirmed(self):
        self.passenger_page()
        self.enable_student_confirmation()
        self.page.locator('#dialog_xsertcj').evaluate("el => {el.style.height='0px'; el.style.bottom='auto'; el.style.overflow='visible'; el.style.top='400px';}")
        self.page.locator('#wanted').check()
        self.page.locator('#dialog_xsertcj_msg').wait_for(state='visible')
        self.assertFalse(self.page.locator('#dialog_xsertcj').is_visible())
        self.booker.handle_student_dialog()
        self.assertEqual(self.page.evaluate('studentConfirmed'), 1)
        self.assertFalse(self.page.locator('#dialog_xsertcj_msg').is_visible())

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.set_default_timeout(3000)
        self.page.route("**/*", lambda route: route.fulfill(
            content_type="application/json", body='{"status":true,"data":{"result":["fixture"]}}'
        ) if "/leftTicket/query?" in route.request.url else route.fulfill(
            content_type="text/html", body=FIXTURE))
        self.directory = tempfile.TemporaryDirectory()
        self.trip = Trip(origin="北京南", destination="上海虹桥", date_start="2026-10-01",
                         date_end="2026-10-01", passengers=["测试"], student=True,
                         time_start="08:00", time_end="10:00", trains=["G103"])
        self.booker = Booker(self.page, self.trip, self.directory.name, preview=True)

    def tearDown(self):
        self.context.close()
        self.directory.cleanup()

    def test_config_is_filled_and_query_sent_despite_login_header(self):
        button, train = self.booker.query("2026-10-01")
        self.assertEqual(train, "G103")
        self.assertEqual(button.inner_text(), "预订")
        self.assertEqual(self.page.evaluate("window.observed"), {
            "origin":"北京南", "destination":"上海虹桥", "fromCode":"VNP", "toCode":"AOH",
            "date":"2026-10-01", "student":True})
        self.assertFalse(self.booker.state_file.exists())

    def test_prepared_form_is_used_without_reloading_or_early_query(self):
        self.booker.prepare_query('2026-10-01')
        self.assertIsNone(self.page.evaluate('window.observed'))
        with patch.object(self.page, 'goto', side_effect=AssertionError('prepared page must not reload')):
            _, train = self.booker.query('2026-10-01')
        self.assertEqual(train, 'G103')
        self.assertIsNone(self.booker.prepared_day)

    def test_query_302_to_another_query_endpoint_uses_final_response(self):
        with self.query_redirect_server('/otn/leftTicket/queryG?fixture=1'):
            _, train = self.booker.query('2026-10-01')
        self.assertEqual(train, 'G103')
        self.assertFalse(self.booker.state_file.exists())

    def test_query_302_to_login_does_not_retry_or_book(self):
        with self.query_redirect_server('/otn/resources/login.html'):
            with self.assertRaisesRegex(ManualAction, '非查询页面'):
                self.booker.query('2026-10-01')
        self.assertFalse(self.booker.state_file.exists())

    def test_jining_north_candidate_is_clicked_before_destination(self):
        from dataclasses import replace
        self.booker.trip = replace(self.trip, origin="济宁北")
        self.booker.query("2026-10-01")
        observed = self.page.evaluate('window.observed')
        self.assertEqual(observed['origin'], '济宁北')
        self.assertEqual(observed['fromCode'], 'MIK')
        self.assertEqual(observed['destination'], '上海虹桥')
        self.assertEqual(self.page.evaluate('window.stationClicks'), ['济宁北', '上海虹桥'])

    def test_keyup_alone_does_not_open_search_panel(self):
        self.page.goto(QUERY_URL)
        self.page.wait_for_function('() => window.jQuery.isReady')
        field = self.page.locator('#fromStationText')
        field.click()
        field.fill('济宁北')
        field.dispatch_event('keyup')
        self.assertTrue(self.page.locator('#form_cities2').is_visible())
        self.assertEqual(self.page.locator('#panel_cities [id^="citem_"]:visible').count(), 0)
        field.press('End')
        self.assertFalse(self.page.locator('#form_cities2').is_visible())
        self.assertTrue(self.page.locator('#panel_cities #citem_1').is_visible())

    def test_real_login_dialog_still_stops(self):
        self.page.goto(QUERY_URL)
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<button id=J-login>立即登录</button>')")
        with self.assertRaises(ManualAction):
            self.booker.guard()

    def test_unpaid_order_dialog_still_stops(self):
        self.page.goto(QUERY_URL)
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div role=dialog>您有未完成订单</div>')")
        with self.assertRaises(ManualAction):
            self.booker.guard()

    def test_closed_target_with_other_tab_still_open(self):
        other = self.page.context.new_page()
        self.booker._query = lambda day: self.page.close() or self.page.locator('#fromStationText').fill('济宁北')
        try:
            with self.assertRaisesRegex(ManualAction, "还有其他标签页打开"):
                self.booker.query("2026-10-01")
            details = json.loads((self.booker.state_file.parent / 'query-error.json').read_text())
            self.assertTrue(details['page_closed'])
            self.assertTrue(details['browser_connected'])
            self.assertIn('page_closed', details['events'])
            self.assertFalse(self.booker.state_file.exists())
        finally:
            other.close()

    def test_open_page_error_does_not_report_browser_closed(self):
        def fail(day):
            raise ValueError('sensitive page content must not be logged')
        self.booker._query = fail
        with self.assertRaisesRegex(ManualAction, "页面尚未关闭"):
            self.booker.query("2026-10-01")
        text = (self.booker.state_file.parent / 'query-error.json').read_text()
        self.assertNotIn('sensitive', text)

    def test_session_cookie_survives_context_restart(self):
        from rail_agent.session import save_session, restore_session
        self.context.add_cookies([{'name':'test_session', 'value':'test-only',
                                  'domain':'kyfw.12306.cn', 'path':'/', 'httpOnly':True, 'secure':True}])
        save_session(self.context, self.directory.name)
        self.context.close()
        self.context = self.browser.new_context()
        self.assertEqual(self.context.cookies(), [])
        restore_session(self.context, self.directory.name)
        cookies = self.context.cookies('https://kyfw.12306.cn/')
        self.assertEqual(cookies[0]['name'], 'test_session')
        self.assertEqual(cookies[0]['expires'], -1)

    def test_login_check_runs_with_browser_cookie_and_ajax_headers(self):
        from rail_agent.session import login_status
        captured = []
        def check(route):
            captured.append(route.request)
            route.fulfill(content_type='application/json', body='{"data":{"flag":true}}')
        self.page.route('**/otn/login/checkUser', check)
        self.context.add_cookies([{'name':'test_session', 'value':'test-only',
                                  'domain':'kyfw.12306.cn', 'path':'/'}])
        self.page.goto(QUERY_URL)
        self.assertIs(login_status(self.context), True)
        self.assertEqual(captured[0].method, 'POST')
        self.assertEqual(captured[0].header_value('x-requested-with'), 'XMLHttpRequest')
        self.assertIn('test_session=test-only', captured[0].header_value('cookie'))
