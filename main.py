"""程序入口：装配配置、日志、业务处理器与聊天适配器，并启动。"""
from __future__ import annotations

import asyncio
from typing import Dict, Type

from app.chat.base import ChatAdapter
from app.chat.onebot11 import OneBot11Adapter
from app.chat.proactive import ProactiveManager
from app.business.deepseek import DeepSeekHandler
from app.business.echo import EchoHandler
from app.business.handler import MessageHandler
from app.business.vision import VisionClient
from app.config import Config, load_config
from app.logging_setup import get_logger, setup_logging

# 可插拔注册表：新增渠道/处理器时在此登记即可
ADAPTERS: Dict[str, Type[ChatAdapter]] = {
    "onebot11": OneBot11Adapter,
}
HANDLERS: Dict[str, Type[MessageHandler]] = {
    "echo": EchoHandler,
    "deepseek": DeepSeekHandler,
}


def build_handler(config: Config) -> MessageHandler:
    name = config.get("business.handler", "echo")
    cls = HANDLERS.get(name)
    if cls is None:
        raise RuntimeError(f"未知的业务处理器：{name}（可选：{', '.join(HANDLERS)}）")
    # DeepSeekHandler 需要 config 里的 api_key
    return cls(config) if name == "deepseek" else cls()


def build_adapter(config: Config, handler: MessageHandler, vision: VisionClient = None) -> ChatAdapter:
    name = config.get("chat.adapter", "onebot11")
    cls = ADAPTERS.get(name)
    if cls is None:
        raise RuntimeError(f"未知的聊天适配器：{name}（可选：{', '.join(ADAPTERS)}）")
    return cls(config, handler, vision=vision)


async def async_main() -> None:
    config = load_config()
    setup_logging(config)
    log = get_logger("main")

    handler = build_handler(config)
    vision = VisionClient(config)
    adapter = build_adapter(config, handler, vision=vision)

    # 主动消息调度器（定时任务 + 主人 /send 指令），独立于消息接收链路
    proactive = ProactiveManager(config, adapter, handler)
    adapter.set_proactive(proactive)
    await proactive.run()

    log.info("启动服务：adapter=%s, handler=%s", adapter.name, handler.name)

    try:
        await adapter.run()
    finally:
        await proactive.stop()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n已收到退出信号，程序结束。")


if __name__ == "__main__":
    main()
