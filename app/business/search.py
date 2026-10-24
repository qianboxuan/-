"""联网搜索客户端：调用 DeepSeek 官方 Search API。

复用 DeepSeek API Key 鉴权，返回结构化网页结果（标题/链接/摘要），
供大模型参考来回答实时问题。
"""
from __future__ import annotations

from typing import Optional

import httpx

from ..config import Config
from ..logging_setup import get_logger

log = get_logger("business.search")


class SearchClient:
    def __init__(self, config: Config):
        self.enabled = bool(config.get("business.search.enabled", True))
        self.api_key = config.get("business.deepseek.api_key", "")
        self.base_url = config.get("business.deepseek.base_url", "https://api.deepseek.com")
        self.timeout = float(config.get("business.search.timeout", 20))
        self.count = int(config.get("business.search.count", 5))
        self._client: Optional[httpx.AsyncClient] = None
        if not self.enabled:
            return
        if not self.api_key:
            log.warning("联网搜索未配置 API Key（复用 DeepSeek key），已禁用")
            self.enabled = False
            return
        self._client = httpx.AsyncClient(timeout=self.timeout)
        log.info("联网搜索已就绪：%s/v1/tools/search", self.base_url)

    async def search(self, query: str, count: Optional[int] = None) -> str:
        """联网搜索，返回格式化的结果文本（供大模型参考）。失败返回空串。"""
        if not self.enabled or self._client is None:
            return ""
        count = count or self.count
        try:
            resp = await self._client.post(
                f"{self.base_url}/v1/tools/search",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"query": query, "count": count, "need_content": False},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("联网搜索失败：%s", exc)
            return ""
        results = data.get("results", []) or []
        if not results:
            return ""
        lines = []
        for r in results[:count]:
            title = r.get("title", "")
            summary = r.get("summary") or r.get("snippet") or ""
            url = r.get("url", "")
            lines.append(f"- {title}（{url}）：{summary}")
        return "\n".join(lines)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
