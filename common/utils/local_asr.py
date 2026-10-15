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
DEFAULT_LOCAL_MODEL = "base"

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


def _transcribe_sync(size: str, audio_bytes: bytes, language: str) -> str:
    """同步转写（内部函数，实际调用请用 transcribe 以免阻塞事件循环）"""
    model = _get_model(size)

    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(audio_bytes)
        segments, _info = model.transcribe(path, language=language or "zh", beam_size=5)
        return "".join(seg.text for seg in segments).strip()
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
