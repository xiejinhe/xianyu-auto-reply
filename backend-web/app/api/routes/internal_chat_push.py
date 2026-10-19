"""
服务间内部接口：接收 websocket 服务转发的 IM 推送

背景（重要，勿删）：
闲鱼服务端对「网页 IM 会话」的推送不稳定——APP 来源的消息常常一帧都不推，
导致网页端只能靠 15 秒对账轮询兜底（表现为"铃声响了但列表/红点不动"）。
而 websocket 服务的 App 通道长连接（7×24 跑自动回复的那条）能收到全部推送帧。
这里接收它的转发，按 messageId 去重后广播给前端，实现秒级展示。

鉴权：服务间共享令牌 X-Internal-Token（common.utils.internal_auth），
与 websocket 的 /internal 接口同一套机制，不对外开放。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Header
from loguru import logger
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.chat_new import get_im_session_manager
from common.utils.internal_auth import INTERNAL_AUTH_HEADER, require_valid_internal_token

router = APIRouter(prefix="/internal", tags=["内部接口"])


class AppChannelPushRequest(BaseModel):
    """websocket 服务转发的单条聊天消息"""

    account_id: str = Field(description="接收方账号ID（本系统的账号）")
    cid: str = Field(description="会话ID")
    message_id: str = Field(default="", description="消息ID，前端去重用")
    sender_id: str = Field(default="", description="发送者用户ID")
    sender_name: str = Field(default="", description="发送者昵称")
    is_self: bool = Field(default=False, description="是否本账号自己发出的消息")
    msg_type: str = Field(default="text", description="text / image / card")
    text: str = Field(default="", description="文本内容")
    images: List[str] = Field(default_factory=list, description="图片URL列表")
    time_ms: int = Field(default=0, description="消息时间（毫秒时间戳）")
    card: Optional[dict] = Field(default=None, description="订单卡片富数据（按钮/订单号/演出信息），非交易卡片为None")


class AppChannelTypingRequest(BaseModel):
    """websocket 服务转发的「对方正在输入」状态"""

    account_id: str = Field(description="接收方账号ID（本系统的账号）")
    cid: str = Field(description="会话ID")
    typing: bool = Field(default=False, description="True=正在输入，False=停止输入")


@router.post("/chat-push")
async def receive_app_channel_push(
    req: AppChannelPushRequest,
    x_internal_token: Optional[str] = Header(default=None, alias=INTERNAL_AUTH_HEADER),
):
    """
    接收 App 通道推送并广播给前端在线聊天

    websocket 服务每解析出一条聊天消息就调用一次；本接口：
    1. 校验服务间令牌；
    2. 没有前端订阅该账号时直接返回（不做任何多余工作）；
    3. 与网页 IM 推送 / 系统内互发补偿按 messageId 去重后广播。
    """
    require_valid_internal_token(x_internal_token, get_settings().internal_api_token)

    if not req.account_id or not req.cid:
        return {"success": False, "delivered": False, "message": "缺少 account_id 或 cid"}

    message = {
        "messageId": req.message_id or "",
        "senderId": req.sender_id,
        "senderName": req.sender_name,
        "isSelf": req.is_self,
        "type": req.msg_type or "text",
        "text": req.text,
        "images": req.images or [],
        "time": req.time_ms or 0,
        # 订单卡片富数据（修改价格/去填写/发货凭证等按钮 + 订单号 + 演出信息），
        # 由 websocket 服务从 msg["1"]["6"]["3"]["5"] 的 dxCard 解析后转发。
        # 前端有 card 时渲染富事件卡，无则退回纯文本。
        "card": req.card,
        # 来源标记：App 通道的 messageId（bizTag 里的 32 位 hex）与 IM 网关
        # 历史接口的 messageId（如 4347684267508.PNM）是两套互不相通的 ID，
        # 同一条消息会以两个不同 ID 的副本到达前端。前端 mergeMessages
        # 依赖这个标记，对带标记的副本按「发送者+内容+时间」合并去重，
        # 避免先实时推一条、历史兜底再拉一条后出现重复气泡（实测踩坑）。
        "pushSource": "app_channel",
    }

    try:
        delivered = await get_im_session_manager().broadcast_app_channel_message(
            account_id=req.account_id, cid=req.cid, message=message
        )
    except Exception as exc:
        logger.warning(f"【{req.account_id}】App通道推送广播失败: {exc}")
        return {"success": False, "delivered": False, "message": str(exc)}

    return {"success": True, "delivered": delivered}


@router.post("/chat-typing")
async def receive_app_channel_typing(
    req: AppChannelTypingRequest,
    x_internal_token: Optional[str] = Header(default=None, alias=INTERNAL_AUTH_HEADER),
):
    """
    接收「对方正在输入」状态并广播给前端

    闲鱼的状态帧（{"1":[{"1":cid,"2":1,"3":1|0,...}]}）由 websocket 服务的
    App 通道解析后转到此处。状态不做去重、不落缓存——1/0 交替是正常序列，
    前端收到 True 后显示提示、收到 False 或超时后隐藏。
    """
    require_valid_internal_token(x_internal_token, get_settings().internal_api_token)

    if not req.account_id or not req.cid:
        return {"success": False, "delivered": False, "message": "缺少 account_id 或 cid"}

    try:
        delivered = await get_im_session_manager().broadcast_typing(
            account_id=req.account_id, cid=req.cid, typing=req.typing
        )
    except Exception as exc:
        logger.warning(f"【{req.account_id}】输入状态广播失败: {exc}")
        return {"success": False, "delivered": False, "message": str(exc)}

    return {"success": True, "delivered": delivered}
