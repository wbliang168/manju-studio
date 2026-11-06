#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
多镜真视频生成器 (A 方案 · 本机验证版)
=======================================
复用 manju_studio/server.py 验证过的「整条分镜链一次提交」接线:
  - GGUF Q4_K 模型 (UnetLoaderGGUF): 10.6G 整个装进 12G 显存, 消除 int8(19.5G) 的分块搬运瓶颈
  - ManjuLatentRelay 纯 latent 末帧接力 + 延迟 VAE 解码: VAE 全程只上卡一次, DiT 加载一次永不被踢
    => 彻底解决旧版「逐镜单独提交 → 第 2 镜 OOM」的死法
  - cfg=1.0 (H3 是蒸馏 cfg-free 模型, CFG>1 无意义)
  - 每镜 length 帧真视频 (默认 25 ≈ 1s @24fps), 全部帧保留 => 真·动态片段

启动 ComfyUI 必须用验证过的参数(run_nvidia.bat 同款):
  --lowvram --reserve-vram 1 --preview-method none --disable-pinned-memory --cache-ram 2 16

输出:
  - output/{prefix}_sN_0000X_.png   每镜的视频帧序列(真动态)
  - output/shots_frames.json        分镜帧清单(供 build_manju_mp4.py 拼接)
"""
import threading
import json, time, sys, urllib.request, os, argparse, re, glob

import app_paths as AP
import stopflag
import progress as PG
URL = AP.COMFY_URL
CLIENT = "workbuddy_h3_shots"
OUTDIR = AP.OUTDIR
WORKDIR = AP.WORKDIR
I2V_INPUT_DIR = AP.INPUTDIR
DEFAULT_SCRIPT = os.path.join(WORKDIR, "manju_studio/scripts/雨落进旧街.json")

# 模型常量(与 server.py 一致, 本机验证过)
GGUF_UNET = "minimax_h3_fl2va_pruned-Q4_K.gguf"
INT8_UNET = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
TE_NAME = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VAE_NAME = "minimax_h3_video_vae_fp16.safetensors"
USE_GGUF = True
LATENT_RELAY = True

# ---- 轻量文本编码器 (ClipProj) ----------------------------------------------
# H3 官方要 32B 文本编码器(15.7G, 塞不进 12G 卡 -> 只能 CPU 算, 每镜几分钟)。
# ClipProj 用 Qwen3-VL-4B + 学习到的投影矩阵还原 32B 的 conditioning 空间:
#   15.7G -> 4.9G, 文本编码从 CPU 换到 GPU, 单镜编码耗时从 ~300s 降到 ~10s。
# mode: streaming = 编码时上卡, 编完退回内存(12G 卡最省); resident = 常驻显存(20G+ 用)。
TE4B_NAME = "qwen3vl_4b_int8_convrot.safetensors"
CLIPPROJ_NAME = "mmh3-4b-ClipProj-v3.1-mlp.safetensors"
USE_CLIPPROJ = False
CLIPPROJ_MODE = "streaming"
CLIPPROJ_DEVICE = "cuda:0"

# 竖屏 9:16; length = 24fps 下的帧数(25 ≈ 1s/镜); steps=18 标准(质量/速度平衡)
# 2026-10-27: 分辨率由 576x1024 提档 720x1280(竖屏短剧标准档, 解决中间柔光镜清晰度偏低问题)
# 注意: 宽高必须是 32 的倍数！H3 视频 latent 是 1/16 + 2x2 patch，
# 720x1280 里 720/16=45(奇数) -> patchify 时 "shape [1,24,1,1,40,2,22,2] invalid"。
# 9:16 附近的可选档: 704x1280(11:20) / 736x1280(23:40) / 576x1024(9:16)。
W, H, LEN, STEPS, FPS = 576, 1024, 25, 18, 24
BASE_SEED = 8844221

# 前置强指令: H3 是 cfg=1.0(无负向提示), 句尾 no text 压不住"把 prompt 中文叙事句渲染成字幕"的毛病。
# 必须在最前面明确: 中文仅为构图描述, 绝不可作为文字绘制到画面中。所有剧本自动套用, 不必逐个改 JSON。
# v2: 追加"物体表面文字"护栏 —— 书籍/报纸/票据/招牌等道具上的假字、镜像字(用户原话"书都是反的")。
TEXT_GUARD = (
    # 【2026-10-29 重写】老版本逐个点名"书籍、报纸、杂志、招牌、包装、照片墙、
    # 九宫格拼贴"—— 结果 i2v 出来满屏报纸拼贴，正是被点名的那几样。
    # cfg=1 的模型读不懂否定，禁词表等于正向提示词。改成只描述"应该是什么样"，
    # 一个被禁的名词都不留。
    "纯实拍的单幅连续画面：所有表面保持材质本来的样子，不承载任何可读符号；"
    "人物可以正常开口说话，声音存在，但说话不会在画面里留下字形；"
    "整张图是一张照片，不是排版页面、不是分格拼贴。"
    "pure photographic single frame, every surface keeps its natural material, "
    "no readable symbols anywhere, speech is audio only and never rendered as glyphs, "
    "one continuous photograph rather than a layout page or a grid of panels."
)

# ---- A 方案: 模型原生发声 (native audio) ------------------------------------
# H3 是音视频联合生成模型: 一次采样同时长出画面 + 语音, 口型和台词天然同步,
# 不需要 edge-tts 配音、也不需要 audio guide 去"喂"音频。
# 代价: 画面护栏必须放宽一点 —— 原来连"台词"两个字都不许出现, 现在允许人物真的说话,
#       只禁止把台词画成字幕/文字。
TEXT_GUARD_SPEECH = (
    # 同 TEXT_GUARD：只描述"应该是什么样"，不点名任何被禁的物体（点名＝提示）。
    # 这里额外交代"人物可以说话、但话只以声音存在"，A 方案要靠它出口型。
    "纯实拍的单幅连续画面：所有表面保持材质本来的样子，不承载任何可读符号；"
    "人物可以正常张口说话，口型与表情自然，说出来的话只作为声音存在，"
    "不会在画面里留下任何字形；整张图是一张照片，不是排版页面、不是分格拼贴。"
    "pure photographic single frame, every surface keeps its natural material, "
    "no readable symbols anywhere; people may talk with natural lip movement, "
    "their words exist as sound only and never appear as glyphs on screen; "
    "one continuous photograph, not a layout page, not a grid of panels."
)
VAE_AUDIO_NAME = "minimax_h3_audio_vae_fp32.safetensors"

# 每个说话人的音色描述: 同一角色在 16 个镜头里音色要一致, 否则听起来像换人。
VOICE_DESC = {
    "李嬢嬢": "六十岁上下的成都老太太嗓音，略沙哑，成都口音，语速偏慢，带着一点不耐烦",
    "小周": "二十五岁左右的男青年嗓音，清亮，普通话里带一点成都腔，语气轻快浮夸",
    "旁白": "沉静的成年女性画外音，成都口音，语速平缓，像在讲一件旧事",
    # —— 武侠《枫林渡》角色音色（按需用，不影响其它剧本）——
    "苏云舟": "二十岁出头的青年男子嗓音，声线偏低偏冷，语速沉稳，带着三年隐忍后的平静与克制，不怒不躁，念白清晰",
    "厉枭": "四十岁上下的中年男子嗓音，沙哑凶狠，语速凌厉，带着居高临下的轻蔑与戾气，念白带狠劲",
    "师父": "苍老温和的老年男子嗓音，语速缓慢，如长者临终叮嘱，气息平缓",
}
VOICE_FALLBACK = "与画面人物年龄、性别一致的自然真人嗓音"
# 出厂音色表快照：每部戏开跑前从它重置，免得上一部剧的角色音色表残留到下一部
VOICE_DESC_BASE = dict(VOICE_DESC)

# H3 的帧数只能落在 17k+5 网格上(5/22/39/56/73/90/107/124…)：
# latent 的音频流按 round(fc/24*40) 算长度，fc 不对齐会让视频/音频两条流的时间轴
# 对不上，采样器直接抛 "shape ... is invalid for input of size ..."。
ALIGN_BASE, ALIGN_OFF = 17, 5


def align_len(n, lo=5):
    """把任意帧数向上对齐到 17k+5 网格（58 -> 73）。"""
    n = max(lo, int(n))
    while n % ALIGN_BASE != ALIGN_OFF:
        n += 1
    return n


# 说话人标签：早期这里写死了一份名单（旁白|李嬢嬢|小周），换一部剧、换一批角色名就不认识了，
# 于是「秀兰：你爸走那年买的」会被原样念成"秀兰，你爸走那年买的"，一开口就出戏。
# 现在改成通用识别：短名字（≤4 字、不含标点数字）+ 冒号，一律当标签剥掉。
_SPK_RE = re.compile(r"^\s*[【\[]?([^：:\n]{1,8}?)[\s】\]]?[：:]\s*(.+)$", re.S)
_SPK_KNOWN = ("旁白", "画外音", "独白", "内心", "内心独白", "解说", "旁白君")
_SPK_TAIL = ("嬢嬢", "婆婆", "爷爷", "奶奶", "叔叔", "阿姨", "大爷", "婶", "哥", "姐",
             "老板", "店长", "师傅", "师父", "先生", "姑娘")
# who 列写成这些值的镜头，画面里根本没人物 —— 台词只能是画外音
_EMPTY_WHO = ("", "空镜", "空镜·无人物", "空镜无人物", "无人物", "无人", "—", "-", "－")
_WHO_WARNED = set()      # 同一镜的 who/台词不一致只提示一次，别每个函数调一遍刷屏


def _is_empty_who(who):
    w = (who or "").strip()
    return (not w) or w in _EMPTY_WHO or w.startswith("空镜") or w.startswith("无人")


def speaker_prefix(text):
    """台词前的说话人（「秀兰：…」->「秀兰」）；没有标签返回 ""。"""
    t = (text or "").strip()
    m = _SPK_RE.match(t)
    if not m:
        return ""
    name, rest = m.group(1).strip(), m.group(2).strip()
    if not rest:                                  # 「他真的来了：」这种不是标签
        return ""
    if name in _SPK_KNOWN or name.endswith(_SPK_TAIL):
        return name
    if len(name) <= 4 and not re.search(r"[，。！？、,.!?…\-—\d\s]", name):
        return name
    return ""


def clean_speech(text):
    """只留要念的字：剥掉「说话人：」前缀和（停顿）这类括号提示。"""
    t = (text or "").strip()
    if speaker_prefix(t):
        t = _SPK_RE.match(t).group(2).strip()
    t = re.sub(r"[（(][^）)]{0,24}[）)]", "", t)   # （停顿）（笑）不念出来
    return re.sub(r"\s+", " ", t).strip()


def speaker_of(shot):
    """这一镜的台词谁说的 + 是不是画外音。

    空镜/无人物的镜头一律按画外音处理：台词照念，但不许在空镜里凭空安排一张嘴
    （《鞋》第 4/7 镜就是「门板特写 + 人物开口」的来源）。画外音仍用说话人自己的音色。
    """
    who = (shot.get("who") or "").strip()
    named = speaker_prefix(shot.get("line") or "")
    person = named if (named and named not in _SPK_KNOWN) else ""
    if _is_empty_who(who):
        return (person or named or "旁白"), True
    if who in _SPK_KNOWN or who.startswith(("旁白", "画外", "解说")):
        return (person or who), True
    if person and person != who:
        # who 列写的是「画面里有谁」，台词标签写的是「谁在说话」。两者冲突时以台词为准
        # （《鞋》第 1 镜 who=秀兰，台词却是「小满：妈，这鞋你还在擦？」）
        _k = str(shot.get("no") or shot.get("line") or "")
        if _k not in _WHO_WARNED:
            _WHO_WARNED.add(_k)
            print(f"[who] 第{shot.get('no') or '?'}镜 who={who} 与台词标签={person} 不一致，"
                  f"按台词标签走", flush=True)
        return person, False
    return who, False


# ---------------- H3 官方对话语法（2026-10-29 重写） ----------------
# 官方文档写得很明确：台词要写成  「(S1) speaks softly, [Chinese] 台词。」
#   · (S1) 是**稳定的说话人 ID** —— 同一个 ID 在全片复用，音色才不变；换 (S2) 就是另一个人。
#   · [Chinese] 是**语言标签**，不写语言模型会自己猜（猜错就是"老外念中文"的腔）。
#   · 括号前的**表演提示**（speaks softly / shouting）对语气的影响，
#     比堆一百个形容词都大。
#
# 以前我们写的是「画面中的周嵩开口说话，用清澈少年音，清清楚楚地用中文说出…」——
# 既没有稳定 ID，也没有语言标签，每镜都是一大段散文。结果就是模型每镜重新挑一把嗓子：
# 同一角色在第 2 镜是青年音、第 5 镜变童声、第 8 镜又变沙哑大叔。
# 「声音跨镜不一致」的根因就在这里。
_SPEAKER_IDS = {}
_SPK_LOCK = threading.Lock()


def reset_speakers():
    """每部片子开跑前清空，保证 (S1) 从头编号、全片一致。"""
    with _SPK_LOCK:
        _SPEAKER_IDS.clear()


def _speaker_id(name):
    nm = (name or "旁白").strip() or "旁白"
    with _SPK_LOCK:
        if nm not in _SPEAKER_IDS:
            _SPEAKER_IDS[nm] = "S%d" % (len(_SPEAKER_IDS) + 1)
        return _SPEAKER_IDS[nm]


# 表演提示：按台词的标点和用词挑一句（官方示例就是这种短英文短语）
_DELIVERY_RULES = (
    (("！", "!", "吼", "喝", "喊", "怒", "滚"), "shouting, forceful delivery"),
    (("？", "?", "么", "吗", "呢"), "questioning, voice rising at the end"),
    (("…", "低", "轻", "悄", "哑", "沉"), "in a low, quiet, restrained voice"),
)


def _delivery_note(line):
    for keys, note in _DELIVERY_RULES:
        if any(k in line for k in keys):
            return note
    return "calm, steady delivery"


_SIZE_EN = {
    "远景": "a wide establishing shot",
    "全景": "a full-body wide shot",
    "中景": "a medium shot, waist up",
    "近景": "a close shot, chest up",
    "特写": "a close-up on the face",
    "局部特写": "a macro detail shot, no face",
}

# 运镜中文 → 官方镜头运动词汇（英文写成镜头描述，而不是堆在句尾）
_CAM_EN = (
    ("推进", "The camera pushes in with small amplitude at slow speed."),
    ("推近", "The camera pushes in with small amplitude at slow speed."),
    ("拉远", "The camera pulls out with small amplitude at slow speed."),
    ("环绕", "The camera arcs around the subject at slow speed."),
    ("上升", "The camera pedestals up slowly."),
    ("下降", "The camera pedestals down slowly."),
    ("摇", "The camera pans with the subject at slow speed."),
    ("横移", "The camera trucks with the subject at slow speed."),
    ("手持", "The camera is handheld with slight shake, following the action."),
    ("固定", "The camera holds a static shot."),
)


def _cam_en(motion):
    t = motion or ""
    for k, v in _CAM_EN:
        if k in t:
            return v
    return "The camera holds a steady shot with subtle movement."


def dialogue_en(shot):
    """官方对白语法的英文版：(S1) <表演提示>, [Chinese] 台词。"""
    line = clean_speech(shot.get("line"))
    if not line:
        return ""
    who, offscreen = speaker_of(shot)
    sid = _speaker_id(who)
    note = _delivery_note(line)
    if offscreen:
        return (f"({sid}) speaks in an off-screen voiceover, {note}, "
                f"while lips remain completely closed: [Chinese] {line}")
    return f"({sid}) says, {note}: [Chinese] {line}"


def speech_clause(shot):
    """A 方案：按 H3 官方语法把台词写进 prompt，让模型自己念（不画成字幕）。"""
    line = clean_speech(shot.get("line"))
    if not line:
        return ""
    who, offscreen = speaker_of(shot)
    sid = _speaker_id(who)
    note = _delivery_note(line)
    # 音色描述保留，但只作为"表演"的补充，不再承担"指定声音"的职责
    desc = (VOICE_DESC.get(who) or VOICE_DESC.get((shot.get("who") or "").strip())
            or VOICE_FALLBACK)
    if offscreen:
        return (f"{sid} is off-screen, mouth not visible, {note}, "
                f"[Chinese] {line}")
    return (f"{sid} is on screen, lips move in sync with the voice, {note}, "
            f"[Chinese] {line}　（{desc}；只说这一句，一个字都不多加）")


def mute_speech_clause(shot):
    """B 方案（后期配音）：不能让画面「闭嘴演哑剧」。

    关掉原生发声后，台词不再进 prompt，模型就完全不安排嘴部动作 —— 成片看起来像
    「一个人在发呆，旁边飘着一段配音」。这里只要求「嘴在动 + 表情跟着情绪」，
    不写具体字句（写了大模型会真的出声，还会把话画成字幕）。
    """
    line = clean_speech(shot.get("line"))
    if not line:
        return ""
    if speaker_of(shot)[1]:          # 旁白 / 空镜画外音：画面里的人别跟着动嘴
        return "这一镜是画外旁白：画面里的人不开口，继续做手里的动作。"
    return ("人物正在开口讲话：嘴唇自然开合、下颌和脸颊随语气轻微起伏，"
            "表情跟着这句话的情绪走；画面里不出现任何文字、字幕或对白条。"
            "the character is talking: lips part and close naturally, "
            "jaw and cheeks move subtly with the tone, no on-screen text.")


# I2V(图生视频) 模式: 用盘里已有的好关键帧作为每镜首帧, H3 在其上生成真实运动。
# 不重新生成构图(彻底避开越白/模糊/镜像字), 只加运动 -> 真视频。
# 默认就用用户选定的「雨夜古镇 6 镜好图」(output/drama_1792714570_s1~s6_00001_.png, 均 576x1024)。
I2V_IMAGES_DEFAULT = [
    os.path.join(OUTDIR, f"drama_1792714570_s{n}_00001_.png") for n in range(1, 7)
]
# I2V 在首帧基础上加的运动引导(构图已由首帧定, 这里只描述期望的运动)
# 只描述"要有持续动态"，不指定运镜 —— 运镜由每镜自己的 prompt 定
# （旧版写死"缓缓推进/微风拂动枝叶"，在灶台、室内镜头里是错的，还会和"镜头稳定"打架）
MOTION_SUFFIX = (
    "画面持续有自然流畅的动态，不要静止定格，不要跳切；"
    "subtle continuous cinematic motion, natural movement, "
    "no freeze frame, no abrupt cut, no static image, "
)
# I2V 默认剧本(与上面 6 张好图同源, 这样台词/动作对得上画面)
I2V_SCRIPT_DEFAULT = os.path.join(WORKDIR, "manju_studio/scripts/雨落在老镇的石街上.json")


def post(path, data):
    req = urllib.request.Request(URL + path,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def build_shots_workflow(prefix, shots, steps, consist="relay", cfg=1.0, turbo=False, i2v_images=None,
                         seek=False, seek_mode="auto", fast_i2v=False, native_audio=False):
    """整条分镜链装入同一条 prompt(模型只加载一次)。
    GGUF + latent relay + 延迟 VAE 解码(本机验证过的稳定接线)。
    turbo=True: 叠加官方 MiniMaxH3 Turbo LoRA(4/8步蒸馏) + SigmaShift + TurboSampler,
                采样步数 18->8(默认; 4-8 步区间, README/教程均推荐 6-8 步, 4 步易有拖影),
                strength 固定 1.0(官方 README: tuned for 1.0, 切勿调低)。
    seek=True: 叠加 seekattention(教程 P12/P14 结论: 官方最推荐的加速方式, 几乎不损画质),
               用 KJNodes 的 PathchSageAttentionKJ 节点 hook 注意力后端(optimized_attention_override)。
               RTX 3060 是 Ampere 架构、不支持 fp8, 故默认 sageattn_qk_int8_pv_fp16_cuda
               (int8 量化 QK + fp16 PV + fp32 累加); 其他可用模式见 KJNodes 节点下拉。
    i2v_images: 图生视频模式, 传[按镜序的关键帧路径]; 每镜用该图作首帧, H3 在其上加真实运动。
                此时强制各镜独立渲染(不走 latent 接力, 否则首帧会被覆盖)。
    返回 (workflow, save_nodes)。
    """
    LEN_ = max(5, int(LEN))
    use_i2v = bool(i2v_images)
    if use_i2v:
        # I2V: 每镜用各自关键帧作首帧, 不需要 latent 接力(接力会把首帧覆盖成上一镜末帧), 强制独立渲染
        consist = "off"
    use_latent_relay = (not use_i2v) and LATENT_RELAY and consist in ("relay", "loop")
    wf = {
        "1": ({"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": GGUF_UNET}} if USE_GGUF
              else {"class_type": "UNETLoader", "inputs": {"unet_name": GGUF_UNET, "weight_dtype": "default"}}),
        "2": ({"class_type": "ClipProjLoader", "inputs": {
            "clip_name": TE4B_NAME, "type": "auto", "projection": CLIPPROJ_NAME,
            "device": CLIPPROJ_DEVICE, "mode": CLIPPROJ_MODE}}
              if USE_CLIPPROJ else
              {"class_type": "CLIPLoader", "inputs": {
                  "clip_name": TE_NAME, "type": "minimax", "device": "default"}}),
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
    }
    if native_audio:
        # A 方案要额外解一条音频流: 音频 VAE(fp32, 很小) 单独加载一次
        wf["4"] = {"class_type": "VAELoader", "inputs": {"vae_name": VAE_AUDIO_NAME}}
    # seekattention(官方最推荐加速, 几乎不损画质): 用 KJNodes 的 PathchSageAttentionKJ 节点
    # hook 注意力后端(optimized_attention_override)。RTX 3060 是 Ampere, 不支持 fp8,
    # 故默认 sageattn_qk_int8_pv_fp16_cuda(int8 量化 QK, fp16 PV, fp32 累加)。
    seek_model = ["1", 0]
    if seek:
        wf["30"] = {"class_type": "PathchSageAttentionKJ", "inputs": {
            "model": ["1", 0], "sage_attention": seek_mode}}
        seek_model = ["30", 0]
    for i, s in enumerate(shots):
        b = (i + 1) * 100
        guard = TEXT_GUARD_SPEECH if native_audio else TEXT_GUARD
        prompt = guard + (s.get("prompt", "") or "")
        speak = speech_clause(s) if native_audio else mute_speech_clause(s)
        if native_audio:
            # 官方三段式：integrated_multimodal_description / overall_soundscape /
            # non_diegetic_music。后两段不写，模型就会自己补环境噪和配乐，
            # 叠在台词上听不清（这也是"声音糊成一团"的一半原因）。
            prompt = (prompt + "\n" + speak
                      + "\noverall_soundscape: ambience of the scene only, "
                        "natural room tone, light wind, nothing else."
                      + "\nnon_diegetic_music: None.")
        # 每镜帧数可按台词长度单独给（s["len"]）；没给就用全局 LEN。
        # 一律向上对齐到 17k+5，否则音视频两条 latent 时间轴对不上（采样器直接报错）。
        shot_len = align_len(int(s.get("len") or LEN_))
        if use_latent_relay:
            # 注意：prompt 里已经含 speak（上面按官方三段式拼好了），
            # 这里再拼一次会让每句台词在 prompt 里出现两遍 —— 模型会念两遍/念串。
            relay_inputs = {"clip": ["2", 0], "prompt": prompt,
                            "width": W, "height": H, "length": shot_len}
            if consist == "relay" and i > 0:
                # 上一镜采样器 LATENT 输出 -> 本镜首帧锚点(视频流末帧, 张量直连, 不经 VAE)
                relay_inputs["prev_latent"] = [str(i * 100 + 9), 0]
            elif consist == "loop" and i > 0:
                # loop: 所有镜(2..N)锚定"第1镜的干净首帧"(节点109=第1镜采样器输出),
                # 斩断"末帧接力"的逐镜白化累积; 且每镜首帧都=第1镜首帧 -> 首尾一致, 可无缝循环。
                relay_inputs["prev_latent"] = ["109", 0]
                relay_inputs["anchor"] = "first"
            wf[str(b + 4)] = {"class_type": "ManjuLatentRelay", "inputs": relay_inputs}
        else:
            cond_inputs = {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
                           "width": W, "height": H, "length": shot_len}
            if use_i2v and i < len(i2v_images):
                # 图生视频: 把盘里好图作为首帧喂给 H3, 只在其上加运动(不重画构图)
                img = i2v_images[i]
                wf[str(b + 3)] = {"class_type": "LoadImage",
                                  "inputs": {"image": os.path.basename(img)}}
                cond_inputs["first_frame"] = [str(b + 3), 0]
                cond_inputs["prompt"] = prompt + MOTION_SUFFIX + "。" + speak
            # fast_i2v: 用自建轻量节点替代官方 I2V —— 首帧只走 VAE 编码作 keyframe 锚点,
            # 不送 32B 视觉编码器(CPU 上前向, 每镜多 2-4 分钟)。构图由 keyframe 锁死。
            node_type = ("ManjuI2VFast" if (fast_i2v and use_i2v)
                         else "MiniMaxH3ImageToVideo")
            wf[str(b + 4)] = {"class_type": node_type, "inputs": cond_inputs}
        wf[str(b + 6)] = {"class_type": "BasicScheduler", "inputs": {
            "model": seek_model, "scheduler": "simple", "steps": steps, "denoise": 1.0}}
        wf[str(b + 7)] = {"class_type": "RandomNoise", "inputs": {"noise_seed": BASE_SEED + i * 101}}
        # BasicGuider 只有 model + conditioning；H3 是蒸馏模型，没有 CFG 这个口子
        wf[str(b + 8)] = {"class_type": "BasicGuider", "inputs": {
            "model": seek_model, "conditioning": [str(b + 4), 0]}}
        if turbo:
            st = int(steps)
            lora = ("minimax_h3_turbo_4step_prunedfmt.safetensors" if st <= 4
                    else "minimax_h3_turbo_8step_prunedfmt.safetensors")
            shift_v = 6.0 if st <= 4 else 12.0
            # strength 固定 1.0: 官方 MiniMaxH3-Turbo README 明确 "tuned for 1.0", 不可调低
            # (第三方 KJ/Larry LoRA 才用 0.75-0.8, 本项目用的官方 Turbo LoRA 不适用)
            wf[str(b + 12)] = {"class_type": "MiniMaxH3TurboLoRA", "inputs": {
                "model": seek_model, "lora_name": lora, "strength": 1.0, "low_vram": True}}
            wf[str(b + 13)] = {"class_type": "MiniMaxH3TurboSampler", "inputs": {}}
            wf[str(b + 15)] = {"class_type": "MiniMaxH3SigmaShift", "inputs": {
                "model": [str(b + 12), 0], "shift_video": shift_v, "shift_audio": 3.0}}
            wf[str(b + 6)]["inputs"]["model"] = [str(b + 15), 0]
            wf[str(b + 8)]["inputs"]["model"] = [str(b + 15), 0]
            wf[str(b + 9)] = {"class_type": "SamplerCustomAdvanced", "inputs": {
                "noise": [str(b + 7), 0], "guider": [str(b + 8), 0], "sampler": [str(b + 13), 0],
                "sigmas": [str(b + 6), 0], "latent_image": [str(b + 4), 1]}}
        else:
            wf[str(b + 5)] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}}
            wf[str(b + 9)] = {"class_type": "SamplerCustomAdvanced", "inputs": {
                "noise": [str(b + 7), 0], "guider": [str(b + 8), 0], "sampler": [str(b + 5), 0],
                "sigmas": [str(b + 6), 0], "latent_image": [str(b + 4), 1]}}
        aud_samples = None
        if native_audio:
            # A 方案: 同一个 latent 里还长着一条音频流 -> 解出来存 wav, 后面直接当配音轨用。
            # 音频 VAE 很小, 单镜解完就存, 不会把 DiT 挤下卡。
            aud_samples = [str(b + 9), 0]
            if use_latent_relay and i + 1 < len(shots):
                # 接力模式是"全部采完才解码", 音频解码也要跟着等, 否则第一镜解完就把 DiT 踢走
                ag = str(b + 19)
                wf[ag] = {"class_type": "ManjuLatentGate", "inputs": {
                    "latent": aud_samples, "gate": [str(len(shots) * 100 + 9), 0]}}
                aud_samples = [ag, 0]
            wf[str(b + 17)] = {"class_type": "VAEDecodeAudio", "inputs": {
                "samples": aud_samples, "vae": ["4", 0]}}
        if use_latent_relay:
            continue  # 不 decode: latent 留给循环后的统一 VAE 解码(VAE 只上卡一次)
        wf[str(b + 10)] = {"class_type": "VAEDecode", "inputs": {"samples": [str(b + 9), 0], "vae": ["3", 0]}}
        img_ref = [str(b + 10), 0]
        if native_audio:
            # 音轨落盘节点顺带透传画面 -> SaveImage 依赖它, 保证一定被执行
            # (自带的 SaveAudioMP3 是 v3 节点, 执行器不认它的 is_output_node, 会被剪掉)
            wf[str(b + 18)] = {"class_type": "ManjuSaveAudio", "inputs": {
                "audio": [str(b + 17), 0], "passthrough": [str(b + 10), 0],
                "filename_prefix": f"{prefix}_a{i+1}"}}
            img_ref = [str(b + 18), 0]
        wf[str(b + 11)] = {"class_type": "SaveImage", "inputs": {
            "images": img_ref, "filename_prefix": f"{prefix}_s{i+1}"}}
    save_nodes = {}
    if use_latent_relay:
        # 延迟解码: 所有镜采样完成后 VAE 才上卡(此时 DiT 被允许卸载无所谓), 逐镜 VAEDecode -> SaveImage。
        # 关键: 用 ManjuLatentGate 把每个解码(除末镜)的 gate 指向最后一镜 sampler,
        # 强制"所有解码都等所有采样完成", 避免第一镜采完立刻解码把 DiT 踢出。
        n_shots = len(shots)
        last_sampler = [str(n_shots * 100 + 9), 0]
        for i in range(n_shots):
            b = (i + 1) * 100
            samples_link = [str(b + 9), 0]
            if i + 1 < n_shots:
                g = str(b + 16)
                wf[g] = {"class_type": "ManjuLatentGate", "inputs": {
                    "latent": samples_link, "gate": last_sampler}}
                samples_link = [g, 0]
            wf[str(b + 10)] = {"class_type": "VAEDecode", "inputs": {
                "samples": samples_link, "vae": ["3", 0]}}
            img_ref2 = [str(b + 10), 0]
            if native_audio and str(b + 17) in wf:
                wf[str(b + 18)] = {"class_type": "ManjuSaveAudio", "inputs": {
                    "audio": [str(b + 17), 0], "passthrough": [str(b + 10), 0],
                    "filename_prefix": f"{prefix}_a{i+1}"}}
                img_ref2 = [str(b + 18), 0]
            wf[str(b + 11)] = {"class_type": "SaveImage", "inputs": {
                "images": img_ref2, "filename_prefix": f"{prefix}_s{i+1}"}}
            save_nodes[i] = str(b + 11)
    return wf, save_nodes


LAST_AUDIO = {}   # A 方案: wait_done 期间收到的模型原生音轨 {镜序号(0基): mp3 路径}


def wait_done(pid, n_shots, prefix, timeout=None, lens=None, native_audio=False):
    """等整条链跑完。lens=[每镜帧数]，用来算真实总帧数和逐镜进度日志。

    native_audio=True 时顺带收每镜的 SaveAudioMP3 产物（模型自己念的台词）。
    """
    # 超时按镜数放宽：实测单镜（GGUF+低显存）可能 10-20 分钟，别再用固定 90 分钟误判
    if not timeout:
        timeout = max(30 * 60, n_shots * 20 * 60)
    lens = [int(x) for x in (lens or [LEN] * n_shots)][:n_shots]
    while len(lens) < n_shots:
        lens.append(LEN)
    announced = set()
    deadline = time.time() + timeout
    frames_by_shot = {i: [] for i in range(n_shots)}
    audio_by_shot = {}
    while time.time() < deadline:
        # 用户点了「停止」：立刻抛 Stopped，不再等 90 分钟
        if stopflag.is_set():
            print("[STOP] 收到停止请求，放弃等待当前渲染")
            raise stopflag.Stopped("user stop")
        # 真实进度：数已落盘的帧（H3 每出一帧就存一张 png，比假进度可靠）
        try:
            made = len(glob.glob(os.path.join(OUTDIR, f"{prefix}_s*_*.png")))
            tot = max(1, sum(lens))
            used = int(time.time() - (deadline - timeout))
            # 逐镜报进度：某一镜的帧齐了就立刻打一行，别让日志 20 分钟不动
            for k in range(n_shots):
                if k in announced:
                    continue
                got = len(glob.glob(os.path.join(OUTDIR, f"{prefix}_s{k + 1}_*.png")))
                if got >= lens[k]:
                    announced.add(k)
                    print(f"[i2v] 第 {k + 1}/{n_shots} 镜完成（{got} 帧）· "
                          f"已用 {used // 60}分{used % 60:02d}秒", flush=True)
            cur = len(announced)
            # 用实测速率算剩余时间：帧数跑得越多越准，比固定 8.5 秒/帧靠谱
            eta = ""
            if made >= 8 and used >= 30:
                per = used / float(made)
                left = max(0.0, (tot - made) * per)
                eta = f" · 剩余约 {int(left // 60)}分{int(left % 60):02d}秒"
            PG.emit("视频渲染", 0.20 + 0.60 * min(1.0, made / float(tot)),
                    f"已完成 {cur}/{n_shots} 镜（整链一起跑，镜号不分先后）· {made}/{tot} 帧 · "
                    f"已用 {used // 60}分{used % 60:02d}秒{eta}")
        except Exception:
            pass
        try:
            with urllib.request.urlopen(URL + "/history/" + pid, timeout=20) as r:
                hist = json.loads(r.read().decode())
        except Exception:
            time.sleep(2)
            continue
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            errs = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
            if errs:
                print("[ERROR] execution error:")
                print(json.dumps(errs[0][1], ensure_ascii=False, indent=2)[:1500])
                return None
            if status.get("completed"):
                for nid, out in entry.get("outputs", {}).items():
                    for img in out.get("images", []):
                        fn = img.get("filename"); sub = img.get("subfolder", "")
                        ftype = img.get("type", "output")
                        base = OUTDIR if ftype == "output" else WORKDIR
                        fp = os.path.join(base, sub, fn) if sub else os.path.join(base, fn)
                        m = re.search(r"_s(\d+)_", fn)
                        if m:
                            frames_by_shot[int(m.group(1)) - 1].append(fp)
                    for aud in (out.get("audio", []) or []):
                        fn = aud.get("filename", ""); sub = aud.get("subfolder", "")
                        ftype = aud.get("type", "output")
                        base = OUTDIR if ftype == "output" else WORKDIR
                        fp = os.path.join(base, sub, fn) if sub else os.path.join(base, fn)
                        m = re.search(r"_a(\d+)_", fn)
                        if m and fp.lower().endswith((".mp3", ".flac", ".wav", ".m4a", ".opus")):
                            audio_by_shot[int(m.group(1)) - 1] = fp
                break
        # 分片 sleep，停止时 0.4s 内就能跳出
        if stopflag.sleep(3):
            raise stopflag.Stopped("user stop")
    # 兜底: 若 history 没给全, 用 glob
    for i in range(n_shots):
        if not frames_by_shot[i]:
            pat = os.path.join(OUTDIR, f"{prefix}_s{i+1}_*.png")
            frames_by_shot[i] = sorted(glob.glob(pat))
        if native_audio and i not in audio_by_shot:
            cand = []
            for ext in ("wav", "mp3", "flac"):
                cand += sorted(glob.glob(os.path.join(OUTDIR, f"{prefix}_a{i+1}_*.{ext}")))
            if cand:
                audio_by_shot[i] = cand[-1]
    LAST_AUDIO.clear()
    LAST_AUDIO.update(audio_by_shot)
    if native_audio:
        got = sum(1 for i in range(n_shots) if i in audio_by_shot)
        print(f"[native] 模型原生音轨 {got}/{n_shots} 镜"
              f"{'' if got == n_shots else '（缺的镜会用静音垫着）'}", flush=True)
        try:
            json.dump({str(k + 1): v for k, v in sorted(audio_by_shot.items())},
                      open(os.path.join(OUTDIR, f"{prefix}_native_audio.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception:
            pass
    return frames_by_shot


def main():
    global LEN, STEPS, W, H, USE_CLIPPROJ, CLIPPROJ_MODE, GGUF_UNET
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=DEFAULT_SCRIPT, help="剧本 JSON 路径")
    ap.add_argument("--shots", type=int, default=0, help="只生成前 N 镜 (0=全部)")
    ap.add_argument("--length", type=int, default=LEN, help="每镜视频帧数 @24fps (默认25≈1s)")
    ap.add_argument("--steps", type=int, default=STEPS, help="采样步数(默认18)")
    ap.add_argument("--consist", default="relay", choices=["relay", "loop", "off"],
                    help="relay=末帧接力(跨镜一致, 但会逐镜累积白化); "
                         "loop=所有镜锚定第1镜干净首帧(斩断白化+首尾一致可循环); off=各镜独立")
    ap.add_argument("--turbo", action="store_true",
                    help="叠加官方 MiniMaxH3 Turbo LoRA(4-8步蒸馏), 约 4-5x 提速, 默认 steps=8(6-8步画质优于4步)")
    ap.add_argument("--seek", action="store_true",
                    help="叠加 seekattention(教程P12/P14: 官方最推荐加速, 几乎不损画质), 用 KJNodes 节点 hook 注意力后端")
    ap.add_argument("--seek-mode", default="auto",
                    help="seekattention 模式(默认适配 RTX3060 的 int8_qk/fp16_pv; 其他见 KJNodes 下拉)")
    ap.add_argument("--prefix", default="manju_shot", help="输出文件名前缀")
    ap.add_argument("--i2v", action="store_true",
                    help="图生视频: 用盘里好关键帧作每镜首帧, H3 在其上加真实运动(不重画构图)")
    ap.add_argument("--i2v-images", default="",
                    help="I2V 关键帧路径(逗号分隔, 按镜序); 缺省用雨夜古镇 6 镜好图")
    ap.add_argument("--fast-i2v", action="store_true",
                    help="I2V 快速模式: 首帧只走 VAE 编码, 跳过 32B 视觉编码器(每镜省 2-4 分钟)")
    ap.add_argument("--unet", default="",
                    help="扩散模型 GGUF 文件名(默认 Q4_K; 12G 卡可用 Q3_K 全塞进显存)")
    ap.add_argument("--te4b", action="store_true",
                    help="轻量文本编码器: Qwen3-VL-4B + ClipProj 投影(15.7G->4.9G, 编码从 CPU 换到 GPU)")
    ap.add_argument("--clipproj-mode", default=CLIPPROJ_MODE,
                    choices=["streaming", "resident", "dynamic"],
                    help="streaming=编码时上卡编完退回(12G卡推荐); resident=常驻显存; dynamic=逐层换页")
    ap.add_argument("--width", type=int, default=0, help="输出宽度(32 的倍数), 默认 576")
    ap.add_argument("--height", type=int, default=0, help="输出高度(32 的倍数), 默认 1024")
    ap.add_argument("--native", action="store_true",
                    help="A 方案: 台词写进 prompt, 让模型自己发声(音画同源, 口型天然同步), 同时导出每镜 mp3")
    a = ap.parse_args()
    LEN, STEPS = a.length, a.steps
    if a.unet:
        GGUF_UNET = a.unet
    USE_CLIPPROJ = bool(a.te4b)
    CLIPPROJ_MODE = a.clipproj_mode
    if a.width:
        W = a.width
    if a.height:
        H = a.height
    if a.turbo and STEPS == 18:
        STEPS = 8  # turbo 默认 8 步(6-8 步画质明显优于 4 步, 且避免 4 步 motion smear/ghosting)

    # I2V 模式: 选好图 + 同源剧本, 并把图复制到 ComfyUI input 目录(LoadImage 只认 input 下文件)
    i2v_images = None
    if a.i2v:
        if a.script == DEFAULT_SCRIPT:
            a.script = I2V_SCRIPT_DEFAULT  # 默认用与好图同源的剧本(台词/动作对得上画面)
        srcs = [s.strip() for s in a.i2v_images.split(",") if s.strip()] or I2V_IMAGES_DEFAULT
        import shutil
        i2v_images = []
        for idx, sp in enumerate(srcs):
            if not os.path.exists(sp):
                print("[ERROR] I2V 源图不存在:", sp); sys.exit(1)
            dst = os.path.join(I2V_INPUT_DIR, f"i2v_s{idx + 1}.png")
            shutil.copy(sp, dst)
            i2v_images.append(dst)
        print(f"[i2v] 已就绪 {len(i2v_images)} 张关键帧 -> {I2V_INPUT_DIR}/i2v_sN.png")

    if not os.path.exists(a.script):
        print("[ERROR] script not found:", a.script); sys.exit(1)
    data = json.load(open(a.script, encoding="utf-8"))
    shots = data.get("shots", [])
    if a.shots:
        shots = shots[:a.shots]
    if not shots:
        print("[ERROR] no shots in script"); sys.exit(1)
    print(f"[config] {len(shots)} 镜, GGUF={USE_GGUF}, latent_relay={LATENT_RELAY}, "
          f"turbo={a.turbo}, seek={a.seek}({a.seek_mode}), consist={a.consist}, "
          f"i2v={bool(i2v_images)}, cfg=1.0, len={LEN}, steps={STEPS}, {W}x{H}, "
          f"native={a.native}, "
          f"TE={'4B+ClipProj/' + CLIPPROJ_MODE if USE_CLIPPROJ else '32B'}")

    wf, _ = build_shots_workflow(a.prefix, shots, STEPS, a.consist, 1.0, a.turbo, i2v_images,
                                 seek=a.seek, seek_mode=a.seek_mode, fast_i2v=a.fast_i2v,
                                 native_audio=a.native)
    payload = {"prompt": wf, "client_id": CLIENT}
    t0 = time.time()
    try:
        resp = post("/prompt", payload)
    except Exception as e:
        print("[ERROR] submit failed:", e); sys.exit(1)
    pid = resp.get("prompt_id")
    if not pid:
        print("[ERROR] no prompt_id:", resp); sys.exit(1)
    print(f"[submit] 整条分镜链 prompt_id = {pid} ({len(shots)} 镜, 模型只加载一次)")

    frames_by_shot = wait_done(pid, len(shots), a.prefix, native_audio=a.native)
    if frames_by_shot is None:
        print("[FAILED] 执行出错"); sys.exit(1)
    ordered = []
    for i in range(len(shots)):
        fs = sorted(frames_by_shot.get(i, []),
                    key=lambda p: int(re.search(r"_(\d+)_\.", os.path.basename(p)).group(1)))
        ordered.append(fs)
        print(f"  shot {i+1}: {len(fs)} 帧")
    json.dump({"shots": ordered, "fps": FPS, "length": LEN, "steps": STEPS, "prefix": a.prefix},
              open(os.path.join(OUTDIR, "shots_frames.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    done = sum(1 for x in ordered if x)
    print(f"\n[done] shots_frames.json written, 成功 {done}/{len(shots)} 镜, "
          f"耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
