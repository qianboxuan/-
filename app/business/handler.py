"""业务逻辑与聊天接入之间的接口定义。

聊天接入模块只依赖这里的 MessageHandler 抽象接口，业务逻辑实现
（如 DeepSeek 处理）完全不感知底层是 QQ 还是其它渠道，从而保证
「聊天接入」可作为独立、可插拔的模块存在。
"""
from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..chat.base import ChatAdapter


def split_long_reply(text: str, max_len: int = 20) -> List[str]:
    """把回复拆成多条超短消息，每个短句一条（模拟真人碎片化发消息）。

    - 按句末标点（。！？~～…换行）拆分，一句一条；
    - 单句超过 max_len 时再按逗号细拆；
    - 保护 CQ 码（如 [CQ:face,id=21]），避免被逗号/冒号拆分拆坏。
    """
    # 保护 CQ 码：用占位符替换，拆分后再还原
    cq_codes: List[str] = []

    def _protect(m: "re.Match") -> str:
        cq_codes.append(m.group(0))
        return f"\x00CQ{len(cq_codes) - 1}\x00"

    def _restore(m: "re.Match") -> str:
        return cq_codes[int(m.group(1))]

    protected = re.sub(r"\[CQ:[^\]]*\]", _protect, text)

    sentences = re.split(r"(?<=[。！？!?~～…])", protected)
    result: List[str] = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if len(s) > max_len:
            subs = re.split(r"(?<=[，,、：:；;])", s)
            cur = ""
            for sub in subs:
                sub = sub.strip()
                if not sub:
                    continue
                if cur and len(cur) + len(sub) > max_len:
                    result.append(cur)
                    cur = sub
                else:
                    cur += sub
            if cur:
                result.append(cur)
        else:
            result.append(s)
    restored = [re.sub(r"\x00CQ(\d+)\x00", _restore, r) for r in result]
    return restored or [text]


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
        """向消息来源会话回复文本；长文本自动分段逐条发送（模拟真人聊天）。"""
        if not text:
            return True
        segments = split_long_reply(text)
        ok = True
        for i, seg in enumerate(segments):
            if self.message_type == "group" and self.group_id:
                r = await self.adapter.send_group(self.group_id, seg)
            else:
                r = await self.adapter.send_private(self.user_id, seg)
            ok = ok and r
            # 多条之间稍作停顿，模拟真人打字节奏
            if i < len(segments) - 1:
                await asyncio.sleep(0.6)
        return ok


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
