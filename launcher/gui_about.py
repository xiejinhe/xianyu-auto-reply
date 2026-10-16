"""
GUI关于页面模块

功能：
1. 作为左侧导航的"关于"菜单对应内容页
2. 显示系统版本信息

二开版本已移除原作者的远程二维码（微信群/QQ群/公众号/Telegram/赞赏码）拉取逻辑，
本页不再发起任何外部网络请求。
"""
import tkinter as tk

from launcher.gui_theme import COLORS
from launcher.version import CURRENT_VERSION


def render_about_page(app):
    """
    在右侧内容区渲染"关于"页面

    仅显示系统名称与版本号，不请求任何远程资源。
    Args:
        app: LauncherApp实例
    """
    content = app._content_frame

    # 滚动容器
    canvas = tk.Canvas(content, bg=COLORS["card_bg"], highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    inner = tk.Frame(canvas, bg=COLORS["card_bg"])
    canvas.create_window((0, 0), window=inner, anchor=tk.NW)
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    # 标题
    tk.Label(inner, text="关于", font=("微软雅黑", 14, "bold"),
             fg=COLORS["text"], bg=COLORS["card_bg"]).pack(
        anchor=tk.W, padx=20, pady=(16, 4))

    # 系统名称和版本
    tk.Label(inner, text="闲鱼自动回复管理系统",
             font=("微软雅黑", 12),
             fg=COLORS["text"], bg=COLORS["card_bg"]).pack(
        anchor=tk.W, padx=20)
    tk.Label(inner, text=f"当前版本: v{CURRENT_VERSION}",
             font=("微软雅黑", 10),
             fg=COLORS["text_secondary"], bg=COLORS["card_bg"]).pack(
        anchor=tk.W, padx=20, pady=(0, 8))

    # 分割线
    tk.Frame(inner, height=1, bg=COLORS["border"]).pack(
        fill=tk.X, padx=20, pady=8)

    tk.Label(inner, text="本项目为本地自部署版本，不依赖任何外部服务器。",
             font=("微软雅黑", 10),
             fg=COLORS["text_secondary"], bg=COLORS["card_bg"]).pack(
        anchor=tk.W, padx=20, pady=(0, 4))
    tk.Label(inner, text="运行过程中产生的账号、消息与商品数据均保存在你自己的数据库中。",
             font=("微软雅黑", 10),
             fg=COLORS["text_secondary"], bg=COLORS["card_bg"]).pack(
        anchor=tk.W, padx=20, pady=(0, 16))
