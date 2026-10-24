"""本地冒烟测试：不依赖真实 QQ / 协议端，直接验证「收消息→处理→回复」链路。

原理：启动 OneBot11 适配器（reverse 模式），再用本脚本模拟一个 OneBot 协议端
连入，发送一条 message 事件，并断言收到了对应的 send_msg 回复。

运行：
    python scripts/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import websockets

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.business.echo import EchoHandler  # noqa: E402
from app.chat.onebot11 import OneBot11Adapter  # noqa: E402
from app.config import Config  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(port: int) -> Config:
    return Config({
        "chat": {
            "adapter": "onebot11",
            "onebot11": {
                "mode": "reverse",
                "host": "127.0.0.1",
                "port": port,
                "path": "/onebot/v11/ws",
                "access_token": "",
            },
            "listen": {"groups": [], "private_users": [], "self_id": 0, "ignore_self": True},
        },
        "business": {"handler": "echo"},
        "logging": {"level": "WARNING", "file": ""},
    })


async def _wait_for_server(port: int, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    url = f"ws://127.0.0.1:{port}/onebot/v11/ws"
    while True:
        try:
            async with websockets.connect(url):
                return
        except Exception:
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError("服务端未能在限时内就绪")
            await asyncio.sleep(0.2)


async def _send_and_expect(ws, event: dict, expected: dict) -> None:
    await ws.send(json.dumps(event, ensure_ascii=False))
    raw = await asyncio.wait_for(ws.recv(), timeout=5)
    data = json.loads(raw)
    assert data.get("action") == "send_msg", f"期望 send_msg，实际收到 {raw}"
    params = data.get("params", {})
    for key, value in expected.items():
        assert params.get(key) == value, f"{key} 不匹配：期望 {value!r}，实际 {params.get(key)!r}"
    print(f"  [OK] 收到回复 -> {params.get('message')!r}")


async def main() -> int:
    port = _free_port()
    config = _make_config(port)
    adapter = OneBot11Adapter(config, EchoHandler())

    task = asyncio.create_task(adapter.run())
    try:
        await _wait_for_server(port)
        url = f"ws://127.0.0.1:{port}/onebot/v11/ws"
        async with websockets.connect(url) as ws:
            print("== 用例1：私聊消息 ==")
            await _send_and_expect(ws, {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "self_id": 20001,
                "message": "你好",
                "sender": {"nickname": "测试用户"},
            }, {
                "message_type": "private",
                "user_id": 10001,
                "message": "[echo] 收到你的消息：你好",
            })

            print("== 用例2：群聊消息 ==")
            await _send_and_expect(ws, {
                "post_type": "message",
                "message_type": "group",
                "user_id": 10002,
                "group_id": 30001,
                "self_id": 20001,
                "message": "hello",
                "sender": {"card": "群友"},
            }, {
                "message_type": "group",
                "group_id": 30001,
                "message": "[echo] 收到你的消息：hello",
            })

            print("== 用例3：忽略机器人自己发的消息（应无回复）==")
            await ws.send(json.dumps({
                "post_type": "message",
                "message_type": "private",
                "user_id": 20001,
                "self_id": 20001,
                "message": "不应被回复",
                "sender": {"nickname": "bot"},
            }, ensure_ascii=False))
            try:
                await asyncio.wait_for(ws.recv(), timeout=1.0)
                print("  [FAIL] 不应收到回复")
                return 1
            except asyncio.TimeoutError:
                print("  [OK] 正确忽略了自己的消息")

        print("\n冒烟测试全部通过：收消息 → 处理 → 回复 链路正常")
        return 0
    finally:
        await adapter.stop()
        task.cancel()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
