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
from app.services.chat_new.peer_read_service import save_peer_reads
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
    managed_sender: bool = Field(
        default=False,
        description="发送者是否为「其它托管账号」（非本账号）：渲染成对方消息(左侧)，但跳过响铃与跨账号 toast",
    )
    msg_type: str = Field(default="text", description="text / image / card / system")
    text: str = Field(default="", description="文本内容")
    images: List[str] = Field(default_factory=list, description="图片URL列表")
    time_ms: int = Field(default=0, description="消息时间（毫秒时间戳）")
    card: Optional[dict] = Field(default=None, description="订单卡片富数据（按钮/订单号/演出信息），非交易卡片为None")
    revoked: bool = Field(default=False, description="是否为撤回占位消息（对方撤回了原消息）")
    revoked_original: str = Field(default="", description="撤回前原文线索（reminderContent 旧摘要），前端默认折叠、点「显示」展开")
    reply_to_message_id: str = Field(default="", description="引用消息（回复）：被引用消息的 IM messageId，前端本地查表渲染引用块")


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
        # 托管账号互聊：对方是自己另一个托管号时 isSelf=false（正确渲染到左侧），
        # 但 managedSender=true，前端照旧跳过响铃/跨账号 toast（2026-10-28）
        "managedSender": req.managed_sender,
        "type": req.msg_type or "text",
        "text": req.text,
        "images": req.images or [],
        "time": req.time_ms or 0,
        # 订单卡片富数据（修改价格/去填写/发货凭证等按钮 + 订单号 + 演出信息），
        # 由 websocket 服务从 msg["1"]["6"]["3"]["5"] 的 dxCard 解析后转发。
        # 前端有 card 时渲染富事件卡，无则退回纯文本。
        "card": req.card,
        # 撤回占位元数据（与历史路径 _parse_message / 推送路径
        # push_message_parser 同格式）：前端渲染「对方撤回了一条消息」
        # 带头像，旧摘要默认折叠、点「显示」才展开
        "revoked": req.revoked,
        "revokedOriginal": req.revoked_original,
        # 引用消息（回复）：被引用消息的 IM messageId（协议实证 2026-10-20），
        # 前端在本地消息列表按它找原消息，渲染「引用块 + 正文」
        "replyToMessageId": req.reply_to_message_id,
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


class AppChannelPeerReadRequest(BaseModel):
    """websocket 服务转发的「对方已读」回执"""

    account_id: str = Field(description="接收方账号ID（本系统的账号，即消息发送方）")
    cid: str = Field(description="会话ID")
    message_ids: List[str] = Field(
        default_factory=list, description="被对方读掉的 IM messageId 列表"
    )
    read_at: int = Field(default=0, description="回执时间戳（毫秒）")


@router.post("/chat-peer-read")
async def receive_app_channel_peer_read(
    req: AppChannelPeerReadRequest,
    x_internal_token: Optional[str] = Header(default=None, alias=INTERNAL_AUTH_HEADER),
):
    """
    接收「对方已读」回执并广播给前端

    闲鱼把已读回执做成 IM 长连接帧 {"1":[被读messageId...], "2":2,
    "3":"cid@goofish", "5":时间戳}，只推给**发送方**——这就是 APP 气泡下
    已读/未读的数据源（2026-10-20 协议实证）。转到这里后按 messageId
    把前端对应的自己发出的消息从「未读」翻成「已读」。

    历史接口的两个字段都不是实时回执，不能用：
    - 外层 readStatus：语义是**我方**有没有读过这条，对自己发的恒为真；
    - message.unreadCount：是消息**发出那一刻**的静态快照，之后不再变。
    """
    require_valid_internal_token(x_internal_token, get_settings().internal_api_token)

    if not req.account_id or not req.cid:
        return {"success": False, "delivered": False, "message": "缺少 account_id 或 cid"}

    try:
        delivered = await get_im_session_manager().broadcast_peer_read(
            account_id=req.account_id,
            cid=req.cid,
            message_ids=req.message_ids or [],
            read_at=req.read_at or 0,
        )
    except Exception as exc:
        logger.warning(f"【{req.account_id}】已读回执广播失败: {exc}")
        return {"success": False, "delivered": False, "message": str(exc)}

    # 落库：即使没有前端在线（delivered=False）也要持久化，否则刷新后
    # 「已读」标记会退回历史值。失败不影响本次广播结果。
    try:
        saved = await save_peer_reads(
            account_id=req.account_id,
            cid=req.cid,
            message_ids=req.message_ids or [],
            read_at=req.read_at or 0,
        )
        if saved:
            logger.info(
                f"【{req.account_id}】已读回执已落库 (cid={req.cid}, n={saved})"
            )
    except Exception as exc:
        logger.warning(f"【{req.account_id}】已读回执落库失败(非致命): {exc}")

    return {"success": True, "delivered": delivered}


# ==================== 协议调研内部接口（勿删） ====================
# 背景：闲鱼 IM 的部分消息结构（如 APP 的「引用/回复」卡片）没有公开协议，
# 需要拉原始历史帧或盲测发送 payload 来逆向。这两个接口仅供协议调研使用，
# 鉴权与上面两个推送接口同一套（X-Internal-Token）。


class RawSendRequest(BaseModel):
    """盲测发送：custom.data 的 JSON（base64 前）与 custom.type 由调用方给"""

    cid: str = Field(description="会话ID（不含@goofish后缀）")
    to_user_id: str = Field(description="对方用户ID")
    custom_type: int = Field(description="custom.type（如文本=1、图片=2）")
    payload: dict = Field(description="custom.data 的 JSON 内容（base64 编码前）")
    reply_to_message_id: str = Field(default="", description="被引用消息的 IM messageId（真引用协议）")


@router.get("/chat-new/raw-history/{account_id}")
async def get_raw_history(
    account_id: str,
    cid: str,
    limit: int = 50,
    x_internal_token: Optional[str] = Header(default=None, alias=INTERNAL_AUTH_HEADER),
):
    """拉取指定会话的原始历史消息（userMessageModels 原文，不做解析）。

    用于逆向没有公开协议的消息结构（如引用消息的 custom.data 字段）。
    """
    require_valid_internal_token(x_internal_token, get_settings().internal_api_token)

    manager = get_im_session_manager()
    client = manager.clients.get(account_id)
    if not client or not client.is_connected:
        return {"success": False, "message": "账号未连接，请先连接"}

    body = await client.get_messages(cid=cid, start_timestamp=None, limit=limit)
    if isinstance(body, dict) and "reason" in body:
        return {"success": False, "message": body.get("developerMessage", body.get("reason", ""))}
    return {"success": True, "data": body}


@router.post("/chat-new/raw-send/{account_id}")
async def raw_send_message(
    account_id: str,
    req: RawSendRequest,
    x_internal_token: Optional[str] = Header(default=None, alias=INTERNAL_AUTH_HEADER),
):
    """盲测发送原始 payload（协议调研用）。

    正常业务发送请走 /chat-new/send-message，勿用本接口。
    """
    require_valid_internal_token(x_internal_token, get_settings().internal_api_token)

    manager = get_im_session_manager()
    client = manager.clients.get(account_id)
    if not client or not client.is_connected:
        return {"success": False, "message": "账号未连接，请先连接"}

    result = await client.send_raw_message(
        cid=req.cid,
        to_user_id=req.to_user_id,
        custom_type=req.custom_type,
        payload=req.payload,
        reply_to_message_id=req.reply_to_message_id,
    )
    return {"success": True, "data": {"uuid": result.get("uuid", "")}}
