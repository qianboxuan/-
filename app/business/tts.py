"""文字转语音（TTS）客户端：基于微软 Edge TTS（免费，无需 key）。

把文字合成中文语音（默认活泼俏皮的「晓伊」女声），输出 mp3 文件，
供聊天接入层以语音消息发送。
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

from ..config import Config
from ..logging_setup import get_logger

log = get_logger("business.tts")


class TTSClient:
    def __init__(self, config: Config):
        self.enabled = bool(config.get("business.tts.enabled", False))
        self.voice = str(config.get("business.tts.voice", "zh-CN-XiaoyiNeural"))
        self.output_dir = str(config.get("business.tts.output_dir", "data/voice"))
        if not self.enabled:
            return
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            log.error("缺少依赖 edge-tts，请先 pip install edge-tts；语音功能已禁用")
            self.enabled = False
            return
        log.info("TTS 已就绪：voice=%s", self.voice)

    async def synthesize(self, text: str) -> Optional[str]:
        """文字转语音，返回 mp3 文件路径。失败返回 None。"""
        if not self.enabled or not text.strip():
            return None
        try:
            import edge_tts
            os.makedirs(self.output_dir, exist_ok=True)
            path = os.path.join(self.output_dir, f"{uuid.uuid4().hex}.mp3")
            communicate = edge_tts.Communicate(text.strip(), self.voice)
            await communicate.save(path)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return path
            return None
        except Exception as exc:
            log.error("TTS 合成失败：%s", exc)
            return None
