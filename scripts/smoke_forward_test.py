"""forward 模式冒烟测试：验证本项目作为 WS 客户端连接协议端，以及断线后自动重连。

本脚本起一个本地「伪协议端」WS 服务，让适配器以 forward 模式连入，
验证：① 收到消息能回复；② 服务端断开后适配器能自动重连并继续工作。

运行：
    python scripts/smoke_forward_test.py
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


async def main() -> int:
    port = _free_port()
    # 用一个可变的 server 引用，模拟「断开→重连」
    state = {"server": None, "connects": 0}

    async def on_conn(ws):
        state["connects"] += 1
        print(f"  [协议端] 第 {state['connects']} 次连入")
        try:
            # 收到来自适配器的 send_msg 后，原样返回给测试方
            async for raw in ws:
                data = json.loads(raw)
                if data.get("action") == "send_msg":
                    state.setdefault("reply_queue", asyncio.Queue()).put_nowait(data)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def start_server():
        state["server"] = await websockets.serve(on_conn, "127.0.0.1", port)

    await start_server()
    state["reply_queue"] = asyncio.Queue()

    config = Config({
        "chat": {
            "adapter": "onebot11",
            "onebot11": {
                "mode": "forward",
                "ws_url": f"ws://127.0.0.1:{port}/",
                "access_token": "",
                "reconnect": {"initial_delay": 0.2, "max_delay": 1, "backoff_factor": 2, "max_retries": -1},
            },
            "listen": {"groups": [], "private_users": [], "self_id": 0, "ignore_self": True},
        },
        "business": {"handler": "echo"},
        "logging": {"level": "WARNING", "file": ""},
    })
    adapter = OneBot11Adapter(config, EchoHandler())
    task = asyncio.create_task(adapter.run())

    async def send_event(msg: str) -> None:
        # 通过服务端侧连接把事件「推」给适配器（模拟协议端上报）
        server = state["server"]
        for client in server.connections:
            await client.send(json.dumps({
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "self_id": 20001,
                "message": msg,
                "sender": {"nickname": "u"},
            }, ensure_ascii=False))
            return
        raise RuntimeError("无活跃连接")

    try:
        # 等第一次连上
        for _ in range(50):
            if state["connects"] >= 1:
                break
            await asyncio.sleep(0.1)

        print("== 用例1：forward 连接后收发 ==")
        await send_event("你好")
        reply = await asyncio.wait_for(state["reply_queue"].get(), timeout=5)
        assert reply["params"]["message"] == "[echo] 收到你的消息：你好"
        print(f"  [OK] 收到回复 -> {reply['params']['message']!r}")

        print("== 用例2：协议端断开，适配器自动重连 ==")
        old_connects = state["connects"]
        # 关闭当前所有连接，模拟协议端掉线
        for client in list(state["server"].connections):
            await client.close()
        # 等待自动重连
        for _ in range(50):
            if state["connects"] > old_connects:
                break
            await asyncio.sleep(0.1)
        assert state["connects"] > old_connects, "未观察到自动重连"
        print(f"  [OK] 已自动重连（第 {state['connects']} 次连入）")

        print("== 用例3：重连后可继续收发 ==")
        await send_event("again")
        reply = await asyncio.wait_for(state["reply_queue"].get(), timeout=5)
        assert reply["params"]["message"] == "[echo] 收到你的消息：again"
        print(f"  [OK] 收到回复 -> {reply['params']['message']!r}")

        print("\nforward 模式冒烟测试通过：连接 → 断线重连 → 继续收发 正常")
        return 0
    finally:
        await adapter.stop()
        task.cancel()
        state["server"].close()
        await state["server"].wait_closed()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
