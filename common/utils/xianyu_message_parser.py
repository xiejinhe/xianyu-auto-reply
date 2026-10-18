"""
闲鱼消息内容解析（公共模块）

统一"闲鱼消息内容 JSON → (文本, 图片URL列表, 消息类型)"的解析逻辑，供以下三处
共同调用，消除历史上各写一套、互相不同步的重复实现：

1. backend-web  chat_new._parse_message        —— HTTP 拉取历史消息（content.custom.data，base64）
2. backend-web  push_message_parser._decode_content —— IM WebSocket 实时推送（["6"]["3"]["5"] 明文 / ["1"] base64）
3. websocket    message_handler.parse_chat_message  —— 自动回复引擎（同 2 的原始推送报文）

设计要点：
- 不同来源的"信封（envelope）"结构不同（HTTP 模型 vs 原始 IM 推送），信封字段的提取
  仍由各调用方自己完成；
- 一旦拿到承载消息内容的**载荷字符串**（明文 JSON 或 base64 编码的 JSON），后续
  "解码 + 判定文本/图片/语音"的核心逻辑完全一致，统一收敛到本模块。

功能：
1. load_content_json    —— 载荷字符串（明文 JSON / base64）→ 内容 dict
2. looks_like_content   —— 判断某 dict 是否为一条消息内容载荷
3. decode_first_content —— 从多个候选载荷中挑第一个"像内容"的，解析为内容 dict
4. interpret_content    —— 内容 dict → (text, images, msg_type)
5. parse_content_payloads —— 便捷组合：候选载荷列表 → (text, images, msg_type)
"""
import base64
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 已知的消息内容标识键：命中任一即认为该 dict 是一条消息内容载荷
_CONTENT_KEYS = ("text", "image", "picUrl", "audio")

# 语音消息的降级文案：contentType==3 的真实内容藏在 audio 字段里，
# interpret_content 统一先降级成这个占位文本，再由调用方提取音频地址。
AUDIO_PLACEHOLDER_TEXT = "[语音消息]"

# 系统 / 交易事件消息里常出现的业务关键词，用于把"卡片 / 系统摘要"归类到具体事件。
_EVENT_KEYWORDS = (
    "演出", "发货", "物流", "付款", "拍下", "专拍", "待付款", "待发货",
    "已发货", "确认收货", "退款", "交易成功", "交易关闭", "订单",
    "填写演出信息", "发货凭证", "收货", "催付",
)

# 闲鱼的系统 / 交易事件消息（付款状态变更、填写演出信息、发货凭证等）在 IM 里
# 通常以"方括号包裹的摘要"出现，例如 [你已填写演出信息，请确认]、[我已发货，请查看
# 发货凭证]；用户正常聊天几乎不会以 [ 开头。据此即可在不采样真实载荷的前提下，
# 把这类消息从普通聊天气泡识别为"事件卡片"。
_EVENT_KIND_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("performance", ("演出",)),
    ("refund", ("退款", "交易关闭")),
    ("shipping", ("发货", "物流", "收货", "凭证")),
    ("payment", ("付款", "拍下", "专拍", "催付", "待付款")),
    ("done", ("交易成功",)),
)


def classify_event(text: str, msg_type: str) -> Tuple[bool, str]:
    """
    判断一条消息是否为「系统 / 交易事件卡片」，并返回事件类别。

    识别依据（不依赖真实载荷采样，纯文本规则）：
    - 闲鱼系统摘要统一用方括号包裹（text 以 [ 开头）→ 视为事件；
    - 或 type=="card" 且文本命中交易 / 演出等业务关键词 → 视为事件。

    Args:
        text: 消息展示文本（已解码后的 title / summary / reminder 等）。
        msg_type: 消息类型（'text' / 'card' / 'image' / 'audio' ...）。

    Returns:
        (is_event, event_kind)：is_event 为真时 event_kind ∈
        {'performance','refund','shipping','payment','done','generic'}。
    """
    if not text or msg_type in ("image", "audio"):
        return False, ""
    t = text.strip()
    if t.startswith("["):
        is_event = True
    elif msg_type == "card" and any(k in t for k in _EVENT_KEYWORDS):
        is_event = True
    else:
        return False, ""
    for kind, keys in _EVENT_KIND_RULES:
        if any(k in t for k in keys):
            return True, kind
    return True, "generic"


def load_content_json(raw: Any) -> Optional[Dict[str, Any]]:
    """将消息内容载荷字符串解析为内容 JSON dict（兼容明文 JSON 与 base64）

    IM 实时推送的内容字段是【明文 JSON 字符串】；HTTP 历史消息与旧格式推送则是
    【base64 编码后的 JSON】。本函数先按明文 JSON 尝试，失败再按 base64 解码后解析，
    两者都失败返回 None。

    Args:
        raw: 消息内容候选值（通常为字符串）
    Returns:
        解析成功的 dict；无法解析时返回 None
    """
    if not raw or not isinstance(raw, str):
        return None

    # 1) 优先按明文 JSON 解析（实时推送为明文）
    text = raw.strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    # 2) 回退按 base64 解码后解析（HTTP 历史 / 旧格式推送为 base64）
    try:
        obj = json.loads(base64.b64decode(raw).decode("utf-8"))
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    return None


def looks_like_content(obj: Any) -> bool:
    """判断解析出的 dict 是否为一条消息内容载荷

    用于在多个候选字段中挑出真正承载消息内容的那个：只要含有内容类型标记
    (contentType) 或任一已知内容键 (text/image/picUrl/audio) 即认定为内容，
    避免误把某个恰好是合法 JSON 的非内容字段当成内容。

    Args:
        obj: 待判断的值
    Returns:
        是否像消息内容
    """
    if not isinstance(obj, dict):
        return False
    return "contentType" in obj or any(k in obj for k in _CONTENT_KEYS)


def decode_first_content(candidates: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """从多个候选载荷中挑第一个能解析且"像消息内容"的，返回内容 dict

    Args:
        candidates: 候选载荷字符串序列，按优先级排列
    Returns:
        第一个像内容的 dict；都不满足时返回 None
    """
    for candidate in candidates:
        decoded = load_content_json(candidate)
        if decoded is not None and looks_like_content(decoded):
            return decoded
    return None


def interpret_content(decoded: Dict[str, Any]) -> Tuple[str, List[str], str]:
    """将内容 dict 解释为 (文本, 图片URL列表, 消息类型)

    覆盖闲鱼消息的通用内容类型；与历史两套实现（chat_new._parse_message /
    push_message_parser._decode_content）保持一致：
    - contentType==1 且含 text → 文本
    - contentType==2 且含 image → 图片（取 image.pics[].url）
    - contentType==3 且含 audio → 语音（以 "[语音消息]" 文本表示）
    - 兜底：含 text → 文本；含 picUrl → 图片（旧格式）
    - 都不匹配 → 返回空类型 ""，由调用方决定降级策略

    Args:
        decoded: 已解析的内容 dict
    Returns:
        (text, images, msg_type)；msg_type ∈ {"text","image",""}
    """
    if not isinstance(decoded, dict):
        return ("", [], "")

    content_type = decoded.get("contentType", 0)

    if content_type == 1 and "text" in decoded:
        return (_extract_text(decoded["text"]), [], "text")

    if content_type == 2 and "image" in decoded:
        return ("", _extract_image_urls(decoded), "image")

    if content_type == 3 and "audio" in decoded:
        return (AUDIO_PLACEHOLDER_TEXT, [], "text")

    # 兜底：未带标准 contentType 但结构可识别
    if "text" in decoded:
        return (_extract_text(decoded["text"]), [], "text")

    if "picUrl" in decoded and decoded.get("picUrl"):
        return ("", [str(decoded["picUrl"])], "image")

    return ("", [], "")


def extract_audio_info(decoded: Dict[str, Any]) -> Tuple[str, int]:
    """从语音消息内容里提取 (音频URL, 时长毫秒)

    闲鱼语音消息为 contentType==3 + audio 对象。audio 内部的字段名在不同端
    上不完全一致，这里做兼容匹配，取不到就返回空串/0。

    Args:
        decoded: 已解析的内容 dict
    Returns:
        (audio_url, duration_ms)；取不到时返回 ("", 0)
    """
    if not isinstance(decoded, dict):
        return ("", 0)

    audio = decoded.get("audio")
    if not isinstance(audio, dict):
        return ("", 0)

    url = ""
    for key in ("url", "mediaUrl", "fileUrl", "audioUrl", "src", "ossUrl"):
        val = audio.get(key)
        if isinstance(val, str) and val.startswith(("http://", "https://", "//")):
            url = val
            if url.startswith("//"):
                url = "https:" + url
            break
    if not url and isinstance(audio.get("url"), str) and audio["url"]:
        url = audio["url"]

    duration = 0
    for key in ("duration", "durationMs", "duration_ms", "length", "time", "second"):
        val = audio.get(key)
        if isinstance(val, (int, float)) and val > 0:
            # 数值较小时按秒处理，较大时按毫秒处理
            duration = int(val * 1000) if val < 1000 else int(val)
            break

    return (url, duration)


def parse_content_payloads(candidates: Sequence[Any]) -> Tuple[str, List[str], str]:
    """便捷组合：从候选载荷列表解析出 (text, images, msg_type)

    等价于 decode_first_content + interpret_content。都无法识别时返回
    ("", [], "text")，与历史推送解析的降级行为一致（交由上层用提醒文本兜底）。

    Args:
        candidates: 候选载荷字符串序列，按优先级排列
    Returns:
        (text, images, msg_type)
    """
    decoded = decode_first_content(candidates)
    if decoded is None:
        return ("", [], "text")
    text, images, msg_type = interpret_content(decoded)
    return (text, images, msg_type or "text")


def parse_content_with_audio(
    candidates: Sequence[Any],
) -> Tuple[str, List[str], str, str, int]:
    """在 parse_content_payloads 基础上，额外解析语音消息的音频地址与时长

    语音（contentType==3 + audio）在 interpret_content 里会先降级成占位文本，
    真实的音频地址藏在 decoded["audio"] 中。调用方必须把 msg_type 改成 "audio"
    并带上 audioUrl，前端才能播放或转文字；只拿占位文本的话用户看到的就是
    一句干巴巴的「[语音消息]」，既听不了也转不了。

    Args:
        candidates: 候选载荷字符串序列，按优先级排列
    Returns:
        (text, images, msg_type, audio_url, audio_duration)
        非语音消息时后两项为 ("", 0)；语音但取不到地址时保持占位文本与 text 类型
    """
    decoded = decode_first_content(candidates)
    if decoded is None:
        return ("", [], "text", "", 0)

    text, images, msg_type = interpret_content(decoded)
    audio_url, audio_duration = "", 0
    if text == AUDIO_PLACEHOLDER_TEXT:
        audio_url, audio_duration = extract_audio_info(decoded)
        if audio_url:
            msg_type = "audio"
            text = ""

    return (text, images, msg_type or "text", audio_url, audio_duration)


def _extract_text(text_obj: Any) -> str:
    """从 text 字段提取纯文本（text 可能是 {"text": "..."} 或直接字符串）"""
    if isinstance(text_obj, dict):
        return str(text_obj.get("text", ""))
    return str(text_obj)


def _extract_image_urls(decoded: Dict[str, Any]) -> List[str]:
    """从图片内容 dict 提取所有图片 URL（image.pics[].url）"""
    pics = decoded.get("image", {}).get("pics", [])
    if not isinstance(pics, list):
        return []
    return [p.get("url", "") for p in pics if isinstance(p, dict) and p.get("url")]
