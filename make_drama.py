#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
make_drama.py —— 一键把"剧本 JSON"渲染成竖屏 AI 漫剧（9:16）
============================================================
以后每来一个新剧本，只需：
  1) 照 drama_template.json 填好一份新 JSON（每镜写 key / prompt / line）
  2) 运行：  python make_drama.py --script 你的剧本.json
  3) 等它跑完 -> output/<前缀>_drama.mp4

全自动四步（中间产物都在 output/，可单独跳过）：
  ① Z-Image-Turbo 出每镜统一风格关键帧      (key  -> 构图/影调/风格)
  ② MiniMaxH3 图生视频(I2V)：关键帧作首帧    (prompt -> 只在上面加真实运动)
  ③ edge-tts 生成每镜中文旁白                (line -> 配音)
  ④ build_manju_mp4.py 拼成带旁白的竖屏成片   (按旁白时长对轨，旁白=视频长度，零填充零冻结)

速度：默认开 --turbo（官方 8 步蒸馏 LoRA，约 4–5x 提速）。
如需更慢但更稳，加 --no-turbo（18 步）。

剧本 JSON 格式（详见 drama_template.json）：
{
  "title": "第一颗香蕉是怎么来的",
  "voice": "zh-CN-XiaoxiaoNeural",
  "shots": [
    {"key":    "Z-Image 关键帧 prompt（决定构图/影调/风格，建议写死统一影调）",
     "prompt": "H3 图生视频运动 prompt（只描述运动，构图已由 key 定，会自动加电影感运镜）",
     "line":   "这一镜的中文旁白台词（建议 ≤ 5 秒，太长会触发缓推补帧）"}
  ]
}

常用开关：
  --script PATH       剧本路径（必填）
  --prefix NAME       输出前缀（默认用 title 拼音化，或 drama）
  --length N          每镜视频帧数 @24fps（默认 90 ≈ 3.75s；网格会自动向上取整）
  --turbo / --no-turbo  官方加速（默认开）
  --skip-keys         跳过①（已有好关键帧，复用 output/<prefix>_key_N_*.png）
  --skip-video        跳过②（复用已有 shots_frames.json）
  --skip-voice        跳过③（复用已有 <prefix>_voice_map.json）
  --only 2,4          只重做指定镜号（对①②③统一生效，产出这 N 镜的小样）

依赖：本机 ComfyUI 已在 127.0.0.1:8188 运行；edge-tts 装在运行此脚本的 venv 里。
"""
import os
import hashlib, sys, json, time, shutil, subprocess, argparse, glob, re

# ---- 避开本机 127.0.0.1 代理（否则 localhost 请求会被代理拦成 502）----
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gen_zimage_keys as Z          # Z-Image 关键帧（stdlib only）
import gen_manju_shots as H3         # MiniMaxH3 图生视频（stdlib only）
import gen_banana_voice as VT        # edge-tts 配音
import stopflag                      # 全局「停止」开关
import progress as PG                # 进度通道（渲染线程 -> GUI 进度条）

OUTDIR = H3.OUTDIR


def _cfg(key, default=None):
    """读 config.json（exe 同目录 / 源码目录 / 上一级 / 当前目录，先命中先用）。

    踩过的坑：这里曾经写 AP.CFG.get(...)，但本文件根本没有 import app_paths ——
    每次都是 NameError，被外面的 try/except 吞掉，于是 face_paste / key_candidates /
    r2v_steps 这些开关**从来没生效过**，全是默认值在跑。配置文件就在 exe 旁边，
    直接读就行，不必依赖别的模块。
    """
    try:
        base = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                               else os.path.abspath(__file__))
    except Exception:
        base = os.getcwd()
    for p in (os.path.join(base, "config.json"),
              os.path.join(os.path.dirname(base), "config.json"),
              os.path.join(os.getcwd(), "config.json")):
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    return json.load(f).get(key, default)
        except Exception:
            continue
    return default

# ---- A 方案音色锁定 --------------------------------------------------------
# 模型原生发声时，音色不由 edge-tts 决定，只由 prompt 里那段"嗓音描述"决定。
# 不写描述 = 每镜由模型随机挑一把嗓子，同一角色在 9 个镜头里能换 9 次声 ——
# 这是 A 方案听起来"像配音演员串戏"的唯一原因，不是模型不行。
# 下面这张表把 GUI 里已经排好的 edge-tts 音色档案（voice + 语速 + 音高）
# 翻译成模型听得懂的嗓音描述，做到"GUI 里排的是谁，成片里就是谁"。
_EDGE_VOICE_DESC = {
    "zh-CN-XiaoxiaoNeural":   "成年女声，中音区，温柔沉稳，尾音轻",
    "zh-CN-XiaoyiNeural":     "十八九岁的清亮少女声，声线偏高偏细，语调轻快",
    "zh-CN-YunxiNeural":      "二十岁出头的清朗青年男声，声线偏亮，尾音干净",
    "zh-CN-YunyangNeural":    "沉稳男声，中低音，字正腔圆，语气克制",
    "zh-CN-YunjianNeural":    "低沉男声，气息浑厚，语速偏慢，压得住场",
    "zh-CN-YunxiaNeural":     "八九岁男孩的童声，声线细软",
    "zh-CN-liaoning-XiaobeiNeural": "东北口音的成年女声，爽利，尾音上扬",
    "zh-CN-shaanxi-XiaoniNeural":   "陕西口音的成年女声，朴实，语调直",
    "zh-HK-HiuGaaiNeural":    "粤语成年女声，声线清亮",
    "zh-HK-HiuMaanNeural":    "粤语成年女声，声线偏柔",
    "zh-HK-WanLungNeural":    "粤语成年男声，声线中低",
    "zh-TW-HsiaoChenNeural":  "台湾腔成年女声，语调软，尾音拖一点",
    "zh-TW-HsiaoYuNeural":    "台湾腔年轻女声，轻软",
    "zh-TW-YunJheNeural":     "台湾腔成年男声，温和",
}


def _tier_words(prof):
    """语速/音高档位 -> 口语化描述（+0% +0Hz 就不写，免得啰嗦）。"""
    out = []
    try:
        rate = float(str((prof or {}).get("rate") or "+0%").replace("%", "").replace("+", ""))
    except Exception:
        rate = 0.0
    try:
        pitch = float(str((prof or {}).get("pitch") or "+0Hz").replace("Hz", "").replace("+", ""))
    except Exception:
        pitch = 0.0
    if rate <= -10:
        out.append("语速偏慢")
    elif rate >= 10:
        out.append("语速偏快")
    if pitch <= -12:
        out.append("音色偏低")
    elif pitch >= 12:
        out.append("音色偏高")
    return "，".join(out)


def _look_age(look):
    """从统一外貌文案里读年龄，返回「六十岁上下」这种口语说法（读不出返回 ""）。"""
    t = look or ""
    if any(k in t for k in ("七十", "八十", "苍老", "耄耋")):
        return "七十岁上下"
    if any(k in t for k in ("六十", "老妇", "老者", "花白", "白发", "老太")):
        return "六十岁上下"
    if any(k in t for k in ("五十", "中老年", "半百")):
        return "五十岁上下"
    if any(k in t for k in ("四十", "中年")):
        return "四十岁上下"
    if any(k in t for k in ("少女", "十六七", "十五六", "豆蔻")):
        return "十六七岁"
    if any(k in t for k in ("二十", "青年", "少年", "十七八")):
        return "二十岁上下"
    if any(k in t for k in ("孩童", "七八岁", "小孩")):
        return "七八岁"
    return ""


def voice_desc(name, prof, look=""):
    """角色 + 音色档案 -> 一句模型能执行的嗓音描述。"""
    if isinstance(prof, str):
        prof = {"voice": prof}
    base = _EDGE_VOICE_DESC.get((prof or {}).get("voice", ""), "")
    if not base:
        base = "%s的自然真人嗓音" % (look.strip()[:12] or "与人物年龄、性别一致")
    tw = _tier_words(prof)
    desc = base + ("，" + tw if tw else "")
    # edge-tts 的音色是"标准音"，不带年龄；60 岁的老母亲配上"二十多岁的女声"照样出戏。
    # 这里把人物实际年龄补一句，让模型自己把气息、颗粒感往这个年纪上带。
    age = _look_age(look)
    if age:
        desc += f"；人物实际年龄{age}，声音要听得出这个年纪，气息与颗粒感跟着年龄走"
    desc += "；同一角色全片只用这一把嗓子，不换人、不带主持腔"
    return desc


def slug(s):
    """把标题粗略变文件名安全串（中文保留，只去掉路径危险字符）。"""
    return re.sub(r'[\\/:*?"<>|]', "_", s).strip().strip(".") or "drama"


def wait_key(pid, prefix, timeout=900):
    """等 Z-Image 出图，返回关键帧路径；失败回退 glob 最新。"""
    import urllib.request as ur
    t0 = time.time()
    while time.time() - t0 < timeout:
        stopflag.check()
        try:
            with ur.urlopen(Z.COMFY + "/history/" + pid, timeout=20) as r:
                d = json.loads(r.read())
            if pid in d:
                for nid, o in d[pid]["outputs"].items():
                    for f in o.get("images", []):
                        if f["filename"].startswith(prefix):
                            return os.path.join(OUTDIR, f["filename"])
                return True
        except Exception:
            pass
        if stopflag.sleep(3):
            raise stopflag.Stopped("user stop")
    # 回退：glob 最新匹配（这行以前被注释吞进上一行了，"cand" 根本没定义，
    # 一旦超时就抛 NameError，日志里只会看到"未完成（超时）"，查不到真因）
    cand = sorted(glob.glob(os.path.join(OUTDIR, f"{prefix}_*.png")))
    return cand[-1] if cand else None


# ---------------- 续渲（停止后再点开始，已经渲好的不重做）----------------
# 记一份 {prefix}_manifest.json：每个镜头的「提示词+参考图+强度」指纹。
# 指纹没变且成品图还在，就跳过；改了提示词/换了参考图自然重渲，不会拿旧图凑数。
def _man_path(prefix):
    return os.path.join(OUTDIR, f"{prefix}_manifest.json")


def _load_man(prefix):
    try:
        return json.load(open(_man_path(prefix), encoding="utf-8"))
    except Exception:
        return {}


def _save_man(prefix, m):
    try:
        json.dump(m, open(_man_path(prefix), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception:
        pass


# 强动作词：判定"这一镜是不是在打/在砸"。命中就不再走"并排双人底图"
# （见 _stage_keys 里的 _wide 判定）—— 并排底图会把打戏钉成两人站着看镜头。
# 强动作词表（唯一一套）：判"这一镜是不是动作戏"、决定该不该重跑、决定关键帧
# 首帧摆什么姿态，全用它。以前 make_drama 和界面各有一套，各认一半 ——
# 结果"刺来/一挑/穿梭/脱手"这些最常见的武戏写法两头都不认。
_ACTION_WORDS = ("劈", "斩", "刺", "挑", "削", "撩", "崩", "抹", "划", "戳",
                 "砍", "厮打", "交手", "格挡", "翻飞", "碎裂", "血溅", "突进",
                 "贴地", "扑上", "扑出", "扑来", "踹", "相撞", "飞溅", "击飞",
                 "撞飞", "缠斗", "劈落", "猛刺", "刀刃", "甩出", "横刀", "穿梭",
                 "炸开", "迸开", "擦着", "脱手", "跃起", "翻身", "翻滚", "冲上",
                 "挥", "扫", "甩", "拧", "扣住", "揪住", "掐", "砸",
                 "飞回", "接住", "回到掌心", "回旋", "脱手")


# ======================= 动作 → 英文动作短语（Ref2VA 用） =======================
# 【为什么需要这张表】整段 prompt 是英文，只有动作描述是中文。模型对英文动作动词的
# 响应远强于夹在英文堆里的中文词；实测《暮色断桥》第 7 镜写"拔刀"，出片是背刀站着
# （帧间运动量 1.85，接近凝固）。
#
# 【写法上的关键】i2v 模型对**终点状态**的响应比"动作名"强得多。所以每条都写到
# "到最后一帧时身体/器械应该在哪"。只写 "draws the sword" 经常只出一个握刀姿势；
# 写成 "刀身在最后一帧完全出鞘、手臂伸展" 才会真的把刀拔出来。
_ACTION_EN = (
    (("拔刀", "拔剑", "拔出", "拔了", "抽刀", "抽剑", "抽出", "亮出", "出鞘",
      "持刀", "横刀", "握刀", "提刀", "举刀", "拔", "抽刀出鞘"),
     "the hand grips the hilt and pulls the weapon free; by the last frame the blade "
     "has cleared the scabbard completely and the arm is extended, a hard glint running "
     "along the steel"),
    (("劈", "斩", "砍", "劈落"),
     "in one fast sharp motion the weapon snaps from raised overhead down through the "
     "strike; the torso whips with the blow and the blade finishes low within a fraction "
     "of a second, cloth and dust thrown outward by the impact"),
    (("刺入", "刺中", "刺出", "猛刺", "突刺"),
     "a fast explosive forward lunge; the thrust snaps out until the arm is straight and "
     "the whole body has travelled forward with it in a fraction of a second"),
    (("格挡", "架住", "挡住", "相撞", "刀刃"),
     "steel meets steel; both bodies brace against the impact, arms locked, sparks and "
     "chips at the contact point"),
    (("扑上", "扑出", "扑来", "突进", "冲上", "冲向", "奔向", "贴身"),
     "the body launches forward and closes the distance across the frame; feet leave the "
     "ground, cloak and hair trailing behind"),
    (("转身", "回身", "旋身"),
     "the torso rotates through three-quarters until the back faces the camera"),
    # 只收"过程"（跪下去），不收"状态"（单膝跪在/俯身）。
    # 状态词会让模型把一个已经跪好的人再跪一次，而且把静止镜误判成动作镜。
    (("跪下", "跪倒", "蹲下", "跪了下去", "坐倒"),
     "the figure lowers the body down until one knee reaches the ground, weight settling"),
    (("站起", "起身", "撑起", "扶起"),
     "the figure rises from the ground up to full standing height"),
    (("走进", "走入", "走近", "走过来", "上前", "迈步"),
     "the figure walks forward from the background into the foreground, each step visibly "
     "displacing the body across the frame"),
    (("离开", "走出", "离去", "退走"),
     "the figure turns and walks away from the camera, receding until small in frame"),
    (("射出", "箭", "飞刀", "掷", "抛出", "扔出"),
     "the projectile leaves the hand and crosses the frame in one fast arc"),
    (("倒下", "倒地", "栽倒", "瘫"),
     "the body falls; weight collapses downward until it meets the ground"),
    (("血溅", "喷血", "溅"),
     "blood sprays outward in a short arc at the instant of impact"),
    (("掀帘", "推门", "撩开", "推开门"),
     "the hand sweeps the curtain aside and the body steps through it, fabric still "
     "swinging after the passage"),
    (("睁眼", "抬眼", "抬头", "看向", "回头"),
     "the head lifts and the gaze shifts onto the subject, eyes focusing sharply"),
)

# 所有动作镜通用的"正在动"描述。**不能写否定句**（cfg=1 的模型不听"不要静止"），
# 只能正向描述"动起来的样子"。
# 【2026-10-30】必须写速度。只描述动作轨迹时，模型默认按"最平滑"的方式生成 ——
# 实测一场劈砍出来像慢动作回放（动作量 5.44 达标，但那是慢镜头产生的帧差）。
# 正向写实时速度 + 爆发力；不写"不要慢动作"（cfg=1 的模型读不懂否定）。
_ACTION_LIVE = ("The instant is captured mid-movement: limbs are blurred by speed, cloth is "
                "still swinging from the last motion, the stance is off-balance and unsettled "
                "rather than settled. The action plays at real-time speed, fast and explosive; "
                "the whole strike completes in a fraction of a second, with hard motion blur "
                "trailing the moving blade and the body snapping through the arc.")


# 这一镜涉及兵器吗（决定要不要强调"刀在手里"）
_WEAPON_HINT = ("剑", "刀", "刃", "劈", "斩", "刺", "挑", "削", "撩", "格挡",
                "出鞘", "兵刃", "剑锋", "刀刃")


def _weapon_line(text):
    """正向描述"器械此刻在手里"。

    只写"挥刀"不够：实测首帧里刀挂在腰间，模型就让主角空手高举。
    必须把"裸刃握在手上、刃身可见、不在鞘里"写死。
    """
    if not any(k in (text or "") for k in _WEAPON_HINT):
        return ""
    return ("A bare blade is gripped in the hand and clearly visible in frame, "
            "the blade is out of its sheath and moving with the arm, "
            "not hanging at the waist.")


def _action_en(text):
    """从中文画面描述里认出动作，返回英文动作短语（认不出返回空串）。"""
    t = text or ""
    for keys, en in _ACTION_EN:
        if any(k in t for k in keys):
            return en
    return ""


def _is_action(text):
    """这一镜算不算动作镜（用于关键帧姿态和出片后的运动量检查）。"""
    t = text or ""
    if _action_en(t):
        return True
    return any(w in t for w in _ACTION_WORDS)



def _fp(d):
    return hashlib.sha1(json.dumps(d, ensure_ascii=False, sort_keys=True)
                        .encode("utf-8")).hexdigest()[:16]


def _duo_base(fa, fb, wide=False):
    """双人镜底图：把两张定妆照左右贴进同一张画布，再整体重绘一次。

    为什么必须这么做：锚定走的是 img2img，起点图就是那张「单人定妆照」，
    模型天然只会往「一个人」上收 —— 无论文案怎么强调「画面中有两个人」，
    第二个人要么被画成第一个人的脸，要么干脆不出现（实测第 5/6 镜都是这样）。
    把两个人先各自贴进同一张底图，两个人的五官在起点就已经是各自本人，
    一次重绘就能同时保住两张脸，背景/光位由提示词统一。
    """
    try:
        from PIL import Image
    except Exception as e:
        print("[duo] 没有 PIL，跳过拼板：", e, flush=True)
        return ""
    ind = H3.I2V_INPUT_DIR
    pa, pb = os.path.join(ind, fa or ""), os.path.join(ind, fb or "")
    if not (fa and fb and os.path.exists(pa) and os.path.exists(pb)):
        print("[duo] 定妆照缺失，退回单人锚定：%s / %s" % (fa, fb), flush=True)
        return ""
    try:
        ia = Image.open(pa).convert("RGB")
        ib = Image.open(pb).convert("RGB")
        Wd, Hd = int(Z.W), int(Z.H)
        bg = tuple(int((a + b) / 2) for a, b in zip(ia.getpixel((2, 2)),
                                                   ib.getpixel((2, 2))))
        canvas = Image.new("RGB", (Wd, Hd), bg)
        if wide:
            # 宽景/动作镜：一大一小、前后错开 —— 不做等距并排。
            # 等距并排的骨架会被模型原样保留成"两个人并肩站好看镜头"的合影，
            # 打戏和全景都毁在这上面（实测《雨夜断剑》三镜全中）。
            # 这里让近处的人占 72% 高、远处的人 52% 高并贴右下：两张脸都在，
            # 但画面天然带纵深，模型会把小的那个当成"在后面"。
            def _fit_h(im, ratio):
                h = max(64, int(Hd * ratio))
                r = h / float(im.height)
                return im.resize((max(1, int(im.width * r)), h), Image.LANCZOS)
            ia = _fit_h(ia, 0.52)
            ib = _fit_h(ib, 0.40)
            # 不能贴画面两侧边缘 —— 贴边等于把"左右分立"的骨架又写回底图，
            # 模型照抄出来就是西部片对峙（中间空一大块）。往里收，两人略微重叠。
            canvas.paste(ia, (int(Wd * 0.20), max(0, Hd - ia.height)))
            m = Image.new("L", (ib.width, ib.height), 255)
            pxm = m.load()
            for x in range(min(90, ib.width)):          # 内侧 90px 羽化，接缝不留硬边
                v = int(255 * x / 90.0)
                for y in range(ib.height):
                    pxm[x, y] = v
            canvas.paste(ib, (int(Wd * 0.54), max(0, Hd - ib.height)), m)
            name = "duow_%s.png" % hashlib.sha1(
            (fa + "|" + fb).encode("utf-8")).hexdigest()[:10]
        else:
            pw = int(Wd * 0.56)                       # 每人占 56% 宽，中间留出重叠区
            def _fit(im):
                r = pw / float(im.width)
                return im.resize((pw, max(1, int(im.height * r))), Image.LANCZOS)
            ia, ib = _fit(ia), _fit(ib)
            canvas.paste(ia, (0, max(0, Hd - ia.height)))          # 左：贴左边
            m = Image.new("L", (ib.width, ib.height), 255)         # 右：内侧 80px 羽化
            pxm = m.load()
            for x in range(min(80, ib.width)):
                v = int(255 * x / 80.0)
                for y in range(ib.height):
                    pxm[x, y] = v
            canvas.paste(ib, (Wd - ib.width, max(0, Hd - ib.height)), m)
            name = "duo_%s.png" % hashlib.sha1(
                (fa + "|" + fb).encode("utf-8")).hexdigest()[:10]
        out = os.path.join(ind, name)
        canvas.save(out)
        print("[duo] %s底图已拼好 -> %s" % ("宽景错位双人" if wide else "双人", name),
              flush=True)
        return name
    except Exception as e:
        print("[duo] 拼板失败：", e, flush=True)
        return ""


def _cutout_person(path):
    """把定妆照里的人抠出来（透明底）。

    角色定妆照是"中性浅灰纯色背景"上的人物半身像（见 CHAR_DESC_TPL）。
    抠像要用的不是"全局色差阈值"——那样会把**浅色衣服**也当成背景抠掉
    （白衣服和浅灰背景的色差很小，实测整片肩膀都被挖空）。
    正确做法是**从四边向内泛洪**：只有跟画面边缘连通的背景色才算背景，
    人物内部哪怕颜色跟背景一样也不会被误删。
    实现用 numpy 的迭代传播（从边缘种子出发，逐轮往内长），
    不依赖 scipy（这台机器没装）。

    抠不出来（用户自己传了带杂物的照片）返回 None，调用方退回老路。
    """
    try:
        from PIL import Image
        import numpy as np
    except Exception as e:
        print("[scene] 没有 PIL/numpy，跳过抠像：", e, flush=True)
        return None
    try:
        im = Image.open(path).convert("RGB")
        a = np.asarray(im).astype(int)
        h, w = a.shape[:2]
        # 背景色：四边一圈像素的中位数（定妆照背景是均匀浅灰）
        edge = np.concatenate([
            a[0, :, :], a[h - 1, :, :], a[:, 0, :], a[:, w - 1, :]])
        bg = np.median(edge, axis=0)
        diff = np.abs(a - bg).max(axis=2)
        # 与背景色足够接近 → 可能是背景。阈值取 18 而不是 34：
        # 定妆照背景虽然均匀，但人物**贴近画面边缘**（白衣服的肩膀就挨着右边缘），
        # 阈值一松，泛洪就会顺着衣服上的暗部渗进衣服里，把肩膀整片挖空
        # （实测 26 时右肩被误删，18 收紧后干净）。
        close = diff < 18
        # 种子只取**四角**（四边整条都种会被贴边的人物污染）：
        # 定妆照的人像居中，四角一定是背景。
        cs = max(4, min(h, w) // 12)
        bgm = np.zeros((h, w), dtype=bool)
        for _r in (slice(0, cs), slice(h - cs, h)):
            for _c in (slice(0, cs), slice(w - cs, w)):
                blk = close[_r, _c]
                bgm[_r, _c] |= blk
        if bgm.sum() < 16:
            # 四角都不像背景 → 这张图不是纯色背景拍的，放弃抠像
            print("[scene] 四角不是纯色背景，抠像失败", flush=True)
            return None
        for _ in range(max(h, w)):          # 最多扩散 max(h,w) 轮，够贯穿整幅
            nxt = bgm.copy()
            nxt[1:, :] |= bgm[:-1, :] & close[1:, :]
            nxt[:-1, :] |= bgm[1:, :] & close[:-1, :]
            nxt[:, 1:] |= bgm[:, :-1] & close[:, 1:]
            nxt[:, :-1] |= bgm[:, 1:] & close[:, :-1]
            if nxt.sum() == bgm.sum():
                break
            bgm = nxt
        fg = ~bgm                          # 前景＝没被泛洪到的部分
        if fg.mean() < 0.04:
            print("[scene] 几乎没有主体，抠像失败", flush=True)
            return None
        if fg.mean() > 0.92:
            print("[scene] 整张图几乎没有背景，抠像失败（照片本身就是满幅？）", flush=True)
            return None
        from PIL import ImageFilter
        m = Image.fromarray((fg * 255).astype("uint8"))
        m = m.filter(ImageFilter.MinFilter(3))          # 腐蚀：缩一圈，去背景残留
        m = m.filter(ImageFilter.GaussianBlur(1.5))     # 羽化：边缘自然
        out = im.convert("RGBA")
        out.putalpha(m)
        # 裁到主体外接框，别把半张空白贴进场景
        bb = m.point(lambda v: 255 if v > 40 else 0).getbbox()
        if bb:
            out = out.crop(bb)
        return out
    except Exception as e:
        print("[scene] 抠像失败：", e, flush=True)
        return None


def _scene_base(fs, fc):
    """场景 + 角色 的合成底图：把角色抠像贴进场景照，拼成一张再整体重绘。

    为什么需要拼：img2img 只有一个起点图，角色定妆照和场景照只能二选一。
    只用定妆照 → 脸锁住了但背景每镜重画（观众看着像在换地方）；
    只用场景照 → 地点锁住了但脸每镜重画（串脸）。
    把人抠出来贴进场景里，两个信息就都在起点图上了，一次重绘全保住。

    返回合成图文件名；失败返回 ""（调用方退回单参考图）。
    """
    try:
        from PIL import Image
    except Exception as e:
        print("[scene] 没有 PIL，跳过合成：", e, flush=True)
        return ""
    ind = H3.I2V_INPUT_DIR
    ps, pc = os.path.join(ind, fs or ""), os.path.join(ind, fc or "")
    if not (fs and fc and os.path.exists(ps) and os.path.exists(pc)):
        print("[scene] 场景照或定妆照缺失，跳过合成", flush=True)
        return ""
    try:
        Wd, Hd = int(Z.W), int(Z.H)
        # 场景照铺满画幅（居中裁切，不拉伸变形）
        sc = Image.open(ps).convert("RGB")
        r = max(Wd / float(sc.width), Hd / float(sc.height))
        sc = sc.resize((max(1, int(sc.width * r)), max(1, int(sc.height * r))),
                       Image.LANCZOS)
        left = (sc.width - Wd) // 2
        top = (sc.height - Hd) // 2
        canvas = sc.crop((left, top, left + Wd, top + Hd))
        # 人物：贴到画面中央偏下，占大部分高度（半身像起步）
        per = _cutout_person(pc)
        if per is None:
            print("[scene] 抠像失败，只用场景照做底图", flush=True)
            name = "scene_%s.png" % re.sub(r"\W+", "", fs.split(".")[0][-8:])
            canvas.save(os.path.join(ind, name))
            return name
        ph = int(Hd * 0.86)
        pw = max(1, int(per.width * ph / float(per.height)))
        if pw > int(Wd * 0.92):
            pw = int(Wd * 0.92)
            ph = max(1, int(per.height * pw / float(per.width)))
        per = per.resize((pw, ph), Image.LANCZOS)
        # 人物底部对齐画布底部（站在场景里，不是浮在空中）
        canvas.paste(per, ((Wd - pw) // 2, Hd - ph), per)
        # 用 hash 而不是"文件名后 6 位"：同一批生成的角色文件名时间戳相同，
        # 会拼出同一个 scenec_xxx，两个角色共用一张场景底图（串人）。
        name = "scenec_%s.png" % hashlib.sha1(
            (fs + "|" + fc).encode("utf-8")).hexdigest()[:10]
        canvas.save(os.path.join(ind, name))
        print("[scene] 场景+角色底图已拼好 -> %s" % name, flush=True)
        return name
    except Exception as e:
        print("[scene] 合成失败：", e, flush=True)
        return ""


# ======================= 构图优先（先自由构图，再贴脸锁身份） =======================
# 老办法是"拿定妆照当 img2img 起点"：起点就是一张正脸半身棚拍照，模型只会照着它
# 描 —— 于是每一镜都是两个人并排站着看镜头，文案写"力劈而下"也没用。
#
# 新办法分两段：
#   ① 纯文生图（不接任何参考图）→ 构图、机位、动作、景别全部按文案来，脸是随机的；
#   ② 用 OpenCV 找出①里的人脸，把定妆照的脸按框大小贴进去（椭圆羽化），
#      再用很低的 denoise 过一遍 → 只做融合，不改构图。
# 这样"构图"和"身份"就解耦了：构图由文案决定，身份由定妆照决定。
#
# 只用在本机已有的 Z-Image + OpenCV，不引入换脸模型（insightface/inswapper 那套
# 在 Windows + py3.12 上基本装不起来）。
COMPOSE_FIRST = True       # False = 退回老办法（定妆照直接当起点）
# 贴不贴脸（OpenCV 把定妆照的脸硬贴到构图图上，再低 denoise 融合）。
# 关掉＝宽景/动作镜只要构图，不贴脸：脸就是这张图自己长出来的。
# 好处：不会出现"一张方形照片嵌在画面里"；代价：全景里主角的脸和定妆照不是同一张
# （全景里脸只占画面 0.3%~2.5%，远看基本分辨不出，中近景不受影响，走定妆照 img2img）。
try:
    FACE_PASTE = bool(_cfg("face_paste", False))
except Exception:
    FACE_PASTE = False
# ComfyUI 有执行缓存：prompt + seed 完全一样就直接返回上次结果、**不再落盘**。
# 一旦上一轮产出的 png 被删掉（清目录、换机器），就会"任务成功但文件不存在"。
# 每次进程启动给 seed 掺一点盐，日常重跑就不会撞上；万一还是撞上，下面还有落盘重试。
_RUN_SALT = int(time.time()) % 100003
# key_candidates = 「最多试几次」：每出一张先检测（脸数/脸大小/清晰度），
# 达及格线立刻停，不达标才换 seed 重出。所以它不再是"不管好坏都出满 N 张"。
# 关键帧约 40 秒/张；及格线 key_pass_score 默认 56（60 分基础分 ± 清晰度）。
# 想更快：key_candidates 设 1（不重试）；想更稳：设 4~5。
try:
    KEY_CANDIDATES = max(1, min(6, int(_cfg("key_candidates", 3))))
except Exception:
    KEY_CANDIDATES = 3
COMPOSE_DN = 0.36          # 贴脸后的融合强度：只融边界，不动构图


_FACE_WARNED = [False]


def _haar_path():
    """找人脸检测器 xml：打包版在 assets/，源码运行回退到 cv2/data。"""
    cands = []
    try:
        import sys as _s
        if getattr(_s, "_MEIPASS", ""):
            cands.append(os.path.join(_s._MEIPASS, "assets"))
        cands.append(os.path.join(os.path.dirname(os.path.abspath(_s.argv[0])),
                                  "assets"))
    except Exception:
        pass
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"))
    try:
        import cv2
        cands.append(cv2.data.haarcascades)
    except Exception:
        pass
    for d in cands:
        p = os.path.join(d, "haarcascade_frontalface_default.xml")
        if os.path.exists(p):
            return p
    return ""


# 按输入尺寸缓存检测器。**不能用固定尺寸初始化再 setInputSize** ——
# 实测同一张 576×1024 的图：用 320×320 初始化只检出 1 张脸（还是佛像），
# 用原图尺寸初始化检出 2 张（佛像 + 真人）。setInputSize 改不动内部的 anchor 网格。
_YUNET = {}
_YUNET_WARN = [False]


def _yunet(path, maxn=8):
    """OpenCV 自带的 FaceDetectorYN。模型文件 230KB，比 Haar 准一个档次。

    Haar 的毛病很具体：侧脸检不出、人脸小于画面 3% 检不出、两个人只报一个。
    这在"多人打斗"里是致命的 —— 检测漏一张脸，那张脸就会被局部重绘换掉，
    而我们自己还以为是"保护了所有脸"。
    """
    try:
        import cv2
        import numpy as np
        buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
        if img is None:
            return []
        h, w = img.shape[:2]
        key = (w, h)
        det = _YUNET.get(key)
        if det is None:
            mp = _asset_path("face_detection_yunet_2023mar.onnx")
            if not mp:
                return []
            # 小脸（全身景别里人脸只有 30~50 像素）阈值必须放低；
            # 0.6 时连真人都检不出，只剩背景里的大佛像。
            det = cv2.FaceDetectorYN.create(mp, "", (w, h), 0.35, 0.3, 5000)
            det.setInputSize((w, h))
            _YUNET[key] = det
        _, faces = det.detect(img)
        out = []
        if faces is not None:
            for f in faces:
                x, y, bw, bh = f[0], f[1], f[2], f[3]
                out.append((int(max(0, x)), int(max(0, y)), int(bw), int(bh)))
        out.sort(key=lambda t: t[2] * t[3], reverse=True)
        return out[:maxn]
    except Exception as e:
        # 以前这里静默 return []，结果是"YuNet 一直抛异常、一直悄悄退回 Haar"，
        # 表面上只是"检测少了一张脸"，查了很久才发现是 NameError。
        if not _YUNET_WARN[0]:
            _YUNET_WARN[0] = True
            print("[face] YuNet 不可用（%s: %s），退回 Haar 检测 —— 侧脸/小脸可能漏检"
                  % (type(e).__name__, e), flush=True)
        return []


def _asset_path(name):
    """assets/ 里的模型文件：打包版在 _MEIPASS，源码运行在工程目录。"""
    cands = []
    try:
        import sys as _s
        if getattr(_s, "_MEIPASS", ""):
            cands.append(os.path.join(_s._MEIPASS, "assets"))
        cands.append(os.path.join(os.path.dirname(os.path.abspath(_s.argv[0])), "assets"))
    except Exception:
        pass
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"))
    for d in cands:
        p2 = os.path.join(d, name)
        if os.path.exists(p2):
            return p2
    return ""


def _face_boxes(path, maxn=4):
    """OpenCV 找脸，按 x 从左到右排序返回 [(x, y, w, h)]。没有 cv2 就返回 []。"""
    try:
        import cv2
    except Exception:
        return []
    # 先用 YuNet（准），失败再退回 Haar
    _yb = _yunet(path, ) if False else _yunet(path)
    if _yb:
        return _yb
    try:
        # cv2.imread 在 Windows 上读不了中文路径（返回 None）—— 而关键帧文件名
        # 天然带剧名中文（雨夜断剑_key_1_…）。踩过：贴脸/打分/质检全部静默失效，
        # 只留下一条 "can't open/read file" 的警告。必须走 numpy 解码。
        import numpy as np
        buf = np.fromfile(path, dtype=np.uint8)
        if buf.size == 0:
            return []
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            return []
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        cas = cv2.CascadeClassifier(_haar_path())
        if cas.empty():
            # 打包版最容易踩：cv2/data 里的 xml 属于"数据文件"，
            # PyInstaller 不会自动收进来 → 贴脸/打分/质检全部静默失效（全报 0 张脸）。
            if not _FACE_WARNED[0]:
                _FACE_WARNED[0] = True
                print("[face] 找不到人脸检测器 haarcascade（%s），"
                      "贴脸与候选打分将失效" % _haar_path(), flush=True)
            return []
        ms = max(24, int(g.shape[1] / 22))
        bs = cas.detectMultiScale(g, scaleFactor=1.08, minNeighbors=5,
                                  minSize=(ms, ms))
        bs = [tuple(int(v) for v in b) for b in bs]
        bs.sort(key=lambda b: b[0])        # 从左到右，跟文案里的角色顺序对齐
        return bs[:maxn]
    except Exception:
        return []


def _plausible_face(box, w, h):
    """这个框像不像一张脸 —— 贴脸前必须过这一关。

    踩过的坑：构图图左上角一块牌匾被检成 276x430 的"脸"（长宽比 0.64、上边贴到 y=1），
    而"按面积最大优先"正好挑中它，于是把定妆照那一整块（连背景带牌匾带血迹）
    贴到画面上，COMPOSE_DN=0.36 又融不掉，成片里直接嵌着一张方形照片。
    人脸框基本是接近正方形的、不贴画面边缘。
    """
    x, y, bw, bh = box
    if bw <= 0 or bh <= 0:
        return False
    ar = bw / float(bh)
    if not (0.70 <= ar <= 1.45):
        return False
    area = float(bw) * bh
    if not (0.003 * w * h <= area <= 0.30 * w * h):
        return False
    if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
        return False
    cx = (x + bw / 2.0) / w
    if not (0.08 <= cx <= 0.92):
        return False
    return True


def _face_paste(comp_path, ref_files, pad=0.20):
    """把定妆照的脸贴到构图图上，返回新图文件名（写进 ComfyUI 的 input 目录）。

    贴完必须再过一遍低 denoise 的 img2img（调用方负责），椭圆羽化只是让接缝不硬。
    找不到脸就返回 ""，让调用方退回老办法 —— 宁可构图差一点，也不要贴错脸。
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except Exception as e:
        print("[face] 没有 PIL：", e, flush=True)
        return ""
    ind = H3.I2V_INPUT_DIR
    try:
        canvas = Image.open(comp_path).convert("RGB")
    except Exception as e:
        print("[face] 打不开构图图：", e, flush=True)
        return ""
    W, H = canvas.size
    boxes = [b for b in _face_boxes(comp_path, maxn=max(1, len(ref_files)) + 2)
             if _plausible_face(b, W, H)]
    if not boxes:
        print("[face] 构图图里没有像样的人脸（检到的可能是牌匾/被裁的框），跳过贴脸",
              flush=True)
        return ""
    try:
        # 贴哪张脸：**主角必须贴到画面里最大的那张脸**（主角通常就是画面主体），
        # 第二个人再从剩下的里按水平位置挑离主角最远的一侧。
        # 只按 x 从左到右排会贴反 —— 两个角色互换脸比"第二个人不像"严重得多。
        boxes = sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)
        order = [0]
        rest = list(range(1, len(boxes)))
        if rest:
            main_x = boxes[0][0] + boxes[0][2] / 2.0
            rest.sort(key=lambda k: -abs(boxes[k][0] + boxes[k][2] / 2.0 - main_x))
            order += rest
        used = 0
        for ref in ref_files:
            if used >= len(order):
                break
            rp = os.path.join(ind, ref or "")
            if not (ref and os.path.exists(rp)):
                continue
            rimg = Image.open(rp).convert("RGB")
            rb = [b for b in _face_boxes(rp, maxn=3)
                  if _plausible_face(b, rimg.width, rimg.height)]
            if not rb:
                print("[face] 定妆照 %s 没检出像样的人脸，跳过（宁可不贴）" % ref,
                      flush=True)
                continue
            rb.sort(key=lambda b: b[2] * b[3], reverse=True)
            rx, ry, rw, rh = rb[0]
            # 参考脸：bbox 外扩 pad（Haar 只框到眉毛到下巴，外扩才带得住鬓角/下颌）
            fw, fh = rw * (1 + pad), rh * (1 + pad)
            rcx, rcy = rx + rw / 2.0, ry + rh / 2.0
            face = rimg.crop((max(0, int(rcx - fw / 2)), max(0, int(rcy - fh / 2)),
                              min(rimg.width, int(rcx + fw / 2)),
                              min(rimg.height, int(rcy + fh / 2))))
            # 目标框：按上面的优先级挑出来的那张脸
            tx, ty, tw, th = boxes[order[used]]
            dw, dh = tw * (1 + pad), th * (1 + pad)
            tcx, tcy = tx + tw / 2.0, ty + th / 2.0
            face = face.resize((max(8, int(dw)), max(8, int(dh))), Image.LANCZOS)
            mask = Image.new("L", face.size, 0)
            ImageDraw.Draw(mask).ellipse(
                (0, 0, face.size[0] - 1, face.size[1] - 1), fill=255)
            mask = mask.filter(ImageFilter.GaussianBlur(
                max(2, int(min(face.size) * 0.12))))
            canvas.paste(face, (int(tcx - dw / 2), int(tcy - dh / 2)), mask)
            used += 1
        if not used:
            return ""
        # 文件名不能把点去掉 —— 没有扩展名 PIL 存盘会直接报
        # "unknown file extension"，贴脸就白干了（第一版就栽在这）。
        name = "face_%s.png" % re.sub(r"[^0-9A-Za-z_]+", "",
                                      os.path.splitext(os.path.basename(comp_path))[0])
        canvas.save(os.path.join(ind, name), format="PNG")
        print("[face] 已把 %d 张定妆照的脸贴进构图图 -> %s" % (used, name), flush=True)
        return name
    except Exception as e:
        print("[face] 贴脸失败：", e, flush=True)
        return ""


def _t2i_compose(prompt, seed, prefix):
    """第①段：纯文生图，只要构图不要脸。"""
    try:
        wf = Z.build_wf(prompt, seed, prefix, ref="", denoise=1.0)
        pid = Z.submit(wf)
        if not pid:
            return ""
        return wait_key(pid, prefix) or ""
    except Exception as e:
        print("[compose] 自由构图失败：", e, flush=True)
        return ""


# ---- 三视图（正/侧/背全身）：给背影镜、侧身镜提供"正确的身体"当起点 ----
# 定妆照只有正面半身，背影镜拿它当起点必然画成"回头看镜头"。
# 角色库里其实已经生成了「定妆照 | 正面全身 | 侧面全身 | 背面全身」四联图
# （漫剧工作室._compose_sheet 合成），以前只当参考图摆着、生成流程一行都没用到。
#
# 这里做两件事：按姿态词挑对应面板，裁出来当 img2img 起点。
# 三个面板排布：左块是贴上去的定妆照（底色不同），右边三等分。
_POSE_BACK = ("背影", "背对", "背身", "背后", "转身离开", "转身走", "走出门",
              "拂袖而去", "离去")
_POSE_SIDE = ("侧身", "侧面", "侧脸", "斜侧", "侧对", "侧影", "侧立")


def _pose_of(text):
    """这一镜要的是背影还是侧身？返回 "back"/"side"/""。"""
    t = text or ""
    if any(k in t for k in _POSE_BACK):
        return "back"
    if any(k in t for k in _POSE_SIDE):
        return "side"
    return ""


def _sheet_cut(sheet_file, part):
    """从三视图里裁出正/侧/背中的一块，存到 input 目录，返回文件名。

    左块是拼进去的定妆照（背景是另一种灰），先靠"列颜色突变"找到它的右边界，
    剩下的宽度三等分 —— 不依赖固定像素，换画幅也不会错位。
    """
    if not (sheet_file and part in ("front", "side", "back")):
        return ""
    ind = H3.I2V_INPUT_DIR
    src = os.path.join(ind, sheet_file)
    if not os.path.exists(src):
        return ""
    out_name = "cut_%s_%s.png" % (part, re.sub(r"[^0-9A-Za-z]+", "", sheet_file)[-8:])
    out = os.path.join(ind, out_name)
    if os.path.exists(out):
        return out_name
    try:
        from PIL import Image
        im = Image.open(src).convert("RGB")
        W, H = im.size
        bg = im.getpixel((W - 3, 3))          # 最右侧的底色（三视图那一侧）
        left_end = 0
        for x in range(0, W - 6):
            # 连续 6 列都是"外底色"才算真的到头，避开人物头发那种窄条
            if all(max(abs(im.getpixel((x + k, 3))[i] - bg[i])
                       for i in range(3)) <= 8 for k in range(6)):
                left_end = x
                break
        if left_end <= 0:
            left_end = int(W * 0.28)
        rest = W - left_end
        idx = {"front": 0, "side": 1, "back": 2}[part]
        x0 = left_end + int(rest * idx / 3.0)
        x1 = left_end + int(rest * (idx + 1) / 3.0)
        im.crop((x0, 0, x1, H)).save(out, format="PNG")
        print("[sheet] 从三视图裁出 %s 视图 -> %s" % (part, out_name), flush=True)
        return out_name
    except Exception as e:
        print("[sheet] 裁三视图失败：", e, flush=True)
        return ""


def _score_key(path, expect=1, back=False):
    """给一张候选关键帧打分（越高越好）。用 OpenCV 判三件事：

    ① 脸数对不对 —— 双人镜只画了一个人、单人镜冒出三张脸，都是废片；
    ② 脸够不够大 —— 特写镜脸只占 0.3% 说明画成远景了；
    ③ 糊不糊 —— Laplacian 方差太低＝糊成一团。

    背影镜反过来判：出现正脸就扣分（说明又被画成"回头看镜头"了）。
    """
    try:
        import cv2
    except Exception:
        return 0.0
    try:
        import numpy as np
        _buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(_buf, cv2.IMREAD_COLOR) if _buf.size else None
        if img is None:
            return -1.0
        h, w = img.shape[:2]
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        score = 60.0
        lap = float(cv2.Laplacian(g, cv2.CV_64F).var())
        # 除以 30 而不是 8：实测清晰的图 lap 都有 100-600，除以 8 会全部顶到上限 20 分，
        # 三张候选拉不开差距（这轮实测 10 镜里 8 镜三张并列 55 分，等于没比）
        score += max(-10.0, min(20.0, lap / 30.0))
        faces = _face_boxes(path, maxn=8)
        if back:
            score -= 30.0 * len(faces)
        else:
            score -= 25.0 * abs(len(faces) - int(expect))
            if faces:
                bx = max(faces, key=lambda b: b[2] * b[3])
                ratio = (bx[2] * bx[3]) / float(max(1, w * h))
                if ratio < 0.004:
                    score -= 10.0                       # 脸小得像远景
                elif ratio > 0.40:
                    score -= 5.0                        # 脸大到糊住画面
        return score
    except Exception:
        return 0.0


def _big_faces(path, min_ratio=0.003, maxn=8):
    """数"像样的脸"：只算面积 ≥ 画面 0.3% 的框。

    为什么不直接数全部脸：远景/街景镜头里路人、招牌上的人像都会被检出
    （实测一张长街镜头 16 个 0.9 分以上的框，最大的才占 2.5%），
    拿总脸数当闸门会把正常远景判成废片。
    这里只关心"主要人物在不在"——该有人的镜头一张像样的脸都没有，就是画跑了。

    返回 -1 表示检测器本身失败（调用方应当放行，别让检测拖累出图）。
    """
    try:
        import cv2
        import numpy as np
        buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
        if img is None:
            return -1
        h, w = img.shape[:2]
        floor = float(min_ratio) * w * h
        return sum(1 for b in _face_boxes(path, maxn=maxn)
                   if b[2] * b[3] >= floor)
    except Exception:
        return -1


def stage_keys(script, prefix, only, res=None, resume=False, t0=0.0,
               force_skip=False):
    """出每镜关键帧。res=H3 渲染分辨率，关键帧按 4/3 放大出图（细节更足，H3 会缩回首帧）。"""
    if res:
        kw = max(256, int(round(res[0] * 4.0 / 3 / 16)) * 16)
        kh = max(256, int(round(res[1] * 4.0 / 3 / 16)) * 16)
        Z.W, Z.H = kw, kh
        print(f"[key] 关键帧画幅 {kw}x{kh}（对应成片 {res[0]}x{res[1]}）", flush=True)
    shots = script["shots"]
    # 角色定妆照锚定：剧本 refs={角色名: input目录下的文件名}，每镜用 ref 指定是谁，
    # 该镜关键帧就改用 img2img（定妆照当起点）锁住五官，而不是每镜重新长一张脸。
    refs = script.get("refs") or {}
    # 场景照锚定：srefs={场景名: input目录下的文件名}，每镜用 scene 指定在哪个地方，
    # 同一场景所有镜共用这张照 → 背景跨镜一致（不然看着像在换地方）。
    srefs = script.get("srefs") or {}
    # 三视图：{角色名: 四联图文件名}，背影/侧身镜拿它裁出的面板当起点
    sheets = script.get("sheets") or {}
    man = _load_man(prefix)
    kman = man.setdefault("keys", {})
    paths = []
    _gen_paths = {}           # 本次实际产出的关键帧 {镜号: 路径}，给拆镜续段复用
    for i, s in enumerate(shots, 1):
        stopflag.check()          # 每镜之间都可被「停止」打断
        p = os.path.join(OUTDIR, f"{prefix}_key_{i}_00001_.png")
        if only and i not in only:
            paths.append(p)
            continue
        _has_key = (os.path.exists(p) and os.path.getsize(p) > 2048)
        _fresh = (_has_key and (not t0 or os.path.getmtime(p) >= t0))
        if (_fresh and resume) or (_has_key and force_skip):
            if _fresh:
                print(f"[key {i}] 上一轮已出图，续渲跳过 -> {os.path.basename(p)}", flush=True)
            else:
                print(f"[key {i}] 已渲过，跳过（跳过已渲镜：不比对本轮改动）"
                      f" -> {os.path.basename(p)}", flush=True)
            paths.append(p)
            _gen_paths[i] = p
            continue
        # 续段镜（同一画面被拆成多段台词）：直接复用主镜的关键帧，不重新出图。
        # A 方案一镜只能念 5.2 秒，长台词必须拆；但拆出来的几段是同一个画面，
        # 各出一张关键帧纯属浪费（每张 1 分钟，还容易长得不一样）。
        _kf = s.get("key_from")
        if _kf:
            try:
                _src = _gen_paths.get(int(_kf))
                if not (_src and os.path.exists(_src)):
                    # 续渲/跨次运行：退回"主镜最新那张"（按 mtime 取最后生成的一张，
                    # 不能写死 _00001_ —— 重新出图会变成 _00002_，写死就复用到旧图）
                    _cand = sorted(glob.glob(os.path.join(
                        OUTDIR, f"{prefix}_key_{int(_kf)}_[0-9]*.png")),
                        key=os.path.getmtime)
                    _src = _cand[-1] if _cand else ""
                if _src and os.path.exists(_src):
                    shutil.copyfile(_src, p)
                    print(f"[key {i}] 复用第 {int(_kf)} 镜的画面（拆镜续段，同一关键帧）",
                          flush=True)
                    paths.append(p)
                    _gen_paths[i] = p
                    continue
            except Exception as _e:
                print(f"[key {i}] 复用关键帧失败({_e})，改为单独出图", flush=True)
        k = (s.get("key") or "").strip()
        if not k:
            print(f"[key {i}] 缺 key 文案，跳过（需手动放入 {p}）")
            paths.append(p)
            continue
        rname = (s.get("ref") or "").strip()
        rfile = refs.get(rname, "") if rname else ""
        dn = float(s.get("denoise") or 0.62)
        # 双人同框：ref2 是画面里第二个人。拼成一张双人底图再重绘，两张脸各归各人。
        #
        # 【2026-10-29 修】以前只要本镜出现第二个人就无条件拼并排底图，结果
        # 《雨夜断剑》三镜全部变成「两个人并排半身、正脸看镜头」的剧照合影：
        #   · 第1镜该是「掀帘进店 + 柜台后有人」的纵深构图 → 被拍成并排；
        #   · 第3镜该是力劈/突进/刺入的打戏 → 被拍成两人站着、剑横在中间。
        # 原因是并排底图的骨架就是"两张棚拍半身照等宽对齐"，denoise 又被锁在
        # ≤0.70，模型只能在骨架上换皮，文案写什么都没用。
        #
        # 现在的规矩：**近距离双人镜用等距并排底图**（对峙、凑近、同框对话——
        # 这种镜头本来就要两张脸并列）；**宽景/动作镜用"一大一小前后错位"底图**
        # （near 0.72 高、far 0.52 高贴右下），两张脸都锁住，但骨架自带纵深，
        # 模型不会把它读成"并肩站好对着镜头"。
        rname2 = (s.get("ref2") or "").strip()
        _key_for_sz = (s.get("key") or "")
        _sz2 = (s.get("size") or "").strip()
        if not _sz2:
            # 界面没传景别就从文案里认（_auto_prompt 会把"全景镜头，"写进 key）
            if "远景镜头" in _key_for_sz:
                _sz2 = "远景"
            elif "全景镜头" in _key_for_sz:
                _sz2 = "全景"
        _wide = (_sz2 in ("远景", "全景")) or any(
            w in _key_for_sz for w in _ACTION_WORDS)
        # 场景照：这一镜在哪个地方。有场景照时把它和角色照拼成一张底图 ——
        # img2img 只有一个起点图，只用定妆照锁不住背景、只用场景照锁不住脸。
        sname = (s.get("scene") or "").strip()
        sfile = srefs.get(sname, "") if sname else ""
        _composed = False
        # 【三视图优先】背影镜 / 侧身镜：拿三视图里对应视角的面板当起点。
        # 定妆照是正面半身，背影镜用它当起点必被画成"回头看镜头"；
        # 而三视图的背面板本来就是全身背影，人体、衣袍、发型都对。
        # 侧/背影镜也没法贴脸（Haar 只认正脸），所以这类镜不走 compose-first。
        _use_sheet = ""
        _pose = _pose_of(_key_for_sz)
        if _pose and sheets.get(rname):
            _use_sheet = _sheet_cut(sheets.get(rname), _pose)
            if _use_sheet:
                rfile = _use_sheet
                # 背影/侧身是靠面板"撑住姿态"的，重绘幅度一大就又被模型转成半侧面。
                # 实测 0.62 会把背面板画成侧脸回头；0.46 能保住朝向，
                # 场景融合靠场景底图（人物本来是贴进场景再重绘的）来补。
                dn = min(max(dn, 0.42), 0.50)
        # 【构图优先】宽景/动作镜：先让模型按文案自由构图，再把定妆照的脸贴回去。
        # 定妆照只提供"这张脸"，不再提供"半身正脸站姿"这个骨架。
        if _wide and COMPOSE_FIRST and (rname2 or rfile) and not _use_sheet:
            try:
                # ComfyUI 有执行缓存：prompt+seed 一模一样时它直接复用上次结果、
                # **不再落盘**，history 里却有文件名 —— wait_key 就会拿到一个不存在的路径。
                # 构图这一步本来就是"要一张新的"，用时间戳掺进 seed 绕开缓存。
                _seed = 990001 + i * 31337 + 77 + (int(time.time()) % 1000007)
                # 顺便清掉这张镜上次留下的构图图，免得 wait_key 的 glob 兜底拿到旧图
                for _old in glob.glob(os.path.join(OUTDIR, f"{prefix}_key_{i}_c*.png")):
                    try:
                        os.remove(_old)
                    except Exception:
                        pass
                if sfile:
                    # 有场景照就以它为起点、denoise 0.86：建筑/光线/布局跟着场景照走，
                    # 人物姿势完全交给文案（0.86 足够把"没人"的店面重排成打斗现场）
                    _wf = Z.build_wf(k, _seed, f"{prefix}_key_{i}_c",
                                     ref=sfile, denoise=0.86)
                    _pid = Z.submit(_wf)
                    _c = wait_key(_pid, f"{prefix}_key_{i}_c") if _pid else ""
                else:
                    _c = _t2i_compose(k, _seed, f"{prefix}_key_{i}_c")
                if _c:
                    _fl = [refs.get(rname, "")]
                    if rname2:
                        _fl.append(refs.get(rname2, ""))
                    _fz = _face_paste(_c, [x for x in _fl if x]) if FACE_PASTE else ""
                    if _fz:
                        rfile, dn = _fz, COMPOSE_DN
                        _composed = True
                        print("[key %d] 构图优先：先自由构图，再贴定妆照的脸"
                              "（融合 denoise=%.2f）" % (i, COMPOSE_DN), flush=True)
                    elif not FACE_PASTE and _c:
                        # 不贴脸：构图图直接当起点，只做 0.35 的轻修（保构图/光线）。
                        # 全景/打斗镜靠"构图对 + 场景对"取胜，脸小到看不出换没换。
                        # 注意：build_wf 的 ref 要的是 ComfyUI input 目录里的**文件名**，
                        # 而 _c 是 output 目录的完整路径 —— 直接塞进去会让工作流 400
                        # （第一版就这么挂了）。必须先复制进 input 再用名字。
                        _nm = "comp_%s.png" % re.sub(
                            r"[^0-9A-Za-z_]+", "",
                            os.path.splitext(os.path.basename(_c))[0])
                        try:
                            shutil.copyfile(_c, os.path.join(H3.I2V_INPUT_DIR, _nm))
                            rfile, dn = _nm, 0.35
                            _composed = True
                            print("[key %d] 构图优先：按设置不贴脸，构图图直接轻修"
                                  "（denoise=0.35，起点=%s）" % (i, _nm), flush=True)
                        except Exception as _e2:
                            print("[key %d] 构图图放不进 input（%s），退回定妆照起点"
                                  % (i, _e2), flush=True)
            except Exception as _e:
                print("[key %d] 构图优先失败(%s)，退回定妆照底图" % (i, _e), flush=True)
        if rname2 and not _composed and not _use_sheet:
            _d = _duo_base(rfile, refs.get(rname2, ""), wide=_wide)
            if _d:
                rfile = _d
                # 两人已经在起点图里，denoise 只要够把两张棚拍人像揉进同一个场景，
                # 太高会把脸洗掉（0.75+ 五官开始漂），太低会留下明显的拼贴边。
                # 宽景底图本身已经错位，可以多给一点重绘余量把骨架揉进雨夜环境。
                dn = (min(max(dn, 0.72), 0.82) if _wide
                      else min(max(dn, 0.55), 0.70))
        # 场景照：构图优先命中的镜已经自带场景（上面就是拿场景照起的底），不再二次拼底
        if sfile and not _composed:
            if rfile:
                _sb = _scene_base(sfile, rfile)
                if _sb:
                    rfile = _sb
                    # 合成底图里已经有人有景，重绘幅度要比纯角色锚定小一点，
                    # 否则背景布局被重画掉，场景照就白贴了。
                    dn = min(max(dn, 0.55), 0.70)
            else:
                # 空镜（没人）：场景照直接当底图，denoise 压低 → 布局原样保留
                rfile = sfile
                dn = min(max(dn, 0.35), 0.60)
        # 人数护栏：这一版第 1 镜(母女两人同框)被画成了三个人，
        # 因为提示词只说"母亲…女儿…"，没说"画面里只能有几个"。
        # 按本镜实际绑定的角色数把人数写死，空镜则明确不要人脸。
        if rname2:
            # 【英文正向约束】原先是中文否定句"不要出现第三个人"—— cfg=1 的模型
            # 读不懂否定，实测照样画出三个围观的人。改成"填空式"正向描述：
            # 把画面的其余部分明确写成"空的、只有雨和石头"，比说"不要有人"有效得多。
            k = (k + "。画面里只有两个人。"
                    " Exactly two people in the entire frame: a young swordsman and a "
                    "masked assassin. Every other part of the frame is empty — wet stone "
                    "floor, broken pillars, hanging strips of old dark cloth and falling "
                    "rain, and nothing else. No bystanders anywhere in the background."
                 ).strip()
            # 宽景/动作镜的构图护栏：不走并排底图时，最容易翻成的就是
            # 「两个人并肩站好、一起看镜头」的剧照感 —— 这几句专门压它。
            if _wide:
                # 只写"不要看镜头"没用（cfg=1 的模型不听否定句），必须给正向描述。
                #
                # 【2026-10-30】以前这里一律写"交手最紧张的一瞬间（一人兵刃高举、
                # 另一人矮身侧闪）"—— 对打斗镜是对的，但对**单人拔刀镜**是错的：
                # 一个人对着谁举刀？模型只能退回站姿。所以按动作类型分档写，
                # 首帧姿态决定了 i2v 的上限（首帧站着，后面再怎么推也拔不出刀）。
                _fight = any(w in k for w in ("劈", "斩", "砍", "刺", "厮打", "交手",
                                              "格挡", "血溅", "碎裂", "翻飞", "踹", "相撞"))
                _draw = any(w in k for w in ("拔刀", "拔剑", "出鞘", "抽出", "亮出",
                                             "持刀", "横刀", "握刀"))
                _walk = any(w in k for w in ("走进", "走入", "掀帘", "推门", "转身",
                                             "离开", "走出", "奔走", "奔向", "迈步"))
                if _fight:
                    k = (k + "。画面定格在交手最紧张的一瞬间：一人兵刃高举、身体前倾发力，"
                             "另一人矮身侧闪、重心压低；衣袍与帘布被气劲带起，"
                             "雨水被震得四溅；两人一近一远错开站位，景深自然、有前后遮挡，"
                             "是电影分镜里的一帧，人物不看镜头。").strip()
                elif _draw:
                    k = (k + "。画面定格在**出鞘的那一瞬间**：手掌已经握住刀柄，"
                             "刀身刚刚滑出鞘口、寒光顺着刃面拉出一条亮线，手腕绷紧，"
                             "肩膀微微抬起，身体重心前压，衣袍被刚才的动作带得尚未落下，"
                             "是电影动作分镜里的一帧，人物不看镜头。").strip()
                elif _walk:
                    k = (k + "。画面定格在**迈步的中间态**：一只脚刚离地、另一只脚承重，"
                             "身体前倾，衣袍下摆被带起离开身体，身后有动态虚影，"
                             "是电影分镜里的一帧，人物不看镜头。").strip()
                else:
                    k = (k + "。人物身体略微前倾、重心不在正中，衣袍和头发有被风带动的"
                             "走向，是电影分镜里的一帧，人物不看镜头。").strip()
                # 英文动作短语：构图提示词主体是中文，夹在里面的中文动作经常被无视
                # （实测"抢先劈下/侧身让过"被画成"垂手持刀站着"）。同一句话用英文
                # 再写一遍，动作词才真的起作用。
                _fight_en = {
                    "fight": "One figure swings a blade down from overhead while the other "
                             "twists aside to evade, bodies angled, cloth torn by the motion.",
                    "draw": "The hand grips the hilt and pulls the blade free of the "
                            "scabbard, arm extended, steel glinting.",
                    "walk": "One leg is lifted mid-stride, body leaning forward, hem of the "
                            "robe swinging away from the body.",
                }
                _kind = ("fight" if _fight else "draw" if _draw else
                         "walk" if _walk else "")
                if _kind:
                    k = (k + " " + _fight_en[_kind]).strip()
                    _wl = _weapon_line(_key_for_sz)
                    if _wl:
                        k = (k + " " + _wl).strip()
                else:
                    _en_act = _action_en(_key_for_sz)
                    if _en_act:
                        k = (k + " " + _en_act).strip()
        elif "空镜" in (rname or ""):
            k = (k + "。这一镜是局部特写，画面里不要出现任何人的脸，只拍门板、鞋、手这些局部。").strip()
        elif rfile:
            k = (k + "。画面里只出现这一个人，不要出现第二个人或背景行人。").strip()
        # 只有"构图优先"产出的贴脸图需要看 mtime（名字固定、内容每次变）；
        # 双人底图/场景底图是确定性拼板，每次重写 mtime 都变，算进指纹会让
        # 每一轮都把全部关键帧重渲一遍（30 分钟白烧）。
        _rmt = 0
        try:
            _rp = os.path.join(H3.I2V_INPUT_DIR, rfile or "")
            if rfile and os.path.exists(_rp) and os.path.basename(rfile).startswith("face_"):
                _rmt = int(os.path.getmtime(_rp))
        except Exception:
            pass
        fp = _fp({"k": k, "ref": rfile, "dn": dn, "w": Z.W, "h": Z.H,
                  "refm": _rmt})        # 续渲：这一镜的提示词没改、图也还在 → 直接跳过
        if not only and kman.get(str(i)) == fp:
            # 只认 _key_N_00001_ 这种正片，别把构图中间图 _key_N_c_xxx 当成品
            _ex = sorted(glob.glob(os.path.join(OUTDIR, f"{prefix}_key_{i}_[0-9]*.png")))
            if _ex:
                print(f"[key {i}] 已渲过且提示词未变，跳过（续渲）")
                paths.append(_ex[0])
                _gen_paths[i] = _ex[0]
                continue
        if _composed:
            tag = "构图优先+贴脸[%s%s] 融合=%.2f" % (
                rname, ("+" + rname2) if rname2 else "", dn)
        elif _use_sheet:
            tag = "三视图%s视图[%s] denoise=%.2f" % (
                {"back": "背", "side": "侧"}.get(_pose, ""), rname, dn)
        elif rname2 and not _wide:
            tag = "双人底图[%s+%s] denoise=%.2f" % (rname, rname2, dn)
        elif rname2:
            tag = ("宽景错位双人底图[%s近+%s远] denoise=%.2f"
                   % (rname, rname2, dn))
        elif sfile and rfile:
            tag = "场景+角色底图[%s+%s] denoise=%.2f" % (sname, rname, dn)
        elif sfile:
            tag = "场景底图[%s] denoise=%.2f" % (sname, dn)
        else:
            tag = ("锚定[%s] denoise=%.2f" % (rname, dn)) if rfile else "文生图"
        # 以前是"盲抽 N 张再挑一张"：不管好坏都出满 N 张，时间 ×N。
        # 现在改成「出一张 → 当场检测 → 达标就停，不合格才换 seed 重出」：
        # 大多数镜 1 张就过，只有崩脸/糊成一片的才多跑。
        # KEY_CANDIDATES 的含义也跟着变了：不是"出几张"，而是"最多试几次"。
        _n = KEY_CANDIDATES if not only else 1
        try:
            _pass = float(_cfg("key_pass_score", 56))
        except Exception:
            _pass = 56.0
        # 空镜（没有 ref）本来就没人，别再按"少了一张脸"扣分
        _expect = 2 if rname2 else (1 if rfile else 0)
        _is_back = bool(_use_sheet and _pose == "back")
        _best, _best_sc = "", -999.0
        for _ci in range(_n):
            _seed = 990001 + i * 31337 + _ci * 7919 + _RUN_SALT
            _cand = ("　第 %d/%d 次" % (_ci + 1, _n)) if _n > 1 else ""
            for _try in range(2):          # 第 2 次是"没落盘"时的换 seed 重提
                _wf = Z.build_wf(k, _seed + _try * 104729,
                                 f"{prefix}_key_{i}", ref=rfile, denoise=dn)
                _pid = Z.submit(_wf)
                if not _pid:
                    print(f"[key {i}] 提交失败"); sys.exit(1)
                PG.emit("关键帧", 0.04 + 0.16 * ((i - 1) + _ci / float(_n))
                        / max(1, len(shots)),
                        f"第 {i}/{len(shots)} 张{_cand}")
                print(f"[key {i}] 已提交 {_pid}（{tag}），等出图…{_cand}")
                _r = wait_key(_pid, f"{prefix}_key_{i}")
                if _r and os.path.exists(_r):
                    break
                print(f"[key {i}] 这次没落盘（多半命中执行缓存），换 seed 重提一次",
                      flush=True)
            if _r and os.path.exists(_r):
                _sc = _score_key(_r, _expect, _is_back)
                # 闸门：该有人的镜头，只要检出至少一张"像样的脸"就放行。
                # 不比较脸数是否严格相等 —— 远景里路人脸很多，比总数必误判；
                # 也不看总分 —— 总分混了清晰度，波动大。
                # 只有"该有人却一张像样的脸都没有"才算画跑了，换 seed 重出。
                _big = _big_faces(_r) if _expect >= 1 else -1
                _ok = (_big != 0)              # -1（检测器失败）也放行
                print(f"[key {i}] 第 {_ci + 1} 张：主要人脸 {_big} 张"
                      f"（该有 {_expect} 人）得分 {_sc:.0f}"
                      f" -> {os.path.basename(_r)}")
                if _sc > _best_sc:
                    _best, _best_sc = _r, _sc
                if _ok:
                    if _ci:
                        print(f"[key {i}] 第 {_ci + 1} 张检测通过（{_sc:.0f} 分），"
                              f"不再重出", flush=True)
                    break
                if _ci + 1 < _n:
                    print(f"[key {i}] 该有人的镜头没检出一张像样的脸（得分 "
                          f"{_sc:.0f}），换 seed 重出第 {_ci + 2} 次", flush=True)
                else:
                    print(f"[key {i}] 试满 {_n} 次仍没脸，用最高分那张 "
                          f"{_best_sc:.0f}（人工看一眼）", flush=True)
        res = _best
        if res and os.path.exists(res):
            # 最高分那张盖回规范名 _00001_，后面的环节（续渲/视频/合成）都只认这个名字
            _canon = os.path.join(OUTDIR, f"{prefix}_key_{i}_00001_.png")
            try:
                if os.path.abspath(res) != os.path.abspath(_canon):
                    shutil.copyfile(res, _canon)
                res = _canon
            except Exception as _e:
                print(f"[key {i}] 覆盖规范名失败：{_e}")
            print(f"[key {i}] 完成 -> {res}"
                  + ("（共试 %d 次，最高分 %.0f）" % (_n, _best_sc) if _n > 1 else ""))
            kman[str(i)] = fp
            _save_man(prefix, man)
            paths.append(res)
            _gen_paths[i] = res
        else:
            print(f"[key {i}] 未完成（超时）；后续如缺图会报错")
            paths.append(p)
    return paths


# ============================ 自动审片（QC） ============================
# 生成完不是结束：黑屏、纯色、冻帧（同一帧复制几十遍）这三类崩坏，肉眼一眼能看出来，
# 但整集几十镜跑上几个小时，没人愿意逐镜盯。这里抽 12 帧算三个指标，命中就重渲那一镜。
# 只做「明显坏了」的判断，不敢碰"好不好看" —— 误判会把好镜头重渲掉，代价是几分钟。
QC_ENABLE = os.environ.get("QC", "1") != "0"
QC_ACTIVE = [True]          # GUI 传 qc=False 时关掉（run() 里赋值）
QC_RETRY = int(os.environ.get("QC_RETRY", "1"))      # 每镜最多重渲几次
QC_MAX_FIX = int(os.environ.get("QC_MAX_FIX", "3"))  # 一集最多修几镜（防时间翻倍）


def _qc_stats(frames, samples=12):
    """抽帧算指标：亮度均值、亮度标准差、相邻帧差。返回 dict 或 None。"""
    try:
        from PIL import Image, ImageChops, ImageStat
    except Exception:
        return None
    fs = [f for f in frames if f and os.path.exists(f)]
    if not fs:
        return None
    # 均匀抽样：首尾都要包含（黑场常在结尾）
    if len(fs) > samples:
        step = max(1, len(fs) // samples)
        fs = fs[::step][:samples]
    lum, std, diff = [], [], []
    prev = None
    for f in fs:
        try:
            im = Image.open(f).convert("L")
        except Exception:
            continue
        st = ImageStat.Stat(im)
        lum.append(float(st.mean[0]))
        std.append(float(st.stddev[0]))
        if prev is not None:
            d = ImageStat.Stat(ImageChops.difference(prev, im)).mean[0]
            diff.append(float(d))
        prev = im
    if not lum:
        return None
    return {"n": len(lum), "lum": lum, "std": std, "diff": diff,
            "lum_avg": sum(lum) / len(lum)}


def qc_judge(st):
    """按指标判「这一镜是不是明显崩了」，返回 (ok, 原因)。"""
    if not st:
        return False, "没有帧"
    n = max(1, st["n"])
    black = sum(1 for x in st["lum"] if x < 8) / float(n)
    blank = sum(1 for x, y in zip(st["lum"], st["std"]) if y < 3.0) / float(n)
    if black >= 0.5:
        return False, "黑屏 %.0f%%" % (black * 100)
    if blank >= 0.7:
        return False, "纯色/白屏 %.0f%%" % (blank * 100)
    if st["diff"]:
        still = sum(1 for d in st["diff"] if d < 0.4) / float(len(st["diff"]))
        if still >= 0.9:
            return False, "冻帧 %.0f%%（画面几乎不变）" % (still * 100)
    return True, ""


def qc_shots(ordered):
    """整集质检：返回 [{ok, why, stat}]，和 ordered 一一对应。"""
    out = []
    for i, fs in enumerate(ordered):
        try:
            st = _qc_stats(fs)
            ok, why = qc_judge(st)
        except Exception as e:
            st, ok, why = None, True, "质检异常 %r" % (e,)
        out.append({"ok": ok, "why": why, "stat": st})
    return out


def qc_fix(prefix, h3_shots, ordered, i2v, steps, turbo, native_audio, only=None):
    """审片 + 坏镜重渲。返回 (ordered, qres, 修好的镜号, 仍异常的镜号)。"""
    qres = qc_shots(ordered)
    bad = [i for i, r in enumerate(qres) if not r["ok"]]
    fixed = []
    if bad:
        print("[审片] 检出 %d 镜异常：%s" % (
            len(bad), "；".join("第%d镜 %s" % (i + 1, qres[i]["why"]) for i in bad)))
        if only:
            print("[审片] 本次是部分重渲，跳过自动修复（免得覆盖你只挑的那几镜）")
        else:
            for k in [i for i in bad if i < len(h3_shots)][:QC_MAX_FIX]:
                for att in range(1, QC_RETRY + 1):
                    stopflag.check()
                    print("[审片] 第 %d 镜重渲（第 %d/%d 次）…"
                          % (k + 1, att, QC_RETRY), flush=True)
                    fs2 = qc_rerender_shot(prefix, h3_shots, k, i2v, steps, turbo,
                                           native_audio, att)
                    if not fs2:
                        continue
                    st2 = _qc_stats(fs2)
                    ok2, why2 = qc_judge(st2)
                    if ok2:
                        ordered[k] = fs2
                        qres[k] = {"ok": True, "why": "", "stat": st2}
                        fixed.append(k + 1)
                        print("[审片] 第 %d 镜已修好（%d 帧）" % (k + 1, len(fs2)),
                              flush=True)
                        break
                    print("[审片] 第 %d 镜仍不合格：%s" % (k + 1, why2), flush=True)
    still = [i + 1 for i, r in enumerate(qres) if not r["ok"]]
    return ordered, qres, fixed, still


def qc_rerender_shot(prefix, h3_shots, k, i2v, steps, turbo, native_audio, attempt=1):
    """只重渲第 k+1 镜：用独立前缀，不碰别的镜已经渲好的帧。"""
    p2 = "%s_qc%d_a%d" % (prefix, k + 1, attempt)
    try:
        wf, _ = H3.build_shots_workflow(p2, [h3_shots[k]], steps, "off", 1.0, turbo,
                                        [i2v[k]] if i2v else None, seek=False,
                                        seek_mode="auto", fast_i2v=True,
                                        native_audio=native_audio)
        resp = H3.post("/prompt", {"prompt": wf, "client_id": H3.CLIENT})
        pid = (resp or {}).get("prompt_id")
        if not pid:
            print("[审片] 第 %d 镜重渲提交失败：%r" % (k + 1, resp))
            return None
        fb = H3.wait_done(pid, 1, p2, lens=[int(h3_shots[k].get("len") or H3.LEN)],
                          native_audio=native_audio)
        if not fb:
            return None
        fs = sorted(fb.get(0, []),
                    key=lambda p: int(re.search(r"_(\d+)_\.", os.path.basename(p)).group(1))
                    if re.search(r"_(\d+)_\.", os.path.basename(p)) else 0)
        return fs or None
    except Exception as e:
        print("[审片] 第 %d 镜重渲出错：%r" % (k + 1, e))
        return None


def stage_video(script, prefix, length, steps, turbo, only, key_paths, res=None,
                te4b=False, te_mode="streaming", unet="", native_audio=False, t2v=False):
    stopflag.check()
    H3.W, H3.H = res or (576, 1024)
    H3.USE_CLIPPROJ = bool(te4b)
    H3.CLIPPROJ_MODE = te_mode
    if unet:
        H3.GGUF_UNET = unet
    H3.LEN = length
    H3.STEPS = steps
    # .gguf 走 UnetLoaderGGUF，.safetensors 走 UNETLoader（换模型时自动判断）
    H3.USE_GGUF = not H3.GGUF_UNET.lower().endswith(".safetensors")
    shots = script["shots"]
    idxs = [i for i in (only or range(1, len(shots) + 1))]
    # 续渲：整段视频已经成功渲完、且剧本/参数没动过 → 不再重跑这条链
    man = _load_man(prefix)
    vfp = _fp({"shots": [{"p": x.get("prompt", ""), "k": x.get("key", ""),
                          "l": x.get("line", ""), "n": int(x.get("len") or length)}
                         for x in shots],
               "res": res, "len": length, "steps": steps, "native": native_audio})
    if not only and man.get("video_fp") == vfp:
        print("[i2v] 视频已渲完且剧本未变，跳过整段（续渲）")
        return True
    h3_shots, i2v = [], []
    for i in idxs:
        s = shots[i - 1]
        if t2v:
            # t2v：把 key(场景/风格/无文字护栏/横构图) 与 prompt(运镜) 合并成完整描述，
            # H3 直接文生视频（本机无 Z-Image，不出关键帧）。不向 i2v 追加 -> use_i2v=False。
            scene = (s.get("key") or "").strip()
            motion = (s.get("prompt") or "").strip()
            combined = (scene + "。" + motion).strip("。") if scene else motion
            h3_shots.append({"prompt": combined,
                             "line": s.get("line", ""), "who": s.get("who", ""),
                             "len": max(NATIVE_MIN_FRAMES, int(s.get("len") or length))})
        else:
            h3_shots.append({"prompt": s.get("prompt", ""),
                             "line": s.get("line", ""), "who": s.get("who", ""),
                             # 下限 73 帧(3秒): 39 帧(1.6s)这种超短镜，H3 在 8 步内
                             # 收敛不出构图 —— 上一版第 7 镜(2秒敲门)直接渲成了
                             # 「一排书册+老太太眼睛」的拼贴页，就是帧数太少的锅。
                             "len": max(NATIVE_MIN_FRAMES, int(s.get("len") or length))})
            kp = key_paths[i - 1]
            if not kp or not os.path.exists(kp):
                print(f"[i2v] 关键帧缺失 shot{i}: {kp}"); sys.exit(1)
            dst = os.path.join(H3.I2V_INPUT_DIR, f"i2v_s{len(i2v) + 1}.png")
            shutil.copy(kp, dst)
            i2v.append(dst)
    # 清掉上一次同名任务的旧帧：否则进度会虚高、成片也可能混进旧画面
    for old in glob.glob(os.path.join(OUTDIR, f"{prefix}_s*_*.png")):
        try:
            os.remove(old)
        except Exception:
            pass
    if native_audio:
        # 同理清掉旧音轨，免得缺镜时把上一次的台词混进来
        for old in glob.glob(os.path.join(OUTDIR, f"{prefix}_a*_*.mp3")):
            try:
                os.remove(old)
            except Exception:
                pass
    wf, _ = H3.build_shots_workflow(prefix, h3_shots, steps, "off", 1.0,
                                    turbo, i2v, seek=False, seek_mode="auto",
                                    fast_i2v=True, native_audio=native_audio)
    payload = {"prompt": wf, "client_id": H3.CLIENT}
    try:
        resp = H3.post("/prompt", payload)
    except Exception as e:
        print("[ERROR] 提交 H3 失败（ComfyUI 是否在线？）:", e); sys.exit(1)
    pid = resp.get("prompt_id")
    if not pid:
        print("[ERROR] 无 prompt_id:", resp); sys.exit(1)
    print(f"[i2v] 整条分镜链 prompt_id = {pid}（{len(h3_shots)} 镜，模型只加载一次）")
    tot = sum(int(x.get("len") or length) for x in h3_shots)
    PG.emit("视频渲染", 0.20, f"0/{tot} 帧（模型加载中…）")
    t0 = time.time()
    frames_by_shot = H3.wait_done(pid, len(h3_shots), prefix,
                                  lens=[int(x.get("len") or length) for x in h3_shots],
                                  native_audio=native_audio)
    if frames_by_shot is None:
        print("[FAILED] H3 执行出错"); sys.exit(1)
    PG.emit("视频完成", 0.80, "")
    ordered = []
    for i in range(len(h3_shots)):
        fs = sorted(frames_by_shot.get(i, []),
                    key=lambda p: int(re.search(r"_(\d+)_\.", os.path.basename(p)).group(1)))
        ordered.append(fs)
        print(f"  shot {i + 1}: {len(fs)} 帧")

    # ---- 自动审片：黑屏 / 纯色 / 冻帧 这三类崩坏，命中就单镜重渲 ----
    if QC_ACTIVE[0] and QC_ENABLE and h3_shots:
        try:
            ordered, qres, fixed, still = qc_fix(prefix, h3_shots, ordered, i2v,
                                                 steps, turbo, native_audio, only)
            print("[审片] 共 %d 镜：正常 %d%s%s" % (
                len(qres), len(qres) - len(still),
                ("，修复 %d 镜" % len(fixed)) if fixed else "",
                ("，仍异常：%s" % "、".join("第%d镜" % i for i in still)) if still else ""))
            json.dump([{"shot": i + 1, "ok": r["ok"], "why": r["why"],
                        "lum": round((r["stat"] or {}).get("lum_avg", 0), 1)}
                       for i, r in enumerate(qres)],
                      open(os.path.join(OUTDIR, "%s_qc.json" % prefix), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=2)
        except stopflag.Stopped:
            raise
        except Exception as _qe:
            print("[审片] 质检跳过：%r" % (_qe,))

    json.dump({"shots": ordered, "fps": H3.FPS, "length": H3.LEN,
               "steps": H3.STEPS, "prefix": prefix},
              open(os.path.join(OUTDIR, "shots_frames.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    done = sum(1 for x in ordered if x)
    print(f"[i2v] shots_frames.json 写出，成功 {done}/{len(h3_shots)} 镜，"
          f"耗时 {time.time() - t0:.0f}s")
    if done == len(h3_shots):
        man["video_fp"] = vfp      # 全部成功才记账，中途停掉下次仍会重跑这一段
        _save_man(prefix, man)
    return done == len(h3_shots)


def stage_voice(script, prefix, voice):
    VT.VOICE = voice
    rate = script.get("rate") or "+0%"        # 语速，如 "+15%"
    pitch = script.get("pitch") or "+0Hz"     # 音调（一般不动）
    shots = script["shots"]
    if rate != "+0%":
        print(f"[voice] 语速 {rate}")
    vm_shots = []
    for i, s in enumerate(shots, 1):
        stopflag.check()
        mp3 = os.path.join(OUTDIR, f"{prefix}_v{i}.mp3")
        txt = (s.get("line") or "").strip()
        if not txt:
            print(f"[voice {i}] 缺 line，写 2s 静音占位")
            vm_shots.append({"mp3": "", "dur": 2.0, "text": ""})
            continue
        print(f"[voice {i}] {txt}")
        PG.emit("配音", 0.81 + 0.09 * (i - 1) / max(1, len(shots)),
                f"第 {i}/{len(shots)} 段")
        # 一人一音色：GUI 会传 {说话人: {voice, rate, pitch}}（旧格式是纯音色名）
        who = (s.get("who") or "旁白").strip()
        _vp = (script.get("voice_map") or {}).get(who)
        if isinstance(_vp, dict):
            v = _vp.get("voice") or voice
            r = _vp.get("rate") or rate
            p = _vp.get("pitch") or pitch
        else:
            v, r, p = (_vp or voice), rate, pitch
        VT.tts(txt, mp3, voice=v, rate=r, pitch=p)
        print(f"         -> 说话人={who} 音色={v} 语速={r} 音高={p}")
        d = VT.duration(mp3) or max(2.0, len(txt) / 6.0)
        # text 存"剥掉说话人"的纯台词，拼片时烧成字幕（不含"旁白："这几个字）
        spoken = VT.clean_line(txt) if hasattr(VT, "clean_line") else txt
        vm_shots.append({"mp3": mp3, "dur": d, "text": spoken})
        print(f"         -> {os.path.basename(mp3)} {d:.2f}s")
    vm = {"shots": vm_shots}
    vmp = os.path.join(OUTDIR, f"{prefix}_voice_map.json")
    json.dump(vm, open(vmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    total = sum(x["dur"] for x in vm_shots)
    print(f"[voice] 映射表 -> {vmp}，总旁白约 {total:.1f}s")
    return vmp


def stage_build(prefix, vmp):
    PG.emit("拼接成片", 0.92, "ffmpeg 合成中…（约 10~60 秒，这一步不能中途停）")
    import build_manju_mp4 as B
    os.environ["VOICE_MAP"] = vmp or ""
    os.environ["ADD_LOOP"] = "0"
    os.environ["ADD_AUDIO"] = "1"
    src = os.path.join(OUTDIR, "manju_drama.mp4")
    dst = os.path.join(OUTDIR, f"{prefix}_drama.mp4")
    # 进拼接前清掉停止标记：前面镜头都渲完了，这里只剩 ffmpeg 收尾几秒到几十秒。
    # 中途打断只会留下半成品（没有"续渲"的说法），而且会把已经合出来的成片丢掉——
    # 之前用户点到停止，界面就停在 92% + 已停止，片子其实已经好了。
    try:
        stopflag.clear()
    except Exception:
        pass
    err = None
    try:
        B.main()
    except BaseException as e:          # noqa: BLE001 —— 出错也要先抢救成片
        err = e
    finally:
        if os.path.exists(src):
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                os.replace(src, dst)
                print(f"[build] 成片 -> {dst} ({os.path.getsize(dst) / 1_000_000:.2f} MB)")
                if err is not None:
                    print(f"[build] 拼接过程报过 {type(err).__name__}: {err}，但成片已落盘，按成功处理")
                err = None
                PG.emit("完成", 1.0, "")
            except Exception as e:
                print(f"[build] 成片改名失败：{e}")
                err = err or e
    if err is not None:
        raise err
    if os.path.exists(dst):
        return True
    print("[build] 未找到 manju_drama.mp4")
    return False



# ============================ 出片前质检（QC） ============================
# 行业 SOP 最后一步是"逐条打勾"的质检清单：脸有没有变、画风有没有跳、
# 音画对不对得上、有没有死时间。这里把能自动判的部分做成一份报告，
# 出片后写在 output/{prefix}_质检.txt，日志里只给摘要 —— 免得渲了两小时才发现缺一镜。
_MEDIA_CACHE = {}

# ---- 动作量检测：动作镜"基本没动"要被当场发现 ----
# 数值 = 相邻帧灰度平均绝对差（0-255），抽若干对取均值。实测标定（本机历史产物）：
#   动作镜合格：暮色断桥_s3 打斗 5.80 / 音色测试_s1 持刀 4.45 / fasttest2_s2 18.10
#   动作镜失败：暮色断桥_s7 该拔刀实际站桩 1.85
#   完全凝固： 鞋_s9 0.84 / 蹄花店_s11 0.79
# 阈值取 2.5：能抓住 1.85 那种"该动没动"，又不会把 3.5~4 的克制表演误报。
MOTION_ACTION_MIN = 2.5     # 明确写了动作（劈/拔刀/奔走…）的镜
MOTION_ANY_MIN = 1.0        # 任何镜低于这个数＝画面几乎凝固
MOTION_MAYBE_MIN = 1.8      # 认不准但"看着像要动"的镜（含"认不出"的兜底）
# 低于这个数＝画面真的凝固了，才值得花时间重跑。1.2~阈值之间只是"不够激烈"。
MOTION_HARD_STILL = 1.2

# 【2026-10-30 为什么要有这一层】
# 之前只按 _ACTION_WORDS / _action_en 判断"是不是动作镜"，没命中就当静态镜、
# 用最宽松的阈值放过去。可词表永远不可能穷尽新剧本的写法 —— 新剧写"他踏前半步、
# 腕子一翻"，一个词都没收录，就会被判成静态镜，站桩打戏照样悄悄交付。
# 所以这里"宽进严出"：**认不出也按 1.8 要求**，只有明确是特写/空镜/物件镜才放宽。
_MOTION_WEAK = ("走", "动", "举", "挥", "拉", "推", "坐", "倒", "看", "转",
                "抬", "低", "伸", "握", "抓", "放", "退", "冲", "跑", "跳",
                "落", "起", "扑", "撞", "扫", "掀", "踢", "跨", "俯", "仰",
                "睁", "闭", "踏", "翻", "甩", "劈", "砍", "刺", "拔", "抽",
                "拖", "扛", "按", "拧", "扯", "扑", "栽", "滚", "跃")
_MOTION_STILL = ("特写", "局部特写", "空镜", "远景", "物件", "铜钱", "玉佩",
                 "刀鞘", "指尖", "手掌", "脚印", "血迹", "门板", "屋檐",
                 # 以下都是"看细节"的线索：这类镜帧差天然很小，
                 # 按动作档要求会白重跑（有强动作词的镜不会走到这里，所以安全）
                 "指缝", "指节", "剑脊", "剑锋", "雨滴", "血顺着", "石阶",
                 "剑柄", "刃面", "掌心", "刀背")


def motion_need(text, size=""):
    """这一镜"应该动多少"。返回 (档位名, 阈值, 最多重跑几次)。

    宽进严出：认不出就当"可能动作"，不用最宽松的阈值放行 ——
    词表永远不可能穷尽新剧本的写法，只有这一层兜底才能保证换剧不漏。

    景别参与判定：远景/局部特写本来就不该有大动作（镜头交代环境 / 拍物件），
    硬要求它们"动起来"会白重跑。但文案里有强动作词时，景别让位（远景里的
    军队冲锋照样算动作镜）。
    """
    t = text or ""
    sz = (size or "").strip()
    strong = any(k in t for k in _ACTION_WORDS) or bool(_action_en(t))
    # 环境/物件镜：没写强动作就按静态（远景里的军队冲锋仍算动作，所以看 strong）
    if not strong and (sz in ("远景", "局部特写", "空镜") or "空镜" in t
                       or any(k in t for k in _MOTION_STILL)):
        return "静态", 0.9, 0
    if strong:
        # 【特写/近景要降档】动作被框在脸和上半身里，帧差天然比全景小。
        # 实测特写镜普遍落在 1.2~2.0，按 2.5 要求会让每个特写都白重跑两次 ——
        # "永久修好"变成"永久变慢"就本末倒置了。
        if sz in ("特写", "近景"):
            return "动作·特写", 1.8, 1
        return "动作", MOTION_ACTION_MIN, 2
    if any(k in t for k in _MOTION_WEAK):
        return "可能动作", MOTION_MAYBE_MIN, 1
    return "未识别", MOTION_MAYBE_MIN, 1   # ← 词表穷尽漏洞的补丁：不认识也要求动


def frame_luma(files, pairs=6):
    """平均亮度（0-255）。用来抓全黑/过曝这种生成崩坏。"""
    files = [f for f in (files or []) if f and os.path.exists(f)]
    if not files:
        return None
    try:
        import numpy as np  # noqa
        from PIL import Image
    except Exception:
        return None
    idx = np.linspace(0, len(files) - 1, min(pairs, len(files))).astype(int)
    vals = []
    for k in idx:
        try:
            a = Image.open(files[k]).convert("L")
            a = a.resize((160, max(1, int(a.height * 160 / float(a.width)))))
            vals.append(float(np.asarray(a, dtype=np.float32).mean()))
        except Exception:
            pass
    return float(np.mean(vals)) if vals else None


def motion_score(files, pairs=12):
    """一镜的"动得多不多"：相邻帧灰度差均值。读不出来返回 None。"""
    files = [f for f in (files or []) if f and os.path.exists(f)]
    if len(files) < 3:
        return None
    try:
        import numpy as np  # noqa
        from PIL import Image
    except Exception:
        return None
    idx = np.linspace(0, len(files) - 2, min(pairs, len(files) - 1)).astype(int)
    vals = []
    for k in idx:
        try:
            # 缩到 240 宽再比：既快又能抹掉压缩噪点造成的假运动
            a = Image.open(files[k]).convert("L")
            b = Image.open(files[k + 1]).convert("L")
            w = 240
            h = max(1, int(a.height * w / float(a.width)))
            a = np.asarray(a.resize((w, h)), dtype=np.float32)
            b = np.asarray(b.resize((w, h)), dtype=np.float32)
            vals.append(float(np.abs(a - b).mean()))
        except Exception:
            pass
    return float(np.mean(vals)) if vals else None



def _speech_units(txt):
    """台词换算成"口语字数"：汉字 1、标点 0.4、字母数字 0.55（与界面侧同源）。"""
    import unicodedata
    u = 0.0
    for ch in (txt or ""):
        if ch in "，。！？、；：…—\u2014\u2026 \"'“”‘’「」『』（）()":
            u += 0.4
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            u += 1.0
        elif ch.isalnum():
            u += 0.55
    return u


def _media_info(path):
    """用 ffmpeg -i 解析时长/分辨率/有没有音轨（不装 ffprobe 也能用）。"""
    if path in _MEDIA_CACHE:
        return _MEDIA_CACHE[path]
    info = {"dur": 0.0, "w": 0, "h": 0, "audio": False, "ok": os.path.exists(path)}
    if not info["ok"]:
        return info
    try:
        import subprocess
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run([exe, "-i", path], capture_output=True, text=True,
                           errors="ignore", timeout=60)
        txt = (r.stderr or "") + (r.stdout or "")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", txt)
        if m:
            info["dur"] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                           + float(m.group(3)))
        m = re.search(r"Video:.*?\s(\d{2,5})x(\d{2,5})[,\s]", txt)
        if m:
            info["w"], info["h"] = int(m.group(1)), int(m.group(2))
        info["audio"] = "Audio:" in txt
    except Exception as e:
        print("[QC] 读媒体信息失败：", e, flush=True)
    _MEDIA_CACHE[path] = info
    return info


def audio_flatness(path, seconds=8.0):
    """成片音轨"像不像噪音"：频谱平坦度 0~1，越接近 1 越像白噪音（＝电流声）。

    实测标定：
      粉红噪音冒充环境声 → 0.61（用户听感就是"嘶——"）
      模型原生现场声     → 0.24（雨、脚步、衣袍摩擦）
    所以 0.55 以上要报警：说明音轨里有一层合成噪音，或者模型音频崩了。
    """
    if not path or not os.path.exists(path):
        return None
    try:
        import subprocess
        import numpy as np
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        w = os.path.join(OUTDIR, "_qc_audio.wav")
        subprocess.run([exe, "-y", "-i", path, "-vn", "-ar", "16000", "-ac", "1",
                        "-t", str(seconds), w],
                       capture_output=True, timeout=120)
        if not os.path.exists(w):
            return None
        import wave
        wv = wave.open(w)
        n = wv.getnframes()
        if n < 4000:
            return None
        d = np.frombuffer(wv.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
        sp = np.abs(np.fft.rfft(d * np.hanning(len(d))))
        tot = float(sp.sum())
        if tot <= 0:
            return None
        p = sp / tot
        flat = float(np.exp(np.mean(np.log(p + 1e-12))) / (np.mean(p) + 1e-12))
        rms = float(np.sqrt((d ** 2).mean()))
        try:
            os.remove(w)
        except Exception:
            pass
        return flat, rms
    except Exception:
        return None


def run_qc(prefix, script, frames_by_shot=None, video=""):
    """出片质检：把能自动判的问题列出来，写 output/{prefix}_质检.txt。"""
    import json as _json
    shots = script.get("shots") or []
    if frames_by_shot is None:
        try:
            _sf = os.path.join(OUTDIR, "shots_frames.json")
            if os.path.exists(_sf):
                _d = json.load(open(_sf, encoding="utf-8"))
                frames_by_shot = {i: v for i, v in enumerate(_d.get("shots") or [])}
        except Exception:
            frames_by_shot = None
    lines, bad = [], 0
    lines.append("《%s》出片质检  %s" % (script.get("title") or prefix,
                                        time.strftime("%Y-%m-%d %H:%M")))
    lines.append("")
    # ① 每镜帧数（视频段是否真的把每一镜都渲完了）
    if frames_by_shot is not None:
        lines.append("① 逐镜帧数")
        for i in range(len(shots)):
            got = len(frames_by_shot.get(i) or [])
            want = int(shots[i].get("len") or 0)
            flag = "OK" if got and (not want or abs(got - want) <= 2) else "缺"
            if flag != "OK":
                bad += 1
            lines.append("   第%d镜 %4d 帧（应为 %s）%s"
                         % (i + 1, got, want or "?", flag))
        lines.append("")
    # ② 关键帧 + 抽帧看脸数（跨镜换脸的早期信号）
    lines.append("② 关键帧与画面（首帧抽检）")
    for i, s in enumerate(shots, 1):
        kp = os.path.join(OUTDIR, f"{prefix}_key_{i}_00001_.png")
        if not os.path.exists(kp):
            lines.append("   第%d镜 关键帧缺失：%s" % (i, os.path.basename(kp)))
            bad += 1
            continue
        want = 2 if s.get("ref2") else (0 if _pose_of(s.get("key") or "") else 1)
        n = len(_face_boxes(kp, maxn=8))
        flag = "OK" if n == want else "注意"
        if flag != "OK":
            bad += 1
        lines.append("   第%d镜 脸数 %d（预期 %d）· %s" % (i, n, want, flag))
    lines.append("")
    # ③ 动作量：这一镜到底动没动
    lines.append("③ 动作量与亮度（绝对下限 %.1f；同时对照本片自身分布）"
                 % MOTION_HARD_STILL)
    _mv_all = []          # (镜号, 运动量, 档位名, 亮度)
    for i, s_ in enumerate(shots, 1):
        fs = (frames_by_shot or {}).get(i - 1) or []
        mv = motion_score(fs)
        need_name, need_val, _c = motion_need(
            (s_.get("key") or "") + " " + (s_.get("prompt") or ""),
            s_.get("size") or "")
        luma = frame_luma(fs)
        if mv is None:
            lines.append("   第%d镜 无法测量（帧数不足）" % i)
            continue
        _mv_all.append((i, mv, need_name))
        flag = ""
        if mv < MOTION_HARD_STILL:
            flag = "凝固"          # 绝对判定：这个数值任何镜头都说明没动
        elif mv < need_val and need_name != "静态":
            flag = "偏静"
        if flag == "凝固":
            bad += 1
        extra = ""
        if luma is not None:
            if luma < 12:
                extra = "　⚠ 接近全黑（亮度 %.0f）：多半是条件没生效或 SigmaShift 又跑出来了" % luma
                bad += 1
            elif luma > 245:
                extra = "　⚠ 接近全白（亮度 %.0f）" % luma
                bad += 1
        lines.append("   第%d镜 %5.2f（%s档≥%.1f）%s%s"
                     % (i, mv, need_name, need_val,
                        (" ← " + flag) if flag else "", extra))

    # ---- 自适应：用本片自己的分布判断"相对偏静" ----
    # 固定阈值对不同题材不公平：打斗片普遍 5~10，情感片普遍 1.5~3。
    # 同一个 2.5 在打斗片里形同虚设（6.10 都远超），在情感片里又会误报。
    # 所以真正判断"这一镜在这部片里算不算没动"，看它相对同片中位数的位置。
    # 这一步只在**全片跑完后**做（逐镜实时的时候还没有基准，只能靠绝对下限）。
    if len(_mv_all) >= 4:
        _vals = sorted(v for _, v, _ in _mv_all)
        _med = _vals[len(_vals) // 2]
        _lo, _hi = _vals[0], _vals[-1]
        lines.append("   本片动作量分布：最低 %.2f / 中位 %.2f / 最高 %.2f"
                     % (_lo, _med, _hi))
        _rel = [(i, v, nm) for i, v, nm in _mv_all
                if nm != "静态" and _med > 0 and v < _med * 0.45]
        if _rel:
            lines.append("   ⚠ 明显比本片其他镜静（低于中位数 45%%）：%s"
                         % "、".join("第%d镜 %.2f" % (i, v) for i, v, _ in _rel))
            lines.append("      → 这几镜多半该动没动；重跑单镜：--only 镜号")
            bad += len(_rel)
    lines.append("")
    # ④ 成片
    lines.append("④ 成片")
    info = _media_info(video)
    if not info["ok"]:
        lines.append("   成片不存在：%s" % video)
        bad += 1
    else:
        lines.append("   文件 %s（%.1f MB）"
                     % (os.path.basename(video),
                        os.path.getsize(video) / 1_000_000.0))
        lines.append("   时长 %.1f 秒 · %dx%d · 音轨 %s"
                     % (info["dur"], info["w"], info["h"],
                        "有" if info["audio"] else "没找到"))
        if not info["audio"]:
            lines.append("   ⚠ 没有音轨：检查是不是把「A 方案·原生发声」和 B 方案搞混了")
            bad += 1
        # 音轨像不像噪音：一层合成噪音（或模型音频崩了）的客观特征就是频谱平坦度偏高。
        # 用户两次反馈"电流声"，都是人工听出来的 —— 这件事必须能自动查。
        _af = audio_flatness(video)
        if _af:
            _flat, _rms = _af
            if _flat > 0.55:
                lines.append("   ⚠ 音轨接近噪音（频谱平坦度 %.2f，>0.55 就是\"嘶——\"的电流声；"
                             "真实现场声约 0.24）" % _flat)
                bad += 1
            elif _flat > 0.45:
                lines.append("   音轨偏噪（平坦度 %.2f，尚可但偏糙）" % _flat)
            else:
                lines.append("   音轨正常（平坦度 %.2f，RMS %.3f）" % (_flat, _rms))
        # 台词总时长 vs 成片时长（差太多说明有镜被截断/补多了）
        need = 0.0
        for s in shots:
            need += max(0.7, (_speech_units(s.get("line") or "") / 4.3) + 0.6)
        if info["dur"] and need:
            gap = info["dur"] - need
            lines.append("   台词约 %.1f 秒，成片 %.1f 秒（差 %+.1f 秒）"
                         % (need, info["dur"], gap))
            if gap < -1.0:
                lines.append("   ⚠ 成片比台词短 %.1f 秒：末句可能被切掉" % -gap)
                bad += 1
    lines.append("")
    lines.append("结论：%s" % ("全部通过 ✅" if bad == 0
                             else "有 %d 项需要注意（见上）⚠" % bad))
    txt = "\n".join(lines)
    try:
        open(os.path.join(OUTDIR, "%s_质检.txt" % prefix), "w",
             encoding="utf-8").write(txt + "\n")
    except Exception:
        pass
    for ln in lines:
        print("[QC] " + ln, flush=True)
    return bad


# H3 节点的官方训练范围（object_info 的 tooltip 原文）：
#   "Frame count at 24 fps, (124 = ~5s, trained range is ~124-362)"
# 也就是说 **124 是下限、362 是上限**。以前把 124 当上限，所有镜都短于训练范围
# （短到 73 帧 = 3 秒），代价是动作做不完、音频流容易塌、还得靠拆镜硬凑。
NATIVE_MIN_FRAMES = 124      # 5.17 秒：训练下限
NATIVE_MAX_FRAMES = 362      # 15.08 秒：训练上限


def plan_lengths(script, vmp, fallback=56, lo=17, hi=NATIVE_MAX_FRAMES):
    """按每句台词的真实配音时长，逐镜算帧数。

    - 24fps 下 frames/24 = 秒，所以台词 5.1 秒就给它 ~5.1 秒的真动作（122 帧）；
    - 上下夹紧：太短的镜至少 lo 帧（0.7s，够看清动作），超长的封顶 hi 帧（5.2s，显存/时间上限）；
    - 超长台词剩下的一点尾巴，由 build 阶段的末帧缓推补足（此时只剩零点几秒，看不出来）。
    """
    shots = script.get("shots", [])
    durs = []
    if vmp and os.path.exists(vmp):
        try:
            vm = json.load(open(vmp, encoding="utf-8"))
            durs = [float(m.get("dur") or 0) for m in vm.get("shots", [])]
        except Exception:
            durs = []
    lens = []
    for i, s in enumerate(shots):
        if s.get("len"):                     # 手工指定优先
            lens.append(int(s["len"]))
            continue
        d = durs[i] if i < len(durs) else 0.0
        if d <= 0:
            n = int(fallback)
        else:
            n = int(round(d * 24)) + 2       # +2 帧余量，收尾不吃字
        n = max(lo, min(hi, n))
        s["len"] = n
        lens.append(n)
    return lens


def plan_native_lengths(script, lo=NATIVE_MIN_FRAMES, hi=NATIVE_MAX_FRAMES, cps=4.3):
    """A 方案：没有 TTS 了，只能按台词的"字数"预估这段话说多久，再换算成帧数。

    A 方案是模型自己念，念多快由模型定 —— 所以帧数必须"够这句话说完还留半拍"，
    不然末句会被切掉（这正是之前 TTS 版踩过的坑）。中文口语约 4.3 字/秒，
    标点算 0.4 个字的气口，再留 0.55 秒收尾。

    区间取 H3 的训练范围 [124, 362]，而不是"越短越好"：
    短于 124 帧（5 秒）时会跑出训练分布，表现为动作做不完、音频流发虚、
    人物容易在中途变形。以前把 124 当上限，每个镜都在 73~124 之间挤，
    只好靠"拆镜"把台词切碎 —— 那是在给错误的边界打补丁。
    现在按台词字量算，落在 124~362 之间就用一镜；超过 362（15 秒）才拆。
    """
    import unicodedata
    shots = script.get("shots", [])
    lens = []
    for s in shots:
        if s.get("len"):
            lens.append(int(s["len"]))
            continue
        txt = VT.clean_line(s.get("line") or "") if hasattr(VT, "clean_line") else (s.get("line") or "")
        units = 0.0
        for ch in txt:
            if ch in "，。！？、；：…—\u2014\u2026 \"'“”‘’「」『』（）()":
                units += 0.4
            elif unicodedata.east_asian_width(ch) in ("W", "F"):
                units += 1.0
            elif ch.isalnum():
                units += 0.55
        if units <= 0:
            n = 73                      # 没台词的镜头：约 3s 的环境镜（下限见下）
        else:
            # 尾巴 0.35→0.55：念完还得留半拍让人看清表情，不然像是被剪断的
            dur = 0.25 + units / cps + 0.55
            n = int(round(dur * 24))
        n = max(lo, min(hi, n))
        n = H3.align_len(n)            # 必须落在 17k+5 网格（否则采样器报 shape 错）
        n = min(hi, n)
        s["len"] = n
        lens.append(n)
    return lens


def stage_native_map(prefix, script):
    """A 方案：把模型自己念出来的音轨攒成配音映射表（替代 edge-tts 的 voice_map）。

    每镜的 mp3 由 H3 和画面一起长出来，时长天然等于该镜视频长度，
    所以这里直接用"帧数/24"当每镜显示时长，一帧都不用补。
    """
    vm_shots = []
    lens = []
    sf = os.path.join(OUTDIR, "shots_frames.json")
    frames = []
    if os.path.exists(sf):
        try:
            frames = json.load(open(sf, encoding="utf-8")).get("shots", [])
        except Exception:
            frames = []
    for i, s in enumerate(script.get("shots", []), 1):
        n = len(frames[i - 1]) if i - 1 < len(frames) and frames[i - 1] else int(s.get("len") or 48)
        dur = n / float(H3.FPS)
        mp3 = H3.LAST_AUDIO.get(i - 1, "")
        if not (mp3 and os.path.exists(mp3)):
            # ManjuSaveAudio 实际落盘的是 .wav，早期只认 .mp3 会导致整片对白全被静音垫替代
            cand = []
            for ext in ("mp3", "wav", "flac", "m4a"):
                cand += glob.glob(os.path.join(OUTDIR, f"{prefix}_a{i}_*.{ext}"))
            cand.sort(key=lambda p: os.path.getmtime(p))   # 同镜补渲过多次时取最新那版
            mp3 = cand[-1] if cand else ""
        spoken = VT.clean_line(s.get("line") or "") if hasattr(VT, "clean_line") else (s.get("line") or "")
        vm_shots.append({"mp3": mp3, "dur": dur, "text": spoken, "native": True})
        lens.append(n)
        ad = VT.duration(mp3) if (mp3 and hasattr(VT, "duration")) else 0
        print(f"[native {i}] {n}帧/{dur:.2f}s  音轨{'%.2f' % ad if ad else '缺失'}s  "
              f"{os.path.basename(mp3) if mp3 else '（静音垫）'}  「{spoken}」")
    vmp = os.path.join(OUTDIR, f"{prefix}_voice_map.json")
    json.dump({"shots": vm_shots, "native": True},
              open(vmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[native] 映射表 -> {vmp}，总时长 {sum(x['dur'] for x in vm_shots):.1f}s")
    return vmp



# ======================= Ref2VA 视频段（音色克隆 + 多图参考） =======================
# 和 fl2va（ManjuI2VFast）的区别：
#   fl2va：一张首帧 + 文字描述，音色只能靠 prompt 猜 → 同一角色跨镜换嗓子
#   ref2va：首帧 + 最多 9 张参考图 + 最多 3 段参考音频 → 音色克隆、脸/服装/环境一起锁
#
# 这个通道是"解决声音不一致"的唯一正路：参考音频给的是**样本**，不是形容词。
_R2V_VOICE_CACHE = {}

# 动作不达标时最多重跑几次。config.json 里 "motion_retry": N 可调（0 = 不重跑）。
# 默认 2：够抓到"偶尔抽到站桩"，又不至于一镜烧掉半小时。
try:
    _R2V_RETRY = max(0, min(4, int(_cfg("motion_retry", 2))))
except Exception:
    _R2V_RETRY = 2


def _r2v_voice_file(script, who):
    """给某个说话人生成并缓存一段参考音频 —— 全片复用同一段，音色才不会飘。"""
    import gen_ref2va as R
    key = (who or "旁白").strip() or "旁白"
    if key in _R2V_VOICE_CACHE:
        return _R2V_VOICE_CACHE[key]
    ids = script.get("voice_ids") or {}
    vid = ids.get(key) or ids.get("旁白") or "zh-CN-YunyangNeural"
    # 角色名多是中文，不能直接剥成 ASCII（会全变成 "v"，全剧共用同一个样本文件，
    # 后一个角色把前一个的音色覆盖掉）。用名字的 hash 保证唯一。
    tag = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
    wav = R.make_ref_audio("我在这条路上走了很多年，风一直没停。", vid,
                           "refvoice_%s_%s.wav" % (tag, vid.split("-")[-1][:6]))
    _R2V_VOICE_CACHE[key] = wav
    if wav:
        print("[r2v] 音色样本 %s → %s（edge-tts 音色 %s）" % (key, wav, vid), flush=True)
    return wav


def _r2v_refs(script, s, key_path):
    """这一镜喂给 Ref2VA 的参考图：关键帧 + 定妆照 + 场景照 + 三视图（最多 9 张）。"""
    out = []

    def add(p):
        if p and os.path.exists(p) and p not in out:
            out.append(p)

    if key_path and os.path.exists(key_path):
        name = "r2vkey_%s.png" % re.sub(r"[^0-9A-Za-z_]+", "",
                                        os.path.splitext(os.path.basename(key_path))[0])
        dst = os.path.join(H3.I2V_INPUT_DIR, name)
        try:
            shutil.copyfile(key_path, dst)
            add(dst)
        except Exception:
            pass
    refs = script.get("refs") or {}
    sheets = script.get("sheets") or {}
    srefs = script.get("srefs") or {}
    for nm in (s.get("ref"), s.get("ref2")):
        fn = refs.get((nm or "").strip())
        if fn:
            add(os.path.join(H3.I2V_INPUT_DIR, fn))
    sc = srefs.get((s.get("scene") or "").strip())
    if sc:
        add(os.path.join(H3.I2V_INPUT_DIR, sc))
    for nm in (s.get("ref"), s.get("ref2")):
        fn = sheets.get((nm or "").strip())
        if fn:
            add(os.path.join(H3.I2V_INPUT_DIR, fn))
    return [os.path.basename(p) for p in out][:9]


def _r2v_prompt(script, s, boost=0):
    """英文三段式（官方格式）：镜头/机位写英文，台词用 [Chinese] 保留原文。"""
    sz = (s.get("size") or "").strip()
    shot_en = H3._SIZE_EN.get(sz, "a cinematic shot")
    cam_en = H3._cam_en(s.get("prompt"))
    act = (s.get("key") or "").strip()
    lines = [
        "[Shot 1] Live-action, cinematic, photorealistic, 35mm film look, natural lighting. "
        "%s. %s Keep identity, costume, hairstyle and location exactly as in the reference images."
        % (shot_en, cam_en),
    ]
    # boost>0 是"这一镜动得不够、正在重跑"：把动作顶到最前面（位置越前权重越大），
    # 并加码到"整帧都在动"。同一 seed 重跑没意义，调用方会换 seed。
    if act:
        enf = _action_en(act)
        if enf and boost > 0:
            lines[0] = ("[Shot 1] Live-action, cinematic. FULL BODY MOTION — the action is "
                        "the subject of this shot: %s. " % enf) + lines[0]
            lines.append("Strong displacement is required: the body must visibly travel "
                         "through the frame, and the final pose must differ clearly from "
                         "the first frame.")
            if boost > 1:
                lines.append("The whole frame is in motion — subject and camera both moving, "
                             "cloth and hair streaming, dust and water disturbed.")
        if enf:
            # 英文动作短语带上"镜末到哪一步"，模型才会真把动作做完
            lines.append("Action in this shot: %s." % enf)
            lines.append(_ACTION_LIVE)
            _wl = _weapon_line(act)
            if _wl:
                lines.append(_wl)
        # 中文原文照旧保留：环境、光线、构图这些细节仍有价值，
        # 只是不再指望它承担"让模型动起来"的职责。
        lines.append("Scene detail: %s" % act)
    dlg = H3.dialogue_en(s)
    if dlg:
        lines.append(dlg)
    lines.append("overall_soundscape: ambience of the scene only, light wind, cloth and "
                 "footfalls, no music.")
    lines.append("non_diegetic_music: None.")
    return "\n".join(lines)


def _run_mark(prefix, resume=False):
    """本轮开跑的时间戳 —— 续渲靠它区分"这一轮渲的"和"上一部留下的同名旧帧"。

    同名旧帧是真事：剧本名不变重跑，output 里躺着一星期前的 {prefix}_s2_*，
    光看"文件在不在"会误判成已完成，直接把旧画面拼进新片。
    所以续渲只认 mtime ≥ 本轮起跑时间的文件。找不到标记（老版本渲的）就不续渲，
    宁可从头重做一遍 —— 拼错片比多等半小时严重。
    """
    p = os.path.join(OUTDIR, "%s_runmark.json" % prefix)
    if resume:
        try:
            t0 = float(json.load(open(p, encoding="utf-8")).get("t0") or 0)
        except Exception:
            t0 = 0.0
        if not t0:
            print("[续渲] 没找到本轮的运行标记（多半是旧版本渲的），这次从头重做",
                  flush=True)
            return float("inf")
        return t0
    try:
        t0 = time.time()
        json.dump({"t0": t0}, open(p, "w", encoding="utf-8"))
        return t0
    except Exception:
        return 0.0


def _done_path(prefix):
    return os.path.join(OUTDIR, "%s_done.json" % prefix)


def _done_load(prefix):
    try:
        return json.load(open(_done_path(prefix), encoding="utf-8"))
    except Exception:
        return {}


def _done_save(prefix, d):
    try:
        json.dump(d, open(_done_path(prefix), "w", encoding="utf-8"),
                  ensure_ascii=False)
    except Exception:
        pass


def _shot_done(prefix, i, n, t0=0.0):
    """这一镜本轮是不是已经渲完了 —— 续渲用。

    帧清单以 shots_frames.json 为准，不按 "{prefix}_s{i}_{n:05d}_.png" 猜文件名：
    ComfyUI 的 SaveImage 遇到同名文件会**顺延编号**（同一剧本重跑，第二轮是 _00249_
    起），按固定文件名查会永远查不到，续渲就白做了。
    还需要 mtime ≥ 本轮起跑时间，避免把上一轮（甚至上周）的同名旧帧当成已完成。
    """
    ent = _done_load(prefix).get(str(i)) or {}
    fs = [f for f in (ent.get("frames") or []) if os.path.exists(f)]
    if len(fs) < max(1, int(n) * 0.98):
        return False
    if t0 and max(os.path.getmtime(f) for f in fs) < t0:
        return False
    au = ent.get("audio") or ""
    if au and os.path.exists(au):
        return (not t0) or os.path.getmtime(au) >= t0
    for ext in ("flac", "wav", "mp3", "m4a"):
        for f in glob.glob(os.path.join(OUTDIR, "%s_a%d_*.%s" % (prefix, i, ext))):
            if not t0 or os.path.getmtime(f) >= t0:
                return True
    return False


def stage_video_r2v(script, prefix, only, key_paths, res=None, steps=8,
                    resume=False, t0=0.0):
    """逐镜跑 Ref2VA。帧存成 {prefix}_s{i}_*，音频存成 {prefix}_a{i}_*，
    正好对上 stage_native_map / build_manju_mp4 的约定。"""
    import gen_ref2va as R
    stopflag.check()
    # 步数跟着 LoRA 走，别按界面档位硬塞。
    # 这个引擎挂的是 ref2v_turbo_**4step** 的蒸馏 LoRA，界面默认传 8 步 ——
    # 等于把同一段轨迹又走了一遍，时间翻倍而且不会更清楚（蒸馏模型过采样还会发肉）。
    # config 里写 "r2v_steps": 8 可以强行改回去。
    try:
        _ls = 4 if "4step" in (getattr(R, "LORA", "") or "") else int(steps)
        _want = int(_cfg("r2v_steps") or _ls)
        if _want != int(steps):
            print("[r2v] 步数按 LoRA 校正：%d → %d（%s）"
                  % (int(steps), _want, getattr(R, "LORA", "")), flush=True)
        steps = _want
    except Exception:
        pass
    w, h = res or (576, 1024)
    w = max(256, int(w) // 32 * 32)
    h = max(256, int(h) // 32 * 32)
    H3.W, H3.H = w, h
    shots = script["shots"]
    frames_by_shot = {}
    # 本片已跑各镜的运动量：用来给后面的镜做相对参照。
    # 打斗片普遍 5~10、情感片普遍 1.5~3，用同一个绝对阈值永远有一头是错的。
    _done_mv = []
    for i, s in enumerate(shots, 1):
        stopflag.check()
        if only and i not in only:
            continue
        key = key_paths[i - 1] if i - 1 < len(key_paths) else ""
        refs = _r2v_refs(script, s, key)
        wav = _r2v_voice_file(script, (s.get("who") or "").strip())
        n = int(s.get("len") or NATIVE_MIN_FRAMES)
        # 续渲：上一轮已经渲完的镜直接跳过（帧和音轨都在才算完成，见 _shot_done）
        if resume and _shot_done(prefix, i, n, t0):
            # 用完成标记里记的那 124 帧，不能 glob 整个目录 —— 同一剧本重跑过，
            # output 里会同时躺着上一轮的 124 张（同名不同编号），glob 出来是 248 张，
            # 拼进成片就是"旧画面+新画面"双倍时长，口型也全对不上。
            _ent = _done_load(prefix).get(str(i)) or {}
            _fs = [f for f in (_ent.get("frames") or []) if os.path.exists(f)]
            if not _fs:
                _fs = sorted(glob.glob(os.path.join(
                    OUTDIR, "%s_s%d_*.png" % (prefix, i))))
            frames_by_shot[i - 1] = _fs
            print("[r2v] 第%d镜 上一轮已渲完（%d 帧），续渲跳过" % (i, len(_fs)),
                  flush=True)
            PG.emit("视频", 0.30 + 0.55 * i / max(1, len(shots)),
                    "第 %d/%d 镜（已完成，跳过）" % (i, len(shots)))
            continue
        # 这一镜该动多少：动作/可能动作/静态 三档，认不出按"可能动作"要求
        need_name, need_val, _cap = motion_need(
            (s.get("key") or "") + " " + (s.get("prompt") or ""),
            s.get("size") or "")
        # 重跑次数按档位封顶：静态镜一次都不重跑（它本来就该静），
        # 可能动作只给 1 次，明确写了动作的才给满 _R2V_RETRY。
        # 不这么分档，遇到"刻意的静止情绪镜"会把整片时间翻倍。
        _retry_cap = min(_R2V_RETRY, _cap)
        cands = []          # (运动量, 帧, 音频, 第几次尝试)
        for attempt in range(0, _retry_cap + 1):
            if attempt:
                stopflag.check()
            prompt = _r2v_prompt(script, s, boost=attempt)
            pfx = ("%s_s%d" % (prefix, i) if attempt == 0
                   else "%s_s%dr%d" % (prefix, i, attempt))
            PG.emit("视频", 0.30 + 0.55 * (i - 1) / max(1, len(shots)),
                    "第 %d/%d 镜 · %d 步%s" % (i, len(shots), steps,
                                              "（强化动作重跑 %d）" % attempt
                                              if attempt else ""))
            wf = R.build_wf(prompt, refs, wav, length=n,
                            seed=770001 + i * 101 + attempt * 9176,
                            prefix=pfx,
                            audio_prefix="%s_a%d" % (prefix, i),
                            steps=steps, width=w, height=h)
            pid = R.submit(wf)
            if not pid:
                print("[r2v] 第%d镜提交失败（尝试 %d）" % (i, attempt), flush=True)
                continue
            print("[r2v] 第%d镜已提交 %s（参考图 %d 张 / 音色样本 %s / %d 帧 / %d步 / 运镜：%s%s）"
                  % (i, pid, len(refs), wav or "无", n, steps,
                     (s.get("prompt") or "默认")[:12],
                     " / 强化动作" if attempt else ""), flush=True)
            # 等待时长按帧数缩放：H3 的时间注意力是平方复杂度，300+ 帧的镜
            # 一个采样步就要 40+ 分钟（lowvram 还会卸载换入换出），固定 1 小时
            # 必然把大镜误判成"0 帧"（2026-11-03 第4镜实翻车：渲了 3 小时，
            # 流水线 1 小时就放弃了，帧其实会正常落盘）。
            imgs, auds = R.wait(pid, pfx, timeout=max(3600, int(n) * 60))
            if not imgs:
                # 等不到帧 = 超时或任务失败。绝不能当"完成 0 帧"收下：
                # 还能重试就换 seed 重提，重试额度用完才放弃（不写 done，
                # 下一轮续渲会自动补渲这一镜）。
                print("[r2v] 第%d镜 第%d次：未等到帧（超时或任务失败）"
                      % (i, attempt + 1), flush=True)
                if attempt < _retry_cap:
                    continue
                print("[r2v] 第%d镜 重试额度已用完仍无帧，跳过该镜（下次续渲补）"
                      % i, flush=True)
                continue
            mv = motion_score(imgs)
            cands.append((mv if mv is not None else -1.0, imgs, auds, attempt))
            print("[r2v] 第%d镜 第%d次：%d 帧，运动量 %s（%s档要求 ≥%.1f）"
                  % (i, attempt + 1, len(imgs),
                     ("%.2f" % mv) if mv is not None else "测不出",
                     need_name, need_val), flush=True)
            if mv is None or mv >= need_val:
                break
            # 【只有真凝固才重跑】判定档位是靠文案关键词猜的，可能把静态镜猜成动作镜。
            # 如果只要"没达标"就重跑，误判一镜就白烧两轮（一部片能多烧四十分钟）。
            # 改成：已经有动作痕迹（≥1.2）就不再烧时间，只在报告里提一句；
            # 真正接近凝固的（<1.2）才重跑 —— 那才是模型没动起来。
            # 相对参照：已经跑过 ≥3 镜、这一镜又明显比本片基准静 → 值得重跑。
            # （固定阈值在打斗片里形同虚设：6.10 远超 2.5，可这一镜只有 1.9 就是漏了）
            _base = sorted(_done_mv)[len(_done_mv) // 2] if len(_done_mv) >= 3 else 0.0
            _rel_low = bool(_base and mv < _base * 0.45)
            if mv >= MOTION_HARD_STILL and not _rel_low:
                print("[r2v] 第%d镜 运动量 %.2f 略低于 %s档的 %.1f，但已有动作痕迹"
                      "（本片基准 %.2f），不再重跑（省时间）"
                      % (i, mv, need_name, need_val, _base), flush=True)
                break
            if _rel_low:
                print("[r2v] 第%d镜 运动量 %.2f 明显低于本片基准 %.2f（其他镜多在这条线上），"
                      "判定该动没动 → 重跑" % (i, mv, _base), flush=True)
            elif mv < MOTION_HARD_STILL:
                print("[r2v] 第%d镜 运动量 %.2f 低于凝固线 %.1f → 重跑"
                      % (i, mv, MOTION_HARD_STILL), flush=True)
            if attempt < _retry_cap:
                print("[r2v] 第%d镜 动得不够（%.2f < %.1f），换 seed + 把动作提到最前重跑"
                      % (i, mv, need_val), flush=True)
            elif _retry_cap == 0:
                print("[r2v] 第%d镜 运动量 %.2f 偏低，但这一镜按分镜是静态镜，不重跑"
                      % (i, mv), flush=True)
            else:
                print("[r2v] 第%d镜 已重跑 %d 次仍只有 %.2f —— 保留最好的一版，"
                      "并在质检里点名（建议：把动作写进首帧描述，或换个更明确的动作词）"
                      % (i, _retry_cap, mv), flush=True)
        if not cands:
            print("[r2v] 第%d镜 全部尝试都提交失败" % i, flush=True)
            continue
        # 取"动得最多"的那一版；全都达标就取第一次（省得白挑）
        cands.sort(key=lambda c: c[0], reverse=True)
        best_mv, best_imgs, best_auds, best_at = cands[0]
        if not best_imgs:
            # 一帧都没有：绝不写 done（写了续渲就永远跳过这镜，成片缺一镜）
            print("[r2v] 第%d镜 失败：所有尝试都没有产出帧，本轮成片将不含该镜；"
                  "下次点「开始生成」会自动补渲" % i, flush=True)
            continue
        frames_by_shot[i - 1] = best_imgs
        # 挑中的不是第一次 → 把它复制成规范名，保证拼接/续渲都按 _s{i}_ 取得到
        if best_at > 0 and best_imgs:
            for _k, _src in enumerate(best_imgs):
                _dst = os.path.join(OUTDIR, "%s_s%d_%05d_.png" % (prefix, i, _k + 1))
                try:
                    shutil.copyfile(_src, _dst)
                except Exception:
                    pass
        if best_mv is not None and best_mv > 0:
            _done_mv.append(best_mv)
        # 立刻记下"这一镜完成"：全片跑完才写 shots_frames.json，中途停掉就什么都没有，
        # 续渲只能从头再来。这个标记 + 文件 mtime 就是续渲的依据。
        try:
            _d = _done_load(prefix)
            _d[str(i)] = {"frames": list(best_imgs),
                          "audio": (best_auds or [""])[0],
                          "t": time.time()}
            _done_save(prefix, _d)
        except Exception:
            pass
        print("[r2v] 第%d镜完成：%d 帧，音频 %s，采用第 %d 次（运动量 %.2f）"
              % (i, len(best_imgs), (best_auds or ["无"])[0], best_at + 1, best_mv),
              flush=True)
    try:
        json.dump({"shots": [frames_by_shot.get(k, []) for k in range(len(shots))]},
                  open(os.path.join(OUTDIR, "shots_frames.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
    except Exception:
        pass
    return frames_by_shot


def run(script, prefix="", length=90, turbo=True, skip_keys=False,
         skip_video=False, skip_voice=False, only=None, res=None, steps=None,
         te4b=False, te_mode="streaming", unet="", auto_len=False, native=True,
         t2v=False, resume=False, qc=True, skip_rendered=False):
    """核心编排：可直接被其它脚本调用（写漫剧.py 就用它）。

    res   : (宽, 高)，默认 576x1024；想快就传 (448, 800) 或 (384, 672)
    steps : 覆盖采样步数（默认 turbo=8 / 非 turbo=18）；4 步同样接 4step LoRA
    te4b  : True = 用 Qwen3-VL-4B + ClipProj 轻量文本编码器（15.7G->4.9G）
    """
    QC_ACTIVE[0] = bool(qc)     # 审片开关（默认开；GUI 可以传 False）
    shots = script.get("shots", [])
    if not shots:
        print("[ERROR] 剧本无 shots"); return
    # 剧本可自带 voices 音色表（覆盖/追加到全局 VOICE_DESC），支持不同题材换音色
    _voices = dict(script.get("voices") or {})
    # A 方案：GUI 给每个角色排好的音色档案（edge-tts 那套 voice/rate/pitch）翻译成嗓音描述，
    # 这样原生发声也能"一人一把嗓子"，跨镜不变。显式 voices 优先级更高。
    _char_v = script.get("char_voices") or {}
    if _char_v:
        _looks = script.get("char_descs") or {}
        _gen = {n: voice_desc(n, p, _looks.get(n, "")) for n, p in _char_v.items() if p}
        _voices = {**_gen, **_voices}
    if _voices:
        H3.VOICE_DESC = {**H3.VOICE_DESC_BASE, **_voices}
        print(f"[voices] 音色表已生效（{len(_voices)} 个说话人）：", flush=True)
        for _n, _d in _voices.items():
            print(f"   {_n} → {_d}", flush=True)
    prefix = prefix or slug(script.get("title", "drama"))
    # 跳过已渲镜（自动）：只要关键帧文件在就跳过（不比对本轮参数）。
    # 顺带按 resume 语义取旧 runmark，视频阶段「上一轮已渲完的镜」也能接上。
    # 指定 only（只渲某几镜）时不生效 —— 那是「重渲」意图，必须真渲。
    if skip_rendered and not only:
        resume = True
    _t0 = _run_mark(prefix, resume=resume)
    voice = script.get("voice") or "zh-CN-XiaoxiaoNeural"
    if isinstance(only, str):
        only = set(int(x) for x in only.split(",") if x.strip()) if only else None
    steps = int(steps) if steps else (8 if turbo else 18)

    # 分辨率必须是 32 的倍数: H3 视频 latent = 1/16 采样 + 2x2 patch，
    # 宽或高不是 32 的倍数时 latent 会出现奇数边长，patchify 直接抛 shape 错。
    if res:
        def _snap32(v):
            n = int(round(v / 32.0)) * 32
            return max(256, n)
        ww, hh = _snap32(res[0]), _snap32(res[1])
        if (ww, hh) != tuple(res):
            print(f"[res] {res[0]}x{res[1]} 不是 32 的倍数，自动改成 {ww}x{hh}"
                  f"（H3 的 patch 要求）", flush=True)
        res = (ww, hh)

    # 成片画布: 渲染分辨率放大 1.25 倍（竖屏 576x1024 -> 720x1280，横屏 1024x576 -> 1280x720）
    _rw, _rh = res or (576, 1024)
    os.environ["OUT_W"] = str(int(round(_rw * 1.25 / 2)) * 2)
    os.environ["OUT_H"] = str(int(round(_rh * 1.25 / 2)) * 2)

    mode_txt = ("A方案·原生发声（模型自己念，音画同源）" if native
                else "B方案·TTS配音（edge-tts 后期配音）")
    print(f"=== make_drama === 前缀={prefix}  镜数={len(shots)}  {mode_txt}  "
          f"turbo={turbo}({steps}步)  "
          f"{'length=按台词预估' if (auto_len or native) else 'length=' + str(length)}  "
          f"res={res or (576, 1024)}  voice={voice} ===")
    PG.reset()
    PG.emit("准备", 0.01, f"共 {len(shots)} 镜")

    # ① 关键帧（Z-Image）—— 本机未装 Z-Image 模型，用 --t2v 让 H3 直接文生视频
    if t2v:
        key_paths = []
        print("[keys] t2v 模式：跳过 Z-Image 关键帧，H3 直接文生视频"
              "（场景+风格+运镜合并进 prompt）")
    elif skip_keys:
        key_paths = []
        for i in range(1, len(shots) + 1):
            cand = sorted(glob.glob(os.path.join(OUTDIR, f"{prefix}_key_{i}_*.png")))
            key_paths.append(cand[-1] if cand else None)
        print(f"[keys] 跳过，复用 {sum(1 for p in key_paths if p)}/{len(shots)} 张已有关键帧")
    else:
        key_paths = stage_keys(script, prefix, only, res=res, resume=resume,
                               t0=_t0, force_skip=skip_rendered and not only)

    # 说话人 ID 每部片子从 (S1) 重新编号 —— 官方语法里 (S1) 是全片共用的稳定标识，
    # 不清空的话上一部剧的编号会带过来，音色跟上一部串。
    try:
        H3.reset_speakers()
    except Exception:
        pass

    # ② A 方案（默认）：不配音 —— 台词直接写进 prompt，模型在同一个 latent 里
    #    一起长出画面和语音，口型和台词天然同步。帧数按台词字数预估。
    if native:
        stopflag.check()
        lens = plan_native_lengths(script)
        print("[auto] 按台词预估逐镜定帧：" + "  ".join(
            f"#{i + 1}={n}帧({n / 24.0:.1f}s)" for i, n in enumerate(lens)), flush=True)
        w0, h0 = res or (576, 1024)
        # 本机实测（RTX 3060 12G / Q3_K / 4B 编码 / 8 步）：576x1024 约 6 秒/帧，
        # 原来按 8.5 秒/帧 估会虚高近一半，跑起来以为要 4 小时，其实 2.5-3 小时。
        spf = 6.0 * (w0 * h0) / (576 * 1024.0)
        est = sum(lens) * spf / 60.0
        print(f"[auto] 共 {sum(lens)} 帧 @{w0}x{h0}，按 {spf:.1f} 秒/帧 粗估 ≈ "
              f"{int(est // 60)} 小时 {int(est % 60)} 分（不含关键帧；实际以界面实测剩余时间为准）",
              flush=True)
        PG.emit("定帧完成", 0.19, f"{len(lens)} 镜共 {sum(lens)} 帧")
        print(f"[engine] 本次视频路线：{script.get('engine') or '(未设置，走默认 i2v 链)'}",
              flush=True)
        if not skip_video:
            if (script.get("engine") or "") == "r2v":
                # 走 r2v 却没有 ref2va 底模 → 会在下面直接报错。提前说清楚，
                # 免得日志只留一句"模型加载失败"，看不出是资产缺了。
                # 音色克隆通道：参考图锁人、参考音频锁嗓子
                stage_video_r2v(script, prefix, only, key_paths, res=res,
                                steps=steps, resume=resume, t0=_t0)
            else:
                stage_video(script, prefix, length, steps, turbo, only, key_paths, res=res,
                            te4b=te4b, te_mode=te_mode, unet=unet, native_audio=True,
                            t2v=t2v)
        else:
            print("[video] 跳过，复用已有 shots_frames.json")
        vmp = stage_native_map(prefix, script)
        os.environ["AUDIO_MODE"] = "native"
    # ② B 方案（--no-native）：先用 edge-tts 配音，拿到真实台词长度再逐镜定帧数
    elif auto_len:
        stopflag.check()
        if skip_voice:
            vmp = os.path.join(OUTDIR, f"{prefix}_voice_map.json")
            if not os.path.exists(vmp):
                print("[voice] 跳过但映射表不存在，按 fallback 帧数继续"); vmp = ""
        else:
            vmp = stage_voice(script, prefix, voice)
        lens = plan_lengths(script, vmp, fallback=length)
        print("[auto] 按台词逐镜定帧：" + "  ".join(
            f"#{i + 1}={n}帧({n / 24.0:.1f}s)" for i, n in enumerate(lens)), flush=True)
        est = sum(lens) * 6.0 / 60.0          # 本机实测约 6 秒/帧（Q3 + 4B 编码 + 576x1024）
        print(f"[auto] 共 {sum(lens)} 帧，按 6.0 秒/帧 粗估 ≈ {int(est // 60)} 小时 "
              f"{int(est % 60)} 分（不含关键帧/配音；实际以界面实测剩余时间为准）", flush=True)
        PG.emit("定帧完成", 0.19, f"{len(lens)} 镜共 {sum(lens)} 帧")
        stopflag.check()
        if not skip_video:
            stage_video(script, prefix, length, steps, turbo, only, key_paths, res=res,
                        te4b=te4b, te_mode=te_mode, unet=unet, t2v=t2v)
        else:
            print("[video] 跳过，复用已有 shots_frames.json")
    else:
        stopflag.check()
        if not skip_video:
            stage_video(script, prefix, length, steps, turbo, only, key_paths, res=res,
                        te4b=te4b, te_mode=te_mode, unet=unet, t2v=t2v)
        else:
            print("[video] 跳过，复用已有 shots_frames.json")

        # ③ 配音
        stopflag.check()
        if skip_voice:
            vmp = os.path.join(OUTDIR, f"{prefix}_voice_map.json")
            if not os.path.exists(vmp):
                print("[voice] 跳过但映射表不存在，改用默认环境音"); vmp = ""
        else:
            vmp = stage_voice(script, prefix, voice)
        os.environ["AUDIO_MODE"] = "tts"

    # ④ 拼接
    stopflag.check()
    if vmp and os.path.exists(vmp):
        stage_build(prefix, vmp)
    else:
        import build_manju_mp4 as B
        os.environ["ADD_LOOP"] = "0"
        B.main()
        src = os.path.join(OUTDIR, "manju_drama.mp4")
        dst = os.path.join(OUTDIR, f"{prefix}_drama.mp4")
        if os.path.exists(src):
            if os.path.exists(dst): os.remove(dst)
            os.replace(src, dst)
            print(f"[build] 成片(纯环境音) -> {dst}")
    # ⑤ 出片质检：文件级问题（缺镜/缺音轨/时长对不上）当场报出来
    try:
        run_qc(prefix, script, None,
               os.path.join(OUTDIR, f"{prefix}_drama.mp4"))
    except Exception as _e:
        print("[QC] 质检没跑起来：%s" % _e, flush=True)
    print("=== done ===")
    PG.emit("完成", 1.0, "")


def main():
    ap = argparse.ArgumentParser(description="一键把剧本 JSON 渲染成竖屏 AI 漫剧")
    ap.add_argument("--script", required=True, help="剧本 JSON 路径")
    ap.add_argument("--prefix", default="", help="输出前缀（默认用 title）")
    ap.add_argument("--length", type=int, default=90, help="每镜视频帧数 @24fps（默认90≈3.75s）")
    ap.add_argument("--turbo", dest="turbo", action="store_true", default=True,
                    help="官方 8 步蒸馏加速（默认开）")
    ap.add_argument("--no-turbo", dest="turbo", action="store_false",
                    help="关闭加速，用 18 步（更稳但慢）")
    ap.add_argument("--skip-keys", action="store_true", help="跳过 Z-Image 关键帧（复用已有）")
    ap.add_argument("--skip-video", action="store_true", help="跳过 H3 渲染（复用 shots_frames.json）")
    ap.add_argument("--skip-voice", action="store_true", help="跳过配音（复用已有 voice_map）")
    ap.add_argument("--only", default="", help="只重做指定镜号，逗号分隔，如 2,4")
    ap.add_argument("--auto-len", action="store_true",
                    help="按每句台词的实际配音时长逐镜定帧（17~124 帧，仅 B 方案用）")
    ap.add_argument("--native", dest="native", action="store_true", default=True,
                    help="A 方案（默认）：台词写进 prompt，模型自己发声，音画同源、口型天然同步")
    ap.add_argument("--no-native", dest="native", action="store_false",
                    help="B 方案：关掉原生发声，回到 edge-tts 后期配音（音色可控但口型靠模型猜）")
    ap.add_argument("--te4b", dest="te4b", action="store_true", default=True,
                    help="4B 轻量文本编码（省内存/快，默认开，与 GUI 一致）")
    ap.add_argument("--no-te4b", dest="te4b", action="store_false",
                    help="关闭 4B 轻量编码，用 32B 完整编码器（更准但每镜编码慢）")
    ap.add_argument("--res", default="", help="分辨率，如 720x1280（默认 576x1024）")
    ap.add_argument("--t2v", action="store_true",
                    help="H3 直接文生视频（跳过 Z-Image 关键帧；本机未装 Z-Image 时用）")
    a = ap.parse_args()

    if not os.path.exists(a.script):
        print("[ERROR] script 不存在:", a.script); sys.exit(1)
    script = json.load(open(a.script, encoding="utf-8"))
    res = None
    if a.res:
        try:
            ww, hh = [int(x) for x in a.res.lower().replace("*", "x").split("x")]
            res = (ww, hh)
        except Exception:
            print("[ERROR] --res 格式应为 宽x高，如 720x1280"); sys.exit(1)
    run(script, prefix=a.prefix, length=a.length, turbo=a.turbo,
        skip_keys=a.skip_keys, skip_video=a.skip_video,
        skip_voice=a.skip_voice, only=a.only, auto_len=a.auto_len,
        native=a.native, res=res, t2v=a.t2v)


if __name__ == "__main__":
    main()
