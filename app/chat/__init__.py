"""聊天接入包：独立、可插拔的渠道适配层，只负责连接与消息收发。"""
from .base import ChatAdapter

__all__ = ["ChatAdapter"]
