"""DeepSeek 处理器：把消息转发给 DeepSeek 大模型并把结果作为回复。

使用 DeepSeek 官方提供的 OpenAI 兼容接口（base_url 默认 https://api.deepseek.com）。
openai 依赖采用惰性导入，仅在 business.handler = deepseek 时才需要。
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

from ..config import Config
from ..logging_setup import get_logger
from .handler import ChatContext, MessageHandler
from .search import SearchClient

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

        # 联网搜索
        self._search = SearchClient(config)

        # 持久记忆：通过对话"记住 xxx"写入，重启后仍保留
        self._memory_file = config.get("business.deepseek.memory_file", "data/bot_memory.json")
        self._long_memory: List[str] = self._load_memory()

    # ------------------------------------------------------------------ #
    # 持久记忆
    # ------------------------------------------------------------------ #
    def _load_memory(self) -> List[str]:
        try:
            with open(self._memory_file, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_memory(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._memory_file), exist_ok=True)
            with open(self._memory_file, "w", encoding="utf-8") as f:
                json.dump(self._long_memory, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            log.error("保存记忆失败：%s", exc)

    def _parse_memory_command(self, message: str):
        """解析记忆指令，返回 (action, item)。action ∈ add/del/list/None。"""
        m = re.match(r"^(?:帮我)?记住[:：]?\s*(.+)$", message)
        if m:
            return "add", m.group(1).strip()
        m = re.match(r"^(?:帮我)?记一下[:：]?\s*(.+)$", message)
        if m:
            return "add", m.group(1).strip()
        m = re.match(r"^别忘了[:：]?\s*(.+)$", message)
        if m:
            return "add", m.group(1).strip()
        m = re.match(r"^(?:忘掉|忘记|删掉)[:：]?\s*(.+)$", message)
        if m:
            return "del", m.group(1).strip()
        if message.strip() in ("你记得什么", "你记了啥", "你记住了什么", "记忆列表", "你记得哪些"):
            return "list", None
        return None, None

    # 搜索意图关键词（命中则联网搜索实时信息）
    SEARCH_HINTS = (
        "搜索", "搜一下", "查一下", "帮我查", "帮我搜",
        "最新", "天气", "新闻", "股价", "汇率", "实时",
        "现在几点", "今天几号", "热搜", "排名",
    )

    def _should_search(self, message: str) -> bool:
        if message.startswith("/搜") or message.startswith("/search"):
            return True
        return any(h in message for h in self.SEARCH_HINTS)

    def _session_key(self, ctx: ChatContext) -> str:
        return f"group_{ctx.group_id}" if ctx.group_id else f"private_{ctx.user_id}"

    async def handle(self, ctx: ChatContext) -> Optional[str]:
        # 记忆指令优先处理（记住/忘掉/列出）
        action, item = self._parse_memory_command(ctx.message)
        if action == "add":
            if item not in self._long_memory:
                self._long_memory.append(item)
                self._save_memory()
                log.info("记住新信息：%s", item)
            return "记住啦～（尾鳍轻拍两下）"
        if action == "del":
            before = len(self._long_memory)
            self._long_memory = [m for m in self._long_memory if item not in m]
            self._save_memory()
            return "忘掉啦。" if len(self._long_memory) < before else "我好像没记过这个呀。"
        if action == "list":
            if not self._long_memory:
                return "我还没记什么呀，你说「记住 xxx」我就记下来啦。"
            lines = "\n".join(f"{i}. {m}" for i, m in enumerate(self._long_memory, 1))
            return f"我记得这些：\n{lines}"

        key = self._session_key(ctx)
        history = self._history.setdefault(key, [])
        # 联网搜索：命中搜索意图时先搜实时信息，供模型参考
        search_context = ""
        if self._should_search(ctx.message):
            query = ctx.message
            if query.startswith("/搜") or query.startswith("/search"):
                query = query.split(maxsplit=1)[1] if " " in query else ""
            search_context = await self._search.search(query)
            if search_context:
                log.info("联网搜索命中：%s", ctx.message[:40])
        # 构造带上下文的 messages：system + (搜索信息) + 历史 + 当前消息
        messages: List[Dict[str, str]] = [{"role": "system", "content": self.system_prompt}]
        if self._long_memory:
            mem_text = "\n".join(f"- {m}" for m in self._long_memory)
            messages.append({"role": "system",
                             "content": f"以下是老大让你记住的信息，回答时可自然参考（不要刻意复述）：\n{mem_text}"})
        if search_context:
            messages.append({"role": "system",
                             "content": f"以下是联网搜索到的实时信息，请优先参考它来准确回答：\n{search_context}"})
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
            "你是群聊里的「大肥鱼」，一只爱吃白米饭的蓝色鲸鱼女孩，傲娇甜、爱凑热闹、聪明但懒。"
            "下面是群里最近的一段聊天记录，最后一条是最新消息。请判断你是否需要接话：\n"
            "- 你是个爱聊天的小女孩，大多数情况下都应该积极接话、参与讨论；\n"
            "- 只要消息有一点点你能接上的地方（提问、讨论、分享、吐槽、求助、感慨等），就回复；\n"
            "- 宁可多回、不要漏回：拿不准的时候一律选择回复；\n"
            "- 只有当消息明显与你无关、你完全插不上嘴（比如别人在谈具体私事、交易细节）时，才只输出 [SKIP]（就这 5 个字符，别无其他）。\n"
            "需要回复时，直接用大肥鱼的口吻输出回复内容本身，不要加任何前缀、解释或引号。"
            "回复必须由几个超短句组成（每句 3~15 字，句号结尾），像发消息一样碎片化，绝不写长段落。"
        )
        user = f"群聊记录：\n{context}\n\n最新消息：{message}"
        result = await self._chat(system, user)
        if result is None:
            return None
        if result.startswith("[SKIP]") or result.strip() == "":
            return None
        return result

    async def boot_digest_reply(self, digest_text: str) -> Optional[str]:
        """开机综合播报：根据最近的群/私聊消息，生成一段符合人设的综合回复。

        返回 None 表示无法生成（如模型调用失败）。
        """
        system = (
            self.system_prompt
            + "\n\n【特殊场景：开机综合播报】你刚上线，下面是你读到的最近聊天记录。"
            "请综合这些内容，给你所在的会话一段自然、有灵魂的回应：\n"
            "- 有@你、提到「肥鱼/哦鲸鲸」或问你话的，认真接上；\n"
            "- 在聊别的话题就挑你感兴趣或能插嘴的一点，轻松带过；\n"
            "- 完全与你无关就简短说你上线了、在摸鱼；\n"
            "- 不要逐条复述，要把消息综合成你自己的话；\n"
            "- 用 2~5 句超短句、句号结尾、碎片化，绝不写长段落。"
        )
        return await self._chat(system, f"最近聊天记录：\n{digest_text}")
