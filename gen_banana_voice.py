#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""边缘语音(edge-tts)封装：生成旁白 mp3 / 试听播放 / 停止。

被 make_drama.py（正式配音）和 漫剧工作室.pyw（试听）共用。
  tts(text, out_path, voice, rate, pitch)  生成一段 mp3
  preview(text, voice, rate)               生成试听 mp3，返回 (路径, 时长)
  play(mp3)                                直接放声音（不弹播放器）
  stop_play()                              停止试听
"""
import asyncio, json, os, re, subprocess
import edge_tts

def _nowin():
    """Windows: 调 ffmpeg 时不弹黑框控制台。"""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


try:
    import imageio_ffmpeg
    FF = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FF = "ffmpeg"  # 退回系统 PATH 里的 ffmpeg

import app_paths as AP
OUTDIR = AP.OUTDIR
VOICE = "zh-CN-XiaoxiaoNeural"   # 温暖女声（默认）

NARRATION = [
    "远古雨林，小猴发现一棵发金光的神树。",
    "树上结着弯弯的、流转金光的奇异果实。",
    "温柔的精灵伸手，赐它生命与甜美。",
    "果实成熟舒展，化作我们熟悉的香蕉。",
    "小猴分给伙伴，甜美传遍世界。",
]

# 试听默认文案（够长，能听出音色和语速差异）
PREVIEW_TEXT = "远古雨林深处，小猴子发现了一棵会发光的神树。"

# 说话人标签：这些前缀要念出来就成笑话了（"旁白：这条巷子……" 会念成"旁白，这条巷子"）
_SPK_KNOWN = ("旁白", "画外音", "独白", "旁白君", "解说", "内心", "字幕")
_SPK_TAIL = ("嬢嬢", "阿姨", "大叔", "叔", "哥", "姐", "爷", "奶", "老师",
             "老板", "店长", "医生", "警察", "小孩", "男人", "女人", "母亲", "父亲")


def clean_line(text):
    """去掉台词前的说话人标签和括号提示，只留真正要念的字。"""
    t = (text or "").strip()
    m = re.match(r"^[\s【\[]*([^：:\n]{1,10}?)[\s】\]]*[：:]\s*(.+)$", t, re.S)
    if m:
        name = m.group(1).strip()
        rest = m.group(2).strip()
        if rest and (name in _SPK_KNOWN or name.endswith(_SPK_TAIL)
                     or (len(name) <= 3 and not re.search(r"[，。！？、,.!?…\-—\d]", name))):
            t = rest
    t = re.sub(r"[（(][^）)]{0,24}[）)]", "", t)      # （停顿）（笑）这类提示不念
    t = re.sub(r"\s+", " ", t).strip()
    return t


def tts(text, out_path, voice=None, rate="+0%", pitch="+0Hz"):
    """把一段文字合成 mp3。voice 为空用模块默认 VOICE。"""
    text = clean_line(text) or (text or "")

    async def _run():
        c = edge_tts.Communicate(text, voice or VOICE, rate=rate, pitch=pitch)
        await c.save(out_path)
    asyncio.run(_run())


def duration(path):
    try:
        r = subprocess.run([FF, "-i", path], capture_output=True, text=True, **_nowin())
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
        if m:
            h, mm, ss = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return round(h * 3600 + mm * 60 + ss, 3)
    except Exception:
        pass
    return None


def preview(text, voice, rate="+0%", out_path=None):
    """生成试听片段，返回 (mp3 路径, 时长秒)。每次都重新生成（保证音色/语速一致）。"""
    os.makedirs(OUTDIR, exist_ok=True)
    out_path = out_path or os.path.join(OUTDIR, "_voice_preview.mp3")
    try:
        if os.path.exists(out_path):
            os.remove(out_path)
    except Exception:
        pass
    tts(text or PREVIEW_TEXT, out_path, voice=voice, rate=rate)
    return out_path, (duration(out_path) or 0.0)


def play(mp3):
    """播放 mp3：先转成 24k 单声道 wav 用系统音频通道放（秒开、可停），
    失败再退回默认播放器。"""
    if not mp3 or not os.path.exists(mp3):
        return False
    wav = os.path.splitext(mp3)[0] + "_play.wav"
    try:
        subprocess.run([FF, "-y", "-loglevel", "error", "-i", mp3,
                        "-ar", "24000", "-ac", "1", wav],
                       capture_output=True, timeout=60, **_nowin())
    except Exception:
        pass
    if os.path.exists(wav):
        try:
            import winsound
            winsound.PlaySound(None, 0)          # 先停掉上一段
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return True
        except Exception:
            pass
    try:
        os.startfile(mp3)                        # 兜底：交给默认播放器
        return True
    except Exception:
        return False


def stop_play():
    try:
        import winsound
        winsound.PlaySound(None, 0)
        return True
    except Exception:
        return False


def voice_label_map():
    """音色 ShortName -> Azure 官方显示名（试听窗口里用，拉不到就返回空表）。"""
    try:
        vs = asyncio.run(edge_tts.list_voices())
        return {v["ShortName"]: v.get("FriendlyName", v["ShortName"]) for v in vs}
    except Exception:
        return {}


def main():
    shots = []
    for i, txt in enumerate(NARRATION, 1):
        mp3 = os.path.join(OUTDIR, f"_banana_v{i}.mp3")
        print(f"[tts] 镜{i}: {txt}")
        tts(txt, mp3)
        d = duration(mp3)
        if not d:
            d = max(2.0, len(txt) / 6.0)   # 退化估算: 约 6 字/秒
        shots.append({"mp3": mp3, "dur": d})
        print(f"       -> {os.path.basename(mp3)} 时长 {d:.2f}s")
    vm = {"shots": shots}
    vm_path = os.path.join(OUTDIR, "banana_voice_map.json")
    json.dump(vm, open(vm_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    total = sum(s["dur"] for s in shots)
    print(f"[done] 配音映射表 -> {vm_path}, 总旁白时长约 {total:.1f}s")


if __name__ == "__main__":
    main()
