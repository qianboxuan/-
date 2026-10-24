"""聊天渠道适配器的抽象接口。

新增一种聊天渠道（例如其它 IM 协议）时，只需继承 ChatAdapter 并实现
run/stop/send_group/send_private，再在 main.py 的注册表里登记即可，
业务逻辑（MessageHandler）无需任何改动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ChatAdapter(ABC):
    """聊天渠道适配器基类。"""

    name = "base"

    @abstractmethod
    async def run(self) -> None:
        """启动适配器，阻塞直至停止（内部负责连接与重连）。"""
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        """停止适配器，释放连接与任务。"""
        raise NotImplementedError

    @abstractmethod
    async def send_group(self, group_id: int, message: str) -> bool:
        """向群聊发送消息。"""
        raise NotImplementedError

    @abstractmethod
    async def send_private(self, user_id: int, message: str) -> bool:
        """向私聊发送消息。"""
        raise NotImplementedError
