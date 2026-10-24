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
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..business.handler import MessageHandler, split_long_reply
from ..business.tts import TTSClient
from ..config import Config
from ..logging_setup import get_logger
from .base import ChatAdapter

log = get_logger("chat.proactive")


class ProactiveManager:
    def __init__(self, config: Config, adapter: ChatAdapter, handler: MessageHandler):
        self._config = config
        self._adapter = adapter
        self._handler = handler
        self._tts = TTSClient(config)
        self._enabled = bool(config.get("chat.proactive.enabled", False))
        self._tasks: List[Dict[str, Any]] = config.get("chat.proactive.tasks", []) or []
        self._owner = int(config.get("chat.owner", 0) or 0)
        # 智能主动活跃配置（冷场驱动，非定时器）
        act = config.get("chat.proactive.active", {}) or {}
        self._active_enabled = bool(act.get("enabled", False))
        self._idle_seconds = int(act.get("idle_minutes", 30)) * 60
        self._check_interval = int(act.get("check_interval", 60))
        self._active_groups: List[int] = [int(x) for x in (act.get("groups") or [])]
        self._mention_ratio = float(act.get("mention_ratio", 0.3))
        self._emoji_ratio = float(act.get("emoji_ratio", 0.2))
        self._active_prompt = str(act.get("prompt", "") or "")
        self._emoji_dir = str(config.get("chat.proactive.emoji_dir", "assets/emojis"))
        self._last_active_sent: Dict[int, float] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._active_task: Optional[asyncio.Task] = None
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
        has_active = self._active_enabled and self._active_groups and self._active_prompt
        if not has_tasks and not has_active:
            log.info("主动消息调度未启用（无定时任务，且智能活跃未配置完整）")
            return
        self._running = True
        if has_tasks:
            self._task = asyncio.create_task(self._scheduler_loop(), name="proactive-scheduler")
            log.info("主动消息定时调度启动，共 %d 个任务", len(self._tasks))
        if has_active:
            self._active_task = asyncio.create_task(self._active_loop(), name="proactive-active")
            log.info("智能主动活跃已启用（群冷场 %d 分钟才主动，检查间隔 %d 秒，群=%s）",
                     self._idle_seconds // 60, self._check_interval, self._active_groups)

    async def stop(self) -> None:
        self._running = False
        tasks = [t for t in (self._task, self._active_task) if t]
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
    # 智能主动活跃（冷场驱动）
    # ------------------------------------------------------------------ #
    async def _active_loop(self) -> None:
        """冷场检测：群里安静够久时，才主动找话题/发表情包/@人活跃气氛。"""
        while self._running:
            await asyncio.sleep(self._check_interval)
            for gid in self._active_groups:
                try:
                    idle = self._adapter.get_group_idle_seconds(gid)
                    last_sent = self._last_active_sent.get(gid, 0)
                    cooldown_ok = (time.time() - last_sent) >= self._idle_seconds
                    if idle >= self._idle_seconds and cooldown_ok:
                        log.info("群 %s 冷场 %.0f 秒，主动活跃", gid, idle)
                        await self._active_once(gid)
                except Exception:
                    log.exception("主动活跃检查出错（群 %s）", gid)

    async def _active_once(self, gid: int) -> None:
        """冷场后随机选择一种活跃方式：@人 / 发表情包 / 找话题。"""
        self._last_active_sent[gid] = time.time()
        r = random.random()
        if r < self._mention_ratio:
            await self._active_mention(gid)
        elif r < self._mention_ratio + self._emoji_ratio:
            await self._active_emoji(gid)
        else:
            await self._active_topic(gid)

    async def _active_mention(self, gid: int) -> None:
        """随机 @ 一个群成员打招呼。"""
        members = await self._adapter.get_group_member_list(gid)
        self_qq = int(getattr(self._adapter, "self_id", 0) or 0)
        members = [m for m in members if m.get("user_id") != self_qq]
        if not members:
            log.warning("群 %s 无可用成员，跳过 @", gid)
            return
        chosen = random.choice(members)
        content = await self._generate(self._active_prompt)
        if content:
            mention = f"[CQ:at,qq={chosen['user_id']}]"
            ok = await self._adapter.send_group(gid, f"{mention} {content}")
            log.info("冷场活跃 @%s(%s)：%s（%s）",
                     chosen["nickname"], chosen["user_id"], content[:100], "成功" if ok else "失败")

    async def _active_emoji(self, gid: int) -> None:
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
        ok = await self._adapter.send_image("group", gid, img)
        log.info("冷场活跃发表情包 %s（%s）", os.path.basename(img), "成功" if ok else "失败")

    async def _active_topic(self, gid: int) -> None:
        """基于群历史主动找话题（结合最近聊天内容，自然不突兀）。"""
        hist = ""
        get_hist = getattr(self._adapter, "_get_history", None)
        if callable(get_hist):
            hist = get_hist(gid) or ""
        prompt = self._active_prompt
        if hist:
            prompt = f"{prompt}\n\n群里最近聊到的内容（供你自然衔接话题，不要重复）：\n{hist}"
        content = await self._generate(prompt)
        if content:
            ok = await self._adapter.send_group(gid, content)
            log.info("冷场活跃找话题：%s（%s）", content[:100], "成功" if ok else "失败")

    # ------------------------------------------------------------------ #
    # 主动发送（AI 生成内容）
    # ------------------------------------------------------------------ #
    async def send_active(self, message_type: str, target: int, prompt: str) -> bool:
        """生成内容并主动发送到指定会话（长内容自动分段）。"""
        if not prompt:
            log.warning("主动发送缺少 prompt，已取消")
            return False
        content = await self._generate(prompt)
        if not content:
            log.warning("内容生成失败，取消发送")
            return False
        segments = split_long_reply(content)
        ok = True
        for i, seg in enumerate(segments):
            if message_type == "group":
                r = await self._adapter.send_group(target, seg)
            else:
                r = await self._adapter.send_private(target, seg)
            ok = ok and r
            if i < len(segments) - 1:
                await asyncio.sleep(0.6)
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
        # /语音 指令：TTS 合成后发语音
        if text.startswith("/语音") or text.startswith("/voice"):
            content = ""
            if " " in text or "\u3000" in text:
                content = text.split(maxsplit=1)[1].strip()
            if not content:
                await self._adapter.send_private(user_id, "用法：/语音 想说的话")
                return True
            if not self._tts or not self._tts.enabled:
                await self._adapter.send_private(user_id, "语音功能未开启（business.tts.enabled=false 或缺少 edge-tts）")
                return True
            audio = await self._tts.synthesize(content)
            if audio:
                ok = await self._adapter.send_voice("private", user_id, audio)
                if not ok:
                    await self._adapter.send_private(user_id, content)
            else:
                await self._adapter.send_private(user_id, content)
            return True

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
