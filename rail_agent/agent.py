"""The model invokes a bounded tool and cannot change the configured trip."""

def run_agent(booker, model_name):
    from langchain.agents import create_agent
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI

    result = None

    @tool
    def watch_and_book() -> str:
        """按本地配置监控余票、选择已有乘车人并购票，停在支付前。只能执行一次。"""
        nonlocal result
        if result is not None:
            return result
        try:
            result = booker.run()
        except Exception as exc:
            from .browser import ManualAction
            result = str(exc) if isinstance(exc, ManualAction) else f"操作停止：{type(exc).__name__}；请查看浏览器及未完成订单，不要直接重试。"
        return result

    agent = create_agent(
        model=ChatOpenAI(model=model_name, temperature=0), tools=[watch_and_book],
        system_prompt="调用 watch_and_book 一次，按工具结果答复。不要声称未经工具确认的成功。无需索取密码或验证码。",
    )
    agent.invoke({"messages": [{"role": "user", "content": "执行本地配置的购票任务。"}]}, {"recursion_limit": 6})
    if result is None:
        raise RuntimeError("模型未调用购票工具；可使用 --direct 调试")
    return result
