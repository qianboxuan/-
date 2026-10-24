"""业务逻辑与聊天接入之间的接口定义。

聊天接入模块只依赖这里的 MessageHandler 抽象接口，业务逻辑实现
（如 DeepSeek 处理）完全不感知底层是 QQ 还是其它渠道，从而保证
「聊天接入」可作为独立、可插拔的模块存在。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from ..chat.base import ChatAdapter


@dataclass
class ChatContext:
    """一条待处理消息的上下文。"""

    message_type: str                    # "group" | "private"
    user_id: int                         # 发消息的用户 QQ 号
    group_id: Optional[int]              # 群号（私聊为 None）
    message: str                         # 已提取的纯文本内容
    raw: Dict[str, Any]                  # 原始事件（OneBot 11 上报）
    adapter: "ChatAdapter"               # 回调适配器，用于主动回复
    sender_nickname: str = ""            # 发送者昵称/群名片

    async def reply(self, text: str) -> bool:
        """向消息来源会话回复文本（业务逻辑可直接调用）。"""
        if not text:
            return True
        if self.message_type == "group" and self.group_id:
            return await self.adapter.send_group(self.group_id, text)
        return await self.adapter.send_private(self.user_id, text)


class MessageHandler(ABC):
    """业务逻辑处理器接口。"""

    name = "base"

    @abstractmethod
    async def handle(self, ctx: ChatContext) -> Optional[str]:
        """处理一条消息。

        返回字符串则聊天接入层会将其回复到对应会话；
        返回 None 表示不回复（也可在实现中直接调用 ctx.reply 主动发多条）。
        """
        raise NotImplementedError

    async def generate(self, prompt: str) -> Optional[str]:
        """按提示词生成一段内容（用于主动消息）。默认返回 None 表示不支持。"""
        return None

    async def decide_reply(self, context: str, message: str) -> Optional[str]:
        """群聊 AI 决策：根据上下文判断是否回复。

        返回 None 表示沉默（不接话）；返回字符串则作为回复内容。
        默认返回 None（不支持 AI 决策，退回关键词触发）。
        """
        return None
