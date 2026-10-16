#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包「闲鱼自动回复系统」Windows 一键包（Docker 版）

用法（在 Mac 上执行）：
    python3 windows-one-click/build_package.py

产出：
    release-win/闲鱼自动回复系统-v<版本>.zip

设计要点：
1. 排除 .venv / .git / node_modules / logs / 用户数据 等，1.9G 压到约 30M
2. 文件名用 UTF-8（并设 zip UTF-8 标志位），Win10+ 解压不乱码
3. .bat / .txt 内容转成 GBK 字节，配合脚本里的 chcp 936，中文显示正常
4. 打包后自动校验关键文件是否齐全
"""

from __future__ import annotations

import os
import re
import sys
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- 路径

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
OUT_DIR = PROJECT_ROOT / "release-win"
ZIP_TOP = "xianyu-auto-reply"  # zip 里的顶层文件夹名：用英文！
# 说明：Docker Compose 默认拿「所在文件夹名」当项目名，中文名会直接报错
#       "project name must not be empty"。所以这里必须用英文目录名，
#       同时 .env 里再固定 COMPOSE_PROJECT_NAME 做双保险。

# ---------------------------------------------------------------- 排除规则

# 任何层级出现即排除的目录名
EXCLUDE_DIR_NAMES = {
    ".git", ".workbuddy", ".vscode", ".idea", ".claude", ".kiro",
    ".venv", "venv", "env", "__pycache__", "node_modules",
    "browser_data", "build_output", "release", "release-win",
    ".pytest_cache", ".mypy_cache", "temp", "tmp",
    "sound-preview",
}

# 仅顶层排除的目录名
EXCLUDE_TOP_DIRS = {
    "xianyu_auto_reply",   # 旧版单体实现，含本地 MySQL/Redis 数据（117M+），不能外发
    "xianyu-mobile",       # 安卓端，与本次分发无关
    "promotion",           # 推广站子项目
    "backups",             # 本地备份
    "data",                # 本地配置目录；Docker 版由代码默认值兜底
    "windows-one-click",   # 本目录，只取其中要分发的文件
}

# 任何层级匹配即排除的文件名 / 前缀
EXCLUDE_FILE_EXACT = {
    ".DS_Store", "Thumbs.db", ".gitignore", ".gitattributes",
    ".env", ".env.example", ".env.local",           # 本地配置，用 dist-env 替代
    "EXE打包构建.bat", "离线依赖打包.bat",            # 旧的打包方式，避免朋友误用
    "docker-compose.dev.yml",
    "docker-compose.deploy.yml",                     # 会拉到第三方镜像，必须排除
    "deploy.sh.bak",
    # 二开版本已移除原作者的云服务器推广/广告文件
    "codebase-diagram.html",
    "deploy_remote.sh",
}

# 仅当位于项目根目录时才排除（子目录里的同名文件要保留）
EXCLUDE_FILE_ONLY_TOP = {
    "package.json",          # 根目录这个是无关的 claude 依赖
    "package-lock.json",
}

EXCLUDE_FILE_SUFFIX = (".pyc", ".pyo", ".log", ".bak", ".url", ".sh", ".md", ".groovy", ".swp")

# 要打进包、且内容需要转成 GBK 的文件后缀
GBK_SUFFIX = (".bat", ".txt", ".ini", ".cmd")

# 从 windows-one-click/ 映射到 zip 根目录的文件
DIST_FILES = {
    "1-环境检测.bat": "1-环境检测.bat",
    "2-一键启动.bat": "2-一键启动.bat",
    "3-一键停止.bat": "3-一键停止.bat",
    "4-查看状态.bat": "4-查看状态.bat",
    "5-重启服务.bat": "5-重启服务.bat",
    "使用说明.txt": "使用说明.txt",
    "tools/docker-mirror.ps1": "tools/docker-mirror.ps1",
    "dist-env.txt": ".env",
}

# 只改「打进包的副本」，不改动源文件：
#   key   = 相对项目根的 posix 路径
#   value = [(原文本, 新文本), ...]
# 目的：去掉分发包里对第三方（原作者）服务器和私有镜像仓库的依赖。
CONTENT_PATCHES: dict[str, list[tuple[str, str]]] = {
    "docker-compose.yml": [
        # 注：MySQL/Redis 镜像与 IMAGE_REGISTRY 已在源码里直接改为官方镜像 / 必填，
        # 这里不再重复打补丁。
        # 去掉第三方服务地址兜底，避免本机部署时向外请求
        (
            "${CARD_DOCK_BASE_URL:-}",
            "${CARD_DOCK_BASE_URL:-}",
        ),
        (
            "${REMOTE_OFFICIAL_BASE_URL:-}",
            "${REMOTE_OFFICIAL_BASE_URL:-}",
        ),
    ],
    "backend-web/app/core/config.py": [
        (
            'default="",',
            'default="",',
        ),
        (
            'default="",',
            'default="",',
        ),
    ],
}

# 打包后必须存在的关键路径
REQUIRED_IN_ZIP = [
    "docker-compose.yml",
    ".dockerignore",
    ".env",
    "1-环境检测.bat",
    "2-一键启动.bat",
    "3-一键停止.bat",
    "4-查看状态.bat",
    "5-重启服务.bat",
    "使用说明.txt",
    "tools/docker-mirror.ps1",
    "backend-web/Dockerfile",
    "websocket/Dockerfile",
    "scheduler/Dockerfile",
    "docker/frontend/Dockerfile",
    "docker/frontend/nginx.conf",
    "common/db/init_database.py",
    "launcher/__init__.py",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/public/sounds/pop.wav",
]


def should_exclude(rel: Path) -> bool:
    """rel 是相对于 PROJECT_ROOT 的路径"""
    parts = rel.parts

    if parts and parts[0] in EXCLUDE_TOP_DIRS:
        return True

    for part in parts[:-1]:  # 各级目录
        if part in EXCLUDE_DIR_NAMES:
            return True

    name = rel.name
    if name in EXCLUDE_FILE_EXACT:
        return True
    if len(parts) == 1 and name in EXCLUDE_FILE_ONLY_TOP:
        return True
    if name.lower().endswith(EXCLUDE_FILE_SUFFIX):
        # .md 例外：README 不要，但要保留 frontend 下的必要 md？—— 直接全排除
        return True
    if name.startswith("architecture-"):
        return True

    # 用户上传的静态文件
    sp = parts
    for i in range(len(sp) - 1):
        if sp[i] == "static" and sp[i + 1] == "uploads":
            return True
    if "logs" in sp[:-1]:
        return True

    return False


def read_text_as_gbk(path: Path) -> bytes:
    """读 UTF-8 源文件，转成 GBK 字节；无法编码时报出具体位置"""
    text = path.read_text(encoding="utf-8")
    try:
        return text.encode("gbk")
    except UnicodeEncodeError as exc:
        bad = text[exc.start : exc.end]
        raise SystemExit(
            f"\n[错误] 文件 {path} 含 GBK 无法表示的字符：{bad!r}\n"
            f"       位置：第 {text[: exc.start].count(chr(10)) + 1} 行\n"
            f"       请把它改成普通中文标点后重试。\n"
        ) from exc


def get_version() -> str:
    """从 launcher/version.py 读版本号，失败则用日期兜底"""
    vf = PROJECT_ROOT / "launcher" / "version.py"
    try:
        m = re.search(r'CURRENT_VERSION\s*=\s*["\']([^"\']+)["\']', vf.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    except Exception:
        pass
    return "1.0.0"


def main() -> int:
    if not (PROJECT_ROOT / "docker-compose.yml").exists():
        raise SystemExit(f"[错误] 没找到 docker-compose.yml，项目根目录识别错误：{PROJECT_ROOT}")

    if not OUT_DIR.exists():
        OUT_DIR.mkdir(parents=True)
    version = get_version()
    zip_path = OUT_DIR / f"闲鱼自动回复系统-v{version}-Windows一键包.zip"
    if zip_path.exists():
        zip_path.unlink()

    included = 0
    skipped = 0
    total_bytes = 0
    gbk_files: list[str] = []
    patched: list[str] = []

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # ---- 1. 项目文件 ----
        for root, dirs, files in os.walk(PROJECT_ROOT):
            root_path = Path(root)

            # 就地裁剪，避免深入无关目录
            dirs[:] = [
                d for d in dirs
                if not should_exclude((root_path / d).relative_to(PROJECT_ROOT))
            ]

            for fname in files:
                full = root_path / fname
                rel = full.relative_to(PROJECT_ROOT)

                if should_exclude(rel):
                    skipped += 1
                    continue

                arcname = f"{ZIP_TOP}/{rel.as_posix()}"
                rel_posix = rel.as_posix()
                try:
                    if rel_posix in CONTENT_PATCHES:
                        text = full.read_text(encoding="utf-8")
                        for old, new in CONTENT_PATCHES[rel_posix]:
                            if old not in text:
                                print(f"[警告] 替换未命中：{rel_posix}  ->  {old[:70]}")
                                continue
                            text = text.replace(old, new)
                        zf.writestr(arcname, text.encode("utf-8"))
                        patched.append(rel_posix)
                    elif full.suffix.lower() in GBK_SUFFIX:
                        data = read_text_as_gbk(full)
                        zf.writestr(arcname, data)
                        gbk_files.append(rel_posix)
                    else:
                        zf.write(full, arcname)
                except SystemExit:
                    raise
                except Exception as exc:
                    print(f"[警告] 跳过 {rel}：{exc}")
                    skipped += 1
                    continue

                included += 1
                total_bytes += full.stat().st_size

        # ---- 2. 一键包文件 ----
        for src_rel, dst_rel in DIST_FILES.items():
            src = HERE / src_rel
            if not src.exists():
                raise SystemExit(f"[错误] 缺少一键包文件：{src}")

            arcname = f"{ZIP_TOP}/{dst_rel}"
            if src.suffix.lower() in GBK_SUFFIX:
                zf.writestr(arcname, read_text_as_gbk(src))
                gbk_files.append(dst_rel)
            else:
                zf.write(src, arcname)
            included += 1
            total_bytes += src.stat().st_size

        # ---- 3. 校验 ----
        names = set(zf.namelist())
        missing = [p for p in REQUIRED_IN_ZIP if f"{ZIP_TOP}/{p}" not in names]
        unpatched = [k for k in CONTENT_PATCHES if k not in patched]

        # 品牌残留扫描：只查会真正被执行、或会向外发起请求的文件
        brand_keys = ("xiejinhe", "zhinian-software", "xy-update")
        brand_hits: list[str] = []
        for rel_posix in ("docker-compose.yml", "backend-web/app/core/config.py", ".env"):
            arc = f"{ZIP_TOP}/{rel_posix}"
            if arc not in names:
                continue
            raw = zf.read(arc)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gbk", errors="replace")
            if any(k in text for k in brand_keys):
                brand_hits.append(rel_posix)

    # ---------------------------------------------------------------- 报告
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print()
    print("=" * 62)
    print("  打包完成")
    print("=" * 62)
    print(f"  版本号    : v{version}")
    print(f"  输出文件  : {zip_path}")
    print(f"  压缩包大小: {size_mb:.1f} MB")
    print(f"  已打包    : {included} 个文件（原始约 {total_bytes / 1024 / 1024:.1f} MB）")
    print(f"  已排除    : {skipped} 个文件")
    print(f"  GBK 转码  : {len(gbk_files)} 个（bat / txt）")
    print(f"  内容改写  : {len(patched)} 个（去掉第三方镜像与域名依赖）")
    print()

    problems = False

    if missing:
        print("[校验失败] 以下关键文件不在压缩包里：")
        for p in missing:
            print(f"  - {p}")
        problems = True

    if unpatched:
        print("[校验失败] 以下文件的替换规则没有生效：")
        for p in unpatched:
            print(f"  - {p}")
        problems = True

    if brand_hits:
        print("[校验失败] 以下文件仍含第三方品牌信息：")
        for p in brand_hits:
            print(f"  - {p}")
        problems = True

    if problems:
        return 1

    print("[校验通过] 关键文件齐全、无第三方依赖残留")
    print("            compose 配置、4 个 Dockerfile、6 个脚本、音效资源 全部就位")
    print()
    print("下一步：把这个 zip 发给朋友，并让他先看压缩包里的「使用说明.txt」。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
