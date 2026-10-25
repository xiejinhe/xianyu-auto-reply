"""聊天消息本地存档模型

功能：
1. 定义 xy_chat_messages 表：网页在线聊天拉取/推送到的消息全文落库
2. 支撑「聊天记录搜索」——搜索只查本地库，零闲鱼接口请求
   （2026-10-26 限流教训：任何搜索都不能打到闲鱼侧）

数据来源（两处落库，见 backend-web chat_new）：
- GET /messages 历史拉取（翻页拉过的都会存）
- WS 推送新消息（在线期间实时存）

覆盖说明：只有「拉过/推送过」的消息才在库；历史更早的消息
（上线前聊的）在首次翻页拉到时才会补进来。
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, Text

from sqlalchemy.orm import Mapped, mapped_column

from common.db.base_class import Base, TimestampMixin


class XYChatMessage(TimestampMixin, Base):
    """聊天消息存档表"""

    __tablename__ = "xy_chat_messages"
    __table_args__ = (
        Index("idx_chatmsg_owner_acct_msg", "owner_id", "account_id", "message_id", unique=True),
        Index("idx_chatmsg_owner_acct_time", "owner_id", "account_id", "msg_time"),
        Index("idx_chatmsg_owner_acct_cid_time", "owner_id", "account_id", "cid", "msg_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, comment="所属用户ID")
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="账号ID（卖家视角）")
    cid: Mapped[str] = mapped_column(String(64), nullable=False, comment="会话ID")
    message_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="消息ID（闲鱼 messageId）")
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="发送者用户ID")
    sender_name: Mapped[str] = mapped_column(String(255), nullable=False, default="", comment="发送者昵称")
    is_self: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否当前视角账号发送")
    managed_sender: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否本系统托管的其它账号发送")
    msg_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text", comment="消息类型 text/image/system/card/audio")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="消息文本（图片/卡片为占位或标题）")
    media_json: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="图片/语音等媒体信息 JSON")
    msg_time: Mapped[int] = mapped_column(DateTime, nullable=False, comment="消息时间（闲鱼时间戳转 datetime）")
