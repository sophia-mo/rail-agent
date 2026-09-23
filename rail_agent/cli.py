from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path

from .config import Trip


def main():
    parser = argparse.ArgumentParser(description="12306 LangChain 购票助手（人工登录、人工支付）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="手动登录专用浏览器，保存状态")
    clear = sub.add_parser("clear-attempt", help="检查未完成订单后清除尝试锁")
    clear.add_argument("--checked-orders", action="store_true", required=True)
    run = sub.add_parser("run", help="按 JSON 配置执行购票")
    run.add_argument("config", type=Path)
    run.add_argument("--preview", action="store_true", help="填写订单后暂停，不提交")
    run.add_argument("--direct", action="store_true", help="跳过模型，直接执行同一购票工具")
    run.add_argument("--start-at", help="北京时间定时查询，例如 16:30 或 '2026-09-23 16:30:00'，覆盖 JSON start_at")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    from .session import state_directory, restore_session, save_session, login_status
    args.state_dir = state_directory()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.state_dir.chmod(0o700)
    print(f"登录状态目录：{args.state_dir}", flush=True)
    if args.command == "clear-attempt":
        (args.state_dir / "attempt.json").unlink(missing_ok=True)
        print("尝试记录已清除。")
        return
    trip = None
    if args.command == "run":
        trip = Trip(**json.loads(args.config.read_text()))
        if args.start_at is not None:
            trip = replace(trip, start_at=args.start_at)
        trip.validate()
        print(f"配置文件：{args.config.resolve()}", flush=True)
        print(f"行程：{trip.origin} → {trip.destination}，{trip.date_start} 至 {trip.date_end}，"
              f"每天 {trip.time_start}–{trip.time_end}，{trip.seat}，"
              f"{'学生票' if trip.student else '成人票'}，车次：{', '.join(trip.trains) or '不限'}", flush=True)
        if not args.direct and (not os.getenv("OPENAI_API_KEY") or not os.getenv("RAIL_MODEL")):
            parser.error("请在 .env 中设置 OPENAI_API_KEY 和 RAIL_MODEL，或用 --direct 调试")
        if (args.state_dir / "attempt.json").exists():
            parser.error("存在上次尝试；请检查未完成订单，再执行 clear-attempt --checked-orders")
    from playwright.sync_api import sync_playwright
    from .browser import Booker, LOGIN_URL, ManualAction
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(args.state_dir / "browser"), headless=False, viewport={"width": 1360, "height": 900},
            locale="zh-CN", timezone_id="Asia/Shanghai",
        )
        # Restore session cookies before opening the task page.
        try:
            if restore_session(context, args.state_dir):
                print("已恢复保存的会话 Cookie；有效性以网站检查为准。", flush=True)
        except Exception as exc:
            print(f"无法恢复会话记录（{type(exc).__name__}），将使用浏览器目录中的状态。", flush=True)
        # Restored tabs may close/redirect during startup. Own a fresh task tab
        # while sharing the persistent context's existing login cookies.
        page = context.new_page()
        page.set_default_timeout(20000)
        login_saved = False
        try:
            if args.command == "login":
                page.goto(LOGIN_URL)
                input("请完成登录并等待跳转，保持浏览器打开，回到终端按回车保存：")
                save_session(context, args.state_dir)
                login_saved = True
                print("当前会话已保存，正在通过浏览器页面检查登录状态……", flush=True)
                status = login_status(context)
                # The check itself can refresh cookies; save those as well.
                save_session(context, args.state_dir)
                if status is True:
                    print("网站确认已登录，登录状态已保存。以后直接运行 run 即可。", flush=True)
                elif status is False:
                    print("会话已保存，但网页购票会话检查返回未登录；登录同步可能尚未完成。"
                          "可以先运行预览检查，保存成功不代表购票登录已生效。", flush=True)
                else:
                    print("会话已保存，但暂时无法确认登录有效性。可先运行预览检查，无需因本次检查未知而反复登录。", flush=True)
            else:
                booker = Booker(page, trip, args.state_dir, args.preview)
                if args.direct:
                    print(booker.run())
                else:
                    from .agent import run_agent
                    print(run_agent(booker, os.environ["RAIL_MODEL"]))
        except KeyboardInterrupt:
            print("自动操作已停止，若曾提交请检查未完成订单。")
        except Exception as exc:
            print(str(exc) if isinstance(exc, ManualAction) else f"操作停止：{type(exc).__name__}；请查看浏览器和未完成订单。")
        finally:
            if page.is_closed():
                print("受控标签页已关闭，自动操作已停止；其他窗口可能仍然打开。")
            if context.pages and not login_saved:
                try:
                    input("自动操作已停止。可在浏览器中检查页面或手动支付；处理完毕后按回车关闭：")
                except (KeyboardInterrupt, EOFError):
                    pass
            try:
                if not login_saved:
                    save_session(context, args.state_dir)
                    print("已保存当前会话。", flush=True)
            except Exception as exc:
                print(f"未能保存本次会话（{type(exc).__name__}）；下次请通过终端回车正常关闭浏览器。")
            finally:
                context.close()


if __name__ == "__main__":
    main()
