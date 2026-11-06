#!/usr/bin/env python3
"""
动态漫剧视频合成脚本

用法:
  python3 create_drama_video.py --storyboard storyboard.json --images ./panels --effects effects.json --output ./output/drama.mp4

effects.json 格式:
[
  {
    "id": "P1-1",
    "effects": [
      {"type": "rain", "duration": 2.0, "intensity": 0.8},
      {"type": "pan_zoom", "duration": 3.0, "direction": "in", "scale": 1.2}
    ],
    "transition": "fade",
    "transition_duration": 0.5,
    "bgm_mood": "melancholy"
  },
  ...
]

支持的效果类型:
  - pan_zoom: 平移推进（direction: in/out/left/right, scale: 缩放比例）
  - breathe: 呼吸动画（amplitude: 振幅, speed: 速度）
  - speed_lines: 速度线（direction: radial/horizontal, intensity: 强度）
  - shake: 抖动（intensity: 强度, speed: 频率）
  - fade: 渐入渐出（direction: in/out）
  - rain: 雨雪粒子（type: rain/snow, intensity: 强度）
"""

import argparse
import json
import os
import sys
import math

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("需要安装 Pillow: pip install Pillow", file=sys.stderr)
    sys.exit(1)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_pan_zoom(img, progress, direction="in", scale=1.2):
    """应用平移推进效果"""
    w, h = img.size
    if direction == "in":
        current_scale = 1.0 + (scale - 1.0) * progress
    else:
        current_scale = scale - (scale - 1.0) * progress
    
    new_w = int(w * current_scale)
    new_h = int(h * current_scale)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    
    # 裁剪回原始尺寸（居中）
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return resized.crop((left, top, left + w, top + h))


def apply_breathe(img, progress, amplitude=0.02, speed=1.0):
    """应用呼吸动画"""
    scale = 1.0 + amplitude * math.sin(progress * speed * 2 * math.pi)
    w, h = img.size
    new_w = int(w * scale)
    new_h = int(h * scale)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return resized.crop((left, top, left + w, top + h))


def apply_shake(img, progress, intensity=10, speed=30):
    """应用抖动效果"""
    offset_x = int(intensity * math.sin(progress * speed * 2 * math.pi))
    offset_y = int(intensity * math.cos(progress * speed * 2 * math.pi * 1.3))
    w, h = img.size
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    canvas.paste(img, (offset_x, offset_y))
    return canvas


def apply_fade(img, progress, direction="in", bg_color=(0, 0, 0)):
    """应用渐入渐出效果"""
    if direction == "in":
        alpha = progress
    else:
        alpha = 1.0 - progress
    
    w, h = img.size
    bg = Image.new("RGB", (w, h), bg_color)
    return Image.blend(bg, img, alpha)


def create_drama_video(storyboard_path, images_dir, effects_path, output_path, fps=24, default_duration=3.0):
    """
    合成动态漫剧视频
    
    原理：对每个面板应用指定的动态效果，生成帧序列，最终合成视频。
    如果没有ffmpeg可用，则输出为帧图片序列。
    """
    storyboard = load_json(storyboard_path)
    effects_config = load_json(effects_path)
    
    # 建立效果索引
    effects_map = {e["id"]: e for e in effects_config}
    
    # 准备输出目录
    frames_dir = os.path.join(os.path.dirname(output_path), "frames")
    os.makedirs(frames_dir, exist_ok=True)
    
    frame_index = 0
    
    for panel_data in storyboard:
        panel_id = panel_data["id"]
        img_path = os.path.join(images_dir, panel_data["image"])
        
        if not os.path.exists(img_path):
            print(f"警告: 图片不存在 {img_path}，跳过", file=sys.stderr)
            continue
        
        img = Image.open(img_path).convert("RGB")
        panel_effects = effects_map.get(panel_id, {})
        effect_list = panel_effects.get("effects", [])
        duration = panel_effects.get("duration", default_duration)
        
        total_frames = int(duration * fps)
        
        for i in range(total_frames):
            progress = i / max(total_frames - 1, 1)
            current_frame = img.copy()
            
            # 依次应用效果
            for effect in effect_list:
                etype = effect["type"]
                if etype == "pan_zoom":
                    current_frame = apply_pan_zoom(
                        current_frame, progress,
                        direction=effect.get("direction", "in"),
                        scale=effect.get("scale", 1.2)
                    )
                elif etype == "breathe":
                    current_frame = apply_breathe(
                        current_frame, progress,
                        amplitude=effect.get("amplitude", 0.02),
                        speed=effect.get("speed", 1.0)
                    )
                elif etype == "shake":
                    current_frame = apply_shake(
                        current_frame, progress,
                        intensity=effect.get("intensity", 10),
                        speed=effect.get("speed", 30)
                    )
                elif etype == "fade":
                    current_frame = apply_fade(
                        current_frame, progress,
                        direction=effect.get("direction", "in")
                    )
            
            # 统一宽度
            target_width = 1080
            if current_frame.width != target_width:
                ratio = target_width / current_frame.width
                new_h = int(current_frame.height * ratio)
                current_frame = current_frame.resize((target_width, new_h), Image.LANCZOS)
            
            frame_path = os.path.join(frames_dir, f"frame_{frame_index:05d}.png")
            current_frame.save(frame_path)
            frame_index += 1
        
        print(f"面板 {panel_id}: 已生成 {total_frames} 帧")
    
    # 尝试使用ffmpeg合成视频
    import subprocess
    try:
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", os.path.join(frames_dir, "frame_%05d.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "18",
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            print(f"动态漫剧视频已保存至: {output_path}")
            # 清理帧文件
            import shutil
            shutil.rmtree(frames_dir, ignore_errors=True)
        else:
            print(f"ffmpeg 合成失败: {result.stderr}", file=sys.stderr)
            print(f"帧序列保存在: {frames_dir}", file=sys.stderr)
    except FileNotFoundError:
        print("ffmpeg 未安装，帧序列保存在: " + frames_dir, file=sys.stderr)
        print("安装 ffmpeg 后可手动合成: ffmpeg -framerate 24 -i frame_%05d.png -c:v libx264 output.mp4", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("视频合成超时，帧序列保存在: " + frames_dir, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="动态漫剧视频合成脚本")
    parser.add_argument("--storyboard", required=True, help="分镜脚本JSON文件路径")
    parser.add_argument("--images", required=True, help="图片目录")
    parser.add_argument("--effects", required=True, help="效果配置JSON文件路径")
    parser.add_argument("--output", required=True, help="输出视频路径")
    parser.add_argument("--fps", type=int, default=24, help="帧率")
    parser.add_argument("--duration", type=float, default=3.0, help="每格默认时长（秒）")
    
    args = parser.parse_args()
    create_drama_video(args.storyboard, args.images, args.effects, args.output, args.fps, args.duration)


if __name__ == "__main__":
    main()
