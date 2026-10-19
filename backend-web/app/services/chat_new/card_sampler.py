"""
订单卡片原始载荷采样器——backend-web 侧入口

实现已上移到 common/utils/card_sampler.py（backend-web 与 websocket 服务
共用：两套 IM 长连接收到的推送集合不同，只挂一边会漏采样）。本文件仅作
转发，保留 `from app.services.chat_new.card_sampler import sample_card`
的既有导入方式不变。
"""
from common.utils.card_sampler import (  # noqa: F401
    SAMPLE_FILE,
    looks_like_trade_card,
    sample_card,
)

__all__ = ["SAMPLE_FILE", "looks_like_trade_card", "sample_card"]
