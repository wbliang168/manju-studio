#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把 H3 输出的帧拼成竖屏 9:16 漫剧视频
=====================================
两种模式(自动选择):

1) 真视频模式 (output/shots_frames.json 存在)
   - 每镜是一段真实动态片段(多帧 PNG 序列)
   - 每镜按 24fps 连续拼成 clip (角色/镜头真动, 不再假推近)
   - 多镜 clip 之间用 xfade 叠化转场
   - 输出 output/manju_drama.mp4

2) 单镜兼容模式 (无 shots_frames.json, 有 last_frames.txt 或 manju_h3_*.png)
   - 把单镜多帧按 24fps 连续拼成一段真视频(无转场)
"""
import subprocess, os, glob, json, re

try:
    import imageio_ffmpeg
    FF = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FF = "ffmpeg"  # 退回系统 PATH 里的 ffmpeg
import app_paths as AP
OUTDIR = AP.OUTDIR
OUT = os.path.join(OUTDIR, "manju_drama.mp4")
SHOTS_JSON = os.path.join(OUTDIR, "shots_frames.json")
LAST = os.path.join(OUTDIR, "last_frames.txt")

# 成片画布尺寸: 默认竖屏 720x1280; 横屏由 make_drama 通过 OUT_W/OUT_H 环境变量传入(1280x720)
W = int(os.environ.get("OUT_W", "720") or 720)
H = int(os.environ.get("OUT_H", "1280") or 1280)
FPS = 24
# 统一调色: 轻微暖金绿 + 加饱和/对比, 把 5 镜影调拉齐(治"五镜不统一")
GRADE = "eq=saturation=1.07:contrast=1.04:gamma=0.97,colorbalance=rs=0.02:gs=0.005:bs=-0.02"
XF = 0.35                 # 镜间叠化(smoothdown)转场时长(秒); 太长方会闪
AI_FILTER = os.environ.get("AI_FILTER", "1") != "0"   # 去AI质感滤镜(颗粒/暗角/降饱和)默认开

# ---- 字幕烧制 --------------------------------------------------------------
# 实测两条爆款漫剧：每一格都挂字幕。台词靠字幕承载，观众就不会去核对口型，
# 这是 AI 剧最省力也最有效的一招。默认开，可用环境变量 SUBTITLE=0 关掉。
SUBTITLE = True
SUB_FONT = "C:/Windows/Fonts/msyhbd.ttc"     # 微软雅黑粗体
SUB_SIZE = 42
SUB_BOTTOM = 62                               # ASS 里 MarginV，距底部
SUB_PER_LINE = 16                             # 每行最多几个汉字
SUB_MAX_LINES = 2

ADD_AUDIO = os.environ.get("ADD_AUDIO", "1") != "0"   # 默认自动混流主题音轨
ADD_LOOP = os.environ.get("ADD_LOOP", "0") != "0"     # 叙事短剧默认不做循环(避免结尾弹回开头)
LOOP_FRAMES = int(os.environ.get("LOOP_FRAMES", "12")) # 末段交叉淡化回第1镜首帧的帧数(默认0.5s@24fps)
# 配音模式: 指向 {shots:[{mp3,dur}]} JSON 时, 用 edge-tts 台词配音(按语音时长对轨) 替代纯合成环境音
VOICE_MAP = os.environ.get("VOICE_MAP", "")
VOICE_VOL = float(os.environ.get("VOICE_VOL", "1.0"))  # 语音相对音量(环境音床默认压到 0.35)
# 音轨来源:
#   tts    = edge-tts 配音 + 合成环境音床(0.35) 垫底
#   native = A 方案, 模型原生音轨(台词+现场环境音本来就是一条) -> 不再叠合成噪声床
AUDIO_MODE = os.environ.get("AUDIO_MODE", "tts")


def _nowin():
    """Windows 下别再弹出黑框控制台(GUI 里每次调 ffmpeg 都会闪一个黑窗口)。"""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, **_nowin())
    print("returncode:", r.returncode)
    if r.returncode != 0:
        print("STDERR:", r.stderr[-2500:])
    return r


def ffprobe_duration(path):
    """用 ffprobe 取视频时长(秒)。"""
    p = os.path.join(os.path.dirname(FF), "ffprobe.exe")
    if not os.path.exists(p):
        return None
    cmd = [p, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    r = subprocess.run(cmd, capture_output=True, text=True, **_nowin())
    try:
        return float(r.stdout.strip())
    except Exception:
        return None


def real_duration(path):
    """取视频真实时长(秒)。无 ffprobe 时解析 ffmpeg 输出的 Duration, 再退化为按帧估算。"""
    # 优先 ffprobe
    d = ffprobe_duration(path)
    if d:
        return d
    # 退化1: 解析 ffmpeg -i 的 stderr "Duration: hh:mm:ss.ss"
    try:
        r = subprocess.run([FF, "-i", path], capture_output=True, text=True, **_nowin())
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
        if m:
            h, mm, ss = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return h * 3600 + mm * 60 + ss
    except Exception:
        pass
    # 退化2: 按 shots_frames.json 估算(与 build_video 的 est total 一致)
    try:
        if os.path.exists(SHOTS_JSON):
            data = json.load(open(SHOTS_JSON, encoding="utf-8"))
            fps = data.get("fps", FPS)
            shots = [s for s in data.get("shots", []) if s]
            if shots:
                total = sum(len(s) / fps for s in shots) - (len(shots) - 1) * XF
                return max(total, 1.0)
    except Exception:
        pass
    return None


def audio_duration(path):
    """取音频(mp3/wav)时长(秒)。无 ffprobe 时解析 ffmpeg stderr 的 Duration。"""
    try:
        r = subprocess.run([FF, "-i", path], capture_output=True, text=True, **_nowin())
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
        if m:
            h, mm, ss = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return h * 3600 + mm * 60 + ss
    except Exception:
        pass
    return None


def make_audio_wav(duration, wav_path):
    """合成主题音轨: 雨声环境(粉噪塑形) + 柔和 A 小调垫音和弦, 归一化并加首尾淡入淡出。"""
    d = max(1.0, float(duration))
    fade_out = max(0.1, d - 2.5)
    # 4 个正弦 = A2(110) C3(130.81) E3(164.81) A3(220), 低通变暖; 粉噪塑形成雨声
    # 【2026-10-30】去掉"粉噪塑形成雨声"那一路：粉红噪音听起来就是电流嘶声，
    # 而且它跟画面里的场景无关（破庙里放"雨声"、室内戏也在放）。
    # 只留 A 小三和弦垫音（音高明确，不会像噪音），音量压到很轻。
    filt = (
        "[0]lowpass=f=1800,volume=0.10[p1];"
        "[1]lowpass=f=1800,volume=0.09[p2];"
        "[2]lowpass=f=1800,volume=0.08[p3];"
        "[3]lowpass=f=1800,volume=0.06[p4];"
        "[p1][p2][p3][p4]amix=inputs=4:duration=longest:dropout_transition=0[m];"
        f"[m]afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out:.2f}:d=2.5[l];"
        "[l]loudnorm=I=-20:TP=-2.0:LRA=11[aout]"
    )
    cmd = [FF, "-y",
           "-f", "lavfi", "-i", "sine=frequency=110",
           "-f", "lavfi", "-i", "sine=frequency=130.81",
           "-f", "lavfi", "-i", "sine=frequency=164.81",
           "-f", "lavfi", "-i", "sine=frequency=220",
           "-t", f"{d + 1.0:.3f}",        # 多给 1s 余量, 后续 -shortest 截断
           "-filter_complex", filt,
           "-map", "[aout]", "-c:a", "pcm_s16le", wav_path]
    return run(cmd).returncode == 0


def add_audio(video):
    """给无声成片混入主题音轨(失败则保留无声版, 不致命)。"""
    if not ADD_AUDIO:
        print("[audio] 已关闭(ADD_AUDIO=0), 跳过")
        return False
    if not os.path.exists(video):
        print("[audio] 成片不存在, 跳过"); return False
    d = real_duration(video)
    if not d:
        print("[audio] 无法获取时长, 跳过"); return False
    wav = os.path.join(OUTDIR, "_audio.wav")
    if not make_audio_wav(d, wav):
        print("[audio] 音轨生成失败, 保留无声版"); return False
    tmp = video + ".aud.mp4"
    cmd = [FF, "-y", "-i", video, "-i", wav,
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-shortest", "-movflags", "+faststart", tmp]
    ok = run(cmd).returncode == 0
    if ok and os.path.exists(tmp):
        os.replace(tmp, video)
        print("[audio] 已混入音轨 ->", video, f"({os.path.getsize(video)/1_000_000:.2f} MB)")
    else:
        print("[audio] 混流失败, 保留无声版")
    try:
        os.remove(wav)
    except Exception:
        pass
    return ok


def make_voice_audio(shot_mp3s, shot_durs, total_dur, out_mp3, bed=True,
                     empty_flags=None):
    """配音模式混音: 每镜语音按各自 display 时长垫静音对轨 -> 拼接成整条语音轨。

    bed=True (TTS 版): 再叠一层合成环境音床 -> '旁白 + 风声垫音'。
    bed=False (A 方案): 模型原生音轨里本来就有现场环境音, 再叠噪声床只会发糊,
                        所以只做拼接 + 统一响度(loudnorm)。

    empty_flags: 与 shot_durs 等长, True 表示这镜没台词(空镜)。
    A 方案下必须靠它把这些镜的原生音轨换成环境声 —— 模型的音频分支不看剧本,
    空镜照样会"说话"(实测第5镜被听成"我色你"这种糊音), 音量还盖过台词。
    """
    if not (shot_mp3s and shot_durs):
        return False
    ch = "2"                          # 统一立体声：单声道成片在手机上会明显更"扁"
    # 1) 每镜语音垫静音到该镜 display 时长(语音短则补静音, 长则截断)
    padded = []
    for i, (mp3, d) in enumerate(zip(shot_mp3s, shot_durs)):
        p = os.path.join(OUTDIR, f"_vpad{i + 1}.mp3")
        _empty = bool(empty_flags and i < len(empty_flags) and empty_flags[i])
        if _empty and not bed:
            # 【2026-10-30 修正】这里以前用 ffmpeg 合成的粉红噪音冒充"环境声"：
            #     anoisesrc=color=pink:amplitude=0.5 + highpass=250 + lowpass=4500
            # 粉红噪音就是"嘶——"的宽带噪声，带通到这个区间之后听起来**就是电流声**。
            # 用户两次反馈"又是电流声"，来源就是这一行 —— 不是模型生成的，是我们自己造的。
            #
            # 正确做法：保留模型原生音轨里的现场声（雨、脚步、衣袍、刀风），
            # 只把人声所在的中频段压下去（模型在没台词的镜里会瞎念，实测被听成"我色你"）。
            # 人声主要能量在 300~3400Hz，压掉这一段，宽带的雨声/摩擦声基本不受影响。
            _src = mp3 if (mp3 and os.path.exists(mp3)) else ""
            _af = ("highpass=f=70,lowpass=f=9500,"
                   "equalizer=f=1300:width_type=h:width=2400:g=-22,"
                   "afftdn=nf=-30,volume=0.9")
            if _src:
                r = run([FF, "-y", "-i", _src,
                         "-af", _af + f",apad=pad_dur={max(0.0, float(d)):.3f}",
                         "-t", f"{float(d):.3f}", "-ar", "44100", "-ac", ch,
                         "-c:a", "libmp3lame", "-q:a", "4", p])
                _ok = (r.returncode == 0 and os.path.exists(p))
                if not _ok:
                    # 处理失败就退回"保留原音"，绝不再拿噪音顶上
                    r = run([FF, "-y", "-i", _src,
                             "-af", f"apad=pad_dur={max(0.0, float(d)):.3f}",
                             "-t", f"{float(d):.3f}", "-ar", "44100", "-ac", ch,
                             "-c:a", "libmp3lame", "-q:a", "4", p])
                    _ok = (r.returncode == 0 and os.path.exists(p))
                if _ok:
                    padded.append(p)
                    print(f"[audio] 第{i + 1}镜无台词: 保留现场声、压掉人声频段"
                          f"（不再用合成噪音冒充环境音）")
                    continue
            else:
                # 真的没有原生音轨（模型没出音频）→ 静音，也比电流声好
                run([FF, "-y", "-f", "lavfi",
                     "-i", f"anullsrc=r=44100:cl={'mono' if ch == '1' else 'stereo'}",
                     "-t", f"{float(d):.3f}", "-ar", "44100", "-ac", ch,
                     "-c:a", "libmp3lame", "-q:a", "4", p])
                if os.path.exists(p):
                    padded.append(p)
                    print(f"[audio] 第{i + 1}镜无台词且没有原生音轨: 置静音")
                    continue
        if mp3 and os.path.exists(mp3):
            nat = audio_duration(mp3) or 1.0
            extra = max(0.0, float(d) - nat)
            r = run([FF, "-y", "-i", mp3, "-af", f"volume={VOICE_VOL:.2f},apad=pad_dur={extra:.3f}",
                     "-t", f"{float(d):.3f}", "-ar", "44100", "-ac", ch,
                     "-c:a", "libmp3lame", "-q:a", "4", p])
            if r.returncode != 0 or not os.path.exists(p):
                run([FF, "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl={'mono' if ch == '1' else 'stereo'}",
                     "-t", f"{float(d):.3f}", "-ar", "44100", "-ac", ch,
                     "-c:a", "libmp3lame", "-q:a", "4", p])
        else:
            run([FF, "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl={'mono' if ch == '1' else 'stereo'}",
                 "-t", f"{float(d):.3f}", "-ar", "44100", "-ac", ch,
                 "-c:a", "libmp3lame", "-q:a", "4", p])
        if os.path.exists(p):
            padded.append(p)
    if not padded:
        return False
    # 2) 交叉淡化拼接: 各镜之间淡入淡出 XF 秒, 总时长 = sum(dur) - (n-1)*XF,
    #    与视频 xfade 严格对齐(否则混流时音频比画面长, 末句被截断或错位)。
    n = len(padded)
    speech = os.path.join(OUTDIR, "_speech.mp3")
    if n == 1:
        try:
            os.replace(padded[0], speech)
        except Exception:
            pass
        ok1 = os.path.exists(speech)
    else:
        inputs = []
        for p in padded:
            inputs += ["-i", p]
        # 链式滤镜: 上一个滤镜的输出 label 自动连入下一个, 不要重复写 prev
        fc = "[0:a]anull[a0]"
        for i in range(1, n):
            prev = "a0" if i == 1 else f"a{i - 1}"
            out = "aout" if i == n - 1 else f"a{i}"
            fc += f";[{prev}][{i}:a]acrossfade=d={XF}[{out}]"
        ok1 = run([FF, "-y"] + inputs + [
            "-filter_complex", fc, "-map", "[aout]",
            "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-q:a", "4", speech]).returncode == 0
    # 清理 padded 中间文件
    for p in padded:
        try:
            os.remove(p)
        except Exception:
            pass
    if not ok1 or not os.path.exists(speech):
        return False
    # 【2026-10-30 删除合成环境音床】
    # 这里以前不论 A/B 方案都会走一段"环境音床"：两条 anoisesrc 粉红噪音 +
    # tremolo 调制成"林间风声"，带通 250-4500。问题有三个：
    #   ① 粉噪的嘶嘶声听起来就是电流声（用户两次反馈"电流声"）；
    #   ② 全片共用同一条音床，不管这一场是破庙、桥上、芦苇荡还是室内；
    #   ③ 它盖在台词底下，人声清晰度直接下降。
    # 环境声的正路只有两条：A 方案的原生音轨（模型自己长出来的雨声/脚步），
    # 或者后期真配一轨音效。合成噪音不属于任何一条，删掉。
    # 现在两种方案都只做一次响度归一，不动内容。
    ok = run([FF, "-y", "-i", speech,
              "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
              "-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "192k",
              out_mp3]).returncode == 0
    try:
        os.remove(speech)
    except Exception:
        pass
    return ok and os.path.exists(out_mp3)
    # 3) 音床：已删除（见上）。
    return ok2 and os.path.exists(out_mp3)


def mux_audio(video, audio_mp3):
    """把音频轨替换进成片(失败保留原片, 不致命)。

    关键：**不能直接用 -shortest**。配音轨按每镜语音长度铺，画面按帧数取整，
    两者累计误差会让 -shortest 从尾巴咬掉最后一句（实测末镜台词被截断）。
    这里先把画面尾巴用末帧补足到语音长度，再按语音长度收尾。
    """
    if not os.path.exists(video) or not os.path.exists(audio_mp3):
        print("[mux] 文件缺失, 跳过"); return False
    V = real_duration(video) or 0.0
    A = audio_duration(audio_mp3) or 0.0
    tail = 0.25
    if A > V - 0.05:
        pad = A - V + tail
        if pad > 0.05:
            fixed = video + ".tail.mp4"
            rp = run([FF, "-y", "-i", video, "-vf",
                      f"tpad=stop_mode=clone:stop_duration={pad:.3f}",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                      "-preset", "medium", "-movflags", "+faststart", fixed])
            if rp.returncode == 0 and os.path.exists(fixed):
                os.replace(fixed, video)
                V = A + tail
                print(f"[mux] 画面补尾 {pad:.2f}s，台词不再被截断")
    end = min(A, V) if (A and V) else 0
    tmp = video + ".va.mp4"
    cmd = [FF, "-y", "-i", video, "-i", audio_mp3,
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    if end > 0:
        cmd += ["-t", f"{end:.3f}"]
    cmd += ["-movflags", "+faststart", tmp]
    ok = run(cmd).returncode == 0
    if ok and os.path.exists(tmp):
        os.replace(tmp, video)
        print("[mux] 已混入配音轨 ->", video, f"({os.path.getsize(video)/1_000_000:.2f} MB)")
    else:
        print("[mux] 混流失败, 保留原片")
    return ok


def make_loop(video, total_frames):
    """无缝循环: 把末尾 LOOP_FRAMES 帧交叉淡化为'第1镜首帧', 使末帧==首帧(循环播放无跳变)。

    做法(纯 ffmpeg, 总帧数不变):
      body  = 原片 0..N-L-1 帧
      tailseg = 原片末 L 帧
      f0    = 首帧复制 L 次(静止)
      trans = xfade(tailseg -> f0, 末帧=首帧)   # L 帧
      out   = body + trans
    于是整片最后一帧 == 首帧, 循环衔接处像素级一致。
    """
    if not ADD_LOOP:
        print("[loop] 已关闭(ADD_LOOP=0), 跳过")
        return False
    if not os.path.exists(video):
        print("[loop] 成片不存在, 跳过")
        return False
    N = int(total_frames)
    L = LOOP_FRAMES
    if N <= L * 2 + 2:
        print(f"[loop] 帧数 {N} 不足(需 > {L*2+2}), 跳过")
        return False
    body_end = N - L
    tmp = video + ".loop.mp4"
    fc = (
        f"[0:v]split=2[main][tail];"
        f"[main]trim=start_frame=0:end_frame={body_end},setpts=PTS-STARTPTS,fps={FPS}[body];"
        f"[tail]trim=start_frame={body_end}:end_frame={N},setpts=PTS-STARTPTS,fps={FPS}[tailseg];"
        f"[0:v]trim=start_frame=0:end_frame=1,setpts=PTS-STARTPTS,fps={FPS}[f0s];"
        f"[f0s]loop=loop={L-1}:size=1,format=yuv420p[f0];"
        f"[tailseg][f0]xfade=transition=fade:duration={L/FPS:.4f}:offset=0[trans];"
        f"[body][trans]concat=n=2:v=1[out]"
    )
    cmd = [FF, "-y", "-i", video, "-filter_complex", fc,
           "-map", "[out]", "-r", str(FPS), "-c:v", "libx264",
           "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "medium",
           "-movflags", "+faststart", tmp]
    ok = run(cmd).returncode == 0
    if ok and os.path.exists(tmp):
        os.replace(tmp, video)
        print(f"[loop] 已做无缝循环(末 {L} 帧淡回首帧) -> {video} "
              f"({os.path.getsize(video)/1_000_000:.2f} MB)")
    else:
        print("[loop] 循环处理失败, 保留原片")
    return ok


def pingpong_extend(src, dst, duration, n_frames):
    """把 n_frames 帧的短片段做往复(ping-pong)循环, 填满 duration 秒。

    正放 + 倒放首尾相接(接缝处是同一帧, 无跳切), 再整体循环。
    比"末帧定格"观感好得多: 画面全程在动, 不会僵住等旁白念完。
    """
    target = max(1, int(round(float(duration) * FPS)))
    unit = max(1, n_frames + max(0, n_frames - 1))   # 正放 + 倒放(去掉重复端点)
    k = max(1, -(-target // unit))                    # ceil
    fc = (f"[0:v]split=2[a][b];"
          f"[b]reverse,trim=start_frame=1,setpts=PTS-STARTPTS,fps={FPS},format=yuv420p[r];"
          f"[a]fps={FPS},format=yuv420p[f];"
          f"[f][r]concat=n=2:v=1,"
          f"loop=loop={k - 1}:size={unit},setpts=PTS-STARTPTS,"
          f"fps={FPS},format=yuv420p[out]")
    cmd = [FF, "-y", "-i", src, "-filter_complex", fc, "-map", "[out]",
           "-frames:v", str(target),
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
           "-preset", "medium", "-movflags", "+faststart", dst]
    return run(cmd).returncode == 0


# 这些标点不能出现在行首（中文排版规矩：标点跟随前一个字）
_NO_LINE_START = "，。！？、；：”’」』）】》…·—~"


def _wrap_cn(text, per=None, maxlines=None):
    """中文按字数硬换行，且不让标点落到下一行行首。

    libass 对没有空格的中文几乎不做自动断行（实测整行直接超出画面），
    所以必须自己数好字。返回用真实换行符分段，拼 ASS 时再转成 \\N。
    """
    per = per or SUB_PER_LINE
    maxlines = maxlines or SUB_MAX_LINES
    t = (text or "").strip().replace("\n", " ").replace("\r", "")
    if not t:
        return ""

    lines, i = [], 0
    while i < len(t) and len(lines) < maxlines:
        seg = t[i:i + per]
        j = i + per
        # 「——」「，」这类不该被甩到下一行开头，并到本行末尾
        while j < len(t) and t[j] in _NO_LINE_START and len(seg) < per + 3:
            seg += t[j]
            j += 1
        lines.append(seg)
        i = j
    if i < len(t) and lines:
        lines[-1] = lines[-1][:-1] + "…"
    return "\n".join(lines)


def _fpath(p):
    """ffmpeg filter 参数里的 Windows 路径：正斜杠 + 冒号转义。"""
    return p.replace("\\", "/").replace(":", "\\:")


def _probe_size(video):
    """读成片真实分辨率——ASS 的 PlayRes 必须与它一致，否则字号和边距全失真。"""
    try:
        r = subprocess.run([FF, "-i", video], capture_output=True, text=True, **_nowin())
        m = re.search(r"Video:.*?[,\s](\d{2,5})x(\d{2,5})", r.stderr)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return W, H


def _ass_time(t):
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    return "%d:%02d:%05.2f" % (h, m, t % 60)


def _ass_escape(t):
    """ASS 里 { } \\ 是控制字符，台词里出现必须转义，否则整条字幕失效。"""
    return (t.replace("\\", "／").replace("{", "｛").replace("}", "｝"))


ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0.4,0,1,{outline},0,2,{ml},{mr},{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def burn_subtitles(video, texts, durs, keep_audio=False, xf=None):
    """把每镜台词烧进成片。

    用 ASS 字幕滤镜而不是 drawtext：drawtext 算不准中文的双宽字符宽度，
    x=(w-tw)/2 居中的结果是左右都被裁掉（实测过）。ASS 自带排版与换行，稳。

    xf：镜间叠化时长。视频用 xfade 拼接，每个接缝吃掉 xf 秒，所以第 i 镜在
    成片里的起点是 sum(dur[:i]) - i*xf；字幕必须按这个算。之前直接累加 dur，
    8 镜就慢了 7*0.35=2.45 秒——最后一镜的字幕还是上一镜的台词。
    """
    if not (texts and durs) or not any(t for t in texts):
        return False
    if xf is None:
        xf = float(XF)

    vw, vh = _probe_size(video)
    size = max(20, int(round(vh * SUB_SIZE / 1280.0)))       # 字号按高度等比
    mlr = max(18, int(round(vw * 0.06)))                     # 左右留白 6%
    mv = max(20, int(round(vh * SUB_BOTTOM / 1280.0)))        # 距底等比
    # 每行能放几个汉字：可用宽度 / 字号（中文每字约 1em）
    per_line = max(8, int((vw - mlr * 2) / float(size) * 0.92))   # 留 8% 余量
    print(f"[sub] 分辨率 {vw}x{vh} 字号 {size}px 左右 {mlr}px 每行 {per_line} 字")

    ass = os.path.join(OUTDIR, "_burn.ass")
    head = ASS_HEAD.format(
        w=vw, h=vh, font="Microsoft YaHei", size=size,
        outline=max(2.0, round(size * 0.09, 1)),
        ml=mlr, mr=mlr, mv=mv)
    body, t0, n = [], 0.0, 0
    for txt, d in zip(texts, durs):
        d = float(d or 0)
        line = _wrap_cn(txt, per=per_line, maxlines=2)
        if line and d > 0.3:
            # 起点＝累加时长 - 已经吃掉的所有叠化（每个接缝 xf 秒）
            st = max(0.0, t0)
            en = max(st + 0.2, st + d - 0.04)
            body.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s"
                        % (_ass_time(st + 0.04), _ass_time(en),
                           _ass_escape(line).replace("\n", "\\N")))
            n += 1
        t0 += d - xf
    if not body:
        return False
    with open(ass, "w", encoding="utf-8-sig") as f:
        f.write(head + "\n".join(body) + "\n")

    tmp = video + ".sub.mp4"
    acodec = ["-c:a", "aac", "-b:a", "192k"] if keep_audio else ["-an"]
    ok = run([FF, "-y", "-i", video,
              "-vf", "ass='%s'" % _fpath(ass),
              "-c:v", "libx264", "-crf", "20", "-preset", "medium",
              "-pix_fmt", "yuv420p"] + acodec + ["-movflags", "+faststart", tmp]).returncode == 0
    if ok and os.path.exists(tmp):
        os.replace(tmp, video)
        print(f"[sub] 已烧入 {n} 条字幕（{size}px 雅黑粗体，距底 {mv}px）")
        try:
            os.remove(ass)
        except Exception:
            pass
        return True
    print("[sub] 字幕烧制失败，保留无字版")
    return False


def concat_clip(frames, out_clip, duration=None):
    """把一镜的连续 PNG 帧按 FPS 拼成 mp4(真实动态, 不做假推近)。

    duration 给定时(配音模式): 先用 ping-pong 往复循环把画面填满该秒数,
    画面全程保持运动(失败则回退为末帧定格)。
    """
    frames = [f for f in frames if os.path.exists(f)]
    if not frames:
        print("[warn] no frames for clip", out_clip)
        return False
    lst = out_clip + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in frames:
            f.write(f"file '{p}'\n")
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,{GRADE},format=yuv420p")

    need_extend = False
    need_trim = False
    extra = 0.0
    if duration:
        nat = len(frames) / float(FPS)
        extra = float(duration) - nat
        need_extend = extra > 0.01          # 真实运动比旁白短 -> Ken Burns 缓推补足
        need_trim = extra < -0.01           # 真实运动比旁白长 -> 截断到旁白时长(旁白=视频长度, 零填充)

    # 1) 先生成原始动态片段(不延长/不截断)
    base = out_clip + ".base.mp4"
    cmd = [FF, "-y", "-f", "concat", "-safe", "0", "-r", str(FPS), "-i", lst,
           "-vf", vf,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
           "-preset", "medium", "-movflags", "+faststart", base]
    ok = run(cmd).returncode == 0
    try:
        os.remove(lst)
    except Exception:
        pass
    if not ok:
        return False

    # 真实运动比旁白长: 截断到旁白时长(避免"画面比旁白长、结尾空转", 实现旁白=视频长度)
    if need_trim:
        print(f"[trim] 真实运动 {len(frames) / float(FPS):.2f}s > 旁白 {float(duration):.2f}s, "
              f"截断到旁白时长(零填充/零冻结)")
        r = run([FF, "-y", "-i", base, "-t", f"{float(duration):.3f}",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                 "-preset", "medium", "-movflags", "+faststart", out_clip])
        try:
            os.remove(base)
        except Exception:
            pass
        return r.returncode == 0

    if not need_extend:
        os.replace(base, out_clip)
        return True

    # 2) 配音模式: 每镜只播一次真实动态, 末尾用 Ken Burns 缓慢推近补足台词时长
    #    (不往复=不重复, 不冻结=有呼吸感; 镜头在静帧上极缓推进, 像电影定镜运镜)
    print(f"[extend] Ken Burns 缓推补足 {len(frames)}帧 -> {float(duration):.2f}s (无重复/不僵死)")
    nf = max(2, int(round(extra * FPS)))
    last = out_clip + ".last.png"
    ex = out_clip + ".kb.mp4"
    # 取末帧
    # 取**末帧**做缓推(以前取第 0 帧, 会导致"动一下就跳回开头定格", 观感像画面卡死)
    n_last = max(0, len(frames) - 1)
    r1 = run([FF, "-y", "-sseof", "-0.2", "-i", base, "-update", "1",
              "-q:v", "2", last])
    if r1.returncode != 0 or not os.path.exists(last):
        r1 = run([FF, "-y", "-i", base, "-vf",
                  f"select=eq(n\\,{n_last})", "-vframes", "1",
                  "-q:v", "2", last])
    if r1.returncode != 0 or not os.path.exists(last):
        # 取不到末帧则回退末帧定格(兜底, 不致命)
        print("[warn] 取末帧失败, 回退定格")
        rvf = vf + f",tpad=stop_mode=clone:stop_duration={extra:.3f}"
        ok2 = run([FF, "-y", "-i", base, "-vf", rvf,
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                   "-preset", "medium", "-movflags", "+faststart", out_clip]).returncode == 0
        try:
            os.remove(base)
        except Exception:
            pass
        return ok2
    # 末帧上做极缓推近(zoom 1.0 -> 1.06), 形成呼吸感运镜
    zf = (f"zoompan=z='min(1.0+0.0006*on,1.06)':d={nf}:s={W}x{H}:fps={FPS}:"
          f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'")
    r2 = run([FF, "-y", "-loop", "1", "-i", last, "-vf",
              f"{GRADE},{zf},format=yuv420p",
              "-frames:v", str(nf),
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
              "-preset", "medium", out_clip + ".kb.mp4"])
    if r2.returncode != 0 or not os.path.exists(ex):
        # 推近失败回退定格
        print("[warn] Ken Burns 失败, 回退定格")
        rvf = vf + f",tpad=stop_mode=clone:stop_duration={extra:.3f}"
        ok2 = run([FF, "-y", "-i", base, "-vf", rvf,
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                   "-preset", "medium", "-movflags", "+faststart", out_clip]).returncode == 0
        try:
            os.remove(base)
            os.remove(last)
        except Exception:
            pass
        return ok2
    # 拼接 真实动态段 + Ken Burns 段
    cc = out_clip + ".concat.txt"
    with open(cc, "w", encoding="utf-8") as f:
        f.write(f"file '{base}'\n")
        f.write(f"file '{ex}'\n")
    ok3 = run([FF, "-y", "-f", "concat", "-safe", "0", "-i", cc,
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
               "-preset", "medium", "-movflags", "+faststart", out_clip]).returncode == 0
    try:
        os.remove(base)
        os.remove(last)
        os.remove(ex)
        os.remove(cc)
    except Exception:
        pass
    return ok3


def deai(src, dst):
    """去 AI 质感滤镜: 轻微胶片颗粒 + 暗角 + 略降饱和, 把'塑料完美感'往下压一档。

    失败(滤镜不支持/返回非 0)时退化为直接复制, 绝不影响出片。
    想关掉就把环境变量 AI_FILTER=0。
    """
    if not AI_FILTER or not os.path.exists(src):
        if src != dst:
            try:
                os.replace(src, dst)
            except Exception:
                pass
        return os.path.exists(dst)
    # 注意: 颗粒用 allf=a(均值, 不逐帧闪烁); 之前用 allf=t(时域随机)会在叠化转场处
    # 闪成"雪花/静电"——两镜叠化的高细节区叠上每帧随机噪点就显出来了。强度也从 7 降到 4。
    vf = "noise=alls=4:allf=a,vignette=0.55,eq=saturation=0.9"
    ok = run([FF, "-y", "-i", src, "-vf", vf, "-an",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
              "-preset", "medium", "-movflags", "+faststart", dst]).returncode == 0
    if ok and os.path.exists(dst):
        try:
            os.remove(src)
        except Exception:
            pass
        return True
    # 滤镜不可用 -> 直接复制
    if src != dst:
        try:
            os.replace(src, dst)
        except Exception:
            pass
    return os.path.exists(dst)


def build_video(shot_lists, shot_durs=None, xf=None, subtitles=None):
    xf = XF if xf is None else float(xf)
    clips = []
    for i, frames in enumerate(shot_lists):
        clip = os.path.join(OUTDIR, f"_clip{i + 1}.mp4")
        dur = shot_durs[i] if (shot_durs and i < len(shot_durs)) else None
        if concat_clip(frames, clip, duration=dur):
            clips.append(clip)
    if not clips:
        print("[ERROR] 没有任何镜片段可拼"); return False
    raw = os.path.join(OUTDIR, "_pre.mp4")
    if len(clips) == 1:
        os.replace(clips[0], raw)
        ok = True
    elif xf <= 0.001:
        # 硬切拼接(re-encode, 以便叠加去 AI 滤镜): 配音模式下仍是硬切,
        # 不吞时长, 所以音轨保持 concat 也不用交叉淡化。
        inputs = []
        for c in clips:
            inputs += ["-i", c]
        fc = "".join("[{}:v]".format(i) for i in range(len(clips))) + \
             "concat=n={}:v=1:a=0[v]".format(len(clips))
        ok = run([FF, "-y"] + inputs + [
            "-filter_complex", fc, "-map", "[v]", "-r", str(FPS),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
            "-preset", "medium", "-movflags", "+faststart", raw]).returncode == 0
    else:
        # 叠化(dissolve)转场: 相邻镜头平滑交叉淡入淡出, 不像硬切那样"跳"。
        inputs = []
        filters = []
        for i, c in enumerate(clips):
            inputs += ["-i", c]
            filters.append(f"[{i}:v]setsar=1[v{i}]")
        prev = "v0"
        offset = 0.0
        for i in range(1, len(clips)):
            nxt = f"v{i}"
            out = "vout" if i == len(clips) - 1 else f"t{i}"
            d_prev = float(shot_durs[i - 1]) if (shot_durs and i - 1 < len(shot_durs)) \
                else (ffprobe_duration(clips[i - 1]) or 2.0)
            offset = offset + d_prev - xf
            if offset < 0:
                offset = 0
            # 注意: 不能用 transition=dissolve —— 它做的是"逐像素随机替换", 两个不同画面
            # 交叉时中间段就是满屏随机彩点, 观感=电视雪花/静电(用户明确吐槽过)。
            # 改用 smoothdown: 平滑方向性交叉淡化, 干净无雪花, 又不是硬切/黑场(PPT 感)。
            filters.append(
                f"[{prev}][{nxt}]xfade=transition=smoothdown:duration={xf}:offset={offset:.3f}[{out}]")
            prev = out
        if shot_durs:
            total = sum(float(d) for d in shot_durs) - (len(clips) - 1) * xf
        else:
            total = sum((ffprobe_duration(c) or 2.0) for c in clips) - (len(clips) - 1) * xf
        filter_complex = ";".join(filters)
        print("filter_complex:", filter_complex)
        print(f"est total duration: {total:.2f}s")
        ok = run([FF, "-y"] + inputs + [
            "-filter_complex", filter_complex,
            "-map", f"[{prev}]", "-r", str(FPS),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", "20", "-preset", "medium", "-movflags", "+faststart", raw]).returncode == 0
    for c in clips:
        try:
            os.remove(c)
        except Exception:
            pass
    if not (ok and os.path.exists(raw)):
        print("[ERROR] 镜头拼接失败")
        return False
    print("OK (多镜/转场=%.2fs %s) 中间片 %s" % (xf, "smoothdown" if xf > 0.001 else "硬切",
          "{:.2f} MB".format(os.path.getsize(raw) / 1_000_000)))
    # 去 AI 质感滤镜 -> 最终 OUT。字幕由调用方在 mux 之前统一烧入(保持清晰)。
    return deai(raw, OUT)


def build_legacy():
    """兼容: 单镜多帧按 24fps 连续拼(无转场)。"""
    if os.path.exists(LAST):
        with open(LAST, encoding="utf-8") as f:
            cand = [l.strip() for l in f if l.strip()]
        frames = [p for p in cand if os.path.exists(p)]
    else:
        frames = sorted(glob.glob(os.path.join(OUTDIR, "manju_h3_0000*.png")))
    if len(frames) < 2:
        print("[ERROR] legacy 模式需要 >=2 帧, 找到", len(frames))
        return False
    frames.sort()
    print("legacy frames:", len(frames))
    return concat_clip(frames, OUT)


def main():
    # 每次调用都重新读取环境变量（被 make_drama 以 import 方式调用时需要）
    global ADD_AUDIO, ADD_LOOP, VOICE_MAP, VOICE_VOL, SUBTITLE, AUDIO_MODE, W, H
    W = int(os.environ.get("OUT_W", str(W)) or W)
    H = int(os.environ.get("OUT_H", str(H)) or H)
    ADD_AUDIO = os.environ.get("ADD_AUDIO", "1") != "0"
    SUBTITLE = os.environ.get("SUBTITLE", "1") != "0"
    ADD_LOOP = os.environ.get("ADD_LOOP", "0") != "0"
    VOICE_MAP = os.environ.get("VOICE_MAP", "")
    VOICE_VOL = float(os.environ.get("VOICE_VOL", "1.0"))
    AUDIO_MODE = os.environ.get("AUDIO_MODE", "tts")

    if os.path.exists(SHOTS_JSON):
        with open(SHOTS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        shot_lists = [s for s in data.get("shots", []) if s]
        if not shot_lists:
            print("[ERROR] shots_frames.json 为空"); return
        # 配音模式: 用 edge-tts 台词配音(按语音时长对轨) 替代纯合成环境音
        if VOICE_MAP and os.path.exists(VOICE_MAP):
            vm = json.load(open(VOICE_MAP, encoding="utf-8"))
            vshots = vm.get("shots", [])
            shot_mp3s = [m.get("mp3") for m in vshots]
            shot_durs = [float(m.get("dur", 1.0)) for m in vshots]
            src = "模型原生发声(A方案)" if AUDIO_MODE == "native" else "edge-tts 配音(B方案)"
            print(f"[mode] 音轨={src}, {len(shot_lists)} 镜"
                  f"{'（含字幕）' if SUBTITLE else ''}")
            texts = [m.get("text", "") for m in vshots] if SUBTITLE else None
            empty_flags = [not (m.get("text") or "").strip() for m in vshots]
            if build_video(shot_lists, shot_durs, xf=XF, subtitles=texts):
                # 总时长必须扣掉镜间叠化：视频用 xfade 拼，7 个接缝少吃 7*XF 秒。
                # 之前这里直接 sum(dur)，环境音床就比画面长 2.5 秒，
                # mux 时只好给画面补一段末帧冻结（看着像结尾卡住）。
                total = sum(shot_durs) - max(0, len(shot_durs) - 1) * XF
                vmp3 = os.path.join(OUTDIR, "_voice.m4a")
                if make_voice_audio(shot_mp3s, shot_durs, total, vmp3,
                                    bed=(AUDIO_MODE != "native"),
                                    empty_flags=empty_flags):
                    if SUBTITLE and texts:
                        burn_subtitles(OUT, texts, shot_durs)   # 字幕在混音前烧, 保持清晰
                    mux_audio(OUT, vmp3)
                    try:
                        os.remove(vmp3)
                    except Exception:
                        pass
                    print("[done] 配音版成片 ->", OUT)
                    return
                else:
                    print("[audio] 配音混音失败, 回退纯环境音")
                    add_audio(OUT)
            else:
                print("[video] 配音版视频拼接失败, 回退纯环境音")
                add_audio(OUT)
            return
        # 默认: 纯合成环境音 + 无缝循环
        print(f"[mode] 真视频模式, {len(shot_lists)} 镜")
        if build_video(shot_lists):
            total = sum(len(s) for s in shot_lists)
            make_loop(OUT, total)   # 无缝循环(末帧淡回首帧), 总帧数不变
            add_audio(OUT)
    else:
        print("[mode] 单镜兼容模式")
        if build_legacy():
            add_audio(OUT)


if __name__ == "__main__":
    main()
