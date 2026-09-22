# rail-agent

LangChain + Playwright 的 12306 网页购票助手。
手动登录，按配置查询余票，选择账户中已保存的乘车人，尝试 F 座偏好，提交后由你支付。

## 安装

需要 Python 3.10+ 和桌面环境。
```bash
# 创建并激活环境
conda create -n rail python=3.11 -y
conda activate rail

# 安装项目依赖和浏览器
python -m pip install -e .
python -m playwright install chromium
```

## 配置文件

添加以下两个文件
### .env
```bash
# OpenAI-compatible model supporting tool calls
OPENAI_API_KEY=YOUR_API_KEY
RAIL_MODEL=YOUR_MODEL
# Optional custom provider endpoint
# OPENAI_BASE_URL=https://your-provider.example/v1  # 可使用 OpenAI 兼容的模型
```

### trip.json
```json
{
  "origin": "北京南",
  "destination": "上海虹桥",    // 具体车站全名，不自动扩展同城站
  "date_start": "2026-10-01",
  "date_end": "2026-10-01",   // 乘车日期范围，含两端，最多 15 天
  "time_start": "10:00",
  "time_end": "15:00",        // 每天发车时间段，北京时间，含两端，不跨午夜
  "trains": ["G123"],         // 空列表表示不限
  "seat": "二等座",            // 商务座、一等座或二等座，不自动改席别
  "student": true,            // 是否学生票
  "passengers": ["Alice"],    // 1–5 名已有乘车人，自动兼容页面上的 (学生) 后缀，同名无法唯一识别时停止
  "prefer_f": true,           // 默认 true，有 F 座选项时尝试选择，否则接受系统分配
  "prefer_quiet": true,       // true 时勾选 "优先分配静音车厢"，默认 false
  "poll_seconds": 30,         // 每次查询间隔，默认 30 秒，至少 15 秒
  "timeout_minutes": 60       // 总监控时长，默认 60 分钟
}
```

## 使用
1. 执行 `rail-agent login`，在专用浏览器中手动登录，可扫码。等登录成功并跳转后，保持窗口打开，回终端按回车。程序先保存会话，再通过浏览器页面检查登录有效性，报告结果后关闭。检查未通过或无法确认时也会保存，但不会宣称已登录。
2. 修改 `trip.json`，填写车站、时间、席别、学生票与已有乘车人姓名。
3. 首次运行请执行 `rail-agent run trip.json --preview --direct`，程序会停在“提交订单”之前，供你核对信息。
4. 检查网站未完成订单，再执行 `rail-agent clear-attempt --checked-orders` 清除未完成订单。
5. 正式运行时执行 `rail-agent run trip.json` 自动购票，进入待支付页面后自行支付。终端按回车关闭浏览器。

> 注：按日期先后、页面车次顺序购买首个符合条件的车次。仅在指定席别余票足够全部乘车人时预订，不买候补、无座或部分乘车人的票。学生资格由网站校验；登录、验证码和未知弹窗需人工处理。

## 登录与重复订单

默认状态目录固定在本项目的 `.rail-agent`，不会随命令运行目录改变；非源码安装默认使用用户主目录的 `.rail-agent`。启动时会打印绝对路径。`browser` 保存浏览器配置和本地存储，`session.json` 额外保存会话 Cookie，启动时自动恢复。不能直接继承平时 Chrome 的登录标签页。之后直接运行 `run`，不必先运行 `login`。网站使会话过期或失效时仍需重新登录。

也可指定固定路径，所有命令必须使用同一路径（`--state-dir` 放在子命令前）：

```bash
rail-agent --state-dir /你的固定目录/rail-session login
rail-agent --state-dir /你的固定目录/rail-session run trip.json --preview --direct
```

或在 `.env` 中设置 `RAIL_STATE_DIR=/你的固定目录/rail-session`。使用原有 `.rail-agent` 目录即可保留已有数据。不要同时运行多个使用该目录的任务。登录完成或手动支付后，通过终端回车正常保存并关闭；直接关闭整个浏览器可能导致本次会话无法保存。`clear-attempt` 只清除购票尝试记录，不清除登录状态。不要分享 `session.json`。

预订前写入 `.rail-agent/attempt.json`。预订后的失败、崩溃或超时都会留下记录，后续运行拒绝重复购票。检查网站未完成订单后才执行 `clear-attempt --checked-orders`；该命令只删除本地记录，不取消网站订单。

`.env`、`trip.json`、浏览器目录已被 Git 忽略。不要分享浏览器目录。模型仅收到工具状态，不接收乘车人列表、截图、Cookie 或身份证信息。

## 开发

```bash
python -m unittest discover -s tests -v
```

测试覆盖时间/车次过滤、多人余票、查询间隔、提交前持久化、防重复尝试和预览不提交，不产生真实订单。网页选择器集中在 `rail_agent/browser.py`。F 座和待支付标记仍需账户联调确认。

`tests/test_query_browser.py` 使用 Chromium 和本地模拟页面，验证延迟初始化、站名候选项的键盘事件、完整表单填写及查询请求。网络请求全部被本地拦截，不访问账户；需先安装 Playwright Chromium。

LangChain `create_agent` 调用无参数工具 `watch_and_book`；本地配置固定行程，工具确定性执行网页流程，限制模型修改行程或重复下单。模型错误不能作为网站订单失败的重试依据。

参考：[12306 查询页](https://kyfw.12306.cn/otn/leftTicket/init)、[LangChain agents](https://docs.langchain.com/oss/python/langchain/agents)、[Playwright 登录状态](https://playwright.dev/python/docs/auth)。
