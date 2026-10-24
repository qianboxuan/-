"""视觉模型客户端：调用多模态大模型识图，返回图片描述。

支持 OpenAI 兼容接口的视觉模型（Kimi / 智谱 GLM-4V / 通义 Qwen-VL）。
配置读取 business.vision，api_key 优先取配置，其次从环境变量读。
"""
from __future__ import annotations

import os
from typing import Optional

from ..config import Config
from ..logging_setup import get_logger

log = get_logger("business.vision")

# 各 provider 的默认 base_url 与 model
_PROVIDERS = {
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2.6",
        "env_key": "KIMI_API_KEY",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4v-flash",
        "env_key": "GLM_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-plus",
        "env_key": "DASHSCOPE_API_KEY",
    },
}


class VisionClient:
    def __init__(self, config: Config):
        v = config.get("business.vision", {}) or {}
        self.enabled = bool(v.get("enabled", False))
        provider = str(v.get("provider", "kimi")).lower()
        info = _PROVIDERS.get(provider, _PROVIDERS["kimi"])

        self.base_url = str(v.get("base_url", "") or info["base_url"])
        self.model = str(v.get("model", "") or info["model"])
        self.timeout = float(v.get("timeout", 60))

        api_key = str(v.get("api_key", "") or "")
        if not api_key:
            api_key = os.environ.get(info["env_key"], "")
        self.api_key = api_key

        self._client = None
        if not self.enabled:
            return
        if not self.api_key:
            log.warning("视觉模型未配置 API Key，识图功能不可用（请设置 %s）", info["env_key"])
            self.enabled = False
            return
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            log.error("缺少依赖 openai，无法使用视觉模型")
            self.enabled = False
            return
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
        )
        log.info("视觉模型已就绪：%s（%s）", self.model, self.base_url)

    async def describe(self, image_url: str, question: str = "请详细描述这张图片的内容") -> Optional[str]:
        """识别一张图片，返回中文描述。失败返回 None。"""
        if not self.enabled or self._client is None:
            return None
        try:
            # Kimi 视觉不支持 URL，需下载图片后 base64 编码
            import base64
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as http:
                r = await http.get(image_url)
                r.raise_for_status()
                img = r.content
            mime = self._guess_mime(image_url, r.headers.get("content-type", ""))
            data_url = f"data:{mime};base64,{base64.b64encode(img).decode()}"
            log.info("图片下载成功：%d 字节，%s", len(img), mime)

            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": question},
                        ],
                    }
                ],
                max_tokens=512,
            )
            content = (resp.choices[0].message.content or "").strip()
            if not content:
                log.warning("Kimi 返回空内容，原始响应：%s", str(resp)[:400])
            return content or None
        except Exception as exc:
            log.error("视觉模型识图失败：%s", exc)
            return None

    @staticmethod
    def _guess_mime(url: str, header_ct: str = "") -> str:
        """根据响应头或 URL 后缀猜测图片 MIME 类型。"""
        if header_ct and header_ct.startswith("image/"):
            return header_ct.split(";")[0].strip()
        path = url.split("?")[0]
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        mapping = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
            "heic": "image/heic", "heif": "image/heif",
        }
        return mapping.get(ext, "image/jpeg")
