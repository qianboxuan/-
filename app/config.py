"""配置加载模块。

配置来源与优先级（从高到低）：
1. 系统环境变量（QQBOT_* / DEEPSEEK_* 等）
2. 项目根目录 .env 文件
3. config.yaml 文件（默认路径，可用环境变量 QQBOT_CONFIG 指定）
4. 内置默认值

敏感/可变信息（账号、AppID、Token、监听对象等）均通过以上方式管理，
不写死在代码中。
"""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

try:
    from dotenv import load_dotenv

    _HAS_DOTENV = True
except ImportError:  # dotenv 为可选依赖
    _HAS_DOTENV = False

    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 环境变量 -> 配置项路径（dotted path）的映射
_ENV_OVERRIDES: Dict[str, Optional[str]] = {
    "QQBOT_CONFIG": None,  # 特殊：配置文件路径
    "QQBOT_ADAPTER": "chat.adapter",
    "QQBOT_MODE": "chat.onebot11.mode",
    "QQBOT_WS_URL": "chat.onebot11.ws_url",
    "QQBOT_ACCESS_TOKEN": "chat.onebot11.access_token",
    "QQBOT_HOST": "chat.onebot11.host",
    "QQBOT_PORT": "chat.onebot11.port",
    "QQBOT_PATH": "chat.onebot11.path",
    "QQBOT_HANDLER": "business.handler",
    "DEEPSEEK_API_KEY": "business.deepseek.api_key",
    "DEEPSEEK_BASE_URL": "business.deepseek.base_url",
    "DEEPSEEK_MODEL": "business.deepseek.model",
    "QQBOT_LOG_LEVEL": "logging.level",
    "QQBOT_LOG_FILE": "logging.file",
}

# 内置默认配置（保证缺省时也可运行）
_DEFAULT_CONFIG: Dict[str, Any] = {
    "chat": {
        "adapter": "onebot11",
        "onebot11": {
            "mode": "reverse",  # reverse=本项目作 WS 服务端；forward=本项目作 WS 客户端
            "ws_url": "ws://127.0.0.1:3001/",
            "access_token": "",
            "host": "127.0.0.1",
            "port": 8080,
            "path": "/onebot/v11/ws",
            "reconnect": {
                "initial_delay": 1,
                "max_delay": 30,
                "backoff_factor": 2,
                "max_retries": -1,  # -1 表示无限重连
            },
            "heartbeat_timeout": 40,
        },
        "listen": {
            "groups": [],          # 仅监听这些群号，空=全部
            "private_users": [],   # 仅监听这些私聊用户，空=全部
            "self_id": 0,          # 0=忽略
            "ignore_self": True,   # 忽略机器人自己发出的消息
            "group_keywords": [],  # 群聊触发关键词，命中任一或@机器人才响应；空=每句都回
            "group_reply_mode": "keyword",  # keyword=关键词/@触发；ai=AI联系上下文决策是否接话
            "group_history_max": 10,        # AI 决策时保留的群聊历史条数
        },
        "owner": 0,                # 主人 QQ 号（/send 指令权限校验），0=不校验
        "proactive": {
            "enabled": False,      # 是否启用主动消息调度
            "tasks": [],           # 定时任务列表
            "emoji_dir": "assets/emojis",  # 表情包目录（随机发表情包用）
            "random": {            # 随机主动消息（不定时冒泡）
                "enabled": False,
                "interval_min": 7200,   # 最小间隔（秒）
                "interval_max": 43200,  # 最大间隔（秒）
                "type": "private",      # private=私聊 / group=群聊
                "target": 0,            # 目标 QQ 号或群号
                "prompt": "",           # 内容提示词（AI 自由发挥）
                "pick_from_group": 0,   # 在该群活跃（0=用固定 target）
                "mention_ratio": 0.3,   # @ 人的概率
                "emoji_ratio": 0.2,     # 发表情包的概率（其余发文字内容）
            },
        },
    },
    "business": {
        "handler": "echo",  # echo | deepseek
        "deepseek": {
            "api_key": "",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "system_prompt": "你是一个乐于助人的中文助手。",
            "temperature": 0.7,
            "max_tokens": 1024,
            "timeout": 60,
            "history_max": 20,   # 会话记忆：每个会话保留的最近消息条数（每轮=user+assistant 两条）
        },
        "vision": {               # 识图用的视觉模型
            "enabled": False,
            "provider": "kimi",   # kimi / glm / qwen
            "api_key": "",        # 空=从环境变量读（KIMI_API_KEY / GLM_API_KEY / DASHSCOPE_API_KEY）
            "base_url": "",       # 空=按 provider 自动填
            "model": "",          # 空=按 provider 自动填
            "timeout": 60,
        },
    },
    "logging": {
        "level": "INFO",
        "file": "logs/qqbot.log",
    },
}


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """递归合并 override 到 base，返回 base。"""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _set_path(data: Dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _coerce(value: str) -> Any:
    """把环境变量字符串尽量还原成 bool/int/float。"""
    lowered = value.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


class Config:
    """对嵌套 dict 的只读包装，支持 dotted-path 取值。"""

    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def as_dict(self) -> Dict[str, Any]:
        return self._data


def load_config(config_path: Optional[str] = None) -> Config:
    """加载配置：.env -> config.yaml -> 环境变量覆盖。"""
    load_dotenv(PROJECT_ROOT / ".env")

    data = copy.deepcopy(_DEFAULT_CONFIG)

    path = Path(config_path or os.environ.get("QQBOT_CONFIG") or (PROJECT_ROOT / "config.yaml"))
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                file_data = yaml.safe_load(f) or {}
            _deep_merge(data, file_data)
        except Exception as exc:  # 配置损坏时给出明确告警并继续用默认值
            logging.getLogger("config").error("加载配置文件 %s 失败：%s", path, exc)
    else:
        logging.getLogger("config").warning("未找到配置文件 %s，使用内置默认配置", path)

    for env_key, dotted in _ENV_OVERRIDES.items():
        if dotted is None:
            continue
        if env_key in os.environ:
            _set_path(data, dotted, _coerce(os.environ[env_key]))

    return Config(data)
