"""
订单卡片原始载荷采样器（只读，零风控）——backend-web 与 websocket 服务共用

背景：闲鱼 IM 的订单/交易卡片（「我已拍下，待付款」+修改价格、「我已付款，请
填写演出信息」+去填写、「我已发货，请查看发货凭证」等）在 APP 里带按钮和
结构化字段，但两套解析链路都只取了标题文本（backend-web 的
push_message_parser 取 msg["6"]["3"]["2"]，websocket 服务同样只到标题），
按钮 / action / 订单号等字段从未被提取——因为真实载荷长什么样没有采样过，
key 名无从谈起。

本模块把这类卡片的【原始解密后 JSON】原样落盘（默认
backend-web/logs/order_card_samples.jsonl，一行一条），供后续：
1. 分析按钮/字段的真实 key 名，设计「卡片按钮 → 系统操作」映射；
2. 核对 classify_event 的事件分类规则是否需要补充关键词；
3. 验证发货凭证卡与 no-logistics-delivery 接口的产物关系。

为什么要两个服务都挂：backend-web 的 IM 是网页会话（sendByReceiverScope），
闲鱼不会把 APP 侧实时卡片 syncPush 给它；websocket 服务那套自动回复长连接
反而收全部推送。只挂一边会漏（2026-10-19 实测：用户 APP 里走完整流程，
backend-web 侧零推送，全部走了 websocket 服务）。

安全承诺：
- 纯本地文件写入 + 日志，**绝不主动调用任何闲鱼接口**，账号零风险；
- 写入失败只静默降级，绝不影响消息解析主链路；
- 按 messageId 去重，历史消息重复拉取不会重复落盘；
- 可用环境变量 ORDER_CARD_SAMPLE=0 一键关闭；
- 落盘文件可用 ORDER_CARD_SAMPLE_FILE 覆盖（默认仓库内 backend-web/logs/）。
"""
import json
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

# 仓库根目录（common/utils/card_sampler.py → parents[2]）
_REPO_ROOT = Path(__file__).resolve().parents[2]

# 采样文件：默认 backend-web/logs/order_card_samples.jsonl（与 loguru 文件日志同目录），
# 两个服务共写一个文件（单行 JSON append，量小无竞争问题）
SAMPLE_FILE: Path = Path(
    os.environ.get("ORDER_CARD_SAMPLE_FILE", "")
).resolve() if os.environ.get("ORDER_CARD_SAMPLE_FILE") else (
    _REPO_ROOT / "backend-web" / "logs" / "order_card_samples.jsonl"
)

# 命中这些关键词的卡片才采样（订单/交易类）；普通卡片不采，控制文件体积
_TRADE_KEYWORDS = (
    "拍下", "付款", "演出", "发货", "确认", "价格", "收货", "退款", "门票", "凭证",
)

# 已采样过的 messageId 集合（防重复落盘），超量后清空重来
_sampled_ids: set[str] = set()
_SAMPLED_IDS_CAP = 10000


def _enabled() -> bool:
    return os.environ.get("ORDER_CARD_SAMPLE", "1").strip().lower() not in ("0", "false", "no")


def looks_like_trade_card(text: str, raw: Any = None) -> bool:
    """卡片是否命中交易关键词（决定是否值得采样）。

    标题没命中时兜底扫一遍原始 JSON——订单卡片的标题不一定在预期字段里，
    但载荷正文几乎必然含「演出/付款/发货」等词。
    """
    t = str(text or "")
    if any(k in t for k in _TRADE_KEYWORDS):
        return True
    if raw is not None:
        try:
            blob = json.dumps(raw, ensure_ascii=False)
        except Exception:
            return False
        return any(k in blob for k in _TRADE_KEYWORDS)
    return False


def sample_card(
    source: str,
    account_id: str,
    cid: str,
    message_id: str,
    text: str,
    raw: Any,
) -> bool:
    """把一张订单卡片的原始载荷写入采样文件。

    Args:
        source: 来源标识：push_card（backend-web 推送卡片）/
            push_card_update（backend-web 卡片更新）/ push_summary（backend-web
            方括号摘要）/ http_history（backend-web 聊天历史）/
            ws_push（websocket 服务长连接推送，主通道）
        account_id: 账号标识
        cid: 会话 id（调用方负责过 normCid）
        message_id: 消息 id（去重键；为空则放弃采样）
        text: 卡片标题/摘要文本（用于关键词命中判断）
        raw: 解密后的原始 dict

    Returns:
        是否实际写入。
    """
    if not _enabled():
        return False
    message_id = str(message_id or "")
    if not message_id:
        return False
    if message_id in _sampled_ids:
        return False
    if not looks_like_trade_card(text, raw):
        return False
    if not isinstance(raw, dict) or not raw:
        return False

    record = {
        "ts": int(time.time() * 1000),
        "source": source,
        "account_id": str(account_id or ""),
        "cid": str(cid or ""),
        "messageId": message_id,
        "text": str(text or ""),
        "raw": raw,
    }
    try:
        line = json.dumps(record, ensure_ascii=False)
        SAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SAMPLE_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        _sampled_ids.add(message_id)
        if len(_sampled_ids) > _SAMPLED_IDS_CAP:
            _sampled_ids.clear()
        logger.info(
            f"[订单卡片采样] 已落盘 source={source} account={account_id} "
            f"cid={cid} messageId={message_id} 文本={text!r}"
        )
        return True
    except Exception as e:
        # 采样是锦上添花，任何失败都不能影响消息解析主链路
        logger.warning(f"[订单卡片采样] 写入失败（不影响主链路）: {e}")
        return False
