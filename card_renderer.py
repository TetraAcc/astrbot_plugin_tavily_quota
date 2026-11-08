"""基于 Pillow 的高颜值 API 额度与余额状态看板渲染器。

纯 Python 实现，零外部浏览器/无头驱动依赖，跨平台字体优雅回退。
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


# ──────────────────────────────────────────────
#  视觉规范与主题色定义
# ──────────────────────────────────────────────

CANVAS_WIDTH = 760
CARD_PAD_X = 28
PAGE_PAD_Y = 24
HEADER_HEIGHT = 104
ROW_HEIGHT = 88
FOOTER_HEIGHT = 44

# 暗色高质感配色 (Dark Mode Slate)
COLOR_BG = (15, 23, 42)          # #0F172A
COLOR_CARD = (30, 41, 59)        # #1E293B
COLOR_CARD_BORDER = (51, 65, 85) # #334155
COLOR_DIVIDER = (40, 53, 72)     # #283548
COLOR_TEXT_PRIMARY = (248, 250, 252) # #F8FAFC
COLOR_TEXT_MUTED = (148, 163, 184)   # #94A3B8
COLOR_PROGRESS_BG = (51, 65, 85)     # #334155

# 状态指示灯颜色
COLOR_STATUS_OK = (34, 197, 94)      # 绿色 #22C55E
COLOR_STATUS_WARN = (234, 179, 8)    # 黄色 #EAB308
COLOR_STATUS_DANGER = (239, 68, 68)  # 红色 #EF4444

# 服务品牌色
SERVICE_COLORS: dict[str, tuple[int, int, int]] = {
    "tavily": (56, 189, 248),      # 青蓝 #38BDF8
    "openrouter": (168, 85, 247),  # 炫紫 #A855F7
    "deepseek": (59, 130, 246),    # 科技蓝 #3B82F6
    "github": (226, 232, 240),      # 银白 #E2E8F0
    "siliconflow": (16, 185, 129),  # 硅基薄荷绿 #10B981
    "moonshot": (245, 158, 11),     # 月之暗面暖橙/金黄 #F59E0B
    "zhipu": (99, 102, 241),        # 智谱靛蓝 #6366F1
    "serper": (236, 72, 153),       # Serper 玫红 #EC4899
}

SERVICE_LABELS: dict[str, str] = {
    "tavily": "Tavily Search",
    "openrouter": "OpenRouter AI",
    "deepseek": "DeepSeek API",
    "github": "GitHub Core API",
    "siliconflow": "SiliconFlow (硅基流动)",
    "moonshot": "Moonshot AI (Kimi)",
    "zhipu": "Zhipu AI (智谱/GLM)",
    "serper": "Serper (Google Search)",
}


@dataclass
class ServiceCardData:
    service: str
    key_masked: str
    status: str            # "ok" | "warn" | "danger" | "error"
    primary_metric: str    # 如 "¥12.50" 或 "950 / 1,000 次"
    primary_label: str     # 如 "可用余额" 或 "剩余配额"
    details: list[str]     # 辅助明细列表
    progress_ratio: float | None = None  # 0.0 ~ 1.0 用于绘制进度条


# ──────────────────────────────────────────────
#  字体安全回退
# ──────────────────────────────────────────────

_ASSETS_FONT = Path(__file__).resolve().parent / "assets" / "font.ttf"

@lru_cache(maxsize=32)
def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """按平台优先级自动探查中文字体，最终回退到默认位图字体。"""
    # 1. 优先使用插件内置自带字体（100% 确保 Linux / Steam Deck / Windows / Docker 跨平台中文正常渲染）
    if _ASSETS_FONT.exists():
        try:
            return ImageFont.truetype(str(_ASSETS_FONT), size)
        except Exception:
            pass

    # 2. 探查同级 parser 插件内自带的中文字体
    try:
        sibling_font = Path(__file__).resolve().parent.parent / "astrbot_plugin_parser" / "core" / "resources" / "HYSongYunLangHeiW-1.ttf"
        if sibling_font.exists():
            return ImageFont.truetype(str(sibling_font), size)
    except Exception:
        pass

    if bold:
        candidates = [
            ("C:/Windows/Fonts/msyhbd.ttc", 0),
            ("C:/Windows/Fonts/msyh.ttc", 0),
            ("C:/Windows/Fonts/simhei.ttf", 0),
            ("/System/Library/Fonts/PingFang.ttc", 1),
            ("/System/Library/Fonts/Hiragino Sans GB.ttc", 1),
            ("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc", 0),
            ("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.otf", 0),
            ("/usr/share/fonts/noto/NotoSansCJK-Bold.ttc", 0),
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.otf", 0),
            ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
            ("/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc", 0),
            ("/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc", 0),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
        ]
    else:
        candidates = [
            ("C:/Windows/Fonts/msyh.ttc", 0),
            ("C:/Windows/Fonts/simsun.ttc", 0),
            ("/System/Library/Fonts/PingFang.ttc", 0),
            ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
            ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", 0),
            ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.otf", 0),
            ("/usr/share/fonts/noto/NotoSansCJK-Regular.ttc", 0),
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf", 0),
            ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
            ("/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc", 0),
            ("/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc", 0),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
        ]

    for path, index in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=index)
            except Exception:
                continue

    # 3. 尝试通过字体名称加载
    for name in ("Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "PingFang SC"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass

    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


# ──────────────────────────────────────────────
#  卡片渲染核心逻辑
# ──────────────────────────────────────────────

def render_quota_dashboard(items: list[ServiceCardData], title: str = "API 配额与余额看板") -> str:
    """渲染多服务看板并输出临时 PNG 图片路径。"""
    if not items:
        items = [
            ServiceCardData(
                service="none",
                key_masked="未配置凭据",
                status="warn",
                primary_metric="无数据",
                primary_label="状态",
                details=["请在 AstrBot WebUI 插件配置页填写相关 API Key / Token"],
            )
        ]

    # 动态计算高度
    content_height = sum(
        ROW_HEIGHT + (24 if item.progress_ratio is not None else 0) + (len(item.details) > 2) * 16
        for item in items
    )
    total_height = PAGE_PAD_Y * 2 + HEADER_HEIGHT + content_height + FOOTER_HEIGHT

    # 创建画布
    image = Image.new("RGB", (CANVAS_WIDTH, total_height), COLOR_BG)
    draw = ImageDraw.Draw(image)

    # 1. 绘制顶栏
    font_title = _get_font(26, bold=True)
    font_sub = _get_font(13, bold=False)
    draw.text((CARD_PAD_X, PAGE_PAD_Y + 12), title, font=font_title, fill=COLOR_TEXT_PRIMARY)
    cur_time = time.strftime("%Y-%m-%d %H:%M:%S")
    draw.text((CARD_PAD_X, PAGE_PAD_Y + 54), f"监控更新时间: {cur_time} · 凭据多源自动隔离", font=font_sub, fill=COLOR_TEXT_MUTED)

    # 顶栏装饰渐变线条
    draw.line(
        [(CARD_PAD_X, PAGE_PAD_Y + 84), (CANVAS_WIDTH - CARD_PAD_X, PAGE_PAD_Y + 84)],
        fill=COLOR_CARD_BORDER,
        width=1,
    )

    # 2. 绘制各服务卡片条目
    curr_y = PAGE_PAD_Y + HEADER_HEIGHT
    font_svc_name = _get_font(18, bold=True)
    font_metric = _get_font(20, bold=True)
    font_small = _get_font(12, bold=False)
    font_detail = _get_font(13, bold=False)

    for item in items:
        extra_h = (24 if item.progress_ratio is not None else 0) + (len(item.details) > 2) * 16
        card_h = ROW_HEIGHT + extra_h - 12
        card_box = (
            CARD_PAD_X,
            curr_y,
            CANVAS_WIDTH - CARD_PAD_X,
            curr_y + card_h,
        )

        # 绘制圆角卡片底色与边框
        draw.rounded_rectangle(card_box, radius=12, fill=COLOR_CARD, outline=COLOR_CARD_BORDER, width=1)

        # 左侧品牌色装饰小条
        svc_color = SERVICE_COLORS.get(item.service.lower(), (148, 163, 184))
        draw.rounded_rectangle(
            (CARD_PAD_X, curr_y + 8, CARD_PAD_X + 4, curr_y + card_h - 8),
            radius=2,
            fill=svc_color,
        )

        # 状态指示圆点
        status_color = COLOR_STATUS_OK
        if item.status == "warn":
            status_color = COLOR_STATUS_WARN
        elif item.status in ("danger", "error"):
            status_color = COLOR_STATUS_DANGER

        draw.ellipse(
            (CARD_PAD_X + 18, curr_y + 19, CARD_PAD_X + 28, curr_y + 29),
            fill=status_color,
        )

        # 服务名称与 Masked Key
        svc_display = SERVICE_LABELS.get(item.service.lower(), item.service.title())
        draw.text(
            (CARD_PAD_X + 36, curr_y + 14),
            svc_display,
            font=font_svc_name,
            fill=COLOR_TEXT_PRIMARY,
        )
        draw.text(
            (CARD_PAD_X + 36, curr_y + 38),
            f"Key: {item.key_masked}",
            font=font_small,
            fill=COLOR_TEXT_MUTED,
        )

        # 右侧主指标 (可用额度 / 余额)
        metric_str = item.primary_metric
        # 计算右对齐坐标
        try:
            m_len = draw.textlength(metric_str, font=font_metric)
        except Exception:
            m_len = len(metric_str) * 12
        m_x = CANVAS_WIDTH - CARD_PAD_X - 18 - m_len
        draw.text((m_x, curr_y + 12), metric_str, font=font_metric, fill=COLOR_TEXT_PRIMARY)

        lbl_str = item.primary_label
        try:
            lbl_len = draw.textlength(lbl_str, font=font_small)
        except Exception:
            lbl_len = len(lbl_str) * 8
        draw.text(
            (CANVAS_WIDTH - CARD_PAD_X - 18 - lbl_len, curr_y + 38),
            lbl_str,
            font=font_small,
            fill=COLOR_TEXT_MUTED,
        )

        # 明细列表绘制（若有）
        detail_y = curr_y + 58
        if item.details:
            detail_text = "  ·  ".join(item.details[:3])
            draw.text(
                (CARD_PAD_X + 36, detail_y),
                detail_text,
                font=font_detail,
                fill=COLOR_TEXT_MUTED,
            )
            detail_y += 18

        # 进度条（针对有明确使用上限的服务）
        if item.progress_ratio is not None:
            bar_x1 = CARD_PAD_X + 36
            bar_x2 = CANVAS_WIDTH - CARD_PAD_X - 18
            bar_y = curr_y + card_h - 14
            bar_h = 6

            draw.rounded_rectangle(
                (bar_x1, bar_y, bar_x2, bar_y + bar_h),
                radius=3,
                fill=COLOR_PROGRESS_BG,
            )

            fill_width = int((bar_x2 - bar_x1) * min(max(item.progress_ratio, 0.0), 1.0))
            if fill_width > 4:
                draw.rounded_rectangle(
                    (bar_x1, bar_y, bar_x1 + fill_width, bar_y + bar_h),
                    radius=3,
                    fill=svc_color,
                )

        curr_y += card_h + 12

    # 3. 绘制底栏
    font_footer = _get_font(12, bold=False)
    draw.text(
        (CARD_PAD_X, total_height - 30),
        "AstrBot · api_quota v1.4.0 | 支持 /quota alert here 开启告警",
        font=font_footer,
        fill=COLOR_TEXT_MUTED,
    )

    # 4. 保存为临时文件
    temp_dir = tempfile.gettempdir()
    output_path = os.path.join(temp_dir, f"astrbot_quota_dashboard_{int(time.time()*1000)}.png")
    image.save(output_path, "PNG", quality=95)
    return output_path
