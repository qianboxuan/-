"""主动消息调度器。

提供两类「主动发消息」能力（内容均由业务处理器生成，如 DeepSeek）：
1. 定时任务：按 chat.proactive.tasks 配置的日程（每天 HH:MM）自动发送；
2. 主人指令：主人在私聊中发送 `/send <private|group> <目标QQ/群号> <内容提示词>`，
   由 AI 现编内容后主动发送给目标。

复用 ChatAdapter.send_group / send_private，独立于消息接收链路，可插拔。
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..business.handler import MessageHandler
from ..config import Config
from ..logging_setup import get_logger
from .base import ChatAdapter

log = get_logger("chat.proactive")


class ProactiveManager:
    def __init__(self, config: Config, adapter: ChatAdapter, handler: MessageHandler):
        self._config = config
        self._adapter = adapter
        self._handler = handler
        self._enabled = bool(config.get("chat.proactive.enabled", False))
        self._tasks: List[Dict[str, Any]] = config.get("chat.proactive.tasks", []) or []
        self._owner = int(config.get("chat.owner", 0) or 0)
        # 随机主动消息配置
        rand = config.get("chat.proactive.random", {}) or {}
        self._random_enabled = bool(rand.get("enabled", False))
        self._interval_min = int(rand.get("interval_min", 1800))
        self._interval_max = int(rand.get("interval_max", 7200))
        self._random_type = str(rand.get("type", "private"))
        self._random_target = int(rand.get("target", 0) or 0)
        self._random_prompt = str(rand.get("prompt", "") or "")
        self._pick_from_group = int(rand.get("pick_from_group", 0) or 0)
        self._mention_ratio = float(rand.get("mention_ratio", 0.3))
        self._emoji_ratio = float(rand.get("emoji_ratio", 0.2))
        self._emoji_dir = str(config.get("chat.proactive.emoji_dir", "assets/emojis"))
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._random_task: Optional[asyncio.Task] = None
        self._fired_today: set = set()
        self._today = None

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._tasks)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        has_tasks = self._enabled and bool(self._tasks)
        has_random = self._random_enabled and self._random_target and self._random_prompt
        if not has_tasks and not has_random:
            log.info("主动消息调度未启用（无定时任务，且随机发送未配置完整）")
            return
        self._running = True
        if has_tasks:
            self._task = asyncio.create_task(self._scheduler_loop(), name="proactive-scheduler")
            log.info("主动消息定时调度启动，共 %d 个任务", len(self._tasks))
        if has_random:
            self._random_task = asyncio.create_task(self._random_loop(), name="proactive-random")
            log.info("随机主动消息已启用（间隔 %d~%d 秒 -> %s %s）",
                     self._interval_min, self._interval_max, self._random_type, self._random_target)

    async def stop(self) -> None:
        self._running = False
        tasks = [t for t in (self._task, self._random_task) if t]
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ #
    # 定时调度
    # ------------------------------------------------------------------ #
    async def _scheduler_loop(self) -> None:
        while self._running:
            now = datetime.now()
            if self._today != now.date():
                self._today = now.date()
                self._fired_today.clear()
            hhmm = now.strftime("%H:%M")
            for task in self._tasks:
                if task.get("schedule") == hhmm and task.get("name") not in self._fired_today:
                    self._fired_today.add(task.get("name"))
                    log.info("触发定时主动任务：%s", task.get("name"))
                    await self._fire_task(task)
            await asyncio.sleep(30)  # 每 30 秒对齐一次，避免错过分钟

    async def _fire_task(self, task: Dict[str, Any]) -> None:
        try:
            await self.send_active(
                message_type=task.get("type", "private"),
                target=int(task.get("target") or 0),
                prompt=task.get("prompt", ""),
            )
        except Exception:
            log.exception("定时主动任务执行失败：%s", task.get("name"))

    # ------------------------------------------------------------------ #
    # 随机主动消息
    # ------------------------------------------------------------------ #
    async def _random_loop(self) -> None:
        """不定时随机主动冒泡：在 interval_min~interval_max 秒之间随机触发。"""
        while self._running:
            wait = random.uniform(self._interval_min, self._interval_max)
            log.info("随机主动消息：%.0f 秒后触发", wait)
            await asyncio.sleep(wait)
            if not self._running:
                break
            try:
                if self._pick_from_group:
                    await self._random_group_activity()
                else:
                    ok = await self.send_active(self._random_type, self._random_target, self._random_prompt)
                    log.info("随机主动消息 %s -> %s：%s",
                             self._random_type, self._random_target, "成功" if ok else "失败")
            except Exception:
                log.exception("随机主动消息发送失败")

    async def _random_group_activity(self) -> None:
        """随机群活跃：@人 / 发表情包 / 讲笑话抛话题（按比例随机）。"""
        r = random.random()
        if r < self._mention_ratio:
            await self._random_mention()
        elif r < self._mention_ratio + self._emoji_ratio:
            await self._random_emoji()
        else:
            await self._random_text()

    async def _random_mention(self) -> None:
        """随机 @ 一个群成员打招呼。"""
        members = await self._adapter.get_group_member_list(self._pick_from_group)
        self_qq = int(getattr(self._adapter, "self_id", 0) or 0)
        members = [m for m in members if m.get("user_id") != self_qq]
        if not members:
            log.warning("群 %s 无可用成员，跳过 @", self._pick_from_group)
            return
        chosen = random.choice(members)
        content = await self._generate(self._random_prompt)
        if content:
            mention = f"[CQ:at,qq={chosen['user_id']}]"
            ok = await self._adapter.send_group(self._pick_from_group, f"{mention} {content}")
            log.info("群里随机@ %s(%s)：%s（%s）",
                     chosen["nickname"], chosen["user_id"], content[:100], "成功" if ok else "失败")

    async def _random_emoji(self) -> None:
        """从表情包目录随机选一张图发到群里。"""
        import glob
        import os
        exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
        files = []
        for ext in exts:
            files += glob.glob(os.path.join(self._emoji_dir, "*" + ext))
            files += glob.glob(os.path.join(self._emoji_dir, "*" + ext.upper()))
        if not files:
            log.warning("表情包目录 %s 为空，跳过发表情包", self._emoji_dir)
            return
        img = random.choice(files)
        ok = await self._adapter.send_image("group", self._pick_from_group, img)
        log.info("群里发表情包 %s（%s）", os.path.basename(img), "成功" if ok else "失败")

    async def _random_text(self) -> None:
        """直接发文字内容（讲笑话/抛话题/分享）。"""
        content = await self._generate(self._random_prompt)
        if content:
            ok = await self._adapter.send_group(self._pick_from_group, content)
            log.info("群里主动发话题：%s（%s）", content[:100], "成功" if ok else "失败")

    # ------------------------------------------------------------------ #
    # 主动发送（AI 生成内容）
    # ------------------------------------------------------------------ #
    async def send_active(self, message_type: str, target: int, prompt: str) -> bool:
        """生成内容并主动发送到指定会话。"""
        if not prompt:
            log.warning("主动发送缺少 prompt，已取消")
            return False
        content = await self._generate(prompt)
        if not content:
            log.warning("内容生成失败，取消发送")
            return False
        if message_type == "group":
            ok = await self._adapter.send_group(target, content)
        else:
            ok = await self._adapter.send_private(target, content)
        log.info("主动发送 %s -> %s：%s", message_type, target, content[:120])
        return ok

    async def _generate(self, prompt: str) -> Optional[str]:
        gen = getattr(self._handler, "generate", None)
        if not callable(gen):
            log.error("当前业务处理器不支持内容生成（缺少 generate 方法），请使用 deepseek handler")
            return None
        return await gen(prompt)

    # ------------------------------------------------------------------ #
    # 主人指令
    # ------------------------------------------------------------------ #
    async def handle_command(self, text: str, user_id: int) -> bool:
        """识别主人指令。返回 True 表示已处理（不再走正常消息处理）。"""
        if not text.startswith("/send"):
            return False
        if self._owner and user_id != self._owner:
            log.warning("非主人（%s）尝试使用 /send 指令，已忽略", user_id)
            return False

        parts = text.split(maxsplit=3)
        if len(parts) < 4:
            await self._adapter.send_private(user_id, "指令格式：/send <private|group> <目标QQ或群号> <内容提示词>")
            return True
        _, mtype, target_s, prompt = parts
        if mtype not in ("private", "group"):
            await self._adapter.send_private(user_id, "类型只能是 private（私聊）或 group（群聊）")
            return True
        try:
            target = int(target_s)
        except ValueError:
            await self._adapter.send_private(user_id, "目标必须是数字 QQ 号或群号")
            return True
        ok = await self.send_active(mtype, target, prompt)
        await self._adapter.send_private(user_id, "✅ 已发送" if ok else "❌ 发送失败，详见日志")
        return True
