#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
漫画长图拼合与文字叠加（漫剧工作室内置版，重写为干净可 import 的模块）

用法（函数级）：
  from manju_skills.manga import assemble_manga, assemble_from_images
  assemble_manga(storyboard_path, images_dir, output_path, width=1080, gap=10, margin=24)
  assemble_from_images(["p1.png","p2.png"], out.png, width=1080)

storyboard.json 格式（每格一项）：
  [
    {
      "id": "P1-1",
      "image": "P1-1.png",
      "dialogues": [{"text": "你好", "position": "top-right"}],
      "narration": "那天的雨，大得像世界要被洗掉一样。",
      "narration_position": "top"
    }
  ]
position 可选：top-right / top-left / bottom-right / bottom-left / center
"""
import json
import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("需要安装 Pillow: pip install Pillow", file=sys.stderr)
    sys.exit(1)


def _load_font(size):
    """尝试加载一个能显示中文的字体，失败退回默认点阵字体。"""
    cands = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for p in cands:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _wrap_text(draw, text, font, max_w):
    """按像素宽度做简单的中文/英文换行。"""
    lines = []
    cur = ""
    for ch in text:
        test = cur + ch
        if draw.textlength(test, font=font) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _speech_bubble(draw, text, position, img_w, font, padding=12):
    """画圆角对白气泡，返回其底部 y（便于后续布局）。"""
    max_w = img_w - 80
    lines = _wrap_text(draw, text, font, max_w)
    line_h = font.size + 6
    text_w = max(draw.textlength(ln, font=font) for ln in lines)
    bubble_w = int(text_w) + padding * 2
    bubble_h = line_h * len(lines) + padding * 2
    if position == "top-right":
        x = img_w - bubble_w - 24
        y = 18
    elif position == "top-left":
        x = 24
        y = 18
    elif position == "bottom-right":
        x = img_w - bubble_w - 24
        y = 18
    elif position == "bottom-left":
        x = 24
        y = 18
    elif position == "center":
        x = (img_w - bubble_w) // 2
        y = 24
    else:
        x = 24
        y = 18
    # 底部对齐：bottom-* 时把气泡贴到图片底部
    if position in ("bottom-right", "bottom-left"):
        y = 0  # 调用方会覆盖；这里仅占位
    draw.rounded_rectangle([x, y, x + bubble_w, y + bubble_h], radius=14,
                           fill="white", outline="black", width=2)
    for i, ln in enumerate(lines):
        draw.text((x + padding, y + padding + i * line_h), ln,
                  fill="black", font=font)
    return y + bubble_h


def _narration_box(draw, text, img_w, font, y=14, padding=10):
    lines = _wrap_text(draw, text, font, img_w - 80)
    line_h = font.size + 6
    box_w = img_w - 48
    box_h = line_h * len(lines) + padding * 2
    x = 24
    draw.rounded_rectangle([x, y, x + box_w, y + box_h], radius=8,
                           fill=(30, 30, 30, 210), outline=None)
    for i, ln in enumerate(lines):
        draw.text((x + padding, y + padding + i * line_h), ln,
                  fill="white", font=font)
    return y + box_h


def assemble_manga(storyboard_path, images_dir, output_path, width=1080,
                   gap=10, margin=24):
    """按 storyboard.json 拼合漫画长图（带对白气泡 / 旁白框）。"""
    with open(storyboard_path, "r", encoding="utf-8") as f:
        storyboard = json.load(f)

    panels = []
    for panel in storyboard:
        img_path = os.path.join(images_dir, panel["image"])
        if not os.path.exists(img_path):
            print("警告: 图片不存在 %s，跳过" % img_path, file=sys.stderr)
            continue
        img = Image.open(img_path).convert("RGB")
        new_w = width - margin * 2
        ratio = new_w / img.width
        img = img.resize((new_w, int(img.height * ratio)), Image.LANCZOS)
        panels.append((panel, img))

    if not panels:
        raise RuntimeError("没有可用的面板图片")

    total_h = sum(p.height for _, p in panels) + gap * (len(panels) + 1)
    canvas = Image.new("RGB", (width, total_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font_d = _load_font(26)
    font_n = _load_font(22)

    y = gap
    for panel, img in panels:
        canvas.paste(img, (margin, y))
        bottom = y + img.height
        # 旁白（顶部）
        narr = panel.get("narration", "")
        if narr:
            nb = _narration_box(draw, narr, width, font_n, y=y + 12)
        # 对白气泡
        for d in panel.get("dialogues", []) or []:
            txt = d.get("text", "")
            if not txt:
                continue
            pos = d.get("position", "top-right")
            if pos in ("bottom-left", "bottom-right"):
                _draw_bottom_bubble(draw, txt, pos, width, bottom, font_d)
            else:
                _speech_bubble(draw, txt, pos, width, font_d)
        y = bottom + gap

    canvas.save(output_path)
    print("漫画长图已保存至: %s" % output_path)
    return output_path


def _draw_bottom_bubble(draw, text, position, img_w, bottom, font, padding=12):
    max_w = img_w - 80
    lines = _wrap_text(draw, text, font, max_w)
    line_h = font.size + 6
    text_w = max(draw.textlength(ln, font=font) for ln in lines)
    bubble_w = int(text_w) + padding * 2
    bubble_h = line_h * len(lines) + padding * 2
    x = (img_w - bubble_w - 24) if position == "bottom-right" else 24
    y = bottom - bubble_h - 18
    draw.rounded_rectangle([x, y, x + bubble_w, y + bubble_h], radius=14,
                           fill="white", outline="black", width=2)
    for i, ln in enumerate(lines):
        draw.text((x + padding, y + padding + i * line_h), ln,
                  fill="black", font=font)


def assemble_from_images(image_paths, output_path, width=1080, gap=10,
                        margin=24):
    """直接按图片顺序拼长图（无对白/旁白），适合快速出条漫。"""
    imgs = []
    for p in image_paths:
        if not os.path.exists(p):
            print("警告: 图片不存在 %s，跳过" % p, file=sys.stderr)
            continue
        img = Image.open(p).convert("RGB")
        new_w = width - margin * 2
        ratio = new_w / img.width
        imgs.append(img.resize((new_w, int(img.height * ratio)), Image.LANCZOS))
    if not imgs:
        raise RuntimeError("没有可用的图片")
    total_h = sum(i.height for i in imgs) + gap * (len(imgs) + 1)
    canvas = Image.new("RGB", (width, total_h), (255, 255, 255))
    y = gap
    for i in imgs:
        canvas.paste(i, (margin, y))
        y += i.height + gap
    canvas.save(output_path)
    print("长图已保存至: %s" % output_path)
    return output_path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--storyboard")
    ap.add_argument("--images", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--gap", type=int, default=10)
    ap.add_argument("--margin", type=int, default=24)
    a = ap.parse_args()
    if a.storyboard:
        assemble_manga(a.storyboard, a.images, a.output, a.width, a.gap, a.margin)
    else:
        files = sorted(os.listdir(a.images))
        paths = [os.path.join(a.images, f) for f in files
                 if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        assemble_from_images(paths, a.output, a.width, a.gap, a.margin)
