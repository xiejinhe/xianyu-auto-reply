"""
版本检测服务

功能：
1. 获取系统当前版本号（复用桌面启动器的版本号常量）
2. 向远程更新服务器请求 version.json，对比版本号判断是否有新版本
3. 返回统一的检测结果字典供路由层包装为 ApiResponse

说明：
- 更新服务器地址与格式跟桌面启动器（launcher/updater.py）保持一致，
  详见 data/update_config.json 与 /version.json。
- 所有外部请求使用 httpx 异步客户端，超时 10 秒。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from loguru import logger


# 默认更新服务器地址（与 launcher/updater.py 保持一致）
# 二开版本默认留空：不再默认连接原作者(xiejinhe)的更新服务器。
# 配置了 data/update_config.json 的 update_url 时才走 version.json 方式，
# 否则优先用 github_repo 走 GitHub Releases。
_DEFAULT_UPDATE_URL = ""
# GitHub Releases API 基础地址（配置了 github_repo 时作为更新来源）
_GITHUB_API_BASE = "https://api.github.com"
# 默认 GitHub 仓库：未配置 github_repo 时使用（Docker 镜像不打包 data/ 目录，
# 仅靠配置文件会失效，故在此兜底）
_DEFAULT_GITHUB_REPO = "xiejinhe/xianyu-auto-reply"
# 远程 version.json 请求超时时间（秒）
_REMOTE_TIMEOUT_SECONDS = 10
# HTTP User-Agent，便于服务端识别来源
_USER_AGENT = "XianyuAutoReply-WebUpdater"


def get_current_version() -> str:
    """
    获取系统当前版本号

    优先复用桌面启动器的版本号常量（launcher.version.CURRENT_VERSION），
    以保证 Windows 桌面版与 Web 端版本号一致；若导入失败则尝试从
    data/version.txt 读取，最后返回空字符串由调用方决定如何向用户提示。

    Returns:
        当前版本号字符串（如 "1.0.3"），失败时返回空字符串
    """
    # 方式1：尝试从 launcher 模块读取（开发模式或完整打包时可用）
    try:
        from launcher.version import CURRENT_VERSION  # type: ignore
        version = str(CURRENT_VERSION or "").strip()
        if version:
            return version
    except Exception:
        pass

    # 方式2：从 data/version.txt 文件读取（打包后独立运行时可用）
    try:
        version_file = Path.cwd() / "data" / "version.txt"
        if version_file.exists():
            version = version_file.read_text(encoding="utf-8").strip()
            if version:
                return version
    except Exception as exc:
        logger.warning(f"从 data/version.txt 读取版本号失败: {exc}")

    # 方式3：从项目根目录的 version.txt 读取
    try:
        version_file = Path.cwd() / "version.txt"
        if version_file.exists():
            version = version_file.read_text(encoding="utf-8").strip()
            if version:
                return version
    except Exception as exc:
        logger.warning(f"从 version.txt 读取版本号失败: {exc}")

    return ""


def _load_update_config() -> dict[str, Any]:
    """
    读取更新配置 data/update_config.json

    依次尝试以下路径，取第一个存在的文件：
    1. 当前工作目录下的 data/（Docker 环境 WORKDIR=/app）
    2. 上级目录的 data/（本地开发时 cwd 为 backend-web，项目根在其上级）

    Returns:
        配置字典，文件不存在或解析失败时返回空字典
    """
    cwd = Path.cwd()
    candidates = (
        cwd / "data" / "update_config.json",
        cwd.parent / "data" / "update_config.json",
    )
    for config_path in candidates:
        try:
            if config_path.exists():
                data = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            logger.warning(f"读取更新配置失败（{config_path}）: {exc}")
    return {}


def _get_update_url() -> str:
    """
    获取更新服务器基础 URL

    读取顺序：
    1. data/update_config.json 中的 update_url 字段
    2. 默认地址 _DEFAULT_UPDATE_URL

    Returns:
        基础 URL（不含尾部斜杠）
    """
    url = str(_load_update_config().get("update_url", "") or "").strip().rstrip("/")
    return url or _DEFAULT_UPDATE_URL


def _get_github_repo() -> str:
    """
    获取用于检查更新的 GitHub 仓库，格式为 "owner/repo"

    优先级：
    1. data/update_config.json 中显式配置的 github_repo（配置成空字符串可关闭 GitHub 方式）
    2. 默认仓库 _DEFAULT_GITHUB_REPO

    返回非空时走 GitHub Releases API，否则沿用 version.json 方式。

    Returns:
        仓库标识（如 "xiejinhe/xianyu-auto-reply"）
    """
    config = _load_update_config()
    if "github_repo" in config:
        return str(config.get("github_repo", "") or "").strip().strip("/")
    return _DEFAULT_GITHUB_REPO


def _compare_versions(local: str, remote: str) -> bool:
    """
    比较版本号，判断远程版本是否比本地新

    规则：按点分割后逐段比较整数值，忽略非数字前缀（如开头的 "v"）。

    Args:
        local: 本地版本号
        remote: 远程版本号

    Returns:
        True 表示远程更新需要升级；False 表示本地已是最新或无法比较
    """
    def _normalize(ver: str) -> list[int]:
        raw = (ver or "").strip().lstrip("vV")
        parts: list[int] = []
        for seg in raw.split("."):
            try:
                parts.append(int(seg))
            except ValueError:
                # 非数字段视为 0，避免抛异常
                parts.append(0)
        return parts

    try:
        local_parts = _normalize(local)
        remote_parts = _normalize(remote)
        # 补齐长度，短的补 0
        max_len = max(len(local_parts), len(remote_parts))
        local_parts += [0] * (max_len - len(local_parts))
        remote_parts += [0] * (max_len - len(remote_parts))
        return remote_parts > local_parts
    except Exception:
        return False


async def check_update() -> dict[str, Any]:
    """
    检查是否有新版本可用

    更新来源二选一（见 data/update_config.json）：
    1. 配置了 github_repo：查询该仓库的最新 Release
    2. 否则：请求 ``{update_url}/version.json``

    与本地版本号比较后返回统一结构。网络错误、解析错误等情况
    由调用方转换为 ApiResponse 的 success=False 返回给前端。

    Returns:
        字典：
          - has_update: bool 是否有新版本
          - current_version: str 当前版本号
          - remote_version: str 远程版本号（失败时为空）
          - description: str 更新说明
          - filename: str 下载文件名
          - download_url: str 完整下载地址
          - error: str 错误信息（正常时为空）
    """
    current_version = get_current_version()
    result: dict[str, Any] = {
        "has_update": False,
        "current_version": current_version,
        "remote_version": "",
        "description": "",
        "filename": "",
        "download_url": "",
        "error": "",
    }

    if not current_version:
        result["error"] = "无法读取当前版本号"
        return result

    repo = _get_github_repo()
    if repo:
        return await _check_from_github(repo, current_version, result)
    return await _check_from_version_json(_get_update_url(), current_version, result)


async def _check_from_version_json(
    update_url: str, current_version: str, result: dict[str, Any]
) -> dict[str, Any]:
    """通过远程 version.json 检查更新"""
    if not update_url:
        result["error"] = "未配置更新服务器，已跳过在线更新"
        return result
    version_url = f"{update_url}/version.json"

    try:
        async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT_SECONDS) as client:
            response = await client.get(
                version_url,
                headers={"User-Agent": _USER_AGENT},
            )
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        result["error"] = "连接更新服务器超时，请稍后重试"
        return result
    except httpx.HTTPStatusError as exc:
        result["error"] = f"更新服务器返回错误状态码: {exc.response.status_code}"
        return result
    except httpx.HTTPError as exc:
        result["error"] = f"无法连接更新服务器: {exc}"
        return result
    except json.JSONDecodeError:
        result["error"] = "更新服务器返回的数据格式无效"
        return result
    except Exception as exc:
        logger.exception("检查更新失败")
        result["error"] = f"检查更新失败: {exc}"
        return result

    remote_version = str(data.get("version", "") or "").strip()
    if not remote_version:
        result["error"] = "更新服务器未返回版本号"
        return result

    filename = str(data.get("filename", "") or "").strip()
    result["remote_version"] = remote_version
    result["description"] = str(data.get("description", "") or "").strip() or "无更新说明"
    result["filename"] = filename
    result["download_url"] = f"{update_url}/{filename}" if filename else ""
    result["has_update"] = _compare_versions(current_version, remote_version)

    return result


async def _check_from_github(
    repo: str, current_version: str, result: dict[str, Any]
) -> dict[str, Any]:
    """
    通过 GitHub Releases API 检查更新

    取仓库的最新 Release：tag_name 作为版本号，body 作为更新说明，
    第一个附件作为下载项；没有附件时下载地址回退到 Release 页面。

    Args:
        repo: 仓库标识，格式 "owner/repo"
        current_version: 本地版本号
        result: 结果字典骨架，本函数只填充字段并返回

    Returns:
        填充后的结果字典
    """
    url = f"{_GITHUB_API_BASE}/repos/{repo}/releases/latest"

    try:
        async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT_SECONDS) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "application/vnd.github+json",
                },
            )
            # 仓库从未发布过 Release 时 GitHub 返回 404。这不是故障——只是
            # 「没有已发布的更新版本」，按无更新处理，避免给用户弹一个吓人的
            # 红色错误。本项目用 git 推送到 main 分发，本来就不会有 Release。
            # （代价：github_repo 配错写错时也会走这条分支，表现为「无更新」
            # 而非报错，属于可接受的权衡。）
            if response.status_code == 404:
                result["description"] = f"仓库 {repo} 未发布 Release，当前无可用更新"
                result["has_update"] = False
                return result
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        result["error"] = "连接 GitHub 超时，请稍后重试"
        return result
    except httpx.HTTPStatusError as exc:
        result["error"] = f"GitHub 返回错误状态码: {exc.response.status_code}"
        return result
    except httpx.HTTPError as exc:
        result["error"] = f"无法连接 GitHub: {exc}"
        return result
    except json.JSONDecodeError:
        result["error"] = "GitHub 返回的数据格式无效"
        return result
    except Exception as exc:
        logger.exception("通过 GitHub 检查更新失败")
        result["error"] = f"检查更新失败: {exc}"
        return result

    tag_name = str(data.get("tag_name", "") or "").strip()
    if not tag_name:
        result["error"] = "GitHub Release 未返回版本号"
        return result

    assets = data.get("assets") or []
    asset = assets[0] if isinstance(assets, list) and assets else {}
    result["remote_version"] = tag_name
    result["description"] = str(data.get("body", "") or "").strip() or "无更新说明"
    result["filename"] = str(asset.get("name", "") or "").strip()
    result["download_url"] = (
        str(asset.get("browser_download_url", "") or "").strip()
        or str(data.get("html_url", "") or "").strip()
    )
    result["has_update"] = _compare_versions(current_version, tag_name)

    return result
