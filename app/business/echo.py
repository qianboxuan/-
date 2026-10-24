"""Echo 处理器：把收到的消息原样回显，用于联调验证「收消息→处理→回复」链路。"""
from __future__ import annotations

from typing import Optional

from .handler import ChatContext, MessageHandler


class EchoHandler(MessageHandler):
    name = "echo"

    async def handle(self, ctx: ChatContext) -> Optional[str]:
        return f"[echo] 收到你的消息：{ctx.message}"
