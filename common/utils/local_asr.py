"""
本地离线语音转文字（ASR）

使用 faster-whisper（CTranslate2 版 Whisper），纯 CPU 即可运行，
不依赖任何外部 API Key / 不需要联网（首次下载模型除外）。

为什么需要它：
    云端 ASR 要填 API Key，本地跑模型则开箱即用、数据不出机器。

注意：
    本模块必须在 import faster_whisper / huggingface_hub **之前**设置好
    HF_ENDPOINT / HF_HUB_DISABLE_XET，否则国内环境会走 xet 通道报 401。
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import threading
from typing import Any

# 必须在导入 faster_whisper 之前设置：
# - HF_ENDPOINT: 国内直连 huggingface.co 很慢，走镜像
# - HF_HUB_DISABLE_XET: huggingface_hub 新版默认走 xet 通道，
#   该通道不认 HF_ENDPOINT 镜像，会 401 Unauthorized
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from loguru import logger  # noqa: E402

# 可选模型：体积从小到大，中文准确率从低到高
MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
# 实测对比（3 段闲鱼砍价/发货场景中文语音）：
#   base 会把「包邮」听成「包油」、「尺码」听成「石码」，还会输出繁体字，且疑问句问号基本丢失
#   small 字词准确率明显更高，问号能正常输出 → 默认用 small
DEFAULT_LOCAL_MODEL = "small"

# 提示词（initial_prompt）对中文效果影响极大，实测能同时解决三件事：
#   1. 繁体字 → 简体（base 不加提示词时输出「這個寶貝還在嗎」）
#   2. 场景词纠错（包邮 / 尺码 / 发货）
#   3. 句末标点和疑问句问号
DEFAULT_INITIAL_PROMPT = (
    "以下是普通话的句子，请使用中文标点符号，句末要有标点。"
    "这是闲鱼二手交易聊天，常见说法：包邮、便宜一点、能优惠吗、什么时候发货、"
    "尺码、库存、已拍下、下单、谢谢老板。"
)

# 需要转成全角的半角标点（仅当它们紧跟在中文字符后面时才转，避免误伤英文和数字）
_PUNCT_MAP = {
    ",": "，",
    "?": "？",
    "!": "！",
    ";": "；",
    ":": "：",
    "(": "（",
    ")": "）",
}

_MODEL_LOCK = threading.Lock()
_MODELS: dict[str, Any] = {}
_AVAILABLE: bool | None = None
_IMPORT_ERROR: str = ""


def is_available() -> bool:
    """判断本机是否装了 faster-whisper 依赖"""
    global _AVAILABLE, _IMPORT_ERROR
    if _AVAILABLE is None:
        try:
            import faster_whisper  # noqa: F401

            _AVAILABLE = True
            _IMPORT_ERROR = ""
        except Exception as e:
            _AVAILABLE = False
            _IMPORT_ERROR = str(e)
            logger.warning(f"未安装本地语音转文字依赖 faster-whisper：{e}")
    return _AVAILABLE


def unavailable_reason() -> str:
    """返回本地 ASR 不可用的原因（供前端提示安装命令）"""
    if is_available():
        return ""
    return _IMPORT_ERROR or "未安装 faster-whisper"


def is_model_ready(size: str) -> bool:
    """模型是否已加载到内存（不触发下载）"""
    return size in _MODELS


def _is_cjk(ch: str) -> bool:
    """判断是否为中日韩统一表意文字（用于标点全角化的上下文判断）"""
    return bool(ch) and ("\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf")


def _get_model(size: str) -> Any:
    """加载（并缓存）指定大小的模型；首次调用会联网下载模型文件"""
    from faster_whisper import WhisperModel

    key = size if size in MODEL_SIZES else DEFAULT_LOCAL_MODEL
    with _MODEL_LOCK:
        model = _MODELS.get(key)
        if model is None:
            logger.info(f"加载本地语音转文字模型 {key}（首次使用会自动下载，请耐心等待）")
            model = WhisperModel(key, device="cpu", compute_type="int8")
            _MODELS[key] = model
            logger.info(f"本地语音转文字模型 {key} 加载完成")
        return model


def normalize_punctuation(text: str) -> str:
    """
    把中文语境里的半角标点转成全角，并去掉中文之间多余的空格

    Whisper 输出的中英文标点混用（「便宜一点吗? 包邮吗?」），中文里读起来别扭，
    统一成全角。只在标点前面是中文字符时才转换，避免误伤英文和数字（如 3.5、iPhone,）。
    """
    if not text:
        return text

    result: list[str] = []
    for i, ch in enumerate(text):
        prev = text[i - 1] if i > 0 else ""
        if ch in _PUNCT_MAP and _is_cjk(prev):
            result.append(_PUNCT_MAP[ch])
        elif ch == "." and _is_cjk(prev):
            result.append("。")
        else:
            result.append(ch)

    out = "".join(result)
    # 去掉中文之间被插入的空格：「今天能发货吗? 包邮吗?」→「今天能发货吗？包邮吗？」
    out = re.sub(r"(?<=[\u4e00-\u9fff\uff0c\u3002\uff1f\uff01]) (?=[\u4e00-\u9fff])", "", out)
    return out.strip()


def _transcribe_sync(size: str, audio_bytes: bytes, language: str) -> str:
    """同步转写（内部函数，实际调用请用 transcribe 以免阻塞事件循环）"""
    model = _get_model(size)

    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(audio_bytes)
        segments, _info = model.transcribe(
            path,
            language=language or "zh",
            beam_size=5,
            # 提示词：显著改善简繁体、场景词与标点符号
            initial_prompt=DEFAULT_INITIAL_PROMPT,
            # 过滤静音段，减少录音头尾空白产生的幻听
            vad_filter=True,
            # 不参考上一段内容，避免长录音里反复重复同一句
            condition_on_previous_text=False,
        )
        raw = "".join(seg.text for seg in segments).strip()
        return normalize_punctuation(raw) if (language or "zh").startswith("zh") else raw
    finally:
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


async def transcribe(audio_bytes: bytes, size: str = DEFAULT_LOCAL_MODEL, language: str = "zh") -> str:
    """
    本地离线转写音频

    Args:
        audio_bytes: 音频字节（推荐 16k/16bit 单声道 WAV，其他常见格式也能解）
        size: 模型大小，见 MODEL_SIZES
        language: 语言代码，默认中文

    Raises:
        RuntimeError: 依赖未安装或转写失败
    """
    if not is_available():
        raise RuntimeError("未安装本地语音转文字依赖，请在后端目录执行：pip install faster-whisper av==14.0.1")

    # 推理是 CPU 密集的同步调用，丢到线程里跑，避免阻塞 FastAPI 事件循环
    return await asyncio.to_thread(_transcribe_sync, size, audio_bytes, language)


async def warmup(size: str = DEFAULT_LOCAL_MODEL) -> str:
    """预热：提前下载并加载模型，避免首次转写时等待"""
    if not is_available():
        raise RuntimeError("未安装本地语音转文字依赖，请在后端目录执行：pip install faster-whisper av==14.0.1")
    await asyncio.to_thread(_get_model, size)
    return size
