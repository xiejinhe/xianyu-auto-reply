"""
Backend-Web服务配置模块

功能：
1. 继承common配置基类
2. 添加Backend-Web服务特定配置
3. 从.env文件读取配置
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import List

from pydantic import Field, computed_field

from common.core.config import BaseConfig


class BackendWebConfig(BaseConfig):
    """
    Backend-Web服务配置类
    
    包含Backend-Web服务特定配置：
    - 服务端口
    - JWT配置
    - CORS配置
    - 服务间通信URL
    """

    # 服务配置
    project_name: str = Field(default="Xianyu Backend-Web Service")
    version: str = Field(default="0.1.0")
    api_v1_prefix: str = Field(default="/api/v1")
    service_port: int = Field(default=8089, alias="BACKEND_WEB_PORT")
    
    # JWT配置
    # 注意：jwt_secret_key 由数据库统一托管（启动时 ensure_jwt_secret_key 自动生成/加载并写回此实例），
    # 此处 default 仅作占位，运行期会被数据库中的值覆盖。
    jwt_secret_key: str = Field(default="change-me", repr=False)
    jwt_algorithm: str = Field(default="HS256")
    access_token_expire_minutes: int = Field(default=1440)
    refresh_token_expire_minutes: int = Field(default=60 * 24 * 7)
    
    # CORS配置
    cors_origins_raw: str = Field(default="*", alias="CORS_ORIGINS")
    
    # 服务间通信URL
    websocket_service_url: str = Field(
        default="http://127.0.0.1:8090",
        alias="WEBSOCKET_SERVICE_URL"
    )
    scheduler_service_url: str = Field(
        default="http://127.0.0.1:8091",
        alias="SCHEDULER_SERVICE_URL"
    )
    
    # 静态文件目录（共享）
    static_dir: str = Field(default="static", alias="STATIC_DIR")
    
    frontend_public_url: str = Field(
        default="",
        alias="FRONTEND_PUBLIC_URL"
    )

    # Backend-Web服务的公网访问地址（用于生成文件URL）
    backend_web_public_url: str = Field(
        default="http://127.0.0.1:8089",
        alias="BACKEND_WEB_PUBLIC_URL"
    )

    # 启动时是否自动启动Goofish定时采集任务
    auto_start_crawl_jobs: bool = Field(default=True, alias="AUTO_START_CRAWL_JOBS")

    # 卡券对接（分销卡券）上游服务基址：用于「分销卡券」页面通过上游卡券系统提货
    # 二开版本默认留空：不再默认指向任何第三方/上游服务，需要使用该功能时自行配置。
    card_dock_base_url: str = Field(
        default="",
        alias="CARD_DOCK_BASE_URL",
    )

    # 外部 API 密钥管理服务：用于个人设置「对接卡密秘钥」一键创建密钥
    # 复用 CARD_DOCK_BASE_URL 作为基址，仅 key 通过环境变量单独配置（禁止写死）
    external_api_key: str = Field(
        default="",
        alias="EXTERNAL_API_KEY",
    )

    # 远程内容拉取基址：仪表盘广告、系统公告等内容的「远程」来源
    # 二开版本默认留空：不再从任何外部服务器拉取可展示内容，避免被远程注入。
    remote_official_base_url: str = Field(
        default="",
        alias="REMOTE_OFFICIAL_BASE_URL",
    )

    # 下面三个开关控制「从外部服务器拉取广告/公告/弹窗并展示到本系统」。
    # 二开版本默认全部关闭：外部来源内容会直接展示给使用者，且远程拉取时不做证书校验，
    # 存在内容注入与钓鱼风险。需要自建内容分发时再单独打开并配置基址。
    enable_remote_ads: bool = Field(
        default=False,
        alias="ENABLE_REMOTE_ADS",
    )

    enable_remote_announcements: bool = Field(
        default=False,
        alias="ENABLE_REMOTE_ANNOUNCEMENTS",
    )

    enable_remote_popup_announcements: bool = Field(
        default=False,
        alias="ENABLE_REMOTE_POPUP_ANNOUNCEMENTS",
    )

    @computed_field(return_type=list[str])
    @property
    def cors_origins(self) -> List[str]:
        """解析CORS配置"""
        raw_value = self.cors_origins_raw.strip()
        if not raw_value:
            return ["*"]
        if raw_value == "*":
            return ["*"]
        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, list):
                return [str(origin).strip() for origin in parsed if str(origin).strip()]
        except json.JSONDecodeError:
            pass
        return [origin.strip() for origin in raw_value.split(",") if origin.strip()]


@lru_cache
def get_settings() -> BackendWebConfig:
    """返回缓存的配置实例"""
    return BackendWebConfig()
