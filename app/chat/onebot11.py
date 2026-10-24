"""OneBot 11 协议聊天适配器。

OneBot 11 是 QQ 机器人接入的事实标准，NapCatQQ / LLOneBot / OpenShamrock 等
协议端均实现该标准，通过 WebSocket 上报事件、接收 API 调用。

支持两种连接模式（由 chat.onebot11.mode 控制）：
- reverse（默认）：本项目作为 WebSocket 服务端，协议端以「反向 WS」主动连入上报事件；
  断线重连由协议端负责，本项目只需处理好连接生命周期。
- forward：本项目作为 WebSocket 客户端，主动连接协议端的「正向 WS」地址；
  断线后由本项目按指数退避自动重连。

事件与 API 均遵循 OneBot 11 标准：
- 上报事件：post_type 为 message / notice / request / meta_event
- API 调用：{"action": "...", "params": {...}, "echo": "..."}
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from ..business.handler import ChatContext, MessageHandler, split_long_reply
from ..config import Config
from ..logging_setup import get_logger
from .base import ChatAdapter

log = get_logger("chat.onebot11")

# 单条消息最大字节数，防止异常大包
_MAX_MESSAGE_SIZE = 2 ** 23


def extract_text(message: Any) -> str:
    """从 OneBot 11 的 message 字段提取纯文本。

    message 可能是 str（CQ 码字符串）或 list（array 格式）。
    """
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        parts: List[str] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "text":
                parts.append((seg.get("data") or {}).get("text", ""))
            elif seg.get("type") == "at":
                qq = (seg.get("data") or {}).get("qq", "")
                parts.append(f"[@qq:{qq}]")
            elif seg.get("type") == "image":
                parts.append("[图片]")
            elif seg.get("type") in ("face", "record", "video", "file"):
                parts.append(f"[{seg.get('type')}]")
        return "".join(parts).strip()
    return ""


def extract_images(message: Any) -> List[Dict[str, Any]]:
    """从消息中提取图片段（每个含 url/file 等 data，用于识图）。"""
    imgs: List[Dict[str, Any]] = []
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "image":
                imgs.append(seg.get("data") or {})
    return imgs


def _new_echo() -> str:
    return str(uuid.uuid4())


class OneBot11Adapter(ChatAdapter):
    name = "onebot11"

    def __init__(self, config: Config, handler: MessageHandler, proactive=None, vision=None, tts=None):
        self._config = config
        self._handler = handler
        self._proactive = proactive
        self._vision = vision
        self._tts = tts
        # 语音回复配置
        tts_cfg = config.get("business.tts", {}) or {}
        self._tts_reply_mode = str(tts_cfg.get("reply_mode", "off"))
        self._tts_reply_ratio = float(tts_cfg.get("reply_ratio", 0.3))
        self._tts_reply_in_group = bool(tts_cfg.get("reply_in_group", False))
        # QQ 表情配置（回复时随机附带小黄脸）
        face_cfg = config.get("chat.face", {}) or {}
        self._face_enabled = bool(face_cfg.get("enabled", False))
        self._face_ratio = float(face_cfg.get("ratio", 0.3))
        self._face_ids = [int(x) for x in (face_cfg.get("ids") or [21, 14, 178, 76, 175, 96])]
        # 收藏表情（自定义斗图）配置：回复时主动随机发
        sticker_cfg = config.get("chat.sticker", {}) or {}
        self._sticker_enabled = bool(sticker_cfg.get("enabled", False))
        self._sticker_ratio = float(sticker_cfg.get("ratio", 0.2))
        self._sticker_cache_seconds = int(sticker_cfg.get("cache_seconds", 600))
        self._sticker_in_group = bool(sticker_cfg.get("in_group", True))
        self._sticker_cache: List[str] = []
        self._sticker_cache_time: float = 0.0

        self.mode = config.get("chat.onebot11.mode", "reverse")
        self.access_token = config.get("chat.onebot11.access_token", "")

        # forward 模式参数
        self.ws_url = config.get("chat.onebot11.ws_url", "ws://127.0.0.1:3001/")
        reconnect = config.get("chat.onebot11.reconnect", {}) or {}
        self.initial_delay = float(reconnect.get("initial_delay", 1))
        self.max_delay = float(reconnect.get("max_delay", 30))
        self.backoff_factor = float(reconnect.get("backoff_factor", 2))
        self.max_retries = int(reconnect.get("max_retries", -1))
        self.heartbeat_timeout = float(config.get("chat.onebot11.heartbeat_timeout", 40))

        # reverse 模式参数
        self.host = config.get("chat.onebot11.host", "127.0.0.1")
        self.port = int(config.get("chat.onebot11.port", 8080))
        self.path = config.get("chat.onebot11.path", "/onebot/v11/ws")

        # 监听对象过滤
        listen = config.get("chat.listen", {}) or {}
        self.listen_groups = {int(x) for x in (listen.get("groups") or [])}
        self.listen_private = {int(x) for x in (listen.get("private_users") or [])}
        self.self_id = int(listen.get("self_id") or 0)
        self.ignore_self = bool(listen.get("ignore_self", True))
        # 群聊触发关键词：非空时，群消息需命中关键词或 @ 机器人才响应（空=每句都回）
        self.group_keywords = [str(x) for x in (listen.get("group_keywords") or [])]
        # 群聊回复模式：keyword=关键词/@触发；ai=AI 联系上下文决策是否接话
        self.group_reply_mode = str(listen.get("group_reply_mode", "keyword"))
        # 群聊历史缓冲（供 AI 决策的上下文）：group_id -> 最近消息列表
        self._group_history: Dict[int, List[str]] = {}
        self._history_max = int(listen.get("group_history_max", 10))
        # 群最后活跃时间（供"冷场检测"主动活跃用）：group_id -> 时间戳
        self._group_last_active: Dict[int, float] = {}

        self._running = False
        self._send_queue: "asyncio.Queue[str]" = asyncio.Queue()
        self._pending: Dict[str, "asyncio.Future"] = {}
        self._tasks: List[asyncio.Task] = []
        self._ws: Optional[Any] = None
        self._server: Optional[Any] = None
        self._last_event_time = 0.0

    def set_proactive(self, proactive) -> None:
        """注入主动消息调度器（用于主人指令拦截）。"""
        self._proactive = proactive

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        self._running = True
        log.info("OneBot11 适配器启动（mode=%s, handler=%s）", self.mode, self._handler.name)
        try:
            self._tasks.append(asyncio.create_task(self._sender_loop(), name="onebot11-sender"))
            if self.mode == "forward":
                await self._run_forward()
            else:
                await self._run_reverse()
        except asyncio.CancelledError:
            raise
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        log.info("OneBot11 适配器已停止")

    # ------------------------------------------------------------------ #
    # 消息发送
    # ------------------------------------------------------------------ #
    async def send_group(self, group_id: int, message: str) -> bool:
        return await self._send_action("send_msg", {
            "message_type": "group",
            "group_id": int(group_id),
            "message": message,
        })

    async def send_private(self, user_id: int, message: str) -> bool:
        return await self._send_action("send_msg", {
            "message_type": "private",
            "user_id": int(user_id),
            "message": message,
        })

    async def send_image(self, message_type: str, target: int, image_path: str, text: str = "") -> bool:
        """发送一张本地图片（base64），可附带文字。"""
        import base64
        try:
            with open(image_path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
        except Exception as exc:
            log.error("读取图片失败 %s：%s", image_path, exc)
            return False
        message: List[Dict[str, Any]] = [{"type": "image", "data": {"file": f"base64://{data}"}}]
        if text:
            message.append({"type": "text", "data": {"text": text}})
        params: Dict[str, Any] = {
            "message_type": message_type,
            "message": message,
        }
        if message_type == "group":
            params["group_id"] = int(target)
        else:
            params["user_id"] = int(target)
        return await self._send_action("send_msg", params)

    async def send_voice(self, message_type: str, target: int, audio_path: str) -> bool:
        """发送一段本地语音（base64）。"""
        import base64
        try:
            with open(audio_path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
        except Exception as exc:
            log.error("读取语音失败 %s：%s", audio_path, exc)
            return False
        message: List[Dict[str, Any]] = [{"type": "record", "data": {"file": f"base64://{data}"}}]
        params: Dict[str, Any] = {"message_type": message_type, "message": message}
        if message_type == "group":
            params["group_id"] = int(target)
        else:
            params["user_id"] = int(target)
        return await self._send_action("send_msg", params)

    async def send_face(self, message_type: str, target: int, face_id: int) -> bool:
        """发送一个 QQ 内置表情（小黄脸）。"""
        message: List[Dict[str, Any]] = [{"type": "face", "data": {"id": str(face_id)}}]
        params: Dict[str, Any] = {"message_type": message_type, "message": message}
        if message_type == "group":
            params["group_id"] = int(target)
        else:
            params["user_id"] = int(target)
        return await self._send_action("send_msg", params)

    async def send_text_face(self, message_type: str, target: int, text: str, face_id: int) -> bool:
        """发送一条「文字 + QQ 表情」合并消息。"""
        message: List[Dict[str, Any]] = [
            {"type": "text", "data": {"text": text}},
            {"type": "face", "data": {"id": str(face_id)}},
        ]
        params: Dict[str, Any] = {"message_type": message_type, "message": message}
        if message_type == "group":
            params["group_id"] = int(target)
        else:
            params["user_id"] = int(target)
        return await self._send_action("send_msg", params)

    async def send_image_ref(self, message_type: str, target: int, file_ref: str) -> bool:
        """用 URL 或 file 引用发送一张图片（用于收藏表情等）。"""
        message: List[Dict[str, Any]] = [{"type": "image", "data": {"file": file_ref}}]
        params: Dict[str, Any] = {"message_type": message_type, "message": message}
        if message_type == "group":
            params["group_id"] = int(target)
        else:
            params["user_id"] = int(target)
        return await self._send_action("send_msg", params)

    @staticmethod
    def _convert_face(text: str) -> str:
        """把 [face:ID] 标记转换成 CQ 码 [CQ:face,id=ID]，NapCat 会渲染成表情。"""
        return re.sub(r"\[face:(\d+)\]", r"[CQ:face,id=\1]", text)

    @staticmethod
    def _build_segments(text: str) -> List[Dict[str, Any]]:
        """把 [face:ID] 标记解析成 text + face 段交错的消息数组。"""
        segments: List[Dict[str, Any]] = []
        last = 0
        for m in re.finditer(r"\[face:(\d+)\]", text):
            if m.start() > last:
                segments.append({"type": "text", "data": {"text": text[last:m.start()]}})
            segments.append({"type": "face", "data": {"id": m.group(1)}})
            last = m.end()
        if last < len(text):
            segments.append({"type": "text", "data": {"text": text[last:]}})
        return segments or [{"type": "text", "data": {"text": text}}]

    async def _send_segments(self, message_type: str, target: int, segments: List[Dict[str, Any]]) -> bool:
        params: Dict[str, Any] = {"message_type": message_type, "message": segments}
        if message_type == "group":
            params["group_id"] = int(target)
        else:
            params["user_id"] = int(target)
        return await self._send_action("send_msg", params)

    async def _reply_with_faces(self, message_type: str, user_id: int, group_id: Optional[int], text: str) -> bool:
        """把含表情的回复按句分段，逐条发送（表情混在话中）。"""
        ok = True
        target = user_id if message_type == "private" else group_id
        parts = split_long_reply(text)
        for i, part in enumerate(parts):
            segments = self._build_segments(part)
            r = await self._send_segments(message_type, target, segments)
            ok = ok and r
            if i < len(parts) - 1:
                await asyncio.sleep(0.5)
        return ok

    async def _send_action(self, action: str, params: Dict[str, Any]) -> bool:
        payload = {"action": action, "params": params, "echo": _new_echo()}
        await self._send_queue.put(json.dumps(payload, ensure_ascii=False))
        return True

    async def _call_api(self, action: str, params: Dict[str, Any], timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        """发送 API 请求并等待对应 echo 的响应，返回响应 data（请求-响应机制）。"""
        loop = asyncio.get_event_loop()
        echo = _new_echo()
        fut: "asyncio.Future" = loop.create_future()
        self._pending[echo] = fut
        payload = {"action": action, "params": params, "echo": echo}
        await self._send_queue.put(json.dumps(payload, ensure_ascii=False))
        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            log.warning("API %s 请求超时", action)
            return None
        finally:
            self._pending.pop(echo, None)
        return resp.get("data") if isinstance(resp, dict) else None

    async def get_group_member_list(self, group_id: int) -> List[Dict[str, Any]]:
        """获取群成员列表（含 QQ 号与昵称，用于随机选人）。失败返回空列表。"""
        data = await self._call_api("get_group_member_list", {"group_id": int(group_id)})
        if not isinstance(data, list):
            log.warning("获取群 %s 成员列表失败", group_id)
            return []
        members: List[Dict[str, Any]] = []
        for m in data:
            if isinstance(m, dict) and m.get("user_id"):
                members.append({
                    "user_id": int(m["user_id"]),
                    "nickname": m.get("card") or m.get("nickname") or str(m["user_id"]),
                })
        return members

    async def get_custom_faces(self) -> List[str]:
        """获取 QQ 收藏表情的图片 file/url 列表（fetch_custom_face）。失败返回空列表。"""
        data = await self._call_api("fetch_custom_face", {"count": 50}, timeout=30.0)
        result: List[str] = []
        if isinstance(data, list):
            for x in data:
                if isinstance(x, str) and x:
                    result.append(x)
                elif isinstance(x, dict):
                    for k in ("url", "file", "path", "src", "md5"):
                        if x.get(k):
                            result.append(str(x[k]))
                            break
        elif isinstance(data, dict):
            for k in ("data", "faces", "list", "result", "urls"):
                if isinstance(data.get(k), list):
                    for x in data[k]:
                        if isinstance(x, str) and x:
                            result.append(x)
                        elif isinstance(x, dict):
                            for kk in ("url", "file", "path", "src", "md5"):
                                if x.get(kk):
                                    result.append(str(x[kk]))
                                    break
        if not result:
            log.warning("fetch_custom_face 未返回表情（原始：%s）", str(data)[:400])
        else:
            log.info("获取到收藏表情 %d 个，示例：%s", len(result), str(result[0])[:120])
        return result

    async def get_custom_faces_cached(self) -> List[str]:
        """带缓存地获取收藏表情列表（避免每次回复都调接口）。缓存过期才重新拉取。"""
        now = time.time()
        if self._sticker_cache and (now - self._sticker_cache_time) < self._sticker_cache_seconds:
            return self._sticker_cache
        faces = await self.get_custom_faces()
        self._sticker_cache = faces
        self._sticker_cache_time = now
        return faces

    async def _warmup_stickers(self) -> None:
        """连入后预热：主动拉取一次收藏表情，验证接口可用并填充缓存。"""
        try:
            faces = await self.get_custom_faces()
            if faces:
                self._sticker_cache = faces
                self._sticker_cache_time = time.time()
                log.info("收藏表情预热成功：%d 个", len(faces))
        except Exception:
            log.exception("收藏表情预热失败")

    async def maybe_send_sticker(self, message_type: str, target: int) -> bool:
        """按概率主动发一张收藏的自定义表情图片（斗图）。返回是否真的发了。"""
        if not self._sticker_enabled:
            return False
        if message_type == "group" and not self._sticker_in_group:
            return False
        if random.random() >= self._sticker_ratio:
            return False
        faces = await self.get_custom_faces_cached()
        if not faces:
            return False
        face = random.choice(faces)
        ok = await self.send_image_ref(message_type, target, face)
        if ok:
            log.info("主动发收藏表情 -> %s(%s)：%s", message_type, target, str(face)[:80])
        return ok

    async def _sender_loop(self) -> None:
        """单一写者：串行地把待发消息写入当前 WebSocket 连接。"""
        while self._running:
            text = await self._send_queue.get()
            ws = self._ws
            if ws is None:
                log.warning("当前无可用连接，丢弃待发消息：%s", text[:200])
                continue
            try:
                await ws.send(text)
            except Exception as exc:
                log.error("发送消息失败：%s（msg=%s）", exc, text[:200])

    # ------------------------------------------------------------------ #
    # forward 模式：作为客户端主动连接，断线自动重连
    # ------------------------------------------------------------------ #
    async def _run_forward(self) -> None:
        attempt = 0
        while self._running:
            try:
                await self._connect_and_receive()
                attempt = 0  # 正常断开后重置退避计数
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("连接异常：%s", exc)
            if not self._running:
                break
            if self.max_retries >= 0 and attempt >= self.max_retries:
                log.error("重连次数达到上限（%d），停止重试", self.max_retries)
                break
            delay = self._backoff(attempt)
            attempt += 1
            log.info("%.1f 秒后进行第 %d 次重连", delay, attempt)
            await asyncio.sleep(delay)

    async def _connect_and_receive(self) -> None:
        headers: Dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        log.info("正在连接协议端正向 WS：%s", self.ws_url)
        async with websockets.connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            max_size=_MAX_MESSAGE_SIZE,
        ) as ws:
            self._ws = ws
            self._last_event_time = time.monotonic()
            log.info("已连接协议端：%s", self.ws_url)
            async for raw in ws:
                self._last_event_time = time.monotonic()
                await self._dispatch_raw(raw)
        self._ws = None
        log.info("与协议端的连接已断开：%s", self.ws_url)

    def _backoff(self, attempt: int) -> float:
        """指数退避 + 抖动。"""
        exp = min(self.initial_delay * (self.backoff_factor ** attempt), self.max_delay)
        return exp + random.uniform(0, 0.5 * exp)

    # ------------------------------------------------------------------ #
    # reverse 模式：作为服务端接收协议端连入
    # ------------------------------------------------------------------ #
    async def _run_reverse(self) -> None:
        log.info("启动反向 WS 服务：ws://%s:%d%s", self.host, self.port, self.path)
        self._server = await websockets.serve(
            self._on_reverse_connection,
            self.host,
            self.port,
            max_size=_MAX_MESSAGE_SIZE,
        )
        log.info("反向 WS 服务已就绪，等待协议端连入……")
        while self._running:
            await asyncio.sleep(1)

    async def _on_reverse_connection(self, ws) -> None:
        peer = getattr(ws, "remote_address", None)
        # 鉴权（可选）：协议端反向 WS 会在请求头带 Authorization: Bearer <token>
        if self.access_token:
            request = getattr(ws, "request", None)
            headers = getattr(request, "headers", {}) or {}
            auth = headers.get("Authorization", "")
            if auth != f"Bearer {self.access_token}":
                log.warning("拒绝来自 %s 的连接：token 不匹配", peer)
                await ws.close(code=4003, reason="Unauthorized")
                return
        log.info("协议端已连入：%s", peer)
        self._ws = ws
        self._last_event_time = time.monotonic()
        # 预热：若启用了收藏表情，连入后主动拉取一次（顺带验证接口可用性）
        if self._sticker_enabled:
            asyncio.create_task(self._warmup_stickers())
        try:
            async for raw in ws:
                self._last_event_time = time.monotonic()
                await self._dispatch_raw(raw)
        except ConnectionClosed:
            log.info("协议端连接断开：%s", peer)
        except Exception as exc:
            log.error("协议端连接异常（%s）：%s", peer, exc)
        finally:
            if self._ws is ws:
                self._ws = None
            log.info("连接已关闭：%s", peer)

    # ------------------------------------------------------------------ #
    # 事件分发
    # ------------------------------------------------------------------ #
    async def _dispatch_raw(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("收到无法解析的上游数据：%s", str(raw)[:200])
            return

        post_type = data.get("post_type")
        if post_type == "message":
            try:
                await self._handle_message(data)
            except Exception:
                log.exception("处理 message 事件出错")
        elif post_type == "meta_event":
            self._handle_meta(data)
        elif post_type == "notice":
            log.debug("notice 事件：%s", data.get("notice_type"))
        elif post_type == "request":
            log.debug("request 事件：%s", data.get("request_type"))
        elif "echo" in data or "retcode" in data:
            # API 响应：若匹配到挂起的请求，则回填结果
            echo = data.get("echo")
            if echo is not None:
                fut = self._pending.pop(echo, None)
                if fut is not None and not fut.done():
                    fut.set_result(data)
            log.debug("收到 API 响应：%s", str(raw)[:300])
        else:
            log.debug("未知上游数据：%s", str(raw)[:300])

    def _handle_meta(self, data: Dict[str, Any]) -> None:
        meta_type = data.get("meta_event_type")
        if meta_type == "lifecycle":
            log.info("协议端生命周期事件：self_id=%s sub_type=%s",
                     data.get("self_id"), data.get("sub_type"))
        elif meta_type == "heartbeat":
            log.debug("收到心跳")
        else:
            log.debug("meta 事件：%s", meta_type)

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        message_type = data.get("message_type", "")
        user_id = int(data.get("user_id") or 0)
        group_id = data.get("group_id")
        group_id = int(group_id) if group_id is not None else None
        self_qq = int(data.get("self_id") or self.self_id or 0)

        # 过滤：忽略自己
        if self.ignore_self and self_qq and user_id == self_qq:
            return

        text = extract_text(data.get("message"))
        if not text:
            return

        # 识图：图片消息时，调用视觉模型描述图片内容
        image_segs = extract_images(data.get("message"))
        if image_segs and self._vision is not None:
            url = image_segs[0].get("url", "")
            if not url:
                # url 缺失时，用 file 调 get_image 获取图片地址
                file = image_segs[0].get("file", "")
                if file:
                    got = await self._call_api("get_image", {"file": file})
                    url = got.get("url", "") if isinstance(got, dict) else ""
                log.info("图片 data：%s", image_segs[0])
            if url:
                desc = await self._vision.describe(url)
                if desc:
                    text = f"{text}\n[图片内容：{desc}]"
                    log.info("识图完成：%s", desc[:60])
                else:
                    log.warning("识图未返回描述")
            else:
                log.warning("图片无可用 url/file，跳过识图")

        if message_type == "group":
            if self.listen_groups and (group_id or 0) not in self.listen_groups:
                return

            sender_nick = (data.get("sender") or {}).get("nickname") or \
                          (data.get("sender") or {}).get("card") or str(user_id)
            mentioned = bool(self_qq) and (f"@{self_qq}" in text or f"[@qq:{self_qq}]" in text)
            hit_keyword = any(kw in text for kw in self.group_keywords)

            # 记录群聊历史（供 AI 决策的上下文）与最后活跃时间
            self._append_history(group_id, f"{sender_nick}: {text}")
            self._group_last_active[group_id] = time.time()
            log.info("收到group消息 来自 %s(%s) group=%s：%s", sender_nick, user_id, group_id, text)

            # 群聊发表情意图：/表情 或自然语言「发个表情」，直接发收藏表情到群里
            if self._proactive is not None:
                try:
                    if await self._proactive.handle_group_sticker(text, group_id):
                        return
                except Exception:
                    log.exception("处理群聊发表情指令出错")

            if self.group_reply_mode == "ai":
                # AI 决策模式：@或关键词直接回；否则交给 AI 联系上下文判断是否接话
                if not mentioned and not hit_keyword:
                    decision = await self._ai_decide(group_id, text)
                    if decision is None:
                        log.info("AI 决策：不接话 group=%s（%s）", group_id, text[:60])
                        return
                    # AI 决定接话，直接回复（把 [face:ID] 转 CQ 码渲染成表情）
                    decision = self._convert_face(decision)
                    ok = await self.send_group(group_id, decision)
                    self._append_history(group_id, f"肥鱼: {decision}")
                    log.info("AI主动接话 -> group=%s %s：%s",
                             group_id, "成功" if ok else "失败", decision[:200])
                    return
            else:
                # keyword 模式：仅关键词/@ 触发（关键词为空则每句都回）
                if self.group_keywords and not mentioned and not hit_keyword:
                    log.debug("群消息未命中关键词，忽略：%s", text[:80])
                    return
        elif message_type == "private":
            if self.listen_private and user_id not in self.listen_private:
                return
            # 主人指令拦截（如 /send ...），命中则不再走正常消息处理
            if self._proactive is not None:
                try:
                    if await self._proactive.handle_command(text, user_id):
                        return
                except Exception:
                    log.exception("处理主人指令出错")
        else:
            log.debug("忽略不支持的消息类型：%s", message_type)
            return

        sender = data.get("sender") or {}
        nickname = sender.get("nickname") or sender.get("card") or str(user_id)
        where = f" group={group_id}" if group_id else " private"
        log.info("收到%s消息 来自 %s(%s)%s：%s", message_type, nickname, user_id, where, text)

        ctx = ChatContext(
            message_type=message_type,
            user_id=user_id,
            group_id=group_id,
            message=text,
            raw=data,
            adapter=self,
            sender_nickname=nickname,
        )
        reply = await self._handler.handle(ctx)
        if reply:
            if self._should_voice_reply(message_type) and not image_segs:
                # 语音回复（有图片时不用语音，改用文字描述）
                voice_text = re.sub(r"\[face:\d+\]", "", reply)
                ok = await self._voice_reply(message_type, user_id, group_id, voice_text)
            else:
                # 文字回复：把 [face:ID] 转成 CQ 码，渲染成表情
                reply = self._convert_face(reply)
                ok = await ctx.reply(reply)
                # 兜底：随机附带一个 QQ 小黄脸表情
                if ok and self._face_enabled and random.random() < self._face_ratio:
                    target = user_id if message_type == "private" else group_id
                    if target:
                        await self.send_face(message_type, target, random.choice(self._face_ids))
                # 主动随机发一张收藏的自定义表情图片（斗图）
                if ok:
                    target = user_id if message_type == "private" else group_id
                    if target:
                        await self.maybe_send_sticker(message_type, target)
            log.info("回复%s消息 -> %s(%s) %s：%s",
                     message_type, nickname, user_id,
                     "成功" if ok else "失败", reply[:200])

    # ------------------------------------------------------------------ #
    # 语音回复
    # ------------------------------------------------------------------ #
    def _should_voice_reply(self, message_type: str) -> bool:
        if not self._tts or not getattr(self._tts, "enabled", False):
            return False
        if message_type == "group" and not self._tts_reply_in_group:
            return False
        if self._tts_reply_mode == "always":
            return True
        if self._tts_reply_mode == "random":
            return random.random() < self._tts_reply_ratio
        return False

    async def _voice_reply(self, message_type: str, user_id: int, group_id: Optional[int], text: str) -> bool:
        """语音回复：TTS 合成后发语音，失败降级为文字。"""
        audio = await self._tts.synthesize(text)
        if audio:
            if message_type == "group" and group_id:
                ok = await self.send_voice("group", group_id, audio)
            else:
                ok = await self.send_voice("private", user_id, audio)
            if ok:
                return True
        # 降级为文字
        if message_type == "group" and group_id:
            return await self.send_group(group_id, text)
        return await self.send_private(user_id, text)

    # ------------------------------------------------------------------ #
    # 群聊历史与 AI 决策
    # ------------------------------------------------------------------ #
    def _append_history(self, group_id: Optional[int], text: str) -> None:
        if group_id is None:
            return
        history = self._group_history.setdefault(group_id, [])
        history.append(text)
        if len(history) > self._history_max:
            history.pop(0)

    def _get_history(self, group_id: Optional[int]) -> str:
        if group_id is None:
            return ""
        return "\n".join(self._group_history.get(group_id, []))

    def get_group_idle_seconds(self, group_id: int) -> float:
        """返回该群距离最后一条消息的秒数（从未有消息则返回超大值）。"""
        last = self._group_last_active.get(group_id)
        if last is None:
            return float("inf")
        return time.time() - last

    async def _ai_decide(self, group_id: Optional[int], text: str) -> Optional[str]:
        """调用业务处理器的 decide_reply 做 AI 决策。返回 None=沉默，否则=回复内容。"""
        decide = getattr(self._handler, "decide_reply", None)
        if not callable(decide):
            log.warning("当前处理器不支持 AI 决策（缺少 decide_reply），请使用 deepseek handler")
            return None
        try:
            return await decide(self._get_history(group_id), text)
        except Exception:
            log.exception("AI 决策出错")
            return None
