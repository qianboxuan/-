"""业务逻辑包：与聊天接入解耦，通过 MessageHandler 接口被调用。"""
from .handler import ChatContext, MessageHandler

__all__ = ["ChatContext", "MessageHandler"]
