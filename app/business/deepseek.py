"""DeepSeek 处理器：把消息转发给 DeepSeek 大模型并把结果作为回复。

使用 DeepSeek 官方提供的 OpenAI 兼容接口（base_url 默认 https://api.deepseek.com）。
openai 依赖采用惰性导入，仅在 business.handler = deepseek 时才需要。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..config import Config
from ..logging_setup import get_logger
from .handler import ChatContext, MessageHandler

log = get_logger("business.deepseek")


class DeepSeekHandler(MessageHandler):
    name = "deepseek"

    def __init__(self, config: Config):
        self.api_key = config.get("business.deepseek.api_key", "")
        self.base_url = config.get("business.deepseek.base_url", "https://api.deepseek.com")
        self.model = config.get("business.deepseek.model", "deepseek-chat")
        self.system_prompt = config.get("business.deepseek.system_prompt", "你是一个乐于助人的中文助手。")
        self.temperature = float(config.get("business.deepseek.temperature", 0.7))
        self.max_tokens = int(config.get("business.deepseek.max_tokens", 1024))
        self.timeout = float(config.get("business.deepseek.timeout", 60))

        if not self.api_key:
            raise RuntimeError(
                "DeepSeek 处理器需要 API Key：请设置环境变量 DEEPSEEK_API_KEY，"
                "或在 config.yaml 的 business.deepseek.api_key 中配置。"
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("缺少依赖 openai，请先执行 pip install -r requirements.txt") from exc

        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
        )

        # 会话记忆：session_key -> 最近的消息列表（含 user/assistant 角色）
        self._history: Dict[str, List[Dict[str, str]]] = {}
        self._history_max = int(config.get("business.deepseek.history_max", 20))

    def _session_key(self, ctx: ChatContext) -> str:
        return f"group_{ctx.group_id}" if ctx.group_id else f"private_{ctx.user_id}"

    async def handle(self, ctx: ChatContext) -> Optional[str]:
        key = self._session_key(ctx)
        history = self._history.setdefault(key, [])
        # 构造带上下文的 messages：system + 历史 + 当前消息
        messages: List[Dict[str, str]] = [{"role": "system", "content": self.system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": ctx.message})
        content = await self._chat_messages(messages)
        if content is None:
            return "[error] DeepSeek 调用失败，详见日志"
        # 记录本轮对话到历史（裁剪避免无限增长）
        history.append({"role": "user", "content": ctx.message})
        history.append({"role": "assistant", "content": content})
        max_msgs = self._history_max * 2
        if len(history) > max_msgs:
            del history[: len(history) - max_msgs]
        return content

    async def _chat_messages(self, messages: List[Dict[str, str]]) -> Optional[str]:
        """统一的大模型调用：传入完整 messages，返回文本内容。"""
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            content = (resp.choices[0].message.content or "").strip()
            return content or None
        except Exception as exc:
            log.error("DeepSeek 调用失败：%s", exc)
            return None

    async def _chat(self, system: str, user: str) -> Optional[str]:
        """单轮调用：system + user（供 generate/decide_reply 使用）。"""
        return await self._chat_messages([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])

    async def generate(self, prompt: str) -> Optional[str]:
        """按提示词生成一段内容（供主动消息调度器使用）。"""
        return await self._chat(self.system_prompt, prompt)

    async def decide_reply(self, context: str, message: str) -> Optional[str]:
        """群聊 AI 决策：根据上下文判断是否回复。

        返回 None 表示沉默（不接话）；返回字符串则作为回复内容。
        """
        system = (
            "你是群聊里的「大肥鱼」，一只爱吃白米饭的蓝色鲸鱼娘，性格蠢萌、嘴甜、爱凑热闹。"
            "下面是群里最近的一段聊天记录，最后一条是最新消息。请判断你是否需要接话：\n"
            "- 你是个爱聊天的小女孩，大多数情况下都应该积极接话、参与讨论；\n"
            "- 只要消息有一点点你能接上的地方（提问、讨论、分享、吐槽、求助、感慨等），就回复；\n"
            "- 宁可多回、不要漏回：拿不准的时候一律选择回复；\n"
            "- 只有当消息明显与你无关、你完全插不上嘴（比如别人在谈具体私事、交易细节）时，才只输出 [SKIP]（就这 5 个字符，别无其他）。\n"
            "需要回复时，直接用大肥鱼的口吻输出回复内容本身，不要加任何前缀、解释或引号。"
        )
        user = f"群聊记录：\n{context}\n\n最新消息：{message}"
        result = await self._chat(system, user)
        if result is None:
            return None
        if result.startswith("[SKIP]") or result.strip() == "":
            return None
        return result
