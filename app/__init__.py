"""DeepSeek 接入 QQ 项目。

结构说明：
- app/chat      ：聊天接入模块（可插拔 adapter），只负责收发消息与连接管理
- app/business  ：业务逻辑模块，通过 MessageHandler 接口与聊天接入解耦
- app/config.py ：配置加载（YAML + 环境变量覆盖）
- app/logging_setup.py ：统一日志
"""

__version__ = "0.1.0"
