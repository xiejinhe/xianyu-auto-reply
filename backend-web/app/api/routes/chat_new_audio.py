"""
在线聊天(新) - 语音相关 API 路由

功能：
1. 语音转文字（ASR）：复用账号已配置的 AI 接口，把语音转成文本
2. 发送语音：转码为 mp3 后上传到闲鱼CDN，再通过IM协议发送

与 chat_new.py 共用同一 prefix="/chat-new"。
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile

import httpx
from fastapi import APIRouter, Depends, File, Form, UploadFile
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_active_user, get_db_session
from app.services.chat_new import get_im_session_manager
from common.models import User, XYAccount
from common.schemas.common import ApiResponse
from common.services.ai_provider_service import build_openai_url, normalize_ai_settings
from common.utils.auth_scope import is_admin_user
from common.utils.image_uploader import ImageUploader
from common.utils.local_asr import (
    DEFAULT_LOCAL_MODEL,
    MODEL_SIZES,
    is_available as local_asr_available,
    is_model_ready,
    transcribe as local_transcribe,
    unavailable_reason,
    warmup as local_warmup,
)

router = APIRouter(prefix="/chat-new")

# 允许的音频类型与大小上限
_ALLOWED_AUDIO_TYPES = {
    "audio/webm",
    "audio/ogg",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/wav",
    "audio/x-wav",
    "audio/x-m4a",
    "audio/aac",
    "audio/flac",
    "audio/amr",
    "application/octet-stream",  # 部分浏览器 MediaRecorder 不带具体类型
}
_MAX_AUDIO_SIZE = 10 * 1024 * 1024  # 10MB

# 语音转文字（ASR）模型候选：按优先级依次尝试
# - 阿里云百炼（DashScope）OpenAI 兼容模式：走 chat/completions 的 input_audio
# - 其他 OpenAI 兼容服务商：走 /audio/transcriptions（Whisper 系）
_DASHSCOPE_ASR_MODELS = ["qwen3-asr-flash", "qwen-audio-3.1-asr-flash-message"]
_WHISPER_ASR_MODELS = ["whisper-1", "FunAudioLLM/SenseVoiceSmall"]
_ASR_TIMEOUT = 120.0


def _ffmpeg_path() -> str | None:
    """查找可用的 ffmpeg，找不到返回 None（找不到则跳过转码、原样上传）"""
    return shutil.which("ffmpeg")


async def _get_owned_chat_account(
    account_id: str, current_user: User, db: AsyncSession
) -> XYAccount | None:
    """校验账号归属：管理员可操作任意账号，普通用户仅能操作自己的账号"""
    query = select(XYAccount).where(XYAccount.account_id == account_id)
    if not is_admin_user(current_user):
        query = query.where(XYAccount.owner_id == current_user.id)
    return (await db.execute(query)).scalar_one_or_none()


def _transcode_to_mp3(src_path: str, dst_path: str) -> bool:
    """把任意音频转码成 mp3（闲鱼App对 webm/opus 支持不佳，mp3 兼容性最好）"""
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return False
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-y", "-i", src_path,
                "-vn", "-ac", "1", "-ar", "24000", "-b:a", "64k",
                "-f", "mp3", dst_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if proc.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 0:
            return True
        logger.warning(f"ffmpeg转码失败: {proc.stderr.decode('utf-8', 'ignore')[:200]}")
        return False
    except Exception as e:
        logger.warning(f"ffmpeg转码异常: {e}")
        return False


@router.post("/send-audio/{account_id}")
async def send_audio(
    account_id: str,
    cid: str = Form(...),
    toUserId: str = Form(...),
    duration: int = Form(0),
    audio: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    发送语音消息

    流程：
    1. 校验账号归属与IM连接状态
    2. 校验音频类型与大小
    3. 尽量用 ffmpeg 转码为 mp3（无 ffmpeg 时原样上传）
    4. 上传到闲鱼CDN，拿到可访问URL
    5. 通过IM协议发送 contentType=3 的语音消息

    Args:
        account_id: 账号ID
        cid: 会话ID（不含@goofish后缀）
        toUserId: 对方用户ID
        duration: 语音时长（毫秒），由前端 MediaRecorder 计时给出
        audio: 上传的音频文件
    """
    if not await _get_owned_chat_account(account_id, current_user, db):
        return ApiResponse(success=False, message="账号不存在或无权操作")

    manager = get_im_session_manager()
    client = manager.clients.get(account_id)
    if not client or not client.is_connected:
        return ApiResponse(success=False, message="账号未连接，请先连接")

    raw_type = (audio.content_type or "").split(";")[0].strip().lower()
    if raw_type and raw_type not in _ALLOWED_AUDIO_TYPES:
        return ApiResponse(success=False, message=f"不支持的音频格式：{raw_type}")

    audio_data = await audio.read()
    if not audio_data:
        return ApiResponse(success=False, message="上传文件为空")
    if len(audio_data) > _MAX_AUDIO_SIZE:
        return ApiResponse(success=False, message="语音大小不能超过10MB")

    temp_in = None
    temp_out = None
    try:
        # 1. 落盘原始音频
        suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
        fd_in, temp_in = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd_in, "wb") as f:
            f.write(audio_data)

        # 2. 转码 mp3（失败则原样上传）
        upload_path = temp_in
        upload_ct = raw_type or "application/octet-stream"
        fd_out, temp_out = tempfile.mkstemp(suffix=".mp3")
        os.close(fd_out)
        if _transcode_to_mp3(temp_in, temp_out):
            upload_path = temp_out
            upload_ct = "audio/mpeg"

        # 3. 上传闲鱼CDN
        uploader = ImageUploader(client.cookies_str)
        async with uploader:
            cdn_url = await uploader.upload_file(
                upload_path, content_type=upload_ct, filename=f"audio_{os.path.basename(upload_path)}"
            )

        if not cdn_url:
            # 闲鱼PC网页端的上传通道（stream-upload.goofish.com）是图片通道，非图片会被
            # 图片安全审核以 PICTURE_NOT_SAFE 拒绝。实测换 floderId/appkey/scene 均无效。
            # 这里把不同失败原因翻译成人话，并带上 voiceUnsupported 标记，便于前端自动降级。
            reason = (uploader.last_error or "").strip()
            if "PICTURE_NOT_SAFE" in reason:
                message = "闲鱼网页端的上传通道只收图片，正在改用「转文字」发送"
            elif "TYPE_NOT_CONFIRMED" in reason:
                message = "闲鱼无法识别这段录音的格式（建议转成 wav/mp3 再试）"
            elif "timed out" in reason.lower() or "TimeoutError" in reason:
                message = "语音上传超时，请检查网络后重试，或改用「转文字」发送"
            elif "Cannot connect" in reason:
                message = "连不上闲鱼上传服务，请检查网络后重试，或改用「转文字」发送"
            else:
                message = "语音上传失败，请检查账号Cookie是否有效或稍后重试"
            return ApiResponse(
                success=False,
                message=message,
                data={"voiceUnsupported": True, "reason": reason[:300]},
            )

        # 4. 发送语音消息
        duration_ms = int(duration or 0)
        send_result = await client.send_audio_message(
            cid=cid,
            to_user_id=toUserId,
            audio_url=cdn_url,
            duration_ms=duration_ms,
        )
        logger.info(f"【{account_id}】发送语音消息到 {toUserId}: {cdn_url} 时长={duration_ms}ms")
        return ApiResponse(
            success=True,
            message="发送成功",
            data={
                "messageId": send_result.get("messageId", ""),
                "audioUrl": cdn_url,
                "duration": duration_ms,
            },
        )
    except Exception as e:
        # IM 安全拦截等业务错误会抛出明文原因，直接透传给前端展示
        logger.warning(f"【{account_id}】发送语音消息失败: {e}")
        return ApiResponse(success=False, message=f"发送失败：{str(e)}")
    finally:
        for p in (temp_in, temp_out):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


# ========================= 语音转文字（ASR） =========================

async def _resolve_asr_settings(db: AsyncSession, account: XYAccount) -> dict:
    """
    解析语音转文字使用的配置，优先级：

    1. 系统设置里的「语音转文字」专用配置（asr.provider / asr.api_key /
       asr.base_url / asr.model / asr.local_model）
    2. 账号 AI 回复设置里已有的接口配置（复用同一份 API Key）

    Returns:
        规范化后的配置，额外带 asr_provider / asr_local_model 两个字段
    """
    provider = "auto"
    local_model = DEFAULT_LOCAL_MODEL
    try:
        from app.services.system_setting_service import SystemSettingService

        all_settings = await SystemSettingService(db).list_settings()
        raw_provider = str(all_settings.get("asr.provider") or "").strip().lower()
        if raw_provider in ("auto", "cloud", "local"):
            provider = raw_provider
        raw_local_model = str(all_settings.get("asr.local_model") or "").strip()
        if raw_local_model in MODEL_SIZES:
            local_model = raw_local_model

        sys_key = str(all_settings.get("asr.api_key") or "").strip()
        payload = normalize_ai_settings(
            {
                "api_key": sys_key,
                "base_url": str(all_settings.get("asr.base_url") or "").strip(),
                "model_name": str(all_settings.get("asr.model") or "").strip(),
                "asr_model": str(all_settings.get("asr.model") or "").strip(),
            }
        )
        payload["asr_provider"] = provider
        payload["asr_local_model"] = local_model
        return payload
    except Exception as e:
        logger.warning(f"读取系统语音转文字配置失败，回退到账号AI配置: {e}")

    stored = (account.metadata_json or {}).get("ai_reply_settings") or {}
    payload = normalize_ai_settings(stored)
    payload["asr_provider"] = provider
    payload["asr_local_model"] = local_model
    return payload


def _is_dashscope_base(base_url: str) -> bool:
    base = (base_url or "").lower()
    return "dashscope" in base or "maas.aliyuncs.com" in base or "bailian" in base


def _guess_mime(raw_type: str, filename: str) -> str:
    ct = (raw_type or "").split(";")[0].strip().lower()
    if ct and ct.startswith("audio/"):
        return ct
    ext = os.path.splitext(filename or "")[1].lower()
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".webm": "audio/webm",
        ".amr": "audio/amr",
        ".flac": "audio/flac",
    }.get(ext, "audio/wav")


async def _asr_via_chat_completions(
    base_url: str, api_key: str, model: str, audio_bytes: bytes, mime: str
) -> str:
    """
    阿里云百炼(DashScope) OpenAI 兼容模式的语音识别：
    走 chat/completions，content 用 input_audio + base64 data URI，无需先传到OSS。
    """
    url = build_openai_url(base_url, "chat/completions")
    data_uri = f"data:{mime};base64,{base64.b64encode(audio_bytes).decode()}"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}],
            }
        ],
        "stream": False,
        "asr_options": {"enable_itn": False},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(_ASR_TIMEOUT)) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"ASR接口返回 {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    try:
        return str(body["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        raise RuntimeError(f"ASR返回结构异常: {str(body)[:200]}")


async def _asr_via_whisper(
    base_url: str, api_key: str, model: str, audio_bytes: bytes, mime: str
) -> str:
    """标准 OpenAI /audio/transcriptions 接口（Whisper 系）"""
    url = build_openai_url(base_url, "audio/transcriptions")
    ext = {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/mp4": "m4a",
        "audio/x-m4a": "m4a",
        "audio/aac": "aac",
        "audio/ogg": "ogg",
        "audio/webm": "webm",
        "audio/flac": "flac",
        "audio/amr": "amr",
    }.get(mime, "wav")
    async with httpx.AsyncClient(timeout=httpx.Timeout(_ASR_TIMEOUT)) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": model},
            files={"file": (f"audio.{ext}", audio_bytes, mime)},
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"ASR接口返回 {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    text = (body.get("text") or "").strip() if isinstance(body, dict) else ""
    if not text:
        raise RuntimeError(f"ASR返回结构异常: {str(body)[:200]}")
    return text


async def _speech_to_text_local(local_model: str, audio_bytes: bytes) -> tuple[str, str]:
    """本地离线转写（faster-whisper），不需要任何 API Key"""
    text = await local_transcribe(audio_bytes, size=local_model)
    if not text:
        raise RuntimeError("本地模型没有识别出内容")
    return text, f"本地模型 {local_model}"


async def _speech_to_text(
    ai_settings: dict, audio_bytes: bytes, mime: str
) -> tuple[str, str]:
    """
    按配置选择 ASR 通道

    - provider=local：只用本地模型（离线，不需要 Key）
    - provider=cloud：只用云端接口
    - provider=auto（默认）：有云端 Key 走云端；没有 Key 或云端失败则回退本地

    Returns:
        (转写文本, 使用的模型名)
    Raises:
        RuntimeError: 所有可用通道都失败
    """
    provider = (ai_settings.get("asr_provider") or "auto").strip().lower()
    local_model = (ai_settings.get("asr_local_model") or DEFAULT_LOCAL_MODEL).strip()
    api_key = (ai_settings.get("api_key") or "").strip()
    base_url = (ai_settings.get("base_url") or "").strip()
    custom_model = (ai_settings.get("asr_model") or "").strip()

    # 明确指定本地：不碰任何云端配置
    if provider == "local":
        if not local_asr_available():
            raise RuntimeError(
                "本地语音转文字依赖未安装，请在后端目录执行：pip install faster-whisper av==14.0.1"
            )
        return await _speech_to_text_local(local_model, audio_bytes)

    cloud_error: Exception | None = None

    # 云端通道（需要 API Key）
    if provider == "cloud" or (provider == "auto" and api_key and base_url):
        try:
            return await _speech_to_text_cloud(
                ai_settings, audio_bytes, mime, api_key, base_url, custom_model
            )
        except Exception as e:
            cloud_error = e
            if provider == "cloud":
                raise
            logger.warning(f"云端语音转文字失败，回退本地模型: {e}")
    elif provider == "cloud":
        raise RuntimeError("选择了云端转写，但没有配置语音转文字 API Key")

    # 走到这里说明：auto 模式下云端不可用/失败 → 尝试本地
    if local_asr_available():
        try:
            return await _speech_to_text_local(local_model, audio_bytes)
        except Exception as e:
            if cloud_error:
                raise RuntimeError(f"云端失败：{cloud_error}；本地失败：{e}")
            raise
    if cloud_error:
        raise cloud_error
    raise RuntimeError(
        "没有可用的语音转文字方式：请在「系统设置 → 语音转文字」选择本地模型并安装依赖，或填写云端 API Key"
    )


async def _speech_to_text_cloud(
    ai_settings: dict,
    audio_bytes: bytes,
    mime: str,
    api_key: str,
    base_url: str,
    custom_model: str,
) -> tuple[str, str]:
    """云端 ASR：按服务商类型依次尝试 chat/completions(input_audio) 与 /audio/transcriptions"""
    dashscope_first = _is_dashscope_base(base_url)
    dashscope_models = ([custom_model] if custom_model else []) + _DASHSCOPE_ASR_MODELS
    whisper_models = ([custom_model] if custom_model else []) + _WHISPER_ASR_MODELS

    attempts: list[tuple[str, list[str]]] = []
    if dashscope_first:
        attempts.append(("chat", dashscope_models))
        attempts.append(("whisper", whisper_models))
    else:
        attempts.append(("whisper", whisper_models))
        attempts.append(("chat", dashscope_models))

    # 全部失败时优先回传「首选通道」的错误，避免兜底通道的噪音掩盖真实原因
    # （例如 DashScope 密钥填错时，兜底的 /audio/transcriptions 只会报 404）
    first_error: Exception | None = None
    last_error: Exception | None = None
    for kind, models in attempts:
        for model in models:
            try:
                if kind == "chat":
                    text = await _asr_via_chat_completions(base_url, api_key, model, audio_bytes, mime)
                else:
                    text = await _asr_via_whisper(base_url, api_key, model, audio_bytes, mime)
                if text:
                    return text, model
                err = RuntimeError(f"模型 {model} 返回空文本")
            except Exception as e:
                err = e
                logger.warning(f"ASR 尝试失败 [{kind}/{model}]: {e}")
            if first_error is None:
                first_error = err
            last_error = err
    raise RuntimeError(str(first_error) if first_error else "未找到可用的语音转文字通道")


@router.post("/transcribe-audio/{account_id}")
async def transcribe_audio(
    account_id: str,
    audio: UploadFile | None = File(None),
    audioUrl: str | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    语音转文字

    复用账号在「AI回复设置」里配置好的接口与密钥，二选一提供音频：
    - audio: 前端录制的音频文件（multipart）
    - audioUrl: 已有的音频直链（如对方发来的语音消息）

    Returns:
        data.text: 转写后的文本
        data.model: 实际使用的ASR模型
    """
    account = await _get_owned_chat_account(account_id, current_user, db)
    if not account:
        return ApiResponse(success=False, message="账号不存在或无权操作")

    audio_bytes = b""
    mime = "audio/wav"
    if audio is not None:
        raw_type = (audio.content_type or "").split(";")[0].strip().lower()
        if raw_type and raw_type not in _ALLOWED_AUDIO_TYPES:
            return ApiResponse(success=False, message=f"不支持的音频格式：{raw_type}")
        audio_bytes = await audio.read()
        mime = _guess_mime(raw_type, audio.filename or "")
    elif audioUrl:
        url = str(audioUrl).strip()
        if not url.startswith(("http://", "https://")):
            return ApiResponse(success=False, message="音频地址无效")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
                resp = await client.get(url)
            if resp.status_code >= 400:
                return ApiResponse(success=False, message=f"音频下载失败（{resp.status_code}）")
            audio_bytes = resp.content
        except Exception as e:
            return ApiResponse(success=False, message=f"音频下载失败：{e}")
        mime = _guess_mime((resp.headers.get("content-type") or ""), url)
    else:
        return ApiResponse(success=False, message="缺少音频文件或音频地址")

    if not audio_bytes:
        return ApiResponse(success=False, message="音频内容为空")
    if len(audio_bytes) > _MAX_AUDIO_SIZE:
        return ApiResponse(success=False, message="音频大小不能超过10MB")

    ai_settings = await _resolve_asr_settings(db, account)
    # 只有本地模式不需要任何云端配置，其余情况做一次前置提示，避免转半天才报错
    if (ai_settings.get("asr_provider") or "auto") != "local" and not local_asr_available():
        if not (ai_settings.get("api_key") or "").strip():
            return ApiResponse(
                success=False,
                message="没有可用的语音转文字方式：请在「系统设置 → 语音转文字」选择「本地模型」并安装依赖，或填写云端 API Key",
            )
        if not (ai_settings.get("base_url") or "").strip():
            return ApiResponse(success=False, message="未配置语音转文字接口地址")

    try:
        text, model = await _speech_to_text(ai_settings, audio_bytes, mime)
    except Exception as e:
        logger.warning(f"【{account_id}】语音转文字失败: {e}")
        return ApiResponse(success=False, message=f"转写失败：{str(e)[:200]}")

    logger.info(f"【{account_id}】语音转文字成功 model={model} 长度={len(text)}")
    return ApiResponse(success=True, message="转写成功", data={"text": text, "model": model})


@router.get("/asr/status")
async def asr_status(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """查询语音转文字能力状态（本地依赖是否可用、模型是否就绪、云端是否配置）"""
    from app.services.system_setting_service import SystemSettingService

    settings = await SystemSettingService(db).list_settings()
    provider = str(settings.get("asr.provider") or "auto").strip().lower() or "auto"
    local_model = str(settings.get("asr.local_model") or DEFAULT_LOCAL_MODEL).strip()
    if local_model not in MODEL_SIZES:
        local_model = DEFAULT_LOCAL_MODEL

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "provider": provider,
            "localAvailable": local_asr_available(),
            "localError": unavailable_reason() if not local_asr_available() else "",
            "localModel": local_model,
            "localModelReady": is_model_ready(local_model),
            "modelSizes": list(MODEL_SIZES),
            "cloudConfigured": bool(str(settings.get("asr.api_key") or "").strip()),
            "installCommand": "pip install faster-whisper av==14.0.1",
        },
    )


@router.post("/asr/warmup")
async def asr_warmup(
    model: str | None = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """提前下载并加载本地模型（首次会比较慢，之后就秒开）"""
    from app.services.system_setting_service import SystemSettingService

    settings = await SystemSettingService(db).list_settings()
    size = (model or str(settings.get("asr.local_model") or "") or DEFAULT_LOCAL_MODEL).strip()
    if size not in MODEL_SIZES:
        return ApiResponse(success=False, message=f"不支持的模型：{size}")

    try:
        await local_warmup(size)
    except Exception as e:
        logger.warning(f"本地语音模型预热失败: {e}")
        return ApiResponse(success=False, message=f"模型下载失败：{str(e)[:200]}")

    return ApiResponse(success=True, message=f"模型 {size} 已就绪", data={"model": size})
