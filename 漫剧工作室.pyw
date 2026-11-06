# -*- coding: utf-8 -*-
"""
漫剧工作室.pyw —— 图形界面版 AI 漫剧生成器
==========================================
双击「启动漫剧工作室.bat」即可打开（无需碰代码）。
用法：
  1) 点【启动后端】，等状态变绿「● 后端在线」
  2) 填写剧名、每镜的「画面描述」和「旁白台词」（可加/删镜）
  3) 点【开始生成 ▶】，等右下日志跑完，成片在 output/
"""
import os, sys, time, threading, subprocess, queue, contextlib, shutil, re, glob
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog, simpledialog

import app_paths as AP
HERE = AP.APP_DIR

# ---- 崩溃留痕（2026-10-29）----
# 打包用的 console=False，双击启动时 sys.stderr 是 None：任何未捕获异常（含定时器回调里
# 的异常）都看不到，表现为「打开后等会就自己退出」。这里把 stdout/stderr 和所有未捕获
# 异常都写进 exe 同目录的 崩溃日志.txt，下次再闪退就有据可查。
import traceback as _tb
_CRASH_LOG = os.path.join(AP.APP_DIR, "崩溃日志.txt")
try:
    if sys.stdout is None or sys.stderr is None:
        _logf = open(_CRASH_LOG, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = _logf
except Exception:
    pass


def _crash(msg):
    try:
        with open(_CRASH_LOG, "a", encoding="utf-8") as _f:
            _f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _excepthook(t, v, tb):
    _crash("【未捕获异常】\n" + "".join(_tb.format_exception(t, v, tb)))


sys.excepthook = _excepthook
_crash("=== 启动 ===  argv=%s  cwd=%s" % (sys.argv, os.getcwd()))
# ---- 崩溃留痕 结束 ----

os.chdir(AP.WORKDIR)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 避开本机 127.0.0.1 代理（否则 localhost 请求会被代理拦成 502）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
          "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import make_drama as M
import model_map as MM
import gen_banana_voice as VT
import ui_theme as TH
import webview2_host as WV          # 把 ComfyUI 网页嵌进界面用（系统 Edge 内核）

# 角色定妆照锚定表：{角色名: ComfyUI input 目录下的文件名}
# 关键帧原本是纯文生图（每镜各自长一张脸），绑了定妆照后该镜改走 img2img，
# 以定妆照为起点重绘，五官跨镜保持同一个人。
CHAR_REFS = dict(AP.CFG.get("char_refs") or {})
CHAR_DESCS = dict(AP.CFG.get("char_descs") or {})   # 角色名 → 统一外貌文案
CHAR_SHEETS = dict(AP.CFG.get("char_sheets") or {})  # 角色名 → 三视图设定稿文件名
CHAR_VOICES = dict(AP.CFG.get("char_voices") or {})  # 角色名 → {voice, rate, pitch} 固定嗓音档案

# 场景照锚定表：{场景名: ComfyUI input 目录下的文件名}
# 跟角色定妆照是两件套里的另一件：定妆照锁「这张脸」，场景照锁「这个地方」。
# 同一场景的多镜共用一张场景照 + 一段统一场景文案，跨镜才不会每镜换个地方。
SCENE_REFS = dict(AP.CFG.get("scene_refs") or {})
SCENE_DESCS = dict(AP.CFG.get("scene_descs") or {})  # 场景名 → 统一场景文案


def _adopt_chars_from_workdir():
    """exe 的 config.json 在 exe 同目录，源码运行的在工程根目录——两边都认。
    免得换个启动方式就"定妆照全丢了"（用户踩过）。"""
    if CHAR_REFS:
        return
    try:
        import json as _j
        p = os.path.join(AP.WORKDIR, "config.json")
        if not os.path.exists(p):
            return
        c = _j.load(open(p, encoding="utf-8"))
        CHAR_REFS.update(c.get("char_refs") or {})
        CHAR_DESCS.update(c.get("char_descs") or {})
        CHAR_SHEETS.update(c.get("char_sheets") or {})
        CHAR_VOICES.update(c.get("char_voices") or {})
        SCENE_REFS.update(c.get("scene_refs") or {})
        SCENE_DESCS.update(c.get("scene_descs") or {})
        if CHAR_REFS:
            print("[char] 从 %s 载入 %d 个角色" % (p, len(CHAR_REFS)))
    except Exception as e:
        print("[char] 回退载入失败:", e)
# 有人的镜头必须挂角色；纯空镜（渡口全景、兵器特写、夕阳拉远）选这个，不算漏填
NOBODY = "空镜·无人物"

# 路径 / 后端配置全部走 app_paths（支持 config.json 自定义，方便发给别人用）
COMFY_MAIN = AP.COMFY_MAIN
COMFY_URL = AP.COMFY_URL
# 无限画布（ComfyUI-Local-Infinite-Canvas 前端），独立跑在 :3000，
# 只「接管」现有 :8188 的 ComfyUI，不改它的任何文件 / 模型 / 配置。
CANVAS_URL = AP.CANVAS_URL
COMFY_PYTHON = AP.COMFY_PYTHON
OUTDIR = AP.OUTDIR
INPUTDIR = AP.INPUTDIR
EXTRA = AP.EXTRA
PORT = AP.PORT
FONT = ("Microsoft YaHei", 10)
FONT_B = ("Microsoft YaHei", 11, "bold")

# 运镜预设：直接照搬教程里验证过的写法（「不要写360°环绕」「不要极特写」这些坑也一并避开）
# 单独命名成常量，是因为「自动识别」（见下面的 guess_motion）要复用同一批文案 ——
# 下拉里能选的，和自动判出来的，必须是同一句话，否则用户会以为识别出了个新东西。
MOTION_PUSH = "镜头缓慢向前推进，主体逐渐放大，背景轻微虚化，画面稳定顺滑"
MOTION_PULL = "镜头缓慢向后拉远，不断展现周围环境，主体慢慢变小，运镜平稳"
MOTION_ARC = "镜头围绕主体缓慢圆弧环绕，主体保持中心，光影随角度轻微变化"
MOTION_RISE = "镜头缓慢垂直上升，逐步展现全景"
MOTION_DOWN = "镜头缓缓下降靠近人物"
MOTION_PAN = "镜头轻微左摇，跟随人物移动，画面稳定"
MOTION_FIX = "固定机位，人物细微动作，衣袂轻动，风过发丝"
MOTION_HAND = "手持轻微晃动跟拍，紧张感"
# 双人同框对白用：左右横移比"推近"更能保住两个人的脸不糊在一起
MOTION_TRACK = "镜头轻微横移，保持人物在画面中心，画面稳定顺滑"

MOTION_PRESETS = [
    MOTION_PUSH, MOTION_PULL, MOTION_ARC, MOTION_RISE, MOTION_DOWN,
    MOTION_PAN, MOTION_FIX, MOTION_HAND, MOTION_TRACK,
]

# 景别：每镜一个下拉，选了就直接写进提示词，不再靠从文字里猜。
# 教程里「像电影还是像 PPT」主要就差在这一项。
SIZE_PRESETS = ["远景", "全景", "中景", "近景", "特写", "局部特写"]
# 景别文案按那份「10 个最常用景别提示词」重写过：原来每句只有一个形容词，
# 现在是「取景范围 + 焦段 + 景深 + 质感」，模型对这四样最敏感（H3 会把 85mm/浅景深 当指令执行）。
SIZE_TEXT = {
    "远景": "极远景，大远景，主体在画面远处只是一个很小的人影，环境占据画面九成以上，"
            "定场镜头，宏大尺度感，24mm 广角镜头，大景深全景清晰，电影感",
    "全景": "远景，全景，全身镜头，人物从头到脚完整入画，头顶留出留白、脚下留出地面，"
            "环境清晰可辨，35mm 镜头，透视自然不变形",
    "中景": "中景，半身镜头，从腰部以上取景，双手完整出现在画面内，背景微微虚化，"
            "50mm 标准镜头，透视接近人眼",
    "近景": "近景，胸部以上取景，头部、肩膀和上半胸入画，看不到手，85mm 中长焦镜头，"
            "背景柔和虚化，保留真实皮肤质感",
    "特写": "特写，整张脸从边框到边框铺满画面，额头与下巴贴近上下边缘，背景完全虚化，"
            "135mm 长焦镜头，极浅景深，真实皮肤纹理可见毛孔，眼中有柔和眼神光",
    "局部特写": "局部特写，只拍物件或部位，不要出现人脸，背景高度虚化，材质与质感分明，"
               "105mm 中长焦，浅景深",
}


# ============================ 景别自动识别 ============================
# 为什么要自动识别：⑦ 景别以前是「留空＝按描述自动判断」——判断藏在
# _auto_prompt 里，用户在界面上看不见、下拉框永远是空的，也不知道
# 机器到底给这一镜判了什么景别。更糟的是：判错了也没法改，只能去改文案。
# 现在改成「先按文案自动填进 ⑦ 下拉框」，用户一眼能看到判成什么、
# 不满意直接改下拉框即可（手动改过的那一镜就不再被自动覆盖）。
#
# 判定要点：
#  1) 用「精确词」而非任意子串，避免"中景"抢走"中远景"这类误判；
#  2) "特写"配上手/剑/衣角这类部位词 → 局部特写（教程：局部特写只拍细节、
#     不出脸；光写"特写"会让模型硬挤一张脸进去）；
#  3) 一句里出现多个景别词时取「最后提到的那个」——编剧习惯把景别写在前面、
#     后面接着描述细节，取最后一个更贴近"这镜最终是什么景"。

# 部位词：跟"特写"搭配时判成局部特写
# 注意"手"要单列（描述里常写"周嵩的手按在…"而不是"手部"），
# 但放在最后 —— 正则里 | 是从左往右试，"手部"必须先于"手"命中。
_SIZE_PART_WORDS = ("手部", "手指", "指尖", "指节", "剑", "刀", "兵器", "衣角",
                    "脚步", "剑鞘", "刀锋", "血", "脚印", "袖口", "眼角", "睫毛",
                    "水珠", "灰尘", "裂纹", "手")
_SIZE_PART_ALT = "|".join(_SIZE_PART_WORDS)

# 「特写」和部位词必须**紧挨着**才算局部特写（中间最多隔一个"的"）。
# 为什么不能全文扫：描述里只要出现这个字就判局部特写会错得离谱 ——
# "特写：周嵩的脸，雨顺着刀身滴落" 明明拍的是脸，却因为句子里有个"刀"
# 被判成局部特写。紧邻匹配才能区分"手部特写"和"…刀身滴落"。
_SIZE_PART_RE = re.compile(
    r"(?:局部|细节|物品|微距)特写"          # 明写的局部特写
    r"|(?:%s)(?:的|之)?特写"                 # 手部特写 / 手指的特写 / 剑的特写
    r"|特写(?:的|之)?(?:%s)"                 # 特写的手 / 特写手部
    % (_SIZE_PART_ALT, _SIZE_PART_ALT))

# 脸/眼/表情这类词说明这一镜拍的是"脸"，就算同一小句里出现了部位词也仍然是特写
_SIZE_FACE_WORDS = ("脸", "眼", "眉", "嘴角", "表情", "目光", "眼神", "泪",
                    "面孔", "神情", "侧脸", "脸颊")

# 「特写：」冒号后面那一小句＝这一镜真正拍的主体
_SIZE_SUBJ_RE = re.compile(r"特写\s*[：:]\s*([^，。；！？]{0,20})")

# 每条：(景别, 精确词组)。越靠前＝越具体，同位置时优先。
_SIZE_RULES = [
    ("局部特写", ("局部特写", "细节特写", "特写手", "特写剑", "特写衣角",
                  "特写脚步", "手部特写", "物品特写", "微距特写", "局部镜头")),
    ("特写", ("大特写", "极特写", "面部特写", "表情特写", "脸部特写", "眼睛特写",
              "特写", "近特写")),
    ("近景", ("中近景", "近景", "胸像", "胸部以上")),
    ("中景", ("中景", "半身", "腰部以上", "七分身")),
    ("全景", ("全景", "全身", "人物全身", "全身入画", "远景全身")),
    ("远景", ("大远景", "远景", "广角", "航拍", "鸟瞰", "大场景", "环境全景")),
]

# 规则顺序＝具体程度（局部特写 > 特写 > 近景 > 中景 > 全景 > 远景），
# 同一位置命中多条时用这个顺序决胜。
_SIZE_ORDER = [n for n, _ in _SIZE_RULES]

# ---- 内容线索：剧本里没写"特写/中景"这些词时，从动作和细节反推景别 ----
# 真实剧本很少明写景别（用户那份《雨店来客》三镜一个景别词都没有），
# 只靠关键词等于识别不出来。所以再加一层：从"拍的是什么"反推 ——
# 拍到指尖/血珠 → 局部特写；拍到眼神/脸色 → 特写；凑近耳边 → 近景；
# 纵身/力劈 → 全景；远山/云雾 → 远景。
# 只在**没有**明写景别时才用这层（明写的永远优先）。
#
# 中景不设线索：中景是"默认档"，靠动作词（转身/坐下/走近）去猜太不准 ——
# "他转身掀帘走入茫茫雨夜"里的"转身"是在说动作，不是在说拍多大。
# 认不出时留空 = 按中景出图，结果一样，还更诚实。
_SIZE_CONTENT = [
    ("局部特写", ("指尖", "指节", "掌心", "刃尖", "剑锋", "血珠", "血水",
                  "鞋底", "鬓角", "锁眼", "袖口", "水滴", "灰尘", "裂纹")),
    ("特写", ("抬眼", "目光", "眼神", "瞳孔", "脸色", "狞笑", "咬牙", "泪",
              "嘴角", "笑意", "喘息")),
    ("近景", ("凑到", "凑近", "贴近", "耳边", "压低声音", "耳语")),
    ("全景", ("纵身", "飞身", "力劈", "拔出", "闯入", "奔逃", "跃起", "落地")),
    ("远景", ("远处", "远远", "孤零零", "独自", "天边", "尽头",
              "云雾", "山峦", "地平线", "一望无际")),
]


def guess_size(text):
    """按「① 画面描述」的措辞自动判断这一镜该用什么景别。

    返回 SIZE_PRESETS 里的一个；实在看不出来返回 ""（留空＝按默认走）。
    这是纯函数，不碰界面，方便单测。
    """
    t = (text or "").strip()
    if not t:
        return ""
    # 收集所有命中：(位置, 词, 景别)
    hits = []
    for name, kws in _SIZE_RULES:
        for k in kws:
            i = t.find(k)
            if i >= 0:
                hits.append((i, k, name))
    if not hits:
        return ""
    # "特写"＋部位 → 局部特写（只拍细节，不出脸）。两种判法：
    #   ① 部位词紧挨着"特写"（手部特写 / 特写的手）；
    #   ② 写成「特写：XXX」时，冒号后第一小句就是这一镜拍的主体，
    #      主体是手/剑/衣角这类部位 → 局部特写（"特写：周嵩的手按在柜台上"）。
    #      但主体里出现脸/眼/表情 → 拍的还是脸，算特写（"特写：周嵩的脸，
    #      雨顺着刀身滴落" —— 刀在第二小句，不该影响判定）。
    if _SIZE_PART_RE.search(t):
        return "局部特写"
    m = _SIZE_SUBJ_RE.search(t)
    if m:
        subj = m.group(1)
        if (any(w in subj for w in _SIZE_PART_WORDS)
                and not any(w in subj for w in _SIZE_FACE_WORDS)):
            return "局部特写"
    # 取位置最靠后的那条；位置相同则取更具体的景别
    hits.sort(key=lambda x: (x[0], -_SIZE_ORDER.index(x[2])))
    # 剧本里明写了景别 → 就用它，不再看内容线索
    return hits[-1][2]


def guess_size_loose(text, say=""):
    """景别识别的宽松版：明写没有就按内容线索反推。

    say 传本镜台词：有台词的镜一定拍得到嘴，所以内容线索即使指着"指尖/玉佩"
    也不判局部特写，退成近景（实测"阿箬指尖冻得发青 + 一段对白"被判成
    局部特写 → 关键帧没有脸，台词却要人来念）。

    和 guess_size 的区别：真实剧本很少写"特写/中景"这些词，
    全靠关键词等于识别不出来（用户那份三镜剧本一个景别词都没写）。
    所以这一版会在明写没有时，退到"拍的是什么"上去反推。

    取线索时有个重要规则：**近的景别优先于宽的景别**。
    "店内桌椅翻飞…他转身掀帘走入茫茫雨夜" 这句里，"掀帘"(全景)
    和"茫茫"都在描述动作和氛围，不是真的在拉远景；
    而"指尖""抬眼"这类才是真在拍细节/表情。
    所以：命中近景类线索时，宽景别线索不再覆盖它。
    """
    s = guess_size(text)
    if s:
        return s
    t = (text or "")
    if not t.strip():
        return ""
    # 写了"空镜"就是没有主体的环境镜 → 远景。
    # 不判远景的话会落到默认中景，模型就画半身人像进去，"空镜"直接废掉。
    if "空镜" in t or "无人" in t or "没有人影" in t:
        return "远景"
    # 【2026-10-29】强动作优先：一场打戏（劈/刺入/突进/翻飞/碎裂…）本来就该用
    # 全景交代"谁打谁、在哪打"。以前只按"最后提到的线索"决胜，第3镜因为结尾有
    # "掌心/玉佩"被判成局部特写 —— 结果提示词里同时写着"局部特写，不需要出现人脸"
    # 和"长剑力劈而下"，模型只能画成一个手部特写，打戏全没了。
    # 只在**没有明写景别**时生效（明写的永远优先），并且要求命中 ≥2 个动作词，
    # 避免"特写：剑锋劈下"这种单动作特写被误升成全景。
    _ACT = ("劈", "斩", "刺", "挑", "削", "撩", "崩", "抹", "划", "戳",
            "砍", "厮打", "交手", "格挡", "翻飞", "碎裂", "血溅", "突进",
            "贴地", "扑上", "扑出", "扑来", "踹", "相撞", "飞溅", "击飞",
            "撞飞", "缠斗", "劈落", "猛刺", "刀刃", "甩出", "横刀", "穿梭",
            "炸开", "迸开", "擦着")
    if len(set(k for k in _ACT if k in t)) >= 2:
        return "全景"
    near_hits = []      # (位置, 景别)
    wide_hits = []      # (位置, 景别)
    for name, kws in _SIZE_CONTENT:
        wide = name in ("全景", "远景")
        for k in kws:
            i = t.find(k)
            if i < 0:
                continue
            (wide_hits if wide else near_hits).append((i, name))
    if near_hits:
        # 有近景线索就用它（取最靠后的那条）
        near_hits.sort(key=lambda x: (x[0], -_SIZE_ORDER.index(x[1])))
        best = near_hits[-1][1]
        if best == "局部特写":
            if (say or "").strip():
                return "近景"    # 有台词 → 必须看得见人
            # 打斗镜里出现"手腕/掌心/剑锋"是正常的（打到哪写到哪），
            # 不能因此判局部特写 —— 那一档的提示词写着"不要出现人脸"，
            # 一场对打会被画成一只手（实测第6镜就栽在这）。
            if any(k in t for k in _ACT):
                return "中景"
        return best
    if wide_hits:
        wide_hits.sort(key=lambda x: (x[0], -_SIZE_ORDER.index(x[1])))
        return wide_hits[-1][1]
    return ""


def size_hint(text):
    """给「⑦ 景别」下拉旁边显示的一句人话：这一镜为什么被判成这个景别。"""
    s = guess_size(text)
    if not s:
        return "描述里没写景别，按中景出图"
    return "按描述自动识别：%s" % s


# ============================ 运镜自动识别 ============================
# 为什么要有这个：⑤ 运镜以前是纯手填，留空就走 DEFAULT_MOTION ——
# 全片几十镜全是同一句「镜头缓缓推进」。结果是：该拉远交代环境的全景在推、
# 该定住看细节的局部特写在推、打戏也在缓缓推，成片看着像一张图在放大。
#
# 教程里的规矩其实是「景别和动作决定运镜」，优先级从高到低：
#   打斗、兵器、冲刺   → 手持轻微晃动跟拍（最高：动作戏不能用定机位/缓推）
#   局部特写 / 细节物件 → 定住（推镜会把细节推糊）
#   特写 / 近景（情绪） → 缓慢推近
#   走动、进出、掀帘   → 摇移跟随
#   远景 / 全景        → 缓慢拉远，展现环境
#   中景双人对白       → 轻微横移，保住两张脸
#
# 判完直接填进 ⑤ 下拉框（用户能看见、能改），改过的那一镜就不再被自动覆盖。

# 强动作词：打起来了 / 砸东西 / 兵器出鞘。命中就直接手持跟拍，**不看景别** ——
# 反例就是上一版的坑：《雨夜断剑》第3镜整场是「力劈、突进、刺入、桌椅翻飞」，
# 但因为结尾有"掌心/玉佩"这类细节词，景别被判成局部特写，运镜就跟着定成
# "固定机位"——用定机位拍打戏，成片会像 PPT 一页页翻。动作永远优先于景别。
_MOTION_ACTION = ("劈", "斩", "刺入", "刺中", "捅", "砍", "挥剑", "挥刀",
                  "厮打", "交手", "格挡", "拳", "踹", "踢", "撞", "扑",
                  "翻飞", "碎裂", "血溅", "拔剑", "拔刀", "突进", "贴地")
# 弱冲突词：扣手腕/揪衣领/掐脖子。这类命中不抢景别的判断
# （"周嵩目光骤锐，猛地伸手扣住他的手腕"整镜是特写看脸，缓推比晃动机位对）
_MOTION_FIGHT = ("扣住", "揪住", "掐住", "挥出", "拔出", "扭住", "按住")
# 走位/进出：命中就用摇移跟随（远景全景不算，"走远"是交代环境不是跟人）
_MOTION_WALK = ("走进", "走入", "进店", "掀帘", "推门", "进门", "转身",
                "离开", "走出", "奔走", "奔向", "跑来", "走向", "追出", "踱")
# 双人同框：判断"这一镜是不是两个人都在画面里"
_MOTION_DUO = ("两人", "二人", "彼此", "并肩", "同框", "对峙", "对视",
               "对面", "对望", "相视", "相对",     # 「剑客与黑衣人对面而立」这种最常见
               "扣住他", "拽着", "凑到", "面前")


def guess_motion(scene, size="", say=""):
    """按「① 画面描述」+「⑦ 景别」+「② 台词」自动判这一镜该用什么运镜。

    返回 MOTION_PRESETS 里的一句（或 "" 表示看不出来、留给默认）。
    纯函数，不碰界面，方便单测。

    判定顺序的道理：**动作优先于景别，景别优先于氛围**。
    打戏就算拍的是手部特写，也得用手持跟拍（定机位拍打戏没有冲击）；
    反过来，安静的特写就该老老实实缓推，别乱晃。
    """
    t = (scene or "").strip()
    if not t:
        return ""
    sz = (size or "").strip()
    if sz not in SIZE_TEXT:
        sz = guess_size_loose(t)

    # 1) 强动作：不管景别，手持跟拍
    if any(k in t for k in _MOTION_ACTION):
        return MOTION_HAND

    # 2) 局部特写/细节物件：定住。推镜+手部细节＝最容易糊成一片的组合
    if sz == "局部特写":
        return MOTION_FIX

    # 3) 特写/近景：情绪推进（这一档推近几乎不会错）
    if sz in ("特写", "近景"):
        return MOTION_PUSH

    # 4) 弱冲突（扣手腕/揪衣领）：手持
    if any(k in t for k in _MOTION_FIGHT):
        return MOTION_HAND

    # 5) 走位/进出：摇移跟随
    if any(k in t for k in _MOTION_WALK):
        return MOTION_PAN

    # 6) 远景/全景：拉远交代环境
    if sz in ("远景", "全景"):
        return MOTION_PULL

    # 7) 剩下的中景：两个人都在画面里对白 → 横移保同框；否则缓慢推近
    if (say or "").strip() and any(k in t for k in _MOTION_DUO):
        return MOTION_TRACK
    return MOTION_PUSH


# 连续两镜用同一个运镜，看片的人会觉得"画面没动、像放幻灯片"。
# 归一的 guess_motion 只看单镜，看不到前一镜是什么，所以中景→推、近景→推这类
# 连着撞车是必然的（实测《雨夜断剑》第 2、3 镜都是"缓缓推进"）。
# 这张表只干一件事：撞车了就在同等合理的几个里换一个，不改变"这一镜该用什么"的
# 判断逻辑，只是在并列首选里挑个不同的。
_MOTION_ALT = {
    MOTION_PULL:  (MOTION_RISE, MOTION_ARC, MOTION_PUSH),
    MOTION_PUSH:  (MOTION_DOWN, MOTION_TRACK, MOTION_ARC),
    MOTION_TRACK: (MOTION_ARC, MOTION_PAN, MOTION_PUSH),
    MOTION_PAN:   (MOTION_TRACK, MOTION_ARC, MOTION_PULL),
    MOTION_FIX:   (MOTION_PUSH, MOTION_ARC, MOTION_DOWN),
    MOTION_HAND:  (MOTION_TRACK, MOTION_PUSH, MOTION_FIX),
    MOTION_ARC:   (MOTION_TRACK, MOTION_RISE, MOTION_PUSH),
    MOTION_RISE:  (MOTION_PULL, MOTION_ARC, MOTION_DOWN),
    MOTION_DOWN:  (MOTION_PUSH, MOTION_ARC, MOTION_FIX),
}


def motion_variety(motion, prev, size=""):
    """连镜运镜撞车时换一个同档位合理的；没撞车就原样返回。

    只对「自动识别」出来的运镜调用 —— 用户在下拉里手选的，撞车也要照他的来。
    """
    if not motion or not prev or motion != prev:
        return motion
    for alt in _MOTION_ALT.get(motion, ()):
        if alt != prev:
            return alt
    return motion


def motion_hint(scene, size="", say=""):
    """给 ⑤ 运镜 旁边显示的一句人话（和景别那套一致）。"""
    m = guess_motion(scene, size, say)
    return ("自动识别：%s" % _motion_short(m)) if m else "没认出来，用默认运镜"


def _motion_short(m):
    """把一句长运镜压成 4-6 个字，给提示文字用（完整句子太长，一格塞不下）。"""
    table = {
        MOTION_PUSH: "缓慢推近", MOTION_PULL: "缓慢拉远", MOTION_ARC: "圆弧环绕",
        MOTION_RISE: "垂直上升", MOTION_DOWN: "缓缓下降", MOTION_PAN: "摇移跟随",
        MOTION_FIX: "固定机位", MOTION_HAND: "手持跟拍", MOTION_TRACK: "轻微横移",
    }
    return table.get((m or "").strip(), "自定义")



# ============================ 场景照（跨镜锁地点） ============================
# 角色定妆照锁的是「这张脸」，场景照锁的是「这个地方」。
# 同一场戏里的多镜，如果每镜各画各的背景，观众看着就像在换地方——
# 这是 AI 漫剧最常见的穿帮之一，比换脸还难忍。
#
# 做法跟角色一模一样，也是两件套：
#   ① 场景照（一张图）→ 同一场景所有镜共用，关键帧 img2img 时当底图，
#      建筑布局、家具位置、光线方向跨镜就一致了；
#   ② 统一场景文案（一段文字）→ 每镜都原样贴上，锁住材质/色调/陈设细节。
#
# 场景从哪来：从① 画面描述里认（"客栈大堂""荒村野店""山门前"…），
# 认出来就自动建一个场景，场景照可以后补（没图就只用文案）。

# 地点词 → 场景名。命中即认成这个场景（按词表顺序取最长的那个）。
_SCENE_WORDS = (
    ("客栈大堂", "客栈", "客店", "旅店", "酒肆", "酒馆", "茶楼", "饭庄", "酒楼"),
    # "破庙"以前被并进这一组（早期只有《雨店来客》一部戏时图省事），
    # 结果新剧写"破庙里残火将熄"被判成"荒村野店" —— 直接绑上一部剧的店面照片，
    # 破庙变野店。破庙下面有自己的组，这里只留真正同类的荒村/茅屋。
    ("荒村野店", "野店", "荒村", "山村", "茅屋"),
    ("山门前", "山门", "山道", "山门石阶", "台阶"),
    ("山腹空地", "空地", "山腹", "山坳", "谷口"),
    ("长街", "长街", "街道", "街口", "街尾", "闹市", "集市", "市集"),
    ("桥上", "桥上", "桥头", "石桥", "拱桥", "断桥", "木桥"),
    ("芦苇荡", "芦苇荡", "芦苇", "苇丛", "芦花", "苇子"),
    ("河边", "河边", "河岸", "渡口", "码头", "水边"),
    ("庭院", "庭院", "院子", "天井", "院中", "门前"),
    ("屋内", "屋内", "房中", "房间", "卧房", "厢房", "厨房", "正堂", "大厅", "内室"),
    ("林间", "林间", "林中", "树林", "竹林", "林里"),
    ("雪原", "雪原", "雪地", "冰面"),
    ("战场", "战场", "沙场", "校场"),
    ("牢房", "牢房", "地牢", "监牢"),
    ("宫殿", "宫殿", "大殿", "正殿", "殿内", "金銮"),
    ("书院", "书院", "学堂", "书房", "私塾"),
    ("厢房外", "房外", "廊下", "回廊"),
    ("竹林深处", "竹海", "竹丛"),
    ("溪边", "溪边", "溪流", "涧边"),
    ("崖上", "崖上", "山崖", "绝壁"),
    ("渡船", "船上", "船舱", "船头"),
    ("城门", "城门", "城楼", "城墙", "箭楼"),
    ("驿站", "驿站", "官道", "官道旁"),
    ("破庙", "破庙", "庙宇", "庙里", "佛堂", "山神庙", "佛像", "残庙"),
)


def guess_scene(text):
    """从「① 画面描述」里认出这一镜在哪个场景；认不出返回 ""。

    纯函数，好单测。判定要点：
      · 取「最长的命中词」——"客栈大堂"和"客栈"同时命中时选前者，
        否则同一个地方会被拆成两个场景；
      · 同一句里出现多个地点词时，取**第一个**（中文习惯先交代地点）；
      · 认出来的是场景名，会被规范化（去空白）。
    """
    t = (text or "").strip()
    if not t:
        return ""
    best = ""
    best_pos = 10 ** 9
    for group in _SCENE_WORDS:
        name = group[0]
        for w in group[1:]:
            i = t.find(w)
            if i < 0:
                continue
            # 更长的词优先；长度相同取更靠前的
            if len(w) > len(best) or (len(w) == len(best) and i < best_pos):
                best = w
                best_pos = i
    if not best:
        return ""
    # 把命中词规范化成场景名（用词表里对应的规范名）
    for group in _SCENE_WORDS:
        if best in group[1:]:
            return group[0]
    return best


# 场景延续：同一场戏的后续几镜往往不再重复写地点（"油灯晃得光影乱颤"→还在那个店）。
# 但会有"陈设/光线"这类延续痕迹。命中这些词就说明"还在上一个场景"。
_SCENE_CONTINUE = ("油灯", "烛火", "灯笼", "柜台", "桌椅", "门环", "帘", "桌",
                   "凳", "床", "灶", "锅", "茶壶", "酒坛", "柜台", "窗棂",
                   "屋檐", "青砖", "木门", "梁", "墙")


def guess_scene_with_prev(text, prev=""):
    """识别场景；本镜没写地点时，看是不是延续上一个场景。

    prev 是上一镜认出来的场景名。命中"陈设/光线"这类延续痕迹就沿用 prev。
    """
    g = guess_scene(text)
    if g:
        return g
    if prev:
        # 没写地点词 → 默认还在这场戏里。
        # 以前必须命中"桌椅/门帘/柜台"这类陈设词才延续，于是纯动作镜
        # （"一步跨到阿七身前，长刀出鞘"）就掉成"无场景" → 关键帧失去场景锚定，
        # 背景每镜各画各的，观众看着像一直在换地方。
        # 行业做法本来就是"同场景多镜共享条件"，这里对齐：默认延续，
        # 真要换场就在文案里写明地点（写了就会被 guess_scene 抓走）。
        return prev
    return ""


def scene_hint(text):
    """场景识别的说明文字，给界面上的提示用。"""
    s = guess_scene(text)
    if not s:
        return "描述里没认出地点"
    return "按描述自动识别：%s" % s


# 场景统一文案模板：给一个场景起草"这个地方长什么样"的那段话
SCENE_DESC_TPL = ("{name}，{desc}，"
                  "电影感写实场景设定，{light}，真实材质与陈设细节，"
                  "空间布局清晰，高细节，8k")


def _scene_choices():
    """场景下拉框的候选：场景库里有的 + 当前分镜自动识别到的。"""
    names = list(SCENE_REFS.keys())
    for r in shot_rows:
        try:
            nm = guess_scene(r["scene"].get("1.0", "end") or "")
        except Exception:
            nm = ""
        if nm and nm not in names:
            names.append(nm)
    return names


def _refresh_all_scene_combos():
    """场景库变了以后，把每镜「⑧ 场景」下拉的候选同步一下。"""
    vals = _scene_choices()
    for r in shot_rows:
        cb = r.get("loc_cb")
        if cb is not None:
            try:
                cur = (r["loc_var"].get() or "").strip()
                cb.configure(values=([cur] if cur and cur not in vals else []) + vals)
            except Exception:
                pass


def _reidentify_all():
    """一键重新识别：所有分镜的 ⑤ 运镜 + ⑦ 景别 + ⑧ 场景，按① 画面描述重认一遍。

    手动指定过的运镜/景别/场景不会被覆盖（用户明确表达过的优先）。

    注意 size_manual / loc_manual / motion_manual 是 [False] 这种"可变标志"，
    不能直接当布尔用（非空列表恒为真），必须取 [0]。
    """
    n_sz = n_loc = n_mo = 0
    for r in shot_rows:
        try:
            if not (r.get("size_manual") and r["size_manual"][0]):
                if r.get("auto_size"):
                    r["auto_size"]()
                    n_sz += 1
        except Exception:
            pass
    # 运镜要排在景别之后：它拿刚认出来的景别当输入
    for r in shot_rows:
        try:
            if not (r.get("motion_manual") and r["motion_manual"][0]):
                if r.get("auto_motion"):
                    r["auto_motion"]()
                    n_mo += 1
        except Exception:
            pass
    # 场景要按顺序重认，后面的镜才能延续前面的（"油灯晃得…"还在同一个店）
    _refresh_all_locs()
    n_loc = sum(1 for r in shot_rows if (r.get("loc_var") or None)
                and (r["loc_var"].get() or "").strip())
    try:
        log_q.put("[识别] 已按① 画面描述重新识别：景别 %d 镜、运镜 %d 镜、场景 %d 镜\n"
                  % (n_sz, n_mo, n_loc))
    except Exception:
        pass
    return n_sz, n_loc


DEFAULT_MOTION = ("镜头缓缓推进，画面轻柔流动，微风拂动枝叶，电影感连续动态，"
                  "subtle cinematic camera drift, gentle motion, leaves swaying, "
                  "continuous fluid movement, no abrupt cuts, no static")
DEFAULT_STYLE = ",电影感写实摄影,自然光,高细节,禁止文字"


def center_win(win, w, h, minw=0, minh=0):
    """把窗口摆到屏幕正中（自动避开底部任务栏；屏幕太小时自动收敛尺寸）。

    Tk 不给位置时 Windows 会把窗口丢到左上角，所以每个窗口都要显式算一次。
    """
    try:
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    except Exception:
        try:
            win.geometry("%dx%d" % (w, h))
        except Exception:
            pass
        return
    w = max(400, min(w, sw - 40))
    h = max(300, min(h, sh - 90))          # 留出标题栏 + 任务栏
    # 弹窗优先居中于父窗口（压在父窗口上更像同一个程序），否则居中于屏幕
    px = py = pw = ph = None
    try:
        par = win.master
        if par is not None and par.winfo_exists() and par.winfo_ismapped():
            pw, ph = par.winfo_width(), par.winfo_height()
            px, py = par.winfo_rootx(), par.winfo_rooty()
    except Exception:
        pw = ph = None
    if pw and ph and pw > 200 and ph > 200:
        x = px + (pw - w) // 2
        y = py + (ph - h) // 3             # 略偏上，避免压住父窗口底部按钮
    else:
        x = (sw - w) // 2
        y = (sh - h) // 2 - 10
    x = max(0, min(x, sw - w))
    # 底部留 70px 给任务栏：原来只留 40，1080 屏上窗口底边（状态条）正好被切掉
    y = max(0, min(y, sh - h - 70))
    win.geometry("%dx%d+%d+%d" % (w, h, x, y))
    if minw and minh:
        win.minsize(min(minw, w), min(minh, h))


def _cli_selftest():
    """命令行自检：不弹界面，直接跑一遍最小流程，把全过程写进 output/selftest.log。
    用法： 漫剧工作室.exe --selftest  [镜数 默认1] [帧数 默认22]
    打包后的 exe 用它可以验证 edge-tts / ffmpeg / 路径解析是否都正常。"""
    os.makedirs(AP.OUTDIR, exist_ok=True)
    logpath = os.path.join(AP.OUTDIR, "selftest.log")
    lf = open(logpath, "w", encoding="utf-8", buffering=1)

    class _Tee:
        def write(self, s):
            if s:
                try:
                    lf.write(s)
                except Exception:
                    pass
        def flush(self):
            try:
                lf.flush()
            except Exception:
                pass
        def isatty(self):
            return False

    sys.stdout = sys.stderr = _Tee()
    t0 = time.time()

    def say(msg):
        print(msg)

    say("=== 漫剧工作室 自检 ===")
    say(f"exe/app 目录 : {AP.APP_DIR}")
    say(f"工程根 WORKDIR: {AP.WORKDIR}")
    say(f"ComfyUI main : {AP.COMFY_MAIN}  存在={os.path.exists(AP.COMFY_MAIN)}")
    say(f"python       : {AP.COMFY_PYTHON}  存在={os.path.exists(AP.COMFY_PYTHON)}")
    say(f"后端 URL     : {AP.COMFY_URL}")
    say(f"输出目录     : {AP.OUTDIR}")

    n = 1
    length = 22
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        rest = [a for a in sys.argv[i + 1:i + 3] if a.isdigit()]
        if len(rest) >= 1:
            n = max(1, min(3, int(rest[0])))
        if len(rest) >= 2:
            length = int(rest[1])
    say(f"自检参数     : {n} 镜 / 每镜 {length} 帧 / turbo")

    shots = []
    seeds = [
        ("电影感写实摄影,清晨城市街道,阳光斜射,穿风衣的女生走过,浅景深,高细节",
         "镜头缓慢推近,人物轻轻走过", "清晨的街道，人来人往。"),
        ("电影感写实摄影,地铁站台,列车进站,暖色灯光,男生侧脸,浅景深",
         "镜头轻微右移,列车缓缓进站", "她在等第三站的那班车。"),
        ("电影感写实摄影,长椅与旧书,午后阳光,暖色调,安静氛围,高细节",
         "镜头缓缓降低,书页被风翻动", "有些相遇，比想象中更早。"),
    ]
    for i in range(n):
        k, p, l = seeds[i % len(seeds)]
        shots.append({"key": k, "prompt": p, "line": l})

    script = {"title": "自检片", "voice": "zh-CN-XiaoxiaoNeural", "shots": shots}

    ok = True
    try:
        # 自检也按「本机实际有的模型」选（换电脑后不会因为写死的文件名而失败）
        _te = bool(MM.find("text_encoders", "qwen3vl_4b_int8_convrot.safetensors"))
        _unets = [u["fname"] for u in MM.scan_unets()]
        _q3 = next((u for u in _unets if "Q3_K" in u), _unets[0] if _unets else "")
        say(f"[自检] 文本编码器={'4B' if _te else '32B'}  扩散模型={_q3 or 'Q4_K'}")
        M.run(script, prefix="selftest", length=length, turbo=True, te4b=_te, unet=_q3)
        say("[自检] M.run() 正常返回")
    except SystemExit as e:
        ok = False
        say(f"[自检][FAIL] 流程中 sys.exit({e}) —— 说明某一步判定失败")
    except Exception as e:
        ok = False
        import traceback
        say(f"[自检][异常] {type(e).__name__}: {e}")
        say(traceback.format_exc())
    say(f"[自检] 总耗时 {time.time() - t0:.0f}s")

    # 产物检查
    checks = [
        ("每镜关键帧", [os.path.join(AP.OUTDIR, f"selftest_key_{i+1}_00001_.png") for i in range(n)]),
        ("帧清单", [os.path.join(AP.OUTDIR, "shots_frames.json")]),
        ("配音映射", [os.path.join(AP.OUTDIR, "selftest_voice_map.json")]),
        ("成片", [os.path.join(AP.OUTDIR, "selftest_drama.mp4")]),
    ]
    say("--- 产物检查 ---")
    for name, paths in checks:
        for p in paths:
            if os.path.exists(p):
                say(f"  [OK]   {name}: {os.path.basename(p)} ({os.path.getsize(p)} bytes)")
            else:
                ok = False
                say(f"  [MISS] {name}: {os.path.basename(p)}")

    # 配音文件是否真的非空（edge-tts 在打包环境里最容易挂）
    vmap = os.path.join(AP.OUTDIR, "selftest_voice_map.json")
    if os.path.exists(vmap):
        try:
            import json as _j
            vm = _j.load(open(vmap, encoding="utf-8"))
            for i, s in enumerate(vm.get("shots", []), 1):
                mp3 = s.get("mp3")
                if not mp3 or not os.path.exists(mp3) or os.path.getsize(mp3) < 1000:
                    ok = False
                    say(f"  [MISS] 配音 shot{i}: {mp3}")
                else:
                    say(f"  [OK]   配音 shot{i}: {os.path.basename(mp3)} "
                        f"{os.path.getsize(mp3)} bytes / {s.get('dur'):.2f}s")
        except Exception as e:
            ok = False
            say(f"  [FAIL] 读配音映射出错: {e}")

    # ffmpeg 可用性（打包后 ffmpeg 是内嵌的）
    try:
        import build_manju_mp4 as B
        say(f"  [OK]   ffmpeg: {B.FF} 存在={os.path.exists(B.FF)}")
        if not os.path.exists(B.FF):
            ok = False
    except Exception as e:
        ok = False
        say(f"  [FAIL] 载入 build_manju_mp4 失败: {e}")

    say(f"=== 自检结果: {'全部通过 ✅' if ok else '有问题 ❌'} ===")
    say(f"日志: {logpath}")
    lf.flush()
    lf.close()
    return 0 if ok else 1


def _cli_voice_test():
    """命令行自检：验证打包后「试听」这条链能不能出声（edge-tts → mp3 → wav → 声卡）。
    用法：漫剧工作室.exe --voice-selftest   结果写 output/voice_selftest.log"""
    import traceback
    os.makedirs(AP.OUTDIR, exist_ok=True)
    logp = os.path.join(AP.OUTDIR, "voice_selftest.log")
    lines = []
    try:
        mp3, dur = VT.preview("这是一段旁白试听测试，用来确认打包后能正常出声。",
                              "zh-CN-YunjianNeural", "+30%",
                              out_path=os.path.join(AP.OUTDIR, "_vtest.mp3"))
        lines.append("tts    OK  %s  %.2fs  %d bytes"
                     % (os.path.basename(mp3), dur, os.path.getsize(mp3)))
        wav = os.path.splitext(mp3)[0] + "_play.wav"
        lines.append("play   %s" % ("OK" if VT.play(mp3) else "FAIL"))
        lines.append("wav    %s  %d bytes"
                     % (os.path.exists(wav),
                        os.path.getsize(wav) if os.path.exists(wav) else 0))
        lines.append("ffmpeg %s  存在=%s" % (VT.FF, os.path.exists(VT.FF)))
        VT.stop_play()
    except Exception as e:
        lines.append("FAIL   %s: %s" % (type(e).__name__, e))
        lines.append(traceback.format_exc())
    txt = "\n".join(lines)
    open(logp, "w", encoding="utf-8").write(txt + "\n")
    print(txt)
    return 0


if "--voice-selftest" in sys.argv:
    sys.exit(_cli_voice_test())

if "--selftest" in sys.argv:
    sys.exit(_cli_selftest())

# 风格预设：切换后自动填入「统一风格后缀」，用户也可手动改
PRESETS = {
    "写实电影":   ",电影感写实摄影,自然光,高细节,禁止文字",
    "动漫二次元": ",动漫风格,二次元插画,清新上色,高细节,禁止文字",
    "水墨国风":   ",中国水墨画风格,写意,留白,墨韵,禁止文字",
    "像素复古":   ",像素艺术,8-bit 复古游戏风格,禁止文字",
    "3D 卡通":    ",3D 卡通渲染,皮克斯风格,柔和光影,禁止文字",
}

# ---- 旁白音色：微软 edge-tts 全部中文音色（14 个），可试听后挑 ----
VOICES = {
    "温柔女声·晓晓（推荐）": "zh-CN-XiaoxiaoNeural",
    "活泼女声·晓伊": "zh-CN-XiaoyiNeural",
    "少年男声·云希": "zh-CN-YunxiNeural",
    "沉稳男声·云扬": "zh-CN-YunyangNeural",
    "激情解说·云健": "zh-CN-YunjianNeural",
    "童声·云夏": "zh-CN-YunxiaNeural",
    "东北话女声·晓北": "zh-CN-liaoning-XiaobeiNeural",
    "陕西话女声·晓妮": "zh-CN-shaanxi-XiaoniNeural",
    "粤语女声·晓佳": "zh-HK-HiuGaaiNeural",
    "粤语女声·晓曼": "zh-HK-HiuMaanNeural",
    "粤语男声·云龙": "zh-HK-WanLungNeural",
    "国语女声·晓臻（台湾腔）": "zh-TW-HsiaoChenNeural",
    "国语女声·晓雨（台湾腔）": "zh-TW-HsiaoYuNeural",
    "国语男声·云哲（台湾腔）": "zh-TW-YunJheNeural",
    # —— 武侠 / 古装旁白（A 方案下会同时切换模型原生发声的音色描述）——
    "武侠旁白·苍劲（推荐）": "zh-CN-YunjianNeural",
    "武侠旁白·沉冷": "zh-CN-YunyangNeural",
}

# A 方案（模型自己念）下，旁白音色不是 edge-tts 决定的，而是这段文字描述决定的。
# 选上面这几个「武侠旁白」时，把描述一起传给引擎，免得旁白还是《蹄花店》的成都老太太腔。
NARRATOR_DESC = {
    "武侠旁白·苍劲（推荐）":
        "四十岁上下的中年男子嗓音，低沉苍劲，语速缓慢克制，像说书人讲一段江湖旧事，"
        "字字压得住，尾音微收，不带笑意",
    "武侠旁白·沉冷":
        "三十多岁的男声画外音，声线冷硬平直，语速偏慢，克制不煽情，像在陈述一桩旧案",
}
# 语速：edge-tts 用百分比，+15% 就是 1.15 倍
RATES = {
    "慢 0.85x": "-15%",
    "正常 1.0x": "+0%",
    "稍快 1.15x": "+15%",
    "快 1.3x": "+30%",
}
RATE_DEFAULT = "正常 1.0x"

# 默认示例（香蕉故事，可随意改）
EXAMPLE = [
    ("电影感写实摄影,远古热带雨林清晨,金色阳光穿透茂密树冠,薄雾在林间流动,"
     "一棵散发淡金色微光的巨大神树矗立中央,一只毛茸茸的小猴子停在藤蔓上仰望神树,"
     "宽大绿叶,丁达尔光束,暖金绿色调,柔和自然光,高细节,8k,电影级构图",
     "远古雨林，小猴发现一棵发金光的神树。"),
    ("电影感写实微距,特写,一根弯弯的、表皮光滑带蜡质光泽的明黄色香蕉,"
     "顶端连着褐色果柄与花萼,明显挂在神树的枝条上、倚靠在宽大绿叶间,"
     "表面流转细密金色光纹,周围花粉般光点闪烁,背景虚化的雨林绿意,暖金绿色调",
     "树上结着弯弯的、流转金光的奇异果实。"),
    ("电影感写实,一位温和的自然精灵,清晰五官,有五根手指的人手,双脚站立接地,"
     "半透明身躯环绕翠绿与金色光雾,在神树前轻轻伸手赐福,柔和光,暖金绿色调,写实身体结构",
     "温柔的精灵伸手，赐它生命与甜美。"),
    ("电影感写实微距,一根成熟的弯月形明黄香蕉,自然垂挂在一串真实香蕉果串上,"
     "果串连着翠绿芭蕉植株的假茎与宽大叶片,表皮泛起温暖光泽,"
     "一只写实的小蜜蜂停在香蕉上,背景雨林在暖光中朦胧,暖金绿色调",
     "果实成熟舒展，化作我们熟悉的香蕉。"),
    ("电影感写实全景,小猴子掰下那根香蕉转身分给林中伙伴,镜头缓缓拉远升高,"
     "俯瞰整片雨林,无数发着金光的香蕉树在绿海中铺展,壮丽日出霞光,宏大而温馨",
     "小猴分给伙伴，甜美传遍世界。"),
]

log_q = queue.Queue()
backend_proc = None
backend_starting = False
gen_thread = None


def _set_starting(v):
    global backend_starting
    backend_starting = v


# ============================ 后端控制 ============================
def check_online(timeout=3.0):
    """探后端是否在线。

    timeout 默认 3 秒：ComfyUI 在推理时是单线程应答，/system_stats 也可能排队，
    给太短会把它误判成「没启动」。但这个函数是同步的 —— 在主线程里调用就会
    实打实冻住界面，所以调用点要么放进后台线程，要么显式传短超时。
    """
    try:
        import urllib.request
        urllib.request.urlopen(COMFY_URL + "/system_stats", timeout=timeout).read()
        return True
    except Exception:
        return False


def backend_ready():
    """给主线程用的「后端在不在」判断，最多等 1 秒。

    先看后台每 8 秒探测的缓存：在线就直接放行（零延迟，这是最常见的情况）。
    只有缓存说没在线时才真的去探一次 —— 这时多半是后端真没起，1 秒足够判死，
    不用让界面白等满 3 秒（以前每点一次「开始生成」都要卡这么一下）。
    """
    if _ONLINE[0]:
        return True
    return check_online(1.0)


# ---------------- 后端没起时"排着队"的活 ----------------
# 新剧导入会连着触发两件重活：生成角色定妆照、生成场景照。这两件都要后端在线。
# 以前后端没起就直接弹警告 + 放弃，让用户"事后自己点一下补"——
# 结果新手导入完剧本直接点生成，出片全是没锁脸的脸（还以为程序坏了）。
# 现在改成排队：活先记下来，后端一上线自动接着干，用户什么都不用补点。
_BACKEND_QUEUE = []          # [(标签, 无参函数)]


def _after_backend(fn, label=""):
    """后端在线就立刻执行；没在线就先排队，等它起来自动跑。"""
    label = label or "后台任务"
    try:
        if backend_ready():
            fn()
            return True
    except Exception:
        pass
    _BACKEND_QUEUE.append((label, fn))
    try:
        log_q.put("[排队] %s：后端还没就绪，已排队 —— 后端一上线自动继续，"
                  "不用再手动点一次。\n" % label)
    except Exception:
        pass
    return False


def _flush_backend_queue():
    """后端上线时调用：把排队的活依次跑掉。"""
    if not _BACKEND_QUEUE:
        return
    try:
        if not backend_ready():
            return
    except Exception:
        return
    items, _BACKEND_QUEUE[:] = list(_BACKEND_QUEUE), []
    for label, fn in items:
        try:
            log_q.put("[排队] 后端已就绪 → 继续：%s\n" % label)
            fn()
        except Exception as e:
            try:
                log_q.put("[排队] %s 没跑起来：%s\n" % (label, e))
            except Exception:
                pass


_status_kind = "gray"


def set_status(text, color):
    """color 传 green/red/gray/orange，实际色值由当前主题决定。

    顺带把工具栏那颗「⚡ 启动后端」按钮切到对应状态 —— 以前只有状态条变色，
    按钮永远写着「启动后端」，用户看着像没启动（反馈"启动后端没有变成已启动"）。
    """
    global _status_kind
    _status_kind = color
    status_lbl.config(text=text, foreground=TH.STATUS.get(color, TH.TEXT2))
    try:
        if color == "green":
            start_btn.config(state="disabled", text="✓ 后端已启动")
        elif color == "red":
            start_btn.config(state="normal", text="⚡ 启动后端")
    except Exception:
        pass


def _release_vram():
    """出片结束后把显存放掉（模型仍留在内存缓存，下次出片快）。"""
    try:
        import urllib.request
        req = urllib.request.Request(COMFY_URL + "/free",
                                     data=b'{"unload_models":true,"free_memory":true}',
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


def stop_backend(silent=False):
    """关闭后端并释放内存：ComfyUI 会把整个模型（十几 GB）缓存在内存里待命，
    不出片时这些内存纯属浪费。关掉进程 = 立刻收回，下次点「启动后端」再加载。"""
    global backend_proc
    freed = ""
    try:
        import urllib.request
        req = urllib.request.Request(COMFY_URL + "/free",
                                     data=b'{"unload_models":true,"free_memory":true}',
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass
    killed = False
    if backend_proc is not None and backend_proc.poll() is None:
        try:
            backend_proc.terminate()
            try:
                backend_proc.wait(timeout=5)
            except Exception:
                backend_proc.kill()
            killed = True
        except Exception:
            pass
    # 兜底：进程不是本程序起的（或句柄丢了），按端口找
    if not killed and check_online():
        try:
            import subprocess as _sp
            out = _sp.run(["netstat", "-ano"], capture_output=True, text=True,
                          timeout=8).stdout
            for line in out.splitlines():
                if f":{PORT}" in line and "LISTENING" in line:
                    pid = line.split()[-1]
                    _sp.run(["taskkill", "/F", "/PID", pid],
                            capture_output=True, timeout=8)
                    killed = True
                    break
        except Exception:
            pass
    backend_proc = None
    set_status("○ 后端已关闭（内存已释放）", "gray")
    start_btn.config(state="normal", text="⚡ 启动后端")
    log_q.put("\n[后端] 已关闭，模型缓存内存已释放。要继续出片就点「⚡ 启动后端」。\n")
    if not silent:
        messagebox.showinfo("已释放内存",
            "后端已关闭，被缓存占用的内存（约 20-25GB）已还给系统。\n\n"
            "下次要出片，点右上角「⚡ 启动后端」，等状态变绿即可\n"
            "（首次加载模型约 1-2 分钟，之后每镜都会快）。")


def start_backend():
    global backend_proc
    # 已有后端进程在跑：直接复用，不再起新的（避免重复黑框）
    if backend_proc is not None and backend_proc.poll() is None:
        set_status("● 后端在线", "green")
        return
    if check_online():
        set_status("● 后端在线", "green")
        return
    if backend_starting:   # 正在启动中，防止连点产生多个后端
        return
    args = [COMFY_PYTHON, "-s", COMFY_MAIN, "--port", str(PORT),
            "--input-directory", INPUTDIR,
            "--output-directory", OUTDIR,
            "--extra-model-paths-config", EXTRA,
            "--lowvram", "--reserve-vram", "1", "--preview-method", "none",
            "--disable-pinned-memory", "--cache-ram", "2", "16"]
    if not os.path.exists(COMFY_MAIN):
        set_status("找不到 ComfyUI: " + COMFY_MAIN, "red")
        messagebox.showerror("路径错误", "未找到 ComfyUI 启动文件：\n" + COMFY_MAIN
                             + "\n\n请在右上角「⚙ 设置」里指定你机器上的 ComfyUI 主程序(main.py)。")
        return
    if not os.path.exists(EXTRA):
        set_status("缺少 extra_model_paths.yaml", "red")
        messagebox.showerror("路径错误", "未在工作目录找到 extra_model_paths.yaml：\n" + EXTRA
                             + "\n\n请确认本程序放在含该文件的工程目录下，或用「⚙ 设置」指定工程目录。")
        return
    import tempfile
    err_log = tempfile.NamedTemporaryFile(mode="w", suffix=".log",
                                         delete=False, encoding="utf-8")
    err_log.close()
    # 关键：用 CREATE_NO_WINDOW 隐藏后端控制台黑框（日志仍抓到 err_log 并在界面显示）
    NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        _set_starting(True)
        start_btn.config(state="disabled", text="启动中…")
        backend_proc = subprocess.Popen(
            args,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | NO_WIN,
            stdout=subprocess.DEVNULL, stderr=open(err_log.name, "w"))
    except Exception as e:
        _set_starting(False)
        start_btn.config(state="normal", text="⚡ 启动后端")
        set_status("启动失败: " + str(e), "red")
        return
    set_status("○ 正在启动后端…(约1-2分钟)", "orange")
    log_q.put("正在后台启动 ComfyUI，请稍候…\n")
    threading.Thread(target=poll_backend, args=(err_log.name,), daemon=True).start()


def poll_backend(err_log):
    for _ in range(80):
        time.sleep(3)
        if check_online():
            log_q.put("后端已就绪 ({})\n".format(PORT))
            root.after(0, lambda: set_status("● 后端在线", "green"))
            root.after(0, lambda: start_btn.config(state="normal", text="⚡ 启动后端"))
            root.after(0, lambda: _set_starting(False))
            root.after(0, _flush_backend_queue)
            return
    tail = ""
    try:
        with open(err_log, encoding="utf-8", errors="ignore") as f:
            tail = "".join(f.readlines()[-25:])
    except Exception:
        pass
    msg = "后端启动超时（{} 秒）。可能原因：ComfyUI 路径不对、缺少依赖、端口被占。\n".format(80 * 3)
    if tail.strip():
        msg += "\nComfyUI 最后输出：\n" + tail
    root.after(0, lambda: set_status("后端启动超时，请手动启动", "red"))
    root.after(0, lambda: log_q.put(msg + "\n"))
    root.after(0, lambda: start_btn.config(state="normal", text="⚡ 启动后端"))
    root.after(0, lambda: _set_starting(False))


# ============================ 主题配色 ============================
_theme_win = None


def _draw_theme_card(cv, key, width=182, height=110):
    """在 Canvas 上画一张「迷你界面」小样 —— 点之前就看得到实际观感。"""
    p = TH.PALETTES[key]
    cur = (key == TH.current_key())
    cv.delete("all")
    try:
        cv.configure(background=TH.BG, highlightthickness=0)
    except Exception:
        pass
    # 卡片底（未选中时用当前主题的描边色，融入背景）
    cv.create_rectangle(1, 1, width - 1, height - 1, fill=p["BG"],
                        outline=p["ACCENT"] if cur else TH.LINE,
                        width=2 if cur else 1)
    # 顶栏
    cv.create_rectangle(2, 2, width - 2, 25, fill=p["PANEL"], outline="")
    cv.create_oval(10, 9, 18, 17, fill=p["ACCENT"], outline="")
    cv.create_text(24, 13, text="漫剧工作室", anchor="w", fill=p["TEXT"],
                   font=(TH.FONT[0], 8, "bold"))
    # 输入框
    cv.create_rectangle(9, 34, width - 9, 50, fill=p["FIELD"], outline=p["LINE"])
    cv.create_text(16, 42, text="输入框 · 正文", anchor="w",
                   fill=p["TEXT2"], font=(TH.FONT[0], 7))
    # 主动作按钮
    TH._round_rect(cv, 9, 57, 78, 75, 5.5, fill=p["ACCENT"], outline="")
    cv.create_text(43, 66, text="开始生成", fill=p["ON_ACC"],
                   font=(TH.FONT[0], 7, "bold"))
    # 旁色点
    for i, c in enumerate((p["TEXT2"], p["TEXT3"], p["LINE"])):
        cv.create_rectangle(86 + i * 13, 61, 94 + i * 13, 71, fill=c, outline="")
    if cur:
        cv.create_text(width - 11, 13, text="✓", anchor="e", fill=p["ACCENT"],
                       font=(TH.FONT[0], 9, "bold"))
    cv.create_text(9, 86, text=p["name"], anchor="w", fill=p["TEXT"],
                   font=(TH.FONT[0], 9, "bold"))
    cv.create_text(9, 100, text=p["desc"], anchor="w", fill=p["TEXT3"],
                   font=(TH.FONT[0], 7))


def open_theme_picker():
    """14 套配色，点一下立刻换（自动记住，下次启动还是这套）。"""
    global _theme_win
    if _theme_win is not None and _theme_win.winfo_exists():
        _theme_win.lift()
        _theme_win.focus_force()
        return
    w = tk.Toplevel(root)
    TH.paint_toplevel(w)
    w.title("主题配色")
    center_win(w, 812, 600, 700, 520)
    w.transient(root)
    w.resizable(False, False)
    _theme_win = w

    head = ttk.Frame(w)
    head.pack(fill="x", padx=16, pady=(14, 2))
    ttk.Label(head, text="主题配色", style="H2.TLabel").pack(side="left")
    ttk.Label(head, text="  点卡片＝预览，点「用这套」才记住（默认跟随系统）", style="Hint.TLabel").pack(
        side="left", pady=(4, 0))

    grid = ttk.Frame(w)
    grid.pack(fill="both", expand=True, padx=12, pady=6)

    cards = {}

    state = {"cur": TH.current_key()}      # 配置里可能是 "auto"，当前实际套用的那套才是卡片要高亮的

    def paint(key):
        """只换外观，不写配置——避免手一抖点到卡片就被永久记住。"""
        TH.apply(root, key)
        TH.restyle(root)
        try:
            status_lbl.config(background=TH.PANEL)
            set_status(status_lbl.cget("text"), _status_kind)
        except Exception:
            pass
        state["cur"] = key
        for k, cv in cards.items():
            _draw_theme_card(cv, k)
        w.configure(background=TH.BG)
        # 剧情预览是手绘 Canvas：底色跟着换，网格线要重画才对得上
        try:
            _story_host.configure(background=TH.BG_DEEP)
            _story_cv.configure(background=TH.BG_DEEP)
            _story_refresh()
        except Exception:
            pass

    def apply_cur():
        key = state["cur"]
        try:
            c = dict(AP.CFG)
            c["theme"] = key
            AP.save_config(c)
            AP.CFG["theme"] = key
        except Exception as e:
            messagebox.showerror("保存失败", str(e)); return
        hint_lbl.config(text=f"已记住：{TH.PALETTES[key]['name']}（下次启动还是这套）")

    def use_auto():
        """跟随系统：现在系统深就深、浅就浅，并且记住这个选择（不是记住某一套颜色）。"""
        key = _system_theme_key()
        paint(key)
        try:
            c = dict(AP.CFG)
            c["theme"] = "auto"
            AP.save_config(c)
            AP.CFG["theme"] = "auto"
        except Exception as e:
            messagebox.showerror("保存失败", str(e)); return
        hint_lbl.config(text=f"已记住：跟随系统（当前 {TH.PALETTES[key]['name']}，系统换深浅会自动跟）")

    def reset_default():
        use_auto()

    for i, (key, name, desc, light) in enumerate(TH.palette_list()):
        cv = tk.Canvas(grid, width=182, height=110, highlightthickness=0, bd=0,
                       cursor="hand2")
        cv.grid(row=i // 4, column=i % 4, padx=6, pady=5)
        cards[key] = cv
        _draw_theme_card(cv, key)
        cv.bind("<Button-1>", lambda e, k=key: paint(k))

    foot = ttk.Frame(w)
    foot.pack(fill="x", padx=16, pady=(2, 12))
    ttk.Label(foot, text="默认「跟随系统」；深色 8 套 / 浅色 4 套，浅色白天·投屏更清楚",
              style="Hint.TLabel").pack(side="left")
    TH.RoundButton(foot, text="关闭", command=w.destroy, height=32).pack(side="right")
    TH.RoundButton(foot, text="🖥 跟随系统", command=use_auto,
                   kind="quiet", height=32).pack(side="right", padx=(0, 8))
    TH.RoundButton(foot, text="↺ 恢复默认", command=reset_default,
                   kind="quiet", height=32).pack(side="right", padx=(0, 8))
    TH.RoundButton(foot, text="✓ 用这套", command=apply_cur,
                   kind="accent", height=32).pack(side="right", padx=(0, 8))
    hint_lbl = ttk.Label(w, text="", style="Hint.TLabel")
    hint_lbl.pack(fill="x", padx=16, pady=(0, 0))


def open_settings():
    w = tk.Toplevel(root)
    TH.paint_toplevel(w)
    w.title("设置（首次使用请先配置）")
    center_win(w, 720, 418, 640, 380)
    w.resizable(True, True)
    w.transient(root)
    try:
        w.grab_set()
    except Exception:
        pass
    ttk.Label(w, text="本机已自动填好默认值，一般无需修改；仅在换电脑 / 路径变了才改这里。",
              foreground=TH.TEXT2, font=TH.FONT_S, wraplength=680).grid(
        row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 8))
    cur = dict(AP.CFG)
    mk = tk.StringVar(value=cur.get("comfy_main", ""))
    pk = tk.StringVar(value=cur.get("comfy_python", ""))
    pt = tk.StringVar(value=str(cur.get("port", 8188)))
    wd = tk.StringVar(value=cur.get("workdir", ""))
    md = tk.StringVar(value=cur.get("models_dir", ""))

    def pick_file(var, title):
        p = filedialog.askopenfilename(title=title,
                                       filetypes=[("Python/程序", "*.py;*.exe"), ("全部", "*.*")])
        if p:
            var.set(p)

    def pick_dir(var):
        d = filedialog.askdirectory(title="选择工程目录")
        if d:
            var.set(d)

    ttk.Label(w, text="ComfyUI 主程序(main.py)：").grid(row=1, column=0, sticky="w", padx=8, pady=5)
    ttk.Entry(w, textvariable=mk, width=46).grid(row=1, column=1, padx=4)
    TH.RoundButton(w, text="浏览", command=lambda: pick_file(mk, "选择 ComfyUI 的 main.py"), height=32).grid(row=1, column=2, padx=4)
    ttk.Label(w, text="Python 解释器(python.exe)：").grid(row=2, column=0, sticky="w", padx=8, pady=5)
    ttk.Entry(w, textvariable=pk, width=46).grid(row=2, column=1, padx=4)
    TH.RoundButton(w, text="浏览", command=lambda: pick_file(pk, "选择 python.exe"), height=32).grid(row=2, column=2, padx=4)
    ttk.Label(w, text="端口：").grid(row=3, column=0, sticky="w", padx=8, pady=5)
    ttk.Entry(w, textvariable=pt, width=10).grid(row=3, column=1, sticky="w", padx=4)
    ttk.Label(w, text="工程目录(可选)：").grid(row=4, column=0, sticky="w", padx=8, pady=5)
    ttk.Entry(w, textvariable=wd, width=46).grid(row=4, column=1, padx=4)
    TH.RoundButton(w, text="浏览", command=lambda: pick_dir(wd), height=32).grid(row=4, column=2, padx=4)
    ttk.Label(w, text="模型目录(可选)：").grid(row=5, column=0, sticky="w", padx=8, pady=5)
    ttk.Entry(w, textvariable=md, width=46).grid(row=5, column=1, padx=4)
    TH.RoundButton(w, text="浏览", command=lambda: pick_dir(md), height=32).grid(row=5, column=2, padx=4)
    ttk.Label(w, text="模型不在 exe 里（约 20G），已放在别的盘就填这里；留空 = 工程目录下的 models\\。\n"
                     "改完会自动改写 extra_model_paths.yaml，重启后端生效。也可用主界面「📦 模型」一键检查/下载。",
              foreground=TH.TEXT3, font=TH.FONT_XS, wraplength=680,
              justify="left").grid(row=6, column=0, columnspan=3, sticky="w", padx=8)

    w.columnconfigure(1, weight=1)

    def save_cfg():
        try:
            port = int(pt.get())
        except Exception:
            port = 8188
        newcfg = {"comfy_main": mk.get().strip(), "comfy_python": pk.get().strip(),
                  "port": port, "workdir": wd.get().strip(),
                  "models_dir": md.get().strip(),
                  "keep_backend": bool(_keep_var.get()),
                  "free_vram_on_exit": bool(_freev_var.get())}
        AP.save_config(newcfg)
        if md.get().strip():
            _apply_models_dir(md.get().strip())
        messagebox.showinfo("已保存", "设置已写入 config.json。\n请重启本程序使配置生效。")
        w.destroy()

    _keep_var = tk.BooleanVar(value=bool(cur.get("keep_backend", True)))
    _freev_var = tk.BooleanVar(value=bool(cur.get("free_vram_on_exit", False)))
    ttk.Checkbutton(w, text="退出时保留后端（下次启动秒连，不用等 ComfyUI 重新加载）",
                    variable=_keep_var).grid(row=7, column=0, columnspan=3,
                                             sticky="w", padx=8, pady=(6, 0))
    ttk.Checkbutton(w, text="退出时卸载模型释放显存（约省 4G，但下次第一次出片要重载 1-2 分钟）",
                    variable=_freev_var).grid(row=8, column=0, columnspan=3,
                                              sticky="w", padx=8)
    TH.RoundButton(w, text="保存并提示重启", command=save_cfg, height=32).grid(row=9, column=1, sticky="w", padx=4, pady=12)


# ============================ 使用说明 ============================
# 帮助正文按「章节 → 块」组织：左边是目录，右边分块渲染。
# 以前是一整坨 100 多行的纯文本（还带 ** 这种纯文本里根本不生效的伪粗体），
# 全糊在一起没法看 —— 用户原话「看着好乱」。
HELP_SECTIONS = [
    ("三步出片", [
        ("li", "① 导入剧本：左下「⇩ 导入剧本」→ 分镜表整段粘进去 → 点「解析并替换分镜」。"),
        ("li", "② 等后端变绿：右上角出现「● 后端在线」才能生成（首次约 1–2 分钟，程序会自己启动）。"),
        ("li", "③ 点「▶ 开始生成」：关键帧 → 视频帧 → 配音 → 拼接，全自动跑完。"),
        ("tip", "10 镜以内最稳；「加速 turbo」默认开着，比不开快 4–5 倍。"),
        ("p", "成片位置：<工程目录>/output/<剧名>_drama.mp4，直接拖进剪映 / 抖音 / 小红书。"),
    ]),
    ("三个界面怎么配合", [
        ("p", "主界面、ComfyUI、无界工作台不是三个软件，而是同一个工具的三层。"
              "顶部那条说明讲的就是这件事，这里说具体怎么用。"),
        ("b", "主界面（你日常用的这一层）"),
        ("li", "写剧本、排分镜、设参数、点「▶ 开始生成」—— 出片只用这一层。"),
        ("li", "它会自己去调 ComfyUI，后端怎么跑你不用管。"),
        ("b", "ComfyUI（真正干活的引擎，:8188）"),
        ("li", "跑模型、出图、出视频都在这里发生，平时在后台。"),
        ("li", "点视口上方的「🌐 ComfyUI」标签就能看到它的节点界面：查队列、看卡在哪一步、读报错。"),
        ("li", "一切正常时不用碰它。"),
        ("b", "无界工作台（画布，:3000）"),
        ("li", "可以理解成「能手动摆节点的 ComfyUI」：拖节点、改参数、单独跑一张图试效果。"),
        ("li", "点视口上方的「🗺 无限画布」标签进入；调好的工作流照样交给同一台 ComfyUI 执行。"),
        ("li", "适合这些时候用：想微调某个工作流、想先试试某个模型的效果、想看看节点到底怎么连的。"),
        ("tip", "一句话记法：主界面 = 指挥，ComfyUI = 干活，无界工作台 = 手动挡。"
                "平时只碰方向盘（主界面），要自己改车才打开引擎盖（另外两个）。"),
    ]),
    ("首次使用 · 后端", [
        ("p", "本工具 = 界面 + 渲染后端（ComfyUI + H3 / Z-Image 模型）。界面只负责指挥，"
              "真正画图渲染要靠本机装好的 ComfyUI 和一张 NVIDIA 显卡。"),
        ("b", "自己的电脑（环境已经装好）"),
        ("li", "什么都不用点：程序启动后自动连后端，右上角变绿「● 后端在线」即可。"),
        ("li", "万一没自动起来（路径不对等），点右上角「⚡ 启动后端」补一下。"),
        ("b", "换到别人的电脑 / 新电脑"),
        ("li", "点右上角「⚙ 设置」，把 ComfyUI 主程序（main.py）、Python（python.exe）、端口指到那台机器的实际路径。"),
        ("li", "保存后重启本程序。找不到模型 / 工作流时，也在设置里指定「工程目录」。"),
    ]),
    ("模型从哪来", [
        ("warn", "这个 exe 里不含模型。整套约 20GB，而 exe 只有 70MB —— 模型必须另外导入。"),
        ("p", "点右上角「📦 模型」，三条路任选一条："),
        ("b", "A · 已经下载好了（在下载文件夹 / 移动硬盘里）"),
        ("li", "点「📥 导入本地模型」，多选那些 .gguf / .safetensors 文件。"),
        ("li", "程序按文件名自动分到 unet / text_encoders / vae / loras / clip_projections，原文件不动。"),
        ("li", "复制完点「🔄 重新检测」，看到「✅ 就绪」就能出片。"),
        ("b", "B · 手动放（一个按钮都不用点）"),
        ("li", "点「📂 打开模型文件夹」，按类型拖进对应子目录："),
        ("li2", "扩散模型 *.gguf → models/unet/"),
        ("li2", "文本编码器 qwen3vl_* → models/text_encoders/"),
        ("li2", "投影矩阵 *ClipProj* → models/clip_projections/"),
        ("li2", "VAE *vae* → models/vae/"),
        ("li2", "加速 LoRA *turbo*step* → models/loras/"),
        ("li", "放完点「🔄 重新检测」。"),
        ("b", "C · 一个都没有"),
        ("li", "点「⬇ 下载全部缺失」自动下（ModelScope 国内镜像，支持断点续传，中断后重开窗口再点会接着下）。"),
        ("li", "也可以「📄 导出下载清单」把链接发给别人去下。"),
        ("b", "其他情况"),
        ("li", "模型在别的盘：点「📁 换个模型目录」指过去，程序自动改写 extra_model_paths.yaml，不用重下、不用改名。"),
        ("li", "官方原版 turbo LoRA 会显示「🔧 需转换」，点「🔧 转换所选 LoRA」自动剥前缀。"),
        ("li", "缺自定义节点：切到「运行环境 / 节点」页检查；本项目专用节点点「📦 一键安装自带节点」，"
              "第三方节点按提示 git clone 到 custom_nodes 后重启后端。"),
    ]),
    ("剧本从哪来", [
        ("li", "让 AI 写（最省事）：点「💡 不会写剧情？」，复制提示词粘给豆包 / DeepSeek / Kimi / ChatGPT，"
              "让它按 5 列表格输出，再把整段结果粘进「⇩ 导入剧本」。"),
        ("li", "自己写：点「＋ 添加一镜」，一镜一镜填；10 镜以内最好。"),
        ("li", "已有表格：Excel / 腾讯文档 / 飞书做好 5 列，整段复制导入即可。"),
        ("li", "不想要预填的示例：点「🗑 清空分镜」从空白开始；想要回来点「🔄 载入示例」。"),
    ]),
    ("填写剧本", [
        ("b", "批量（推荐）"),
        ("li", "点「⇩ 导入剧本」→ 粘分镜表 → 点「解析并替换分镜」，全部镜头自动生成。"),
        ("li", "列顺序：镜号 / 时长 / 画面描述 / 台词·旁白 / 音效·备注（后面几列可选）。"),
        ("li", "台词跨行会自动续接，支持从 Excel / 网页表格直接复制。"),
        ("b", "手写"),
        ("li", "点「＋ 添加一镜」，每镜 5 栏："),
        ("li2", "① 画面描述 —— 画什么、什么构图氛围，越具体越好"),
        ("li2", "② 台词 / 旁白 —— 这一镜要念的文字（必填）"),
        ("li2", "③ 时长（秒）—— 现实时长，如 8"),
        ("li2", "④ 音效 / 备注 —— 如“地铁轰鸣、进站提示音”"),
        ("li2", "⑤ 运镜 —— 按①自动判一句，可改"),
        ("p", "顶部还能设：剧名、音色、每镜帧数、是否开加速（turbo）。"),
    ]),
    ("会自动识别的三样", [
        ("b", "⑤ 运镜"),
        ("li", "按景别 + 动作判：特写推近、全景拉远、打斗手持、局部特写定住、双人对白轻微横移。"),
        ("li", "填进下拉框，旁边灰字写「自动识别：缓慢推近」。改过的那一镜以后不会被覆盖。"),
        ("b", "⑦ 景别"),
        ("li", "写「特写 / 近景 / 中景 / 全景 / 远景」直接认；没写就从内容反推"
              "（拍指尖 = 局部特写、拍眼神 = 特写、拍打斗 = 全景）。"),
        ("li", "判错了在下拉框里改一下即可。分镜栏「⟳ 重新识别」可以全部重认一遍。"),
        ("b", "⑧ 场景"),
        ("li", "同一个地点的多镜共用一张场景照，背景跨镜才一致"
              "（不然同一间客栈每镜长得都不一样，观众看着像在换地方）。"),
        ("li", "程序从①里认地点；后几镜没写地点的会延续上一镜（灰字写「延续上一镜：xxx」）。"),
        ("li", "导入新剧本后自动：认场景 → 写场景文案 → 生成场景照 → 绑到每镜。"),
        ("li", "场景照在「🏞 场景照」里管（生成 / 加图 / 改文案 / 解绑）。"
              "后端没起时照样建好，起来后点「🎬 全部补场景照」。"),
    ]),
    ("旁白与配音", [
        ("li", "音色：顶部「旁白音色」共 14 个中文音色 —— 男女老少、东北话、陕西话、粤语、台湾腔。"),
        ("li", "试听：点「🔊 试听」→ 选音色 + 语速，声音直接从电脑放出来（不弹播放器）。"
              "试听文案可改，默认念第一镜台词。满意点「✔ 用这个音色」写回。"),
        ("li", "语速：0.85x / 1.0x / 1.15x / 1.3x，短视频一般 1.15x 更利落。"),
        ("li", "配音要联网（微软语音）。生成时会按每镜的语音长度自动对齐画面。"),
    ]),
    ("生成与导出", [
        ("li", "点「▶ 开始生成」，右下日志实时滚动。镜数越多、帧数越高越久。"),
        ("warn", "生成过程中别关窗口，也别重复点「开始生成」。"),
        ("li", "成片在：<工程目录>/output/<剧名>_drama.mp4"),
    ]),
    ("常见问题", [
        ("b", "状态一直红 / 启动失败"),
        ("li", "点「⚙ 设置」确认 ComfyUI 路径正确，再看日志里「ComfyUI 最后输出」排查：路径错 / 缺依赖 / 端口被占。"),
        ("b", "出图很慢"),
        ("li", "确认「加速 turbo」开着；把「画质」降一档，时间大概能砍一半。"),
        ("b", "换了电脑要重新配什么"),
        ("li", "设置里的三个路径（main.py / python.exe / 工程目录）+ 模型目录。"),
        ("b", "界面里网页不显示"),
        ("li", "关掉程序重开一次；WebView2 内核异常时重新加载即可。"),
    ]),
]


def open_help():
    """使用说明：左边目录、右边正文，按章节分块渲染。"""
    w = tk.Toplevel(root)
    TH.paint_toplevel(w)
    w.title("使用说明")
    center_win(w, 880, 620, 760, 500)
    w.transient(root)
    try:
        w.grab_set()
    except Exception:
        pass

    nav = tk.Frame(w, background=TH.PANEL, width=172)
    nav.pack(side="left", fill="y")
    nav.pack_propagate(False)
    tk.Label(nav, text="目录", background=TH.PANEL, foreground=TH.TEXT3,
             font=(TH.FONT[0], 9), anchor="w").pack(fill="x", padx=14,
                                                    pady=(14, 8))
    tk.Frame(nav, background=TH.LINE, height=1).pack(fill="x", padx=14)

    body = tk.Frame(w, background=TH.BG)
    body.pack(side="left", fill="both", expand=True)

    txt = scrolledtext.ScrolledText(body, wrap="word", bd=0,
                                    font=(TH.FONT[0], 10), padx=18, pady=14)
    txt.pack(fill="both", expand=True)

    txt.tag_configure("h1", font=(TH.FONT[0], 12, "bold"),
                      foreground=TH.ACCENT, spacing1=20, spacing3=8)
    txt.tag_configure("b", font=(TH.FONT[0], 10, "bold"),
                      foreground=TH.TEXT, spacing1=10, spacing3=3)
    txt.tag_configure("p", font=(TH.FONT[0], 10), foreground=TH.TEXT2,
                      lmargin1=2, lmargin2=2, spacing3=7)
    txt.tag_configure("li", font=(TH.FONT[0], 10), foreground=TH.TEXT2,
                      lmargin1=12, lmargin2=26, spacing3=5)
    txt.tag_configure("li2", font=(TH.FONT[0], 9), foreground=TH.TEXT3,
                      lmargin1=28, lmargin2=44, spacing3=3)
    txt.tag_configure("tip", font=(TH.FONT[0], 9), foreground=TH.TEXT2,
                      background=TH.SEL_BG, lmargin1=8, lmargin2=8,
                      spacing1=8, spacing3=8)
    txt.tag_configure("warn", font=(TH.FONT[0], 9), foreground=TH.WARN,
                      background=TH.PANEL, lmargin1=8, lmargin2=8,
                      spacing1=8, spacing3=8)

    for i, (title, blocks) in enumerate(HELP_SECTIONS):
        mk = "sec%d" % i
        txt.mark_set(mk, "end-1c")
        txt.mark_gravity(mk, "left")
        txt.insert("end", "%d. %s\n" % (i + 1, title), "h1")
        for kind, text in blocks:
            if kind == "b":
                txt.insert("end", "▍ " + text + "\n", "b")
            elif kind == "li":
                txt.insert("end", "· " + text + "\n", "li")
            elif kind == "li2":
                txt.insert("end", "– " + text + "\n", "li2")
            elif kind == "tip":
                txt.insert("end", "  💡 " + text + "\n", "tip")
            elif kind == "warn":
                txt.insert("end", "  ⚠ " + text + "\n", "warn")
            else:
                txt.insert("end", text + "\n", "p")
    txt.configure(state="disabled")

    def go(i):
        try:
            txt.see("sec%d" % i)
            txt.yview("sec%d" % i)
        except Exception:
            pass

    for i, (title, _b) in enumerate(HELP_SECTIONS):
        lb = tk.Label(nav, text="%d. %s" % (i + 1, title), background=TH.PANEL,
                      foreground=TH.TEXT2, font=(TH.FONT[0], 9), anchor="w",
                      padx=16, pady=6, cursor="hand2")
        lb.pack(fill="x")
        lb.bind("<Button-1>", lambda e, k=i: go(k))
        lb.bind("<Enter>", lambda e, x=lb: x.configure(background=TH.SEL_BG,
                                                       foreground=TH.TEXT))
        lb.bind("<Leave>", lambda e, x=lb: x.configure(background=TH.PANEL,
                                                       foreground=TH.TEXT2))

    TH.RoundButton(nav, text="关闭", command=w.destroy, height=30).pack(
        side="bottom", pady=14)


# ============================ 旁白试听 ============================
_voice_win = None


def open_voice_preview(preset_label=None):
    """试听旁白：选音色 + 语速 + 文案 → 生成后直接从电脑放声（不弹播放器）。
    满意就点「✔ 用这个音色」写回主界面。"""
    global _voice_win
    if _voice_win is not None:
        try:
            if _voice_win.winfo_exists():
                _voice_win.lift()
                _voice_win.focus_force()
                return
        except Exception:
            pass
    w = tk.Toplevel(root)
    TH.paint_toplevel(w)
    _voice_win = w
    w.title("旁白试听 · 挑音色")
    center_win(w, 620, 306, 560, 300)
    w.transient(root)

    vv = tk.StringVar(value=preset_label or voice_var.get())
    rv = tk.StringVar(value=voice_rate_var.get())
    st = tk.StringVar(value="选好音色点「试听」，声音直接从电脑放出来（需联网，约 1-3 秒）。")

    body = ttk.Frame(w, padding=10)
    body.pack(fill="both", expand=True)

    r0 = ttk.Frame(body)
    r0.pack(fill="x", pady=(0, 8))
    ttk.Label(r0, text="音色：", font=FONT_B).pack(side="left")
    ttk.Combobox(r0, textvariable=vv, values=list(VOICES.keys()),
                 width=24, state="readonly").pack(side="left")
    ttk.Label(r0, text="　语速：").pack(side="left")
    ttk.Combobox(r0, textvariable=rv, values=list(RATES.keys()),
                 width=13, state="readonly").pack(side="left")

    ttk.Label(body, text="试听文案（可改，念的就是这段；默认取第一镜台词）：",
              foreground=TH.TEXT2, font=TH.FONT_S).pack(anchor="w")
    txt = tk.Text(body, height=4, font=FONT, wrap="word")
    txt.pack(fill="x", pady=(2, 8))
    _first = ""
    try:
        if shot_rows:
            _first = (shot_rows[0]["say_var"].get() or "").strip()
    except Exception:
        _first = ""
    txt.insert("1.0", _first or "远古雨林深处，小猴子发现了一棵会发光的神树。")


    q = queue.Queue()

    def _worker(text, voice, rate):
        try:
            mp3, dur = VT.preview(text, voice, rate)
            q.put(("ok", mp3, dur))
        except Exception as e:
            q.put(("err", "%s: %s" % (type(e).__name__, e), 0))

    def _poll():
        try:
            while True:
                kind, a, b = q.get_nowait()
                play_btn.config(state="normal")
                if kind == "ok":
                    if VT.play(a):
                        st.set("▶ 正在播放 · %s · %s · 约 %.1f 秒"
                               % (vv.get(), rv.get(), b))
                    else:
                        st.set("⚠ 语音已生成，但播放失败：%s" % a)
                else:
                    st.set("✗ 生成失败（检查网络/代理）：%s" % a)
        except queue.Empty:
            pass
        if w.winfo_exists():
            w.after(200, _poll)

    def do_preview():
        t = txt.get("1.0", "end").strip()
        if not t:
            messagebox.showwarning("没有文案", "写一句话再试听。")
            return
        play_btn.config(state="disabled")
        st.set("⏳ 正在生成语音…")
        threading.Thread(target=_worker,
                         args=(t, VOICES.get(vv.get(), "zh-CN-XiaoxiaoNeural"),
                               RATES.get(rv.get(), "+0%")),
                         daemon=True).start()

    btns = ttk.Frame(body)
    btns.pack(fill="x", pady=(4, 0))
    play_btn = TH.RoundButton(btns, text="▶ 试听", command=do_preview, height=32,
                              kind="accent")
    play_btn.pack(side="left")
    TH.RoundButton(btns, text="■ 停止",
               command=lambda: (VT.stop_play(), st.set("已停止")), height=32).pack(side="left", padx=6)

    def use_it():
        voice_var.set(vv.get())
        voice_rate_var.set(rv.get())
        st.set("✔ 已选用「%s / %s」，关掉本窗口后点「▶ 开始生成」就生效。"
               % (vv.get(), rv.get()))

    TH.RoundButton(btns, text="✓ 用这个音色", command=use_it, height=32).pack(side="left", padx=6)
    TH.RoundButton(btns, text="关闭", command=w.destroy, height=32).pack(side="right")

    ttk.Label(body, textvariable=st, foreground=TH.OK, justify="left",
              font=("Microsoft YaHei", 9)).pack(anchor="w", fill="x", pady=(10, 4))
    ttk.Label(body, foreground=TH.TEXT3, justify="left", font=TH.FONT_S,
              text="这 14 个就是微软语音库里全部的中文音色\n"
                   "（含东北话 / 陕西话 / 粤语 / 台湾腔）；换音色不用重装，选完直接出片。"
              ).pack(anchor="w", fill="x")

    w.after(200, _poll)
    try:
        w.lift()
    except Exception:
        pass


# ============================ 模型管理 ============================
ST_LABEL = {"ok": "✅ 就绪", "conv": "🔧 需转换", "miss": "❌ 缺失", "opt": "⭕ 可选"}
_model_win = None
_mq = queue.Queue()          # 模型窗口自己的消息队列


def _mb_txt(mb):
    return "%.1fG" % (mb / 1024.0) if mb >= 1024 else "%dM" % int(mb or 0)


def refresh_model_dropdown():
    """按本机实际文件重建「模型精度」下拉（换电脑/换了量化版本都能选到）。"""
    global MODEL_PRESETS
    found = MM.scan_unets()
    if not found:
        MODEL_PRESETS = {"⚠ 没找到模型，点上面「📦 模型」去下载": ""}
    else:
        MODEL_PRESETS = {u["label"]: u["fname"] for u in found}
    try:
        model_cb.config(values=list(MODEL_PRESETS.keys()))
        if model_var.get() not in MODEL_PRESETS:
            pref = next((k for k in MODEL_PRESETS if "推荐" in k), None)
            model_var.set(pref or list(MODEL_PRESETS.keys())[0])
    except Exception:
        pass


def _apply_models_dir(newdir):
    """把模型目录指到别处：建好子目录 + 改写 extra_model_paths.yaml。"""
    subs = ("unet", "diffusion_models", "text_encoders", "vae", "loras",
            "clip_projections")
    try:
        os.makedirs(newdir, exist_ok=True)
        for s in subs:
            os.makedirs(os.path.join(newdir, s), exist_ok=True)
    except Exception as e:
        messagebox.showerror("无法使用该目录", "%s\n\n%s" % (newdir, e))
        return False
    cfg = dict(AP.CFG)
    cfg["models_dir"] = newdir
    AP.save_config(cfg)
    AP.CFG["models_dir"] = newdir
    AP.MODELSDIR = newdir
    MM.MODELS_ROOT = newdir
    MM.ensure_extra_yaml()
    return True


def open_models(auto=False):
    global _model_win
    _t_open = time.time()
    if _model_win is not None and _model_win.winfo_exists():
        _model_win.lift()
        _model_win.focus_force()
        return
    w = tk.Toplevel(root)
    TH.paint_toplevel(w)
    _model_win = w
    w.title("模型管理 · 换电脑必看")
    center_win(w, 900, 700, 780, 620)
    w.transient(root)

    head = ttk.Label(w, wraplength=840, justify="left", font=("Microsoft YaHei", 9))
    head.pack(fill="x", padx=12, pady=(10, 4))

    nb = ttk.Notebook(w)
    nb.pack(fill="both", expand=True, padx=10, pady=4)

    # ---------------- 页1：模型 ----------------
    f1 = ttk.Frame(nb)
    nb.add(f1, text="  模型文件  ")

    cols = ("st", "desc", "file", "size")
    tree = ttk.Treeview(f1, columns=cols, show="headings", height=8,
                        selectmode="extended")
    for c, t, wd in (("st", "状态", 80), ("desc", "用途", 300),
                     ("file", "文件名", 300), ("size", "实际大小", 80)):
        tree.heading(c, text=t)
        tree.column(c, width=wd, anchor="w" if c != "size" else "e")
    # 注意：tree 放到最后才 grid —— 先让下面的日志/进度/按钮拿到各自高度，
    # 剩下的空间才给列表，否则窗口一小就把进度条挤没（900x700 实测会消失）。

    logbox = scrolledtext.ScrolledText(f1, height=5, wrap="word",
                                       font=("Microsoft YaHei", 9))
    logbox.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))

    pbar = ttk.Progressbar(f1, mode="determinate", maximum=100)
    pbar.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 1))
    pstate = ttk.Label(f1, text="空闲", font=("Microsoft YaHei", 9),
                       foreground=TH.TEXT3)
    pstate.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 2))

    btns = ttk.Frame(f1)
    btns.grid(row=4, column=0, sticky="ew", padx=6, pady=(2, 2))
    btns2 = ttk.Frame(f1)
    btns2.grid(row=5, column=0, sticky="ew", padx=6, pady=(0, 4))

    def log(msg):
        logbox.insert("end", msg + "\n")
        logbox.see("end")

    def refresh():
        tree.delete(*tree.get_children())
        s = MM.summary()
        for e in s["items"]:
            tree.insert("", "end", iid=e["key"], values=(
                ST_LABEL[e["st"]], e["desc"], e["fname"],
                _mb_txt(e["real_mb"] or e["mb"]), e["path"] or "—"))
        root_txt = "模型目录：%s" % MM.MODELS_ROOT
        if not os.path.isdir(MM.MODELS_ROOT):
            root_txt += "（不存在，会自动创建）"
        if s["ready"]:
            st_txt = "✅ 模型齐全，可以直接出片。"
        else:
            st_txt = ("⚠ 还缺 %d 项，共约 %.1f GB —— 选中后点「⬇ 下载选中」，"
                      "或点「⬇ 下载全部缺失」。" % (len(s["need"]), s["need_mb"] / 1024.0))
        head.config(text=root_txt + "\n" + st_txt)
        refresh_model_dropdown()
        return s

    # --- 下载 ---
    def _prog(got, tot):
        tot = tot or 1
        pct = max(0, min(100, got * 100.0 / tot))
        _mq.put(("prog", pct, "%.0f / %.0f MB" % (got, tot)))

    def _worker(keys):
        for i, k in enumerate(keys):
            e = next((x for x in MM.inventory() if x["key"] == k), None)
            if not e:
                continue
            _mq.put(("log", "\n[%d/%d] %s → %s" % (i + 1, len(keys), e["desc"], e["fname"])))
            if e["st"] == "ok":
                _mq.put(("log", "    已存在，跳过"))
                continue
            if e["st"] == "conv":
                _mq.put(("log", "    已有官方原版，直接转换…"))
                ok, msg = MM.convert_lora(e)
                _mq.put(("log", "    " + ("转换成功：" + msg if ok
                                          else "转换失败：" + str(msg))))
                continue
            ok, msg = MM.download_entry(e, cb=_prog)
            if ok:
                _mq.put(("log", "    完成：%s" % msg))
                if e.get("convert_from"):
                    _mq.put(("log", "    正在转成本项目要用的 prunedfmt 版本…"))
                    ok2, msg2 = MM.convert_lora(e)
                    _mq.put(("log", "    " + ("转换成功" if ok2 else "转换失败：" + str(msg2))))
            else:
                _mq.put(("log", "    ✗ 失败：%s" % msg))
                _mq.put(("log", "    （可换镜像重试，或点「📄 导出清单」把链接发给别人手动下）"))
        _mq.put(("done",))

    def _poll():
        try:
            while True:
                msg = _mq.get_nowait()
                if msg[0] == "log":
                    log(msg[1])
                elif msg[0] == "prog":
                    pbar["value"] = msg[1]
                    pstate.config(text="正在下载 " + msg[2])
                elif msg[0] == "nodes":
                    # 节点检测在后台线程做完，结果从队列回来再写 UI（不能跨线程碰 Tk）
                    ntxt.delete("1.0", "end")
                    ntxt.insert("end", msg[1])
                elif msg[0] == "done":
                    pbar["value"] = 0
                    pstate.config(text="结束")
                    refresh()
        except queue.Empty:
            pass
        if w.winfo_exists():
            w.after(250, _poll)

    def start_dl(keys):
        keys = [k for k in keys]
        if not keys:
            messagebox.showinfo("没选", "先在列表里选中要下载的项（可按住 Ctrl 多选）。")
            return
        pstate.config(text="准备中…")
        threading.Thread(target=_worker, args=(keys,), daemon=True).start()

    def dl_selected():
        start_dl(tree.selection())

    def dl_missing():
        s = MM.summary()
        keys = [e["key"] for e in s["need"]]
        if not keys:
            messagebox.showinfo("不缺", "模型都齐了，不用下载。")
            return
        if not messagebox.askyesno(
                "确认下载",
                "将下载 %d 项，共约 %.1f GB：\n\n%s\n\n存到：\n%s\n\n"
                "支持断点续传，中途关掉也能下次接着下。"
                % (len(keys), s["need_mb"] / 1024.0,
                   "\n".join("· " + e["desc"] for e in s["need"]),
                   MM.MODELS_ROOT)):
            return
        start_dl(keys)

    def conv_selected():
        s = MM.inventory()
        conv = [e for e in s if e["key"] in tree.selection() and e["st"] == "conv"]
        if not conv:
            messagebox.showinfo("没有要转换的",
                                "「🔧 需转换」的项才需要转换（Turbo LoRA 要剥掉前缀）。")
            return
        start_dl([e["key"] for e in conv])

    def change_dir():
        d = filedialog.askdirectory(title="选择模型所在的文件夹（里面应有 unet / text_encoders 等子目录）")
        if not d:
            return
        if _apply_models_dir(d):
            log("[目录] 模型目录已改为：" + d)
            log("       extra_model_paths.yaml 已同步改写。")
            log("       请点「🔻 关闭后端·省内存」再点「⚡ 启动后端」，让 ComfyUI 重新读目录。")
            refresh()

    def open_dir():
        try:
            os.makedirs(MM.MODELS_ROOT, exist_ok=True)
            os.startfile(MM.MODELS_ROOT)
        except Exception as e:
            messagebox.showerror("打不开", str(e))

    def import_files():
        """把手上已有的模型文件导入模型目录（自动按文件名归类）。
        先选文件；没选文件就改问「要不要选整个文件夹」。"""
        ps = list(filedialog.askopenfilenames(
            title="选模型文件（可多选：.gguf / .safetensors）",
            filetypes=[("模型文件", "*.gguf *.safetensors *.sft"),
                       ("所有文件", "*.*")]) or [])
        if not ps:
            if not messagebox.askyesno(
                    "没选文件",
                    "刚才没选文件。\n\n要不要改为选一个文件夹？\n"
                    "（会把该文件夹及其一层子目录里所有 .gguf / .safetensors 一起导入）"):
                return
            d = filedialog.askdirectory(title="选择放模型的文件夹")
            if not d:
                return
            base = d.rstrip("\\/").count(os.sep)
            for r, dirs, fs in os.walk(d):
                if r.rstrip("\\/").count(os.sep) - base >= 1:
                    dirs[:] = []          # 只往下找一层，避免扫全盘
                for fn in fs:
                    if fn.lower().endswith((".gguf", ".safetensors", ".sft")):
                        ps.append(os.path.join(r, fn))
            if not ps:
                messagebox.showwarning("没找到模型文件",
                                       "这个文件夹里没有 .gguf / .safetensors 文件。")
                return
        ps = [p for p in ps if os.path.isfile(p)]
        if not ps:
            return
        pre = "\n".join("· %s → models/%s/" % (os.path.basename(p), MM.guess_cat(p))
                        for p in ps[:12])
        if len(ps) > 12:
            pre += "\n… 共 %d 个" % len(ps)
        if not messagebox.askyesno(
                "确认导入",
                "将把这些文件复制进模型目录：\n\n%s\n\n存到：\n%s\n\n"
                "（原文件保留，不删；复制大文件要一会儿，进度看下面进度条）"
                % (pre, MM.MODELS_ROOT)):
            return
        pstate.config(text="正在导入…")

        def _cb(i, n, fn, cat):
            _mq.put(("log", "\n[%d/%d] %s → models/%s/" % (i, n, fn, cat)))

        def _prog(done, total):
            _mq.put(("prog", done * 100.0 / max(1, total),
                     "%.1f / %.1f GB" % (done / 1073741824.0, total / 1073741824.0)))

        def _worker():
            ok, bad = MM.import_local_files(ps, move=False, cb=_cb, prog=_prog)
            for dst, cat, note in ok:
                _mq.put(("log", "    ✓ %s（%s）" % (os.path.basename(dst), note)))
            for p, err in bad:
                _mq.put(("log", "    ✗ %s：%s" % (os.path.basename(p), err)))
            _mq.put(("log", "\n导入结束：成功 %d，失败 %d。点「🔄 重新检测」确认状态。"
                            % (len(ok), len(bad))))
            _mq.put(("done",))

        threading.Thread(target=_worker, daemon=True).start()

    def export_list():
        p = os.path.join(AP.WORKDIR, "模型下载清单.txt")
        try:
            MM.export_list_txt(p)
            os.startfile(os.path.dirname(p))
            messagebox.showinfo("已导出", "已生成：\n" + p
                                + "\n\n可以直接发给别人，照着链接下就行。")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def make_bat():
        p = os.path.join(AP.WORKDIR, "下载缺失模型.bat")
        try:
            MM.write_download_bat(p)
            os.startfile(os.path.dirname(p))
            messagebox.showinfo("已生成", "已生成：\n" + p
                                + "\n\n双击它就会自动下载缺失的模型（需联网）。")
        except Exception as e:
            messagebox.showerror("生成失败", str(e))

    TH.RoundButton(btns, text="↻ 重新检测", command=refresh, height=32).pack(side="left")
    TH.RoundButton(btns, text="↓ 下载选中", command=dl_selected, height=32).pack(side="left", padx=6)
    TH.RoundButton(btns, text="↓ 下载全部缺失", command=dl_missing, height=32).pack(side="left")
    TH.RoundButton(btns, text="※ 转换所选 LoRA", command=conv_selected, height=32).pack(side="left", padx=6)
    TH.RoundButton(btns, text="关闭", command=w.destroy, height=32).pack(side="right")

    TH.RoundButton(btns2, text="↓ 导入本地模型", command=import_files, height=32).pack(side="left")
    TH.RoundButton(btns2, text="▸ 打开模型文件夹", command=open_dir, height=32).pack(side="left", padx=6)
    TH.RoundButton(btns2, text="▸ 换个模型目录", command=change_dir, height=32).pack(side="left")
    btns3 = ttk.Frame(f1)
    btns3.grid(row=6, column=0, sticky="ew", padx=6, pady=(0, 8))
    TH.RoundButton(btns3, text="▤ 导出下载清单", command=export_list, height=32).pack(side="left")
    TH.RoundButton(btns3, text="▤ 生成下载.bat", command=make_bat, height=32).pack(side="left", padx=6)
    tree.grid(row=0, column=0, sticky="nsew", padx=6, pady=(6, 2))
    f1.columnconfigure(0, weight=1)
    f1.rowconfigure(0, weight=1)

    # ---------------- 页2：运行环境 / 节点 ----------------
    f2 = ttk.Frame(nb)
    nb.add(f2, text="  运行环境 / 节点  ")
    ntxt = scrolledtext.ScrolledText(f2, wrap="word", font=("Microsoft YaHei", 9))
    ntxt.pack(fill="both", expand=True, padx=8, pady=8)
    nbtns = ttk.Frame(f2)
    nbtns.pack(fill="x", padx=8, pady=(0, 8))

    def node_report():
        """检测自定义节点。

        check_nodes 要打后端 HTTP，后端没起时只能等网络超时（实测 ~2s），
        放主线程会把整个模型窗口卡住——这里改成后台线程跑，
        主线程先写好本地信息、把节点那几行留成「检测中…」，结果从队列回来再补。
        """
        ntxt.delete("1.0", "end")
        L = []
        L.append("ComfyUI 主程序 : %s   [%s]"
                 % (COMFY_MAIN, "存在" if os.path.exists(COMFY_MAIN) else "❌ 找不到"))
        L.append("Python        : %s   [%s]"
                 % (COMFY_PYTHON, "存在" if os.path.exists(COMFY_PYTHON) else "❌ 找不到"))
        L.append("custom_nodes  : %s   [%s]"
                 % (MM.comfy_custom_nodes_dir(),
                    "存在" if os.path.isdir(MM.comfy_custom_nodes_dir()) else "❌ 找不到"))
        L.append("")
        L.append("—— 自定义节点（缺了出不了片，装在 ComfyUI/custom_nodes 下）——")
        ntxt.insert("end", "\n".join(L) + "\n正在检测后端…\n")

        def _work():
            miss = MM.check_nodes(timeout=6)
            L2 = list(L)
            if miss is None:
                L2.append("   后端不在线，无法检测。先点「⚡ 启动后端」，再点「检查节点」。")
                for cls, repo, desc, url in MM.NEEDED_NODES:
                    L2.append("   · %-26s %s" % (repo, desc))
            else:
                if not miss:
                    L2.append("   ✅ 全部节点都在，可以出片。")
                for cls, repo, desc, url in miss:
                    L2.append("   ❌ 缺 %s" % cls)
                    L2.append("      节点包：%s" % repo)
                    L2.append("      说明：  %s" % desc)
                    L2.append("      地址：  %s" % url)
                L2.append("")
                L2.append("提示：本项目专用节点（ManjuI2VFast / ManjuLatentRelay）"
                          "已打包在程序里，点下面「一键安装自带节点」即可；")
                L2.append("      第三方节点（GGUF / Turbo / ClipProj）可用 git clone 装到 "
                          "custom_nodes 后重启后端。")
            _mq.put(("nodes", "\n".join(L2)))

        threading.Thread(target=_work, daemon=True).start()

    def install_node():
        ok, msg = MM.install_bundled_node()
        if ok:
            messagebox.showinfo("完成", "自带节点已装到：\n%s\n\n请重启后端生效。" % msg)
        else:
            messagebox.showerror("失败", msg)
        node_report()

    def open_cn():
        d = MM.comfy_custom_nodes_dir()
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as e:
            messagebox.showerror("打不开", str(e))

    TH.RoundButton(nbtns, text="◎ 检查节点", command=node_report, height=32).pack(side="left")
    TH.RoundButton(nbtns, text="▣ 一键安装自带节点", command=install_node, height=32).pack(side="left", padx=6)
    TH.RoundButton(nbtns, text="▸ 打开 custom_nodes", command=open_cn, height=32).pack(side="left")
    TH.RoundButton(nbtns, text="关闭", command=w.destroy, height=32).pack(side="right")

    w.update_idletasks()      # 先把窗口画出来，别让后面的检测挡住
    refresh()
    node_report()              # 节点检测走后台线程，不阻塞
    w.after(250, _poll)
    _crash("模型窗口打开耗时 %.0f ms（节点检测已转后台）"
           % ((time.time() - _t_open) * 1000.0))
    if auto:
        w.after(400, dl_missing)
    log("模型不在这个 exe 里（整套约 20GB），需要单独放/下。")
    log("· 文件已经在硬盘上（自己下好的 / 别人拷给你的）：点「📥 导入本地模型」→ 自动归类；")
    log("  也可以手动拖进「📂 打开模型文件夹」里的 unet / text_encoders / vae / loras 子目录，")
    log("  放好后点「🔄 重新检测」，状态变 ✅ 就绪 即可出片。")
    log("· 本机已经有整套模型：点「📁 换个模型目录」指过去即可，不用重下。")
    log("· 本机没有：勾选「❌ 缺失」的项，点「⬇ 下载选中」（ModelScope 镜像，支持断点续传）。")
    log("· 想发给别人：点「📄 导出下载清单」或「✂ 生成下载.bat」，让对方自己下。")


# ============================ 镜头编辑 ============================
shot_rows = []  # 每个元素: {"frame","no","scene","say_var"}

PH_MOTION = "可选：如『镜头缓慢推近』『蜜蜂绕飞』（留空则用默认运镜）"
PH_SAY = "这一镜念出来的旁白台词（必填）"
PH_DUR = "如 8（秒）"
PH_SFX = "如 地铁轰鸣、进站提示音"
# 锚定（denoise）留空＝交给程序按镜别自动定：全景/打戏 0.72~0.82、
# 中近景 0.55~0.70、三视图背影 0.42~0.50。以前占位写 "0.80"，
# 看着像"已经填好了"，其实是个空值的灰字提示。
PH_DN = "留空自动"


def _set_placeholder(entry, var, text):
    """输入框灰字占位提示：仅当内容为空时显示；聚焦清空，失焦且为空时恢复。"""
    def on_in(e):
        if var.get() == text:
            entry.delete(0, "end")
            try:
                entry.configure(style="TEntry")
            except Exception:
                pass
    def on_out(e):
        if var.get().strip() == "":
            entry.delete(0, "end")
            entry.insert(0, text)
            try:
                entry.configure(style="Placeholder.TEntry")
            except Exception:
                pass
    if var.get().strip() == "":
        entry.insert(0, text)
        try:
            entry.configure(style="Placeholder.TEntry")
        except Exception:
            pass
    entry.bind("<FocusIn>", on_in)
    entry.bind("<FocusOut>", on_out)








# ======================== 分镜数据模型（UE5 停靠布局改造） ========================
# 改造前：每一镜的所有字段都住在它自己那套控件里（ScrolledText / Entry / Combobox），
# 界面上一屏摆 N 张卡片。
#
# 改成虚幻编辑器那种停靠布局后，左栏只放「大纲条目」、右栏只放「当前选中那一镜」的
# 详情表单 —— 同一时刻只有一份表单，所以数据必须先挪到一个中立的地方，
# 否则点一下别的镜，刚敲的字就跟着控件一起没了。
#
# 现在 shot_rows[i] 是一个 dict：
#   scene       —— _SceneBuf（纯文本，接口和 tk.Text 一样，但不需要控件）
#   *_var       —— 真正的 tk 变量（有没有控件绑着都能读写）
#   *_manual    —— [False] 这种可变标志（用户手动改过就锁住，自动识别不再覆盖）
#   auto_size / auto_motion / refresh_loc —— 自动识别函数（不依赖控件）
#   frame / no / *_cb / *_hint_lbl / face_lbl —— 只在「被选中」时才有控件，其余时刻 None
class _SceneBuf:
    """① 画面描述的存储：接口对齐 tk.Text，但不需要控件。

    原来靠 ScrolledText 存这一镜的描述，控件一销毁数据就没了；改成停靠布局后
    详情表单会反复重建，所以文本必须自己存。
    """

    __slots__ = ("_t",)

    def __init__(self, text=""):
        self._t = text or ""

    def get(self, *_a):
        return self._t

    def set(self, text):
        self._t = text if text is not None else ""

    def insert(self, _index, s):
        self._t = (self._t or "") + (s or "")

    def delete(self, *_a):
        self._t = ""

    def edit_modified(self, _flag=None):
        return False

    def mark_set(self, *_a):
        pass

    def see(self, *_a):
        pass


_SEL = {"i": -1}          # 当前选中的镜（-1 = 没选）


def _idx_of(rec):
    """按**身份**找下标。不能用 list.index：两镜字段完全相同时会被判成同一个。"""
    for i, r in enumerate(shot_rows):
        if r is rec:
            return i
    return -1


def _row_of(i):
    try:
        return shot_rows[i] if 0 <= i < len(shot_rows) else None
    except Exception:
        return None


def _hint(rec, key, text, color=None):
    """给提示文字赋值 —— 只有该镜当前显示在详情页时，那个 Label 才存在。"""
    lb = rec.get(key)
    if lb is None:
        return
    try:
        lb.configure(text=text, foreground=color or TH.TEXT3)
    except Exception:
        pass


def _prev_loc_of(idx):
    """上一镜的场景名（用来判断"这一镜还在同一个地方"）。"""
    for j in range(idx - 1, -1, -1):
        r = shot_rows[j]
        try:
            v = (r["loc_var"].get() or "").strip()
        except Exception:
            v = ""
        if not v:
            try:
                v = guess_scene(r["scene"].get() or "")
            except Exception:
                v = ""
        if v:
            return v
    return ""


def _prev_shot_loc(row):
    """兼容旧调用：传进行 frame 时换算成上一镜场景。"""
    i = -1
    for k, r in enumerate(shot_rows):
        if r.get("frame") is row:
            i = k
            break
    return _prev_loc_of(i)


def _new_shot(scene="", say="", motion="", dur="", sfx="", ref="", dn="",
              size="", loc="", direct=""):
    """建一镜的**数据**（不建控件），返回它在 shot_rows 里的下标。"""
    rec = {
        "scene": _SceneBuf(scene),
        "say_var": tk.StringVar(value=say or ""),
        "dur_var": tk.StringVar(value=dur or ""),
        "sfx_var": tk.StringVar(value=sfx or ""),
        "motion_var": tk.StringVar(value=motion or ""),
        "size_var": tk.StringVar(value=size or ""),
        "loc_var": tk.StringVar(value=loc or ""),
        "ref_var": tk.StringVar(value=ref or ""),
        "dn_var": tk.StringVar(value=dn or ""),
        # 自动导演给的镜头语言（景别/角度/运镜/构图），拼进提示词就是这一镜的拍法
        "direct_var": tk.StringVar(value=direct or ""),
        # 剧本/表格里写了的算「用户指定」，锁住，自动识别不再覆盖
        "motion_manual": [bool((motion or "").strip())],
        "size_manual": [bool((size or "").strip())],
        "loc_manual": [bool((loc or "").strip())],
        # 下面这些只有该镜被选中、详情表单正在显示它时才有控件
        "frame": None, "no": None, "row_labels": None,
        "loc_cb": None, "size_cb": None, "ref_cb": None, "face_lbl": None,
        "motion_hint_lbl": None, "size_hint_lbl": None, "loc_hint_lbl": None,
    }
    shot_rows.append(rec)

    # ---------------- 自动识别三件套（不依赖控件，换镜后照样能用） ----------------
    def _sync_dur(*_a):
        """自动定帧时把「这一镜实际多长」填进③，别让它一直空着像没填的坑。

        A 方案下帧数由台词决定（下限 124 帧 = 5.2 秒），这里只是换算成秒显示；
        用户关了自动定帧就不动，免得覆盖手填的值。
        """
        try:
            if not (native_var.get() or auto_len_var.get()):
                return
            u = _speech_units(rec["say_var"].get() or "")
            sec = 0.25 + u / NATIVE_CPS + 0.55
            sec = max(NATIVE_MIN_FRAMES / 24.0, min(NATIVE_MAX_FRAMES / 24.0, sec))
            rec["dur_var"].set("%.1f" % sec)
        except Exception:
            pass

    def auto_size():
        """按① 画面描述重新认一次景别（用户手动选过的不动）。"""
        try:
            if rec["size_manual"][0]:
                return
            g = guess_size_loose(rec["scene"].get(), rec["say_var"].get())
            if g and g in SIZE_PRESETS and rec["size_var"].get() != g:
                rec["size_var"].set(g)
            _refresh_size_hint()
        except Exception:
            pass

    def _refresh_size_hint():
        try:
            if rec["size_manual"][0]:
                return
            cur = (rec["size_var"].get() or "").strip()
            if cur:
                _hint(rec, "size_hint_lbl", "自动识别：%s" % cur)
            else:
                _hint(rec, "size_hint_lbl", size_hint(rec["scene"].get()))
        except Exception:
            pass

    def auto_motion():
        """按描述+景别+台词重新认一次运镜（用户手动改过的不动）。

        景别先定再认运镜（特写推、全景拉、局部特写定住）；上一镜已经是同一个运镜
        就换一个同档合理的 —— 下拉框里显示什么，成片就用什么。
        """
        try:
            if rec["motion_manual"][0]:
                return
            g = guess_motion(rec["scene"].get(), rec["size_var"].get(),
                             rec["say_var"].get())
            _i = _idx_of(rec)
            if _i > 0:
                g = motion_variety(g or "",
                                   shot_rows[_i - 1]["motion_var"].get().strip(),
                                   rec["size_var"].get())
            if g and rec["motion_var"].get() != g:
                rec["motion_var"].set(g)
            _hint(rec, "motion_hint_lbl",
                  (u"自动识别：%s" % _motion_short(g)) if g else "没认出来，用默认运镜")
        except Exception:
            pass

    def refresh_loc():
        """刷新场景：自己写的地点 → 续上一镜。提示要分清是哪种来源。"""
        try:
            txt = rec["scene"].get()
            cur = (rec["loc_var"].get() or "").strip()
            own = guess_scene(txt)
            if not cur:
                g = guess_scene_with_prev(txt, _prev_loc_of(_idx_of(rec)))
                if g:
                    rec["loc_var"].set(g)
                    cur = g
            cb = rec.get("loc_cb")
            if cb is not None:
                vals = _scene_choices()
                if cur and cur not in vals:
                    vals = [cur] + vals
                try:
                    cb.configure(values=vals)
                except Exception:
                    pass
            if not rec["loc_manual"][0]:
                if cur and own == cur:
                    _hint(rec, "loc_hint_lbl", "自动识别：%s" % cur)
                elif cur:
                    _hint(rec, "loc_hint_lbl", "延续上一镜：%s" % cur)
                else:
                    _hint(rec, "loc_hint_lbl", scene_hint(txt))
        except Exception:
            pass

    rec["auto_size"] = auto_size
    rec["auto_motion"] = auto_motion
    rec["refresh_loc"] = refresh_loc
    rec["sync_dur"] = _sync_dur
    rec["refresh_size_hint"] = _refresh_size_hint

    try:
        rec["say_var"].trace_add("write", _sync_dur)
    except Exception:
        pass

    # 刚建好先认一遍（导入剧本时表格里景别/运镜那几列常常是空的）
    if not rec["size_manual"][0]:
        auto_size()
    if not rec["motion_manual"][0]:
        auto_motion()
    _sync_dur()
    refresh_loc()
    return len(shot_rows) - 1


# -------------------------------- 左栏大纲 --------------------------------
def _shot_meta(rec):
    """大纲条目第一行：景别 · 场景 · 时长。"""
    parts = []
    sz = (rec["size_var"].get() or "").strip()
    loc = (rec["loc_var"].get() or "").strip()
    dur = (rec["dur_var"].get() or "").strip()
    if sz:
        parts.append(sz)
    if loc:
        parts.append(loc)
    if dur:
        parts.append("%ss" % dur)
    return "  ·  ".join(parts) or "（未设置景别/场景）"


def _shot_summary(rec):
    """大纲条目第二行：先显示台词，没台词就显示画面描述前段。"""
    say = (rec["say_var"].get() or "").strip()
    txt = (say or (rec["scene"].get() or "").strip()).replace("\n", " ")
    if not txt:
        return "（还没写内容）"
    return txt[:30] + ("…" if len(txt) > 30 else "")


def _outline_touch(i):
    """只刷新一条大纲的文字（打字时用这个，不整表重建）。"""
    rec = _row_of(i)
    if not rec:
        return
    lb = rec.get("row_labels")
    if not lb:
        return
    try:
        lb[0].configure(text=_shot_meta(rec))
        lb[1].configure(text=_shot_summary(rec))
    except Exception:
        pass


def _outline_highlight():
    """选中态：UE5 用整行蓝底（SEL）+ 左侧一根亮条。"""
    for i, r in enumerate(shot_rows):
        fr = r.get("frame")
        if fr is None:
            continue
        on = (i == _SEL["i"])
        bg = TH.SEL_BG if on else TH.BG
        try:
            fr.configure(background=bg)
            fr._bar.configure(background=(TH.ACCENT if on else bg))
            # 序号框也要跟着换底色，否则选中行里会留一块深色方块（看着像没对齐）
            if r.get("no") is not None:
                r["no"].configure(background=bg,
                                  foreground=(TH.TEXT if on else TH.TEXT3))
            for lb in (r.get("row_labels") or ()):
                lb.configure(background=bg,
                             foreground=(TH.TEXT if on else TH.TEXT2))
        except Exception:
            pass


def _outline_empty_card(parent):
    """一个分镜都没有时，在大纲栏里摆一张「从这儿开始」的卡片。

    以前导入入口只是脚部那排小按钮里的「⇩ 导入」—— 和 🗑 ↑ ↓ 同尺寸、同灰色，
    第一次用的人根本注意不到（用户反馈「一般人看不到」）。空列表本来就空着，
    不如把这块地方直接用来告诉他第一步点哪儿。
    """
    card = tk.Frame(parent, background=TH.FIELD,
                    highlightthickness=1, highlightbackground=TH.ACCENT)
    card.pack(fill="x", padx=6, pady=(8, 6))
    tk.Label(card, text="还没有分镜", background=TH.FIELD, foreground=TH.TEXT,
             font=(TH.FONT[0], 10, "bold"), anchor="w").pack(
        fill="x", padx=12, pady=(10, 3))
    tip = tk.Label(card,
                   text="粘贴分镜表，自动排好每一镜。",
                   background=TH.FIELD, foreground=TH.TEXT2, justify="left",
                   font=(TH.FONT[0], 9), anchor="w")
    tip.pack(fill="x", padx=12, pady=(0, 2))

    def _wrap_tip(_e=None, lb=tip, c=card):
        # 大纲栏可以被拖得很窄：按卡片实际宽度重设折行宽度，别让字挤成一条竖线
        try:
            lb.configure(wraplength=max(140, c.winfo_width() - 28))
        except Exception:
            pass
    card.bind("<Configure>", _wrap_tip, add="+")
    TH.RoundButton(card, text="导入剧本表格", command=open_import,
                   kind="accent", height=34, padx=18,
                   font=(TH.FONT[0], 10, "bold")).pack(anchor="w", padx=12,
                                                       pady=(10, 0))
    sub = tk.Frame(card, background=TH.FIELD)
    sub.pack(fill="x", padx=12, pady=(6, 10))
    TH.RoundButton(sub, text="📂 从文件导入", command=open_import,
                   kind="normal", height=26, padx=8,
                   font=(TH.FONT[0], 8)).pack(side="left")
    ex = tk.Label(sub, text="看看示例", background=TH.FIELD,
                  foreground=TH.ACCENT,
                  font=(TH.FONT[0], 9, "underline"), cursor="hand2")
    ex.pack(side="left", padx=(8, 0))
    ex.bind("<Button-1>", lambda e: load_example())


def _outline_refresh():
    """重建左栏大纲。条目只是一行字，真正的数据都在 shot_rows 里。"""
    try:
        for w in shots_inner.winfo_children():
            w.destroy()
        # 卡片里的圆角按钮也一起销毁了：不清掉注册表，频繁刷新会把 _all 攒爆
        TH.RoundButton._all[:] = [b for b in TH.RoundButton._all
                                  if b.winfo_exists()]
    except Exception:
        return
    for r in shot_rows:
        r["frame"] = None
        r["no"] = None
        r["row_labels"] = None
    kw = (_filter_var.get() or "").strip().lower()
    if not _rows_alive():
        _outline_empty_card(shots_inner)
    for i, rec in enumerate(shot_rows):
        hay = " ".join([rec["scene"].get() or "", rec["say_var"].get() or "",
                        rec["size_var"].get() or "", rec["loc_var"].get() or ""])
        if kw and kw not in hay.lower():
            continue
        fr = tk.Frame(shots_inner, background=TH.BG, cursor="hand2")
        fr.pack(fill="x", padx=2, pady=1)
        # 左侧竖条：选中时亮成强调色，比整行描边更像 UE5 大纲
        bar = tk.Frame(fr, width=3, background=TH.BG)
        bar.pack(side="left", fill="y")
        fr._bar = bar
        no = tk.Label(fr, text="%02d" % (i + 1), background=TH.BG,
                      foreground=TH.TEXT2, font=(TH.FONT[0], 9), width=3,
                      anchor="w")
        no.pack(side="left", padx=(5, 2), pady=4)
        mid = tk.Frame(fr, background=TH.BG)
        mid.pack(side="left", fill="x", expand=True, pady=3)
        l1 = tk.Label(mid, text=_shot_meta(rec), background=TH.BG,
                      foreground=TH.TEXT2, font=(TH.FONT[0], 8), anchor="w")
        l1.pack(fill="x")
        l2 = tk.Label(mid, text=_shot_summary(rec), background=TH.BG,
                      foreground=TH.TEXT3, font=(TH.FONT[0], 8), anchor="w")
        l2.pack(fill="x")
        rec["frame"], rec["no"] = fr, no
        rec["row_labels"] = (l1, l2)

        def _pick(_e=None, _i=i):
            _select(_i)
            return "break"
        for w in (fr, bar, no, mid, l1, l2):
            w.bind("<Button-1>", _pick)
    _outline_highlight()
    try:
        canvas.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))
    except Exception:
        pass
    _update_char_stat()


def _shots_refresh():
    """兼容旧名字：现在等于重建大纲。"""
    _outline_refresh()


def renumber():
    for i, r in enumerate(shot_rows, 1):
        lb = r.get("no")
        if lb is not None:
            try:
                lb.configure(text="%02d" % i)
            except Exception:
                pass


def _refresh_all_locs():
    """全部重认一次场景（按顺序，后面的镜能延续前面的）。"""
    for r in shot_rows:
        if r.get("refresh_loc"):
            r["refresh_loc"]()
    _refresh_all_scene_combos()


def _overall_frac(stage, frac, text):
    """把「视频」这段的真实进度算出来。

    make_drama 在视频阶段只在**开镜时**报一次 0.30（第 i 镜 → 0.30+0.55*i/n），
    之后 124 帧采样要跑十几分钟，进度条就一动不动地卡在 30%
    （用户反馈「剧情预览没跟上进度，也没有提示到哪里了」）。
    这里按采样步数把 0.30~0.85 这段均匀填满：采样完成时那段也基本走完了。
    """
    try:
        if str(stage or "") != "视频":
            return frac
        cur = int(_STEP.get("cur") or 0)
        total = int(_STEP.get("total") or 0)
        if not total or (time.time() - float(_STEP.get("at") or 0)) > 25:
            return frac
        import re as _r
        m = _r.search(r"第\s*(\d+)\s*/\s*(\d+)", str(text or ""))
        i, n = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
        inner = ((i - 1) + max(0.0, min(1.0, cur / float(total)))) / max(1, n)
        return 0.30 + 0.55 * inner
    except Exception:
        return frac


def _sync_all_combos(node=None):
    """把只读下拉框的**显示文字**跟它的变量对齐。

    ttk.Combobox 在 state="readonly" 建出来后不会去读 textvariable 的初值，
    直接把 values[0] 画在框里 —— 实测「画幅」明明跑的是横屏 1152x640，
    框里却写着「竖屏 9:16（抖音·快手）」；「风格预设」显示「水墨国风」，
    实际跑的是「写实电影」。用户看一眼就以为选错了，属于纯误导。
    """
    if node is None:
        node = root
    try:
        kids = node.winfo_children()
    except Exception:
        return
    for c in kids:
        try:
            if isinstance(c, ttk.Combobox):
                tv = str(c.cget("textvariable") or "")
                if tv:
                    try:
                        want = str(c.getvar(tv))
                    except Exception:
                        want = ""
                    if want and str(c.get()) != want:
                        c.set(want)
        except Exception:
            pass
        _sync_all_combos(c)


def _select(i, build=True):
    """选中第 i 镜：大纲高亮 + 右栏详情换成它。"""
    if not (0 <= i < len(shot_rows)):
        i = -1
    _SEL["i"] = i
    _outline_highlight()
    if build:
        try:
            _detail_show(i)
        except Exception as e:
            _crash("详情面板刷新失败: %r" % (e,))
        try:
            root.after(20, _sync_all_combos)
        except Exception:
            pass


# -------------------------------- 右栏细节 --------------------------------
_DET = {"i": -1, "job": None}


def _detail_empty():
    """没选中任何镜时的占位（UE5 里 Details 空着就是一块提示）。"""
    box = tk.Frame(_detail_host, background=TH.PANEL)
    box.pack(fill="both", expand=True, pady=24)
    tk.Label(box, text="没有选中的镜头", background=TH.PANEL,
             foreground=TH.TEXT2, font=(TH.FONT[0], 10)).pack(pady=(0, 4))
    tk.Label(box, text="点左栏大纲里的某一条，或者：", background=TH.PANEL,
             foreground=TH.TEXT3, font=(TH.FONT[0], 8)).pack()
    r = tk.Frame(box, background=TH.PANEL)
    r.pack(pady=8)
    _ue_tb(r, text="＋ 新建一镜", command=lambda: add_shot(), height=30,
           padx=12).pack(side="left")
    _ue_tb(r, text="⇩ 导入剧本", command=open_import, height=30,
           padx=12).pack(side="left", padx=(6, 0))


def _detail_show(i):
    """右栏「细节」：显示第 i 镜的完整表单。

    同一时刻只有一份表单 —— 换镜就整块重建。数据在 shot_rows[i] 里，重建不丢东西。
    """
    # 先把上一镜的控件引用清掉：重建后那些控件已经不存在了，
    # 残留的引用会让自动识别往一片死控件里写提示文字。
    for r in shot_rows:
        for k in ("loc_cb", "size_cb", "ref_cb", "face_lbl",
                  "motion_hint_lbl", "size_hint_lbl", "loc_hint_lbl"):
            r[k] = None
    try:
        for w in _detail_host.winfo_children():
            w.destroy()
    except Exception:
        return
    _DET["i"] = i
    _DET["tw"] = None
    rec = _row_of(i)
    if rec is None:
        _detail_empty()
        return

    box = tk.Frame(_detail_host, background=TH.PANEL)
    box.pack(fill="x", padx=6, pady=(4, 0))

    # ---- 标题行：第 N 镜 + 复制/删除
    hd = tk.Frame(box, background=TH.PANEL)
    hd.pack(fill="x", pady=(0, 2))
    tk.Label(hd, text="第 %d 镜" % (i + 1), background=TH.PANEL,
             foreground=TH.TEXT, font=(TH.FONT[0], 10, "bold")).pack(side="left")
    _ue_tb(hd, text="✕ 删除", command=del_shot_selected, kind="danger",
           height=24, padx=9).pack(side="right")
    _ue_tb(hd, text="⧉ 复制", command=lambda: _dup_shot(i), height=24,
           padx=9).pack(side="right", padx=(0, 4))

    # ---- 导演注记：自动导演给这一镜排的镜头语言（会原样拼进提示词）
    try:
        _dnote = (rec["direct_var"].get() if rec.get("direct_var") else "").strip()
        if _dnote:
            tk.Label(box, text="🎬 " + _dnote, background=TH.PANEL,
                     foreground=TH.TEXT3, font=(TH.FONT[0], 8), anchor="w",
                     justify="left", wraplength=340).pack(fill="x", pady=(0, 2))
    except Exception:
        pass

    def lab(text):
        l = ttk.Label(box, text=text, font=FONT_B)
        l.pack(anchor="w", pady=(7, 2))
        return l

    # ---- ① 画面描述（多行，跟着敲自动重认景别/运镜/场景）
    lab("① 画面描述")
    scene_tw = tk.Text(box, height=5, font=FONT, wrap="word", bd=0,
                       relief="flat", background=TH.FIELD, foreground=TH.TEXT,
                       insertbackground=TH.TEXT, highlightthickness=1,
                       highlightbackground=TH.LINE, highlightcolor=TH.ACCENT,
                       padx=7, pady=5)
    scene_tw.insert("1.0", rec["scene"].get() or "")
    scene_tw.pack(fill="x")
    _DET["tw"] = scene_tw

    def _pull_scene():
        try:
            rec["scene"].set(scene_tw.get("1.0", "end-1c"))
        except Exception:
            return
        _outline_touch(i)
        rec["auto_size"]()
        rec["auto_motion"]()
        rec["refresh_loc"]()

    def _scene_typing(_e=None):
        # 打字时别每敲一下就全量重算：300ms 防抖，停手才算
        try:
            if _DET["job"]:
                root.after_cancel(_DET["job"])
        except Exception:
            pass
        _DET["job"] = root.after(300, _pull_scene)

    scene_tw.bind("<KeyRelease>", _scene_typing, add="+")
    scene_tw.bind("<FocusOut>", lambda e: _pull_scene(), add="+")

    # ---- ② 台词/旁白
    lab("② 台词 / 旁白")
    ttk.Entry(box, textvariable=rec["say_var"], font=FONT).pack(fill="x")
    try:
        rec["say_var"].trace_add("write", lambda *a: _outline_touch(i))
    except Exception:
        pass

    # ---- ③ 时长 + ⑦ 景别（并排）
    g2 = tk.Frame(box, background=TH.PANEL)
    g2.pack(fill="x", pady=(7, 0))
    g2.columnconfigure(0, weight=1)
    g2.columnconfigure(1, weight=1)
    ttk.Label(g2, text="③ 时长（秒）", font=FONT_B).grid(row=0, column=0, sticky="w")
    ttk.Label(g2, text="⑦ 景别", font=FONT_B).grid(row=0, column=1, sticky="w",
                                                  padx=(8, 0))
    ttk.Entry(g2, textvariable=rec["dur_var"], width=8, font=FONT).grid(
        row=1, column=0, sticky="ew", pady=(2, 0))
    size_cb = ttk.Combobox(g2, textvariable=rec["size_var"], values=SIZE_PRESETS,
                           state="readonly", font=FONT, width=9)
    size_cb.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(2, 0))
    rec["size_cb"] = size_cb
    # 下拉右边一颗：10 条景别/构图提示词，点一条写进这一镜
    try:
        TH.RoundButton(g2, text="🎬", command=lambda: open_size_lib(rec), height=24,
                       padx=6, kind="quiet", font=(TH.FONT[0], 8)).grid(
            row=1, column=2, sticky="e", padx=(4, 0), pady=(2, 0))
    except Exception:
        pass
    rec["size_hint_lbl"] = ttk.Label(g2, text="", foreground=TH.TEXT3,
                                     font=(TH.FONT[0], 8), anchor="w")
    rec["size_hint_lbl"].grid(row=2, column=0, columnspan=2, sticky="w",
                              pady=(2, 0))

    def _size_pick(*_a):
        rec["size_manual"][0] = True
        _hint(rec, "size_hint_lbl", "手动指定")
        rec["auto_motion"]()      # 景别变了，运镜跟着重算

    size_cb.bind("<<ComboboxSelected>>", _size_pick)

    # ---- ⑤ 运镜
    lab("⑤ 运镜（按描述自动识别，可改）")
    mrow = tk.Frame(box, background=TH.PANEL)
    mrow.pack(fill="x")
    motion_cb = ttk.Combobox(mrow, textvariable=rec["motion_var"],
                             values=MOTION_PRESETS, font=FONT)
    motion_cb.pack(side="left", fill="x", expand=True)
    # 右边挂一颗：48 条能直接进 prompt 的运镜写法，点一条写进这一镜
    try:
        TH.RoundButton(mrow, text="🎥 48", command=lambda: open_motion_lib(rec),
                       height=26, padx=8, kind="quiet",
                       font=(TH.FONT[0], 8)).pack(side="left", padx=(6, 0))
    except Exception:
        pass
    rec["motion_hint_lbl"] = ttk.Label(box, text="", foreground=TH.TEXT3,
                                       font=(TH.FONT[0], 8), anchor="w")
    rec["motion_hint_lbl"].pack(fill="x")

    def _motion_pick(*_a):
        rec["motion_manual"][0] = True
        _hint(rec, "motion_hint_lbl", "手动指定")

    motion_cb.bind("<<ComboboxSelected>>", _motion_pick)
    motion_cb.bind("<KeyRelease>", _motion_pick)

    # ---- ⑥ 参考角色（带定妆照缩略图）
    lab("⑥ 参考角色（锁脸必选）")
    rf = tk.Frame(box, background=TH.PANEL)
    rf.pack(fill="x")
    face_lbl = tk.Label(rf, text="无人", bd=1, relief="solid",
                        background=TH.FIELD, foreground=TH.TEXT3,
                        font=(TH.FONT[0], 8), width=5, height=2)
    face_lbl.pack(side="left", padx=(0, 6))
    ref_cb = ttk.Combobox(rf, textvariable=rec["ref_var"],
                          values=[NOBODY] + list(CHAR_REFS.keys()), font=FONT)
    ref_cb.pack(side="left", fill="x", expand=True)
    rec["face_lbl"], rec["ref_cb"] = face_lbl, ref_cb

    def _show_face(*_a):
        nm = (rec["ref_var"].get() or "").strip()
        fn = CHAR_REFS.get(nm)
        if fn:
            ph = _char_thumb(os.path.join(AP.INPUTDIR, fn), 44)
            if ph is not None:
                try:
                    face_lbl.configure(image=ph, text="", width=44, height=44)
                    face_lbl.image = ph
                    return
                except Exception:
                    pass
        try:
            face_lbl.configure(image="", text="无人", width=5, height=2)
        except Exception:
            pass

    ref_cb.bind("<<ComboboxSelected>>", _show_face)
    try:
        rec["ref_var"].trace_add("write", _show_face)
    except Exception:
        pass
    _show_face()

    # ---- 锚定（denoise）
    rfd = tk.Frame(box, background=TH.PANEL)
    rfd.pack(fill="x", pady=(6, 0))
    ttk.Label(rfd, text="锚定强度（留空＝按景别自动 0.42~0.82）",
              font=(TH.FONT[0], 8), foreground=TH.TEXT3).pack(anchor="w")
    ttk.Entry(rfd, textvariable=rec["dn_var"], width=10, font=FONT).pack(
        anchor="w", pady=(2, 0))

    # ---- ⑧ 场景
    lab("⑧ 场景")
    loc_cb = ttk.Combobox(box, textvariable=rec["loc_var"],
                          values=_scene_choices(), font=FONT)
    loc_cb.pack(fill="x")
    rec["loc_cb"] = loc_cb
    rec["loc_hint_lbl"] = ttk.Label(box, text="", foreground=TH.TEXT3,
                                    font=(TH.FONT[0], 8), anchor="w")
    rec["loc_hint_lbl"].pack(fill="x")

    def _loc_pick(*_a):
        rec["loc_manual"][0] = True
        _hint(rec, "loc_hint_lbl", "手动指定")
        _outline_touch(i)

    loc_cb.bind("<<ComboboxSelected>>", _loc_pick)

    # ---- ④ 音效/备注
    lab("④ 音效 / 备注")
    ttk.Entry(box, textvariable=rec["sfx_var"], font=FONT).pack(fill="x")

    # 下拉框收起时滚轮不该切换选项（ttk 默认会），改去滚右栏
    def _combo_no_wheel(e):
        d = getattr(e, "delta", 0) or (120 if getattr(e, "num", 0) == 4 else -120)
        try:
            _rt_cv.yview_scroll(int(-1 * (d / 120)), "units")
        except Exception:
            pass
        return "break"
    for _c in (motion_cb, ref_cb, size_cb, loc_cb):
        for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                _c.bind(_seq, _combo_no_wheel)
            except Exception:
                pass

    # 提示文字补一次（自动识别/续上一镜/剧本指定 都要能看到结论）
    if rec["size_manual"][0]:
        _hint(rec, "size_hint_lbl", "剧本指定")
    else:
        rec["refresh_size_hint"]()
    if rec["motion_manual"][0]:
        _hint(rec, "motion_hint_lbl", "剧本指定")
    rec["refresh_loc"]()


def _detail_flush():
    """把详情表单里正在编辑的内容写回数据（① 是多行文本，不在 tk 变量里）。"""
    i = _DET["i"]
    rec = _row_of(i)
    if rec is None:
        return
    try:
        tw = _DET.get("tw")
        if tw is not None and tw.winfo_exists():
            rec["scene"].set(tw.get("1.0", "end-1c"))
    except Exception:
        pass


def add_shot(scene="", say="", motion="", dur="", sfx="", ref="", dn="",
             size="", defer=False, loc=""):
    """新建一镜：写数据 + 刷新大纲（defer=True 时批量建完再统一刷）。"""
    i = _new_shot(scene=scene, say=say, motion=motion, dur=dur, sfx=sfx,
                  ref=ref, dn=dn, size=size, loc=loc)
    if not defer:
        _outline_refresh()
        _select(i)
    return i


def _dup_shot(i):
    """复制一镜（UE5 里 Ctrl+D 那种）：放在它后面。"""
    rec = _row_of(i)
    if rec is None:
        return
    ni = _new_shot(scene=rec["scene"].get(),
                   say=rec["say_var"].get(), motion=rec["motion_var"].get(),
                   dur=rec["dur_var"].get(), sfx=rec["sfx_var"].get(),
                   ref=rec["ref_var"].get(), dn=rec["dn_var"].get(),
                   size=rec["size_var"].get(), loc=rec["loc_var"].get())
    moved = shot_rows.pop()
    shot_rows.insert(i + 1, moved)
    for k in ("motion_manual", "size_manual", "loc_manual"):
        try:
            moved[k][0] = rec[k][0]
        except Exception:
            pass
    _outline_refresh()
    _select(i + 1)


def move_shot(d):
    """大纲里上下移（顺序就是成片顺序）。"""
    i = _SEL["i"]
    j = i + d
    if not (0 <= i < len(shot_rows) and 0 <= j < len(shot_rows)):
        return
    shot_rows[i], shot_rows[j] = shot_rows[j], shot_rows[i]
    _outline_refresh()
    _select(j)


def del_shot(frame=None, defer=False):
    """删一镜。

    参数可以是：下标 / 大纲行控件（兼容旧调用）/ None（删当前选中的）。
    """
    if isinstance(frame, int):
        i = frame
    elif frame is not None:
        i = -1
        for k, r in enumerate(shot_rows):
            if r.get("frame") is frame:
                i = k
                break
        if i < 0:
            i = _SEL["i"]
    else:
        i = _SEL["i"]
    if not (0 <= i < len(shot_rows)):
        return
    shot_rows.pop(i)
    if not defer:
        if _SEL["i"] >= len(shot_rows):
            _SEL["i"] = len(shot_rows) - 1
        _outline_refresh()
        _select(_SEL["i"])


def del_shot_selected():
    del_shot(None)


def del_shot_rec(rec, defer=False):
    """按「镜头记录」本身删（批量清空时用）。

    不能传 r["frame"]：被大纲搜索过滤掉的行 frame 是 None，
    那样会误删「当前选中」的那一镜。
    """
    del_shot(_idx_of(rec), defer)




def import_from_file(path, title=""):
    """直接从剧本文件（tab/逗号分隔）载入分镜，跟「导入剧本表格」对话框同一套解析。
    给命令行用：漫剧工作室.pyw 剧本.tsv --run 就能一键跑，不用人肉点。"""
    raw = open(path, "r", encoding="utf-8-sig", errors="ignore").read()
    parsed = parse_table(raw)
    if not parsed:
        # 表格认不出来就按自然语言剧本再试一遍（1. **第一镜：xxx** 那种写法）
        parsed = parse_script(raw)
    if not parsed:
        _crash("[导入] %s：表格/剧本两种格式都没认出分镜行"
               % os.path.basename(path))
        return 0
    for r in shot_rows[:]:
        del_shot_rec(r, defer=True)
    for p in parsed:
        add_shot(scene=p.get("scene", ""), say=p.get("line", ""),
                 motion=p.get("motion", ""), dur=p.get("dur", ""),
                 sfx=p.get("sfx", ""), ref=p.get("ref", ""), dn=p.get("dn", ""),
                 size=p.get("size", ""), defer=True)
    _shots_refresh()
    _refresh_all_locs()      # 批量建完行再统一认场景，后面的镜才能延续前面的
    _t = _auto_title(path, parsed, title)
    _cur = (title_var.get() or "").strip()
    # 从文件导入＝换了一部新剧，剧名一律按新剧本重起，别沿用上一部。
    # 原来只在「空着 / 上次自动起的 / 示例名」时才改，可「上次自动起的」这个标记
    # 只存在内存里，重启后存档恢复的旧剧名就被当成用户手写的、永远不更新 ——
    # 于是导入了新剧本，成片文件名还是上一部的名字（本次实跑就撞上了）。
    if _t and _t != _cur:
        try:
            title_var.set(_t)
        except Exception:
            pass
        _TITLE_AUTO[0] = _t
        log_q.put("[剧本] 剧名改为：《%s》（上一部：%s）\n" % (_t, _cur or "（空）"))
    try:
        canvas.yview_moveto(0.0)
    except Exception:
        pass
    names = _script_char_names(parsed)
    _crash("[导入] %s → 导入 %d 镜，剧名=%s，角色=%s"
           % (os.path.basename(path), len(parsed), title_var.get(),
              "、".join(names[:6])))
    _new_char_flow(parsed, names)      # 角色都绑好了 → 只做自动锚定
    # 场景这一套也自动到位：认出场景 → 写场景文案 → 生成场景照 → 绑到每镜。
    # 跟角色对称：角色锁脸，场景锁地点。
    try:
        _new_scene_flow()
    except Exception as _se:
        _crash("[场景] 自动建场景失败: %s" % _se)
    # 配好剧情就把它「画」出来：中间那块从节点图切到分镜缩略图（用户要求）
    try:
        _vp_apply("story")
    except Exception as _ve:
        _crash("[预览] 切剧情预览失败: %r" % (_ve,))
    # 自动导演（这条路以前漏了，只有对话框那条路才会排镜头语言）
    root.after(1200, lambda: _direct_all(silent=True))
    # 配了「导入后自动出片」就直接排队等资产
    root.after(1600, lambda: _auto_go_arm("文件导入"))
    return len(parsed)


def _wipe_shots():
    """清空所有分镜数据（大纲条目跟着一起消失）。"""
    shot_rows.clear()
    _SEL["i"] = -1
    _DET["i"] = -1
    _DET["tw"] = None
    _outline_refresh()


def clear_shots(ask=True):
    """清空所有分镜（别人不想用预填的示例时，点这个从白纸开始）。"""
    if not shot_rows:
        add_shot()
        return
    if ask and not messagebox.askyesno("清空分镜",
                                       "确定清空当前 %d 个镜头？\n（清空后从空白开始，只留 1 个空镜）"
                                       % len(shot_rows)):
        return
    _wipe_shots()
    add_shot()
    _shots_refresh()


def load_example():
    """重新载入自带示例（香蕉的故事），想照着改的时候用。"""
    if shot_rows and not messagebox.askyesno("载入示例",
                                             "会用示例替换当前 %d 个镜头，继续？" % len(shot_rows)):
        return
    _wipe_shots()
    for sc, sa in EXAMPLE:
        add_shot(sc, sa, defer=True)
    _shots_refresh()
    try:                      # 示例剧本就配它自己的剧名，导入别的剧本时会被自动换掉
        title_var.set(SAMPLE_TITLE)
        _TITLE_AUTO[0] = SAMPLE_TITLE
    except Exception:
        pass
    log_q.put("已载入示例剧本（香蕉的故事），可直接改文字或点「📥 导入剧本表格」换成自己的。\n")
    try:
        _vp_apply("story")      # 载入示例也顺手把剧情画出来
    except Exception:
        pass


STORY_PROMPT = """你是竖屏短剧编剧。请帮我写一个【题材：______】的短剧分镜，共【__】镜。
要求：
1) 竖屏 9:16，每镜约 1 秒（22 帧），全程靠旁白推进，不靠对白；
2) 旁白口语化、有画面感，一句话说完，不超过 20 个字；
3) 只输出一张表格，5 列，表头固定写成：
   镜号 | 画面描述 | 台词/旁白 | 时长(秒) | 音效备注
4) 画面描述必须写清：景别（近景/中景/全景）、主体在做什么、光线方向、色调、氛围，
   越具体越好（这段话会直接交给 AI 出图）；画面里不要出现任何文字、字幕、招牌字；
5) 不要表格以外的解释，不要分镜以外的空话。

我的故事想法：______（一句话就行，例如：一个女孩每天在地铁等同一班车，只为再见到那个人）
"""

STORY_TIPS = """剧情从哪来？（三个办法，按省事排序）

① 让 AI 写（最省事）
   把下面的提示词复制走，粘给豆包 / DeepSeek / Kimi / 通义 / ChatGPT 任意一个，
   把【题材】和【故事想法】填好发过去；它回一张表格，你整段复制，
   回到这里点「📥 导入剧本表格」→ 粘贴 → 解析，镜头就全填好了。

② 自己写（最自由）
   点「＋ 添加一镜」一镜一镜写：画面描述写清「谁 + 在哪 + 在做什么 + 什么光 + 什么色调」，
   旁白写一句人话。10 镜以内最好，太长观众看不完。

③ 已经写好表格（最快）
   用 Excel / 腾讯文档 / 飞书表格做 5 列（镜号/画面描述/台词旁白/时长/音效备注），
   整段复制过来，点「📥 导入剧本表格」直接解析。

剧情素材可以这样找：
· 自己或身边人的真实小事最能打（等车、加班、异地、旧物）；
· 公版故事改编：山海经、民间传说、老童话；
· 一条新闻 / 一个冷知识，换个视角拍；
· 老电影、老歌的桥段，用新场景重讲。
"""


def open_story_help():
    """不会写剧情时的引导：给可直接复制的 AI 提示词 + 素材来源。"""
    w = tk.Toplevel(root)
    TH.paint_toplevel(w)
    w.title("不会写剧情？看这里")
    center_win(w, 760, 620, 680, 520)
    w.transient(root)
    try:
        w.grab_set()
    except Exception:
        pass
    ttk.Label(w, text="剧情不用自己想 —— 复制下面这段，让 AI 按「能直接导入的表格」写给你",
              font=FONT_B).pack(anchor="w", padx=12, pady=(10, 4))
    box = scrolledtext.ScrolledText(w, height=14, wrap="word", font=TH.FONT)
    box.pack(fill="x", padx=12)
    box.insert("1.0", STORY_PROMPT)
    tips = scrolledtext.ScrolledText(w, height=10, wrap="word",
                                    font=("Microsoft YaHei", 9))
    tips.pack(fill="both", expand=True, padx=12, pady=(6, 4))
    tips.insert("1.0", STORY_TIPS)
    tips.configure(state="disabled")

    bar = ttk.Frame(w)
    bar.pack(fill="x", padx=12, pady=(0, 10))

    def copy_prompt():
        txt = box.get("1.0", "end").strip()
        root.clipboard_clear()
        root.clipboard_append(txt)
        messagebox.showinfo("已复制",
                            "提示词已复制。\n\n粘给任意 AI 聊天，把【题材】和【故事想法】填上，"
                            "它回一张表格后，整段复制回来点「📥 导入剧本表格」。")
        w.destroy()
        open_import()

    TH.RoundButton(bar, text="▤ 复制提示词并去导入", command=copy_prompt, height=32).pack(side="left")
    TH.RoundButton(bar, text="关闭", command=w.destroy, height=32).pack(side="right")




def _auto_prompt(scene, style="", who="", size="", direct=""):
    """按 4 集教程的公式自动把「画面描述」补成完整提示词（不用人手写后半截）。

    公式：人物 + 在哪 + 在干什么 + 细节 + 景别 + 角度 + 光线 + 防崩词 + 风格
    用户只写前半截（谁在哪干什么），景别/角度/光线/防崩词由这里自动补齐；
    已经自己写了的项不重复加，避免提示词打架。
    """
    t = (scene or "").strip().rstrip("，,。")
    if not t:
        return ""
    parts = [t]

    # 景别：特写看脸，中景看动作对话，远景看环境
    # 但「手部/兵器/衣角特写」不是脸——写"面部细节"会让模型硬挤一张脸进去
    # 识别统一走 guess_size()：界面上的 ⑦ 下拉框和这里用同一套规则，
    # 用户看到的景别就是提示词里真正生效的景别（不会出现"框里写特写、
    # 提示词里却是远景"这种对不上的情况）。
    if (direct or "").strip():
        # 导演已经给了一整套镜头语言（景别/角度/运镜/构图），别再叠一层自己判的，
        # 否则同一句话里会出现「中景……特写」这种自相矛盾的写法。
        parts.append(direct.strip())
    else:
        _sz = (size or "").strip()
        if _sz not in SIZE_TEXT:
            _sz = guess_size(t)
        if _sz == "局部特写":
            parts.append("局部特写，细节清晰，材质与质感分明，不需要出现人脸")
        elif _sz == "特写":
            parts.append("特写镜头，面部细节清晰，情绪强化")
        elif _sz == "近景":
            parts.append("近景镜头，胸部以上，表情清楚")
        elif _sz == "中景":
            parts.append("中景镜头，人物腰部以上，动作与表情清晰")
        elif _sz == "全景":
            parts.append("全景镜头，人物全身入画，能看清动作与环境的关系")
        elif _sz == "远景":
            parts.append("远景镜头，环境开阔，交代场景布局，氛围感强")
        else:
            parts.append("中景镜头，人物腰部以上，动作与表情清晰")

        # 角度：俯拍显渺小、仰拍显威严、平视最自然
        if not any(k in t for k in ("俯拍", "俯视", "俯角", "仰拍", "仰视", "仰角", "平视")):
            parts.append("平视角度，与人眼齐平")
        elif any(k in t for k in ("俯拍", "俯视", "俯角")):
            parts.append("俯拍角度，从上往下，人物显得渺小，交代场景布局")
        else:
            parts.append("仰拍角度，从下往上，人物高大威严，有压迫感")

    # 光线：人物光必须和环境光统一，否则光影割裂。
    # 注意别用单字匹配：「雨林」会被当成雨天、「弯月形的香蕉」会被当成夜晚（实测踩过）。
    if any(k in t for k in ("雨天", "细雨", "大雨", "暴雨", "雨夜", "雨幕",
                            "下雨", "雨水", "雨丝", "雷雨")):
        parts.append("雨天，湿润地面反光，环境光与人物光统一")
    elif any(k in t for k in ("夜晚", "夜色", "夜幕", "深夜", "夜里", "月色",
                              "月光", "月下", "星光", "星空", "烛光")):
        parts.append("夜晚，冷调月光，环境光与人物光统一")
    elif any(k in t for k in ("黄昏", "夕阳", "落日", "暮色", "残阳", "傍晚")):
        parts.append("黄昏暖色侧逆光，环境光与人物光统一")
    elif "雾" in t:
        parts.append("薄雾弥漫，柔和散射光，环境光与人物光统一")
    elif "雪" in t:
        parts.append("雪天，冷色调，环境光与人物光统一")
    elif "自然光" in (style or ""):
        parts.append("环境光与人物光统一")
    else:
        parts.append("自然光，环境光与人物光统一")

    # 防崩词：教程里的翻车点全压住（手指畸形、脸变形、文字、现代物件）
    parts.append("五官清晰不畸形，手部结构正常，单个主体，画面稳定无扭曲")
    st = (style or "").strip().strip(",，").replace(",", "，")
    if st:
        parts.append(st)
    return "，".join([x for x in parts if x])


def _split_who(say):
    """把「李嬢嬢：撇干净了，汤才清。」拆成 (说话人, 纯台词)。

    A 方案要用说话人决定音色（老太太 / 小年轻 / 画外旁白），
    没有冒号的当"画面里的人自己在说"，由模型按画面人物定声音。
    """
    t = (say or "").strip()
    for sep in ("：", ":"):
        if sep in t:
            who, line = t.split(sep, 1)
            who = who.strip()
            # 冒号前的短前缀才当说话人；像"他说：xxx"这种长句不当
            if 0 < len(who) <= 6 and not any(c in who for c in "，。？！,?!"):
                return who, line.strip()
    return "", t


# ============================ 自动导演 ============================
# 以前每镜的「⑤ 运镜」只是下拉框里的一句话，**根本没进提示词**；角度永远是「平视」。
# 模型拿到的只有画面描述，镜头语言全靠它自己猜 —— 于是同一场戏十个中景、镜头乱晃。
# 这里按叙事功能把一整集排一遍：开场交代环境、换场重定位、对白正反打、情绪怼脸、
# 动作跟拍、收尾拉远；并保证相邻两镜不会同景别、同运镜。
_DIR_WEAK = ("跪", "低头", "蜷", "发抖", "颤抖", "哭", "求", "倒地", "跌坐",
             "后退", "退缩", "虚弱", "伤口", "流血", "被按", "被拖", "被逼")
_DIR_POWER = ("站起身", "站起", "俯视", "居高", "逼近", "怒喝", "喝斥", "命令",
              "拍桌", "拔刀", "拔剑", "举刀", "掐住", "按住", "逼问", "冷笑")
_DIR_ACTION = ("打斗", "出拳", "挥刀", "挥剑", "劈", "斩", "砍", "刺杀", "冲",
               "扑", "追", "逃", "摔", "撞", "踢", "翻", "跃", "跳", "奔",
               "缠斗", "交手")
_DIR_EMO = ("！", "!", "？", "?", "吼", "喊", "嘶", "咆哮", "痛哭", "大笑",
            "尖叫", "咬牙")

_ANGLE_TXT = {
    "平视": "平视角度，与人眼齐平",
    "俯拍": "俯拍角度，从上往下，人物显得渺小、被压制",
    "仰拍": "仰拍角度，从下往上，人物高大、有压迫感",
}
_SIZE_ORDER = ["远景", "全景", "中景", "近景", "特写", "局部特写"]


def _direct_plan():
    """按整集节奏给每一镜排镜头语言，返回 [{i,size,angle,move,framing,note}]。

    和原来的 auto_size / auto_motion 的区别：那两个只看单镜（顶多瞄一眼上一镜），
    不知道这镜是开场还是收尾、是不是换了地方、整集是不是连着三个中景。
    """
    rows = []
    n = len(shot_rows)
    prev_size = ""
    prev_move = ""
    prev_loc = ""
    prev_side = "右"
    for i, rec in enumerate(shot_rows):
        try:
            scene = (rec["scene"].get("1.0", "end") or "").strip()
        except Exception:
            scene = ""
        say = (rec["say_var"].get() or "").strip()
        loc = (rec["loc_var"].get() or "").strip()
        _who, line = _split_who(say)
        t = (scene + " " + line).strip()

        first = (i == 0)
        last = (i == n - 1) and n > 1
        scene_changed = bool(loc and prev_loc and loc != prev_loc)
        is_action = any(k in t for k in _DIR_ACTION)
        is_weak = any(k in t for k in _DIR_WEAK)
        is_power = any(k in t for k in _DIR_POWER)
        short_line = bool(line) and len(line) <= 10
        is_emo = any(k in t for k in _DIR_EMO) or short_line
        has_line = bool(line)

        # ---- 景别 ----
        # 画面描述里已经写明景别的（「特写」「微距」这些），导演别抢戏 —— 那是最准的信号
        _txt_size = ""
        for _k in ("局部特写", "微距", "特写", "近景", "中景", "全景", "远景"):
            if _k in t:
                _txt_size = "局部特写" if _k == "微距" else _k
                break
        if _txt_size:
            size = _txt_size
        elif first or scene_changed:
            size = "全景"                     # 开场/换场先把地方交代清楚
        elif is_action:
            size = "近景"                     # 动作看得清，又不丢环境
        elif is_emo:
            size = "特写"                     # 情绪点怼脸
        else:
            size = "中景"                     # 对白 / 常规叙事
        if scene_changed and not has_line and ("空" in t or "无人" in t):
            size = "远景"
        if size == prev_size:                 # 连着同景别 → 降一档，别让画面发闷
            k = _SIZE_ORDER.index(size) if size in _SIZE_ORDER else 2
            size = _SIZE_ORDER[max(0, k - 1)] if k > 0 else _SIZE_ORDER[1]

        # ---- 角度：谁强势谁压人 ----
        if is_weak and not is_power:
            angle = "俯拍"
        elif is_power and not is_weak:
            angle = "仰拍"
        else:
            angle = "平视"

        # ---- 运镜 ----
        if first:
            move = MOTION_TRACK               # 开场横移，扫一遍环境
        elif is_action:
            move = MOTION_HAND                # 打戏必须手持跟，定机位没有冲击
        elif is_emo:
            move = MOTION_PUSH                # 情绪点推近
        elif last:
            move = MOTION_PULL                # 收尾拉远收气
        elif scene_changed:
            move = MOTION_RISE                # 换场升机位重新定位
        elif has_line:
            move = MOTION_FIX                 # 对白稳住，别晃
        else:
            move = MOTION_PUSH
        if move == prev_move:                 # 和上一镜撞了就换同档合理的
            alts = _MOTION_ALT.get(move) or ()
            move = alts[0] if alts else MOTION_ARC

        # ---- 构图 ----
        _has_person = bool(line) or any(k in t for k in ("人物", "他", "她", "两人", "俩人"))
        if is_action:
            framing = "主体居中，动作方向留足空间"
        elif has_line and _has_person:
            prev_side = "左" if prev_side == "右" else "右"
            eye = "右" if prev_side == "左" else "左"
            framing = "人物位于画面%s侧三分线，视线留白朝%s" % (prev_side, eye)
        else:
            framing = "主体偏%s三分之一，环境留白" % prev_side

        note = "%s镜头；%s；%s；%s" % (
            size, _ANGLE_TXT.get(angle, _ANGLE_TXT["平视"]), move, framing)
        if is_action:
            # 打戏最容易糊成一团：明确要求动作可辨、冲击方向一致
            note += "；动作清晰、肢体可辨，冲击方向一致，不要糊成一团"
        rows.append({"i": i, "size": size, "angle": angle, "move": move,
                     "framing": framing, "note": note})
        prev_size, prev_move = size, move
        if loc:
            prev_loc = loc
    return rows


def _direct_all(silent=False, force=True):
    """一键自动导演：给整集排镜头语言，写进每镜并参与提示词。

    silent=True 时不弹窗不刷状态（导入剧本 / 载入示例后自动跑）。
    force=True 会连用户手选过的景别/运镜一起重排 —— 点「自动导演」本来就是重来一遍。
    """
    if not shot_rows:
        if not silent:
            messagebox.showinfo("还没有分镜", "先导入剧本，或点「＋ 添加一镜」。")
        return 0
    plan = _direct_plan()
    for rec, p in zip(shot_rows, plan):
        try:
            if force or not rec["size_manual"][0]:
                rec["size_var"].set(p["size"])
            if force or not rec["motion_manual"][0]:
                rec["motion_var"].set(p["move"])
            rec["direct_var"].set(p["note"])
        except Exception:
            pass
    try:
        _outline_refresh()
    except Exception:
        pass
    try:
        if _DET.get("i", -1) >= 0:
            _detail_show(_DET["i"])
    except Exception:
        pass
    if not silent:
        try:
            log_q.put("[导演] 已为 %d 镜排好镜头语言（景别 / 角度 / 运镜 / 构图）\n"
                      % len(plan))
            set_status("自动导演：%d 镜已排好" % len(plan), "ok")
        except Exception:
            pass
    return len(plan)


# ============================ A 方案拆镜（长台词） ============================
# A 方案是模型自己念台词：音频和画面在**同一次前向**里长出来，所以一镜能念多久
# 完全由帧数决定。单镜上限 124 帧 ≈ 5.2 秒（再往上显存和耗时都是平方级涨）。
#
# 以前的做法是「算出来超过 124 就直接截到 124」—— 结果《雨夜断剑》三镜台词
# 分别需要 10.5 / 12.3 / 3.9 秒，全被砍到 5.2 秒，模型只念得完一半，后半句没了。
#
# 现在生成前先把超长台词按标点拆成几段，每段一镜，画面/运镜/角色全部沿用，
# 关键帧复用主镜那张（make_drama 认 "key_from"），所以不多花出图时间。
# H3 官方训练范围（节点 tooltip）：124 = ~5s，trained range ~124-362。
# 124 是**下限**不是上限 —— 以前当上限用，所有镜都短于训练范围。
NATIVE_MIN_FRAMES = 124
NATIVE_MAX_FRAMES = 362
NATIVE_CPS = 4.3        # 中文口语约 4.3 字/秒（与 make_drama.plan_native_lengths 同源）
# 单镜可念的字量：362 帧 ≈ 15.08s，去掉头尾气口 ≈ 61 字，留点余量。
NATIVE_BUDGET = 56.0     # 超过这个字量的台词才拆镜
NATIVE_MERGE_CAP = 58.0  # 合并碎段时的硬上限（再高就会超过 362 帧被截断）
# 实测出片速度：RTX 3060 · 1152x640 · 8 步 · Q3_K，124 帧用了 18 分 10 秒。
# 界面上的"还要多久"全按这个算 —— 别再写"镜数×1.5分钟"那种差十倍的估法。
PER_FRAME_SEC = 8.8


def _speech_units(txt):
    """把台词换算成"口语字数"：汉字 1、标点 0.4、字母数字 0.55。"""
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


def _split_say(text, budget=NATIVE_BUDGET):
    """把一段台词切成若干"一镜念得完"的段。

    切法：先按句末标点（。！？；…）切 → 单句还超长就按句中标点（，、：）切
    → 仍然超长就按字数硬切。最后贪心把小句合并，尽量把每镜塞满，
    避免切出一堆 0.5 秒的碎镜。
    """
    t = (text or "").strip()
    if not t or _speech_units(t) <= budget:
        return [t] if t else [""]
    pieces = []
    for sent in re.findall(r"[^。！？；…!?;]+[。！？；…!?;]?", t):
        sent = sent.strip()
        if not sent:
            continue
        if _speech_units(sent) <= budget:
            pieces.append(sent)
            continue
        for sub in re.findall(r"[^，、,：:]+[，、,：:]?", sent):
            sub = sub.strip()
            if not sub:
                continue
            if _speech_units(sub) <= budget:
                pieces.append(sub)
                continue
            buf = ""
            for ch in sub:                     # 硬切（一句没有标点的长词）
                if buf and _speech_units(buf + ch) > budget:
                    pieces.append(buf)
                    buf = ch
                else:
                    buf += ch
            if buf:
                pieces.append(buf)
    out, cur = [], ""
    for pc in pieces:
        if not cur:
            cur = pc
        elif _speech_units(cur + pc) <= budget:
            cur += pc
        else:
            out.append(cur)
            cur = pc
    if cur:
        out.append(cur)
    # 收尾：把过碎的段（不到 1 秒）并进相邻段，别切出 0.5 秒的闪镜。
    # 合并后仍卡在 19.6 字以内 —— 超过就是超过 124 帧，又会被截断。
    merged = []
    for seg in out:
        if (merged and (_speech_units(seg) < 4.0 or _speech_units(merged[-1]) < 4.0)
                and _speech_units(merged[-1] + seg) <= NATIVE_MERGE_CAP):
            merged[-1] += seg
        else:
            merged.append(seg)
    return merged or [t]


def _expand_native_shots(items):
    """A 方案生成前：把超长台词拆成多镜。返回 (新分镜列表, 拆镜说明)。"""
    out, notes = [], []
    for it in items:
        parts = _split_say(it.get("line") or "")
        if len(parts) <= 1:
            out.append(it)
            continue
        base = len(out) + 1                    # 主镜在最终列表里的 1-based 序号
        for j, seg in enumerate(parts):
            n = dict(it)
            n["line"] = seg
            if j:
                n["key_from"] = base           # 续段：复用主镜关键帧
            out.append(n)
        notes.append("第%d镜「%s」台词 %.1f 秒 → 拆成 %d 段"
                     % (base, (it.get("who") or "旁白"),
                        _speech_units(it.get("line")) / NATIVE_CPS, len(parts)))
    return out, notes


def collect_shots():
    out = []
    _prev_loc = ""          # 上一镜的场景，用来判断"还在同一个地方"
    for r in shot_rows:
        scene = r["scene"].get("1.0", "end").strip()
        motion = r["motion_var"].get().strip()
        say = r["say_var"].get().strip()
        dur = r["dur_var"].get().strip()
        sfx = r["sfx_var"].get().strip()
        ref = r["ref_var"].get().strip()
        dn = r["dn_var"].get().strip()
        size = r["size_var"].get().strip()
        direct = (r["direct_var"].get() if r.get("direct_var") else "").strip()
        # 过滤占位提示灰字
        if motion == PH_MOTION:
            motion = ""
        if say == PH_SAY:
            say = ""
        if dur == PH_DUR:
            dur = ""
        if sfx == PH_SFX:
            sfx = ""
        if dn == PH_DN:
            dn = ""            # 留空＝按默认 0.62 处理，避免把占位文字当数值
        # 场景：优先用用户手动指定的，否则按① 画面描述自动识别；
        # 本镜没写地点时，看是不是延续上一镜（同一场戏的后几镜常不再重复写地点）。
        loc = (r.get("loc_var").get() or "").strip() if r.get("loc_var") else ""
        if not loc:
            loc = guess_scene_with_prev(scene, _prev_loc)
        if loc:
            _prev_loc = loc
        if scene or say:
            out.append({"scene": scene, "motion": motion, "say": say,
                        "dur": dur, "sfx": sfx, "ref": ref, "dn": dn,
                        "size": size, "loc": loc, "direct": direct})
    return out


# ====================== 自动存档（断电 / 误关都不白干） ======================
# 分镜表原来只在内存里：关窗口、断电、崩溃，整张表连同剧名/画风/音色一起没了
# （用户：「停电了那不就是白干了吗」）。这里每 3 秒把当前表抓成 JSON 落盘并 fsync，
# 下次启动自动接上；渲染进度本来就按镜记在 output/{prefix}_done.json 里，
# 所以恢复之后「接着渲」不会重做已经渲完的镜。
_AS_FILE = os.path.join(OUTDIR, ".autosave.json")
_AS_STATE = {"sig": None, "t": 0.0}
# 这些参数直接影响出片（画风、音色、画质、帧数、A/B 方案），一起存
_AS_VARS_STR = (("title", "title_var"), ("style", "style_var"),
                ("voice", "voice_var"), ("rate", "voice_rate_var"),
                ("length", "length_var"), ("speed", "speed_var"),
                ("aspect", "aspect_var"), ("model", "model_var"))
_AS_VARS_BOOL = (("turbo", "turbo_var"), ("auto_len", "auto_len_var"),
                 ("native", "native_var"), ("te4b", "te4b_var"),
                 ("auto_prompt", "auto_prompt_var"))
_AS_FIELDS = ("say_var", "motion_var", "dur_var", "sfx_var", "ref_var",
              "dn_var", "size_var", "loc_var", "direct_var")
_AS_PLACEHOLDER = (("motion", PH_MOTION), ("say", PH_SAY), ("dur", PH_DUR),
                   ("sfx", PH_SFX), ("dn", PH_DN))


def _shots_snapshot():
    """把分镜表抓成纯数据：Text 控件和 StringVar 都不能直接 json。"""
    out = []
    for r in list(shot_rows):
        try:
            d = {"scene": r["scene"].get("1.0", "end").strip()}
        except Exception:
            d = {"scene": ""}
        for k in _AS_FIELDS:
            v = r.get(k)
            try:
                d[k[:-4]] = (v.get() if v is not None else "") or ""
            except Exception:
                d[k[:-4]] = ""
        for k, ph in _AS_PLACEHOLDER:     # 占位灰字不是内容，存进去下次会被当真数据
            if (d.get(k) or "").strip() == ph:
                d[k] = ""
        for mk, dk in (("motion_manual", "m_manual"),
                       ("size_manual", "s_manual"),
                       ("loc_manual", "l_manual")):
            try:
                d[dk] = bool(r[mk][0])
            except Exception:
                pass
        # 没画面也没台词的镜不存（和 collect_shots 同一个口径）：
        # 程序启动时本来就会留一行空白待填，自动补参考角色/时长也会落在它身上，
        # 存进去下次就变成"第 1 镜是空的"。
        if (d.get("scene") or "").strip() or (d.get("say") or "").strip():
            out.append(d)
    return out


def _autosave_payload():
    pay = {"v": 1, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
           "shots": _shots_snapshot()}
    for key, vname in _AS_VARS_STR:
        var = globals().get(vname)
        try:
            if var is not None:
                pay[key] = var.get()
        except Exception:
            pass
    for key, vname in _AS_VARS_BOOL:
        var = globals().get(vname)
        try:
            if var is not None:
                pay[key] = bool(var.get())
        except Exception:
            pass
    try:
        pay["title_auto"] = _TITLE_AUTO[0]
    except Exception:
        pass
    return pay


def _autosave_now(force=False):
    """把当前状态写进 .autosave.json（原子写：tmp → replace）。"""
    import json as _j
    try:
        pay = _autosave_payload()
    except Exception:
        return
    shots = pay.get("shots") or []
    # 空表 + 空剧名 = 刚启动还没恢复成功，不能拿它盖掉上一次的好存档
    if not shots and not (pay.get("title") or "").strip():
        return
    sig = _j.dumps(pay, ensure_ascii=False, sort_keys=True)
    if not force and sig == _AS_STATE["sig"]:
        return
    tmp = _AS_FILE + ".tmp"
    try:
        os.makedirs(OUTDIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            _j.dump(pay, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())     # 只 write 不 fsync 的话，断电时可能还在系统缓存里
        if os.path.exists(_AS_FILE):
            try:
                shutil.copy2(_AS_FILE, _AS_FILE.replace(".json", ".bak.json"))
            except Exception:
                pass
        os.replace(tmp, _AS_FILE)     # 原子替换：断电也不会留下半截 JSON
        _AS_STATE["sig"] = sig
        _AS_STATE["t"] = time.time()
    except Exception as e:
        try:
            log_q.put("[存档] 写入失败：%s\n" % e)
        except Exception:
            pass


def _autosave_tick():
    """每 3 秒对一次指纹，变了才落盘（改一个字不会写个没完）。"""
    try:
        _autosave_now()
    except Exception:
        pass
    try:
        root.after(3000, _autosave_tick)
    except Exception:
        pass


def _autosave_restore():
    """启动时把上次的表接回来，返回恢复的镜数。"""
    import json as _j
    pay = None
    for path in (_AS_FILE, _AS_FILE.replace(".json", ".bak.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = _j.load(f)
            if isinstance(d, dict) and isinstance(d.get("shots"), list):
                pay = d
                break
        except Exception:
            continue
    if not pay:
        return 0

    # 参数先还原：建镜时 auto_len / native 会影响③ 时长，顺序反了算出来的值不一样
    for key, vname in _AS_VARS_STR:
        var = globals().get(vname)
        if var is None or key not in pay:
            continue
        try:
            if key == "speed" and pay[key] not in _SPEED_KEYS:
                continue
            var.set(pay[key])
        except Exception:
            pass
    for key, vname in _AS_VARS_BOOL:
        var = globals().get(vname)
        if var is None or key not in pay:
            continue
        try:
            var.set(bool(pay[key]))
        except Exception:
            pass
    try:
        _TITLE_AUTO[0] = pay.get("title_auto") or ""
    except Exception:
        pass

    shots = pay.get("shots") or []
    if shots:
        _wipe_shots()
        for d in shots:
            try:
                i = add_shot(scene=d.get("scene", ""), say=d.get("say", ""),
                             motion=d.get("motion", ""), dur=d.get("dur", ""),
                             sfx=d.get("sfx", ""), ref=d.get("ref", ""),
                             dn=d.get("dn", ""), size=d.get("size", ""),
                             loc=d.get("loc", ""), defer=True)
                rec = shot_rows[i]
                try:
                    rec["direct_var"].set(d.get("direct", "") or "")
                except Exception:
                    pass
                # 手动标记要按存档还原：自动识别的景别不能因为"有值"就被锁成手动
                for mk, dk in (("motion_manual", "m_manual"),
                               ("size_manual", "s_manual"),
                               ("loc_manual", "l_manual")):
                    if dk in d:
                        rec[mk][0] = bool(d[dk])
            except Exception:
                pass
        _shots_refresh()
        try:
            _refresh_all_locs()      # 场景名是"延续上一镜"算出来的，批量建完要重认一遍
        except Exception:
            pass

    _AS_STATE["sig"] = None
    try:
        if shots:
            log_q.put("[存档] 已恢复上次编辑的 %d 镜（存档时间 %s）\n"
                      % (len(shots), pay.get("ts") or "?"))
            set_status("已恢复上次编辑的 %d 镜" % len(shots), "ok")
        else:
            log_q.put("[存档] 上次的表是空的（存档时间 %s）\n"
                      % (pay.get("ts") or "?"))
    except Exception:
        pass
    return len(shots)


# ============================ 导入剧本表格 ============================
def parse_table(text):
    """把「镜号/时长/画面描述/台词·旁白/音效·备注[/镜头描述]」表格解析成分镜列表。
    第 6 列（可选）是「镜头描述(运动)」，导入后会填进每镜的「⑤ 运镜动作」，
    避免所有镜退化成默认运动。支持制表符(Tab，从 Excel/表格复制)或逗号分隔；
    台词可跨行（自动续接）。自动忽略表头行（首列不是数字的行）。
    """
    text = (text or "").lstrip("\ufeff")   # 去掉 Excel/记事本存的 BOM，否则第1镜会被当表头丢掉
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    sep = "\t" if "\t" in lines[0] else ","
    shots, cur = [], None

    def norm(raw):
        return [p.strip() for p in raw.split(sep)]

    def flush():
        nonlocal cur
        if cur is not None and (cur.get("scene") or cur.get("line")):
            shots.append(cur)
        cur = None

    for raw in lines:
        parts = norm(raw)
        is_num = bool(parts) and parts[0].isdigit()
        if is_num and len(parts) >= 2:
            flush()
            # 列：镜号/时长/画面描述/台词/音效/镜头描述(可选)/参考角色(可选)
            cur = {"dur": parts[1],
                   "scene": parts[2] if len(parts) >= 3 else "",
                   "line": parts[3] if len(parts) >= 4 else "",
                   "sfx": parts[4] if len(parts) >= 5 else "",
                   "motion": parts[5] if len(parts) >= 6 else "",
                   "ref": parts[6] if len(parts) >= 7 else "",
                   "dn": parts[7] if len(parts) >= 8 else "",
                   "size": parts[8] if len(parts) >= 9 else ""}
            if len(parts) == 4:
                # 台词换行、音效被推到下一行
                cur["_need_sfx"] = True
        else:
            if cur is None:
                continue  # 表头/无关行
            if cur.get("_need_sfx"):
                p = norm(raw)
                if p and p[0]:
                    cur["line"] = (cur["line"] + " " + p[0]).strip()
                if len(p) >= 2:
                    cur["sfx"] = p[1]
                if len(p) >= 3:
                    cur["motion"] = p[2]
                cur["_need_sfx"] = False
            else:
                p = norm(raw)
                if p:
                    cur["line"] = (cur["line"] + " " + " ".join(p)).strip()
    if cur is not None:
        flush()

    for s in shots:
        s.pop("_need_sfx", None)
        s["scene"] = " ".join((s.get("scene") or "").split())
        s["line"] = " ".join((s.get("line") or "").split())
        s["dur"] = (s.get("dur") or "").strip()
        s["sfx"] = (s.get("sfx") or "").strip()
        s["motion"] = " ".join((s.get("motion") or "").split())
        s["ref"] = " ".join((s.get("ref") or "").split())
        if s["ref"] in ("无", "—", "-", "/", "N/A", "n/a", "none", "None"):
            s["ref"] = ""
        # 第 8 列：锚定强度（大特写给 0.45 保脸，全景给 0.7 保构图）
        # 第 9 列：景别（远景/全景/中景/近景/特写/局部特写）
        s["size"] = (s.get("size") or "").strip()
        if s["size"] not in SIZE_TEXT:
            s["size"] = ""
        s["dn"] = (s.get("dn") or "").strip()
        try:
            _d = float(s["dn"])
            s["dn"] = str(max(0.20, min(0.95, _d))) if s["dn"] else ""
        except Exception:
            s["dn"] = ""
        # 台词写「无 / — / N/A」是不念的，否则会被 TTS 照念出来
        if s["line"] in ("无", "—", "-", "/", "N/A", "n/a", "none", "None"):
            s["line"] = ""
    return shots


# ============== 自然语言剧本解析（表格之外的第二套格式） ==============
_SCRIPT_NUM = r"(?:[0-9０-９]+|[一二三四五六七八九十百零]+)"

# 标签词 → 分镜字段。sub＝字幕（只留档、不念，免得跟台词重复）
_SCRIPT_TAG = {
    "画面": "scene", "画面描述": "scene", "画面内容": "scene", "镜头": "scene",
    "场景": "scene", "场景描述": "scene",
    "台词": "line", "对白": "line", "旁白": "line", "独白": "line",
    "画外音": "line", "人物对白": "line",
    "字幕": "sub",
    "音效": "sfx", "声音": "sfx", "环境音": "sfx", "音效备注": "sfx",
    "备注": "sfx", "动作": "sfx", "动作描述": "sfx", "神态": "sfx",
    "运镜": "motion", "运镜动作": "motion", "镜头运动": "motion",
    "镜头描述": "motion", "运动": "motion", "镜头语言": "motion",
    "时长": "dur", "时间": "dur", "秒数": "dur",
    "角色": "ref", "参考角色": "ref", "出场角色": "ref",
    "景别": "size", "镜头景别": "size", "画面景别": "size",
}

# 镜号行：1. **第一镜：雨店来客** / 第一镜：… / 【第1镜】/ 镜3：…
_SCRIPT_HEAD_RE = re.compile(
    r"^(?:第\s*(?P<a>" + _SCRIPT_NUM + r")\s*[镜場场幕]"
    r"|(?P<b>" + _SCRIPT_NUM + r")\s*[.、,，::：]?\s*第?\s*(?P<c>" + _SCRIPT_NUM + r")?\s*[镜場场幕]"
    r"|[镜場场幕]\s*(?P<d>" + _SCRIPT_NUM + r"))")
_SCRIPT_TAG_RE = re.compile(r"^\s*[【\[（(《]\s*([^】\])）》\s]{1,8})\s*[】\])）》]\s*[:：]?\s*(.*)$")
_SCRIPT_KV_RE = re.compile(r"^\s*([一-龥]{2,8})\s*[:：]\s*(.*)$")
_SCRIPT_SAY_RE = re.compile(
    r"^\s*([^：:（(【\[]{1,12})\s*[（(]\s*([^）)]{0,60})\s*[）)]\s*[:：]\s*(.+)$")
_SCRIPT_PAREN_RE = re.compile(r"^\s*[（(【\[][^）)】\]]*[）)】\]]\s*$")


def _script_norm(s):
    """去掉标点空白只留字，用来比较两段文字是不是同一句。"""
    return re.sub(r"[\s，。！？、：；…—\-·「」『』“”\"'（）()【】\[\]!,.?;:~*#]+",
                  "", s or "")


def _script_head(line):
    """判断这行是不是镜号行；是则返回 (镜号, 标题)，否则 None。"""
    s = re.sub(r"[#*_~=`>\-]", " ", line or "").strip()
    s = s.strip("【】[]（）() 　\t")
    m = _SCRIPT_HEAD_RE.match(s)
    if not m:
        return None
    no = next((g for g in m.groups() if g), "")
    rest = s[m.end():].strip().strip(":： ")
    title = ""
    for sep in ("：", ":"):
        if sep in rest:
            title = rest.split(sep, 1)[1]
            break
    else:
        title = rest
    return no, title.strip(" *_#~")


_SCRIPT_HEAD_LOOSE_RE = re.compile(r"^(\d{1,3})\s*[.、,，:：)）]\s*(\S.*)$")


def _script_head_loose(line):
    """宽松镜号行："1. 内容" / "1、内容" / "1) 内容"。

    【为什么需要】严格规则的镜号行**必须带"镜"字**（"1. 第1镜：…"）。
    可很多人写剧本就是"1. 女医师跪在雪地里…"这种，于是整份剧本解析出 0 镜，
    而且一声不吭 —— 用户只会以为程序坏了。只有在严格规则一镜都认不出时
    才降级到这条，避免把剧情里的"1. "列表项误当分镜。
    """
    s2 = re.sub(r"[#*_~=`>\-]", " ", line or "").strip()
    m = _SCRIPT_HEAD_LOOSE_RE.match(s2)
    if not m:
        return None
    return m.group(1), m.group(2).strip()


def parse_script(text):
    """把自然语言剧本解析成分镜列表（非表格格式）。

    认得这些写法：
      1. 镜号行：1. **第一镜：雨店来客** / 第一镜：… / 【第1镜】/ 镜3：…
      2. 标签行：【画面】… 【台词】… 【字幕】… 【音效】… 【运镜】… 【时长】8
               【角色】林砚 【景别】特写
      3. 对白行：周嵩：小二！两斤熟牛肉…
                 林砚（头也不抬，声音压得很低）：客官稍候，这就来。  ← 括号神态→备注
      4. 动作行：（林砚端着酒肉走近…）                            ← 整行括号→并进画面
    """
    text = (text or "").lstrip("\ufeff")
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    shots, cur, pending = [], None, None
    # 严格镜号行一个都认不出 → 自动降级到宽松规则，而不是交白卷
    _head_fn = _script_head
    try:
        if not any(_script_head(ln) is not None for ln in lines):
            if sum(1 for ln in lines if _script_head_loose(ln) is not None) >= 2:
                _head_fn = _script_head_loose
    except Exception:
        pass

    def _join(key, val, sep=" "):
        old = (cur.get(key) or "").strip()
        cur[key] = (old + sep + val).strip() if old else val

    def flush():
        nonlocal cur, pending
        pending = None
        if cur is None:
            return
        sub = (cur.pop("_sub", "") or "").strip()
        if sub:
            a, b = _script_norm(sub), _script_norm(cur.get("line") or "")
            if not (a and (a in b or b in a)):
                _join("sfx", sub)       # 字幕只留档，不进台词（免得跟台词重复念）
        if not (cur.get("scene") or "").strip():
            t = (cur.get("_title") or "").strip()
            if t:
                cur["scene"] = t
        if (cur.get("scene") or cur.get("line") or cur.get("sfx")):
            shots.append(cur)
        cur = None

    for raw in lines:
        head = _head_fn(raw)
        if head is not None:
            flush()
            cur = {"dur": "", "scene": "", "line": "", "sfx": "", "motion": "",
                   "ref": "", "dn": "", "size": "", "_title": head[1]}
            continue
        if cur is None:
            continue          # 镜号行之前的内容（说明、文档标题）忽略
        # --- 标签行：【画面】xxx / 画面：xxx ---
        tag = rest = None
        m = _SCRIPT_TAG_RE.match(raw)
        if m:
            tag, rest = m.group(1).strip(), m.group(2).strip()
        else:
            m = _SCRIPT_KV_RE.match(raw)
            if m and m.group(1).strip() in _SCRIPT_TAG:
                tag, rest = m.group(1).strip(), m.group(2).strip()
        if tag:
            key = _SCRIPT_TAG.get(tag)
            if key == "sub":
                _join("_sub", rest)
            elif key:
                if rest:
                    _join(key, rest)
                else:
                    pending = key     # 标签单独一行 → 下一行算它的内容
            continue
        if pending:
            _join(pending, raw.strip())
            pending = None
            continue
        # --- 对白：角色：台词（可能带括号神态）---
        m = _SCRIPT_SAY_RE.match(raw)
        if m:
            who, note = m.group(1).strip(), m.group(2).strip()
            _join("line", "%s：%s" % (who, m.group(3).strip()), sep="\n")
            if note:
                _join("sfx", note)
            continue
        who, say = _split_who(raw)
        if who:
            _join("line", "%s：%s" % (who, say), sep="\n")
            continue
        # --- 整行括号 = 动作描写 → 并进画面描述（模型能画出来）---
        if _SCRIPT_PAREN_RE.match(raw):
            _join("scene", raw.strip())
            continue
        # --- 其它：没画面当画面，有画面当台词续行 ---
        if not (cur.get("scene") or "").strip():
            _join("scene", raw.strip())
        else:
            _join("line", raw.strip())
    flush()

    for s in shots:
        s["scene"] = " ".join((s.get("scene") or "").split())
        s["line"] = "\n".join(" ".join(x.split())
                              for x in (s.get("line") or "").splitlines() if x.strip())
        s["dur"] = (s.get("dur") or "").strip()
        s["sfx"] = " ".join((s.get("sfx") or "").split())
        s["motion"] = " ".join((s.get("motion") or "").split())
        s["ref"] = " ".join((s.get("ref") or "").split())
        if s["ref"] in ("无", "—", "-", "/", "N/A", "n/a", "none", "None"):
            s["ref"] = ""
        s["size"] = (s.get("size") or "").strip()
        if s["size"] not in SIZE_TEXT:
            s["size"] = ""
        s["dn"] = (s.get("dn") or "").strip()
        try:
            _d = float(s["dn"])
            s["dn"] = str(max(0.20, min(0.95, _d))) if s["dn"] else ""
        except Exception:
            s["dn"] = ""
        if s["line"] in ("无", "—", "-", "/", "N/A", "n/a", "none", "None"):
            s["line"] = ""
    return shots


_TITLE_META = re.compile(
    r"[_\-\s·]*(对白版|旁白版|配音版|锚定版|定稿|终版|无旁白|试跑|v\d+|\d+镜|第?\d+集)$")


def _clean_title(t):
    """把「鞋_9镜_对白版.tsv」这种文件名收拾成能上屏的剧名「鞋」。

    用户反馈过：从命令行/文件载入时，剧名被自动改成文件名，一串下划线数字很难看。
    """
    s = os.path.splitext(os.path.basename(t or ""))[0]
    for _ in range(5):
        s2 = _TITLE_META.sub("", s)
        if s2 == s:
            break
        s = s2
    return s.strip(" _-·") or s


_TITLE_AUTO = [""]        # 上一次「自动起」的剧名，用来判断当前剧名是不是用户自己填的

# 「载入示例」那颗香蕉的剧名。只是示例的标题，不再是空白启动时的默认值
# （用户反馈：新剧导入后这里还挂着「第一颗香蕉是怎么来的」，看着像没换剧）
SAMPLE_TITLE = "第一颗香蕉是怎么来的"

# 一看就不是剧名的文件名：这类名字不配当剧名，改按首镜画面起
_TITLE_JUNK = ("剧本", "分镜", "新剧本", "未命名", "新建", "表格", "sheet", "table",
               "story", "script", "draft", "untitled", "副本")


def _clean_title(t):
    """把「鞋_9镜_对白版.tsv」这种文件名收拾成能上屏的剧名「鞋」。

    用户反馈过：从命令行/文件载入时，剧名被自动改成文件名，一串下划线数字很难看。
    """
    s = os.path.splitext(os.path.basename(t or ""))[0]
    for _ in range(5):
        s2 = _TITLE_META.sub("", s)
        if s2 == s:
            break
        s = s2
    return s.strip(" _-·") or s


def _suggest_title(parsed):
    """根据内容自动起一个剧名（去【镜头】前缀，取第一镜场景首句）。"""
    import re as _re
    if not parsed:
        return ""
    # 剧本格式：镜号行自带的标题（如「1. 第一镜：雨店来客」）就是最准的剧名
    t = (parsed[0].get("_title") or "").strip()
    if t:
        return t[:16]
    scene = (parsed[0].get("scene") or "").strip()
    s = _re.sub(r"^【[^】]*】", "", scene).strip()
    if s:
        s = _re.split(r"[，。,.！!？?；;]", s)[0].strip()
    if not s:
        line = (parsed[0].get("line") or "").strip()
        s = _re.sub(r"^[^：:]{0,6}[：:]", "", line).strip()
        s = _re.split(r"[，。,.！!？?；;]", s)[0].strip()
    if not s:
        s = "我的漫剧"
    return s[:16]


def _auto_title(path, parsed, typed=""):
    """自动起剧名：用户填了就用用户的 → 否则用文件名 → 文件名太水就按首镜画面起。

    优先级写死在这里，导入（对话框 / 命令行）都走它，免得两处逻辑各说各话。
    """
    typed = (typed or "").strip()
    if typed:
        return typed
    t = _clean_title(path or "")
    if t and t != SAMPLE_TITLE and not any(j == t.lower() or j in t.lower()
                                          for j in _TITLE_JUNK):
        return t[:20]
    return _suggest_title(parsed)


def open_import():
    w = tk.Toplevel(root)
    TH.paint_toplevel(w)
    w.title("导入剧本（表格 / 文本）")
    center_win(w, 760, 560, 680, 480)
    w.transient(root)
    # 格式说明原来铺在对话框最上面，十几行把粘贴框挤到下面 ——
    # 用户反馈「这里的字能不能不要」。收进右上角「ⓘ 格式说明」，要看再点开。
    _IMP_HELP = ("① 先在「剧名」填好这部漫剧的名字（导入后会写进顶部『基本设置』的剧名）。\n"
              "② 把剧本表整段粘贴到下面（列顺序：镜号 / 时长 / 画面描述 / 台词·旁白 / 音效·备注 / 镜头描述(可选) / 参考角色(必填) / 锚定强度(可选) / 景别(可选)），\n"
              "　　第 6 列「镜头描述」填每镜的运动/运镜，导入后会写进每镜『⑤ 运镜动作』，避免全镜退化成默认运动；\n"
              "　　第 7 列「参考角色」填角色名（如 苏云舟），导入后写进每镜『⑥ 参考角色』——这一列是必选项，\n"
              "　　该镜关键帧就会按这个角色的定妆照重绘，跨镜锁住同一张脸（定妆照在顶部『🎭 角色』里管理）。\n"
              "　　角色库只有一个角色时，留空的镜头会自动挂上它，不用一镜一镜点；\n"
              "　　第 9 列「景别」填 远景/全景/中景/近景/特写/局部特写，导入后写进每镜『⑦ 景别』。\n"
              "　　或点「打开文件」选 .txt/.csv/.tsv；支持从 Excel、表格直接复制，台词跨行也能自动续接。\n"
              "　　⚠ 第 4 列尽量写「角色：台词」的形式（如 阿棠：谁？）——观众要看的是人物对白，\n"
              "　　   整段用「旁白：…」讲故事会显得平、像念课文；旁白只用来交代时间地点，一两句就够。\n"
              "③ 点「解析并替换分镜」，会替换掉当前所有分镜。\n"
              "── 或者直接贴「自然语言剧本」也行（程序会自动认）──\n"
              "　　1. **第一镜：雨店来客**\n"
              "　　【画面】夜雨滂沱，荒村野店只亮着一盏油灯。\n"
              "　　周嵩：小二！两斤熟牛肉，一坛烧刀子！\n"
              "　　林砚（头也不抬，声音压得很低）：客官稍候，这就来。\n"
              "　　（林砚端着酒肉走近，领口滑落半块双鱼玉佩。）\n"
              "　　· 镜号行认这些写法：1. 第一镜 / 第一镜 / 【第1镜】/ 镜3\n"
              "　　· 认这些标签：【画面】【台词】【字幕】【音效】【运镜】【时长】【角色】【景别】\n"
              "　　· 「角色：台词」自动进②台词；「（…）」整行并进①画面描述；括号里的神态进④备注。")

    def _show_fmt_help():
        hw = tk.Toplevel(w)
        TH.paint_toplevel(hw)
        hw.title("剧本格式说明")
        center_win(hw, 760, 540, 620, 420)
        hw.transient(w)
        _box = scrolledtext.ScrolledText(hw, wrap="word",
                                         font=("Microsoft YaHei", 9))
        _box.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        _box.insert("1.0", _IMP_HELP)
        _box.configure(state="disabled")
        TH.RoundButton(hw, text="知道了", command=hw.destroy,
                       height=30).pack(pady=(0, 10))


    # 剧名（会写入基本设置）
    _imp_path = [""]                 # 上一次「打开文件」选的路径，用来按文件名起剧名
    title_row = ttk.Frame(w)
    title_row.grid(row=0, column=0, columnspan=3, sticky="ew",
                   padx=10, pady=(10, 6))
    ttk.Label(title_row, text="剧名：", font=FONT_B).pack(side="left")
    # 不预填上一部的剧名：预填会让新剧本导入后「基本设置」还是旧剧名（换了剧却没换名）
    title_imp = tk.StringVar(value="")
    ttk.Entry(title_row, textvariable=title_imp, width=34, font=FONT).pack(
        side="left", padx=(4, 8))
    ttk.Label(title_row, text="（留空按文件名起名）",
              foreground=TH.TEXT3, font=TH.FONT_S).pack(side="left")
    TH.RoundButton(title_row, text="ⓘ 格式说明", command=_show_fmt_help,
                   height=26, padx=8, kind="quiet",
                   font=(TH.FONT[0], 8)).pack(side="right")

    txt = scrolledtext.ScrolledText(w, height=16, font=("Microsoft YaHei", 10),
                                    wrap="word")
    txt.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=10, pady=4)
    w.rowconfigure(1, weight=1)      # 少了那段说明，让粘贴框把多出来的高度吃掉

    def pick_file():
        p = filedialog.askopenfilename(
            title="选择剧本文件",
            filetypes=[("文本文件", "*.txt *.csv *.tsv"), ("所有文件", "*.*")])
        if p:
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    data = f.read()
            except Exception as e:
                messagebox.showerror("读取失败", str(e)); return
            txt.delete("1.0", "end")
            txt.insert("1.0", data)
            _imp_path[0] = p
            # 剧名框还空着 → 拿文件名当剧名（雪夜叩门_8镜.tsv → 雪夜叩门，比按首镜瞎猜准）
            if not title_imp.get().strip():
                title_imp.set(_clean_title(p))

    TH.RoundButton(w, text="▸ 打开文件 (.txt/.csv/.tsv)",
               command=pick_file, height=32).grid(row=2, column=0, sticky="w", padx=10, pady=(6, 0))

    btns = ttk.Frame(w)
    btns.grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 10))

    def do_parse():
        raw = txt.get("1.0", "end").strip()
        if not raw:
            messagebox.showwarning("内容为空", "请先粘贴剧本表，或点「打开文件」选一个文件。")
            return
        parsed = parse_table(raw)
        fmt = "表格"
        if not parsed:
            # 表格认不出来就按自然语言剧本再试一遍（1. **第一镜：xxx** 那种写法）
            parsed = parse_script(raw)
            fmt = "剧本"
        if not parsed:
            messagebox.showerror(
                "没识别到分镜",
                "没认出分镜行。程序支持两种格式，任选一种：\n\n"
                "① 表格：每行「镜号(数字)」开头，后面用 Tab 或逗号分隔\n"
                "　　镜号 / 时长 / 画面 / 台词 / 音效\n\n"
                "② 剧本：以「数字.」开头的分镜行 + 对白行\n"
                "　　1. 第一镜：雨店来客\n"
                "　　夜雨滂沱，荒村野店只亮着一盏油灯。\n"
                "　　周嵩：小二！两斤熟牛肉！\n"
                "　　林砚（头也不抬）：客官稍候，这就来。\n\n"
                "最少要求：每一镜都要有一行以「数字.」开头（1. / 2. / 3. …），\n"
                "后面写这一镜的画面；台词另起一行写「角色名：台词」。\n"
                "（带「镜」字的写法也支持：第一镜 / 第1镜 / 【第1镜】）")
            return
        # 清空当前全部分镜，再按表格重建
        for r in shot_rows[:]:
            del_shot_rec(r, defer=True)
        for p in parsed:
            add_shot(scene=p.get("scene", ""), say=p.get("line", ""),
                     motion=p.get("motion", ""), dur=p.get("dur", ""),
                     sfx=p.get("sfx", ""), ref=p.get("ref", ""),
                     dn=p.get("dn", ""), size=p.get("size", ""), defer=True)
        _shots_refresh()
        _refresh_all_locs()   # 批量建完行再统一认场景，后面的镜才能延续前面的
        # 剧名写回「基本设置」：填了用填的 → 没填按首镜画面起名 → 绝不再沿用它上一部的名字
        # （原来最后一步回退到 title_var，导新剧本时剧名永远不变，看着就像「基本设置没反应」）
        _old_title = title_var.get().strip()
        t = _auto_title(_imp_path[0], parsed, title_imp.get())
        if t:
            title_var.set(t)
            # 记下「这名字是自动起的」，下次导入才能判断该不该换掉；
            # 用户在对话框里手填的名字不算自动，别覆盖。
            _TITLE_AUTO[0] = t if not (title_imp.get() or "").strip() else ""
            if t != _old_title:
                log_q.put("[剧本] 剧名：%s → %s\n" % (_old_title or "（空）", t))
        try:
            canvas.yview_moveto(0.0)     # 导入后回到第 1 镜，别停在第 10 镜
        except Exception:
            pass
        messagebox.showinfo(
            "导入完成",
            f"已按「{fmt}」格式导入 {len(parsed)} 镜。\n\n"
            f"顶部「基本设置」的剧名：{_old_title or '（空）'}  →  {title_var.get()}\n"
            f"画面/台词/时长/音效/参考角色/景别均已填入。")
        w.destroy()
        # 新剧情 → 自动认角色 → 生成定妆照 → 自动锚定每镜（⑥ 参考角色必选）
        _names = _script_char_names(parsed)
        if _names:
            log_q.put("[角色] 剧本识别到 %d 个角色：%s\n"
                      % (len(_names), "、".join(_names[:10])))
        root.after(240, lambda: _new_char_flow(parsed, _names))
        # 场景这一套同样自动到位：认出场景 → 写文案 → 生成场景照 → 绑到每镜
        root.after(400, lambda: _new_scene_flow())
        # 新剧情 → 自动导演：按整集节奏排景别 / 角度 / 运镜 / 构图
        # （排在场景识别之后：换场判断要用 loc，得等它认完）
        root.after(1500, lambda: _direct_all(silent=True))
        # 勾了「导入后自动出片」：等定妆照/场景照补齐 → 倒数 8 秒 → 自动开跑
        _set_auto_go(bool(_auto_imp.get()))
        root.after(1800, lambda: _auto_go_arm("导入剧本"))

    TH.RoundButton(btns, text="◎ 解析并替换分镜", command=do_parse, height=32).pack(side="left")
    TH.RoundButton(btns, text="关闭", command=w.destroy, height=32).pack(side="left", padx=(8, 0))

    # 「导入后自动出片」＝无人值守：导入 → 自动导演 → 等资产 → 开跑。
    # 资产没齐就开跑会换脸，所以真正开跑要等定妆照/场景照都生成完。
    _auto_imp = tk.BooleanVar(value=_auto_go_enabled())
    ttk.Checkbutton(btns, text="导入后自动出片（无人值守）",
                    variable=_auto_imp).pack(side="left", padx=(16, 0))

    w.columnconfigure(0, weight=1)
    w.rowconfigure(2, weight=1)
    try:
        w.grab_set()
    except Exception:
        pass


# ============================ 角色定妆照（跨镜锁脸） ============================
# 原理：关键帧原本每镜各自文生图，同一个"白衣青年"会变成好几个人。
# 绑一张定妆照后，该镜关键帧改走 img2img（定妆照当起点），五官跨镜保持同一人。
# 参考图只锁得住「脸」，锁不住发型/衣袍/气质——所以每个角色还要有一段固定的外貌文案，
# 定妆照用它生成，之后每一镜的关键帧也把同一段原样贴进去（这就是 4 集教程里的「统一文案」）。
# 参考图 + 统一文案 两件套一起上，人物才算真正锁死。
DEFAULT_LOOK = "古代发型，古装交领衣袍，气质清冷，身形清瘦"

# ---- 新剧情自动起草外貌 ----------------------------------------------------
# 原来所有新角色都拿 DEFAULT_LOOK 同一句话，两个人画出来像同一个人（撞脸），
# 锚定再准也白搭。这里改成：姿态/衣袍/年龄段从剧本画面里抠，抠不到的按角色序号轮换，
# 保证同一部戏里每个角色拿到的文案至少有三项不同。
# 名字用字只是**兜底**信号（前面还有身份词/她他），但兜错了代价很大：
# 定妆照会画成另一个人。实测「陆沉」被当少女、「砚秋」被当男子 —— 因为只做了
# any() 存在判断，而这两个名字各只命中一个字。改成按命中个数计分，并补齐常用字。
_FEMALE_HINTS = ("娘", "棠", "婉", "嫣", "雪", "月", "婵", "姝", "妹", "姑",
                 "姐", "婶", "婆", "氏", "娥", "媛", "妤", "妃", "兰", "秀",
                 "婷", "雅", "萱", "琳", "薇", "蓉", "燕", "娟", "芳", "霞",
                 "梅", "菊", "婧", "妍", "悦", "瑶", "颖", "蕾", "茹", "茜",
                 "晴", "柔", "音", "琴", "画", "绾", "絮", "怡", "欣", "淑",
                 "静", "秋", "春", "桃", "柳", "荷", "莲", "玉", "珠", "钗",
                 "巧", "灵", "儿", "姬", "嬛", "若", "初", "颜", "眉", "黛")
_MALE_HINTS = ("郎", "生", "尘", "沉", "砚", "枭", "爷", "叔", "伯", "兄",
               "弟", "父", "僧", "道", "客", "侠", "川", "山", "屠", "刀",
               "嵩", "锋", "铭", "毅", "刚", "强", "军", "杰", "鹏", "昊",
               "轩", "阳", "虎", "豹", "龙", "霆", "震", "烈", "铮", "锐",
               "磊", "峰", "崇", "渊", "澈", "渊", "牧", "野", "征", "战",
               "戈", "甲", "将", "帅", "臣", "相", "帝", "王", "君", "霸")
_DRESS_WORDS = ("青衫", "白衣", "月白", "红衣", "黑袍", "黑衣", "灰袍", "粗布",
                "锦袍", "劲装", "斗篷", "披风", "长袍", "棉袍", "布氅", "襦裙",
                "罗裙", "铠甲", "斗笠", "蓑衣", "短打", "官服")
_HAIR_WORDS = ("长发", "束发", "发髻", "双丫髻", "木簪", "散发", "发冠", "辫子", "披发")
_AGE_WORDS = ("孩童", "少年", "少女", "青年", "中年", "老者")
_AGE_FULL = {"孩童": "七八岁孩童", "少年": "十七八岁少年", "少女": "十六七岁少女",
             "青年": "二十上下青年", "中年": "四十上下中年", "老者": "六旬老者"}
_FRAME_M = ("清瘦挺拔", "魁梧壮实", "中等身材", "高挑纤长")
_FRAME_F = ("娇小纤细", "清瘦纤长", "中等身材", "匀称挺拔")
_TEMPER_WORDS = ("清冷孤傲", "温和沉静", "爽利明快", "阴沉凶戾", "憨厚质朴")

# 剧本画面里自带的身份词，比名字里的字可靠得多：「母亲秀兰」一看就是女的，
# 光看名字「小满」会猜成男孩（实测把女儿写成了「青年男子」）。
_FEMALE_ROLE = ("母亲", "妈妈", "娘亲", "女儿", "闺女", "妻子", "媳妇", "婆婆",
                "老太太", "老妇", "妇人", "女子", "少女", "姑娘", "奶奶",
                "外婆", "姐姐", "妹妹", "嫂子", "阿姨")
_MALE_ROLE = ("父亲", "爸爸", "爹", "儿子", "丈夫", "男人", "男子", "少年",
              "老头", "老者", "老人", "爷爷", "外公", "哥哥", "弟弟",
              "小伙", "师父", "掌柜")
# 年龄段线索按「越具体越优先」排，先命中先用
_AGE_CUES = (
    ("孩童", ("孩童", "小孩", "幼童", "五六岁", "七八岁")),
    ("少女", ("少女", "姑娘", "十六七", "十七八岁", "十几岁")),
    ("少年", ("少年", "半大")),
    ("青年", ("青年", "年轻", "二十上下", "二十出头", "小伙子")),
    ("中年", ("中年", "四十", "五十上下", "五十岁")),
    ("老者", ("白发", "花白", "斑白", "老妇", "老太太", "老人", "老者",
              "六旬", "七旬", "上了年纪")),
)
_DRESS_F = ("月白色交领襦裙", "藕荷色对襟长衫", "深青色棉布裙", "素灰色粗布衣裙")
_DRESS_M = ("深青色交领长袍", "灰褐色粗布短打", "石青色直裰", "靛蓝色旧棉袍")
# 「双丫髻」只给剧本里明说是小丫头 / 少女的角色：默认池子里放它，二十岁的女主
# 一上来就是两个丫髻，一眼县衙丫鬟（自动匹配时没人能改，错了只能重出）。
_HAIR_F_Y = ("长发挽髻、素色发带", "半挽发、垂在肩前", "长发披肩、鬓边簪一朵小花")
_HAIR_F_KID = "双丫髻、系一根红绳"
_HAIR_M_Y = ("黑发束冠、鬓角利落", "长发半束、额前碎发", "发髻高束、束带垂后")
_HAIR_OLD = ("花白头发盘在脑后、一根旧木簪", "两鬓斑白、须发未修",
             "花白短发、额前有些乱")


def _char_scenes(parsed, name):
    """把这个角色出现过的画面描述捞出来，用来抠衣袍/发型这些词。"""
    out = []
    for p in parsed or []:
        who = ""
        try:
            who, _ = _split_who(p.get("line") or "")
        except Exception:
            who = ""
        if (p.get("ref") or "") == name or who == name:
            out.append(p.get("scene") or "")
    return out


def _guess_female(name, blob):
    """判断角色性别：先看「名字前面那个身份词」，再看句子里的她/他，最后看名字用字。"""
    nm = (name or "").strip()
    f = m = 0
    if nm:
        i = 0
        while True:
            i = blob.find(nm, i)
            if i < 0:
                break
            before = blob[max(0, i - 9):i]
            if any(k in before for k in _FEMALE_ROLE):
                f += 1
            if any(k in before for k in _MALE_ROLE):
                m += 1
            i += len(nm)
    if f != m:
        return f > m
    # 名字没在画面里出现（比如只写「一个白发男人」）：直接数身份词
    gf = sum(blob.count(k) for k in _FEMALE_ROLE)
    gm = sum(blob.count(k) for k in _MALE_ROLE)
    if gf != gm:
        return gf > gm
    ff = blob.count("她")
    mm = blob.count("他")
    if ff != mm:
        return ff > mm
    # 按命中字数计分比较（「砚秋」= 砚(男)+秋(女) 平局 → 落回女性，
    # 这类偏柔的名字在漫剧里八成是女角）；平局仍按老规矩当女性。
    nf = sum(1 for k in _FEMALE_HINTS if k in nm)
    nm_ = sum(1 for k in _MALE_HINTS if k in nm)
    if nf != nm_:
        return nf > nm_
    return True          # 实在看不出来：当女性（女性音色/造型更难画崩，也更好认）


# ---- 年龄档：决定「这个角色该配哪一类嗓子」 --------------------------------
# 只按性别分池是不够的：六十岁的老母亲和二十岁的女儿都进女声池，
# 先排到谁谁就拿到「十八九岁清亮少女声」—— 老太太一开口是少女音，出戏得没法修。
# 所以再按统一外貌文案里的年龄线索分一档，年长的先挑成熟的嗓子。
def _char_age(name):
    """0 童 / 1 少女少年 / 2 青年 / 3 中年 / 4 五十上 / 5 老年。"""
    look = CHAR_DESCS.get((name or "").strip()) or ""
    if any(k in look for k in ("孩童", "七八岁", "小孩", "童子")):
        return 0
    if any(k in look for k in ("六十", "七十", "八十", "老妇", "老者", "老太",
                               "花白", "白发", "苍老")):
        return 5
    if any(k in look for k in ("五十", "中老年", "半百")):
        return 4
    if any(k in look for k in ("四十", "中年", "三十")):
        return 3
    if any(k in look for k in ("少女", "十六七", "十五六", "豆蔻", "及笄")):
        return 1
    if any(k in look for k in ("二十", "青年", "少年", "十七八")):
        return 2
    return 2


# 同一性别里按年龄分「成熟 / 年轻 / 童」三套优先顺序，排在前面的先被挑走。
_VOICE_ORDER_F_MATURE = ("zh-CN-XiaoxiaoNeural", "zh-CN-liaoning-XiaobeiNeural",
                         "zh-CN-shaanxi-XiaoniNeural", "zh-TW-HsiaoChenNeural",
                         "zh-CN-XiaoyiNeural")
_VOICE_ORDER_F_YOUNG = ("zh-CN-XiaoyiNeural", "zh-TW-HsiaoChenNeural",
                        "zh-CN-XiaoxiaoNeural", "zh-CN-liaoning-XiaobeiNeural",
                        "zh-CN-shaanxi-XiaoniNeural")
# 成年男声池里**不能**出现云夏（八九岁童声）：角色一多，最后一个成年男人
# 就会被配上小孩嗓子（实测《雨夜断剑》周嵩十七八岁，拿到了八九岁童声）。
# 池子用完时的正确做法是"借同一个音色、换语速音高档位"，不是换成童声。
_VOICE_ORDER_M_MATURE = ("zh-CN-YunyangNeural", "zh-CN-YunjianNeural",
                         "zh-TW-YunJheNeural", "zh-CN-YunxiNeural")
_VOICE_ORDER_M_YOUNG = ("zh-CN-YunxiNeural", "zh-TW-YunJheNeural",
                        "zh-CN-YunyangNeural", "zh-CN-YunjianNeural")
_VOICE_ORDER_M_CHILD = ("zh-CN-YunxiaNeural", "zh-CN-YunxiNeural",
                        "zh-TW-YunJheNeural", "zh-CN-YunyangNeural",
                        "zh-CN-YunjianNeural")


def _voice_pool_for(name):
    """按性别 + 年龄给出这座角色的音色优先顺序。"""
    female = _char_is_female(name)
    age = _char_age(name)
    if female:
        return _VOICE_ORDER_F_MATURE if age >= 3 else _VOICE_ORDER_F_YOUNG
    if age <= 0:
        return _VOICE_ORDER_M_CHILD
    return _VOICE_ORDER_M_MATURE if age >= 3 else _VOICE_ORDER_M_YOUNG


def _draft_look(name, scenes=None, idx=0):
    """按「名字 + 剧本画面」自动起草一段外貌：性别 / 年龄 / 发型 / 衣袍 / 身形 / 气质。
    目的只有一个——同剧不同角色不能拿到同一句话，否则定妆照会像同一个人。"""
    nm = (name or "").strip()
    blob = " ".join(scenes or [])
    female = _guess_female(nm, blob)

    def pick(words, fallback):
        for w in words:
            if w in blob:
                return w
        return fallback

    age_key = "少女" if female else "青年"
    # 剧本里写的是「女儿/妇人/妻子」这种成年身份，就别默认成十六七的小姑娘
    if female and any(k in blob for k in ("女儿", "妇人", "女子", "母亲",
                                          "妻子", "嫂子", "阿姨", "媳妇")):
        age_key = "青年"
    for _k, _cues in _AGE_CUES:            # 剧本写了年龄就按剧本，没写才按性别兜底
        if any(c in blob for c in _cues):
            age_key = _k
            break
    if age_key == "老者":
        head = "六十上下老妇" if female else "六十上下老者"
    elif age_key == "中年":
        head = "四十上下妇人" if female else "四十上下男子"
    elif age_key in ("孩童", "少年", "少女"):     # 这几个词本身带性别，别再拼「男子/女子」
        head = _AGE_FULL.get(age_key, age_key)
    else:
        head = _AGE_FULL.get(age_key, age_key) + ("女子" if female else "男子")

    # 上了年纪的角色统一给花白头发，否则「六旬老者」配一头黑发，一眼假
    if age_key in ("老者", "中年"):
        hair = pick(_HAIR_WORDS, _HAIR_OLD[idx % len(_HAIR_OLD)])
    elif female and age_key == "少女":
        if any(k in blob for k in ("小丫头", "丫鬟", "丫环", "婢", "双丫", "小丫鬟")):
            hair = pick(_HAIR_WORDS, _HAIR_F_KID)
        else:
            hair = pick(_HAIR_WORDS, _HAIR_F_Y[idx % len(_HAIR_F_Y)])
    elif female:
        # 成年女性别给「双丫髻」——那是小姑娘的发型，配二十岁会一眼出戏
        hair = pick(_HAIR_WORDS, ("长发挽髻、素色发带", "半挽发、垂在肩前",
                                  "长发松松挽在脑后")[idx % 3])
    else:
        hair = pick(_HAIR_WORDS, _HAIR_M_Y[idx % len(_HAIR_M_Y)])
    dress = pick(_DRESS_WORDS, (_DRESS_F if female else _DRESS_M)[idx % 4])
    frame = (_FRAME_F if female else _FRAME_M)[idx % len(_FRAME_F)]
    if age_key in ("老者", "中年"):
        temper = ("温和沉静", "隐忍疲惫", "憨厚质朴")[idx % 3]
    else:
        temper = _TEMPER_WORDS[idx % len(_TEMPER_WORDS)]
    return "%s，%s，身穿%s，身形%s，气质%s" % (head, hair, dress, frame, temper)


# ---- B 方案「一人一音色」----------------------------------------------------
# A 方案（模型原生发声）是每镜独立出片，嗓音由模型当场随机定：实测同一段旁白在
# 第 1/3/8 镜的基频能是 212 / 153 / 181 Hz —— 一部戏三个旁白，用户直接听崩。
# B 方案用 edge-tts，音色是写死的参数，跨镜绝对一致；这里按性别给每个角色分一个，
# 免得全剧共用一把嗓子（男主和少女念成同一个人）。
VOICE_POOL_F = ("zh-CN-XiaoyiNeural", "zh-CN-XiaoxiaoNeural",
                "zh-CN-liaoning-XiaobeiNeural", "zh-CN-shaanxi-XiaoniNeural",
                "zh-TW-HsiaoChenNeural")
VOICE_POOL_M = ("zh-CN-YunxiNeural", "zh-CN-YunyangNeural",
                "zh-CN-YunjianNeural", "zh-CN-YunxiaNeural",
                "zh-TW-YunJheNeural")

# ---- 第二层区分：语速 + 音高 ----------------------------------------------
# 音色池男女各 5 个，一部戏里同性别角色超过 5 个就必然撞音色。撞了也能救：
# 再叠一层写死的「语速 ±、音高 ±」，同一个音色听感立刻分化成两个人。
# tier 0 = 原样；tier 1 = 慢一点沉一点；tier 2 = 快一点亮一点。
_VOICE_TIERS = (
    {"rate": "+0%",  "pitch": "+0Hz"},
    {"rate": "-8%",  "pitch": "-18Hz"},
    {"rate": "+10%", "pitch": "+16Hz"},
    {"rate": "-14%", "pitch": "+10Hz"},
)
_VOICE_TIER_LABEL = ("本音", "低缓", "快亮", "慢亮")


def _voice_profile(name, idx=0):
    """给一个角色算出确定性的嗓音档案：音色 + 语速 + 音高。

    同一个 idx 永远算出同一套（换机器、重开软件都不变）；
    同剧里 idx 不同，就算音色撞车，tier 也不同 —— 听得出是两个人。
    """
    pool = VOICE_POOL_F if _char_is_female(name) else VOICE_POOL_M
    n = max(1, len(pool))
    t = _VOICE_TIERS[(idx // n) % len(_VOICE_TIERS)]
    return {"voice": pool[idx % n], "rate": t["rate"], "pitch": t["pitch"]}


def _voice_for_char_name(name, idx=0):
    return _voice_for_char(name, idx)["voice"]


def _assign_char_voices(names):
    """给整部戏的角色统一排嗓音，保证同一性别里「音色＋档位」不重复。

    5 个音色 × 4 个档位 = 同性别最多 20 个角色互不撞；
    真超过 20 个才回到第 1 个（短剧不会有这种规模）。
    排序规则：年长的先挑（老太太先拿成熟嗓），年轻的放后面拿清亮嗓。
    """
    used, out = set(), {}
    order = sorted(names, key=lambda n: (-_char_age(n), str(n)))
    for n in order:
        pool = _voice_pool_for(n)
        got = None
        for voice in pool:                       # 优先：给自己一把没人用过的新嗓子
            if (voice, 0) not in used:
                got = (voice, 0)
                break
        if not got:                              # 池子用完：借音色 + 换语速/音高档位
            for voice in pool:
                for t_i in range(len(_VOICE_TIERS)):
                    if (voice, t_i) not in used:
                        got = (voice, t_i)
                        break
                if got:
                    break
        voice, t_i = got or (pool[0], 0)
        used.add((voice, t_i))
        t = _VOICE_TIERS[t_i]
        out[n] = {"voice": voice, "rate": t["rate"], "pitch": t["pitch"]}
    return out


def _voice_profile_label(p):
    """界面/日志用的中文描述：云希·低缓（-8% -18Hz）。"""
    rate = (p or {}).get("rate") or "+0%"
    pitch = (p or {}).get("pitch") or "+0Hz"
    tier = "自定义"
    for t, lab in zip(_VOICE_TIERS, _VOICE_TIER_LABEL):
        if t["rate"] == rate and t["pitch"] == pitch:
            tier = lab
            break
    return "%s·%s（%s %s）" % (_edge_label((p or {}).get("voice", "")), tier, rate, pitch)


def _char_is_female(name):
    nm = (name or "").strip()
    look = CHAR_DESCS.get(nm) or ""
    if any(k in nm for k in _FEMALE_HINTS) and not any(k in nm for k in _MALE_HINTS):
        return True
    return any(k in look for k in ("少女", "女子", "姑娘", "妇人", "老妇", "丫鬟"))


def _voice_for_char(name, idx=0):
    """取角色已分配的嗓音档案（音色+语速+音高）；旧代码只需要音色名时用 _voice_for_char_name。"""
    p = CHAR_VOICES.get(name)
    if isinstance(p, dict) and p.get("voice"):
        return dict(p)
    if isinstance(p, str) and p:            # 兼容旧配置：直接存了音色名
        return {"voice": p, "rate": "+0%", "pitch": "+0Hz"}
    return _voice_profile(name, idx)


def _edge_label(edge_name):
    """edge 音色内部名 -> 界面上的中文标签，写进日志方便核对。"""
    for k, v in VOICES.items():
        if v == edge_name:
            return k.split("·")[-1].replace("（推荐）", "")
    return edge_name

# 三视图设定稿：正面/侧面/背面 + 服装细节 + 面部特写，全白背景。
# 教程里用它核对发型/衣袍到底长什么样，比单张正面照更不容易跑偏。
# 只让模型画「正 / 侧 / 背」三个全身视角——脸另外用定妆照本身贴在最左边，
# 这样"左边的脸"和"三视图里的脸"是同一张图，绝不可能是两个人（用户反馈过不一致）。
CHAR_SHEET_TPL = (
    "角色三视图设定稿，同一人物的三个全身视角并排画在同一张图里，从左到右："
    "正面全身站姿、侧面全身站姿、背面全身站姿；"
    "浅米白色纯净背景，无场景、无阴影、无道具、无任何文字与标注；"
    "人物设定：{who}，{look}；"
    "三个视角的五官、发型、身高、衣袍款式与配色完全一致，全身立绘完整入画（含头顶与鞋底），"
    "平视，均匀柔光，高清，服装纹样细节清晰，人物设定集风格"
)


# 脸部/服饰细节板：三视图解决"这个人长什么样"，这张解决"近景怼脸时细节对不对"
# （豆包那套角色设定板的右半边就是这个）。只用来给人校对，不进生成。
CHAR_FACE_TPL = (
    "角色面部细节设定资料板，同一个人物的细节网格图，2 行 4 列共 8 格，格间留白均匀；"
    "上排：正面头像、四分之三侧头像、正侧头像、背面发型；"
    "下排：眼部特写、鼻梁与嘴唇特写、发型与发饰特写、衣领或配饰特写；"
    "纯白背景，无文字、无标注、无边框线，光线均匀柔和；"
    "8 格里的脸型、五官、发型、衣领完全一致；人物设定：{who}，{look}；"
    "高清，皮肤纹理真实，人物设定集风格"
)


def _gen_face_board(who, look, seed=0, timeout=1200):
    """生成一张 8 格脸部/服饰细节板，返回图片路径；失败返回 None（不影响锁脸）。"""
    try:
        if not bool(AP.CFG.get("face_board", True)):
            return None
    except Exception:
        pass
    if not (look or "").strip():
        look = "五官清晰，服饰简洁"
    try:
        import gen_zimage_keys as Z
        old_w, old_h = Z.W, Z.H
        Z.W, Z.H = 1024, 1024
        pfx = "face_%s_%s" % (re.sub(r"[^\w]", "", who)[:12] or "c",
                              time.strftime("%H%M%S"))
        try:
            wf = Z.build_wf(CHAR_FACE_TPL.format(who=who, look=look),
                            990001 + int(seed) * 3571, pfx)
            pid = Z.submit(wf)
            res = Z.wait(pid, pfx, timeout=timeout) if pid else None
        finally:
            Z.W, Z.H = old_w, old_h
        if res and os.path.exists(res):
            return res
    except Exception as e:
        log_q.put("[脸部板] %s 生成失败：%s\n" % (who, e))
    return None


def _trim_blank(im, tol=14, keep=0.004):
    """裁掉三视图四周的纯色空白（AI 出图常留一大圈白边，白白浪费画布）。"""
    try:
        import numpy as np
        a = np.asarray(im.convert("RGB")).astype(int)
        bg = a[2:8, 2:8].reshape(-1, 3).mean(axis=0)
        mask = (np.abs(a - bg).max(axis=2) > tol)
        h, w = mask.shape
        cols = mask.mean(axis=0)
        rows = mask.mean(axis=1)
        xs = np.where(cols > keep)[0]
        ys = np.where(rows > keep)[0]
        if len(xs) and len(ys):
            x0, x1 = max(0, xs[0] - 6), min(w, xs[-1] + 7)
            y0, y1 = max(0, ys[0] - 6), min(h, ys[-1] + 7)
            if (x1 - x0) > w * 0.2 and (y1 - y0) > h * 0.2:
                return im.crop((x0, y0, x1, y1))
    except Exception:
        pass
    return im


def _compose_sheet(sheet_path, ref_path, out_path, who="", face_path=None):
    """把「定妆照 + 三视图 + 脸部细节板」拼成一张角色设定稿。

    为什么不用模型直接画脸：模型每画一次就是一张新脸，跟定妆照对不上。
    贴原图＝零误差，左边那张脸就是后面每镜锚定用的那张脸。
    有脸部板时版面变成「定妆照 | 三视图 | 细节网格」，跟豆包那套设定板一个思路。
    """
    try:
        from PIL import Image, ImageDraw
        sh = Image.open(sheet_path).convert("RGB")
        # 底色取三视图左上角，贴图后不会出现两块不同的白
        bg = sh.getpixel((4, 4))
        face = None
        if face_path and os.path.exists(face_path):
            try:
                face = Image.open(face_path).convert("RGB")
            except Exception:
                face = None
        W, H = (1760, 900) if face is not None else (1600, 800)
        canvas = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(canvas)

        # 左：定妆照（整张，含肩，不裁脸 → 不会切下巴）
        try:
            ref = Image.open(ref_path).convert("RGB")
            ref.thumbnail((520 if face is not None else 620, H - 56), Image.LANCZOS)
            ry = (H - ref.height) // 2
            canvas.paste(ref, (14, ry))
            d.rectangle([14, ry, 14 + ref.width - 1, ry + ref.height - 1],
                        outline=(200, 200, 205), width=2)
            left_x = 14 + ref.width + 20
        except Exception:
            left_x = 20

        # 右：脸部细节板（有就占最右边一列）
        right_x = W - 14
        if face is not None:
            face.thumbnail((420, H - 56), Image.LANCZOS)
            fx = right_x - face.width
            fy = (H - face.height) // 2
            canvas.paste(face, (fx, fy))
            d.rectangle([fx, fy, fx + face.width - 1, fy + face.height - 1],
                        outline=(200, 200, 205), width=2)
            right_x = fx - 20

        # 中：三视图（正/侧/背）——先裁掉四周空白，三个人才显得大
        sh2 = _trim_blank(sh)
        sh2.thumbnail((max(200, right_x - left_x), H - 40), Image.LANCZOS)
        sy = (H - sh2.height) // 2
        canvas.paste(sh2, (left_x + max(0, (right_x - left_x - sh2.width) // 2), sy))
        # 三张图都比画布矮时会剩一大条空白：按实际内容高度裁底，版面才紧凑
        bottom = sy + sh2.height + 14
        try:
            bottom = max(bottom, ry + ref.height + 14)
        except Exception:
            pass
        if face is not None:
            bottom = max(bottom, fy + face.height + 14)
        bottom = min(H, bottom)
        if bottom < H - 40:
            canvas = canvas.crop((0, 0, W, bottom))
        canvas.save(out_path, quality=95)
        return True
    except Exception as e:
        print("[三视图] 合成失败:", e)
        return False
CHAR_DESC_TPL = (
    "电影感写实摄影，中国古代武侠人物定妆照，{who}，{look}，正面半身像，五官清晰，"
    "中性浅灰纯色背景，柔和均匀的正面平光，高清真实皮肤纹理，"
    "85mm镜头，浅景深，高细节，8k"
)


def _lock_text(name):
    """每镜关键帧都要贴的同一段人物文案（锁发型/衣袍/气质）。"""
    look = (CHAR_DESCS.get(name) or "").strip()
    parts = ["同一人物「%s」" % name if name else "同一人物"]
    if look:
        parts.append(look)
    return "，".join(parts)


# ============================ 新剧情 · 自动建场景 ============================
# 角色那条线已经有"新剧情自动定妆照"了（_new_char_flow）：
# 导入剧本 → 认出角色 → 生成定妆照 → 自动锚定每镜。
# 场景这条线要的是同一套体验：导入剧本 → 认出场景 → 自动写场景文案
# → 生成场景照 → 自动绑到每镜。
#
# 为什么必须自动：场景照是"锁地点"的那一套，一个场景要一张照，
# 手动建的话 8 镜的剧本通常有 3~5 个场景，等于每部剧都要额外点十几次。
#
# 场景文案从剧情里抠：把这一场景所有镜头的画面描述合起来，
# 摘出环境元素（夜/雨/油灯/木门…），拼成"这个地方长什么样"。

def _scene_texts_from_shots(scene_name):
    """把这个场景在当前分镜里的所有画面描述合起来（生成场景文案的原料）。"""
    out = []
    for r in shot_rows:
        try:
            t = r["scene"].get("1.0", "end") or ""
        except Exception:
            continue
        if not t.strip():
            continue
        loc = ""
        try:
            loc = (r.get("loc_var").get() or "").strip() if r.get("loc_var") else ""
        except Exception:
            loc = ""
        if not loc:
            loc = guess_scene_with_prev(t, "")
        if loc == scene_name:
            out.append(t)
    return out


# 环境元素词 → 归类。做场景文案时按这个抽，比"随便挑几个字"准得多。
_SCENE_ENV_WORDS = (
    ("时间", ("夜", "子夜", "黄昏", "清晨", "拂晓", "正午", "傍晚", "深夜")),
    ("天气", ("雨", "雪", "雾", "风", "晴", "阴", "雷", "霜", "露")),
    ("光", ("油灯", "烛火", "灯笼", "月光", "日光", "火光", "霞光", "霓虹",
            "灯火", "晨光", "残阳")),
    ("建筑", ("木门", "朱门", "青砖", "石阶", "回廊", "廊柱", "木窗", "窗棂",
              "屋檐", "飞檐", "牌匾", "石狮", "城墙", "门环", "破庙", "佛堂",
              "祠堂", "大殿", "长桌", "板凳", "床榻", "柜台", "灶台", "酒坛",
              "竹帘", "帷幔", "香炉", "书架", "茶壶", "木梁")),
    ("质感", ("斑驳", "破旧", "残垣", "断壁", "青苔", "尘土", "油烟", "潮湿",
              "腐朽", "陈旧", "残破", "荒芜")),
)


def draft_scene_desc(name):
    """根据剧情自动起草这个场景的「统一场景文案」。

    从这个场景所有镜头的画面描述里，按"时间/天气/光/建筑/质感"五类
    抽取出现的元素，拼成一句"这个地方长什么样"的话。
    认不出元素就退回一句通用描述（总比空着强）。
    """
    texts = _scene_texts_from_shots(name)
    blob = " ".join(texts)
    bits = []
    for _cat, words in _SCENE_ENV_WORDS:
        for w in words:
            if w in blob and w not in bits:
                bits.append(w)
                break                      # 每类最多取一个，代表性最强
    if not bits:
        return "%s，古代中国风，电影感写实，环境细节丰富" % name
    return "%s，%s" % (name, "、".join(bits))


def _auto_scenes_in_play():
    """当前分镜里出现过的所有场景（按第一次出现的顺序）。

    场景名的来源：每镜「⑧ 场景」下拉（自动识别或手动指定）。
    """
    names = []
    for r in shot_rows:
        try:
            v = (r.get("loc_var").get() or "").strip() if r.get("loc_var") else ""
        except Exception:
            v = ""
        if not v:
            try:
                v = guess_scene_with_prev(r["scene"].get("1.0", "end") or "", "")
            except Exception:
                v = ""
        if v and v not in names:
            names.append(v)
    return names


def _gen_scenes_batch(names, quiet=False):
    """给一批场景排队生成场景照（每个场景一张，约 1 分钟）。

    返回成功张数。生成前会先给没有文案的场景自动起草一段。
    """
    if not names:
        return 0
    made = 0
    try:
        import gen_zimage_keys as Z
        Z.W, Z.H = 1024, 1024
        for i, n in enumerate(names):
            desc = (SCENE_DESCS.get(n) or "").strip() or draft_scene_desc(n)
            SCENE_DESCS[n] = desc
            prefix = "scene_%s_%s" % (re.sub(r"[^\w]", "", n)[:12] or "s",
                                      time.strftime("%H%M%S"))
            prompt = SCENE_DESC_TPL.format(name=n, desc=desc,
                                          light="自然光，柔和均匀布光")
            wf = Z.build_wf(prompt, 660001 + i * 31337
                            + int(time.time()) % 97, prefix)
            pid = Z.submit(wf)
            if not pid:
                log_q.put("[场景照] %s 提交失败（后端在线吗）\n" % n)
                continue
            log_q.put("[场景照] %s 生成中…（%d/%d）\n" % (n, i + 1, len(names)))
            res = Z.wait(pid, prefix, timeout=1500)
            if res and os.path.exists(res):
                fn = prefix + ".png"
                shutil.copy2(res, os.path.join(AP.INPUTDIR, fn))
                SCENE_REFS[n] = fn
                made += 1
                log_q.put("[场景照] %s 完成 -> input/%s\n" % (n, fn))
            else:
                log_q.put("[场景照] %s 失败或超时\n" % n)
        _save_scene_refs()
    except Exception as e:
        log_q.put("[场景照] 出错: %s\n" % e)
    if not quiet:
        log_q.put("[场景照] 本轮 %d/%d 张成功\n" % (made, len(names)))
    root.after(0, _after_scene_image)
    return made


def _after_scene_image():
    """场景照生成完：刷新每镜下拉 + 场景库窗口。"""
    try:
        _refresh_all_scene_combos()
    except Exception:
        pass
    try:
        _refresh_scene_views()
    except Exception:
        pass


def _new_scene_flow():
    """导入新剧本后：认出场景 → 自动写场景文案 → 排队生成场景照 → 自动绑到每镜。

    和角色那条线（_new_char_flow）对称：导入完剧本，场景这套资产也自动到位。
    绑着名字但 input 里图片被删了也算没有，会重新生成；后端没起就排队，
    上线后自动补，不用手动点「🏞 场景照」→「🎬 全部补场景照」。
    """
    names = _auto_scenes_in_play()
    if not names:
        return
    # 新场景（没在库里的）→ 自动起草文案
    new_ones = []
    for n in names:
        if not (SCENE_DESCS.get(n) or "").strip():
            SCENE_DESCS[n] = draft_scene_desc(n)
            new_ones.append(n)
        if n not in SCENE_REFS:
            if n not in new_ones:
                new_ones.append(n)
    if new_ones:
        _save_scene_refs()
        try:
            _refresh_all_scene_combos()
        except Exception:
            pass
        log_q.put("[场景] 从剧情认出 %d 个场景：%s\n"
                  % (len(new_ones), "、".join(new_ones[:8])))
        for n in new_ones:
            log_q.put("    %s → %s\n" % (n, SCENE_DESCS.get(n) or ""))
    # 没有场景照的 → 后端在线就排队生成。
    # 和角色那条线一样：绑着名字但文件被删了，也算"没有"，否则每镜都会串景。
    reuse, lost, todo = [], [], []
    for n in names:
        fn = SCENE_REFS.get(n)
        if fn and os.path.exists(os.path.join(AP.INPUTDIR, fn)):
            reuse.append(n)
            continue
        if fn:
            lost.append(n)
            SCENE_REFS.pop(n, None)
        todo.append(n)
    if lost:
        try:
            _save_scene_refs()
        except Exception:
            pass
        log_q.put("[场景] 这些场景照文件不在了，重新生成：%s\n" % "、".join(lost[:8]))
    if reuse:
        log_q.put("[场景] 复用已有场景照：%s\n" % "、".join(reuse[:8]))
    if not todo:
        return
    if not backend_ready():
        log_q.put("[场景] %d 个场景还没有场景照，已排队；"
                  "后端一上线会自动补（不用手动点「全部补场景照」）。\n" % len(todo))
        _after_backend(
            lambda: threading.Thread(
                target=lambda: _gen_scenes_batch(todo, quiet=True),
                daemon=True).start(),
            "新剧场景照（%d 个场景）" % len(todo))
        return

    def work():
        _gen_scenes_batch(todo, quiet=True)

    threading.Thread(target=work, daemon=True).start()


_THUMBS = []      # 必须留着引用，否则 Tk 会把图片回收掉，显示成空白
_CHAR_VIEWERS = []   # 打开着的角色库 render 函数；定妆照/三视图生成完要回调刷新


def _refresh_char_views():
    for f in list(_CHAR_VIEWERS):
        try:
            f()
        except Exception:
            try:
                _CHAR_VIEWERS.remove(f)
            except Exception:
                pass


def _theme_rgb(hexstr, default=(247, 247, 249)):
    """把主题色 #RRGGBB 转成 PIL 用的 RGB，缩略图底色才能跟当前主题一致。"""
    try:
        h = (hexstr or "").lstrip("#")
        if len(h) == 6:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        pass
    return default


def _box_thumb(path, w=380, h=214):
    """把任意比例的图塞进 w×h 的框里（等比缩放 + 居中 + 浅底），给三视图用。"""
    try:
        from PIL import Image, ImageTk
        im = Image.open(path).convert("RGB")
        im.thumbnail((w, h), Image.LANCZOS)
        bg = Image.new("RGB", (w, h), _theme_rgb(getattr(TH, "BG", "")))
        bg.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
        ph = ImageTk.PhotoImage(bg)
        _THUMBS.append(ph)
        return ph
    except Exception:
        return _char_thumb(path, min(w, h))


def _char_thumb(path, size=76):
    """读定妆照并缩成方形缩略图；失败返回 None（不抛异常，界面不能因为一张图崩）。"""
    try:
        from PIL import Image, ImageTk
        im = Image.open(path).convert("RGB")
        im.thumbnail((size, size), Image.LANCZOS)
        bg = Image.new("RGB", (size, size), _theme_rgb(getattr(TH, "BG", "")))
        # 竖图/横图都居中，别拉伸变形
        bg.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
        ph = ImageTk.PhotoImage(bg)
        _THUMBS.append(ph)
        return ph
    except Exception:
        try:
            ph = tk.PhotoImage(file=path)
            _THUMBS.append(ph)
            return ph
        except Exception:
            return None


_adopt_chars_from_workdir()


def _save_char_refs():
    try:
        c = dict(AP.CFG)
        c["char_refs"] = dict(CHAR_REFS)
        c["char_descs"] = dict(CHAR_DESCS)
        c["char_sheets"] = dict(CHAR_SHEETS)
        c["char_voices"] = dict(CHAR_VOICES)
        AP.save_config(c)
        AP.CFG["char_refs"] = dict(CHAR_REFS)
        AP.CFG["char_descs"] = dict(CHAR_DESCS)
        AP.CFG["char_sheets"] = dict(CHAR_SHEETS)
        AP.CFG["char_voices"] = dict(CHAR_VOICES)
    except Exception as e:
        print("[char] 保存配置失败:", e)


def _refresh_ref_combos(autofill=False):
    """角色库变化后，把每镜「⑥ 参考角色」下拉的候选同步更新。
    autofill=True 时（角色库刚改完），若库里只有一个角色，就把空着的镜头自动挂上它——
    参考角色是必选项，只有一个角色时没必要让用户一镜一镜点。"""
    vals = [NOBODY] + list(CHAR_REFS.keys())
    for r in shot_rows:
        cb = r.get("ref_cb")
        if cb is not None:
            try:
                cb.configure(values=vals)
            except Exception:
                pass
        v = r["ref_var"].get()
        if v and v not in vals:
            r["ref_var"].set("")
    if autofill and len(vals) == 1:
        for r in shot_rows:
            if not r["ref_var"].get().strip():
                r["ref_var"].set(vals[0])
    _update_char_stat()


def _update_char_stat():
    """分镜栏上那行角色状态：一眼看出定妆照绑没绑、够不够。"""
    try:
        n = len(CHAR_REFS)
    except Exception:
        return
    # 头像条：每个已绑定角色一个小圆图
    try:
        for ch in char_faces.winfo_children():
            ch.destroy()
        for nm, fn in list(CHAR_REFS.items())[:6]:
            ph = _char_thumb(os.path.join(AP.INPUTDIR, fn), 30) if fn else None
            if ph is not None:
                lb = tk.Label(char_faces, image=ph, bd=1, relief="solid")
                lb.image = ph
                lb.pack(side="left", padx=(0, 3))
    except Exception:
        pass
    try:
        if n == 0:
            char_stat_lbl.config(
                text="⚠ 还没有定妆照 → 点「🎭 角色定妆照」（⑥ 必选）",
                foreground=TH.ERR)
        else:
            char_stat_lbl.config(
                text="已绑定 %d 个角色：%s"
                     % (n, "、".join(list(CHAR_REFS.keys())[:4])
                        + ("…" if n > 4 else "")),
                foreground=TH.OK)
    except Exception:
        pass


def _after_char_image():
    """新图落地后：同步每镜下拉、刷新角色库里的大图、刷分镜栏头像条。"""
    try:
        _refresh_ref_combos(True)
    except Exception:
        pass
    _refresh_char_views()
    try:
        _update_char_stat()
    except Exception:
        pass


def _gen_char_sheet(name, desc, kind="portrait"):
    """用 Z-Image 直接生成定妆照（kind=sheet 时生成三视图设定稿）并绑到该角色。"""
    def work():
        try:
            import gen_zimage_keys as Z
            if kind == "sheet":
                Z.W, Z.H = 1360, 768       # 三视图横排，宽幅才排得下
            else:
                Z.W, Z.H = 1024, 1024
            Z.NEG = Z.NEG + ("，现代服装,现代发型,短发,寸头,西装,衬衫,夹克,T恤,"
                             "牛仔裤,墨镜,手表,手机,现代建筑,室内装修,塑料质感")
            prefix = ("sheet_%s_%s" if kind == "sheet" else "char_%s_%s") % (
                re.sub(r"[^\w]", "", name)[:12] or "c", time.strftime("%H%M%S"))
            wf = Z.build_wf(desc, 880001 + len(CHAR_REFS) * 7919 + int(time.time()) % 97,
                            prefix)
            pid = Z.submit(wf)
            if not pid:
                log_q.put("[定妆照] 提交失败——后端在线吗？\n"); return
            log_q.put("[定妆照] 已提交，生成中（约 1 分钟）…\n")
            res = Z.wait(pid, prefix)
            if res and os.path.exists(res):
                fn = prefix + ".png"
                shutil.copy2(res, os.path.join(AP.INPUTDIR, fn))
                if kind == "sheet":
                    # 再把这张脸的定妆照贴到三视图最左边，右边补一张脸部细节板，
                    # 合成「定妆照 | 三视图 | 细节网格」——跟豆包那套设定板一个思路
                    ref_fn = CHAR_REFS.get(name)
                    _face = _gen_face_board(name, CHAR_DESCS.get(name, ""),
                                            seed=len(CHAR_REFS) + 7)
                    if _face:
                        log_q.put("[脸部板] %s 完成\n" % name)
                    if ref_fn:
                        _compose_sheet(os.path.join(AP.INPUTDIR, fn),
                                       os.path.join(AP.INPUTDIR, ref_fn),
                                       os.path.join(AP.INPUTDIR, fn), name,
                                       face_path=_face)
                    CHAR_SHEETS[name] = fn          # 三视图只当参考图，不当 img2img 起点
                    _save_char_refs()
                    log_q.put("[三视图] %s 完成 -> input/%s\n" % (name, fn))
                    root.after(0, _after_char_image)
                    return
                CHAR_REFS[name] = fn
                _save_char_refs()
                log_q.put("[定妆照] %s 完成 -> input/%s\n" % (name, fn))
                root.after(0, _after_char_image)
            else:
                log_q.put("[定妆照] %s 生成超时或失败\n" % name)
        except Exception as e:
            log_q.put("[定妆照] 出错: %s\n" % e)
    threading.Thread(target=work, daemon=True).start()


def _script_char_names(parsed):
    """从剧本里认出所有角色名：第 7 列写了算，台词「名字：」也算。
    按首次出现排序；旁白/画外音这类不是角色，剔掉。"""
    # 这些是"念白身份"不是角色，别拿去生成定妆照（回忆音：… / 旁白：… 都是常见写法）
    skip = {"旁白", "画外音", "画外", "内心", "内在", "内心音", "回忆音", "回忆",
            "心声", "独白", "旁白者", "音效", "字幕", "无人", "镜头", "环境音"}
    names = []

    def add(n):
        n = (n or "").strip()
        if not n or n in names or len(n) > 8:
            return
        if n in skip or n == NOBODY:
            return
        if n.endswith("旁白") or n.endswith("音") or n.endswith("白"):
            return
        names.append(n)

    for p in parsed:
        add(p.get("ref"))
        # 台词可能有多行、多个说话人（剧本格式常见），逐行都要认出来，
        # 否则同一镜里第二个角色不会被生成定妆照。
        for _ln in (p.get("line") or "").splitlines():
            who, _ = _split_who(_ln)
            add(who)
    return names


def _autofill_refs(parsed, names=None, quiet=True):
    """把每镜「⑥ 参考角色」按剧本自动填好：第 7 列 > 台词说话人 > 全剧只有一个角色就用它。
    返回 (填上的镜数, 仍空着的镜号列表)。"""
    pool = [n for n in (names or list(CHAR_REFS)) if n in CHAR_REFS]
    filled, left, n_nobody = 0, [], 0
    for i, r in enumerate(shot_rows):
        cur = r["ref_var"].get().strip()
        if cur in CHAR_REFS:
            continue
        p = parsed[i] if i < len(parsed) else {}
        c7 = (p.get("ref") or "").strip()
        who, _ = _split_who(p.get("line") or "")
        cand = ""
        if c7 == NOBODY:
            cand = NOBODY
        elif c7 in CHAR_REFS:
            cand = c7
        elif who in CHAR_REFS:
            cand = who
        else:
            # 没台词的镜（空镜、纯动作镜）永远轮不到"说话人"这条路 ——
            # 以前第 1/8/10 镜就这么空着，然后被「⑥ 参考角色必选」拦下，
            # 命令行跑片直接卡在弹窗上。按文案兜底：
            body = (p.get("scene") or "") + " " + (p.get("line") or "")
            hits = [(body.find(n), n) for n in CHAR_REFS if n and n in body]
            if hits:
                cand = sorted(hits)[0][1]                  # 文案里最先提到的那个人
            elif (("空镜" in body) or ("无人" in body) or ("没有人" in body)
                  or ("不见人影" in body)):
                cand = NOBODY                              # 明确的空镜
            elif len(CHAR_REFS) == 1:
                cand = list(CHAR_REFS)[0]
            elif len(pool) == 1:
                cand = pool[0]
            elif body.strip():
                # 文案里一个人名都没提到 → 就是空镜（远景/环境/道具镜头）。
                # 这条必须兜住：留空会被"⑥ 参考角色必选"拦下，整条片子卡在弹窗上，
                # 命令行跑片尤其致命（没人点）。
                cand = NOBODY
            else:
                cand = ""                                  # 真的没内容，留给人选
        if cand:
            r["ref_var"].set(cand)
            filled += 1
            if cand == NOBODY:
                n_nobody += 1
        else:
            left.append(str(i + 1))
    _update_char_stat()
    if not quiet and (filled or left):
        _tail = ("；第 %s 镜还没匹配到角色，请手动选" % "、".join(left[:12])) if left else ""
        if n_nobody and not CHAR_REFS:
            _tail += ("；本片没认出角色名，%d 镜按「%s」处理，不需要定妆照"
                      "（想有人物就在台词里写「角色名：台词」）" % (n_nobody, NOBODY))
        log_q.put("[锚定] 已自动填 %d 镜的参考角色%s\n" % (filled, _tail))
    return filled, left


def _regen_missing_assets(ch_names, sc_names):
    """「下一步」栏那个按钮：把缺的定妆照和场景照一起补上。"""
    if ch_names:
        _after_backend(lambda: _regen_char_sheets(ch_names), "补定妆照")
    if sc_names:
        _after_backend(
            lambda: threading.Thread(
                target=lambda: _gen_scenes_batch(sc_names, quiet=True),
                daemon=True).start(),
            "补场景照")
    log_q.put("[资产] 已排队补 %d 个角色定妆照、%d 个场景照；"
              "跑完日志会写「完成」\n" % (len(ch_names or []), len(sc_names or [])))


def _regen_char_sheets(names):
    """按角色库里已有的外貌文案，重新生成这些角色的定妆照。

    用在"名字还绑着、但图片文件已经不在了"的情况（换过模型目录、手动清过 input）。
    没有外貌文案的角色补不了 —— 那需要人来写，直接跳过并在日志里点名。
    """
    done = 0
    for nm in names or []:
        look = (CHAR_DESCS.get(nm) or "").strip()
        if not look:
            log_q.put("[角色] %s 没有外貌文案，补不了定妆照"
                      "（去「🎭 角色定妆照」里写一段再点「✦ 一键生成」）\n" % nm)
            continue
        try:
            _gen_char_sheet(nm, CHAR_DESC_TPL.format(who=nm, look=look))
            done += 1
        except Exception as e:
            log_q.put("[角色] %s 定妆照重生成失败：%s\n" % (nm, e))
    if done:
        log_q.put("[角色] 已提交 %d 个角色的定妆照重生成（约 1 分钟/个）\n" % done)


def _char_batch_worker(items, parsed=None, names=None):
    """生成定妆照 + 三视图 + 脸部板，完成后自动锚定到各镜。

    弹窗那条路和「新剧情全自动」那条路共用这一份，免得两处逻辑各改一半。
    """
    try:
        import gen_zimage_keys as Z
        Z.W, Z.H = 1024, 1024
        Z.NEG = Z.NEG + ("，现代服装,现代发型,短发,寸头,西装,衬衫,夹克,T恤,"
                         "牛仔裤,墨镜,手表,手机,现代建筑,室内装修,塑料质感")
        made = 0
        for i, (nm, look) in enumerate(items):
            prefix = "char_%s_%02d" % (time.strftime("%H%M%S"), i)
            dsc = CHAR_DESC_TPL.format(who=nm, look=look)
            CHAR_DESCS[nm] = look          # 统一文案跟着角色一起存盘
            wf = Z.build_wf(dsc, 880001 + len(CHAR_REFS) * 7919 + i * 31337
                            + int(time.time()) % 97, prefix)
            pid = Z.submit(wf)
            if not pid:
                log_q.put("[定妆照] %s 提交失败（后端在线吗）\n" % nm)
                continue
            log_q.put("[定妆照] %s 生成中…（%d/%d）\n" % (nm, i + 1, len(items)))
            res = Z.wait(pid, prefix, timeout=1500)
            if res and os.path.exists(res):
                fn = prefix + ".png"
                shutil.copy2(res, os.path.join(AP.INPUTDIR, fn))
                CHAR_REFS[nm] = fn
                made += 1
                log_q.put("[定妆照] %s 完成 -> input/%s\n" % (nm, fn))

                # 紧跟着出三视图（正/侧/背全身），并把这张脸贴到最左边——
                # 新剧情也要有这套资产，不然角色库里只有一张脸、对不上衣袍
                try:
                    sprefix = "sheet3_%s_%s" % (
                        re.sub(r"[^\w]", "", nm)[:12] or "c",
                        time.strftime("%H%M%S"))
                    Z.W, Z.H = 1360, 768
                    log_q.put("[三视图] %s 生成中…\n" % nm)
                    wf2 = Z.build_wf(CHAR_SHEET_TPL.format(who=nm, look=look),
                                     770001 + i * 31337, sprefix)
                    pid2 = Z.submit(wf2)
                    res2 = Z.wait(pid2, sprefix, timeout=1500) if pid2 else None
                    if res2 and os.path.exists(res2):
                        fn2 = sprefix + ".png"
                        raw2 = os.path.join(OUTDIR, fn2)
                        shutil.copy2(res2, raw2)
                        _face = _gen_face_board(nm, look, seed=i + 3)
                        if _face:
                            log_q.put("[脸部板] %s 完成\n" % nm)
                        _compose_sheet(raw2, os.path.join(AP.INPUTDIR, fn),
                                       os.path.join(AP.INPUTDIR, fn2), nm,
                                       face_path=_face)
                        CHAR_SHEETS[nm] = fn2
                        log_q.put("[三视图] %s 完成 -> input/%s\n" % (nm, fn2))
                    else:
                        log_q.put("[三视图] %s 生成失败（不影响锁脸，可事后补）\n" % nm)
                    Z.W, Z.H = 1024, 1024      # 还原，下一个角色还要出定妆照
                except Exception as _e2:
                    log_q.put("[三视图] %s 出错：%s\n" % (nm, _e2))
            else:
                log_q.put("[定妆照] %s 失败或超时\n" % nm)
        _save_char_refs()

        def done():
            _refresh_ref_combos(True)
            filled, left = _autofill_refs(parsed, names, quiet=False)
            log_q.put("[定妆照] 本轮 %d/%d 张成功；已锚定 %d 镜%s\n"
                      % (made, len(items), filled,
                         ("；第 %s 镜待手动选角色" % "、".join(left[:12])) if left else ""))
            # 锚定完这一步是「新手最茫然」的时刻：脸出来了、日志也打了，但没人告诉他
            # 接下来点哪儿。这里立刻重算一次提示条，并在状态栏给出下一步。
            try:
                _refresh_next_step(force=True)
            except Exception:
                pass
            try:
                if left:
                    set_status("定妆照已出：%d 镜已锚定；第 %s 镜还需手选角色"
                               % (filled, "、".join(str(x) for x in left[:4])),
                               "warn")
                else:
                    set_status("定妆照已出：%d 镜已锚定 → 下一步点「▶ 开始出片」"
                               % filled, "ok")
            except Exception:
                pass
        root.after(0, done)
    except Exception as e:
        log_q.put("[定妆照] 出错: %s\n" % e)


def _auto_char_enabled():
    """新剧情要不要「全自动匹配定妆照」：不弹窗，直接按剧本起草文案开跑。"""
    try:
        v = AP.CFG.get("auto_char")
        if v is None:
            return bool(AP.CFG.get("auto_go"))    # 没单独设过就跟着「自动出片」走
        return bool(v)
    except Exception:
        return False


def _set_auto_char(on):
    try:
        c = dict(AP.CFG)
        c["auto_char"] = bool(on)
        AP.save_config(c)
        AP.CFG["auto_char"] = bool(on)
    except Exception:
        pass


def _auto_char_flow(parsed, names, todo, extra):
    """新剧情全自动：库里有的直接复用，没有的按剧本起草外貌文案 → 生成 → 锚定。"""
    # 旧剧的角色挂着会串戏（上一部的脸被锚到这一部），自动模式下直接解绑
    if extra:
        # 自动模式没人拦着，解绑错了就找不回 → 先落一份底，事后能照着加回来
        try:
            import json as _j
            with open(os.path.join(OUTDIR, "char_unbound.json"), "w",
                      encoding="utf-8") as f:
                _j.dump([{"name": n, "ref": CHAR_REFS.get(n) or "",
                          "look": CHAR_DESCS.get(n) or "",
                          "sheet": CHAR_SHEETS.get(n) or "",
                          "when": time.strftime("%Y-%m-%d %H:%M:%S")}
                         for n in extra], f, ensure_ascii=False, indent=1)
            log_q.put("[自动匹配] 解绑记录已存 output/char_unbound.json\n")
        except Exception:
            pass
        for n in extra:
            CHAR_REFS.pop(n, None)
            CHAR_DESCS.pop(n, None)
            CHAR_SHEETS.pop(n, None)
        try:
            _save_char_refs()
        except Exception:
            pass
        try:
            _refresh_ref_combos(True)
        except Exception:
            pass
        log_q.put("[自动匹配] 解绑上一部留下的角色：%s\n" % "、".join(extra[:8]))

    # 「匹配」要连文件一起查：库里绑着名字但 input 里的图被删了（换机、清目录），
    # 光看名字会以为有脸可用，结果每镜都换脸。文件不在就重出一张。
    reuse, lost, todo = [], [], []
    for n in names:
        fn = CHAR_REFS.get(n)
        if fn and os.path.exists(os.path.join(AP.INPUTDIR, fn)):
            reuse.append(n)
            continue
        if fn:
            lost.append(n)
            CHAR_REFS.pop(n, None)
            CHAR_DESCS.setdefault(n, "")     # 文案留着，重出时还用这段
        todo.append(n)
    if lost:
        try:
            _save_char_refs()
        except Exception:
            pass
        log_q.put("[自动匹配] 这些角色的定妆照文件不在了，重新生成：%s\n"
                  % "、".join(lost[:8]))
    if reuse:
        log_q.put("[自动匹配] 复用库里已有的定妆照：%s\n" % "、".join(reuse[:8]))
    if not todo:
        _autofill_refs(parsed, names, quiet=False)
        return

    if not backend_ready():
        log_q.put("[自动匹配] %d 个新角色（%s）等着出定妆照；后端一上线自动继续。\n"
                  % (len(todo), "、".join(todo[:6])))
        _after_backend(
            lambda: _new_char_flow(parsed, names),
            "新剧定妆照（%d 个角色）" % len(todo))
        return

    # 外貌文案按剧本自动起草（名字 + 他出现过的画面），不同角色不会撞同一句话
    items = []
    for i, n in enumerate(todo):
        look = (CHAR_DESCS.get(n) or "").strip()
        if not look:
            try:
                look = _draft_look(n, _char_scenes(parsed, n), i).strip()
            except Exception:
                look = ""
        if look:
            items.append((n, look))
        else:
            log_q.put("[自动匹配] %s 没起草出外貌文案，先跳过（可在角色库里补）\n" % n)
    if not items:
        _autofill_refs(parsed, names, quiet=False)
        return

    for n, look in items:
        log_q.put("[自动匹配] %s → %s\n" % (n, look))
    log_q.put("[自动匹配] 开始生成 %d 个新角色（定妆照 → 三视图 → 脸部板），"
              "完成后自动锚定到各镜并接着出片…\n" % len(items))
    try:
        set_status("自动匹配：%d 个新角色生成中（%s）"
                   % (len(items), "、".join(n for n, _ in items[:4])), "ok")
    except Exception:
        pass
    threading.Thread(target=_char_batch_worker,
                     args=(items, parsed, names), daemon=True).start()


def _new_char_flow(parsed, names):
    """导入新剧情后：角色库里没有的角色，一次性生成定妆照 + 自动锚定到各镜。
    为什么要弹这一下：AI 必须知道这个人长什么样才画得出一致的脸，
    所以把外貌描述摊出来让人扫一眼、顺手改两个字，比事后返工 20 镜便宜。
    点一次「全部生成并锚定」之后全流程自动跑，不用一镜一镜点。"""
    todo = [n for n in names if n not in CHAR_REFS]
    extra = [n for n in CHAR_REFS if n not in names]     # 旧剧留下的角色，会串戏
    # 全自动：不弹窗，直接复用/起草/生成。没人守着的时候（导入后自动出片）
    # 这个弹窗会把整条链卡死 —— auto_go 在等参考角色，参考角色在等这个确认框。
    if _auto_char_enabled():
        return _auto_char_flow(parsed, names, todo, extra)
    if not todo and not extra:
        _autofill_refs(parsed, names, quiet=False)
        return
    if not todo and extra:
        # 没有新角色，但有旧剧的人挂着 → 问一句要不要清，避免锚错脸
        if messagebox.askyesno(
                "换了新剧情",
                "这部戏里没有这些角色：%s\n\n要把它们从角色库里解绑吗？\n"
                "（解绑＝不再参与本片锚定；定妆照文件仍在 input 目录，随时能加回来）"
                % "、".join(extra[:8])):
            for n in extra:
                CHAR_REFS.pop(n, None)
                CHAR_DESCS.pop(n, None)
                CHAR_SHEETS.pop(n, None)
            _save_char_refs()
            _refresh_ref_combos(True)
        _autofill_refs(parsed, names, quiet=False)
        return
    if not backend_ready():
        _autofill_refs(parsed, names, quiet=False)
        # 以前这里弹一个"定妆照先跳过"的警告就让用户自己事后补 —— 新手根本不会补，
        # 直接点生成，出片全是没锁住的脸，还以为是程序坏了。
        # 现在只排队：后端一上线自动弹定妆照那一步，顺序不用人记。
        _after_backend(
            lambda: _new_char_flow(parsed, names),
            "新剧定妆照（%d 个角色）" % len(todo))
        log_q.put("[角色] 后端还没就绪，定妆照已排队；"
                  "启动完成后会自动继续（不用手动补）。\n")
        return

    w = tk.Toplevel(root)
    w.title("新剧情 · 自动定妆照")
    TH.paint_toplevel(w)
    w.transient(root)
    center_win(w, 820, 560, 700, 460)

    ttk.Label(w, text="检测到 %d 个新角色" % len(todo), font=FONT_B).pack(
        anchor="w", padx=16, pady=(14, 2))
    ttk.Label(w, style="Hint.TLabel", wraplength=740, justify="left",
              text="锁脸靠两件套：① 定妆照当参考图（锁五官）　② 每个角色一段固定的外貌文案（锁发型/衣袍/气质）。\n"
                   "下面填的就是第 ② 项——越具体越稳（年龄/发色/衣袍/神态）。点「全部生成并锚定」后：\n"
                   "　先按这段文案生成定妆照 → 再出三视图（正/侧/背全身，脸用定妆照原图贴）→ "
                   "自动绑定角色 → 自动填进对应镜头的『⑥ 参考角色』。"
              ).pack(anchor="w", padx=16)

    keep_old = tk.BooleanVar(value=True)
    if extra:
        ttk.Checkbutton(w, text="同时解绑不属于本剧的角色：%s（避免串戏）"
                        % "、".join(extra[:6]), variable=keep_old).pack(
            anchor="w", padx=16, pady=(6, 0))

    body = ttk.Frame(w)
    body.pack(fill="both", expand=True, padx=16, pady=(10, 4))
    rows = []
    for i, n in enumerate(todo):
        row = ttk.Frame(body)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=n, font=FONT_B, width=8).pack(side="left")
        v = tk.StringVar(value=(CHAR_DESCS.get(n) or _draft_look(
            n, _char_scenes(parsed, n), i)))
        e = ttk.Entry(row, textvariable=v, font=FONT)
        e.pack(side="left", fill="x", expand=True, padx=(6, 0))
        rows.append((n, v))

    opt = ttk.Frame(w)
    opt.pack(fill="x", padx=16, pady=(6, 0))
    _auto_next = tk.BooleanVar(value=_auto_char_enabled())
    ttk.Checkbutton(opt, text="以后新剧情都这样自动跑（不再弹这个框：按剧本起草文案 → "
                             "生成 → 自动锚定 → 接着出片）",
                    variable=_auto_next).pack(anchor="w")

    btns = ttk.Frame(w)
    btns.pack(fill="x", padx=16, pady=(6, 14))

    def worker(items):
        _char_batch_worker(items, parsed, names)

    def go():
        items = [(n, v.get().strip()) for n, v in rows if v.get().strip()]
        if not items:
            messagebox.showwarning("文案是空的", "每个角色至少留一句外貌文案。", parent=w)
            return
        w.destroy()
        if _auto_next.get():
            _set_auto_char(True)
            log_q.put("[自动匹配] 已记住：以后新剧情不再弹窗，直接自动生成。\n")
        if extra and keep_old.get():
            for n in extra:
                CHAR_REFS.pop(n, None)
                CHAR_DESCS.pop(n, None)
                CHAR_SHEETS.pop(n, None)
            _save_char_refs()
            log_q.put("[角色] 已解绑不属于本剧的角色：%s\n" % "、".join(extra[:6]))
        _refresh_ref_combos(True)
        log_q.put("[定妆照] 开始生成 %d 张定妆照（约 %d 分钟）…\n"
                  % (len(items), max(1, len(items))))
        threading.Thread(target=worker, args=(items,), daemon=True).start()

    TH.RoundButton(btns, text="✦ 全部生成并锚定", command=go, kind="accent",
                   height=34, padx=18).pack(side="left")
    TH.RoundButton(btns, text="跳过，我自己弄", command=lambda: (w.destroy(),
                   _autofill_refs(parsed, names, quiet=False)), height=34).pack(
        side="left", padx=(8, 0))
    try:
        w.grab_set()
    except Exception:
        pass


def open_chars():
    """角色定妆照管理：从图片添加 / 直接生成 / 删除绑定。"""
    w = tk.Toplevel(root)
    w.title("角色定妆照 · 锁住跨镜同一张脸")
    w.transient(root)
    w.grab_set()
    center_win(w, 1020, 820, 900, 600)

    hd = ttk.Frame(w)
    hd.pack(anchor="w", fill="x", padx=16, pady=(14, 2))
    ttk.Label(hd, text="🎭 角色定妆照", font=FONT_B).pack(side="left")
    cnt_lbl = ttk.Label(hd, style="Hint.TLabel", text="")
    cnt_lbl.pack(side="left", padx=(12, 0))
    # 发型是三维结构，正面照拍不到后脑的髻 —— 这里直接挑一款写进外貌文案
    try:
        TH.RoundButton(hd, text="💇 古装发型库", command=lambda: open_hair_lib(),
                       height=28, padx=10).pack(side="right")
    except Exception:
        pass

    ttk.Label(w, style="Hint.TLabel",
              text="左＝正面定妆照（每镜关键帧按这张脸锚定）；中＝三视图：最左边就是这张脸，"
                   "后面接正面/侧面/背面全身。两边是同一张图，不可能对不上。"
                   "每镜「⑥ 参考角色」必选，空着生成会被拦下。",
              wraplength=960, justify="left").pack(anchor="w", padx=16)

    body = ttk.Frame(w)
    body.pack(fill="both", expand=True, padx=16, pady=(10, 6))
    cvs = tk.Canvas(body, background=TH.BG, highlightthickness=0, bd=0, height=300)
    sb = ttk.Scrollbar(body, orient="vertical", command=cvs.yview)
    inner = ttk.Frame(cvs)
    win_id = cvs.create_window((0, 0), window=inner, anchor="nw")
    cvs.configure(yscrollcommand=sb.set)
    cvs.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    cvs.bind("<Configure>", lambda e: cvs.itemconfigure(win_id, width=e.width))
    inner.bind("<Configure>", lambda e: cvs.configure(scrollregion=cvs.bbox("all")))

    def _voice_edit(name):
        """给单个角色挑嗓音：音色 + 语速 + 音高。撞音色时靠后两项把两人拉开。"""
        d = tk.Toplevel(w)
        d.title("角色嗓音 · " + name)
        d.transient(w)
        d.grab_set()
        center_win(d, 500, 330, 470, 290)
        cur = _voice_for_char(name)
        labels = list(VOICES.keys())
        cur_lab = next((k for k, v in VOICES.items() if v == cur.get("voice")), labels[0])
        vv = tk.StringVar(value=cur_lab)
        rr = tk.StringVar(value=cur.get("rate") or "+0%")
        pp = tk.StringVar(value=cur.get("pitch") or "+0Hz")

        ttk.Label(d, text="🎙 %s 的嗓音" % name, font=FONT_B).pack(
            anchor="w", padx=16, pady=(14, 2))
        ttk.Label(d, style="Hint.TLabel",
                  text="同一部戏里音色重复时，用「语速 / 音高」拉开：同一个人换了档位，"
                       "听感就是两个不同的人。只有 B 方案（edge-tts 配音）用得上。",
                  wraplength=440, justify="left").pack(anchor="w", padx=16)

        box = ttk.Frame(d)
        box.pack(fill="x", padx=16, pady=(10, 0))
        ttk.Label(box, text="音色", width=6).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(box, textvariable=vv, values=labels, state="readonly",
                     width=30, font=FONT).grid(row=0, column=1, sticky="w")
        ttk.Label(box, text="语速", width=6).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(box, textvariable=rr, state="readonly", width=10, font=FONT,
                     values=["+0%", "+10%", "+20%", "-8%", "-14%", "-20%"]
                     ).grid(row=1, column=1, sticky="w")
        ttk.Label(box, text="音高", width=6).grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(box, textvariable=pp, state="readonly", width=10, font=FONT,
                     values=["+0Hz", "+10Hz", "+16Hz", "+30Hz", "-18Hz", "-30Hz"]
                     ).grid(row=2, column=1, sticky="w")

        def save():
            CHAR_VOICES[name] = {"voice": VOICES.get(vv.get()) or cur.get("voice"),
                                 "rate": rr.get() or "+0%",
                                 "pitch": pp.get() or "+0Hz"}
            _save_char_refs()
            try:
                render()
            except Exception:
                pass
            d.destroy()

        bar = ttk.Frame(d)
        bar.pack(fill="x", padx=16, pady=14)
        TH.RoundButton(bar, text="保存", kind="accent", height=30, padx=16,
                       command=save).pack(side="right")
        TH.RoundButton(bar, text="取消", height=30, padx=14,
                       command=d.destroy).pack(side="right", padx=(0, 8))

    def render():
        for ch in inner.winfo_children():
            ch.destroy()
        if render not in _CHAR_VIEWERS:
            _CHAR_VIEWERS.append(render)
        if not CHAR_REFS:
            try:
                cnt_lbl.configure(text="还没有角色")
            except Exception:
                pass
            ttk.Label(inner, style="Hint.TLabel",
                      text="还没有角色。点下面「➕ 从图片添加」或「🎬 生成定妆照」。"
                      ).pack(anchor="w", pady=6)
            return
        try:
            cnt_lbl.configure(text="共 %d 位：%s　·　窗口放不下就用滚轮上下翻"
                              % (len(CHAR_REFS), " / ".join(CHAR_REFS.keys())))
        except Exception:
            pass
        for name, fn in list(CHAR_REFS.items()):
          try:
            card = ttk.LabelFrame(inner, text="  " + name + "  ", padding=8)
            card.pack(fill="x", pady=6)
            strip = ttk.Frame(card)
            strip.pack(fill="x")

            # ── 左：正面定妆照（锁脸用的就是这张脸）
            leftc = ttk.Frame(strip)
            leftc.pack(side="left", anchor="n")
            full = os.path.join(AP.INPUTDIR, fn)
            ph = _char_thumb(full, 116) if os.path.exists(full) else None
            if ph is not None:
                tk.Label(leftc, image=ph, borderwidth=1, relief="solid",
                         background=TH.BG).pack()
            else:
                ttk.Label(leftc, text="无图", width=8, foreground=TH.ERR).pack()
            ttk.Label(leftc, text="正面 · 锁脸用", foreground=TH.TEXT3,
                      font=TH.FONT_XS).pack(pady=(3, 0))

            # ── 中：三视图（正面 / 侧面 / 背面 全身）
            midc = ttk.Frame(strip)
            midc.pack(side="left", padx=(10, 0), anchor="n")
            _sh = CHAR_SHEETS.get(name)
            _shfull = os.path.join(AP.INPUTDIR, _sh) if _sh else ""
            _shp = (_box_thumb(_shfull, 340, 192)
                    if _shfull and os.path.exists(_shfull) else None)
            if _shp is not None:
                tk.Label(midc, image=_shp, borderwidth=1, relief="solid",
                         background=TH.BG).pack()
                ttk.Label(midc, text="定妆照 + 三视图 · 正面 / 侧面 / 背面（全身）",
                          foreground=TH.TEXT3, font=TH.FONT_XS).pack(pady=(3, 0))
            else:
                ph2 = tk.Frame(midc, width=340, height=192, background=TH.FIELD,
                               highlightthickness=1, highlightbackground="#c9c9cf")
                ph2.pack()
                ph2.pack_propagate(False)
                tk.Label(ph2, text="三视图还没生成", font=FONT_B,
                         bg=TH.FIELD, fg=TH.TEXT2).pack(pady=(78, 2))
                tk.Label(ph2, text="点右边「📐 生成三视图」→ 一张图出\n"
                                   "这张脸 + 正面/侧面/背面全身",
                         font=TH.FONT_XS, fg=TH.TEXT3, bg=TH.FIELD,
                         justify="center").pack()
                ttk.Label(midc, text="三视图 · 未生成", foreground=TH.WARN,
                          font=TH.FONT_XS).pack(pady=(3, 0))

            # ── 右：文案 + 操作
            col = ttk.Frame(strip)
            col.pack(side="left", fill="both", expand=True, padx=(10, 0))
            ttk.Label(col, text=fn, foreground=TH.TEXT3,
                      font=TH.FONT_XS).pack(anchor="w")
            look = (CHAR_DESCS.get(name) or "").strip()
            ttk.Label(col, text=("文案：" + look) if look else
                      "文案：空 → 只锁脸，不锁发型衣袍",
                      foreground=(TH.TEXT3 if look else TH.WARN),
                      font=TH.FONT_XS, wraplength=250,
                      justify="left").pack(anchor="w", pady=(2, 6))
            # ── 嗓音：B 方案下这个角色固定用哪把嗓子（音色 + 语速 + 音高）
            _vp = _voice_for_char(name)
            vrow = ttk.Frame(col)
            vrow.pack(anchor="w", pady=(0, 6))
            ttk.Label(vrow, text="🎙 " + _voice_profile_label(_vp),
                      foreground=TH.TEXT2, font=TH.FONT_XS).pack(side="left")
            TH.RoundButton(vrow, text="改", height=24, padx=8,
                           command=lambda n=name: _voice_edit(n)).pack(
                side="left", padx=(6, 0))
            rowb = ttk.Frame(col)
            rowb.pack(anchor="w")
            TH.RoundButton(rowb, text="📐 生成三视图", height=28, padx=10,
                           command=lambda n=name: _sheet_one(n)).pack(side="left")
            TH.RoundButton(rowb, text="✎ 改文案", height=28, padx=10,
                           command=lambda n=name: _edit_look(n)).pack(
                side="left", padx=(6, 0))
            TH.RoundButton(rowb, text="✕ 解绑", kind="danger", height=28, padx=10,
                           command=lambda n=name: _unbind(n)).pack(
                side="left", padx=(6, 0))
            if not os.path.exists(full):
                ttk.Label(col, text="⚠ 定妆照文件缺失：" + fn, foreground=TH.ERR,
                          font=TH.FONT_XS).pack(anchor="w", pady=(4, 0))
          except Exception as _e:
            ttk.Label(inner, text="⚠ %s 这张卡片没画出来：%s" % (name, _e),
                      foreground=TH.ERR).pack(anchor="w", pady=4)

        def _sheet_one(n):
            look = (CHAR_DESCS.get(n) or "").strip() or DEFAULT_LOOK
            _gen_char_sheet(n, CHAR_SHEET_TPL.format(who=n, look=look), kind="sheet")
            log_q.put("[三视图] %s 已提交，约 1 分钟出图（出完这张卡片上就能看到正面/侧面/背面）\n" % n)

        def _edit_look(n):
            cur = (CHAR_DESCS.get(n) or DEFAULT_LOOK)
            v = simpledialog.askstring(
                "统一文案 · %s" % n,
                "这个角色的外貌文案（每镜关键帧都会原样贴上，锁发型/衣袍/气质）：",
                initialvalue=cur, parent=w)
            if v is None:
                return
            CHAR_DESCS[n] = v.strip()
            _save_char_refs()
            render()

        def _unbind(n):
            CHAR_REFS.pop(n, None)
            CHAR_DESCS.pop(n, None)
            CHAR_SHEETS.pop(n, None)
            _save_char_refs()
            _refresh_ref_combos()
            render()
    render()

    def add_from_file():
        p = filedialog.askopenfilename(
            title="选一张角色定妆照（正面、脸清楚最好）",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not p:
            return
        name = simpledialog.askstring(
            "角色名", "这个角色叫什么？\n（要和台词里的写法一致，如「苏云舟」）",
            parent=w)
        if not name:
            return
        name = name.strip()
        ext = os.path.splitext(p)[1].lower() or ".png"
        fn = "char_%s_%s%s" % (time.strftime("%H%M%S"), abs(hash(name)) % 9973, ext)
        try:
            shutil.copy2(p, os.path.join(AP.INPUTDIR, fn))
        except Exception as e:
            messagebox.showerror("复制失败", str(e), parent=w); return
        CHAR_REFS[name] = fn
        _save_char_refs(); _refresh_ref_combos(True); render()

    def gen_sheet():
        name = simpledialog.askstring(
            "角色名", "要生成哪个角色的定妆照？\n（如「苏云舟」）", parent=w)
        if not name:
            return
        name = name.strip()
        look = simpledialog.askstring(
            "外貌文案", "这个角色长什么样、穿什么（定妆照和之后每镜都贴这段）：",
            initialvalue=(CHAR_DESCS.get(name) or DEFAULT_LOOK), parent=w)
        if not look:
            return
        CHAR_DESCS[name] = look.strip()
        _gen_char_sheet(name, CHAR_DESC_TPL.format(who=name, look=look.strip()))
        render()

    def gen_sheet3():
        """一键补齐：所有还没有三视图的角色，全部排队生成。"""
        todo = [n for n in CHAR_REFS if not CHAR_SHEETS.get(n)]
        if not CHAR_REFS:
            messagebox.showinfo("还没有角色", "先「➕ 从图片添加」或「🎬 生成定妆照」。", parent=w)
            return
        if not todo:
            if not messagebox.askyesno(
                    "三视图都有了",
                    "全部角色都有三视图了。要按现在的文案重画一遍吗？", parent=w):
                return
            todo = list(CHAR_REFS)
        log_q.put("[三视图] 排队生成 %d 张：%s\n" % (len(todo), "、".join(todo)))
        for n in todo:
            look = (CHAR_DESCS.get(n) or "").strip() or DEFAULT_LOOK
            _gen_char_sheet(n, CHAR_SHEET_TPL.format(who=n, look=look), kind="sheet")

    foot = ttk.Frame(w)
    foot.pack(fill="x", padx=16, pady=(0, 14))
    TH.RoundButton(foot, text="➕ 从图片添加", command=add_from_file,
                   height=34, padx=14).pack(side="left")
    TH.RoundButton(foot, text="🎬 生成定妆照", command=gen_sheet,
                   height=34, padx=14).pack(side="left", padx=(8, 0))
    TH.RoundButton(foot, text="📐 全部补三视图", command=gen_sheet3,
                   height=34, padx=14).pack(side="left", padx=(8, 0))
    TH.RoundButton(foot, text="关闭", command=w.destroy,
                   height=34, padx=18).pack(side="right")
    ttk.Label(foot, style="Hint.TLabel",
              text="三视图＝定妆照这张脸 ＋ 正面/侧面/背面全身（同一张图，绝不串脸）；约 1 分钟一张。"
              ).pack(side="left", padx=(10, 0))


# ============================ 场景照（跨镜锁地点） ============================
# 角色定妆照锁脸，场景照锁地点。同一场戏的多镜共用同一张场景照，
# 建筑布局/家具位置/光线方向跨镜才一致，不会看着像在换地方。
#
# 为什么做成"两件套"（照 + 统一文案），跟角色那边完全对称：
#   照锁大结构（布局、光位），文案锁小细节（材质、陈设、色调）。
#   光有照，模型重绘时小物件会漂；光有文案，布局又会乱。
#
# 场景照当 img2img 底图时，denoise 要比角色锚定低一点（0.45~0.55），
# 否则背景重绘得太狠，等于没有参考；这一镜的人脸则交给角色定妆照。

_SCENE_VIEWERS = []   # 打开着的场景库 render 函数；场景照生成完要回调刷新


def _save_scene_refs():
    """场景库落盘（跟角色库存在同一个 config.json 里）。"""
    try:
        c = dict(AP.CFG)
        c["scene_refs"] = dict(SCENE_REFS)
        c["scene_descs"] = dict(SCENE_DESCS)
        AP.save_config(c)
        AP.CFG["scene_refs"] = dict(SCENE_REFS)
        AP.CFG["scene_descs"] = dict(SCENE_DESCS)
    except Exception as e:
        print("[scene] 保存配置失败:", e)


def _refresh_scene_views():
    for f in list(_SCENE_VIEWERS):
        try:
            f()
        except Exception:
            try:
                _SCENE_VIEWERS.remove(f)
            except Exception:
                pass


def _gen_scene_photo(name, desc):
    """用 Z-Image 生成一张场景参考照（纯场景、不带人），存进 input 目录并绑上。"""
    def work():
        try:
            import gen_zimage_keys as Z
            Z.W, Z.H = 1024, 1024
            prefix = "scene_%s_%s" % (re.sub(r"[^\w]", "", name)[:12] or "s",
                                      time.strftime("%H%M%S"))
            wf = Z.build_wf(desc, 770001 + len(SCENE_REFS) * 7919
                           + int(time.time()) % 97, prefix)
            pid = Z.submit(wf)
            if not pid:
                log_q.put("[场景照] %s 提交失败——后端在线吗？\n" % name)
                return
            log_q.put("[场景照] %s 已提交，生成中（约 1 分钟）…\n" % name)
            res = Z.wait(pid, prefix, timeout=1500)
            if res and os.path.exists(res):
                fn = prefix + ".png"
                shutil.copy2(res, os.path.join(AP.INPUTDIR, fn))
                SCENE_REFS[name] = fn
                _save_scene_refs()
                log_q.put("[场景照] %s 完成 -> input/%s\n" % (name, fn))
                root.after(0, _refresh_scene_views)
            else:
                log_q.put("[场景照] %s 生成超时或失败\n" % name)
        except Exception as e:
            log_q.put("[场景照] 出错: %s\n" % e)
    threading.Thread(target=work, daemon=True).start()


def _draft_scene_desc(name):
    """给一个还没写文案的场景，起草一句"这个地方长什么样"。

    保留这个旧名做别名 —— 真正的实现是 draft_scene_desc（按时间/天气/光/
    建筑/质感五类抽元素，比这里随便挑几个字准）。
    """
    return draft_scene_desc(name)


def open_scenes():
    """场景照管理：每个场景一张参考照 + 一段统一场景文案。"""
    w = tk.Toplevel(root)
    w.title("场景照 · 锁住跨镜同一个地方")
    TH.paint_toplevel(w)
    w.transient(root)
    center_win(w, 1000, 780, 880, 560)

    hd = ttk.Frame(w)
    hd.pack(anchor="w", fill="x", padx=16, pady=(14, 2))
    ttk.Label(hd, text="🏞 场景照", font=FONT_B).pack(side="left")
    cnt_lbl = ttk.Label(hd, style="Hint.TLabel", text="")
    cnt_lbl.pack(side="left", padx=(12, 0))

    ttk.Label(w, style="Hint.TLabel",
              text="同一场景的多镜共用这一张场景照 → 背景跨镜不会像在换地方。\n"
                   "两件套：① 场景照锁大结构（布局/光位）② 统一场景文案锁小细节（材质/陈设）。\n"
                   "场景从① 画面描述自动识别；识别到新场景后，在这里补一张照（或只写文案也行）。",
              wraplength=960, justify="left").pack(anchor="w", padx=16)

    body = ttk.Frame(w)
    body.pack(fill="both", expand=True, padx=16, pady=(10, 6))
    cvs = tk.Canvas(body, background=TH.BG, highlightthickness=0, bd=0, height=300)
    sb = ttk.Scrollbar(body, orient="vertical", command=cvs.yview)
    inner = ttk.Frame(cvs)
    win_id = cvs.create_window((0, 0), window=inner, anchor="nw")
    cvs.configure(yscrollcommand=sb.set)
    cvs.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    cvs.bind("<Configure>", lambda e: cvs.itemconfigure(win_id, width=e.width))
    inner.bind("<Configure>", lambda e: cvs.configure(scrollregion=cvs.bbox("all")))

    def render():
        for ch in inner.winfo_children():
            ch.destroy()
        if render not in _SCENE_VIEWERS:
            _SCENE_VIEWERS.append(render)
        # 场景清单 = 已绑定的 + 当前分镜里自动识别到但还没建库的
        names = list(SCENE_REFS.keys())
        for r in shot_rows:
            try:
                nm = guess_scene(r["scene"].get("1.0", "end") or "")
            except Exception:
                nm = ""
            if nm and nm not in names:
                names.append(nm)
        if not names:
            ttk.Label(inner, style="Hint.TLabel",
                      text="还没有场景。写好① 画面描述（含地点，如「客栈大堂」「长街」），"
                           "程序会自动认出场景；或点下面「➕ 新建场景」。"
                      ).pack(anchor="w", pady=6)
            try:
                cnt_lbl.configure(text="")
            except Exception:
                pass
            return
        try:
            cnt_lbl.configure(text="共 %d 个场景：%s　·　窗口放不下就用滚轮上下翻"
                              % (len(names), " / ".join(names[:6])))
        except Exception:
            pass
        for name in names:
            try:
                card = ttk.LabelFrame(inner, text="  " + name + "  ", padding=8)
                card.pack(fill="x", pady=6)
                strip = ttk.Frame(card)
                strip.pack(fill="x")

                # ── 左：场景照（锁布局用）
                leftc = ttk.Frame(strip)
                leftc.pack(side="left", anchor="n")
                fn = SCENE_REFS.get(name) or ""
                full = os.path.join(AP.INPUTDIR, fn) if fn else ""
                ph = _char_thumb(full, 140) if full and os.path.exists(full) else None
                if ph is not None:
                    tk.Label(leftc, image=ph, borderwidth=1, relief="solid",
                             background=TH.BG).pack()
                else:
                    ph2 = tk.Frame(leftc, width=140, height=140, background=TH.FIELD,
                                   highlightthickness=1, highlightbackground="#c9c9cf")
                    ph2.pack()
                    ph2.pack_propagate(False)
                    tk.Label(ph2, text="场景照\n还没生成", font=TH.FONT_XS,
                             bg=TH.FIELD, fg=TH.TEXT3, justify="center").pack(pady=58)
                ttk.Label(leftc, text="场景照 · 锁布局", foreground=TH.TEXT3,
                          font=TH.FONT_XS).pack(pady=(3, 0))

                # ── 右：文案 + 操作
                col = ttk.Frame(strip)
                col.pack(side="left", fill="both", expand=True, padx=(10, 0))
                desc = (SCENE_DESCS.get(name) or "").strip()
                ttk.Label(col, text=("文案：" + desc) if desc else
                          "文案：空 → 只锁布局，不锁材质细节",
                          foreground=(TH.TEXT3 if desc else TH.WARN),
                          font=TH.FONT_XS, wraplength=340,
                          justify="left").pack(anchor="w", pady=(0, 6))
                # 这个场景在当前分镜里被用了几镜 —— 让用户知道改一处会影响几镜
                used = 0
                for r in shot_rows:
                    try:
                        if guess_scene(r["scene"].get("1.0", "end") or "") == name:
                            used += 1
                    except Exception:
                        pass
                ttk.Label(col, text="当前分镜里有 %d 镜用这个场景" % used,
                          foreground=TH.TEXT3, font=TH.FONT_XS).pack(anchor="w",
                                                                  pady=(0, 6))
                rowb = ttk.Frame(col)
                rowb.pack(anchor="w")
                TH.RoundButton(rowb, text="🎬 生成场景照", height=28, padx=10,
                               command=lambda n=name: _gen_one(n)).pack(side="left")
                TH.RoundButton(rowb, text="✎ 改文案", height=28, padx=10,
                               command=lambda n=name: _edit_desc(n)).pack(
                    side="left", padx=(6, 0))
                TH.RoundButton(rowb, text="✕ 解绑", kind="danger", height=28, padx=10,
                               command=lambda n=name: _unbind(n)).pack(
                    side="left", padx=(6, 0))
                if fn and not os.path.exists(full):
                    ttk.Label(col, text="⚠ 场景照文件缺失：" + fn, foreground=TH.ERR,
                              font=TH.FONT_XS).pack(anchor="w", pady=(4, 0))
            except Exception as _e:
                ttk.Label(inner, text="⚠ %s 这张卡片没画出来：%s" % (name, _e),
                          foreground=TH.ERR).pack(anchor="w", pady=4)

    def _gen_one(n):
        desc = (SCENE_DESCS.get(n) or "").strip() or _draft_scene_desc(n)
        SCENE_DESCS[n] = desc
        _save_scene_refs()
        prompt = SCENE_DESC_TPL.format(
            name=n, desc=desc, light="自然光，柔和均匀布光")
        _gen_scene_photo(n, prompt)
        log_q.put("[场景照] %s 已提交，约 1 分钟出图\n" % n)

    def _edit_desc(n):
        cur = (SCENE_DESCS.get(n) or "").strip()
        v = simpledialog.askstring(
            "统一场景文案 · %s" % n,
            "这个场景的固定描述（这个场景每镜的关键帧都会原样贴上，锁材质/陈设/色调）：",
            initialvalue=cur, parent=w)
        if v is None:
            return
        SCENE_DESCS[n] = v.strip()
        _save_scene_refs()
        render()

    def _unbind(n):
        SCENE_REFS.pop(n, None)
        SCENE_DESCS.pop(n, None)
        _save_scene_refs()
        render()

    render()

    def add_from_file():
        p = filedialog.askopenfilename(
            title="选一张场景参考照（同一个地方的参考图）",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not p:
            return
        name = simpledialog.askstring(
            "场景名", "这个场景叫什么？\n（要和① 画面描述里的地点对得上，如「客栈大堂」）",
            parent=w)
        if not name:
            return
        name = name.strip()
        ext = os.path.splitext(p)[1].lower() or ".png"
        fn = "scene_%s_%s%s" % (time.strftime("%H%M%S"), abs(hash(name)) % 9973, ext)
        try:
            shutil.copy2(p, os.path.join(AP.INPUTDIR, fn))
        except Exception as e:
            messagebox.showerror("复制失败", str(e), parent=w); return
        SCENE_REFS[name] = fn
        if not (SCENE_DESCS.get(name) or "").strip():
            SCENE_DESCS[name] = _draft_scene_desc(name)
        _save_scene_refs()
        render()

    def new_scene():
        name = simpledialog.askstring(
            "新建场景", "场景名（如「客栈大堂」「长街」）：", parent=w)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        SCENE_DESCS.setdefault(name, _draft_scene_desc(name))
        _save_scene_refs()
        render()

    def gen_all():
        """一键补齐：所有还没有场景照的场景，全部排队生成。"""
        names = []
        for r in shot_rows:
            try:
                nm = guess_scene(r["scene"].get("1.0", "end") or "")
            except Exception:
                nm = ""
            if nm and nm not in names:
                names.append(nm)
        for nm in list(SCENE_REFS.keys()):
            if nm not in names:
                names.append(nm)
        todo = [n for n in names if not SCENE_REFS.get(n)]
        if not todo:
            if not messagebox.askyesno("场景照都有了",
                                       "全部场景都有场景照了。要按现在的文案重画一遍吗？",
                                       parent=w):
                return
            todo = names
        if not todo:
            messagebox.showinfo("没有场景", "先在① 画面描述里写上地点，或点「➕ 新建场景」。",
                                parent=w)
            return
        log_q.put("[场景照] 排队生成 %d 张：%s\n" % (len(todo), "、".join(todo)))
        for n in todo:
            _gen_one(n)

    foot = ttk.Frame(w)
    foot.pack(fill="x", padx=16, pady=(0, 14))
    TH.RoundButton(foot, text="➕ 从图片添加", command=add_from_file,
                   height=34, padx=14).pack(side="left")
    TH.RoundButton(foot, text="＋ 新建场景", command=new_scene,
                   height=34, padx=14).pack(side="left", padx=(8, 0))
    TH.RoundButton(foot, text="🎬 全部补场景照", command=gen_all,
                   height=34, padx=14).pack(side="left", padx=(8, 0))
    TH.RoundButton(foot, text="关闭", command=w.destroy,
                   height=34, padx=18).pack(side="right")
    ttk.Label(foot, style="Hint.TLabel",
              text="场景照＝这个地方的参考图，同场景所有镜共用；约 1 分钟一张。"
              ).pack(side="left", padx=(10, 0))


# ============================ 提示词公式（来自 4 集教程） ============================
PROMPT_HELP = """【一】画面描述公式（写「① 画面描述」就按这五段拼）
　　人物是谁 ＋ 在哪里 ＋ 在干什么 ＋ 细节（五官/发型/服装/光影） ＋ 风格
　　① 搭骨架：人物、场景、动作　② 加细节：五官、发型、服装、光影　③ 加风格词：定质感
　　避坑：
　　· 不要一次同时大改「动作＋服装＋场景」，最容易崩脸、手指畸形
　　· 人物光要和环境光统一（白天场景就别写黄昏，光影会割裂）
　　· 手部复杂动作最容易出现畸形手指，能避开就避开
　　· 细节越具体，AI 发挥空间越小，出图越接近你想的那张

【二】景别（「⑦ 景别」会自动识别，判得不对再手动改）
　　远景：环境开阔，人物只占一小块 → 开场、转场、交代这是哪儿
　　全景：人物全身入画 → 看清动作和环境的关系（打斗、独行）
　　中景：腰部以上 → 对话、动作展示，最常用
　　近景：胸部以上 → 表情、眼神，情绪推进
　　特写：整张脸 → 情绪爆发点，一集别超过 3~4 个
　　局部特写：手 / 剑 / 衣角 / 脚步 → 只拍细节，不出现人脸
　　一条片子要远近交替：老用中景会像 PPT，老用特写又会闷。
　　禁用：极特写、毛孔/眼球特写、超微距 —— 会让人脸变形、五官错位
　　（程序按① 画面描述自动判：写了"特写/近景/中景…"直接认；
　　　没写就从内容反推 —— 拍指尖=局部特写、拍眼神=特写、拍打斗=全景。
　　　灰字写着"自动识别：xxx"，手动改过的不会被自动覆盖。）

【三】角度
　　俯拍：从上往下，人物显得渺小无助，适合交代场景布局
　　仰拍：从下往上，人物显得高大威严、有压迫感
　　平视：与人眼齐平，客观自然，看得清表情、有亲近感
　　禁用：荷兰角/倾斜/斜角镜头 —— AI 容易持续歪画面、人物扭曲，只适合 1~2 秒短镜头

【四】运镜（「⑤ 运镜」会自动填，点下拉可以改）
　　前推：镜头缓慢向前推进，主体逐渐放大，背景轻微虚化，画面稳定顺滑
　　后拉：镜头缓慢向后拉远，不断展现周围环境，主体慢慢变小，运镜平稳
　　环绕：镜头围绕主体缓慢圆弧环绕，主体保持中心，光影随角度轻微变化
　　升降：镜头缓慢垂直上升，逐步展现全景 / 镜头缓缓下降靠近人物
　　横移：镜头轻微横移，保持人物在画面中心，画面稳定顺滑（双人对白用）
　　原则：运镜跟景别走 —— 特写推近、全景拉远、局部特写定住、打戏手持
　　注意：不要写「360°环绕」，画面会扭曲

【五】锁脸（人物永不换人 = 三件套）
　　① 一张精挑的定妆照：必须完整正脸、五官清晰、看着顺眼（歪脸/侧脸/挡脸/糊脸都不要）
　　② 每镜都带参考图（本软件：『⑥ 参考角色』必选，关键帧按定妆照 img2img 重绘）
　　③ 全程统一文案，一字不改（本软件：『🎭 角色定妆照』→「✎ 改文案」，每镜关键帧都会原样贴上）
　　参考图只锁五官；发型、衣袍、气质靠统一文案锁。两件套一起上才锁得死。

【五·五】锁场景（地点永不换 = 同样两件套）
　　锁脸只管人不管地方：同一间客栈，不挂场景照的话每镜背景都不同，
　　观众看着就像在换地方 —— 这是比换脸更劝退的穿帮。
　　① 一张场景照：这个地方的样子（同场景所有镜共用）
　　② 一段统一场景文案：这个地方的材质/陈设/色调（每镜原样贴上）
　　本软件：导入新剧本后自动认场景、自动写文案、自动生成场景照、自动绑到每镜；
　　分镜栏的『⑧ 场景』可以手动改，管理在『🏞 场景照』。
　　有角色时，引擎会把「场景照 + 角色定妆照」拼成一张底图再重绘 ——
　　地点和脸一起锁住（img2img 只有一个起点图，只能拼）。

【六】打斗镜头（一场戏 = 4 个节拍，不是一镜打到底）
　　写「两人激烈打斗」，出来往往是一团看不清的肢体 —— 拆成四个节拍，一拍一镜：
　　　① 起手：谁先动手、手往哪去（中景，机位不动，先让人看清距离）
　　　② 接触：打到的那一下（近景 / 局部特写，动作放慢，别跳接）
　　　③ 反制：被压的一方怎么翻回来（近景，手持跟着动）
　　　④ 收势：谁站着谁倒下（全景，拉远收气）
　　写法要点：
　　· 动词要能拍：不写「激烈交锋」，写「反手扣住手腕、往下压、膝盖顶上去」
　　· 一镜一动作：模型一镜只扛得住一个连贯动作，写三个会变成三个人在动
　　· 越轴：两人对打，镜头始终站同一条轴线的一侧；要换边先用一个中性镜头过渡
　　· 强弱靠机位：被压制→俯拍；反制成功→仰拍（本软件「🎬 自动导演」已按这套判）
　　· 打戏一律手持跟拍，别用固定机位，也别写 360° 环绕（画面会扭曲）
　示例（第 3 镜 · 反制）：
　　雨夜长街，林砚左臂被周嵩反扣，被压得单膝跪地（近景，略俯）；
　　他右手摸到桌角猛地往上一顶，周嵩吃痛松手、后退半步（手持跟拍，轻微晃动）；
　　人物居中，动作方向（右上）留出空间

【七】剪辑节奏（成片阶段）
　　① 先把片段按分镜顺序排好，不加转场/特效/音乐，静音看一遍——光看画面能不能看懂故事
　　② 转场三原则：同场景剧情连贯→硬切；时间/场景换了→淡入淡出；情绪转折→快速闪黑
　　③ 卡点：只卡关键切换点，「重拍切画面，轻拍做衔接」
　　别每个转场都炫特效——转场是为剧情服务的，不是抢戏的。
"""


# ============================ 运镜库（48 条 · 完整版） ============================
# 来源：那份「AI 漫剧运镜提示词模板（完整版）」的 8 组 48 条，提示词正文按原文照抄，
# 只补了最后一列"什么镜头用"（原文没有，是我按每条的运动特征加的）。
# 为什么值得做成库：⑤ 运镜原本只有 9 个下拉预设，翻来覆去就那几句，
# 全片镜头语言长得一样。这里点一条就写进当前镜。
# 每条：(分组, 名称, 提示词正文, 什么镜头用)
CAMERA_MOVES = [
    ("固定与微动", "固定机位", "镜头保持完全静止，机位与焦距都不动，只有画面内的人物动作、光影和气流在变化", "对话、留白、让人物自己动"),
    ("固定与微动", "呼吸感微推", "镜头以极慢的速度向前微微推进，幅度很小，几乎难以察觉，像人在不自觉地屏息靠近", "情绪酝酿、克制"),
    ("固定与微动", "轻微手持", "手持摄影机拍摄，画面带有轻微自然的呼吸式晃动，不是剧烈抖动", "紧张、纪录片感"),
    ("固定与微动", "剧烈晃动", "镜头剧烈晃动，画面大幅抖动，像摄影机被人撞到或处于震动环境中", "打斗、撞击、灾难"),
    ("推拉镜头", "推镜头", "镜头缓慢向前推进，从中景逐渐靠近主体，最终停在面部特写或关键细节上", "情绪推进、盯住"),
    ("推拉镜头", "急推 / 撞击推", "镜头在极短时间内猛然推近到主体面部，速度极快，带强烈冲击", "震惊、爆发前一瞬"),
    ("推拉镜头", "拉镜头", "镜头缓慢向后拉远，从主体特写逐渐拉开，露出他周围完整环境和处境", "收尾、孤独、离别"),
    ("推拉镜头", "急拉揭示", "镜头从特写瞬间拉远到全景，快速揭示主体所处的真实环境", "反转、揭穿"),
    ("推拉镜头", "光学变焦推", "机位保持不动，焦距发生变化，画面被压缩着拉近，透视关系不变", "人群里盯人、压缩感"),
    ("推拉镜头", "希区柯克变焦", "镜头向前推进的同时反向变焦，主体在画面中大小不变，背景被急剧拉伸或压缩", "眩晕、失重、震惊"),
    ("推拉镜头", "前景遮挡揭示", "镜头缓慢横移，让前景的遮挡物（门框、人影、树叶）滑开，露出后方的主体", "发现、窥视"),
    ("摇镜与俯仰", "水平摇镜", "镜头机位不动，镜头横向匀速转动，从左到右扫过整个场景", "开场交代、找人"),
    ("摇镜与俯仰", "甩镜 / 急摇", "镜头以极快速度横向甩动，中间画面产生强烈运动模糊和拖影", "突发、被打断"),
    ("摇镜与俯仰", "垂直摇上", "镜头机位不动，镜头自下而上缓缓抬起，从脚部或地面升到头部或天空", "登场、压迫感"),
    ("摇镜与俯仰", "垂直摇下", "镜头自上而下缓缓压低，从天空或高处落到地面的人物或细节", "从环境落到人"),
    ("摇镜与俯仰", "摇镜落幅定人", "镜头横向摇过去，最终稳稳停在某人或某物上并定住", "锁定目标、揭示"),
    ("平移与跟随", "横移镜头", "镜头整体平行横向移动，始终保持与主体相同的距离和角度", "展示服装、并肩走"),
    ("平移与跟随", "侧面跟拍", "镜头跟随移动中的主体同步平移，始终保持主体在画面中的位置和大小", "赶路、并行对话"),
    ("平移与跟随", "背后跟拍", "镜头始终跟在主体背后，拍摄他的背影和前方未知的道路", "潜行、进入陌生地方"),
    ("平移与跟随", "前置倒退跟拍", "镜头在主体前方一边后退一边拍摄，正对主体的表情和步伐", "边走边说、逼近"),
    ("平移与跟随", "载具跟拍", "镜头固定在车辆或载具上，随载具一起高速移动拍摄", "车戏、追逃"),
    ("升降镜头", "升镜头", "镜头整体垂直向上升起，画面从局部逐渐展开为环境全景", "结尾、命运感"),
    ("升降镜头", "降镜头", "镜头从高空垂直下降，从俯瞰全景逐渐落到地面的人物身上", "先交代全貌，再落到人"),
    ("升降镜头", "摇臂大范围", "镜头在摇臂上做大幅度复合运动，边升边横移，跨越整个场景", "开场调度、大场面"),
    ("升降镜头", "天顶俯拍", "镜头在主体正上方垂直俯视，画面呈现自上而下的平面布局感", "布局、无助、战术"),
    ("环绕镜头", "半环绕", "镜头以主体为圆心做弧线运动，水平环绕约半圈，始终正对主体", "对峙、展示全貌"),
    ("环绕镜头", "360 度环绕", "镜头围绕主体完整旋转一圈，全方位展示主体", "慎用：这套渲染实测会扭曲"),
    ("环绕镜头", "螺旋上升环绕", "镜头环绕主体的同时持续升高，形成螺旋上升的运动轨迹", "高潮、仪式感"),
    ("环绕镜头", "低角度贴地环绕", "镜头贴近地面，以极低的角度围绕主体旋转", "压迫、英雄感"),
    ("视角与 POV", "第一人称 POV", "镜头即角色的眼睛，观众看到的就是角色正在看的东西", "代入、恐惧"),
    ("视角与 POV", "过肩镜头", "镜头越过前景人物的肩膀，拍摄对面的人物，前景肩膀占画面一侧", "对峙、谈判"),
    ("视角与 POV", "FPV 穿越", "镜头像穿越机一样高速飞行，穿过狭窄空间后豁然展开", "炫技、快速转场"),
    ("视角与 POV", "航拍", "镜头在高空以无人机视角飞行俯瞰地面", "大场面、地理位置"),
    ("视角与 POV", "微距镜头", "镜头以极近距离拍摄微小对象，背景高度虚化，细节被极度放大", "细节、道具、伏笔"),
    ("视角与 POV", "镜面反射视角", "镜头不直接拍主体，而是拍摄镜面、水面或玻璃中的倒影", "自我审视、暗示"),
    ("风格化与特殊", "荷兰角", "画面整体倾斜，地平线不再水平，构图失衡", "失衡、不安"),
    ("风格化与特殊", "镜头翻滚", "镜头沿光轴顺时针或逆时针旋转，画面整体翻转", "失控、坠落"),
    ("风格化与特殊", "子弹时间", "镜头围绕处于慢动作中的主体环绕，时间与运动形成强烈反差", "关键一击、绝境"),
    ("风格化与特殊", "焦点转移", "机位不动，画面焦点从前景人物转移到后景人物", "另一个人在场、暗示"),
    ("风格化与特殊", "一镜到底", "镜头连续运动不切断，跟随主体穿过多个空间完成一段完整叙事", "连续感、长镜头"),
    ("风格化与特殊", "运动模糊慢门", "画面带有明显的运动拖影和拖尾，像是慢门拍摄", "速度、混乱"),
    ("风格化与特殊", "抽离式拉远", "镜头持续拉远，主体在画面中越来越小，最终被环境完全吞没", "余味、被吞没"),
    ("风格化与特殊", "穿物转场", "镜头穿过某个物体表面（水面、镜面、屏幕、洞口），进入另一个空间", "时间/空间跳跃"),
    ("风格化与特殊", "电梯视角", "镜头在狭小的垂直空间中随电梯一起上升或下降", "狭小空间、上升感"),
    ("风格化与特殊", "门缝窥视", "镜头透过半开的门缝或窗框向室内拍摄，画面被边框强烈限制", "偷看、秘密"),
    ("风格化与特殊", "摇臂俯冲", "镜头从高空快速俯冲下降，直奔地面的人物而去", "追逐、压迫"),
    ("风格化与特殊", "推近锁定", "镜头快速推近到主体后猛然定住，画面瞬间稳定", "定论、宣判"),
    ("风格化与特殊", "螺旋推进", "镜头一边旋转一边向前推进，形成螺旋钻入的运动轨迹", "眩晕、钻入"),
]
_CAM_GROUPS = []
for _g, _n, _t, _w in CAMERA_MOVES:
    if _g not in _CAM_GROUPS:
        _CAM_GROUPS.append(_g)


# ============================ 古装发型库（第一期 13 款） ============================
# 发型是三维结构，正面定妆照拍不到后脑的髻，所以真正决定"这个人物是谁"的一大半在头发上。
# 这里把「古装女主发型（第一期）」的 13 条直接做成可选清单：
# 挑一款 → 追加到某个角色的外貌文案（CHAR_DESCS）→ 定妆照和之后每一镜的关键帧都会带上它。
# 每条：(名称, 完整提示词)  —— 追加时会自动去掉公共前缀「古装女子发型，」。
HAIR_STYLES = [
    ("抛家髻", "古装女子发型，抛家髻，乌黑长发偏分后向一侧斜拢，在头顶偏侧盘成蓬松高耸的发髻，"
              "髻身向一侧倾斜下垂如坠云，鬓发留长贴着脸颊两侧微微内卷，鬓边插一支点翠小簪与数朵细碎珠花，"
              "发丝蓬松微乱有空气感，黑发柔润有光泽"),
    ("低马尾", "古装女子发型，低马尾，乌黑长直发中分，全部头发松松拢至脑后颈窝，用一条素色缎带低低束成一束，"
              "发尾自然垂落至肩胛，额前与鬓边留几缕柔软碎发，鬓角别一朵素白小花，发丝柔顺服帖"),
    ("飞天髻", "古装女子发型，飞天髻，乌黑长发全部向上梳起，分成三股在头顶盘成高耸上扬的环状发髻，"
              "髻形如飞天飘举，髻上系两条轻薄青白飘带垂落肩后，插金累丝小钗与珍珠步摇，发髻饱满高耸，发丝顺滑"),
    ("垂耳发", "古装女子发型，垂耳发，乌黑长发中分，两侧各留出一缕长及锁骨的发丝自然垂在耳前，"
              "其余头发在脑后松松挽成低髻，耳侧以细小珠花夹固定，半垂半挽，发丝柔软服帖"),
    ("牡丹头", "古装女子发型，牡丹头，乌黑长发全部梳起，在头顶盘成饱满浑圆的圆形大发髻，形如盛开的牡丹，"
              "发髻四周层层插满金牡丹花钿、点翠簪与垂珠步摇，额前贴花钿，发髻厚重华丽，金饰繁复"),
    ("双垂髻", "古装女子发型，双垂髻，乌黑长发中分，两侧各绾一个圆髻垂在肩前耳下，"
              "髻根用浅色发带系成蝴蝶结，带尾垂落，髻上点缀小朵珠花，发丝柔顺"),
    ("一字髻", "古装女子发型，一字髻，乌黑长发全部向后梳拢，在脑后偏上盘成扁平横长的一字形发髻，"
              "髻面平滑如带，仅用一支素白玉簪横穿固定，鬓发服帖，整体干净利落"),
    ("流苏髻", "古装女子发型，流苏髻，乌黑长发高盘成髻，髻后垂下一束长发编成的流苏状长尾，长及腰际，"
              "髻上插金步摇与长流苏簪，珠串垂落轻晃，发丝黑亮"),
    ("惊鸿髻", "古装女子发型，惊鸿髻，乌黑长发全部向上梳起，在头顶盘成高耸轻盈的飞鸟展翅状发髻，"
              "髻形上扬舒展，两侧发丝微微外扬，插细长金钗与羽毛状发饰，发丝轻盈有空气感"),
    ("双丫髻", "古装女子发型，双丫髻，乌黑长发中分，两侧各绾一个小圆髻高高立于耳侧上方，形如双丫，"
              "髻根缠红绳系结，发间插几朵粉色小野花，额前留空气刘海，发丝蓬松俏皮"),
    ("垂鬟髻", "古装女子发型，垂鬟髻，乌黑长发上半部梳起在头顶挽成中空环形发髻，下半部自然垂落肩背，"
              "发尾微卷，鬓边插一支珍珠步摇，发丝柔顺有光泽"),
    ("高螺髻", "古装女子发型，高螺髻，乌黑长发全部向上梳起，盘旋成螺旋状的高髻立于头顶，层层盘绕如螺壳，"
              "一支白玉簪横插固定，髻身紧实饱满，鬓发一丝不乱"),
    ("半披发", "古装女子发型，半披发，乌黑长发上半部在脑后松松束起挽成小髻，一支羊脂玉簪横穿固定，"
              "下半部长发自然披散垂至腰际，发丝顺滑如瀑，额前留几缕碎发"),
]


def _hair_brief(text):
    """去掉公共前缀「古装女子发型，」，留下能直接接在外貌文案后面的部分。"""
    t = (text or "").strip()
    for pre in ("古装女子发型，", "古装女子发型,", "古装女子发型："):
        if t.startswith(pre):
            return t[len(pre):]
    return t


# ============================ 景别库（10 条） ============================
# 来源：那份「10 个最常用景别提示词」。前 6 条正好对应 ⑦ 景别 的下拉（远景/全景/中景/近景/特写/局部特写），
# 后 4 条是**构图型**（过肩 / 双人 / 主观 / 插入），下拉里没有，所以走"拼到画面描述末尾"。
# 每条：(名称, 提示词, 对应下拉项 或 None, 什么镜头用)
SHOT_SIZES = [
    ("大远景", "极远景，大远景，主体在画面远处只是一个很小的人影，环境占据画面百分之九十以上，"
              "定场镜头，宏大尺度感，24mm广角镜头，大景深全景清晰，电影感",
     "远景", "开场定场、荒原、大场面"),
    ("远景 / 全景", "远景，全景，全身镜头，主体站在场景中，人物从头到脚完整入画，"
                   "头顶留出留白，脚下留出地面，环境清晰可辨，35mm镜头，透视自然不变形",
     "全景", "交代动作与环境的关系"),
    ("中景", "中景，半身镜头，镜头从主体腰部以上取景，双手完整出现在画面内，背景微微虚化，"
            "50mm标准镜头，透视接近人眼", "中景", "对白、常规叙事"),
    ("中近景", "中近景，胸部以上取景，画面里有主体的头部、肩膀和上半胸，看不到手，"
              "85mm中长焦镜头，背景柔和虚化", "近景", "从对白往特写收"),
    ("近景", "近景，特写半身，头部和肩膀入画，脸部占画面大部分，头顶略微出画，"
            "背景重度虚化，105mm中长焦镜头，浅景深，保留真实皮肤质感", "近景", "情绪开始收紧"),
    ("特写 / 大特写", "特写，主体整张脸从边框到边框铺满画面，额头和下巴贴近画面上下边缘，"
                    "背景完全虚化，135mm长焦镜头，极浅景深，真实皮肤纹理，可见毛孔，"
                    "眼中有柔和眼神光", "特写", "情绪顶点、沉默"),
    ("过肩镜头", "过肩镜头，画面左侧前景是一个人的肩膀和后脑勺，虚化不清，主体在画面中央面向镜头，"
                "取景到胸部以上，两个人处在同一个空间里，50mm镜头，浅景深",
     None, "对峙、谈判（拼进画面描述）"),
    ("双人 / 多人镜头", "双人镜头，人物A和人物B并排站在场景中，两人从膝盖以上完整入画，"
                     "面对面交谈，两人之间留出空隙，35mm镜头，两张脸都在焦点上",
     None, "同框对话（拼进画面描述）"),
    ("主观镜头 / 第一人称", "主观镜头，第一人称视角，主体的双手从画面下方伸入画面，伸向目标物，"
                        "画面就是角色眼睛看到的东西，轻微的手持晃动，24mm镜头，自然光",
     None, "代入、发现（拼进画面描述）"),
    ("插入镜头 / 空镜", "插入镜头，一件物品在台面上的特写，画面里没有人，背景环境柔和虚化，"
                     "暖色方向性光线斜切过台面，50mm镜头，浅景深，电影感静物",
     "局部特写", "伏笔、转场、空镜"),
]


def open_size_lib(rec=None):
    """景别库：10 条景别/构图提示词。能对应下拉的写进 ⑦，不能的拼到画面描述末尾。"""
    rec = rec or (_row_of(_DET["i"]) if _DET.get("i", -1) >= 0 else None)
    w = tk.Toplevel(root)
    w.title("景别库 · 10 个最常用景别提示词")
    TH.paint_toplevel(w)
    w.transient(root)
    center_win(w, 900, 700, 820, 600)

    head = tk.Frame(w, background=TH.PANEL)
    head.pack(fill="x", padx=14, pady=(12, 4))
    tk.Label(head, text="🎬 景别库", background=TH.PANEL, foreground=TH.TEXT,
             font=(TH.FONT[0], 12, "bold")).pack(side="left")
    tk.Label(head, text="  能对上 ⑦ 的直接写进景别；过肩/双人/主观/插入 会拼到画面描述末尾",
             background=TH.PANEL, foreground=TH.TEXT3,
             font=(TH.FONT[0], 9)).pack(side="left")

    sr = tk.Frame(w, background=TH.PANEL)
    sr.pack(fill="x", padx=14, pady=(2, 6))
    kw = tk.StringVar()
    ent = ttk.Entry(sr, textvariable=kw, font=FONT)
    ent.pack(fill="x")

    body = tk.Frame(w, background=TH.PANEL)
    body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
    cv, inner = _ue_scroller(body)

    state = {"sel": None, "rows": []}
    preview = tk.StringVar(value="")

    def _use(entry):
        nm, txt, preset, _when = entry
        if rec is None:
            messagebox.showinfo("先选一镜", "先在左边大纲选一镜，再用景别。", parent=w)
            return
        try:
            if preset:
                rec["size_var"].set(preset)
                rec["size_manual"][0] = True
                try:
                    rec["size_hint_lbl"].configure(text="来自景别库：%s" % nm)
                except Exception:
                    pass
                msg = "第 %d 镜景别 → %s（%s）" % (_DET.get("i", -1) + 1, preset, nm)
            else:
                cur = (rec["scene"].get("1.0", "end") or "").strip()
                rec["scene"].set((cur + "，" if cur else "") + txt)
                msg = "第 %d 镜画面描述已拼上「%s」" % (_DET.get("i", -1) + 1, nm)
        except Exception as e:
            messagebox.showerror("写入失败", str(e), parent=w)
            return
        log_q.put("[景别库] %s\n" % msg)
        set_status(msg, "ok")
        # 表单里 ① 是重建时读的，改完重新画一遍当前镜
        try:
            _detail_show(_DET["i"])
        except Exception:
            pass

    def _pick(row, entry):
        if state["sel"] is not None:
            try:
                state["sel"].configure(background=TH.PANEL)
            except Exception:
                pass
        state["sel"] = row
        try:
            row.configure(background=TH.SEL_BG)
        except Exception:
            pass
        preview.set("%s —— %s" % (entry[0], entry[1]))

    def _build():
        for c in inner.winfo_children():
            c.destroy()
        state["sel"] = None
        q = (kw.get() or "").strip().lower()
        for i, entry in enumerate(SHOT_SIZES, 1):
            nm, txt, preset, when = entry
            if q and q not in (nm + txt + when).lower():
                continue
            row = tk.Frame(inner, background=TH.PANEL, cursor="hand2")
            row.pack(fill="x", padx=6, pady=1)
            t1 = tk.Label(row, text="%02d %s" % (i, nm), background=TH.PANEL,
                          foreground=TH.TEXT, font=(TH.FONT[0], 9, "bold"),
                          anchor="w", width=19)
            t1.pack(side="left", padx=(6, 2), pady=3)
            t2 = tk.Label(row, text=txt, background=TH.PANEL, foreground=TH.TEXT2,
                          font=(TH.FONT[0], 9), anchor="w", justify="left",
                          wraplength=520)
            t2.pack(side="left", fill="x", expand=True, pady=3)
            tag = ("→ " + preset) if preset else "拼描述"
            t3 = tk.Label(row, text=tag, background=TH.PANEL,
                          foreground=(TH.ACCENT if preset else TH.TEXT3),
                          font=(TH.FONT[0], 8), anchor="e")
            t3.pack(side="right", padx=(4, 8))
            for wd in (row, t1, t2, t3):
                wd.bind("<Button-1>", lambda e, r=row, en=entry: _pick(r, en))
                wd.bind("<Double-Button-1>", lambda e, en=entry: (_use(en), w.destroy()))
            state["rows"].append((row, entry))
        if not state["rows"]:
            tk.Label(inner, text="没找到，换个词试试（比如「特写」「过肩」）",
                     background=TH.PANEL, foreground=TH.TEXT3,
                     font=(TH.FONT[0], 9)).pack(pady=20)

    kw.trace_add("write", lambda *a: _build())
    _build()

    pv = tk.Label(w, textvariable=preview, background=TH.PANEL, foreground=TH.TEXT2,
                  font=(TH.FONT[0], 9), anchor="w", justify="left", wraplength=840)
    pv.pack(fill="x", padx=16, pady=(0, 4))

    foot = tk.Frame(w, background=TH.PANEL)
    foot.pack(fill="x", padx=14, pady=(0, 12))

    def _sel():
        for r, en in state["rows"]:
            if r is state["sel"]:
                return en
        return None

    def _use_sel():
        en = _sel()
        if not en:
            messagebox.showinfo("还没选", "先点一条景别。", parent=w)
            return
        _use(en)
        w.destroy()

    TH.RoundButton(foot, text="✔ 用到这一镜", command=_use_sel, kind="accent",
                   height=32, padx=14).pack(side="left")
    TH.RoundButton(foot, text="⧉ 复制提示词",
                   command=lambda: (root.clipboard_clear(),
                                    root.clipboard_append(preview.get().split(" —— ", 1)[-1]),
                                    set_status("景别提示词已复制", "ok")),
                   height=32, padx=12).pack(side="left", padx=(8, 0))
    TH.RoundButton(foot, text="关闭", command=w.destroy, height=32,
                   padx=16).pack(side="right")
    ent.focus_set()
    try:
        w.grab_set()
    except Exception:
        pass


def open_hair_lib(name=None):
    """古装发型库：挑一款 → 追加到某个角色的外貌文案（定妆照 + 每镜关键帧都会带上）。"""
    w = tk.Toplevel(root)
    w.title("古装发型库 · 第一期 13 款")
    TH.paint_toplevel(w)
    w.transient(root)
    center_win(w, 900, 720, 820, 620)

    head = tk.Frame(w, background=TH.PANEL)
    head.pack(fill="x", padx=14, pady=(12, 4))
    tk.Label(head, text="💇 古装发型库", background=TH.PANEL, foreground=TH.TEXT,
             font=(TH.FONT[0], 12, "bold")).pack(side="left")
    tk.Label(head, text="  挑一款 → 追加到角色的外貌文案（定妆照和每镜关键帧都会带上）",
             background=TH.PANEL, foreground=TH.TEXT3,
             font=(TH.FONT[0], 9)).pack(side="left")

    sr = tk.Frame(w, background=TH.PANEL)
    sr.pack(fill="x", padx=14, pady=(2, 6))
    kw = tk.StringVar()
    ent = ttk.Entry(sr, textvariable=kw, font=FONT)
    ent.pack(fill="x")

    # 追加给谁：默认取当前镜的「⑥ 参考角色」
    tgt = tk.StringVar()
    _def = name
    if not _def:
        try:
            _i = _DET.get("i", -1)
            if _i >= 0:
                _r = _row_of(_i)
                if _r is not None:
                    _def = (_r["ref_var"].get() or "").strip()
        except Exception:
            _def = None
    if _def not in CHAR_REFS:
        _def = (list(CHAR_REFS)[0] if CHAR_REFS else "")
    tgt.set(_def)

    tr = tk.Frame(w, background=TH.PANEL)
    tr.pack(fill="x", padx=14, pady=(0, 6))
    tk.Label(tr, text="追加给角色：", background=TH.PANEL, foreground=TH.TEXT2,
             font=(TH.FONT[0], 9)).pack(side="left")
    ttk.Combobox(tr, textvariable=tgt, values=(list(CHAR_REFS) or ["（还没有角色）"]),
                 font=FONT, width=16).pack(side="left")
    tk.Label(tr, text="（没有的话先在「🎭 角色定妆照」里建一个）", background=TH.PANEL,
             foreground=TH.TEXT3, font=(TH.FONT[0], 8)).pack(side="left", padx=(6, 0))

    body = tk.Frame(w, background=TH.PANEL)
    body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
    cv, inner = _ue_scroller(body)

    state = {"sel": None, "rows": []}
    preview = tk.StringVar(value="")

    def _append(text, regen=False):
        nm = (tgt.get() or "").strip()
        if nm not in CHAR_REFS:
            messagebox.showinfo("先选角色", "先在「追加给角色」里选一个已有角色。", parent=w)
            return
        rest = _hair_brief(text)
        old = (CHAR_DESCS.get(nm) or "").strip()
        CHAR_DESCS[nm] = (old + ("，" if old else "") + rest)[:600]
        try:
            _save_char_refs()
        except Exception as e:
            messagebox.showerror("保存失败", str(e), parent=w)
            return
        log_q.put("[发型] %s 的外貌文案已追加：%s\n" % (nm, rest[:24]))
        set_status("发型已写进「%s」的外貌文案" % nm, "ok")
        if regen:
            try:
                desc = CHAR_DESC_TPL.format(who=nm, look=CHAR_DESCS[nm])
                _gen_char_sheet(nm, desc, kind="portrait")
                log_q.put("[定妆照] %s 按新发型重出中…\n" % nm)
            except Exception as e:
                messagebox.showerror("重出定妆照失败", str(e), parent=w)
        try:
            _refresh_char_views()
        except Exception:
            pass

    def _pick(row, text_name, text):
        if state["sel"] is not None:
            try:
                state["sel"].configure(background=TH.PANEL)
            except Exception:
                pass
        state["sel"] = row
        try:
            row.configure(background=TH.SEL_BG)
        except Exception:
            pass
        preview.set("%s —— %s" % (text_name, text))

    def _build():
        for c in inner.winfo_children():
            c.destroy()
        state["sel"] = None
        q = (kw.get() or "").strip().lower()
        for i, (nm, txt) in enumerate(HAIR_STYLES, 1):
            if q and q not in (nm + txt).lower():
                continue
            row = tk.Frame(inner, background=TH.PANEL, cursor="hand2")
            row.pack(fill="x", padx=6, pady=1)
            t1 = tk.Label(row, text="%02d %s" % (i, nm), background=TH.PANEL,
                          foreground=TH.TEXT, font=(TH.FONT[0], 9, "bold"),
                          anchor="w", width=12)
            t1.pack(side="left", padx=(6, 2), pady=3)
            t2 = tk.Label(row, text=_hair_brief(txt), background=TH.PANEL,
                          foreground=TH.TEXT2, font=(TH.FONT[0], 9), anchor="w",
                          justify="left", wraplength=640)
            t2.pack(side="left", fill="x", expand=True, pady=3)
            for wd in (row, t1, t2):
                wd.bind("<Button-1>", lambda e, r=row, n=nm, t=txt: _pick(r, n, t))
                wd.bind("<Double-Button-1>", lambda e, t=txt: (_append(t), w.destroy()))
            state["rows"].append((row, nm, txt))
        if not state["rows"]:
            tk.Label(inner, text="没找到，换个词试试（比如「髻」「马尾」）",
                     background=TH.PANEL, foreground=TH.TEXT3,
                     font=(TH.FONT[0], 9)).pack(pady=20)

    kw.trace_add("write", lambda *a: _build())
    _build()

    pv = tk.Label(w, textvariable=preview, background=TH.PANEL, foreground=TH.TEXT2,
                  font=(TH.FONT[0], 9), anchor="w", justify="left", wraplength=840)
    pv.pack(fill="x", padx=16, pady=(0, 4))

    foot = tk.Frame(w, background=TH.PANEL)
    foot.pack(fill="x", padx=14, pady=(0, 12))

    def _sel_text():
        for r, n, t in state["rows"]:
            if r is state["sel"]:
                return t
        return None

    def _use(regen=False):
        t = _sel_text()
        if not t:
            messagebox.showinfo("还没选", "先点一款发型。", parent=w)
            return
        _append(t, regen=regen)
        if not regen:
            w.destroy()

    TH.RoundButton(foot, text="✔ 写进外貌文案", command=lambda: _use(False),
                   kind="accent", height=32, padx=14).pack(side="left")
    TH.RoundButton(foot, text="✔ 写入并重出定妆照", command=lambda: _use(True),
                   height=32, padx=12).pack(side="left", padx=(8, 0))
    TH.RoundButton(foot, text="⧉ 复制提示词",
                   command=lambda: (root.clipboard_clear(),
                                    root.clipboard_append(preview.get().split(" —— ", 1)[-1]),
                                    set_status("发型提示词已复制", "ok")),
                   height=32, padx=12).pack(side="left", padx=(8, 0))
    TH.RoundButton(foot, text="关闭", command=w.destroy, height=32,
                   padx=16).pack(side="right")
    ent.focus_set()
    try:
        w.grab_set()
    except Exception:
        pass


def open_motion_lib(rec=None):
    """运镜库：48 条能直接进 prompt 的运镜写法，点一下写进当前镜的 ⑤。"""
    rec = rec or (_row_of(_DET["i"]) if _DET.get("i", -1) >= 0 else None)
    w = tk.Toplevel(root)
    w.title("运镜库 · 48 条电影级运镜提示词")
    TH.paint_toplevel(w)
    w.transient(root)
    center_win(w, 900, 720, 820, 620)

    head = tk.Frame(w, background=TH.PANEL)
    head.pack(fill="x", padx=14, pady=(12, 4))
    tk.Label(head, text="🎥 运镜库", background=TH.PANEL, foreground=TH.TEXT,
             font=(TH.FONT[0], 12, "bold")).pack(side="left")
    tk.Label(head, text="  点一条 → 写进当前选中镜的「⑤ 运镜」（会标成手动指定）",
             background=TH.PANEL, foreground=TH.TEXT3,
             font=(TH.FONT[0], 9)).pack(side="left")

    sr = tk.Frame(w, background=TH.PANEL)
    sr.pack(fill="x", padx=14, pady=(2, 6))
    kw = tk.StringVar()
    ent = ttk.Entry(sr, textvariable=kw, font=FONT)
    ent.pack(fill="x")

    body = tk.Frame(w, background=TH.PANEL)
    body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
    cv, inner = _ue_scroller(body)
    _lbg = TH.PANEL

    state = {"sel": None, "rows": []}
    preview = tk.StringVar(value="")

    def _apply(text):
        if rec is None:
            messagebox.showinfo("先选一镜", "先在左边大纲选一镜，再挑运镜。", parent=w)
            return
        try:
            rec["motion_var"].set(text)
            rec["motion_manual"][0] = True
            _crash("[运镜库] 已写入第 %d 镜" % (_DET.get("i", -1) + 1))
        except Exception as e:
            messagebox.showerror("写入失败", str(e), parent=w)
            return
        try:      # 和景别那套一样，给一句人话提示
            rec["motion_hint_lbl"].configure(text="来自运镜库")
        except Exception:
            pass
        set_status("已把这条运镜写进第 %d 镜" % (_DET.get("i", -1) + 1), "ok")

    def _pick(row, name, text):
        if state["sel"] is not None:
            try:
                state["sel"].configure(background=_lbg)
            except Exception:
                pass
        state["sel"] = row
        try:
            row.configure(background=TH.SEL_BG)
        except Exception:
            pass
        preview.set("%s —— %s" % (name, text))

    def _build():
        for c in inner.winfo_children():
            c.destroy()
        state["sel"] = None
        q = (kw.get() or "").strip().lower()
        shown = 0
        for g in _CAM_GROUPS:
            items = [m for m in CAMERA_MOVES if m[0] == g]
            if q:
                items = [m for m in items
                         if q in (m[0] + m[1] + m[2] + m[3]).lower()]
            if not items:
                continue
            tk.Label(inner, text=g, background=TH.PANEL, foreground=TH.ACCENT,
                     font=(TH.FONT[0], 9, "bold"), anchor="w").pack(
                fill="x", padx=8, pady=(8, 2))
            for _g, name, text, when in items:
                row = tk.Frame(inner, background=_lbg, cursor="hand2")
                row.pack(fill="x", padx=6, pady=1)
                t1 = tk.Label(row, text=name, background=_lbg, foreground=TH.TEXT,
                              font=(TH.FONT[0], 9, "bold"), anchor="w", width=10)
                t1.pack(side="left", padx=(6, 2), pady=3)
                t2 = tk.Label(row, text=text, background=_lbg, foreground=TH.TEXT2,
                              font=(TH.FONT[0], 9), anchor="w", justify="left",
                              wraplength=520)
                t2.pack(side="left", fill="x", expand=True)
                t3 = tk.Label(row, text=when, background=_lbg, foreground=TH.TEXT3,
                              font=(TH.FONT[0], 8), anchor="e")
                t3.pack(side="right", padx=(4, 8))
                for wd in (row, t1, t2, t3):
                    wd.bind("<Button-1>", lambda e, r=row, n=name, t=text: _pick(r, n, t))
                    wd.bind("<Double-Button-1>",
                            lambda e, n=name, t=text: (_apply(t), w.destroy()))
                state["rows"].append((row, name, text))
                shown += 1
        if not shown:
            tk.Label(inner, text="没找到匹配的运镜，换个词试试", background=TH.PANEL,
                     foreground=TH.TEXT3, font=(TH.FONT[0], 9)).pack(pady=20)

    kw.trace_add("write", lambda *a: _build())
    _build()

    pv = tk.Label(w, textvariable=preview, background=TH.PANEL, foreground=TH.TEXT2,
                  font=(TH.FONT[0], 9), anchor="w", justify="left", wraplength=840)
    pv.pack(fill="x", padx=16, pady=(0, 4))

    foot = tk.Frame(w, background=TH.PANEL)
    foot.pack(fill="x", padx=14, pady=(0, 12))

    def _use_sel():
        if state["sel"] is None:
            messagebox.showinfo("还没选", "先点一条运镜。", parent=w)
            return
        for r, n, t in state["rows"]:
            if r is state["sel"]:
                _apply(t)
                w.destroy()
                return

    TH.RoundButton(foot, text="✔ 用这一条", command=_use_sel, kind="accent",
                   height=32, padx=14).pack(side="left")
    TH.RoundButton(foot, text="⧉ 复制提示词",
                   command=lambda: (root.clipboard_clear(),
                                    root.clipboard_append(preview.get().split(" —— ", 1)[-1]),
                                    set_status("运镜提示词已复制", "ok")),
                   height=32, padx=12).pack(side="left", padx=(8, 0))
    TH.RoundButton(foot, text="关闭", command=w.destroy, height=32,
                   padx=16).pack(side="right")
    ent.focus_set()
    try:
        w.grab_set()
    except Exception:
        pass


def open_prompt_help():
    w = tk.Toplevel(root)
    w.title("提示词公式 · 4 集教程浓缩版")
    TH.paint_toplevel(w)
    w.transient(root)
    center_win(w, 860, 700, 780, 600)

    ttk.Label(w, text="📖 提示词公式 / 景别 / 角度 / 运镜 / 锁脸 / 节奏",
              font=FONT_B).pack(anchor="w", padx=16, pady=(14, 2))
    ttk.Label(w, style="Hint.TLabel",
              text="照着填就能出「像电影」的画面；常用的运镜已做成下拉，直接选。"
              ).pack(anchor="w", padx=16)

    body = tk.Frame(w)
    body.pack(fill="both", expand=True, padx=16, pady=(8, 6))
    txt = tk.Text(body, wrap="word", font=("Microsoft YaHei", 9))
    sb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    TH.paint_text(txt, pad=(10, 8))
    txt.insert("1.0", PROMPT_HELP)
    txt.configure(state="disabled")

    def copy_all():
        try:
            root.clipboard_clear()
            root.clipboard_append(PROMPT_HELP)
            log_q.put("[提示词] 已复制到剪贴板\n")
        except Exception as e:
            messagebox.showerror("复制失败", str(e), parent=w)

    foot = ttk.Frame(w)
    foot.pack(fill="x", padx=16, pady=(0, 14))
    TH.RoundButton(foot, text="⧉ 复制全部", command=copy_all,
                   height=32, padx=14).pack(side="left")
    TH.RoundButton(foot, text="关闭", command=w.destroy,
                   height=32, padx=18).pack(side="right")
    try:
        w.grab_set()
    except Exception:
        pass


# ============================ 生成 / 停止 ============================
class QW:
    def __init__(self, q): self.q = q
    def write(self, s):
        if s:
            self.q.put(s)
    def flush(self): pass


def set_gen_running(running, done=False):
    was = _GEN_RUNNING[0]
    _GEN_RUNNING[0] = bool(running)
    if running:
        gen_btn.config(state="disabled")
        stop_btn.config(state="normal", text="■ 停止")
        # 开跑就把底部面板切到「进度」页：进度条躲在看不见的标签页里等于没有
        try:
            _ue_show_prog_tab()
        except Exception:
            pass
        try:
            next_lbl.configure(text="正在生成…", foreground=TH.ACCENT)
            next_sub.configure(text="想停点右边「■ 停止」；停了再点开始会接着渲，渲完的镜跳过")
            next_btn.configure(text="■ 停止生成", command=stop_gen)
        except Exception:
            pass
        # 左侧一开始生成，右侧提示条变红加强警告（两边不能并行用同一后端）
        try:
            _web_conflict_lbl.configure(
                text="🔴 生成中：别在右边 ComfyUI 同时排队",
                foreground=TH.ERR)
        except Exception:
            pass
    else:
        gen_btn.config(state="normal")
        stop_btn.config(state="disabled", text="■ 停止")
        # 生成结束：右侧提示条恢复常驻琥珀色
        try:
            _web_conflict_lbl.configure(
                text="⚠ 左右共用后端：同时跑会冲突",
                foreground=TH.WARN)
        except Exception:
            pass
        # 只有真跑完（成片已落盘）才记「刚出片」。中途停掉也记的话，
        # 提示条会显示「✅ 这一版出片了，点右边看视频」，可其实什么都没合出来。
        if was and done:
            _DONE_AT[0] = time.time()
        # 收尾：清掉节点图上残留的「▶ 出图中 46%」，让它回到「✓ 已出图 / 未开始」
        try:
            (_STORY.get("live") or {}).clear()
        except Exception:
            pass
        try:
            _refresh_next_step(force=True)
        except Exception:
            pass


# 上一轮是「点停止」中断的 → 下次点开始就走续渲（已渲完的镜跳过）。
# 只在按下停止时置位；自然跑完 / 新剧本都不会继承，免得改了文案还复用旧帧。
_GEN_RESUME = [False]
# 等后端就绪的重试计数（点了开始但 8188 还没起来时用，别弹窗打断）
_GEN_WAIT = {"n": 0}
# 本轮起跑时间：进度条右侧的"已用/剩余"靠它算
_GEN_T0 = [0.0]


def generate():
    try:
        _AUTO_GO["armed"] = False     # 手动点「开始生成」＝接管，排队的自动出片作废
    except Exception:
        pass
    if not backend_ready():      # 用缓存判断，别在主线程里干等一个 3 秒的 HTTP
        # 后端正在自启（开机、或刚关过一次又打开）时**不要弹窗**：
        # 以前弹一个"请先点【启动后端】"，可它其实正在后台加载模型，
        # 用户看到的就是"又弹这个"。现在改成自动等，起来就开跑。
        _w = _GEN_WAIT["n"]
        if _w == 0:
            log_q.put("\n[等待] 后端还没就绪，正在自动启动，起来后自动开跑…\n")
            try:
                set_status("● 正在启动后端，起来后自动开跑", "warn")
            except Exception:
                pass
            try:
                if not check_online() and not backend_starting:
                    start_backend()
            except Exception:
                pass
        if _w < 90:                      # 最多等 3 分钟（2 秒一轮）
            _GEN_WAIT["n"] = _w + 1
            root.after(2000, generate)
            return
        _GEN_WAIT["n"] = 0
        messagebox.showwarning("后端还没起来",
            "等了 3 分钟还是连不上 8188。\n\n"
            "看一眼日志里有没有模型加载报错；或者点右上角【⚡ 启动后端】再试一次。")
        return
    _GEN_WAIT["n"] = 0
    shots = collect_shots()
    if not shots:
        messagebox.showwarning("空剧本", "请至少写一镜的『画面描述』和『旁白台词』。")
        return
    # 资产还在排队（新剧刚导入、后端刚起来，定妆照/场景照正在补）：
    # 这时候开跑，关键帧会用"没有定妆照"的脸，跨镜必换人。拦住，等它跑完。
    if _BACKEND_QUEUE:
        messagebox.showinfo(
            "先等资产补完",
            "刚导入的剧本还在自动补资产：%s\n\n"
            "定妆照/场景照生成完（界面上会有提示）再点生成，"
            "否则这一部会跨镜换脸、场景也会串。"
            % "、".join(lb for lb, _ in _BACKEND_QUEUE))
        return
    # 参考角色是必选项 —— 但只对「画面里有人」的镜头成立。
    # 纯空镜/环境镜（⑥ 填「空镜·无人物」）本来就没脸可锁，不需要定妆照：
    # 以前这里无条件拦，导入无人物剧本 → 8 秒后自动出片 → 弹这个框，
    # 无人值守直接断链（日志停在「资产齐了 → 开跑」就没下文了）。
    if not CHAR_REFS and _needs_char_refs():
        messagebox.showwarning("还没有角色定妆照",
            "这一部有镜头绑了角色，但角色库是空的。\n\n"
            "多半是剧本里没写「角色名：台词」，导入时没认出人物。\n"
            "点【🎭 角色】→「✦ 一键生成定妆照」（用文字描述直接生成），\n"
            "或「＋ 从图片添加」用已有的定妆照；\n"
            "整部都是空镜/环境镜的话，把那一镜的『⑥ 参考角色』选成「%s」就行。"
            % NOBODY)
        return
    if not CHAR_REFS:
        # 全片无人物：⑥ 还空着的统一按空镜处理，别再被下面的逐镜校验弹第二个框
        _n_nobody = 0
        for r in shot_rows:
            try:
                alive = (r["scene"].get("1.0", "end").strip()
                         or r["say_var"].get().strip())
            except Exception:
                alive = True
            if alive and not (r["ref_var"].get() or "").strip():
                r["ref_var"].set(NOBODY)
                _n_nobody += 1
        if _n_nobody:
            try:
                _update_char_stat()
            except Exception:
                pass
            shots = collect_shots()   # 上面那份快照是在改 ⑥ 之前取的，必须刷新
        log_q.put("[校验] 本片没有人物镜（⑥ 全是「%s」），不需要角色定妆照，直接出片。\n"
                  % NOBODY)
    # 先按台词里的「说话人：」自动补一遍参考角色，补不上的才拦。
    # （改成大纲+细节布局后，新建一镜不会自动带角色，直接点生成就会撞上弹框）
    _filled = 0
    try:
        _filled = _fill_refs_from_line()
    except Exception:
        pass
    if _filled:
        # 【关键】上面那份 shots 快照是在补角色**之前**取的：不重新 collect 一次，
        # 引擎拿到的 ref 还是空的 —— 关键帧会退回「文生图」（定妆照白绑），
        # 人脸质检还会按「该有 0 人」直接放行，等于整条锚定链路静默失效。
        shots = collect_shots()
    _miss = []
    _idx = 0
    for r in shot_rows:
        if not (r["scene"].get("1.0", "end").strip() or r["say_var"].get().strip()):
            continue          # 空镜不计入，编号跟 collect_shots 一致
        _idx += 1
        _rv = r["ref_var"].get().strip()
        if _rv == NOBODY:
            continue
        if not _rv or _rv not in CHAR_REFS:
            _miss.append(str(_idx))
    if _miss:
        log_q.put("[校验] 实际 ref=%s / 角色库=%s\n"
                  % ([r["ref_var"].get() for r in shot_rows],
                     list(CHAR_REFS.keys())))
        _lst = "、".join(_miss[:24]) + ("…" if len(_miss) > 24 else "")
        messagebox.showwarning("有镜头没挂参考角色",
            "「⑥ 参考角色」是必选项，这 %d 镜还空着（或名字对不上角色库）：\n\n"
            "第 %s 镜\n\n请先在左栏点一下那一镜，再在右栏『细节 → ⑥ 参考角色』里选好；\n"
            "纯空镜（没有人）就选「%s」。"
            % (len(_miss), _lst, NOBODY))
        return
    # 名字绑了、但定妆照文件不在了（换过模型目录 / 手动删过 input / 新剧刚导入还没生成完）：
    # 不拦的话会静默退化成文生图，跨镜换脸还查不出原因。
    _lost = [nm for nm, fn in CHAR_REFS.items()
             if not os.path.exists(os.path.join(INPUTDIR, fn or ""))]
    _lost_sc = [nm for nm, fn in SCENE_REFS.items()
                if not os.path.exists(os.path.join(INPUTDIR, fn or ""))]
    if _lost or _lost_sc:
        _msg = []
        if _lost:
            _msg.append("· 角色定妆照不见了：%s" % "、".join(_lost[:8]))
        if _lost_sc:
            _msg.append("· 场景照不见了：%s" % "、".join(_lost_sc[:8]))
        if not messagebox.askyesno(
                "有资产文件还没就位",
                "这些文件在 input 目录里找不到：\n\n%s\n\n"
                "如果刚导入剧本、日志里还在「生成中」，等它出现「完成」再点生成就行。\n"
                "确实是丢了的话，点【是】我按现有外貌文案重新生成。\n\n"
                "硬着头皮出片＝缺定妆照的角色跨镜换脸、缺场景照的背景每镜重画。"
                % "\n".join(_msg)):
            pass
        else:
            if _lost:
                _after_backend(lambda: _regen_char_sheets(_lost), "补定妆照")
            if _lost_sc:
                _after_backend(lambda: threading.Thread(
                    target=lambda: _gen_scenes_batch(_lost_sc, quiet=True),
                    daemon=True).start(), "补场景照")
            messagebox.showinfo("已排队补资产",
                                "补生成排上了，等日志出现「完成」再点一次生成。")
            return
    title = title_var.get().strip() or "我的漫剧"
    voice = VOICES.get(voice_var.get(), "zh-CN-XiaoxiaoNeural")
    rate = RATES.get(voice_rate_var.get(), "+0%")
    style = style_var.get().strip()
    _parsed = [_split_who(s["say"]) for s in shots]
    _shots = []
    _prev_mo = ""       # 上一镜自动判出来的运镜：连镜撞车时用来换一个
    for s, pw in zip(shots, _parsed):
        _szs = (s.get("size") or "").strip()
        _szsuffix = ("，" + SIZE_TEXT[_szs]) if (_szs in SIZE_TEXT and not auto_prompt_var.get()) else ""
        base_key = (_auto_prompt(s["scene"], style, size=_szs,
                                 direct=s.get("direct", ""))
                    if auto_prompt_var.get()
                    else (s["scene"] + _szsuffix + style))
        # ⑧ 场景：同一场景的多镜共用一张场景照 → 背景跨镜一致（不然像在换地方）
        # 跟角色一样是两件套：场景照锁大结构，统一场景文案锁小细节。
        _loc = (s.get("loc") or "").strip()
        _locdesc = (SCENE_DESCS.get(_loc) or "").strip() if _loc else ""
        if _locdesc:
            base_key = "同一场景「%s」：%s。%s" % (_loc, _locdesc, base_key)
        # ⑥ 参考角色：命中已绑定定妆照时，该镜关键帧改走 img2img 锚定，锁住同一张脸
        rname = (s.get("ref") or "").strip()
        # 参考图只锁脸，发型/衣袍/气质要靠这段「统一文案」——每镜都贴同一段，一字不改
        if rname == NOBODY:
            rname = ""
        _coplayer = ""
        if rname and rname in CHAR_REFS and (CHAR_DESCS.get(rname) or "").strip():
            base_key = _lock_text(rname) + "。" + base_key
            # 一镜里出现两个人时，参考图只能锁住其中一张脸，另一个人的长相和衣袍就得
            # 靠文案钉死；否则模型会把同框的人也画成参考角色本人
            # （第 6 镜「两人对望」就把红衣少女画成了青衫少年）。
            _blob = (s.get("scene") or "") + " " + (s.get("line") or "")
            _others = [n for n in CHAR_REFS
                       if n != rname and n != NOBODY and n in _blob]
            if not _others and len(CHAR_REFS) == 2 and re.search(
                    r"两人|二人|对望|相对|彼此|同框|并肩|他们", _blob):
                _others = [n for n in CHAR_REFS if n != rname]
            if _others:
                _coplayer = _others[0]
                _a = (CHAR_DESCS.get(rname) or "").strip()
                _b = (CHAR_DESCS.get(_coplayer) or "").strip()
                base_key = ("画面中有两个人，外貌与衣着必须明显不同。一人「%s」：%s；"
                            "另一人「%s」：%s。%s"
                            % (rname, _a, _coplayer, _b, base_key))
        # ⑤ 运镜：优先用界面上的值；万一还是空的（老脚本、命令行导入），
        # 生成前再按描述兜底判一次 —— 绝不让整片退回同一句默认运镜。
        _mo = (s.get("motion") or "").strip()
        if not _mo:
            _mo = guess_motion(s.get("scene"), s.get("size"), s.get("say"))
            # 自动识别才做去重：手选的就是用户想要的那一个，撞车也照他选
            _mo = motion_variety(_mo or DEFAULT_MOTION, _prev_mo, _szs)
            _prev_mo = _mo
        # 台词没写「角色：」前缀时，按"画面里的参考角色自己在说"处理
        # —— 这正是 _split_who 的约定，但以前 who 直接留空，到了
        # gen_manju_shots.speaker_of 那儿会被当成**画外旁白**：于是全片用旁白
        # 那把女声念主角的台词，嘴还一直闭着（日志里每镜都显示"音色样本 旁白"）。
        # 想真念旁白，就在台词里写「旁白：……」。
        _who = pw[0] or ((rname or "").strip() if (pw[1] or "").strip() else "")
        item = {"key": base_key,
                "prompt": (_mo or DEFAULT_MOTION),
                "line": pw[1], "who": _who}
        if rname and rname in CHAR_REFS:
            item["ref"] = rname
            try:
                dn = float(s.get("dn") or 0.80)
            except Exception:
                dn = 0.80
            if _coplayer:
                # 双人镜：不再靠「把锚定压到 0.88 让模型自己发挥」——实测那样第二个人
                # 要么不出现、要么长成主角的脸。现在把两个人的定妆照左右拼成一张底图，
                # 用 0.62 左右重绘，两张脸各归各人（详见 make_drama._duo_base）。
                item["ref2"] = _coplayer
                dn = min(max(dn, 0.55), 0.70)
            item["denoise"] = max(0.20, min(0.95, dn))
        # ⑧ 场景：这一镜在哪个地方。有场景照时，引擎会把「场景照 + 角色定妆照」
        # 拼成一张底图再重绘 —— 地点和脸一起锁住。
        if _loc and _loc in SCENE_REFS:
            item["scene"] = _loc
        # ⑦ 景别透传：make_drama 靠它判断"这镜是近景还是宽景"，
        # 宽景/动作镜不再拼并排双人底图（否则打戏会变成两人合影）。
        if _szs:
            item["size"] = _szs
        _shots.append(item)
    # A 方案：单镜只能念 5.2 秒，超长台词先拆段（画面沿用、关键帧复用），
    # 否则后半句会被硬截掉 —— 这是之前"每镜都像念了一半"的根因。
    if bool(native_var.get()):
        _before = len(_shots)
        _shots, _notes = _expand_native_shots(_shots)
        if _notes:
            log_q.put("[拆镜] A 方案单镜上限 %.1f 秒，超长台词已自动拆段"
                      "（画面与关键帧沿用，不额外多花出图时间）\n"
                      % (NATIVE_MAX_FRAMES / 24.0))
            for _n in _notes:
                log_q.put("    " + _n + "\n")
            log_q.put("    分镜 %d → %d 镜\n" % (_before, len(_shots)))
    # 把自动拼好的提示词打到日志里，前 3 镜给全文，后面只给长度——方便核对"公式有没有生效"
    if auto_prompt_var.get():
        log_q.put("[提示词] 已按公式自动补齐（景别/角度/光线/防崩词），示例：\n")
        for _i, _it in enumerate(_shots[:3], 1):
            log_q.put("  第%d镜 → %s\n" % (_i, _it["key"]))
        if len(_shots) > 3:
            log_q.put("  …其余 %d 镜同样处理\n" % (len(_shots) - 3))
    script = {"title": title, "voice": voice, "rate": rate, "shots": _shots}
    if CHAR_REFS:
        script["refs"] = dict(CHAR_REFS)
    if SCENE_REFS:
        script["srefs"] = dict(SCENE_REFS)
    # 三视图也传下去：背影/侧身镜要用它裁出的正/侧/背面板当起点
    # （以前只当参考图摆着，生成流程一行都没用到，白白浪费了这份资产）
    if CHAR_SHEETS:
        script["sheets"] = dict(CHAR_SHEETS)
    # A 方案：选了「武侠旁白」这类预设，就把旁白的音色描述一起送进引擎（覆盖旧剧的旁白音色）
    _narr = NARRATOR_DESC.get(voice_var.get())
    if _narr:
        script["voices"] = {"旁白": _narr}
    native = bool(native_var.get())
    # 一人一音色：旁白用顶部选的那个，角色按性别各锁一个 —— 否则全剧共用一把嗓子。
    # A 方案同样需要这份档案：模型原生发声时音色只能靠文字描述锁，不传就每镜随机换人。
    _prof = {}
    if CHAR_REFS:
        _prof = _assign_char_voices(list(CHAR_REFS))
        CHAR_VOICES.update(_prof)
        try:
            _save_char_refs()
        except Exception:
            pass
    # 【修】原来下面整段音色/引擎逻辑被缩进包在 if CHAR_REFS 里：
    # 纯空镜（角色库被清空）时 script 里连 engine / 旁白音色都没有，
    # config 指定的引擎悄悄失效，A 方案还会丢掉旁白的音色档案。
    # 角色分档可以没有，旁白和引擎不能没有。
    _vm = {"旁白": {"voice": voice, "rate": "+0%", "pitch": "+0Hz"}}
    _vm.update(_prof)
    if native:
        # 翻译成 model 能执行的嗓音描述，交给引擎写进每镜 prompt
        script["char_voices"] = dict(_prof)
        # Ref2VA 通道要用**真实音色名**去合成参考音频（音色克隆），
        # 不是文字描述。这里把 edge-tts 的音色名一起传下去。
        script["voice_ids"] = {n: p.get("voice") for n, p in _vm.items() if p}
        # 引擎选择：本机有 ref2va 底模就走音色克隆通道（参考音频锁嗓子），
        # 没有就退回 fl2va（音色只能靠文字描述猜）。config 里可以强制指定。
        _eng = (AP.CFG.get("engine") or "").strip()
        if not _eng:
            _eng = "r2v"
        script["engine"] = _eng
        if CHAR_DESCS:
            script["char_descs"] = dict(CHAR_DESCS)
        _narr_v = dict(script.get("voices") or {})
        _narr_v.setdefault("旁白", M.voice_desc("旁白", _vm["旁白"]))
        script["voices"] = _narr_v
        log_q.put("[音色] A 方案·一人一把嗓子（写成文字描述锁进 prompt，跨镜不变）：\n")
        for _k, _v in _vm.items():
            log_q.put("   %s → %s\n" % (_k, _voice_profile_label(_v)))
    else:
        script["voice_map"] = _vm
        log_q.put("[音色] B 方案·一人一音色（含语速/音高区分）：\n")
        for _k, _v in _vm.items():
            log_q.put("   %s → %s\n" % (_k, _voice_profile_label(_v)))
    auto_len = bool(auto_len_var.get())
    try:
        length = int(length_var.get())
    except Exception:
        length = 22
    turbo = turbo_var.get()
    res, steps_override = SPEED_PRESETS.get(speed_var.get(), ((448, 800), 4))
    landscape = ASPECTS.get(aspect_var.get(), "portrait") == "landscape"
    if landscape:
        res = (res[1], res[0])      # 横屏就是竖屏宽高对调（H3 要求 32 的倍数，两边都满足）
    try:
        import stopflag
        stopflag.clear()          # 清掉上一次的停止标记
    except Exception:
        pass
    # 开了自动同步就把这次的参数推到画布上：右边手动排队时用的一定是同一套设置
    try:
        if _web_sync_auto.get():
            _web_sync_params(auto=True)
    except Exception:
        pass
    try:
        import progress as PG
        PG.reset(); PG.emit("准备", 0.01, "")
    except Exception:
        pass
    _GEN_T0[0] = time.time()
    prog_lbl.config(text="准备中…"); prog_pct.config(text="0%"); prog_var.set(0)
    try:
        prog_time.config(text="")
    except Exception:
        pass
    set_gen_running(True)
    _kc = getattr(M, "KEY_CANDIDATES", 1)
    log_q.put(f"\n=== 开始生成：《{title}》  共{len(shots)}镜  "
              f"关键帧每镜出 1 张→检测→像废片才重出（最多试 {_kc} 次）  "
              f"{'横屏' if landscape else '竖屏'} {res[0]}x{res[1]}·{steps_override}步  "
              f"{'A方案·模型原生发声（音画同源）' if native else 'B方案·TTS配音'}  "
              f"{'帧数=按台词预估' if (auto_len or native) else 'length=' + str(length)}  "
              f"文本编码={'4B轻量' if te4b_var.get() else '32B完整'}  "
              f"{'音色=每角色文字锁定' if (native and _prof) else ('音色=模型自定' if native else '音色=' + voice_var.get())} ===\n")
    global gen_thread

    _resume = _GEN_RESUME[0]
    _GEN_RESUME[0] = False        # 只用一次：再点一次开始就是正常重跑
    if _resume:
        log_q.put("[续渲] 上一轮是中断的：已渲完的镜会跳过，没渲完的继续\n")
    log_q.put("[跳过已渲] 已有关键帧/成片的镜自动跳过（不比对本轮改动）；"
              "想重画某一镜，把 output 里它那张 _key_ 图删掉再点开始\n")

    def worker():
        stopped = False
        try:
            with contextlib.redirect_stdout(QW(log_q)):
                M.run(script, prefix=M.slug(title), length=length, turbo=turbo,
                      res=res, steps=steps_override, te4b=te4b_var.get(),
                      unet=MODEL_PRESETS.get(model_var.get(), ""),
                      auto_len=auto_len, native=native, resume=_resume,
                      skip_rendered=True,
                      qc=bool(AP.CFG.get("qc_auto", True)))
            log_q.put("\n=== 全部完成！成片在 output/ 文件夹 ===\n")
        except SystemExit:
            stopped = True
            log_q.put("\n=== 已停止 ===\n")
        except Exception as e:
            if type(e).__name__ == "Stopped" or "user stop" in str(e):
                stopped = True
                log_q.put("\n=== 已停止（未完成的镜不会出现在成片里）===\n")
            else:
                # 以前只记一句错误信息，没有堆栈，出片失败只能靠猜。
                # 把 traceback 一起写进运行日志，下一轮才能一眼定位到哪一行。
                import traceback
                log_q.put(f"\n[异常] {e}\n{traceback.format_exc()}\n")
        finally:
            try:
                import stopflag
                stopflag.clear()
            except Exception:
                pass

            def _finish():
                film = ""
                try:
                    _p = os.path.join(M.OUTDIR, "%s_drama.mp4" % M.slug(title))
                    if os.path.exists(_p):
                        film = _p
                except Exception:
                    film = ""
                # 真的跑完 + 成片已落盘，才让「下一步」变「✅ 打开成片」；中途停掉就只是停下来了
                set_gen_running(False, done=(not stopped and bool(film)))
                stop_btn.config(text="■ 停止")
                # 停止的瞬间可能刚好把成片合完：那就别报「已停止」，直接把结果交出来
                if stopped:
                    if film:
                        prog_var.set(100)
                        prog_pct.config(text="100%")
                        prog_lbl.config(text="✅ 已完成（成片已合成，点「→ 打开成片文件夹」）")
                        log_q.put("\n[提示] 停止时成片已经合完，已保存：%s\n" % film)
                    else:
                        # 进度条归零：留着 92% 会让人以为还在跑
                        prog_var.set(0)
                        prog_pct.config(text="—")
                        prog_lbl.config(text="已停止 —— 再点「▶ 开始生成」会接着渲，前面渲好的镜不重做")
                else:
                    prog_var.set(100)
                    prog_pct.config(text="100%")
                    prog_lbl.config(text="✅ 已完成")
                # 出片结束就把显存还回去(模型留在内存缓存里，下次出片不必重读盘)
                threading.Thread(target=_release_vram, daemon=True).start()

            root.after(0, _finish)

    gen_thread = threading.Thread(target=worker, daemon=True)
    gen_thread.start()


def stop_gen():
    """真·停止：① 通知本进程的渲染循环别再等 ② 打断后端 ③ 立刻改界面反馈。"""
    # 记一笔：这一轮是被中断的 → 下次点开始走续渲，不从头重做
    try:
        _GEN_RESUME[0] = True
    except Exception:
        pass
    # ① 进程内停止标记（wait_done / 各阶段循环都会检查它）
    try:
        import stopflag
        stopflag.set()
    except Exception as e:
        log_q.put(f"[停止] 无法设置停止标记: {e}\n")
    # ② 打断 ComfyUI 当前任务并清空队列（否则后端会继续把剩下的镜渲完）
    ok, detail = False, ""
    try:
        import urllib.request
        for path in ("/interrupt", "/queue?clear=true"):
            req = urllib.request.Request(COMFY_URL + path, data=b"", method="POST")
            try:
                urllib.request.urlopen(req, timeout=6).read()
                if path == "/interrupt":
                    ok = True
            except Exception as e:
                detail = f"{path}: {e}"
    except Exception as e:
        detail = str(e)
    # ③ 界面立刻反馈（不再"点了没反应"）
    stop_btn.config(text="■ 正在停止…", state="disabled")
    if ok or not detail:
        log_q.put("\n[停止] 已请求停止：正在中断当前渲染并清空后续队列…\n")
    else:
        log_q.put(f"\n[停止] 已请求停止（后端中断返回：{detail}）\n"
                  "       本程序不会再等待，但后端可能仍在收尾。\n")


def _kill_backend_now():
    """让 ComfyUI 立刻停下（关程序时用）。

    以前关掉 exe 只是这个进程没了：ComfyUI 那边的任务照渲不误，队列里还压着新任务，
    实测关掉十几分钟后显卡还在跑。所以关窗口必须先 /interrupt + 清队列。
    """
    try:
        import stopflag
        stopflag.set()
    except Exception:
        pass
    try:
        import urllib.request
        for path in ("/interrupt", "/queue?clear=true"):
            try:
                req = urllib.request.Request(COMFY_URL + path, data=b"", method="POST")
                urllib.request.urlopen(req, timeout=3).read()
            except Exception:
                pass
    except Exception:
        pass


def _on_app_close():
    """关窗口 = 任务停、显存还、**后端留着**。

    实测出来的两难：
    ① 只关窗口不停任务 —— ComfyUI 会把这一镜渲完，还继续跑队列；
    ② 只停任务不 unload —— 模型缓存留在显存里，任务管理器上 4.6GB 不降；
    ③ 每次都杀进程 —— 显存是干净了，但下次启动要重新加载 ComfyUI（1~2 分钟），
       用户反馈"每次都要等后端起来"。
    所以默认：中断+清队列 → /free 卸模型（显存 4.5G→0.5G）→ **不杀进程**。
    下次启动 8188 直接在线、秒开，模型等第一次出片时再按需加载。
    想彻底结束进程：顶栏「关闭后端省内存」，或 config 里 "keep_backend": false。
    """
    try:
        # 先刷新几何缓存：WM_CLOSE 是同步回调，此时 root.winfo_x/y 可能还是移动前的
        # 旧坐标（Tk 几何缓存未刷新），会把初始位置存回去 —— 这正是「点 X 关闭后
        # 回到初始位置」的根因。update_idletasks 强制把 pending 的几何更新刷进 winfo。
        root.update_idletasks()
    except Exception:
        pass
    try:
        _ue_save_layout(force=True)   # 先记住窗口/三栏位置，免得下次回到默认
    except Exception:
        pass
    try:
        _autosave_now(force=True)     # 顺手把分镜表存死：下次打开直接接着改
    except Exception:
        pass
    _busy = False
    try:
        _busy = bool(_GEN_RUNNING[0])
    except Exception:
        pass
    if _busy:
        try:
            _GEN_RESUME[0] = True      # 下次开起来点开始，已渲完的镜能接上
        except Exception:
            pass
        _kill_backend_now()            # stopflag + /interrupt + 清队列
    _keep = True
    try:
        _keep = bool(AP.CFG.get("keep_backend", True))
    except Exception:
        _keep = True
    if _keep:
        # 默认**什么都不做**：进程留着、模型也留在显存里。
        # 这样下次打开就是「秒连 + 第一次出片不用重新加载模型」——
        # 以前这里会 /free 卸模型，虽然省了 4G 显存，但用户等到的是
        # 「每次启动都像在重新启动后端」（反馈原话）。
        # 想省显存：设置里勾「退出时卸载模型」，或用「▼ 关闭后端」手动关。
        try:
            _free = bool(AP.CFG.get("free_vram_on_exit", False))
        except Exception:
            _free = False
        if _free:
            try:
                _release_vram()
            except Exception:
                pass
        try:
            _crash("退出：后端保持运行（不卸载模型），下次启动直接连上 8188")
        except Exception:
            pass
    else:
        try:
            stop_backend(silent=True)   # /free + 结束进程
        except Exception:
            pass
    # 画布（:3000 的 launcher）故意不关：它是独立进程、不占显存，下次启动直接复用。
    try:
        root.destroy()
    except Exception:
        pass


def _latest_video():
    """成片目录里最新的一条视频（按修改时间），没有就返回空串。"""
    try:
        vids = [os.path.join(OUTDIR, f) for f in os.listdir(OUTDIR)
                if os.path.isfile(os.path.join(OUTDIR, f))
                and f.lower().endswith((".mp4", ".mov", ".mkv", ".webm"))]
        if vids:
            return max(vids, key=os.path.getmtime)
    except Exception:
        pass
    return ""


def open_output():
    """打开成片文件夹，并直接在资源管理器里选中最新那条成片。
    只 open 文件夹的话，镜头几百帧的 png 会把成片淹掉、得自己翻——所以改成 /select。"""
    os.makedirs(OUTDIR, exist_ok=True)
    target = _latest_video()
    norm_dir = os.path.normpath(OUTDIR)
    try:
        if target:
            # explorer 的 /select 必须写在同一个参数里，用列表分开传会丢掉选中效果
            subprocess.Popen('explorer /select,"%s"' % os.path.normpath(target))
            log_q.put("[成片] 已在文件夹中定位：%s\n" % os.path.basename(target))
        else:
            os.startfile(norm_dir)
            log_q.put("[成片] 还没有 mp4 成片，先打开文件夹：%s\n" % norm_dir)
            messagebox.showinfo("还没有成片",
                                "文件夹已打开，但里面还没有 mp4 成片。\n\n"
                                "先生成一版，再点这个按钮就会自动选中最新成片。")
    except Exception:
        try:
            subprocess.Popen('explorer "%s"' % norm_dir)
        except Exception as e:
            messagebox.showerror("打不开", str(e))


# ============================ 日志轮询 ============================
LOG_FILE = os.path.join(AP.OUTDIR, "studio.log")
_ERR_KEYS = ("[异常]", "[ERROR]", "[FAILED]", "[error]", "Traceback", "失败", "错误")


_LOG_FP = [None]


def _log_append(line):
    """同步落一份日志文件，出问题可以直接发给别人。

    句柄常开着：原来每条日志都 open/write/close 一次，生成时每秒几十条，
    光反复开关文件就够呛（还容易招来杀软扫描）。行缓冲保证内容及时落盘。
    """
    try:
        if _LOG_FP[0] is None:
            _LOG_FP[0] = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        _LOG_FP[0].write(line)
    except Exception:
        pass


def poll_log():
    _got = False
    while not log_q.empty():
        try:
            line = log_q.get_nowait()
        except Exception:
            break
        tag = "err" if any(k in line for k in _ERR_KEYS) else ()
        log_txt.insert("end", line, tag)
        _log_append(line)
        _got = True
    if _got:
        log_txt.see("end")      # 有新日志才滚动，且整批只滚一次（原来每条都滚一次）
    # 太长就截掉前一半，免得越跑越卡
    try:
        if int(log_txt.index("end-1c").split(".")[0]) > 4000:
            log_txt.delete("1.0", "1500.0")
    except Exception:
        pass
    # 进度条：读渲染线程写下的真实进度（帧数/阶段）
    # 只在「真的在跑」时才让后台覆盖界面。之前的坑：停止/跑完之后 PG.last() 还留着
    # 最后那条（拼接成片 0.92），这个 150ms 轮询会把 _finish 写的「已停止」立刻刷回去，
    # 看上去就像卡死在 92%（其实进程早停了，成片都出来了）。
    try:
        import progress as PG
        st = PG.last() if _GEN_RUNNING[0] else {}
        if st.get("stage"):
            frac = _overall_frac(st.get("stage"), float(st.get("frac") or 0.0),
                                 st.get("text"))
            prog_var.set(frac * 100.0)
            prog_pct.config(text=f"{int(round(frac * 100))}%")
            label = st["stage"] + (("  " + st["text"]) if st.get("text") else "")
            extra = ""
            if "· 已用" in label:
                label, extra = label.split("· 已用", 1)
                extra = "已用 " + extra.strip()
            prog_lbl.config(text=label)
            prog_time.config(text=extra)
            # 剧情预览的节点图也跟着走：记下阶段 / 当前第几镜 / 百分比，
            # 每镜节点上就会显示「▶ 关键帧 46%」「✓ 已出图」「排队中…」。
            try:
                import re as _re3
                _live = _STORY.setdefault("live", {})
                _live["stage"] = str(st.get("stage") or "")
                _live["text"] = str(st.get("text") or "")
                _live["pct"] = frac
                _m3 = _re3.search(r"第\s*(\d+)\s*/\s*(\d+)", _live["text"])
                _live["i"] = int(_m3.group(1)) if _m3 else 0
                _live["n"] = int(_m3.group(2)) if _m3 else 0
            except Exception:
                pass
            # 从"第 2/3 镜 · 4 步"里取出本轮该有的步数，给步数看门狗做过滤：
            # 不然关键帧的 8 步（40 秒就跑完）会被当成视频进度显示出来。
            try:
                import re as _re2
                _m = _re2.search(r"(\d+)\s*步", label)
                if _m:
                    _STEP["want"] = int(_m.group(1))
            except Exception:
                pass
    except Exception:
        pass
    root.after(150, poll_log)


# ============================ 构建界面 ============================
_MUTEX = None


def _claim_single_instance():
    """单实例：已经开着一个窗口就把它抬到前面，这个进程直接退出。

    以前能开出两个（一个还没窗口的僵尸进程），既吃内存又会在打包时锁住 exe。
    自检模式(--xxx)不拦，方便同时跑多个自检。
    """
    global _MUTEX
    if os.name != "nt" or any(a.startswith("--") for a in sys.argv[1:]):
        return True
    try:
        import ctypes
        _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "ManjuStudio_Single_v1")
        if ctypes.windll.kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            hwnd = ctypes.windll.user32.FindWindowW(None, "漫剧工作室 · AI 漫剧一键生成")
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            return False
    except Exception:
        pass
    return True


if not _claim_single_instance():
    sys.exit(0)

def _system_theme_key():
    """「跟随系统颜色」：Windows 应用模式是深色套 graphite，浅色套 plain。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        winreg.CloseKey(k)
        return "plain" if int(v) else "graphite"
    except Exception:
        return TH.DEFAULT


root = tk.Tk()
# 让字号随真实 DPI 对齐（声明感知后 Tk 默认已按 DPI 设 scaling，这里再对齐一次，
# 避免个别机器上字体偏小；几何尺寸按物理像素走，不受此影响，故不破版）。
try:
    _dpi = root.winfo_fpixels("1i")
    if _dpi and _dpi > 1:
        root.tk.call("tk", "scaling", _dpi / 72.0)
except Exception:
    pass
root.title("漫剧工作室 · AI 漫剧一键生成")

# 禁用窗口最小化/还原/最大化动画。WebView2 嵌在 Tk 里，窗口从任务栏还原时，
# DWM 的还原动画会在动画结束前给 WebView 区域补一帧黑表面，肉眼看着就是「闪黑」
# （实测还原后 ~160ms 有一帧约 14% 区域变黑）。关掉这个动画，窗口瞬间出现、
# WebView 立即合成，那一帧黑就没了。config: wm_disable_restore_anim（默认开）。
def _disable_dwm_transitions():
    try:
        import ctypes as _ct
        if not bool(AP.CFG.get("wm_disable_restore_anim", True)):
            return
        _dwm = _ct.windll.dwmapi
        hwnd = root.winfo_id()
        val = _ct.c_int(1)
        _dwm.DwmSetWindowAttribute.argtypes = [_ct.c_void_p, _ct.c_int, _ct.c_void_p, _ct.c_int]
        _dwm.DwmSetWindowAttribute.restype = _ct.c_int
        _dwm.DwmSetWindowAttribute(_ct.c_void_p(hwnd), 3, _ct.byref(val), _ct.sizeof(val))
    except Exception:
        pass
_disable_dwm_transitions()

# 定时器/按钮回调里的异常默认只往 stderr 打，windowed 下 stderr 是 None 会直接把程序带崩。
# 改成记日志界面继续活着，不再「莫名其妙自己退出」。
def _tk_error(exc, val, tb):
    _crash("【界面回调异常】\n" + "".join(_tb.format_exception(exc, val, tb)))


try:
    root.report_callback_exception = _tk_error
except Exception:
    pass
_crash("=== 界面已创建 ===")
THEME_KEY = AP.CFG.get("theme", "unreal")
if THEME_KEY == "auto":
    THEME_KEY = _system_theme_key()
elif THEME_KEY not in TH.PALETTES:      # 配置里留了个不存在的主题名 -> 回落到「跟随系统」
    THEME_KEY = _system_theme_key()
    try:
        _c = dict(AP.CFG); _c["theme"] = "auto"; AP.save_config(_c); AP.CFG["theme"] = "auto"
    except Exception:
        pass
# 【2026-11-01】界面改成虚幻编辑器那种停靠布局后，浅色主题会显得脏（标题条/工具栏是
# 深色块、内容区是白底，割裂）。这里把旧配置里的浅色主题一次性迁到 "unreal"，
# 迁过一次就记 ue_layout=1，之后用户在「主题」里自己换什么都不再干预。
if not AP.CFG.get("ue_layout"):
    THEME_KEY = "unreal"
    try:
        _c = dict(AP.CFG); _c["theme"] = "unreal"; _c["ue_layout"] = 1
        AP.save_config(_c)
        AP.CFG["theme"] = "unreal"
        AP.CFG["ue_layout"] = 1
    except Exception:
        pass
TH.apply(root, THEME_KEY)           # 主题：必须在建控件之前套用
_SCR_H = root.winfo_screenheight()
_SCR_W = root.winfo_screenwidth()
# 高度按屏幕算：1080 屏上 1024 高 + 标题栏 = 1055，再算上任务栏，窗口底部会被切掉
# （用户反馈「最下面的都没显示出来」）。留 150 给标题栏 + 任务栏，并整体上移。
_W0 = min(1680, _SCR_W - 80)
_H0 = min(980, _SCR_H - 150)
# 上次关窗时的大小/位置优先（用户要求「记住我调整的位置，别启动就回默认」）。
# 只认还放得进当前屏幕的值 —— 换过分辨率/缩放时旧值可能已经越界。
_GX0, _GY0 = max(8, (_SCR_W - _W0) // 2), 12
try:
    import json as _json0
    _p0 = os.path.join(AP.OUTDIR, ".ui_state.json")
    with open(_p0, encoding="utf-8") as _f0:
        _lay0 = (_json0.load(_f0) or {}).get("layout") or {}
    _wl0 = _lay0.get("win") or []
    if len(_wl0) == 4:
        _w1, _h1, _x1, _y1 = (int(v) for v in _wl0)
        # 下限放宽到比 minsize 还低（宽 760 / 高 460），保证「缩到允许的最小尺寸」也能
        # 被原样记住并还原；过小的值（0、损坏数据）仍会被拒，退回默认。
        if 760 <= _w1 <= _SCR_W and 460 <= _h1 <= _SCR_H:
            _W0, _H0 = _w1, _h1
        if 0 <= _x1 <= _SCR_W - 200 and 0 <= _y1 <= _SCR_H - 200:
            _GX0, _GY0 = _x1, _y1
except Exception:
    pass
center_win(root, _W0, _H0, 1180, 720)
# 再整体往上顶一点（用户要求「整个主窗口往上移」）：居中会让窗口偏下，
# 底部那排（状态条 / 日志）以前正好压在任务栏上。没有存过位置就贴到上方 12px。
try:
    root.geometry("%dx%d+%d+%d" % (_W0, _H0, _GX0, _GY0))
except Exception:
    pass
root.option_add("*Font", FONT)

# ============================================================================
# UE5 风格停靠布局骨架（2026-11-01 重做）
# ----------------------------------------------------------------------------
# 结构照虚幻引擎编辑器摆：
#
#   ① 标题条：🎬 品牌 + 文件/编辑/视图/工具/帮助
#   ② 工具栏：启动后端 · 开始生成 · 角色 · 场景 …
#   ③ 主体三栏（可拖分隔条）：大纲 | 视口 | 细节
#   ④ 底部面板（可拖高度）：内容浏览器 / 输出日志 / 进度
#   ⑤ 状态条：状态文字 + 后端端口
#
# 为什么用 tk.PanedWindow 而不是继续手算宽度：原来那套 _layout_fit 三栏预算
# 在 1280/1920 两种窗口宽度之间反复打架（日志里一堆「画布面板=320 主面板≈532」），
# 用户拖一下分隔条就被下一次 resize 顶回去。PanedWindow 由 Tk 自己管，
# 拖动即所见，窗口缩放也不会跳。
# ============================================================================
_UE = {"dock": True}
UE_BG = TH.BG


def _ue_frame(parent, **kw):
    kw.setdefault("background", TH.PANEL)
    return tk.Frame(parent, **kw)


def _ue_header(parent, text, hint=""):
    """UE5 面板标题条：26px、比面板深一档、下缘一条 1px 线。"""
    bar = tk.Frame(parent, background=TH.HL, height=26)
    bar.pack(fill="x")
    bar.pack_propagate(False)
    tk.Label(bar, text=text, background=TH.HL, foreground=TH.TEXT2,
             font=(TH.FONT[0], 9), anchor="w").pack(side="left", padx=(9, 0))
    if hint:
        tk.Label(bar, text=hint, background=TH.HL, foreground=TH.TEXT3,
                 font=(TH.FONT[0], 8), anchor="e").pack(side="right", padx=(0, 9))
    # 下缘细线：UE5 每个面板都靠这条线分界，不靠阴影
    tk.Frame(parent, background=TH.LINE, height=1).pack(fill="x")
    return bar


# ---------------------------------------------------------------- ① 标题条
_ue_title = tk.Frame(root, background=TH.PANEL, height=30)
_ue_title.pack(fill="x", side="top")
_ue_title.pack_propagate(False)
tk.Label(_ue_title, text="🎬  漫剧工作室", background=TH.PANEL,
         foreground=TH.TEXT, font=(TH.FONT[0], 10, "bold")).pack(
    side="left", padx=(12, 14))
_ue_menu_host = tk.Frame(_ue_title, background=TH.PANEL)
_ue_menu_host.pack(side="left")
tk.Frame(root, background=TH.LINE, height=1).pack(fill="x", side="top")

# ---------------------------------------------------------------- ② 工具栏
_ue_toolbar = tk.Frame(root, background=TH.HL, height=46)
_ue_toolbar.pack(fill="x", side="top")
_ue_toolbar.pack_propagate(False)
tk.Frame(root, background=TH.LINE, height=1).pack(fill="x", side="top")

# ------------------------------------------------------ ② 三者关系（常驻一行说明）
# 新手最容易懵的是：主界面、ComfyUI、无界工作台到底什么关系、为什么界面里有两个网页。
# 在工具栏上面压一条说明，一眼能看明白；不想要了点右边 ✕（记进配置，视图菜单能再打开）。
_INTRO = {"bar": None}


def _hide_intro(_e=None):
    b = _INTRO.get("bar")
    _INTRO["bar"] = None
    if b is not None:
        try:
            b.destroy()
        except Exception:
            pass
    try:
        c = dict(AP.CFG)
        c["hide_intro"] = True
        AP.save_config(c)
        AP.CFG["hide_intro"] = True
    except Exception:
        pass


def _build_intro():
    """主界面 / ComfyUI / 无界工作台 三者关系的说明条。"""
    if _INTRO.get("bar") is not None:
        return
    bar = tk.Frame(root, background=TH.PANEL)
    bar.pack(fill="x", side="top", before=_ue_toolbar)
    _INTRO["bar"] = bar
    tk.Frame(bar, background=TH.ACCENT, width=3).pack(side="left", fill="y")
    x = tk.Label(bar, text="✕", background=TH.PANEL, foreground=TH.TEXT3,
                 font=(TH.FONT[0], 9), padx=10, cursor="hand2")
    x.pack(side="right", fill="y")
    x.bind("<Button-1>", _hide_intro)
    x.bind("<Enter>", lambda e: x.configure(foreground=TH.TEXT))
    x.bind("<Leave>", lambda e: x.configure(foreground=TH.TEXT3))
    tk.Frame(bar, background=TH.LINE, height=1).pack(side="bottom", fill="x")

    body = tk.Frame(bar, background=TH.PANEL)
    body.pack(side="left", fill="x", expand=True, padx=(10, 6), pady=5)

    def seg(parent, text, fg, bold=False):
        tk.Label(parent, text=text, background=TH.PANEL, foreground=fg,
                 font=(TH.FONT[0], 10, "bold") if bold else (TH.FONT[0], 10),
                 anchor="w").pack(side="left")

    r1 = tk.Frame(body, background=TH.PANEL)
    r1.pack(fill="x")
    seg(r1, "主界面", TH.ACCENT, True)
    seg(r1, " ＝ 你下指令的地方（写剧本 / 排分镜 / 点生成）", TH.TEXT2)
    seg(r1, "　→　", TH.TEXT3)
    seg(r1, "ComfyUI", TH.ACCENT, True)
    seg(r1, " ＝ 真正跑模型的后端，出图出视频都靠它（:8188）", TH.TEXT2)

    r2 = tk.Frame(body, background=TH.PANEL)
    r2.pack(fill="x", pady=(2, 0))
    seg(r2, "无界工作台", TH.ACCENT, True)
    seg(r2, " ＝ ComfyUI 的可视化画布，手动搭 / 改工作流、单张试图用它；"
            "和主界面共用同一个引擎（:3000）", TH.TEXT2)

    r3 = tk.Frame(body, background=TH.PANEL)
    r3.pack(fill="x", pady=(2, 0))
    seg(r3, "怎么用", TH.ACCENT, True)
    seg(r3, " ＝ 日常出片只碰主界面（它会自动调 ComfyUI）；"
            "调工作流 / 试单图 → 视口上「🗺 无限画布」；"
            "查队列、排错 → 「🌐 ComfyUI」", TH.TEXT2)


def _toggle_intro():
    if _INTRO.get("bar") is not None:
        _hide_intro()
        set_status("已隐藏顶部说明（视图 → 三者关系说明 可再打开）", "ok")
    else:
        try:
            c = dict(AP.CFG)
            c["hide_intro"] = False
            AP.save_config(c)
            AP.CFG["hide_intro"] = False
        except Exception:
            pass
        _build_intro()
        set_status("已显示顶部说明", "ok")


try:
    if not AP.CFG.get("hide_intro"):
        _build_intro()
except Exception as _e:
    _crash("顶部说明创建失败: %r" % (_e,))

# ---------------------------------------------------------------- ⑤ 状态条
_ue_status = tk.Frame(root, background=TH.HL, height=24)
_ue_status.pack(fill="x", side="bottom")
_ue_status.pack_propagate(False)
tk.Frame(root, background=TH.LINE, height=1).pack(fill="x", side="bottom")

# ---------------------------------------------------------------- ③④ 主体
# 纵向：上面三栏（stretch），下面底部面板（固定起步高度）
_ue_body = tk.PanedWindow(root, orient="vertical", bd=0, sashwidth=5,
                          sashrelief="flat", background=TH.LINE,
                          borderwidth=0, opaqueresize=True)
_ue_body.pack(fill="both", expand=True, padx=6, pady=(5, 5))

_ue_row = tk.PanedWindow(_ue_body, orient="horizontal", bd=0, sashwidth=5,
                         sashrelief="flat", background=TH.LINE,
                         borderwidth=0, opaqueresize=True)

# 左：大纲
_ue_left = _ue_frame(_ue_row)
# 中：视口
_ue_center = _ue_frame(_ue_row)
# 右：细节
_ue_right = _ue_frame(_ue_row)

# 底：内容浏览器 / 日志 / 进度
_ue_bottom = _ue_frame(_ue_body)

_ue_row.add(_ue_left, minsize=200, stretch="always")
_ue_row.add(_ue_center, minsize=300, stretch="always")
_ue_row.add(_ue_right, minsize=360, stretch="always")
_ue_body.add(_ue_row, minsize=260, stretch="always")
_ue_body.add(_ue_bottom, minsize=140, stretch="never")

_UE.update(title=_ue_title, toolbar=_ue_toolbar, status=_ue_status,
           body=_ue_body, row=_ue_row, left=_ue_left, center=_ue_center,
           right=_ue_right, bottom=_ue_bottom, menu_host=_ue_menu_host)


def _ue_sash_get(pw, i):
    """取第 i 条分隔条的位置（横向取 x、纵向取 y）。

    tk.PanedWindow 没有 ttk 那个 sashpos()，只有 sash_coord()/sash_place()。
    """
    try:
        c = pw.sash_coord(i)
        return c[0] if str(pw.cget("orient")) == "horizontal" else c[1]
    except Exception:
        return 0


def _ue_sash_set(pw, i, pos):
    """把第 i 条分隔条移到 pos。"""
    try:
        if str(pw.cget("orient")) == "horizontal":
            pw.sash_place(i, max(0, int(pos)), 0)
        else:
            pw.sash_place(i, 0, max(0, int(pos)))
    except Exception as _e:
        _crash("摆分隔条失败: %r" % (_e,))


def _ue_dump_tree():
    """调试用：把 root 和标题条的子控件连位置一起写进日志（找幽灵控件用）。"""
    def dump(w, depth=0, maxd=2):
        try:
            for c in w.winfo_children():
                try:
                    txt = c.cget("text")
                except Exception:
                    txt = ""
                _crash("%s%s %s @%d,%d %dx%d %r"
                       % ("  " * depth, c.winfo_class(), str(c), c.winfo_x(),
                          c.winfo_y(), c.winfo_width(), c.winfo_height(),
                          str(txt)[:20]))
                if depth < maxd:
                    dump(c, depth + 1, maxd)
        except Exception:
            pass
    dump(root)
    try:
        _crash("窗口 menu 属性=%r" % (str(root.cget("menu")),))
        for m in root.winfo_children():
            if m.winfo_class() == "Menu":
                _crash("根级菜单 %s entries=%d 名字=%r" % (
                    str(m), m.index("end") or 0,
                    [m.entrycget(i, "label") for i in range((m.index("end") or 0) + 1)
                     if m.type(i) != "separator"]))
    except Exception as e:
        _crash("菜单检查失败 %r" % (e,))


_LAYOUT_READY = {"on": False}


def _ue_save_layout(only_win=False, force=False):
    """记住用户拖出来的分隔位置 + 窗口大小位置。

    only_win=True：只更新窗口大小/位置。窗口被拖小时 PanedWindow 会把三栏压到
    最小值，这时候若把压扁的三栏也存下来，等窗口再放大，用户原来的比例就没了。
    force=True：绕过 _LAYOUT_READY 闸门。用于「关闭窗口」——窗口都要销毁了，存的是
    最终态，不存在覆盖启动期中间态的风险；还能保证「启动后马上关」也把位置落盘。
    """
    if (not only_win) and (not force) and (not _LAYOUT_READY["on"]):
        # 面板位置(only_win=False)要等布局完全落位再存，否则会把启动期的中间态
        # 盖掉用户存的三栏比例；但「只存窗口尺寸」(only_win=True) 重存一遍当前
        # 尺寸是无害的，反而能在「用户刚缩放就被强杀」时也把尺寸落盘。
        # 注意：_LAYOUT_READY 的 2600ms 计时按 mainloop 时间算，而窗口的 Configure
        # 要等 mainloop 起来才派发，所以缩放保存往往早于闸门开启——尺寸保存不能
        # 被这个闸门挡掉，否则强杀后窗口尺寸就丢了。
        return
    try:
        lay = dict(_web_read_state().get("layout") or {})
        if not only_win:
            lay.update({
                "left": _ue_sash_get(_ue_row, 0),
                "right": _ue_sash_get(_ue_row, 1),
                "bottom": _ue_sash_get(_ue_body, 0),
                "bot_split": _ue_sash_get(_ue_bot_split, 0),
                "vp_ratio": round(float(_VP.get("ratio") or 0.5), 3),
            })
        lay["win"] = [root.winfo_width(), root.winfo_height(),
                      root.winfo_x(), root.winfo_y()]
        _web_write_state(layout=lay)
    except Exception:
        pass


def _ue_sash_init(force=False):
    """初始分隔位置。PanedWindow 的 sashpos 必须在几何算完之后设，
    否则拿到的是 1px 宽度、算出来的位置全错（第一次启动会看到三栏挤在左边）。

    force=True 是「视图 → 重置布局」用的：忽略存下来的位置，回到默认。
    """
    lay = {}
    if not force:
        try:
            lay = _web_read_state().get("layout") or {}
        except Exception:
            lay = {}
    try:
        w = _ue_row.winfo_width()
        h = _ue_body.winfo_height()
        if w > 200:
            left_w = 310 if w >= 1500 else 270
            right_w = 430 if w >= 1500 else 360
            try:
                _sl, _sr = int(lay.get("left") or 0), int(lay.get("right") or 0)
            except Exception:
                _sl = _sr = 0
            # 存的位置必须还放得下（换过屏幕/缩放过就可能不合法），否则退回默认
            if 80 <= _sl <= w - 320:
                left_w = _sl
            if left_w + 260 <= _sr <= w - 60:
                right_w = max(0, w - _sr)
            else:
                _sr = max(left_w + 320, w - right_w)
                right_w = max(0, w - _sr)
            _ue_sash_set(_ue_row, 0, left_w)
            _ue_sash_set(_ue_row, 1, _sr)
            if os.environ.get("MANJU_LAYOUT_DEBUG") == "1":
                _crash("停靠: 宽=%d 左分隔=%d 右分隔=%d（目标 %d / %d）"
                       % (w, _ue_sash_get(_ue_row, 0),
                          _ue_sash_get(_ue_row, 1),
                          left_w, max(left_w + 320, w - right_w)))
        if h > 300:
            try:
                _sb = int(lay.get("bottom") or 0)
            except Exception:
                _sb = 0
            if not (160 <= _sb <= h - 90):
                _sb = max(240, h - 230)
            # 底部现在要塞三样（进度 + 资产缩略图 + 日志）：太矮的话缩略图会被切掉半个，
            # 存下来的 428 是"三 Tab 时代"拖出来的，可能已经不够用了，够不到 200px 就抬一抬
            if h - _sb < 245:
                _sb = max(160, h - 245)
            _ue_sash_set(_ue_body, 0, _sb)
            # 底部内部：内容浏览器 / 日志 的分隔位置
            try:
                _bs = int(lay.get("bot_split") or 0)
            except Exception:
                _bs = 0
            _bw = _ue_bottom.winfo_width()
            if _bw > 300 and not (150 <= _bs <= _bw - 220):
                _bs = 340
            _ue_sash_set(_ue_bot_split, 0, _bs)
        try:
            if lay.get("vp_ratio"):
                _VP["ratio"] = min(0.8, max(0.2, float(lay["vp_ratio"])))
        except Exception:
            pass
    except Exception as _e:
        _crash("停靠初始位置失败: %r" % (_e,))


def _ue_reset_layout():
    """视图 → 重置布局：三栏与底部面板回到默认比例（并覆盖存档）。"""
    _ue_sash_init(force=True)
    root.after(140, _ue_save_layout)
    set_status("布局已重置", "ok")


root.after(160, _ue_sash_init)
root.after(600, _ue_sash_init)      # WebView2 内核起来后几何还会变一次
root.after(1300, _ue_sash_init)
root.after(2200, _ue_sash_init)     # 最后一次落位：之后才允许写盘
root.after(2600, lambda: _LAYOUT_READY.update(on=True))

# 拖完一根分隔条就写一次（120ms 防抖：拖动过程中每一帧都写盘太浪费）。
# 视口内那根（web_split）在下面才创建，绑在那儿。
for _pw in (_ue_row, _ue_body):
    try:
        _pw.bind("<ButtonRelease-1>",
                 lambda e: root.after(120, _ue_save_layout), add="+")
    except Exception:
        pass

# 进度条现在常驻底部第一行，不用再"切页"了；函数名保留，别处还在调
def _ue_show_prog_tab():
    return



# ---------------- 右键菜单 ----------------
# Tk 的 Text / Entry 在 Windows 上默认**没有**右键菜单，所以用户点右键没反应、
# 只能靠 Ctrl+V。这里给所有文本控件统一装一个：粘贴 / 复制 / 剪切 / 全选 / 清空。
# ============================================================================
# 暗色弹窗（替掉系统原生 messagebox）
# ----------------------------------------------------------------------------
# Windows 的 messagebox 永远是浅灰底 + 系统字体，跟这套暗色停靠界面撞得很厉害
# （用户反馈：「弹窗也没有 UE5 化？」）。这里做一个**同名接口的替身**：
#   showinfo / showwarning / showerror / askyesno
# 然后把模块里的 messagebox 名字指向它 —— 几十处调用一个字都不用改。
# ============================================================================
class _UEMsgBox:
    _KIND = {
        "info":  ("ℹ", "ACCENT"),
        "warn":  ("⚠", "WARN"),
        "error": ("✕", "ERR"),
        "ask":   ("？", "ACCENT"),
    }

    def _dlg(self, kind, title, msg, ask=False, yes="确定", no="取消"):
        icon, ckey = self._KIND.get(kind, self._KIND["info"])
        color = getattr(TH, ckey, TH.ACCENT)
        try:
            w = tk.Toplevel(root)
            TH.paint_toplevel(w)
            # 去掉系统标题栏（Windows 那条永远是浅色的，跟暗色界面撞在一起），
            # 自己画一条标题条 + 关闭按钮，再补 1px 边框当窗框。
            w.overrideredirect(True)
            w.transient(root)
            w.configure(background=TH.LINE)
            outer = tk.Frame(w, background=TH.PANEL)
            outer.pack(fill="both", expand=True, padx=1, pady=1)
            hd = tk.Frame(outer, background=TH.HL, height=30)
            hd.pack(fill="x")
            hd.pack_propagate(False)
            tk.Label(hd, text=icon, background=TH.HL, foreground=color,
                     font=(TH.FONT[0], 12, "bold")).pack(side="left",
                                                         padx=(10, 6))
            tk.Label(hd, text=title, background=TH.HL, foreground=TH.TEXT,
                     font=(TH.FONT[0], 10, "bold")).pack(side="left")

            def _drag_start(e):
                w._dx, w._dy = e.x_root - w.winfo_x(), e.y_root - w.winfo_y()

            def _drag_go(e):
                try:
                    w.geometry("+%d+%d" % (e.x_root - w._dx, e.y_root - w._dy))
                except Exception:
                    pass
            hd.bind("<Button-1>", _drag_start)
            hd.bind("<B1-Motion>", _drag_go)
            _x = tk.Label(hd, text="✕", background=TH.HL, foreground=TH.TEXT3,
                          font=(TH.FONT[0], 10), padx=10, cursor="hand2")
            _x.pack(side="right")
            _x.bind("<Button-1>", lambda e: w.destroy())
            _x.bind("<Enter>", lambda e: _x.configure(foreground="#ff6b6b"))
            body = tk.Frame(outer, background=TH.PANEL)
            body.pack(fill="both", expand=True, padx=16, pady=(12, 4))
            tk.Label(body, text=str(msg), background=TH.PANEL,
                     foreground=TH.TEXT2, font=(TH.FONT[0], 9),
                     justify="left", anchor="w", wraplength=460).pack(fill="x")
            bt = tk.Frame(outer, background=TH.PANEL)
            bt.pack(fill="x", padx=16, pady=(6, 14))
            res = {"v": False}

            def _ok():
                res["v"] = True
                w.destroy()

            def _no():
                res["v"] = False
                w.destroy()

            if ask:
                TH.RoundButton(bt, text=no, command=_no, kind="normal",
                               height=30, padx=16).pack(side="right")
                TH.RoundButton(bt, text=yes, command=_ok, kind="accent",
                               height=30, padx=18).pack(side="right",
                                                        padx=(0, 8))
            else:
                TH.RoundButton(bt, text=yes, command=_ok, kind="accent",
                               height=30, padx=22).pack(side="right")
            # 居中到主窗口 + 模态
            w.update_idletasks()
            try:
                px, py = root.winfo_rootx(), root.winfo_rooty()
                pw, ph = root.winfo_width(), root.winfo_height()
                ww, wh = w.winfo_width(), w.winfo_height()
                w.geometry("+%d+%d" % (max(0, px + (pw - ww) // 2),
                                       max(0, py + (ph - wh) // 3)))
            except Exception:
                pass
            try:
                w.grab_set()
                w.focus_force()
            except Exception:
                pass
            w.bind("<Return>", lambda e: _ok())
            w.bind("<Escape>", lambda e: _no())
            root.wait_window(w)
            return res["v"]
        except Exception as _e:
            # 兜底：真出异常就退回系统弹窗，别因为弹窗本身把流程卡死
            try:
                if ask:
                    return bool(messagebox_orig.askyesno(title, msg))
                messagebox_orig.showinfo(title, msg)
            except Exception:
                pass
            _crash("弹窗失败: %r" % (_e,))
            return False

    # ---- 和 tkinter.messagebox 同名同签名 ----
    def showinfo(self, title="提示", message="", **kw):
        self._dlg("info", title, message)
        return "ok"

    def showwarning(self, title="注意", message="", **kw):
        self._dlg("warn", title, message)
        return "ok"

    def showerror(self, title="出错", message="", **kw):
        self._dlg("error", title, message)
        return "ok"

    def askyesno(self, title="确认", message="", **kw):
        return self._dlg("ask", title, message, ask=True, yes="是", no="否")

    def askokcancel(self, title="确认", message="", **kw):
        return self._dlg("ask", title, message, ask=True)

    def showwarning_async(self, *a, **kw):
        return self.showwarning(*a, **kw)


messagebox_orig = messagebox      # 留一份原版给兜底用
messagebox = _UEMsgBox()          # 之后所有 messagebox.xxx 都走暗色弹窗


def install_context_menu():
    def editable(widget):
        try:
            return str(widget.cget("state")).lower() not in ("disabled", "readonly")
        except Exception:
            return True

    def do_paste(w):
        try:
            w.event_generate("<<Paste>>")
        except Exception:
            try:
                w.insert("insert", w.clipboard_get())
            except Exception:
                messagebox.showinfo("剪贴板是空的",
                                    "没读到内容。先在 Excel / 文档里复制一段再回来粘贴。")

    def show_menu(e):
        w = e.widget
        try:
            w.focus_set()
        except Exception:
            pass
        is_text = isinstance(w, tk.Text)
        # 让插入点跟到鼠标点击处，粘贴才会落在想放的位置
        try:
            if is_text:
                w.mark_set("insert", w.index("@%d,%d" % (e.x, e.y)))
            else:
                w.icursor(w.index("@%d" % e.x))
        except Exception:
            pass
        can_edit = editable(w)
        has_sel = False
        try:
            has_sel = bool(w.tag_ranges("sel")) if is_text else bool(w.selection_present())
        except Exception:
            pass

        m = tk.Menu(w, tearoff=0, font=("Microsoft YaHei", 9))
        if is_text and can_edit:
            m.add_command(label="撤销        Ctrl+Z",
                          command=lambda: w.edit_undo())
            m.add_separator()
        if has_sel:
            m.add_command(label="复制        Ctrl+C",
                          command=lambda: w.event_generate("<<Copy>>"))
        if can_edit:
            if has_sel:
                m.add_command(label="剪切        Ctrl+X",
                              command=lambda: w.event_generate("<<Cut>>"))
            m.add_command(label="粘贴        Ctrl+V", command=lambda: do_paste(w))
            m.add_separator()
        if is_text:
            m.add_command(label="全选        Ctrl+A",
                          command=lambda: w.tag_add("sel", "1.0", "end-1c"))
            if can_edit:
                m.add_command(label="清空", command=lambda: w.delete("1.0", "end"))
        else:
            m.add_command(label="全选        Ctrl+A",
                          command=lambda: (w.select_range(0, "end"),
                                           w.icursor("end")))
            if can_edit:
                m.add_command(label="清空", command=lambda: w.delete(0, "end"))
        w._ctx_menu = m          # 存一份，避免菜单一闪就没
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            try:
                m.grab_release()
            except Exception:
                pass
        return "break"

    for cls in ("Text", "Entry", "TEntry", "TCombobox", "TSpinbox", "Spinbox"):
        try:
            root.bind_class(cls, "<Button-3>", show_menu, add="+")
        except Exception:
            pass
    # Tk 在 Windows 上 Text 默认没绑 Ctrl+A，补一个全选
    for cls in ("Text",):
        for seq in ("<Control-a>", "<Control-A>"):
            try:
                root.bind_class(
                    cls, seq,
                    lambda e: (e.widget.tag_add("sel", "1.0", "end-1c"), "break")[1])
            except Exception:
                pass


install_context_menu()

# ---------------------------------------------------------------- 工具栏内容
# top 这个名字保留（下面还有 _top_relayout 之类的引用），但它现在指的是工具栏那一行。
top = _ue_toolbar


def _ue_sep(parent):
    """工具栏分组竖线：UE5 用一条 1px 线分组，不用空白堆。"""
    f = tk.Frame(parent, background=TH.LINE, width=1, height=26)
    f.pack(side="left", padx=8, pady=10)
    return f


def _ue_tb(parent, **kw):
    """工具栏按钮：比内容区的小一号，高度统一 32。"""
    kw.setdefault("height", 32)
    kw.setdefault("padx", 12)
    return TH.RoundButton(parent, **kw)


_tb_l = tk.Frame(top, background=TH.HL)
_tb_l.pack(side="left", padx=(6, 0), pady=6)
_tb_r = tk.Frame(top, background=TH.HL)
_tb_r.pack(side="right", padx=(0, 6), pady=6)

start_btn = _ue_tb(_tb_l, text="⚡ 启动后端", command=start_backend,
                   kind="accent", padx=16)
start_btn.pack(side="left")
free_btn = _ue_tb(_tb_l, text="▼ 关闭后端", command=lambda: stop_backend(False))
free_btn.pack(side="left", padx=(6, 0))
_ue_sep(_tb_l)

gen_btn = _ue_tb(_tb_l, text="▶  开始生成", command=generate, kind="accent",
                 padx=22, font=(TH.FONT[0], 10, "bold"))
gen_btn.pack(side="left")
stop_btn = _ue_tb(_tb_l, text="■ 停止", command=stop_gen, kind="danger",
                  padx=14, state="disabled")
stop_btn.pack(side="left", padx=(6, 0))
_ue_sep(_tb_l)

_ue_tb(_tb_l, text="＋ 一镜", command=lambda: add_shot(),
       kind="quiet").pack(side="left")
_ue_tb(_tb_l, text="⇩ 导入剧本", command=open_import,
       kind="quiet").pack(side="left")
_ue_tb(_tb_l, text="🎭 定妆照", command=open_chars,
       kind="quiet").pack(side="left")
_ue_tb(_tb_l, text="🏞 场景照", command=open_scenes,
       kind="quiet").pack(side="left")
_ue_tb(_tb_l, text="⟳ 重识别", command=_reidentify_all,
       kind="quiet").pack(side="left")
# 自动导演：按整集节奏排景别 / 角度 / 运镜 / 构图（导入新剧本时会自己跑一遍）
_ue_tb(_tb_l, text="🎬 自动导演", command=lambda: _direct_all(),
       kind="normal").pack(side="left")

# 右侧：视口 / 工坊 / 设置
_web_top_btn = _ue_tb(_tb_r, text="🌐 视口", command=lambda: _web_top_click())
_web_top_btn.pack(side="left")
_manju_top_btn = _ue_tb(_tb_r, text="🎬 工坊", command=lambda: _manju_col_toggle())
_manju_top_btn.pack(side="left", padx=(6, 0))
_ue_sep(_tb_r)
settings_btn = _ue_tb(_tb_r, text="⚙ 设置", command=open_settings, kind="quiet")
settings_btn.pack(side="left")
model_btn = _ue_tb(_tb_r, text="▣ 模型", command=open_models, kind="quiet")
model_btn.pack(side="left", padx=(4, 0))
help_btn = _ue_tb(_tb_r, text="▤ 说明", command=open_help, kind="quiet")
help_btn.pack(side="left", padx=(4, 0))
theme_btn = _ue_tb(_tb_r, text="◑ 主题", command=open_theme_picker, kind="quiet")
theme_btn.pack(side="left", padx=(4, 0))

# 工具栏窄了就按顺序收起次要按钮（PanedWindow 让中间视口能一直保持宽，
# 所以这里只是兜底：窗口被拖到很窄时不至于把按钮挤成半截）。
_TOP_COMPACT_W = 1080
_top_state = {"narrow": None}
_TOP_ORDER = [(start_btn, None), (free_btn, (6, 0)), (gen_btn, None),
              (stop_btn, (6, 0)), (_web_top_btn, None),
              (_manju_top_btn, (6, 0)), (settings_btn, None),
              (model_btn, (4, 0)), (help_btn, (4, 0)), (theme_btn, (4, 0))]
_TOP_SECONDARY = {free_btn, model_btn, help_btn, theme_btn}

# ---------------------------------------------------------------- 菜单条内容
# 用 tk.Menu 挂在自绘的标题条上（不用 root.config(menu=...) 是因为系统菜单
# 在 Windows 上永远是浅灰的，跟暗色主题对不上）。
_UE_POP = {"win": None, "unbind": None}


def _ue_menu_close(_e=None):
    """收起自绘菜单。"""
    w = _UE_POP.get("win")
    _UE_POP["win"] = None
    fid = _UE_POP.get("unbind")
    _UE_POP["unbind"] = None
    if fid:
        try:
            root.unbind("<Button-1>", fid)
        except Exception:
            pass
    if w is not None:
        try:
            w.destroy()
        except Exception:
            pass


def _ue_menu(text, items):
    """顶部菜单：自绘下拉面板。

    Tk 在 Windows 上给 tk_popup 的菜单画的是系统边框，-bd / -relief 都管不了它，
    深色主题下菜单四周会留一圈扎眼的白边（用户反馈「为什么是白色框框」）。
    干脆自己用 Toplevel + Frame 画：描边、分隔线、悬停色全跟着主题走。
    """
    b = tk.Label(_ue_menu_host, text=text, background=TH.PANEL,
                 foreground=TH.TEXT, font=(TH.FONT[0], 9), padx=10,
                 pady=2, cursor="hand2")
    b.pack(side="left")

    def _on_outside(_e=None):
        _ue_menu_close()

    def _open(_e=None):
        if _UE_POP.get("win") is not None:
            _ue_menu_close()
            return
        p = tk.Toplevel(root)
        p.withdraw()
        p.overrideredirect(True)
        try:
            p.configure(background=TH.LINE)
        except Exception:
            pass
        # 外圈 1px 用 padding 做描边：这样不会引出系统边框
        outer = tk.Frame(p, background=TH.LINE, padx=1, pady=1)
        outer.pack()
        inner = tk.Frame(outer, background=TH.FIELD)
        inner.pack()
        for it in items:
            if it == "-":
                tk.Frame(inner, background=TH.LINE, height=1).pack(
                    fill="x", padx=8, pady=4)
                continue
            label, cmd = it

            def _run(_e=None, c=cmd):
                _ue_menu_close()
                try:
                    c()
                except Exception as _ex:
                    _crash("菜单命令失败: %r" % (_ex,))

            row = tk.Label(inner, text=label, background=TH.FIELD,
                           foreground=TH.TEXT, font=(TH.FONT[0], 9),
                           anchor="w", padx=16, pady=6, cursor="hand2")
            row.pack(fill="x")
            row.bind("<Button-1>", _run)
            row.bind("<Enter>", lambda e, r=row: r.configure(background=TH.SEL_BG))
            row.bind("<Leave>", lambda e, r=row: r.configure(background=TH.FIELD))
        p.update_idletasks()
        w = max(p.winfo_reqwidth(), b.winfo_width())
        x = b.winfo_rootx()
        y = b.winfo_rooty() + b.winfo_height()
        try:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            if x + w > sw - 8:
                x = sw - 8 - w
            if y + p.winfo_reqheight() > sh - 8:
                y = b.winfo_rooty() - p.winfo_reqheight()
        except Exception:
            pass
        p.geometry("+%d+%d" % (x, y))
        p.deiconify()
        p.lift()
        _UE_POP["win"] = p

        # 点菜单外面任意位置就收起。这里必须延后一拍再绑：本次「点标签打开菜单」
        # 这个事件本身也会冒泡到 root，立刻绑的话菜单刚弹出就被自己关掉了。
        def _bind_out():
            if _UE_POP.get("win") is not p:
                return
            try:
                _UE_POP["unbind"] = root.bind("<Button-1>", _on_outside, add="+")
            except Exception:
                _UE_POP["unbind"] = None

        root.after(40, _bind_out)

    b.bind("<Button-1>", _open)
    b.bind("<Enter>", lambda e: b.configure(background=TH.ON))
    b.bind("<Leave>", lambda e: b.configure(background=TH.PANEL))
    return b


def _ue_toggle_pane(which):
    """视图菜单里折叠/展开三栏之一（拖分隔条也行，这只是快捷方式）。"""
    try:
        if which == "left":
            _left_toggle()
        elif which == "right":
            w = _ue_row.winfo_width()
            pos = _ue_sash_get(_ue_row, 1)
            if pos < w - 8:
                _LEFT_SHRINK["right"] = pos
                _ue_sash_set(_ue_row, 1, w)
            else:
                _ue_sash_set(_ue_row, 1, _LEFT_SHRINK.get("right") or (w - 400))
            _ue_refit_webs()
        elif which == "bottom":
            h = _ue_body.winfo_height()
            pos = _ue_sash_get(_ue_body, 0)
            if pos < h - 8:
                _LEFT_SHRINK["bottom"] = pos
                _ue_sash_set(_ue_body, 0, h)
            else:
                _ue_sash_set(_ue_body, 0, _LEFT_SHRINK.get("bottom") or (h - 240))
    except Exception:
        pass


_ue_menu("文件", [
    ("新建（清空分镜）", lambda: clear_shots(True)),
    ("导入剧本…", lambda: open_import()),
    ("载入示例", lambda: load_example()),
    "-",
    ("打开成片文件夹", lambda: open_output()),
    "-",
    ("退出", lambda: _on_app_close()),
])
_ue_menu("编辑", [
    ("添加一镜", lambda: add_shot()),
    ("复制选中镜", lambda: _dup_shot(_SEL["i"])),
    ("删除选中镜", lambda: del_shot_selected()),
    "-",
    ("上移", lambda: move_shot(-1)),
    ("下移", lambda: move_shot(1)),
    "-",
    ("重新识别景别/运镜/场景", lambda: _reidentify_all()),
])
_ue_menu("视图", [
    ("三者关系说明（顶部）", lambda: _toggle_intro()),
    "-",
    ("大纲面板", lambda: _ue_toggle_pane("left")),
    ("细节面板", lambda: _ue_toggle_pane("right")),
    ("底部面板", lambda: _ue_toggle_pane("bottom")),
    "-",
    ("无限画布（:3000）", lambda: _canvas_toggle()),
    ("ComfyUI 视口", lambda: _web_top_click()),
    ("视口最大化", lambda: _web_fullscreen()),
    "-",
    ("AI 工坊", lambda: _manju_col_toggle()),
    ("重置布局", lambda: _ue_reset_layout()),
])
def _toggle_auto_go():
    """工具菜单里的开关：导入剧本后要不要自动跑完整集。"""
    on = not _auto_go_enabled()
    _set_auto_go(on)
    if not on:
        _auto_go_cancel(quiet=True)
    set_status("导入后自动出片：%s" % ("开（导入 → 自动导演 → 等资产 → 开跑）" if on
                                  else "关（导入后只排分镜，手动点开始）"),
               "ok" if on else "warn")


def _toggle_qc():
    """自动审片开关：跑完抽帧查黑屏/纯色/冻帧，坏的镜头自动重渲。"""
    on = not bool(AP.CFG.get("qc_auto", True))
    try:
        c = dict(AP.CFG)
        c["qc_auto"] = on
        AP.save_config(c)
        AP.CFG["qc_auto"] = on
    except Exception:
        pass
    set_status("自动审片：%s" % ("开（坏镜自动重渲）" if on
                              else "关（跑完不质检，坏镜自己翻）"), "ok" if on else "warn")


def _toggle_face_board():
    """角色设定稿要不要多出一张脸部细节板（每个角色多等约 1 分钟）。"""
    on = not bool(AP.CFG.get("face_board", True))
    try:
        c = dict(AP.CFG)
        c["face_board"] = on
        AP.save_config(c)
        AP.CFG["face_board"] = on
    except Exception:
        pass
    set_status("角色设定稿脸部细节板：%s" % ("开" if on else "关"), "ok" if on else "warn")


def _toggle_auto_char():
    """新剧情自动匹配：不弹「自动定妆照」确认框，直接按剧本起草文案开跑。"""
    on = not _auto_char_enabled()
    _set_auto_char(on)
    set_status("新剧情自动匹配定妆照：%s" % (
        "开（导入即生成，不弹窗）" if on else "关（导入后弹窗让你改文案）"),
        "ok" if on else "warn")


_ue_menu("工具", [
    ("导入后自动出片（切换开关）", lambda: _toggle_auto_go()),
    ("新剧情自动匹配定妆照（切换开关）", lambda: _toggle_auto_char()),
    ("自动审片（切换开关）", lambda: _toggle_qc()),
    ("设定稿脸部细节板（切换开关）", lambda: _toggle_face_board()),
    ("运镜库（48 条）", lambda: open_motion_lib()),
    ("景别库（10 条）", lambda: open_size_lib()),
    ("古装发型库（13 款）", lambda: open_hair_lib()),
    "-",
    ("模型管理", lambda: open_models()),
    ("设置", lambda: open_settings()),
    ("主题配色", lambda: open_theme_picker()),
    ("音色试听", lambda: open_voice_preview()),
    "-",
    ("角色定妆照", lambda: open_chars()),
    ("场景照", lambda: open_scenes()),
])
_ue_menu("帮助", [
    ("使用说明", lambda: open_help()),
    ("提示词公式", lambda: open_prompt_help()),
    ("不会写剧情？", lambda: open_story_help()),
    ("画布怎么用", lambda: _web_help()),
])

# ---------------------------------------------------------------- 状态条内容
status_lbl = tk.Label(_ue_status, text="○ 后端未连接", foreground=TH.ERR,
                      background=TH.HL, font=TH.FONT_S, anchor="w")
status_lbl.pack(side="left", padx=(10, 0), pady=3)
_ue_stat_right = tk.Label(_ue_status, text="", foreground=TH.TEXT3,
                          background=TH.HL, font=(TH.FONT[0], 8), anchor="e")
_ue_stat_right.pack(side="right", padx=(0, 10))
try:
    _ue_stat_right.configure(text="ComfyUI :%s   ·   UE5 布局" % AP.CFG.get("port", 8188))
except Exception:
    pass


def _top_relayout(narrow=None):
    """工具栏现在横跨整个窗口（不再被右侧画布面板挤窄），窗口最小宽度 1200
    已经大于紧凑阈值，所以「窄了收按钮」这套逻辑不再需要 —— 保留空实现，
    免得下面几处调用点（_top_on_configure / 启动流程）报 NameError。"""
    return



def _top_on_configure(e):
    if e.widget is not top:
        return
    _top_relayout(e.width < _TOP_COMPACT_W)


top.bind("<Configure>", _top_on_configure, add="+")
# 「🎭 角色」「🏞 场景」现在在工具栏里，入口固定可见。
_guide_lines = (
    "① 点「⚡ 启动后端」，等状态条变绿「● 后端在线」（首次 1-2 分钟，需本机有 ComfyUI + N 卡）。\n"
    "② 没模型？点「▣ 模型」：已有就「📥 导入本地模型」，没有就「⬇ 下载全部缺失」。\n"
    "③ 剧情：点「⇩ 导入剧本」整段粘贴，或「文件 → 载入示例」。\n"
    "④ 左边大纲选一镜，右边「细节」填参数；工具栏「▶ 开始生成」，成片在 output 下。"
)


# ---------------------------------------------------------------- ③ 右栏：细节
_ue_header(_ue_right, "细节  DETAILS")
_rt_tabs = tk.Frame(_ue_right, background=TH.PANEL)
_rt_tabs.pack(fill="x", pady=(4, 0), padx=6)
# 「下一步做什么」固定挂在页签下面：以前它长在细节滚动区的最底下，内容一长
# 就沉到看不见的地方，要滚到底才发现（用户反馈「是不是应该放最上面」）。
_next_host = tk.Frame(_ue_right, background=TH.PANEL)
_next_host.pack(fill="x", padx=6, pady=(6, 2), after=_rt_tabs)
# 左边一条主色竖条：一眼能看出这是"该干什么"的提示，不是普通设置项
_next_edge = tk.Frame(_next_host, background=TH.ACCENT, width=3)
_next_edge.pack(side="left", fill="y")
_ue_detail_wrap = tk.Frame(_ue_right, background=TH.PANEL)
_ue_manju_wrap = tk.Frame(_ue_right, background=TH.PANEL)


def _ue_scroller(parent, bg=None):
    """给面板加竖直滚动条（UE5 的 Details 面板也是可滚的，字段多了要能翻）。"""
    bg = bg or TH.PANEL
    cv = tk.Canvas(parent, background=bg, highlightthickness=0, bd=0)
    sb = ttk.Scrollbar(parent, orient="vertical", command=cv.yview)
    inner = tk.Frame(cv, background=bg)
    win = cv.create_window((0, 0), window=inner, anchor="nw")
    cv.configure(yscrollcommand=sb.set)
    cv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    inner.bind("<Configure>",
               lambda e: cv.configure(scrollregion=cv.bbox("all")))
    cv.bind("<Configure>",
            lambda e: cv.itemconfigure(win, width=max(1, e.width)))

    def _wheel(e):
        if getattr(e, "num", 0) == 4:
            d = -3
        elif getattr(e, "num", 0) == 5:
            d = 3
        else:
            d = -1 if getattr(e, "delta", 0) > 0 else 1
        try:
            cv.yview_scroll(int(d), "units")
        except Exception:
            pass
        return "break"
    cv.bind("<Enter>", lambda e: cv.bind_all("<MouseWheel>", _wheel))
    cv.bind("<Leave>", lambda e: cv.unbind_all("<MouseWheel>"))
    return cv, inner


_rt_cv, _detail_body = _ue_scroller(_ue_detail_wrap)

# 右栏页签：细节 / AI 工坊。工坊原来是挤在主界面的一根常驻列，现在收进右栏 ——
# 需要时点开，不需要时视口能占满，三栏抢宽度的问题就此消失。
_rt_state = {"tab": "detail"}


def _rt_tab_btn(text, key):
    b = tk.Label(_rt_tabs, text=text, background=TH.PANEL, foreground=TH.TEXT2,
                 font=(TH.FONT[0], 9), padx=12, pady=4, cursor="hand2")
    b.pack(side="left", padx=(0, 2))
    b.bind("<Button-1>", lambda e: _rt_show(key))
    b.bind("<Enter>", lambda e: b.configure(background=TH.HL))
    b.bind("<Leave>", lambda e: b.configure(
        background=(TH.HL if _rt_state["tab"] == key else TH.PANEL)))
    return b


def _rt_show(key):
    _rt_state["tab"] = key
    for w in (_ue_detail_wrap, _ue_manju_wrap):
        try:
            w.pack_forget()
        except Exception:
            pass
    if key == "manju":
        _ensure_manju_col()
        try:
            manju_col_visible["on"] = True
            _manju_update_btn()
        except Exception:
            pass
        _ue_manju_wrap.pack(fill="both", expand=True)
    else:
        _ue_detail_wrap.pack(fill="both", expand=True)
        try:
            manju_col_visible["on"] = False
            _manju_update_btn()
        except Exception:
            pass
    for b, k in ((_rt_b_detail, "detail"), (_rt_b_manju, "manju")):
        try:
            b.configure(background=(TH.HL if k == key else TH.PANEL),
                        foreground=(TH.TEXT if k == key else TH.TEXT2))
        except Exception:
            pass


_rt_b_detail = _rt_tab_btn("  细节  ", "detail")
_rt_b_manju = _rt_tab_btn("  AI 工坊  ", "manju")

# 细节页顶部：当前选中那一镜的表单（内容由 _detail_show 动态重建）
_detail_host = tk.Frame(_detail_body, background=TH.PANEL)
_detail_host.pack(fill="x", padx=0, pady=(4, 0))

# 「下一步做什么」提示：挂在右栏顶部那条常驻位里（_next_host），不占视口
next_bar = tk.Frame(_next_host, background=TH.PANEL)
next_bar.columnconfigure(0, weight=1)

# 设置区（项目设置）：进右栏细节页
cfg = ttk.LabelFrame(_detail_body, text=" 基本设置 ", padding=4)
cfg.pack(fill="x", padx=6, pady=6)
title_var = tk.StringVar(value="")     # 不预置示例剧名：导入剧本时按文件名自动填，见 import_from_file
voice_var = tk.StringVar(value=list(VOICES.keys())[0])
voice_rate_var = tk.StringVar(value=RATE_DEFAULT)
style_var = tk.StringVar(value=DEFAULT_STYLE)
# 124 是 H3 的训练下限（5.2 秒）：低于它模型会跑出分布 —— 动作做不完、
# 音频发虚、人物中途变形。以前这里写 22，用户以为能出 0.9 秒的镜头，
# 实际每次都被抬到 124，看着就像"124 帧又出现了"。
length_var = tk.StringVar(value="124")
turbo_var = tk.BooleanVar(value=True)
# 画质/速度档：分辨率 + 采样步数（3060 12G 实测：采样只占小头，模型加载+文本编码才是大头）
# 实测（22帧/镜，模型已加载）：448x800·4步 ≈138秒/镜；576x1024·8步 ≈175秒/镜
SPEED_PRESETS = {
    # 名字别太长：下拉框宽度有限，写太长选中项会被截（2026-10-29 精简）
    # 【2026-10-29 晚】「极速 448×800·4步」已按用户要求**下线**：
    # 4 步蒸馏在低分辨率下脸会变形、音色发糊，只能用来验证流程，不该出现在成品档位里。
    # 「试看」保留但改名点明用途（只验证流程），默认真的是高清。
    "试看 288×512·4步（只验证流程）": ((288, 512), 4),
    "标准 576×1024·8步": ((576, 1024), 8),
    "高清 640×1152·8步": ((640, 1152), 8),
}
_SPEED_KEYS = list(SPEED_PRESETS.keys())
# 默认给高清：标准档只有 1024x576，拉到 1280x720 会明显发糊，
# 上一版用户反馈"效果差"一半来自这里。跑不动再手动退回标准。
speed_var = tk.StringVar(value=_SPEED_KEYS[-1])   # 默认最高档（高清）
# 扩散模型精度：按本机实际存在的文件自动列出（换电脑 / 换量化版本都能选到）
# 12G 卡：Q3 体积 8.4G 基本全进显存最快；Q4 10.6G 更清晰但要换页
FoundUnets = MM.scan_unets()
if FoundUnets:
    MODEL_PRESETS = {u["label"]: u["fname"] for u in FoundUnets}
    _pref = next((k for k in MODEL_PRESETS if "推荐" in k), None)
    model_var = tk.StringVar(value=_pref or list(MODEL_PRESETS.keys())[0])
else:
    MODEL_PRESETS = {"⚠ 没找到模型，点上面「📦 模型」去下载": ""}
    model_var = tk.StringVar(value=list(MODEL_PRESETS.keys())[0])

# ===== 基本设置：自适应网格 =====
# 宽栏（≥820）排成「标签/控件 两对一行」的 4 列；主面板被右边画布挤窄（~640）时
# 自动折成「一行一对」的 2 列。以前写死 4 列，栏一窄右边那对就被边界切断
# （用户截图里「画质档 / 每镜帧数 / 模型精度」只露出半截就是这原因）。
_CFG_NARROW_W = 580
_cfg_state = {"narrow": None}

_lbl_title = ttk.Label(cfg, text="剧名：")
_e_title = ttk.Entry(cfg, textvariable=title_var)

_lbl_speed = ttk.Label(cfg, text="画质档：")
_cb_speed = ttk.Combobox(cfg, textvariable=speed_var,
                         values=list(SPEED_PRESETS.keys()),
                         width=19, state="readonly")

_lbl_voice = ttk.Label(cfg, text="旁白音色：")
_voice_row = ttk.Frame(cfg)
voice_cb = ttk.Combobox(_voice_row, textvariable=voice_var,
                         values=list(VOICES.keys()), width=10, state="readonly")
voice_cb.pack(side="left")
TH.RoundButton(_voice_row, text="🔊", height=28, padx=6,
               command=lambda: open_voice_preview(voice_var.get())).pack(
    side="left", padx=(4, 0))
ttk.Label(_voice_row, text="速").pack(side="left", padx=(6, 2))
rate_cb = ttk.Combobox(_voice_row, textvariable=voice_rate_var,
                        values=list(RATES.keys()), width=7, state="readonly")
rate_cb.pack(side="left")

_lbl_len = ttk.Label(cfg, text="每镜帧数：")
auto_len_var = tk.BooleanVar(value=True)
_len_row = ttk.Frame(cfg)
length_entry = ttk.Entry(_len_row, textvariable=length_var, width=8)
length_entry.pack(side="left")


def _auto_len_toggle(*_):
    native = bool(native_var.get())
    auto = native or bool(auto_len_var.get())
    try:
        length_entry.configure(state="disabled" if auto else "normal")
    except Exception:
        pass
    try:
        for _r in shot_rows:
            _de = _r.get("dur_e")
            if _de is not None:
                _de.configure(state="disabled" if auto else "normal")
    except Exception:
        pass
    try:
        voice_cb.configure(state="disabled" if native else "readonly")
        rate_cb.configure(state="disabled" if native else "readonly")
    except Exception:
        pass
    try:
        auto_cb.configure(state="disabled" if native else "normal")
    except Exception:
        pass


auto_cb = ttk.Checkbutton(_len_row, text="自动定帧", variable=auto_len_var,
                          command=_auto_len_toggle)
auto_cb.pack(side="left", padx=(8, 0))
# 原来这里还有一枚「A 方案每镜最少 124 帧（5.2 秒）」的说明标签，主面板被挤到
# 640 时正是它把「每镜帧数」整行顶出边界。说明并进复选框文案，少一个控件少一截。
_len_hint = None

native_var = tk.BooleanVar(value=True)   # A 方案：台词进 prompt，模型自己发声
_lbl_preset = ttk.Label(cfg, text="风格预设：")
preset_var = tk.StringVar(value="写实电影")
_cb_preset = ttk.Combobox(cfg, textvariable=preset_var,
                          values=list(PRESETS.keys()), width=13,
                          state="readonly")
_lbl_aspect = ttk.Label(cfg, text="画幅：")
ASPECTS = {"竖屏 9:16（抖音·快手）": "portrait",
           "横屏 16:9（B站·西瓜）": "landscape"}
ASPECT_DEFAULT = "横屏 16:9（B站·西瓜）"
aspect_var = tk.StringVar(value=ASPECT_DEFAULT)
_cb_aspect = ttk.Combobox(cfg, textvariable=aspect_var, values=list(ASPECTS.keys()),
                          width=19, state="readonly")

def on_preset(*_):
    style_var.set(PRESETS.get(preset_var.get(), ""))
preset_var.trace_add("write", lambda *a: on_preset())

_lbl_style = ttk.Label(cfg, text="统一风格后缀：")
_e_style = ttk.Entry(cfg, textvariable=style_var)

_lbl_model = ttk.Label(cfg, text="模型精度：")
model_cb = ttk.Combobox(cfg, textvariable=model_var,
                        values=list(MODEL_PRESETS.keys()), width=21,
                        state="readonly")
te4b_var = tk.BooleanVar(value=True)
_te4b_has = os.path.exists(os.path.join(AP.WORKDIR, "models", "text_encoders",
                                        "qwen3vl_4b_int8_convrot.safetensors"))
_cb_4b = ttk.Checkbutton(cfg, text="🚀 4B 轻量编码（省 11G 内存）",
                         variable=te4b_var)
if not _te4b_has:
    te4b_var.set(False)

_cb_native = ttk.Checkbutton(cfg, text="🎙 模型原生发声（A 方案）",
                             variable=native_var,
                             command=_auto_len_toggle)
auto_prompt_var = tk.BooleanVar(value=True)
_cb_autop = ttk.Checkbutton(cfg, text="✦ 自动补提示词",
                            variable=auto_prompt_var)
hint = tk.Text(cfg, height=3, font=TH.FONT_XS, wrap="word",
               foreground=TH.TEXT3, relief="flat", background=TH.BG, width=58,
               state="disabled", cursor="arrow")
hint.configure(state="normal")
hint.insert("1.0", "A 方案：台词交给模型自己念（口型最准），但每镜嗓音是模型现编的、不固定；"
                   "想全片一个人一个嗓子，就取消勾选改用 B 方案（edge-tts）。")
hint.configure(state="disabled")

_CFG_WIDGETS = (_lbl_title, _e_title, _lbl_speed, _cb_speed, _lbl_voice,
                _voice_row, _lbl_len, _len_row, _lbl_preset, _cb_preset,
                _lbl_aspect, _cb_aspect, _lbl_style, _e_style, _lbl_model,
                model_cb, _cb_4b, _cb_native, _cb_autop, hint)


def _cfg_reflow(narrow=None):
    """按栏宽重排基本设置：宽=两对一行（4 列），窄=一行一对（2 列）。"""
    try:
        if narrow is None:
            narrow = cfg.winfo_width() < _CFG_NARROW_W
        if _cfg_state["narrow"] == narrow:
            return
        _cfg_state["narrow"] = narrow
        for _w in _CFG_WIDGETS:
            try:
                _w.grid_forget()
            except Exception:
                pass
        if narrow:
            _pairs = ((_lbl_title, _e_title), (_lbl_speed, _cb_speed),
                      (_lbl_voice, _voice_row), (_lbl_len, _len_row),
                      (_lbl_preset, _cb_preset), (_lbl_aspect, _cb_aspect))
            for _i, _pw in enumerate(_pairs):
                _pw[0].grid(row=_i, column=0, sticky="w", pady=2)
                _pw[1].grid(row=_i, column=1, columnspan=3, sticky="ew",
                            pady=2, padx=(6, 0))
            _lbl_style.grid(row=6, column=0, sticky="w", pady=2)
            _e_style.grid(row=6, column=1, columnspan=3, sticky="ew", pady=2)
            _lbl_model.grid(row=7, column=0, sticky="w", pady=2)
            model_cb.grid(row=7, column=1, columnspan=3, sticky="ew", pady=2)
            _cb_4b.grid(row=8, column=0, columnspan=4, sticky="w", pady=(2, 0))
            _cb_native.grid(row=9, column=0, columnspan=4, sticky="w", pady=(2, 0))
            _cb_autop.grid(row=10, column=0, columnspan=4, sticky="w", pady=(2, 0))
            hint.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(2, 0))
            cfg.columnconfigure(0, minsize=60)
            cfg.columnconfigure(1, weight=1, minsize=120)
            cfg.columnconfigure(2, weight=0, minsize=0)
            cfg.columnconfigure(3, weight=0, minsize=0)
        else:
            _lbl_title.grid(row=0, column=0, sticky="w", pady=2)
            _e_title.grid(row=0, column=1, sticky="ew", pady=2, padx=(0, 10))
            _lbl_speed.grid(row=0, column=2, sticky="w", pady=2)
            _cb_speed.grid(row=0, column=3, sticky="w", pady=2)
            _lbl_voice.grid(row=1, column=0, sticky="w", pady=2)
            _voice_row.grid(row=1, column=1, sticky="w", pady=2, padx=(0, 10))
            _lbl_len.grid(row=1, column=2, sticky="w", pady=2)
            _len_row.grid(row=1, column=3, sticky="w", pady=2)
            _lbl_preset.grid(row=2, column=0, sticky="w", pady=2)
            _cb_preset.grid(row=2, column=1, sticky="w", pady=2, padx=(0, 10))
            _lbl_aspect.grid(row=2, column=2, sticky="w", pady=2)
            _cb_aspect.grid(row=2, column=3, sticky="w", pady=2)
            _lbl_style.grid(row=3, column=0, sticky="w", pady=2)
            _e_style.grid(row=3, column=1, columnspan=3, sticky="ew", pady=2)
            _lbl_model.grid(row=4, column=0, sticky="w", pady=2)
            model_cb.grid(row=4, column=1, sticky="w", pady=2, padx=(0, 10))
            _cb_4b.grid(row=4, column=2, columnspan=2, sticky="w",
                        padx=(8, 0), pady=2)
            _cb_native.grid(row=5, column=0, columnspan=2, sticky="w",
                            pady=(4, 0))
            _cb_autop.grid(row=5, column=2, columnspan=2, sticky="w",
                           padx=(8, 0), pady=(4, 0))
            hint.grid(row=6, column=0, columnspan=4, sticky="w", pady=(2, 0))
            # 左半（剧名 / 旁白音色）比右半（画质档 / 每镜帧数）更需要宽度：
            # 音色那一行是「下拉 + 试听 + 速 + 语速」四件套，等宽分会被掐掉语速框。
            cfg.columnconfigure(0, minsize=64)
            cfg.columnconfigure(1, weight=2, minsize=205)
            cfg.columnconfigure(2, minsize=64)
            cfg.columnconfigure(3, weight=1, minsize=165)
    except Exception as _e:
        _crash("基本设置重排失败: %r" % (_e,))


def _cfg_on_configure(e):
    if e.widget is not cfg:
        return
    _cfg_reflow(e.width < _CFG_NARROW_W)


cfg.bind("<Configure>", _cfg_on_configure, add="+")
_cfg_reflow(False)


# ---------------------------------------------------------------- ③ 左栏：大纲
# UE5 的 Outliner：一列紧凑条目，点一条右边的 Details 就换成那一条。
# 这里每一镜就是一条；上面的搜索框按台词/描述过滤（对应 UE5 大纲的搜索栏）。
_ue_header(_ue_left, "大纲  OUTLINER")

_fbar = tk.Frame(_ue_left, background=TH.PANEL)
_fbar.pack(fill="x", padx=6, pady=(6, 2))
_filter_var = tk.StringVar(value="")
_filter_e = ttk.Entry(_fbar, textvariable=_filter_var, font=TH.FONT_S)
_filter_e.pack(fill="x", ipady=1)


def _on_filter(*_):
    _outline_refresh()


_filter_var.trace_add("write", lambda *a: _on_filter())

# 已绑定角色的小头像：一眼看到这剧有哪几个人（原来是分镜工具栏里那一排）
char_faces = tk.Frame(_ue_left, background=TH.PANEL)
char_faces.pack(fill="x", padx=6, pady=(0, 2))
char_stat_lbl = tk.Label(char_faces, text="", foreground=TH.TEXT3,
                         background=TH.PANEL, font=(TH.FONT[0], 8))
char_stat_lbl.pack(side="left", padx=(0, 6))
_update_char_stat()

# 大纲主体（可滚动）
_cvhost = tk.Frame(_ue_left, background=TH.BG)
_cvhost.pack(fill="both", expand=True, padx=(4, 0), pady=(2, 0))
canvas = tk.Canvas(_cvhost, height=120, background=TH.BG,
                   highlightthickness=0, bd=0)
scroll = ttk.Scrollbar(_cvhost, orient="vertical", command=canvas.yview)
shots_inner = tk.Frame(canvas, background=TH.BG)
_canvas_win = canvas.create_window((0, 0), window=shots_inner, anchor="nw")
canvas.configure(yscrollcommand=scroll.set)
canvas.pack(side="left", fill="both", expand=True)
scroll.pack(side="right", fill="y")

# 画布宽度跟着容器走，否则内层 frame 用自然宽度，右侧的按钮会被裁掉
_fit_last = [0]


def _fit_canvas_width(e):
    if e.width == _fit_last[0]:
        return
    _fit_last[0] = e.width
    try:
        canvas.itemconfigure(_canvas_win, width=e.width)
    except Exception:
        pass
canvas.bind("<Configure>", _fit_canvas_width, add="+")

# 大纲脚部：增删与上下移（UE5 大纲下面那排小按钮）
_obar = tk.Frame(_ue_left, background=TH.PANEL)
_obar.pack(fill="x", padx=6, pady=6)
_ue_tb(_obar, text="＋", command=lambda: add_shot(), height=28, padx=10).pack(
    side="left")
# 导入是新手第一件事，别混在 🗑 ↑ ↓ 那排灰按钮里 —— 主色按钮才看得见
_ue_tb(_obar, text="⇩ 导入剧本", command=open_import, height=28, padx=12,
       kind="accent").pack(side="left", padx=(4, 0))
_ue_tb(_obar, text="🗑", command=lambda: del_shot_selected(),
       height=28, padx=10).pack(side="right")
_ue_tb(_obar, text="↑", command=lambda: move_shot(-1), height=28, padx=9).pack(
    side="right", padx=(0, 4))
_ue_tb(_obar, text="↓", command=lambda: move_shot(1), height=28, padx=9).pack(
    side="right", padx=(0, 4))


def _scrollable_ancestor(node):
    """从指针下的控件往上找最近的「带滚动条的画布」；找不到返回 None。"""
    while node is not None:
        try:
            if isinstance(node, tk.Canvas):
                try:
                    if node.cget("yscrollcommand"):
                        return node
                except Exception:
                    pass
        except Exception:
            pass
        try:
            node = node.master
        except Exception:
            node = None
    return None


def _wheel_on_combo(event):
    """指针停在只读下拉框上滚轮：滚**面板**，不换选项。

    ttk 自带的 TCombobox 类绑定会在滚轮时直接改选中项（ttk::combobox::Scroll），
    而且它比任何 root 绑定都先执行 —— 用户在右栏随手滚一下，「画幅」就从横屏变竖屏、
    「画质档」从高清变预览，还完全没提示（用户反馈「鼠标指在上面滚动就变了」）。
    这里直接把这个类绑定换掉，滚轮改为滚动所在的滚动面板。
    """
    try:
        cv = _scrollable_ancestor(event.widget)
        if cv is not None:
            cv.yview_scroll(int(-1 * (event.delta / 120)), "units")
    except Exception:
        pass
    return "break"


def _on_mousewheel(event):
    """滚轮：只滚「指针所在」的那个可滚动区域，不在任何可滚动区域内就不滚。

    - 指针落在某个带滚动条的画布（分镜区 / 角色卡片弹窗）范围内时，滚那个画布 ——
      即便指针正停在里面的文本框 / 输入框上也一样。这是「滚轮不要只滚框里的字」的关键：
      内层文本框不再独吞滚轮，整列跟着走；
    - 只有那种「独立、不在任何滚动画布里」的多行文本框（运行日志等）才自己滚；
    - 指针在设置项、按钮、空白处等无滚动区域的位置时，滚轮不滚动任何东西。
    """
    try:
        w = root.winfo_containing(*root.winfo_pointerxy())
    except Exception:
        w = None
    # 优先：指针位于某个带滚动条的画布内（含画布里的文本框 / 输入框）→ 滚那个画布。
    tgt = _scrollable_ancestor(w)
    if tgt is not None:
        tgt.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"
    # 回退：独立、不在任何滚动画布里的多行文本框（如运行日志）才自己滚
    if isinstance(w, tk.Text) and w.cget("yscrollcommand"):
        try:
            w.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        except Exception:
            return None
    return None


# 换掉 ttk 的滚轮换值绑定（下拉框 / 数值框），滚轮一律当"滚面板"
for _cls in ("TCombobox", "TSpinbox", "Spinbox"):
    try:
        root.bind_class(_cls, "<MouseWheel>", _wheel_on_combo)
    except Exception:
        pass


root.bind_all("<MouseWheel>", _on_mousewheel, add="+")

canvas.pack(side="left", fill="both", expand=True)
scroll.pack(side="right", fill="y")
def _refresh_scrollregion():
    try:
        canvas.configure(scrollregion=canvas.bbox("all"))
    except Exception:
        pass


def _on_shots_configure(_e):
    # 缩放/内容变化时重算滚动区；用 after_idle 合并连发事件，
    # 避免拖窗口时每一帧都全量遍历所有分镜控件（卡顿的次要来源）
    root.after_idle(_refresh_scrollregion)


shots_inner.bind("<Configure>", _on_shots_configure)
shots_inner.columnconfigure(0, weight=1)

# 操作按钮（原来那一排「添加一镜 / 导入剧本 / 开始生成」已经搬进工具栏和左栏脚部）
try:
    _auto_len_toggle()          # 让界面初态跟默认开关一致（A 方案默认开）
except Exception:
    pass

# ---------------------------------------------------------------- ④ 底部面板
# UE5 底部是 Content Browser + Output Log；这里对应 资产浏览器 / 输出日志 / 进度。
# ---- 底部三合一：进度常驻一行，下面「内容浏览器 | 运行日志」并排 ----
# 原来是三个 Tab（内容浏览器 / 输出日志 / 进度），生成时程序还自动切到「进度」，
# 日志就看不见了；想对照角色和日志得来回点。现在三样同时在场：
#   上：进度条（永远在，不用切页）
#   下：左边资产缩略图，右边运行日志（中间的分隔条可拖）
_ue_bottom_nb = None          # Tab 容器已取消（别处可能还有引用，置空最安全）

_ue_tab_prog = ttk.Frame(_ue_bottom, padding=(3, 2))
_ue_tab_prog.pack(fill="x", side="top")
tk.Frame(_ue_bottom, background=TH.LINE, height=1).pack(fill="x", side="top")

_ue_bot_split = tk.PanedWindow(_ue_bottom, orient="horizontal", bd=0,
                               sashwidth=5, sashrelief="flat",
                               background=TH.LINE, borderwidth=0,
                               opaqueresize=True)
_ue_bot_split.pack(fill="both", expand=True, side="top")

_ue_tab_asset = ttk.Frame(_ue_bot_split, padding=(2, 0, 0, 0))
_ue_tab_log = ttk.Frame(_ue_bot_split, padding=(0, 0, 2, 0))
_ue_bot_split.add(_ue_tab_asset, minsize=140, width=340, stretch="never")
_ue_bot_split.add(_ue_tab_log, minsize=240, stretch="always")

# 原来靠 Tab 标签说明这块是什么，现在常驻了，给它一行小字标题
_ab_head = tk.Frame(_ue_tab_asset, background=TH.PANEL)
_ab_head.pack(fill="x")
tk.Label(_ab_head, text="内容浏览器", background=TH.PANEL,
         foreground=TH.TEXT3, font=(TH.FONT[0], 8)).pack(side="left", padx=6,
                                                         pady=(2, 1))
_ab_cv, _ab_inner = _ue_scroller(_ue_tab_asset)
# 缩略图区给一个实在的初始高度：Canvas 默认只请求很小的尺寸，底部面板就会矮到
# 把角色格切掉半个（三合一之后三样都要装得下）
try:
    _ab_cv.configure(height=140)
except Exception:
    pass


def _ue_tile(parent, ph, title, sub="", cmd=None):
    """内容浏览器里的一个资产格（缩略图 + 名字），像 UE5 的 Asset Tile。"""
    t = tk.Frame(parent, background=TH.PANEL, bd=0, cursor="hand2",
                 highlightthickness=1, highlightbackground=TH.LINE)
    t.pack(side="left", padx=4, pady=2)
    lb = tk.Label(t, image=ph, text=("" if ph else "🎬"), compound="top",
                  background=TH.FIELD, foreground=TH.TEXT3,
                  font=(TH.FONT[0], 12), width=76, height=76)
    lb.pack(padx=3, pady=(3, 0))
    if ph:
        lb.image = ph
    tk.Label(t, text=title[:10], background=TH.PANEL, foreground=TH.TEXT2,
             font=(TH.FONT[0], 8), anchor="w").pack(fill="x", padx=4)
    tk.Label(t, text=sub[:12], background=TH.PANEL, foreground=TH.TEXT3,
             font=(TH.FONT[0], 7), anchor="w").pack(fill="x", padx=4, pady=(0, 3))
    if cmd:
        for w in (t, lb):
            w.bind("<Button-1>", lambda e: cmd())
    return t


def _ue_assets_refresh():
    """重画内容浏览器：角色定妆照 / 场景照 / 最近成片。"""
    try:
        for w in _ab_inner.winfo_children():
            w.destroy()
    except Exception:
        return

    def _section(title, right_btn=None, btn_cmd=None):
        h = tk.Frame(_ab_inner, background=TH.BG)
        h.pack(fill="x", pady=(4, 0), padx=2)
        tk.Label(h, text=title, background=TH.BG, foreground=TH.TEXT3,
                 font=(TH.FONT[0], 8)).pack(side="left")
        if right_btn:
            b = tk.Label(h, text=right_btn, background=TH.BG,
                         foreground=TH.ACCENT, font=(TH.FONT[0], 8),
                         cursor="hand2")
            b.pack(side="right")
            b.bind("<Button-1>", lambda e: btn_cmd())
        row = tk.Frame(_ab_inner, background=TH.BG)
        row.pack(fill="x")
        return row

    try:
        row = _section("角色  CHARACTERS", "打开角色库 ›", open_chars)
        if CHAR_REFS:
            for nm, fn in list(CHAR_REFS.items())[:12]:
                ph = _char_thumb(os.path.join(AP.INPUTDIR, fn), 76)
                _ue_tile(row, ph, nm, "定妆照", open_chars)
        else:
            _nobody_txt = ("本片全是空镜·无人物，不需要角色定妆照"
                           if (shot_rows and not _needs_char_refs()) else
                           "还没有角色定妆照（点右上「打开角色库」生成）")
            tk.Label(row, text=_nobody_txt,
                     background=TH.BG, foreground=TH.TEXT3,
                     font=(TH.FONT[0], 8)).pack(side="left", padx=6, pady=8)
    except Exception as _e:
        _crash("内容浏览器: 角色区失败 %r" % (_e,))

    try:
        row = _section("场景  SCENES", "打开场景库 ›", open_scenes)
        if SCENE_REFS:
            for nm, fn in list(SCENE_REFS.items())[:12]:
                ph = _char_thumb(os.path.join(AP.INPUTDIR, fn), 76)
                _ue_tile(row, ph, nm, "场景照", open_scenes)
        else:
            tk.Label(row, text="还没有场景照（同一场景的多镜共用一张，背景才一致）",
                     background=TH.BG, foreground=TH.TEXT3,
                     font=(TH.FONT[0], 8)).pack(side="left", padx=6, pady=8)
    except Exception as _e:
        _crash("内容浏览器: 场景区失败 %r" % (_e,))

    try:
        row = _section("成片  OUTPUT", "打开文件夹 ›", open_output)
        outs = sorted(glob.glob(os.path.join(AP.OUTDIR, "*.mp4")),
                      key=lambda p: os.path.getmtime(p), reverse=True)[:10]
        if outs:
            for p in outs:
                _ue_tile(row, None, os.path.basename(p),
                         _hms(max(0, time.time() - os.path.getmtime(p))) + "前",
                         lambda pp=p: os.startfile(pp))
        else:
            tk.Label(row, text="还没有成片（生成完自动出现在这里）",
                     background=TH.BG, foreground=TH.TEXT3,
                     font=(TH.FONT[0], 8)).pack(side="left", padx=6, pady=8)
    except Exception as _e:
        _crash("内容浏览器: 成片区失败 %r" % (_e,))


_UE_ASSET_SIG = {"sig": None}


def _ue_assets_sig():
    """内容浏览器的内容指纹：角色/场景绑定 + output 里的成片。"""
    try:
        s1 = tuple(sorted(CHAR_REFS.items()))
        s2 = tuple(sorted(SCENE_REFS.items()))
    except Exception:
        s1 = s2 = ()
    try:
        s3 = tuple(sorted(f for f in os.listdir(OUTDIR)
                          if f.lower().endswith(".mp4"))[-12:])
    except Exception:
        s3 = ()
    return (s1, s2, s3)


def _ue_assets_tick():
    """常驻了就得自己盯着更新：指纹变了才重画（每 2.5 秒看一眼，不会闪）。"""
    try:
        sig = _ue_assets_sig()
        if sig != _UE_ASSET_SIG["sig"]:
            _UE_ASSET_SIG["sig"] = sig
            _ue_assets_refresh()
    except Exception:
        pass
    try:
        root.after(2500, _ue_assets_tick)
    except Exception:
        pass


def _ue_on_tab(_e=None):
    """(历史遗留) Tab 已经取消，留着空壳兼容旧调用。"""
    try:
        _ue_assets_refresh()
    except Exception:
        pass


root.after(1500, _ue_assets_tick)

# ---- 「下一步做什么」提示条 -------------------------------------------------
# 新手最常卡在「现在该点哪儿」。这里按当前进度算出一条建议，点一下就做掉：
# 启动后端 → 导入剧本 → 生成定妆照锁脸 → 补参考角色 → 开始生成 → 打开成片。
next_bar.pack(side="left", fill="both", expand=True, padx=(8, 0))
next_bar.columnconfigure(0, weight=1)
# 两行结构：第一行「该干什么」一句加粗大字 + 按钮，第二行才是估算/细节小字。
# 以前全塞进一行，120 多字挤在 250px 宽里折成五六行小字，"点开始生成"这个真正的
# 动作沉在最后一句，等于没提示（用户反馈「已锚定 3 镜但没提示我该干什么」）。
next_btn = TH.RoundButton(next_bar, text="", command=lambda: _next_do(),
                          kind="accent", height=28, padx=10)
next_btn.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(8, 0), pady=(1, 0))
next_lbl = ttk.Label(next_bar, text="", font=(TH.FONT[0], 10, "bold"), anchor="w",
                     wraplength=250, justify="left")
next_lbl.grid(row=0, column=0, sticky="w", pady=(2, 0))
next_sub = ttk.Label(next_bar, text="", font=(TH.FONT[0], 8), anchor="w",
                     foreground=TH.TEXT3, wraplength=250, justify="left")
next_sub.grid(row=1, column=0, sticky="w", pady=(2, 0))
_next_sep = tk.Frame(next_bar, background=TH.LINE, height=1)
_next_sep.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(5, 0))


def _next_wrap(_e=None):
    """右栏可以被拖得很窄：按文字列的实际宽度重设折行宽度。

    不能拿 _next_host 的总宽算 —— 右边还站着一个按钮占了一列，按总宽设
    wraplength 会让小字伸到按钮底下、被面板边缘切掉（实测第二行末尾被截）。
    """
    try:
        w = next_bar.winfo_width()
        if w <= 80:
            return
        try:
            bw = (next_btn.winfo_width() if next_btn.winfo_ismapped()
                  else next_btn.winfo_reqwidth())
        except Exception:
            bw = 90
        avail = max(140, w - bw - 12)
        next_lbl.configure(wraplength=avail)
        next_sub.configure(wraplength=avail)
    except Exception:
        pass


try:
    next_bar.bind("<Configure>", _next_wrap, add="+")
except Exception:
    pass

_GEN_RUNNING = [False]
_ONLINE = [False]      # 没探过之前先当离线：宁可多探一次，也别放行到没起的后端
_DONE_AT = [0.0]        # 上次生成结束的时间戳：90 秒内优先提示「打开成片」
_next_act = [None]


def _probe_online():
    """后端在不在线单独用后台线程探，别让 3 秒超时卡住界面。

    【2026-10-29 修】以前只更新 _ONLINE 这个内部标志，状态栏还是开局那句
    "○ 后端未连接" —— 后端明明在跑（比如上次没关、或从别的窗口启动的），
    界面照样显示未连接，用户以为必须再点一次「启动后端」。
    现在状态一变就同步状态栏：探测到在线自动变绿，掉线自动变红。
    """
    def work():
        try:
            ok = check_online()
            prev = _ONLINE[0]
            _ONLINE[0] = ok
            if ok and not prev:
                root.after(0, lambda: set_status("● 后端在线", "green"))
                root.after(0, lambda: _set_starting(False))
                # 排队的活（新剧定妆照/场景照）趁这一刻自动接着跑
                root.after(0, _flush_backend_queue)
            elif (not ok) and prev:
                root.after(0, lambda: set_status("○ 后端已断开（点右上角重新启动）", "red"))
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()
    root.after(8000, _probe_online)


def _auto_connect():
    """开机自连：后端已在跑 → 直接亮绿灯；没跑 → 自动拉起来，不用手点「启动后端」。

    「先点启动后端、等 1-2 分钟、再点开始生成」这三步以前是必须手动走的；
    但绝大多数情况下用户打开程序就是要出片，多这一步只会让人以为程序没连上。
    """
    try:
        if check_online(1.0):
            _ONLINE[0] = True
            set_status("● 后端在线", "green")
            return
        if not (os.path.exists(COMFY_MAIN) and os.path.exists(EXTRA)):
            # 路径不对就别自动启（会弹模态框打断用户），留给手动点
            return
        log_q.put("[启动] 后端没在跑，正在自动启动（约 1-2 分钟，"
                  "这期间可以先编辑剧本/分镜）\n")
        start_backend()
    except Exception as e:
        log_q.put("[启动] 自动连后端失败：%s（可手动点右上角「启动后端」）\n" % e)


def _rows_alive():
    return [r for r in shot_rows
            if (r["scene"].get("1.0", "end") or "").strip()]


def _fill_refs_from_line():
    """⑥ 参考角色还空着的镜头：按台词说话人补；全剧只有一个角色就都给它。"""
    n = 0
    for r in _rows_alive():
        if (r["ref_var"].get() or "").strip():
            continue
        who, _ = _split_who(r["say_var"].get() or "")
        if who in CHAR_REFS:
            r["ref_var"].set(who)
            n += 1
    if n == 0 and len(CHAR_REFS) == 1:
        only = list(CHAR_REFS)[0]
        for r in _rows_alive():
            if not (r["ref_var"].get() or "").strip():
                r["ref_var"].set(only)
                n += 1
    _update_char_stat()
    if n:
        log_q.put("[锚定] 按台词补齐 %d 镜的参考角色\n" % n)
    return n


def _per_frame_now():
    """当前画质档位下的单帧耗时（按像素数 × 步数，从实测基准线性外推）。

    基准：1152×640 / 8 步 = 8.8 秒/帧（3060 实测 124 帧 18 分 10 秒）。
    """
    try:
        res, steps = SPEED_PRESETS.get(speed_var.get(), ((640, 1152), 8))
        if ASPECTS.get(aspect_var.get(), "portrait") == "landscape":
            res = (res[1], res[0])
        px = float(res[0]) * float(res[1])
        return PER_FRAME_SEC * (px / (1152.0 * 640.0)) * (float(steps) / 8.0)
    except Exception:
        return PER_FRAME_SEC


def _estimate_video(rows):
    """按台词预估成片规模：返回 (帧数, 分钟数, 实际镜数)。

    以前这里写的是"镜数 × 1.5 分钟"，实际一镜 124 帧在 3060 上要 18 分钟 ——
    差了十倍，用户看着"约 12 分钟"等了快一小时。现在按引擎那套公式真算：
    台词字量 → 每秒 4.3 字 → 帧数（含 A 方案的拆镜），再乘当前档位的实测单帧耗时。
    """
    tot_f = 0
    tot_shots = 0
    for r in rows:
        try:
            line = _split_who(r["say_var"].get() or "")[1]
        except Exception:
            line = r["say_var"].get() or ""
        segs = _split_say(line) if line else [""]
        for sg in segs:
            u = _speech_units(sg)
            f = 56 if u <= 0 else int(round((0.25 + u / NATIVE_CPS + 0.35) * 24))
            f = min(NATIVE_MAX_FRAMES, max(NATIVE_MIN_FRAMES, f))
            tot_f += f
            tot_shots += 1
    # 每镜的固定开销：关键帧要出 3 张候选（约 40 秒/张）＋构图/贴脸＋解码，
    # 平均下来差不多 2 分钟/镜 —— 不算进去会少报十几分钟。
    mins = tot_f * _per_frame_now() / 60.0 + tot_shots * 2.2
    return tot_f, int(round(mins)), tot_shots, tot_f / 24.0   # 帧数/渲染分钟/段数/成片秒数


def _assets_lost():
    """绑着名字但图片文件不在的资产：([角色], [场景])。"""
    ch = [nm for nm, fn in CHAR_REFS.items()
          if not os.path.exists(os.path.join(INPUTDIR, fn or ""))]
    sc = [nm for nm, fn in SCENE_REFS.items()
          if not os.path.exists(os.path.join(INPUTDIR, fn or ""))]
    return ch, sc


def _resume_probe():
    """上次是不是停在半路（断电 / 关窗口 / 崩溃）—— 还能不能接着渲。

    判定全用 make_drama 自己写的东西，不额外记状态：
      output/{prefix}_runmark.json   本轮起跑时间戳（续渲只认比它新的文件）
      output/{prefix}_done.json      每镜渲完的帧清单（逐镜追加）
    已完成镜数 < 当前镜数 → 这一轮没跑完。
    """
    import json as _j
    title = (title_var.get() or "").strip()
    live = _rows_alive()
    if not title or not live:
        return None
    try:
        prefix = M.slug(title)
    except Exception:
        return None
    mark = os.path.join(OUTDIR, "%s_runmark.json" % prefix)
    done_p = os.path.join(OUTDIR, "%s_done.json" % prefix)
    if not (os.path.exists(mark) and os.path.exists(done_p)):
        return None
    try:
        with open(done_p, encoding="utf-8") as f:
            d = _j.load(f)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    n_done = len([1 for v in d.values()
                  if isinstance(v, dict) and v.get("frames")])
    total = len(live)
    if n_done <= 0 or n_done >= total:
        return None
    try:
        when = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(done_p)))
    except Exception:
        when = "?"
    return prefix, n_done, total, when


def _resume_go():
    """接着渲：已渲完的镜跳过（make_drama 按 done.json + 本轮 runmark 判定）。"""
    _GEN_RESUME[0] = True
    generate()


# ==================== 导入后自动出片（无人值守） ====================
# 行业里「一键成片」的差别不在生成，而在**链路有没有串起来**：剧本 → 分镜 → 角色一致性
# → 逐镜生成 → 审片重抽 → 拼接。这里把前半段接到「导入」后面：
#   导入剧本 → 认角色 / 定妆照 / 场景照 → 自动导演排镜头语言 → 等资产齐 → 倒数 8 秒开跑
# 资产没齐就开跑＝换脸，所以倒计时只在「资产齐 + 参考角色齐」之后才开始。
_AUTO_GO = {"armed": False, "until": 0.0, "filled": False}


def _missing_refs():
    """还空着『⑥ 参考角色』的镜号（这几镜会换脸）。"""
    return [i + 1 for i, r in enumerate(shot_rows)
            if (r["scene"].get("1.0", "end") or "").strip()
            and not (r["ref_var"].get() or "").strip()]


def _needs_char_refs():
    """本片是否真有需要锁脸的人物镜。

    ⑥ 填「空镜·无人物」/ 留空的镜是纯环境、道具、氛围镜，本来就没脸可锁；
    只有绑了具体角色名的镜才需要角色定妆照。角色库为空时靠它区分两种情形：
    该拦（有人却没定妆照），还是该放行（整部都是空镜，压根不需要定妆照）。
    """
    for r in shot_rows:
        try:
            alive = (r["scene"].get("1.0", "end").strip()
                     or r["say_var"].get().strip())
        except Exception:
            alive = True
        if not alive:
            continue
        rv = (r["ref_var"].get() or "").strip()
        if rv and rv != NOBODY:
            return True
    return False


def _auto_go_enabled():
    try:
        return bool(AP.CFG.get("auto_go"))
    except Exception:
        return False


def _set_auto_go(on):
    try:
        c = dict(AP.CFG)
        c["auto_go"] = bool(on)
        AP.save_config(c)
        AP.CFG["auto_go"] = bool(on)
    except Exception:
        pass


def _auto_go_arm(why=""):
    """导入之后调用：把「自动出片」排进队，等资产齐了倒数开跑。"""
    if not _auto_go_enabled() or not _rows_alive():
        return False
    _AUTO_GO.update(armed=True, until=0.0, filled=False)
    try:
        log_q.put("[自动出片] 已排队%s：等定妆照/场景照补齐就自动开跑"
                  "（想反悔点提示条上的「✕ 取消」）。\n" % ("（%s）" % why if why else ""))
    except Exception:
        pass
    return True


def _auto_go_cancel(_e=None, quiet=False):
    was = _AUTO_GO["armed"]
    _AUTO_GO.update(armed=False, until=0.0)
    if was and not quiet:
        try:
            set_status("已取消自动出片 —— 分镜和镜头语言都留着", "warn")
            log_q.put("[自动出片] 已取消；想跑就点「▶ 开始生成」。\n")
        except Exception:
            pass


def _auto_go_ready():
    """资产和参考角色都齐了才能开跑 —— 缺一样都会换脸/串景。"""
    if not _ONLINE[0] or _BACKEND_QUEUE:
        return False
    try:
        ch, sc = _assets_lost()
        if ch or sc:
            return False
    except Exception:
        pass
    return not _missing_refs()


def _auto_go_tick():
    """每 1.5 秒看一眼：能不能开跑、倒计时到点了没。"""
    try:
        if _AUTO_GO["armed"] and not _GEN_RUNNING[0]:
            # 参考角色偶尔还会漏（剧本里没写说话人）→ 先按台词补一轮，补不上就等
            if not _AUTO_GO["filled"] and _missing_refs():
                try:
                    _fill_refs_from_line()
                except Exception:
                    pass
                _AUTO_GO["filled"] = True
            if _auto_go_ready():
                if not _AUTO_GO["until"]:
                    _AUTO_GO["until"] = time.time() + 8
                    try:
                        log_q.put("[自动出片] 资产齐了 → 8 秒后开跑"
                                  "（要停就点提示条上的「✕ 取消」）。\n")
                    except Exception:
                        pass
                elif time.time() >= _AUTO_GO["until"]:
                    _auto_go_cancel(quiet=True)
                    try:
                        log_q.put("[自动出片] 开跑。中途想停点「■ 停止」，"
                                  "下次接着渲会跳过已渲完的镜。\n")
                    except Exception:
                        pass
                    generate()
    except Exception:
        pass
    try:
        root.after(1500, _auto_go_tick)
    except Exception:
        pass


def _compute_next():
    """返回 (key, 标题, 按钮文字, 动作, 细节小字)。

    第一项是「现在该干什么」的大字，第五项是估算/原因这类小字（可为空）。
    以前全挤在一句里，"点开始生成"沉到最后，等于没提示。
    """
    live = _rows_alive()
    # 导入后自动出片：排队/倒计时期间占住这一行，否则会显示"开始出片"，
    # 可它其实已经排好队要自己跑了。等后端、等资产的时候也要看得见、能取消。
    if _AUTO_GO["armed"] and live:
        _left = int(_AUTO_GO["until"] - time.time()) if _AUTO_GO["until"] else 0
        if not _ONLINE[0]:
            _wait = "后端还在启动"
        elif _BACKEND_QUEUE:
            _wait = "定妆照 / 场景照还在生成"
        elif _missing_refs():
            _wait = "有镜还没选参考角色"
        else:
            _wait = "马上开跑"
        if _left > 0:
            return ("autogo", "%d 秒后自动开跑（%d 镜）" % (_left, len(live)),
                    "✕ 取消", _auto_go_cancel,
                    "无人值守：跑完直接出片；现在取消就只留分镜，不渲染")
        return ("autogo", "等就绪后自动开跑（%d 镜）" % len(live),
                "✕ 取消", _auto_go_cancel,
                "%s —— 补齐就自动开始；取消就只导入不渲染" % _wait)
    if not _ONLINE[0]:
        if backend_starting:
            return ("booting", "下一步：等后端启动（1–2 分钟）", "", None,
                    "变绿就能生成，这步不用点任何按钮")
        return ("offline", "下一步：把后端拉起来", "⚡ 启动后端", start_backend,
                "还没连上，正在自动重试；手动点一下更快")
    # 后端起来了，但新剧的定妆照/场景照还在自动补 → 这时候点生成会换脸
    if _BACKEND_QUEUE:
        return ("assets", "下一步：等资产补完", "", None,
                "正在生成 %s；日志出现「完成」就绪（现在就点生成会换脸）"
                % "、".join(lb for lb, _ in _BACKEND_QUEUE)[:40])
    if not live:
        return ("import", "下一步：导入剧本", "⇩ 导入剧本", open_import,
                "把分镜表整段粘进来，自动排好每一镜")
    # 全片都是空镜时角色库本来就该是空的，别再让人白做一张定妆照
    if not CHAR_REFS and _needs_char_refs():
        return ("chars", "下一步：生成定妆照锁脸", "🎭 角色定妆照", open_chars,
                "没锁脸就出片，每一镜都会换成不同的脸")
    _ch_lost, _sc_lost = _assets_lost()
    if _ch_lost or _sc_lost:
        return ("lost", "下一步：资产文件缺失（%s）" % "、".join((_ch_lost + _sc_lost)[:3]),
                "补生成资产", lambda: _regen_missing_assets(_ch_lost, _sc_lost),
                "点按钮按现有文案补生成，否则会换脸 / 串景")
    miss = _missing_refs()
    if not CHAR_REFS and not _needs_char_refs():
        miss = []      # 全空镜：留空的 ⑥ 由 generate() 统一按空镜处理
    if miss:
        s2 = "、".join(str(x) for x in miss[:5]) + ("…" if len(miss) > 5 else "")
        return ("refs", "下一步：第 %s 镜还没选『⑥ 参考角色』" % s2,
                "按台词补齐", _fill_refs_from_line,
                "这几镜会换脸；点按钮按台词自动补，或在大纲里逐镜选")
    # 半路断过（断电/关窗口/崩溃）→ 先提示接着渲，别让人以为白干了
    _rp = _resume_probe()
    if _rp:
        return ("resume", "上次渲到第 %d 镜（共 %d 镜）" % (_rp[1], _rp[2]),
                "▶ 接着渲", _resume_go,
                "%s 停下的 · 只补没渲完的镜；整套重来点工具栏「▶ 开始生成」" % _rp[3])
    if _DONE_AT[0] and time.time() - _DONE_AT[0] < 90:
        return ("done", "✅ 这一版出片了", "→ 打开成片", open_output,
                "点按钮直接看视频（已定位到文件）")
    _f, _m, _n, _sec = _estimate_video(live)
    # 成片时长也报出来：行业一集是 1–3 分钟，很多人写到一半才发现只有 40 秒
    _dur = "%d 分 %02d 秒" % (int(_sec) // 60, int(_sec) % 60) if _sec >= 60 \
        else "%.0f 秒" % _sec
    sub = "%d 镜已锚定 · %d 段 / 成片 %s / %d 帧 · 约 %d 分钟" % (
        len(live), _n, _dur, _f, _m)
    if _sec < 60:
        _need = int(round((60 - _sec) / max(1.0, _sec / max(1, _n))))
        sub += "（不足 1 分钟，再加约 %d 段才够一集）" % max(1, _need)
    if _m > 90:
        # 超过一个半小时就别让人干等了：直接告诉他"降一档能省一半"
        sub += "；嫌慢把「画质」调低一档，时间大概砍一半"
    return ("ready", "下一步：开始出片（点右边按钮）", "▶ 开始出片", generate, sub)


def _refresh_next_step(force=False):
    if _GEN_RUNNING[0]:
        return
    try:
        res = _compute_next()
    except Exception:
        return
    key, txt = res[0], res[1]
    btn, act = res[2], res[3]
    sub = res[4] if len(res) > 4 else ""
    _next_act[0] = act
    # 文字为空 = 这一步没有可点的事（比如等资产补完），按钮就收起来
    try:
        if btn:
            next_btn.configure(text=btn, command=_next_do)
            next_btn.grid()
        else:
            next_btn.grid_remove()
    except Exception:
        pass
    try:
        if force or next_lbl.cget("text") != txt:
            next_lbl.configure(text=txt,
                               foreground=TH.OK if key == "done" else TH.ACCENT)
        if force or next_sub.cget("text") != sub:
            next_sub.configure(text=sub)
            if sub:
                next_sub.grid()
            else:
                next_sub.grid_remove()
    except Exception:
        pass
    # 换过主题后主色会变：这条竖条是普通 Frame，restyle 管不到它，顺手对一下色
    try:
        if _next_edge.cget("background") != TH.ACCENT:
            _next_edge.configure(background=TH.ACCENT)
    except Exception:
        pass
    try:
        if _next_sep.cget("background") != TH.LINE:
            _next_sep.configure(background=TH.LINE)
    except Exception:
        pass


def _next_do():
    fn = _next_act[0]
    if not fn:
        return
    try:
        fn()
    except Exception as e:
        log_q.put("[下一步] 动作失败：%s\n" % e)
    root.after(600, lambda: _refresh_next_step(force=True))


def _tick_next():
    _refresh_next_step()
    root.after(1200, _tick_next)


root.after(700, _tick_next)
root.after(1800, _auto_go_tick)      # 自动出片的排队/倒计时
_probe_online()


# 进度条（真实进度：关键帧 → 视频帧 → 配音 → 拼接）
prog_frame = ttk.Frame(_ue_tab_prog)
prog_frame.pack(fill="x", padx=6, pady=2)   # 常驻行：压薄，把高度让给下面的资产/日志
# 状态文字独占一行 + 已用时间靠右 + 进度条在下面整行铺满：
# 原来 label 固定 width=30，文字一长「已用时间」就被截成「已用」，窗口一窄更看不到。
prog_lbl = ttk.Label(prog_frame, text="待机", font=FONT_B, anchor="w")
prog_lbl.grid(row=0, column=0, sticky="w")
# 采样步数：后台从 ComfyUI 的 /internal/logs 里读 tqdm 的 "6/8 [13:56<04:38]"
step_lbl = ttk.Label(prog_frame, text="", font=TH.FONT_S, anchor="e")
step_lbl.grid(row=0, column=1, sticky="e", padx=(8, 6))
prog_time = ttk.Label(prog_frame, text="", font=TH.FONT_S, anchor="e")
prog_time.grid(row=0, column=2, sticky="e", padx=(0, 6))
prog_pct = ttk.Label(prog_frame, text="0%", font=FONT_B, width=6, anchor="e")
prog_pct.grid(row=0, column=3, sticky="e")
prog_var = tk.DoubleVar(value=0.0)
prog_bar = ttk.Progressbar(prog_frame, variable=prog_var, maximum=100.0)
prog_bar.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(3, 0))
prog_frame.columnconfigure(0, weight=1)

# ---- 采样步数：后台读 ComfyUI 日志里的 tqdm（6/8 [13:56<04:38]），界面上显示出来 ----
# 为什么要读日志：ComfyUI 的步进进度只走 websocket，HTTP 上只有 /internal/logs 能拿到；
# 这里每 2 秒取一次日志末尾，够用了，也不用引入 websocket 依赖。
_STEP = {"cur": 0, "total": 0, "eta": "", "at": 0.0,
         "jid": "", "t0": 0.0, "avg": 0.0, "want": 0}


def _step_watch():
    """每 2 秒从 ComfyUI 日志里读一次采样进度。

    两个坑（都是看着数字乱跳才发现的）：
    ① 日志里躺着**上一轮**的 tqdm（关键帧 8 步、上一镜 8 步…），直接取最后一条
       会把旧进度当成现在 —— 所以用 queue_running 的任务 id 判断换任务，换了就清零，
       并且只认"总步数 == 本轮该有的步数"的那些行（关键帧 8 步不会污染视频 4 步）；
    ② 任务刚提交、还没开始采样时，日志里最后一条一定是旧的 —— 用时间戳兜底，
       超过 15 分钟的记录不予采信。
    """
    import datetime as _dt
    import json as _json          # 本文件没有全局 json（只有局部 import）
    import re as _re
    import urllib.request as _u
    pat = _re.compile(r"(\d+)/(\d+) \[([0-9:]+)<([0-9:]+)")
    stamp = _re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
    while True:
        time.sleep(2)
        try:
            if not _GEN_RUNNING[0]:
                _STEP.update(cur=0, total=0, eta="", at=0.0, jid="", t0=0.0)
                continue
            q = _json.loads(_u.urlopen(COMFY_URL + "/queue", timeout=3).read())
            run = q.get("queue_running") or []
            if not run:
                _STEP.update(cur=0, total=0, eta="", at=0.0, jid="", t0=0.0)
                continue
            jid = str(run[0][1])
            if _STEP["jid"] != jid:          # 换任务了：一切从头算
                _STEP.update(cur=0, total=0, eta="", at=0.0, jid=jid,
                             t0=time.time(), avg=0.0)
            txt = _u.urlopen(COMFY_URL + "/internal/logs",
                             timeout=3).read().decode("utf-8", "ignore")
            want = int(_STEP.get("want") or 0)
            pick = None
            for m in pat.finditer(txt):
                cur, total = int(m.group(1)), int(m.group(2))
                if want and total != want:
                    continue                 # 关键帧的 8 步不要
                ts = None
                for t in stamp.finditer(txt, 0, m.start()):
                    ts = t.group(1)
                if ts:
                    try:
                        age = (time.time() - _dt.datetime.fromisoformat(ts)
                               .timestamp())
                        if age > 900:
                            continue         # 太老的记录，不是这一轮
                    except Exception:
                        pass
                pick = (cur, total, m.group(3), m.group(4))
            if pick:
                cur, total, _spent, eta = pick
                now = time.time()
                if cur > 0 and _STEP["t0"]:
                    per = (now - _STEP["t0"]) / float(cur)
                    _STEP["avg"] = per if not _STEP["avg"] else \
                        (_STEP["avg"] * 0.6 + per * 0.4)
                _STEP.update(cur=cur, total=total, eta=eta, at=now)
        except Exception as _e:
            _STEP["err"] = _STEP.get("err", 0) + 1
            if _STEP["err"] == 1:
                print("[step] 读采样进度失败：%r" % (_e,), flush=True)


def _story_live_text():
    """当前进度里那句「第 i/n 镜 …」，给 ETA 用（没有就返回空串）。"""
    try:
        import progress as _PG
        st = _PG.last() if _GEN_RUNNING[0] else {}
        return str(st.get("text") or "")
    except Exception:
        return ""


def _hms(sec):
    sec = max(0, int(sec))
    h, m, s2 = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s2)) if h else ("%02d:%02d" % (m, s2))


def _step_tick():
    """每秒重画一次"步数 + 时间"那一格 —— 只有一个写入者，不会闪。

    剩余时间只报**本镜**，不报全片：
    全片进度是按镜跳的（30%→48%→…），一秒不动地停着，'剩余'就会一直往上涨，
    看着像倒计时坏了。本镜剩余用"已测的平均每步耗时 × 剩余步数"，单调往下走。
    """
    try:
        busy = bool(_GEN_RUNNING[0])
        parts = []
        if busy and _STEP["total"] and (time.time() - _STEP["at"]) < 20:
            cur, total = _STEP["cur"], _STEP["total"]
            t = "采样 %d/%d 步" % (cur, total)
            if cur < total and _STEP["avg"]:
                t += " · 本镜剩 %s" % _hms(_STEP["avg"] * (total - cur))
            elif cur >= total:
                t += " · 本镜采样完成，合成中"
            parts.append(t)
        if busy and _GEN_T0[0]:
            parts.append("已用 %s" % _hms(time.time() - _GEN_T0[0]))
        # 全片预计剩余：按"每步实测耗时"外推（本镜剩余步数 + 后面几镜各一整套步数）。
        # 用户吐槽「22 分钟了，看不到他在干什么」——至少得让它说出还要多久。
        try:
            if busy and _STEP["avg"] and _STEP["total"] and \
                    (time.time() - _STEP["at"]) < 25:
                _cur, _tot = _STEP["cur"], _STEP["total"]
                import re as _r4
                _m4 = _r4.search(r"第\s*(\d+)\s*/\s*(\d+)",
                                 _story_live_text())
                _i, _n = (int(_m4.group(1)), int(_m4.group(2))) if _m4 else (1, 1)
                _eta = _STEP["avg"] * (_tot - _cur) + \
                    _STEP["avg"] * _tot * max(0, _n - _i)
                if _eta > 0:
                    parts.append("预计还要 %s" % _hms(_eta))
        except Exception:
            pass
        txt = "　　".join(parts)
        if txt != step_lbl.cget("text"):
            step_lbl.configure(text=txt)
    except Exception:
        pass
    root.after(1000, _step_tick)


threading.Thread(target=_step_watch, daemon=True).start()
root.after(1200, _step_tick)

# 日志
log_box = ttk.LabelFrame(_ue_tab_log, text=" 运行日志 ", padding=4)
log_box.pack(fill="both", expand=True)
_logbar = ttk.Frame(log_box)
_logbar.pack(fill="x", pady=(0, 4))
ttk.Label(_logbar, text="出错的行会标红；日志同时写到 output/studio.log",
          style="Hint.TLabel").pack(side="left")
TH.RoundButton(_logbar, text="▤ 打开日志文件",
               command=lambda: subprocess.Popen(["explorer", LOG_FILE]) if os.path.exists(LOG_FILE)
               else messagebox.showinfo("还没有日志", "先跑一次生成吧。"),
               kind="quiet", height=28).pack(side="right")
TH.RoundButton(_logbar, text="⧉ 复制日志",
               command=lambda: (root.clipboard_clear(),
                                root.clipboard_append(log_txt.get("1.0", "end").strip()),
                                set_status("日志已复制到剪贴板", "ok")),
               kind="quiet", height=28).pack(side="right", padx=(0, 6))
log_txt = scrolledtext.ScrolledText(log_box, height=3, font=("Microsoft YaHei", 9),
                                     state="normal", wrap="word")
log_txt.pack(fill="both", expand=True)
TH.paint_text(log_txt, pad=(9, 5))

# ============================================================================
# 停靠布局接管（原来的手算宽度体系全部下线）
# ----------------------------------------------------------------------------
# 旧版是「左侧参数栏 + 工坊列 + ComfyUI 面板」三栏并排，靠 _layout_fit 按窗口宽度
# 一点点分配，还配了 3 根自绘分割条和一套「收起左侧」逻辑。实测 1280 宽时
# 主面板被压到 532、画布面板只剩 320 —— 用户拖一下分割条，下一次 resize 又顶回去。
#
# UE5 布局下这些全部由 PanedWindow 负责：三栏 + 底部，用户拖到哪就是哪。
# 下面这些函数名保留为空实现，是因为程序别处（WebView2 回调、启动流程、
# 主题切换）还在调用它们 —— 直接删会连锁报 NameError。
# ============================================================================
_LEFT_SHRINK = {"on": False, "items": [], "prev": None, "width": 300}


def _ue_refit_webs():
    """两块 WebView2 重新贴合各自的容器（PanedWindow 拖动后要手动叫一次）。"""
    for h in (_web.get("wv"), _canvas.get("wv")):
        if h is not None:
            try:
                h.fit()
            except Exception:
                pass


def _left_bar_tip(on=False):
    return


def _left_collect_pack():
    return []


def _left_fit_window(shrink=False):
    return


def _panel_repack():
    _ue_refit_webs()


def _canvas_half_sync(*_a):
    return


def _canvas_fit_to(*_a, **kw):
    return


def _left_toggle():
    """视图 → 折叠/展开左栏（等于 UE5 里关掉 Outliner 面板，拖回来就恢复）。"""
    try:
        pos = _ue_sash_get(_ue_row, 0)
    except Exception:
        return
    if pos > 8:
        _LEFT_SHRINK["width"] = pos
        _ue_sash_set(_ue_row, 0, 0)
        set_status("左栏已折叠（视图 → 大纲面板 可展开）", "ok")
    else:
        _ue_sash_set(_ue_row, 0, _LEFT_SHRINK.get("width") or 300)
    _ue_refit_webs()


# ---------------- 三栏预算 / 视口宽度：UE5 下不再计算，全部交给 PanedWindow ------
_WEB_W = 900                 # 兼容旧引用（面板实际宽度由 PanedWindow 决定）
_WEB_W_PREF = 900
_WEB_W_DEF = 900
_WEB_W_MIN, _WEB_W_MAX = 320, 1400
_LEFT_FLOOR = 680
_LAYOUT_LEFT_MIN = 640
_LAYOUT_MANJU_MIN = 400
_LAYOUT_WEB_MIN = 420
_layout_job = {"id": None, "last": None}


def _layout_bar_w():
    return 0


def _window_need(web_w=None):
    return 0


def _layout_web_cap(expect_w=None):
    return _WEB_W_MAX


def _layout_fit(tag=""):
    """旧版每次窗口变化都重算三栏宽度。现在只负责让两块网页重新贴合。"""
    _ue_refit_webs()


def _layout_on_configure(ev=None):
    """窗口变化时 PanedWindow 自己会分好宽度，这里只需补一次网页贴边。"""
    try:
        if ev is not None and ev.widget is not root:
            return
    except Exception:
        pass
    if _layout_job["id"]:
        try:
            root.after_cancel(_layout_job["id"])
        except Exception:
            pass
    def _dbg():
        _ue_refit_webs()
        if os.environ.get("MANJU_LAYOUT_DEBUG") == "1":
            try:
                _crash("窗口 宽高=%dx%d 三栏=%d/%d/%d 底=%d"
                       % (root.winfo_width(), root.winfo_height(),
                          _ue_left.winfo_width(), _ue_center.winfo_width(),
                          _ue_right.winfo_width(), _ue_bottom.winfo_height()))
            except Exception:
                pass
    try:
        _layout_job["id"] = root.after(160, _dbg)
    except Exception:
        _layout_job["id"] = None


root.bind("<Configure>", _layout_on_configure, add="+")

# 窗口大小/位置一改就记下来（900ms 防抖）。用户要求「我随意缩小窗口，下次启动
# 还是缩小后的样子」——只在关闭时存的话，进程被强杀/崩溃就丢，所以这里边改边存。
_WSZ = {"job": None, "last": None}


def _win_size_changed(ev=None):
    try:
        if ev is not None and ev.widget is not root:
            return
        size = (root.winfo_width(), root.winfo_height(),
                root.winfo_x(), root.winfo_y())
        if size == _WSZ["last"]:
            return
        _WSZ["last"] = size
    except Exception:
        return
    try:
        if _WSZ["job"]:
            root.after_cancel(_WSZ["job"])
    except Exception:
        pass
    try:
        _WSZ["job"] = root.after(900, lambda: _ue_save_layout(only_win=True))
    except Exception:
        _WSZ["job"] = None


root.bind("<Configure>", _win_size_changed, add="+")

# ===== AI漫剧工坊：收进右栏第二个页签「AI 工坊」 =====
# 以前它是一根常驻列，和 ComfyUI 面板、主面板三栏抢 1920 的宽度。现在改成右栏页签：
# 要看工坊就点右栏的「AI 工坊」，不需要时视口能占满整块。
_MANJU_W = 460
manju_col = tk.Frame(_ue_manju_wrap, background=TH.PANEL)
manju_col.pack(fill="both", expand=True)
manju_col_visible = {"on": False}
_manju_web_prev_w = {"w": None}


def _ensure_manju_col():
    """惰性创建工坊内容（第一次展开才建 Notebook，省开机开销）。"""
    if getattr(manju_col, "_built", False):
        return
    manju_col._built = True
    try:
        import manju_studio_ui as M
        if getattr(M, "_IMPORT_ERR", None) is not None:
            ttk.Label(manju_col, text="manju_skills 未加载：%s" % M._IMPORT_ERR,
                      foreground=TH.ERR, font=TH.FONT_S).pack(padx=16, pady=16)
        else:
            M.build_manju_panel(manju_col, root)
    except Exception as e:
        ttk.Label(manju_col, text="AI漫剧工坊加载失败：%s" % e,
                  foreground=TH.ERR, font=TH.FONT_S).pack(padx=16, pady=16)


def _manju_col_show():
    _ensure_manju_col()
    manju_col_visible["on"] = True
    _manju_update_btn()


def _manju_col_hide():
    manju_col_visible["on"] = False
    _manju_update_btn()
    _rt_show("detail")


def _manju_col_toggle():
    """工具栏「🎬 工坊」：切到右栏的工坊页 / 切回细节页。"""
    _rt_show("detail" if _rt_state["tab"] == "manju" else "manju")


def _manju_update_btn():
    try:
        _manju_top_btn.configure(
            text=("🎬 收起工坊" if manju_col_visible["on"] else "🎬 工坊"))
    except Exception:
        pass


# ==================== 视口（中栏）：无限画布 + ComfyUI 双屏 ====================
web_panel = ttk.Frame(_ue_center)
web_panel.pack(fill="both", expand=True)

web_bar = tk.Frame(web_panel, background=TH.HL, height=26)
web_bar.pack(fill="x", padx=0, pady=(0, 3))
web_bar.pack_propagate(False)
# 面板内部左右分屏：左边无限画布(:3000)，右边原始 ComfyUI(:8188)。
# 原始 ComfyUI 面板逻辑（web_host / _web）一行不动，画布作为它的左邻列嵌入。
_CANVAS_INNER_W = 780
_CANVAS_INNER_W_DEF = 780
_CANVAS_INNER_W_MIN, _CANVAS_INNER_W_MAX = 320, 1600

# 视口内部：左「无限画布」/ 右「ComfyUI」两个可分屏，用 PanedWindow 管 ——
# 以前是 canvas_host 固定 780px + 自绘分割条，视口一窄（<800）就把 ComfyUI 挤成一条缝。
web_split = tk.PanedWindow(web_panel, orient="horizontal", bd=0, sashwidth=6,
                           sashrelief="flat", background=TH.LINE,
                           borderwidth=0, opaqueresize=True)
web_split.pack(fill="both", expand=True)
# 收起状态下窗口一变宽（尤其是最大化铺满屏），立刻把内部分界重算成一半
web_split.bind("<Configure>", _canvas_half_sync, add="+")

canvas_host = tk.Frame(web_split, background="#1e1e1e", width=_CANVAS_INNER_W)
canvas_host.pack_propagate(False)
web_split.add(canvas_host, minsize=220, stretch="always")


def _canvas_fit_to(web_w=None):
    """视口变宽/变窄时让「画布 / ComfyUI」保持合理比例（ComfyUI 那侧至少留 260px）。"""
    try:
        w = web_split.winfo_width()
        if w <= 300:
            return
        cur = _ue_sash_get(web_split, 0)
        cc = max(_CANVAS_INNER_W_MIN, min(cur, w - 260))
        if abs(cc - cur) > 2:
            _ue_sash_set(web_split, 0, cc)
    except Exception:
        pass

canvas_title = ttk.Frame(canvas_host, height=22)
canvas_title.pack(fill="x")
canvas_title_lbl = tk.Label(canvas_title, text="🗺 无限画布 · :3000",
                            background="#1e1e1e", foreground="#9aa0a6",
                            font=TH.FONT_XS)
canvas_title_lbl.pack(side="left", padx=8, pady=1)
_canvas_open_btn = TH.RoundButton(canvas_title, text="⟩ 收起",
                                  command=lambda: _canvas_toggle(),
                                  kind="quiet", height=20)
_canvas_open_btn.pack(side="right", padx=(0, 6))
# 拖歪了想回到初始分界：一键复位（双击内部分割条也是同一件事，但手势太隐蔽）
TH.RoundButton(canvas_title, text="⇤ 复位",
               command=lambda: _canvas_apply_w(_CANVAS_INNER_W_DEF),
               kind="quiet", height=20).pack(side="right", padx=(0, 4))
# 「⇢ 推给画布」原本只挂在 web_bar（ComfyUI 网页工具栏）上。默认视口改成
# 「无限画布」后那块面板被收起，按钮就看不见了 —— 在画布自己的标题栏也放一个，
# 单文档 / 分屏 / 只看 ComfyUI 三种模式都能点到。
TH.RoundButton(canvas_title, text="⇢ 推分镜",
               command=lambda: _canvas_push_params(),
               kind="quiet", height=20).pack(side="right", padx=(0, 4))
# 「▶ 出图 / 出视频」原来也只挂在 web_bar，默认视口是无限画布时那块面板收起，
# 按钮根本看不见（和上面「推分镜」同一个回归）。画布标题栏常驻一份。
# 文字用短的：画布标题栏只有 400 多像素，五个按钮写全称会被截成「▶ 出视…」。
TH.RoundButton(canvas_title, text="出图",
               command=lambda: _gw_generate("image"),
               kind="quiet", height=20).pack(side="right", padx=(0, 4))
TH.RoundButton(canvas_title, text="视频",
               command=lambda: _gw_generate("video"),
               kind="quiet", height=20).pack(side="right", padx=(0, 4))
# 注意：这一层是直接承载 WebView2（画图工作台/图生视频表单页）的容器。
# 原来底色是 #1e1e1e（深色），而表单页是浅色页面——窗口从任务栏还原时，
# 浏览器子窗口还没贴上来那一两帧，露出的就是这层深色，看起来就是"整块闪黑"。
# 改成接近页面的白色后，同一瞬间露白，肉眼几乎看不出，闪黑消失。
canvas_web = tk.Frame(canvas_host, background="#ffffff")
canvas_web.pack(fill="both", expand=True)

# 分屏分隔条由 web_split(PanedWindow) 自带，不再自绘 inner_sash
inner_sash = None

# 底色必须跟**网页本身**接近：WebView2 是独立的子窗口，窗口还原/拖动时它会有
# 一两帧没贴上来，这时露出的就是这个 Frame 的底色。原来这里是 #1e1e1e，
# 而 ComfyUI 页面是浅色的 —— 于是还原那一下就是"整块黑闪一下"。
# 改成白色后，同一瞬间露白，和页面几乎分不出，视觉上就不闪了。
web_host = tk.Frame(web_split, background="#ffffff")
web_split.add(web_host, minsize=260, stretch="always")
# ================== 视口文档标签（照 UE5 蓝图那种文档区） ==================
# UE5 里蓝图是「主窗口里的一个文档标签」：一排标签，点谁谁占满整块画布区。
# 这里把中间那栏做成同一个意思：
#   点「🗺 无限画布」→ 画布占满；点「🌐 ComfyUI」→ 网页占满；
#   点右边「▥ 分屏」→ 两块并排一半一半（默认）。
# 手动拖分隔条也会自动把标签状态切对应（不然标签会一直亮着"分屏"，看着像坏了）。
_VP = {"mode": "canvas", "bar": None, "tabs": {}, "ratio": 0.5, "busy": False}


def _vp_tab(text, key, hint=""):
    """一个文档标签：上面 2px 强调条 + 文字，选中时亮条变色、底变浅一档。"""
    holder = tk.Frame(_VP["bar"], background=TH.PANEL, cursor="hand2")
    holder.pack(side="left", padx=(0, 2))
    line = tk.Frame(holder, height=2, background=TH.PANEL)
    line.pack(fill="x")
    lb = tk.Label(holder, text=text, background=TH.PANEL,
                  foreground=TH.TEXT2, font=(TH.FONT[0], 9), padx=12, pady=3)
    lb.pack(fill="x")
    for w in (holder, lb):
        w.bind("<Button-1>", lambda e, k=key: _vp_apply(k))
    if hint:
        holder.bind("<Enter>", lambda e: set_status(hint, "ok"))
    _VP["tabs"][key] = (holder, lb, line)
    return holder


def _vp_paint():
    """按当前模式刷新标签外观。"""
    for k, (h, lb, line) in _VP["tabs"].items():
        on = (_VP["mode"] == k)
        seen = on or (_VP["mode"] == "split" and k in ("canvas", "web"))
        try:
            h.configure(background=(TH.HL if on else TH.PANEL))
            lb.configure(background=(TH.HL if on else TH.PANEL),
                         foreground=(TH.TEXT if seen else TH.TEXT3))
            line.configure(background=(TH.ACCENT if on else TH.PANEL))
        except Exception:
            pass


def _vp_pane_has(host):
    """这个控件当前是不是 web_split 里的一个 pane。"""
    try:
        return any(str(p) == str(host) for p in web_split.panes())
    except Exception:
        return False


def _vp_pane_show(host, visible, minsize=None):
    """把视口的某一半加回 / 移出分屏。

    独占模式（只显示无限画布 / 只显示 ComfyUI）用"摘掉另一半"实现：
    Tk 的 PanedWindow 在可用宽度小于两块请求宽度时会自己重排 pane，
    把 sash 推到尽头的效果保持不住（实测点完「无限画布」分隔条立刻
    从 613 弹回 427），于是看着还是两列。摘掉就不用跟 Tk 抢 sash 了。
    """
    has = _vp_pane_has(host)
    if visible:
        if not has:
            try:
                if _vp_pane_has(web_host):
                    web_split.add(host, before=web_host,
                                  minsize=(minsize or 1), stretch="always")
                else:
                    web_split.add(host, minsize=(minsize or 1),
                                  stretch="always")
            except Exception as _e:
                _crash("视口 pane 加回失败: %r" % (_e,))
        elif minsize is not None:
            try:
                web_split.paneconfigure(host, minsize=minsize)
            except Exception:
                pass
    elif has:
        try:
            web_split.forget(host)
        except Exception as _e:
            _crash("视口 pane 移除失败: %r" % (_e,))



def _vp_apply(mode, save=True):
    """切视口文档：story（剧情预览）/ canvas（无限画布）/ web（ComfyUI）/ split（并排）。"""
    if mode not in ("story", "canvas", "web", "split"):
        mode = "web"
    _VP["mode"] = mode
    # 剧情预览是另一层：它铺满整个视口，和「画布 / ComfyUI 分屏」互斥
    try:
        if mode == "story":
            web_split.pack_forget()
            _story_host.pack(fill="both", expand=True)
            _story_refresh()
        elif not _story_host.winfo_ismapped():
            pass
        else:
            _story_host.pack_forget()
            web_split.pack(fill="both", expand=True)
    except Exception:
        pass
    try:
        if mode != "story" and not web_split.winfo_ismapped() and _story_host.winfo_ismapped():
            _story_host.pack_forget()
            web_split.pack(fill="both", expand=True)
    except Exception:
        pass
    try:
        # 独占模式（只看画布 / 只看 ComfyUI）不再靠"把分隔条推到边"：
        # Tk 的 PanedWindow 在可用宽度小于两块请求宽度时会自己重排 pane，
        # 推满的分隔条 30ms 内就被拽回来（实测 613 → 427），看着就像点了没反应。
        # 这里改成把另一半直接从分屏里摘掉 —— 独占就一定是独占。
        if mode == "canvas":
            _vp_pane_show(canvas_host, True, 220)
            _vp_pane_show(web_host, False)
        elif mode == "web":
            _vp_pane_show(canvas_host, False)
            _vp_pane_show(web_host, True, 260)
        else:
            _vp_pane_show(canvas_host, True, 220)
            _vp_pane_show(web_host, True, 260)
        if mode == "split":
            w = web_split.winfo_width()
            if w > 60:
                r = min(0.8, max(0.2, float(_VP.get("ratio") or 0.5)))
                _ue_sash_set(web_split, 0,
                             max(220, min(int((w - 6) * r), w - 240)))
    except Exception:
        pass
    # 两块网页都保持「已启用」：标签只是改可见宽度，不销毁 WebView2，
    # 这样切回来是秒开（重建内核要 1-2 秒，还会白闪一下）。
    try:
        _web["open"] = (mode != "canvas")
        _canvas["open"] = (mode != "web")
        _canvas_open_btn.configure(
            text=("⟩ 收起" if _canvas["open"] else "⟨ 展开画布"))
    except Exception:
        pass
    _vp_paint()
    if save:
        try:
            _web_write_state(vp_mode=mode)
        except Exception:
            pass
    try:
        if mode in ("web", "split"):
            _web_ensure()
        if mode in ("canvas", "split"):
            _canvas_ensure()
    except Exception:
        pass
    root.after(70, _ue_refit_webs)
    try:
        _web_update_bar()
    except Exception:
        pass


def _vp_on_split(_e=None):
    """手动拖完分隔条：按最终宽度反推标签状态（0=只有网页，满=只有画布，中间=分屏）。"""
    if _VP.get("mode") == "story":
        return
    try:
        if len(web_split.panes()) < 2:      # 独占模式没有分隔条可拖
            return
        w = web_split.winfo_width()
        if w < 60:
            return
        pos = _ue_sash_get(web_split, 0)
        if pos <= 40:
            mode = "web"
        elif pos >= w - 40:
            mode = "canvas"
        else:
            mode = "split"
        if mode == "split":
            try:
                _VP["ratio"] = min(0.8, max(0.2, pos / float(w)))
            except Exception:
                pass
        if mode != _VP["mode"]:
            _VP["mode"] = mode
            _web["open"] = (mode != "canvas")
            _canvas["open"] = (mode != "web")
            _vp_paint()
            try:
                _web_write_state(vp_mode=mode)
            except Exception:
                pass
            _ue_refit_webs()
    except Exception:
        pass


web_split.bind("<ButtonRelease-1>", _vp_on_split, add="+")
# 视口内分屏比例也要记住（下次开机还是这个宽窄）
web_split.bind("<ButtonRelease-1>",
               lambda e: root.after(140, _ue_save_layout), add="+")


def _vp_keep_ratio(_e=None):
    """视口尺寸变化时按当前模式重新摆分隔条。

    分隔条记的是**绝对像素**：窗口一变宽，原本藏在边上的那块会重新露出来；
    分屏时两块也不再是一半一半（实测开局就成了 73/27）。这里统一按比例修正。
    """
    if _VP.get("busy"):
        return
    try:
        if len(web_split.panes()) < 2:      # 独占模式（只有一半）没有分隔条
            return
        w = web_split.winfo_width()
        if w < 200:
            return
        mode = _VP.get("mode") or "split"
        if mode == "canvas":
            want = w
        elif mode == "web":
            want = 0
        else:
            r = min(0.8, max(0.2, float(_VP.get("ratio") or 0.5)))
            want = max(220, min(int((w - 6) * r), w - 240))
        pos = _ue_sash_get(web_split, 0)
        if abs(pos - want) > 6:
            _VP["busy"] = True
            _ue_sash_set(web_split, 0, want)
            if os.environ.get("MANJU_LAYOUT_DEBUG") == "1":
                _crash("视口: %s 宽=%d 原=%d 目标=%d 画布宽=%d"
                       % (mode, w, pos, want, canvas_host.winfo_width()))
            root.after(140, lambda: _VP.update(busy=False))
    except Exception:
        pass


web_split.bind("<Configure>", _vp_keep_ratio, add="+")


def _vp_sash_init():
    """视口第一次露出来时按记住的模式摆好。

    没存过模式时：看「开机自动打开」那个开关 —— 用户关掉过就别自作主张把
    ComfyUI 拉起来（这是之前被反馈过的老毛病），退到「只显示无限画布」。
    """
    try:
        st = _web_read_state()
        mode = st.get("vp_mode")
    except Exception:
        st, mode = {}, None
    # 开机默认落在「无限画布」这一页（ComfyUI-Local-Infinite-Canvas 前端，:3000）。
    # 想看原版 ComfyUI 点「🌐 ComfyUI」标签；用户手动切过页会以 .ui_state.json 为准。
    if mode not in ("canvas", "web", "split"):
        mode = "canvas"
    _vp_apply(mode, save=False)


# 标签栏本体：放在视口最上面（UE5 的文档标签就长在工具栏下面那一行）
_VP["bar"] = tk.Frame(web_panel, background=TH.PANEL)
_VP["bar"].pack(fill="x", side="top", before=web_bar)
tk.Frame(_VP["bar"], background=TH.LINE, height=1).pack(side="bottom", fill="x")

_vp_max = tk.Label(_VP["bar"], text="⛶ 视口最大化", background=TH.PANEL,
                   foreground=TH.TEXT3, font=(TH.FONT[0], 8), padx=10, pady=4,
                   cursor="hand2")
_vp_max.pack(side="right", padx=(0, 8))
_vp_max.bind("<Button-1>", lambda e: _web_fullscreen())
_vp_max.bind("<Enter>", lambda e: _vp_max.configure(foreground=TH.TEXT))
_vp_max.bind("<Leave>", lambda e: _vp_max.configure(foreground=TH.TEXT3))

_vp_tab("🎬  剧情预览", "story", "把当前剧情画成分镜缩略图（配好剧本自动切到这页）")
_vp_tab("🗺  无限画布", "canvas", "点这里：无限画布放大到占满整个视口")
_vp_tab("🌐  ComfyUI", "web", "点这里：ComfyUI 视口放大到占满整个视口")
_vp_tab("▥  分屏", "split", "两块并排各一半")

# ==================== 剧情预览（分镜可视化文档） ====================
# 配置好剧本之后，中间这块直接变成一排分镜缩略图：第几镜 / 景别·场景·时长 / 台词，
# 出了关键帧就换成真图。相当于 UE5 里的 Sequencer 那一页 —— 从「节点图」切到「成片长什么样」。
_story_host = tk.Frame(web_panel, background=TH.BG_DEEP)
_sbar = tk.Frame(_story_host, background=TH.HL, height=26)
_sbar.pack(fill="x")
_sbar.pack_propagate(False)
_story_title = tk.Label(_sbar, text="剧情预览", background=TH.HL,
                        foreground=TH.TEXT2, font=(TH.FONT[0], 9))
_story_title.pack(side="left", padx=(9, 0), pady=3)
_story_hint = tk.Label(_sbar, text="", background=TH.HL, foreground=TH.TEXT3,
                       font=(TH.FONT[0], 8))
_story_hint.pack(side="left", padx=(10, 0), pady=3, fill="x", expand=True)

# ============================================================================
# 剧情预览：一张 ComfyUI 风格的节点图
# ----------------------------------------------------------------------------
# 剧本 → 角色/场景 → 各镜（缩略图直接画在节点里）→ 成片。
# 画布：点阵网格 + 拖动平移 + 滚轮缩放 + 右下角缩放条；
# 节点：圆角、按类型着色的标题条、内部 widget 行、左右圆形接口；
# 连线：按关系着色（主链琥珀 / 角色青 / 场景绿），带一条深色底做"发光"。
# 也能自己连：从右边圆点拖到别的节点左边圆点（见 _story_connect）。
# ============================================================================
_story_cv = tk.Canvas(_story_host, background=TH.BG_DEEP, highlightthickness=0,
                      bd=0)
_story_cv.pack(fill="both", expand=True)
_STORY = {"z": 1.0, "ox": 40, "oy": 40, "drag": None, "auto": True,
          "nodes": {}, "off": {}, "link": None}

# 节点类型配色（照 ComfyUI 那种一眼分得清的做法）
_NC = {
    "script": "#5B4B8A",     # 剧本：紫
    "char": "#2E6F8E",       # 角色：青
    "scene": "#2F7D5B",      # 场景：绿
    "shot": "#8A5A2B",       # 分镜：橙棕
    "out": "#7A3B52",        # 成片：酒红
}
_WC = {"mine": "#FFC24B", "char": "#4FC3F7", "scene": "#6FD08C"}
_GRID = "#26262c"          # 深色主题下的网格线


_NC_CACHE = {}


def _nc_map():
    """节点图里那套「面板/行/描边/文字」中性色。

    以前全是硬编码炭灰（#1e1e23 之类），浅色主题下就成了一块块深色补丁。
    这里按主题深浅给两套：浅色主题直接取主题的 FIELD/PANEL/LINE/TEXT。
    """
    key = (TH.current_key(), bool(getattr(TH, "IS_LIGHT", False)))
    hit = _NC_CACHE.get(key)
    if hit is None:
        if key[1]:
            hit = {"shadow": TH.LINE, "body": TH.FIELD, "edge": TH.LINE_HL,
                   "row": TH.PANEL, "rowln": TH.LINE, "lab": TH.TEXT2,
                   "val": TH.TEXT, "port": TH.FIELD, "portln": TH.TEXT3,
                   "wire": TH.LINE_HL}
        else:
            hit = {"shadow": "#0a0a0c", "body": "#1e1e23", "edge": "#141418",
                   "row": "#232329", "rowln": "#33333a", "lab": "#8b8b93",
                   "val": "#dcdce2", "port": "#1b1b1f", "portln": "#c9c9d1",
                   "wire": "#0d0d10"}
        _NC_CACHE.clear()
        _NC_CACHE[key] = hit
    return hit


def _story_grid_col():
    """网格线颜色：浅色主题用主题的描边色，硬编码 #26262c 在白底上就是一张黑网。"""
    try:
        return TH.LINE if TH.IS_LIGHT else _GRID
    except Exception:
        return _GRID


def _story_key_path(i):
    """第 i 镜（0 基）最新的关键帧图片；没有就返回空串。"""
    try:
        pre = (title_var.get() or "").strip()
    except Exception:
        pre = ""
    pats = []
    if pre:
        pats.append(os.path.join(AP.OUTDIR, "%s_key_%d_*.png" % (pre, i + 1)))
    pats.append(os.path.join(AP.OUTDIR, "*_key_%d_*.png" % (i + 1)))
    for pat in pats:
        try:
            f = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
        except Exception:
            f = []
        if f:
            return f[0]
    return ""


def _rrect(cv, x1, y1, x2, y2, r=7, **kw):
    """圆角矩形（Tk 没有原生圆角矩形，用平滑多边形近似）。"""
    r = max(1, min(r, (y2 - y1) / 2, (x2 - x1) / 2))
    pts = (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1)
    return cv.create_polygon(pts, smooth=True, splinesteps=14, **kw)


def _story_wire(cv, x1, y1, x2, y2, color="#FFC24B", w=2):
    """连线：真正的三次贝塞尔（两端切线都是水平的），和 ComfyUI 画法一致。

    之前用 Tk 的 smooth 折线凑，控制点位置不对，竖向连的时候会拧成一个个小 S 弯，
    看着特别别扭。这里自己按公式采样 24 段，形状完全可控：
      · 出口朝右、入口朝左，所以两端都往水平方向先伸一段再拐弯；
      · 两个节点上下叠着时，线会从右侧兜一个弧再进下一镜的左边 —— 就是蓝图那种感觉。
    """
    z = _STORY["z"]
    k = max(46 * z, abs(x2 - x1) * 0.65 + 26 * z)
    p0 = (x1, y1)
    p1 = (x1 + k, y1)
    p2 = (x2 - k, y2)
    p3 = (x2, y2)
    n = 26
    pts = []
    for i in range(n + 1):
        t = i / float(n)
        u = 1.0 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        pts.append(a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0])
        pts.append(a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])
    cv.create_line(*pts, width=w + 3, fill=_nc_map()["wire"], joinstyle="round")
    cv.create_line(*pts, width=w, fill=color, joinstyle="round")


def _story_widget(cv, x, y, w, label, value, tag=""):
    """节点里的一行 widget（ComfyUI 那种小圆角输入行）。"""
    z = _STORY["z"]
    h = max(14, int(20 * z))
    nc = _nc_map()
    _rrect(cv, x, y, x + w, y + h, max(2, int(4 * z)), fill=nc["row"],
           outline=nc["rowln"], tags=tag)
    cv.create_text(x + 7 * z, y + h / 2, text=label, anchor="w",
                   fill=nc["lab"], font=(TH.FONT[0], max(6, int(8 * z))),
                   tags=tag)
    cv.create_text(x + w - 7 * z, y + h / 2, text=value, anchor="e",
                   fill=nc["val"], font=(TH.FONT[0], max(6, int(8 * z))),
                   tags=tag)
    return y + h + 3 * z


def _story_node(cv, x, y, w, title, kind="shot", rows=None, img=None,
                sel=False, key="", h=None):
    """画一个 ComfyUI 风格的节点，返回实际矩形 (x1,y1,x2,y2)。

    rows: [(标签, 值), ...] 会渲染成节点内部的 widget 行
    img:  缩略图（直接放在标题条下面，像 Load/Save Image 节点那样）
    """
    z = _STORY["z"]
    tag = ("node:" + key) if key else ""
    if key:
        ox, oy = _STORY["off"].get(key, (0, 0))
        x += ox
        y += oy
    pad = max(3, int(5 * z))
    hd = max(16, int(24 * z))
    rad = max(4, int(8 * z))
    rows = rows or []
    rh = max(14, int(20 * z)) + 3 * z
    # 内容高度
    body = pad
    if img is not None:
        body += img.height() + pad
    body += rh * len(rows) + pad
    if h is None:
        h = hd + max(body, max(16, int(20 * z)))
    accent = _NC.get(kind, "#3a3a42")
    nc = _nc_map()
    edge = TH.ACCENT if sel else nc["edge"]
    # 投影 + 主体
    _rrect(cv, x + 2, y + 3, x + w + 2, y + h + 3, rad, fill=nc["shadow"],
           outline="")
    _rrect(cv, x, y, x + w, y + h, rad, fill=nc["body"], outline=edge,
           width=(2 if sel else 1), tags=tag)
    # 标题条（上半圆角 + 补一块直角，做出"只在顶部圆角"的效果）
    _rrect(cv, x, y, x + w, y + hd, rad, fill=accent, outline="", tags=tag)
    cv.create_rectangle(x, y + hd - rad, x + w, y + hd, fill=accent,
                        outline="", tags=tag)
    cv.create_text(x + 8 * z, y + hd / 2, text=title, anchor="w",
                   fill="#FFFFFF", font=(TH.FONT[0], max(7, int(9 * z)), "bold"),
                   tags=tag)
    # 内容
    cy = y + hd + pad
    if img is not None:
        cv.create_image(x + w / 2, cy, image=img, anchor="n", tags=tag)
        cy += img.height() + pad
    for lab, val in rows:
        cy = _story_widget(cv, x + pad, cy, w - pad * 2, lab, val, tag)
    if key:
        r = max(3, int(4.5 * z))
        cx, cy2 = x, y + hd / 2
        cv.create_oval(cx - r, cy2 - r, cx + r, cy2 + r, fill=nc["port"],
                       outline=nc["portln"], width=2, tags=tag)
        cx2 = x + w
        cv.create_oval(cx2 - r, cy2 - r, cx2 + r, cy2 + r, fill=accent,
                       outline=nc["portln"], width=2, tags=tag)
        _STORY["nodes"][key] = {"rect": (x, y, x + w, y + h),
                                "in": (x, cy2), "out": (cx2, cy2),
                                "kind": kind}
    return (x, y, x + w, y + h)


def _story_grid(cv, w, h):
    """画布点阵网格（随平移/缩放走）。用细线而不是点：Tk 里几千个点是自杀。"""
    _col = _story_grid_col()
    step = max(18, int(38 * _STORY["z"]))
    x0 = _STORY["ox"] % step
    y0 = _STORY["oy"] % step
    for gx in range(x0, w, step):
        cv.create_line(gx, 0, gx, h, fill=_col)
    for gy in range(y0, h, step):
        cv.create_line(0, gy, w, gy, fill=_col)


# 剧情预览缩略图缓存：(路径, 宽, mtime) -> PhotoImage。
# 以前每格滚轮 / 每 2 秒定时刷新都要把 26 张关键帧原图重新解码缩放一遍，
# 这是「剧情预览滚轮很卡」的主因。缓存后同一张图同一尺寸只解码一次；
# 文件被重新生成时 mtime 变化自动失效。
_STORY_THUMBS = {}


def _story_thumb(kp, w):
    """带缓存的分镜缩略图（等价于 _box_thumb(kp, w, w*0.56)）。"""
    h = int(w * 0.56)
    try:
        mt = os.path.getmtime(kp)
    except Exception:
        mt = 0
    key = (kp, w, mt)
    ph = _STORY_THUMBS.get(key)
    if ph is not None:
        return ph
    ph = _box_thumb(kp, w, h)
    if ph is not None:
        if len(_STORY_THUMBS) > 400:      # 换剧本后旧缓存作废，兜底防爆内存
            _STORY_THUMBS.clear()
        _STORY_THUMBS[key] = ph
    return ph


def _story_draw():
    """重画整张节点图。"""
    cv = _story_cv
    _STORY["nodes"] = {}
    try:
        cv.delete("all")
    except Exception:
        return
    try:
        W, H = cv.winfo_width(), cv.winfo_height()
    except Exception:
        W, H = 900, 500
    _story_grid(cv, max(W, 100), max(H, 100))

    n = len(shot_rows)
    z = _STORY["z"]
    ox, oy = _STORY["ox"], _STORY["oy"]
    nw = int(238 * z)
    gapx = int(86 * z)
    gapy = int(26 * z)

    if not n:
        cv.create_text(ox + 30, oy + 30, text="还没有剧情", anchor="nw",
                       fill="#c9c9d1", font=(TH.FONT[0], 13))
        cv.create_text(ox + 30, oy + 64, anchor="nw", width=520,
                       fill="#7d7d86", font=(TH.FONT[0], 9),
                       text="点工具栏「⇩ 导入剧本」把剧本贴进来，或者左栏「＋」一镜一镜写。\n"
                            "导入后这里会画成节点图：剧本 → 角色/场景 → 各镜 → 成片。")
        return

    # ---- 三列：剧本 | 角色·场景 | 分镜（竖排）→ 成片
    c_script = ox
    c_ref = ox                      # 角色/场景和剧本叠在同一列（更紧凑，也像 ComfyUI 的左侧输入组）
    c_shot = ox + nw + gapx

    try:
        ttl = (title_var.get() or "未命名").strip()
    except Exception:
        ttl = "未命名"
    r_script = _story_node(cv, c_script, oy, nw, "📜 剧本", kind="script",
                           rows=[("剧名", (ttl[:5] + "…") if len(ttl) > 5 else ttl),
                                 ("镜数", "%d" % n)],
                           key="script")

    # 角色 / 场景（紧凑小节点，竖着排）
    ref_nodes = []
    y = oy + (r_script[3] - r_script[1]) + gapy
    try:
        chars = list(CHAR_REFS.items())[:8]
    except Exception:
        chars = []
    for nm, _fn in chars:
        r = _story_node(cv, c_ref, y, nw, "👤 " + nm, kind="char",
                        rows=[("用途", "角色定妆照")], key="char:" + nm)
        ref_nodes.append(("char", nm, r))
        y = r[3] + gapy
    try:
        scenes = list(SCENE_REFS.items())[:8]
    except Exception:
        scenes = []
    for nm, _fn in scenes:
        r = _story_node(cv, c_ref, y, nw, "🏞 " + nm, kind="scene",
                        rows=[("用途", "场景底图")], key="scene:" + nm)
        ref_nodes.append(("scene", nm, r))
        y = r[3] + gapy

    # 分镜（竖着串，缩略图直接画在节点里）
    pos = []
    sy = oy
    for i, rec in enumerate(shot_rows):
        img = None
        kp = _story_key_path(i)
        if kp:
            img = _story_thumb(kp, int(nw - 12 * z))
        _st, _live = _story_shot_state(i, bool(kp))
        rows = [("景别", (rec["size_var"].get() or "—")),
                ("时长", (rec["dur_var"].get() or "—") + "s"),
                ("角色", (rec["ref_var"].get() or "未绑")),
                ("状态", _st)]
        r = _story_node(cv, c_shot, sy, nw, "🎬 第 %d 镜" % (i + 1),
                        kind="shot", rows=rows, img=img,
                        sel=(i == _SEL["i"] or _live), key="shot:%d" % i)
        pos.append(r)
        sy = r[3] + gapy

    # 成片
    _lv = _STORY.get("live") or {}
    _stg = str(_lv.get("stage") or "")
    if _GEN_RUNNING[0] and _stg and _stg not in ("关键帧",):
        _ost = "▶ %s %d%%" % (_stg, int(round(float(_lv.get("pct") or 0) * 100)))
    elif _GEN_RUNNING[0]:
        _ost = "等关键帧"
    else:
        _ost = "未开始"
    r_out = _story_node(cv, c_shot, sy, nw, "🎬 成片", kind="out",
                        rows=[("输出", "output/*.mp4"), ("状态", _ost)],
                        key="out")

    # ---- 连线
    _story_wire(cv, r_script[2], (r_script[1] + r_script[3]) / 2,
                pos[0][0], pos[0][1] + 12 * z, color=_WC["mine"], w=2)
    for i in range(n - 1):
        # 从上一镜右侧出口 → 下一镜左侧入口（和 ComfyUI 的接法一样）
        _story_wire(cv, pos[i][2], pos[i][1] + 12 * z,
                    pos[i + 1][0], pos[i + 1][1] + 12 * z,
                    color=_WC["mine"], w=2)
    _story_wire(cv, pos[-1][2], pos[-1][1] + 12 * z,
                r_out[0], r_out[1] + 12 * z, color=_WC["mine"], w=2)
    # 角色 / 场景 → 用到它的镜
    for i, rec in enumerate(shot_rows):
        want = [(rec["ref_var"].get() or "").strip(),
                (rec["loc_var"].get() or "").strip()]
        for kind, name, (rx1, ry1, rx2, ry2) in ref_nodes:
            if name and name in want:
                col = _WC["char"] if kind == "char" else _WC["scene"]
                _story_wire(cv, rx2, (ry1 + ry2) / 2,
                            pos[i][0], pos[i][1] + 12 * z, color=col, w=2)


def _story_fit_view():
    """把整张图缩到能一屏看完（只在首次进入 / 复位时用）。"""
    n = max(1, len(shot_rows))
    try:
        avail_w = max(360, _story_cv.winfo_width() - 40)
        avail_h = max(260, _story_cv.winfo_height() - 30)
    except Exception:
        avail_w, avail_h = 900, 480
    # 宽度：两列 + 间距；高度只按"看得见两镜"算 —— 全塞进一屏会缩到 40%，
    # 字全糊了。竖排本来就是要往下看，留两镜高度、其余靠平移。
    need_w = 238 * 2 + 86 + 70
    z = min(1.0, avail_w / float(need_w),
            avail_h / float(max(1, min(n, 2)) * (250 + 26) + 40))
    _STORY["z"] = round(max(0.55, z), 2)
    _STORY["ox"] = 24
    _STORY["oy"] = 24


def _story_shot_state(i, has_key):
    """这一镜现在的状态文字 + 是否正在处理（1 基镜号对 PG 的「第 i/n 张」）。"""
    live = _STORY.get("live") or {}
    stage = str(live.get("stage") or "")
    cur = int(live.get("i") or 0)
    pct = int(round(float(live.get("pct") or 0) * 100))
    running = bool(_GEN_RUNNING[0])
    if running and stage and cur == i + 1:
        if stage == "视频" and (time.time() - float(_STEP.get("at") or 0)) < 25:
            _c, _t = int(_STEP.get("cur") or 0), int(_STEP.get("total") or 0)
            if _t:
                return ("▶ 视频 · 采样 %d/%d 步" % (_c, _t), True)
        return ("▶ %s %d%%" % (stage, pct), True)
    if has_key:
        return ("✓ 已出图", False)
    if running:
        return ("排队中…", False)
    return ("待出图", False)


def _story_refresh():
    """刷新标题/统计并重画节点图。"""
    n = len(shot_rows)
    try:
        _story_title.configure(
            text="《%s》 · %d 镜" % ((title_var.get() or "未命名").strip(), n))
        got = sum(1 for i in range(n) if _story_key_path(i))
        live = _STORY.get("live") or {}
        if _GEN_RUNNING[0] and live.get("stage"):
            _p = int(round(float(live.get("pct") or 0) * 100))
            _txt = "⏳ 正在生成：%s %s · 总进度 %d%%" % (
                live.get("stage"), live.get("text") or "", _p)
            if str(live.get("stage")) == "视频" and \
                    (time.time() - float(_STEP.get("at") or 0)) < 25 and \
                    _STEP.get("total"):
                _txt += " · 采样 %d/%d 步" % (_STEP["cur"], _STEP["total"])
            if _GEN_T0[0]:
                _txt += " · 已用 %s" % _hms(time.time() - _GEN_T0[0])
        else:
            _txt = ("关键帧 %d/%d · 拖空白处平移 · 滚轮缩放 · 拖右边圆点到左边圆点＝连线"
                    % (got, n))
        _story_hint.configure(text=(_txt if n else ""))
    except Exception:
        pass
    if _STORY.get("auto"):
        _story_fit_view()
    _story_draw()
    _story_paint_zoom()


def _story_reset_view():
    _STORY["auto"] = True
    _STORY["off"] = {}
    _story_refresh()


# ---------------- 右下角缩放条（和 ComfyUI 一样：− 100% + 复位） -------------
_zoombar = tk.Frame(_story_host, background="#232329", bd=0,
                    highlightthickness=1, highlightbackground="#33333a")
_zoombar.place(relx=1.0, rely=1.0, x=-12, y=-12, anchor="se")
_zoom_lbl = tk.Label(_zoombar, text="100%", background="#232329",
                     foreground="#c9c9d1", font=(TH.FONT[0], 8), width=5)


def _story_zoom(d):
    z = min(1.8, max(0.4, round(_STORY["z"] + d, 2)))
    if abs(z - _STORY["z"]) < 0.001:
        return
    _STORY["auto"] = False
    _STORY["z"] = z
    _story_draw()
    _story_paint_zoom()


def _story_paint_zoom():
    try:
        _zoom_lbl.configure(text="%d%%" % int(_STORY["z"] * 100))
    except Exception:
        pass


def _zbtn(text, cmd):
    b = tk.Label(_zoombar, text=text, background="#232329",
                 foreground="#9a9aa3", font=(TH.FONT[0], 9), padx=7, pady=1,
                 cursor="hand2")
    b.pack(side="left")
    b.bind("<Button-1>", lambda e: cmd())
    b.bind("<Enter>", lambda e: b.configure(foreground="#ffffff"))
    b.bind("<Leave>", lambda e: b.configure(foreground="#9a9aa3"))
    return b


_zbtn("−", lambda: _story_zoom(-0.1))
_zoom_lbl.pack(side="left", pady=1)
_zbtn("+", lambda: _story_zoom(0.1))
_zbtn("⤢", _story_reset_view)


# ---------------- 交互：拖动 / 平移 / 连线 ---------------------------------
def _story_hit(x, y, what="out", tol=12):
    best, bd = "", tol * tol
    for k, nd in _STORY["nodes"].items():
        px, py = nd.get(what, (1e9, 1e9))
        d = (px - x) ** 2 + (py - y) ** 2
        if d <= bd:
            best, bd = k, d
    return best


def _story_hit_body(x, y):
    for k, nd in _STORY["nodes"].items():
        x1, y1, x2, y2 = nd["rect"]
        if x1 <= x <= x2 and y1 <= y <= y2:
            return k
    return ""


def _story_connect(src, dst):
    """把一条手拉的线变成实际改动。

    · 角色/场景 → 某一镜 = 给那一镜绑这个角色 / 这个场景
    · 镜A → 镜B        = 把第 A 镜挪到第 B 镜前面（调整成片顺序）
    · 其它（剧本/成片）没有可改的，给一句提示
    """
    if not src or not dst or src == dst:
        return
    sk, dk = src.split(":"), dst.split(":")
    if sk[0] in ("char", "scene") and dk[0] == "shot":
        i = int(dk[1])
        rec = _row_of(i)
        if rec is None:
            return
        if sk[0] == "char":
            rec["ref_var"].set(sk[1])
            set_status("已把角色「%s」绑到第 %d 镜" % (sk[1], i + 1), "ok")
        else:
            rec["loc_var"].set(sk[1])
            rec["loc_manual"][0] = True
            set_status("已把场景「%s」绑到第 %d 镜" % (sk[1], i + 1), "ok")
        try:
            _outline_refresh()
            _select(i)
        except Exception:
            pass
        return
    if sk[0] == "shot" and dk[0] == "shot":
        a, b = int(sk[1]), int(dk[1])
        if a == b:
            return
        try:
            rec = shot_rows.pop(a)
            b2 = b - 1 if a < b else b
            shot_rows.insert(b2, rec)
            _outline_refresh()
            _select(b2)
            set_status("已把第 %d 镜移到第 %d 镜前面" % (a + 1, b + 1), "ok")
        except Exception as _e:
            _crash("调整镜序失败: %r" % (_e,))
        return
    set_status("这条线没有含义：剧本 / 成片是固定端点，改不了", "warn")


def _story_press(e):
    _STORY["auto"] = False
    k = _story_hit(e.x, e.y, "out")
    if k:
        _STORY["link"] = k
        _STORY["drag"] = None
        _STORY["drag_node"] = None
        return
    kb = _story_hit_body(e.x, e.y)
    if kb:
        _STORY["drag_node"] = kb
        _STORY["drag"] = (e.x, e.y)
        if kb.startswith("shot:"):
            try:
                _select(int(kb.split(":")[1]))
            except Exception:
                pass
        return
    _STORY["drag"] = (e.x, e.y)
    _STORY["drag_node"] = None


def _story_drag(e):
    if _STORY.get("link"):
        nd = _STORY["nodes"].get(_STORY["link"])
        if not nd:
            return
        try:
            _story_cv.delete("tempwire")
            px, py = nd["out"]
            _story_cv.create_line(px, py, e.x, e.y, smooth=True, width=2,
                                  fill=_WC["mine"], dash=(5, 4),
                                  tags="tempwire")
            _story_cv.create_oval(e.x - 4, e.y - 4, e.x + 4, e.y + 4,
                                  fill=_WC["mine"], outline="", tags="tempwire")
        except Exception:
            pass
        return
    if not _STORY.get("drag"):
        return
    x0, y0 = _STORY["drag"]
    dx, dy = e.x - x0, e.y - y0
    if dx == 0 and dy == 0:
        return
    _STORY["drag"] = (e.x, e.y)
    k = _STORY.get("drag_node")
    if k and k in _STORY["nodes"]:
        try:
            _story_cv.move("node:" + k, dx, dy)
        except Exception:
            return
        ox, oy = _STORY["off"].get(k, (0, 0))
        _STORY["off"][k] = (ox + dx, oy + dy)
        nd = _STORY["nodes"][k]
        nd["rect"] = (nd["rect"][0] + dx, nd["rect"][1] + dy,
                      nd["rect"][2] + dx, nd["rect"][3] + dy)
        nd["in"] = (nd["in"][0] + dx, nd["in"][1] + dy)
        nd["out"] = (nd["out"][0] + dx, nd["out"][1] + dy)
        return
    # 空白处拖动 = 平移整张图（网格也在 "all" 里，一起走）
    _STORY["ox"] += dx
    _STORY["oy"] += dy
    try:
        _story_cv.move("all", dx, dy)
    except Exception:
        pass


def _story_release(e=None):
    lk = _STORY.get("link")
    _STORY["link"] = None
    try:
        _story_cv.delete("tempwire")
    except Exception:
        pass
    if lk and e is not None:
        tgt = _story_hit(e.x, e.y, "in")
        if tgt:
            _story_connect(lk, tgt)
        _story_draw()
        _story_paint_zoom()
        _STORY["drag"] = None
        _STORY["drag_node"] = None
        return
    if _STORY.get("drag_node"):
        _STORY["drag_node"] = None
        _story_draw()
        _story_paint_zoom()
    _STORY["drag"] = None


# 滚轮合并：Windows 高速滚轮一秒能来十几个事件，每次都整图重画必然卡。
# 把连续事件攒起来，30ms 窗口内只重绘一次（缩放量取累加值，手感不变）。
_WHEEL = {"d": 0.0, "aid": None}


def _story_wheel(e):
    _WHEEL["d"] += 0.1 * (1 if getattr(e, "delta", 0) > 0 else -1)
    if _WHEEL["aid"] is None:
        _WHEEL["aid"] = _story_cv.after(30, _story_wheel_apply)
    return "break"


def _story_wheel_apply():
    _WHEEL["aid"] = None
    if _WHEEL["d"]:
        _story_zoom(round(_WHEEL["d"], 2))
        _WHEEL["d"] = 0.0


def _story_menu(e):
    """右键菜单：空白处＝加一镜 / 导入剧本；节点上＝这一镜的常用操作。

    ComfyUI 里右键是「加节点」，这里对应「加一镜 / 绑角色 / 调顺序」——
    同一套操作习惯，只是对象从节点变成镜头。
    """
    import tkinter as tk2
    key = _story_hit_body(e.x, e.y)
    m = tk2.Menu(_story_cv, tearoff=0, font=(TH.FONT[0], 9),
                 background=TH.PANEL, foreground=TH.TEXT,
                 activebackground=TH.SEL_BG, activeforeground=TH.TEXT)
    if key.startswith("shot:"):
        i = int(key.split(":")[1])
        m.add_command(label="选中这一镜（右栏编辑）",
                      command=lambda: (_select(i), _story_draw()))
        m.add_command(label="复制这一镜", command=lambda: (_dup_shot(i), _story_draw()))
        m.add_command(label="上移", command=lambda: (move_shot(-1), _story_draw()))
        m.add_command(label="下移", command=lambda: (move_shot(1), _story_draw()))
        m.add_separator()
        sub = tk2.Menu(m, tearoff=0, font=(TH.FONT[0], 9))
        for nm in list(CHAR_REFS.keys())[:12]:
            sub.add_command(
                label=nm,
                command=lambda n=nm, ii=i: (
                    _row_of(ii)["ref_var"].set(n),
                    _outline_refresh(), _select(ii), _story_draw()))
        m.add_cascade(label="绑定参考角色", menu=sub)
        m.add_separator()
        m.add_command(label="删除这一镜", command=lambda: (del_shot(i), _story_draw()))
    elif key:
        m.add_command(label="这一项不能单独操作", state="disabled")
    else:
        m.add_command(label="＋ 添加一镜", command=lambda: (add_shot(), _story_draw()))
        m.add_command(label="⇩ 导入剧本…", command=open_import)
        m.add_separator()
        m.add_command(label="⤢ 复位视图", command=_story_reset_view)
        m.add_command(label="⟳ 刷新", command=_story_refresh)
    try:
        m.tk_popup(e.x_root, e.y_root)
    finally:
        try:
            m.grab_release()
        except Exception:
            pass
    return "break"


def _story_dbl(e):
    k = _story_hit_body(e.x, e.y)
    if k.startswith("shot:"):
        _select(int(k.split(":")[1]))
        try:
            _rt_show("detail")      # 双击＝把右栏切到细节，直接改这一镜
        except Exception:
            pass
    elif k:
        set_status("剧本 / 角色 / 场景节点：拖到某一镜的左边圆点可以建立关联", "ok")
    return "break"


_story_cv.bind("<Button-3>", _story_menu)
_story_cv.bind("<Double-Button-1>", _story_dbl)
_story_cv.bind("<ButtonPress-1>", _story_press)
_story_cv.bind("<B1-Motion>", _story_drag)
_story_cv.bind("<ButtonRelease-1>", _story_release)
_story_cv.bind("<MouseWheel>", _story_wheel)
_CFG_AID = {"v": None}


def _story_on_configure(e):
    """窗口/面板尺寸变化：防抖 120ms 后再重画，拖边框时不至于连重绘几十次。"""
    if not _STORY.get("auto"):
        return
    if _CFG_AID["v"] is not None:
        try:
            _story_cv.after_cancel(_CFG_AID["v"])
        except Exception:
            pass
    _CFG_AID["v"] = _story_cv.after(120, _story_refresh)


_story_cv.bind("<Configure>", _story_on_configure, add="+")


def _story_tick():
    """停在剧情预览页时定期刷新：生成中 2 秒一次，空闲 6 秒一次。

    正在拖节点 / 拉线的时候跳过（每 2 秒整块重画会把拖动打断）。
    """
    try:
        busy = bool(_STORY.get("drag") or _STORY.get("link")
                    or _STORY.get("drag_node"))
        if _VP.get("mode") == "story" and not busy:
            _story_refresh()
    except Exception:
        pass
    try:
        root.after(2000 if _GEN_RUNNING[0] else 6000, _story_tick)
    except Exception:
        pass


root.after(4000, _story_tick)
_vp_paint()

root.after(400, _vp_sash_init)
root.after(1200, _vp_sash_init)

_web = {"wv": None, "open": False, "full": False, "saved": None,
        "nav": False, "base_min": None, "prev_w": None,
        "drag_x": None, "drag_w": None, "drag_ev": None,
        "drag_pending": False, "last_fit_w": None}

# ===================== 无限画布面板（左侧，:3000） =====================
# 完全独立：自己的 WebView2、自己的状态、自己的提示。任何异常都被兜住，
# 不会影响右侧原始 ComfyUI 面板，也不会拖垮整个工作室。
_canvas = {"wv": None, "open": True, "nav": False,
           "drag_x": None, "drag_w": None, "drag_ev": None,
           "drag_pending": False, "last_fit_w": None}

_canvas_tip = tk.Label(canvas_web, text="", font=TH.FONT_S, justify="left",
                       wraplength=_CANVAS_INNER_W - 90, background="#1e1e1e",
                       foreground="#c8c8c8")
_canvas_tip.place(relx=.5, rely=.5, anchor="center")

# ---- 无限画布服务（:3000）自启动 ----------------------------------------
# 【2026-10-30 修】以前 :3000 只能靠用户自己去双击 start-local.bat，
# exe 打开画布面板只会 navigate，没起服务就是「127.0.0.1 拒绝连接」。
# 现在：面板一加载就探测 3000-3010，没在跑就用自带 node 拉起 launcher/start.mjs。
_CANVAS_SVC = {"proc": None, "starting": False, "port": 0,
               "fail": "", "log": None}


def _port_open(p, timeout=0.25):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=timeout):
            return True
    except Exception:
        return False


def _http_alive(url, timeout=1.2):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= int(getattr(r, "status", 200)) < 400
    except Exception:
        return False


def _canvas_probe():
    """找已经在跑的画布服务端口；找不到返回 0。

    先看端口通不通（快），再用 /index.html 确认那真的是画布，
    免得 3000 被别的程序占了还误当成画布。
    """
    for p in AP.CANVAS_PORTS:
        if _port_open(p) and _http_alive("http://127.0.0.1:%d/index.html" % p):
            return p
    return 0


def _canvas_set_url(port):
    global CANVAS_URL
    CANVAS_URL = "http://127.0.0.1:%d" % port
    _CANVAS_SVC["port"] = port
    try:
        canvas_title_lbl.configure(text="🗺 无限画布 · :%d" % port)
    except Exception:
        pass


def _canvas_console_log():
    path = os.path.join(AP.CANVAS_DIR, "_canvas_console.log") if AP.CANVAS_DIR else ""
    return path


def _canvas_log_tail(n=10):
    p = _CANVAS_SVC.get("log") or _canvas_console_log()
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            return "".join(f.readlines()[-n:]).strip()
    except Exception:
        return ""


def start_canvas_service(on_done=None, quiet=False):
    """确保无限画布（:3000）在跑，起来后回调 on_done。

    顺序：画布已在跑 → 直接用；否则先确认 ComfyUI(:8188) 在线（由本程序拉起，
    参数/输入输出目录才和出片流程一致），再跑 launcher/start.mjs --no-open
    —— 它只负责网关 :8190 + 画布 :3000，检测到 8188 已在线就接管，不会重启它。
    全程走后台线程，界面不卡。
    """
    if _CANVAS_SVC["starting"]:
        return
    port = _canvas_probe()
    if port:
        # 已经在跑：也要走一遍 _canvas_on_ready —— 否则面板第一次打开时
        # 服务本来就活着，这里直接 return，WebView2 永远停在空白页。
        _canvas_set_url(port)
        root.after(0, lambda: _canvas_on_ready(port, on_done))
        return
    if not AP.CANVAS_DIR or not AP.CANVAS_LAUNCHER:
        _canvas_set_tip("没找到「ComfyUI-本地无限画布」目录（要含 launcher\\start.mjs）。\n\n"
                        "把画布文件夹放到 %s 下面，\n或在它同级的 config.json 里加 "
                        "\"canvas_dir\": \"D:\\\\你的路径\\\\ComfyUI-Local-Infinite-Canvas\"。"
                        % os.path.dirname(AP.WORKDIR))
        _CANVAS_SVC["fail"] = "canvas dir not found"
        return
    node = AP.CANVAS_NODE or "node"
    _CANVAS_SVC["starting"] = True
    _CANVAS_SVC["fail"] = ""
    if not quiet:
        _canvas_set_tip("正在启动无限画布（:3000）…\n\n"
                        "要先等 ComfyUI(:8188) 就绪，再起网关(:8190)。\n"
                        "首次启动约 10–40 秒，之后秒开。")

    def work():
        try:
            # 1) ComfyUI 必须在线：否则启动器会自己起一份默认配置的 ComfyUI，
            #    输入/输出目录和本程序不一致，出片会“找不到文件”。
            if not check_online(1.5):
                root.after(0, lambda: _canvas_set_tip(
                    "正在启动后端 ComfyUI(:8188)…\n画布要挂在它上面。"))
                root.after(0, start_backend)
                for _ in range(100):
                    time.sleep(1.5)
                    if check_online(1.5):
                        break
                if not check_online(1.5):
                    _CANVAS_SVC["starting"] = False
                    _CANVAS_SVC["fail"] = "comfy offline"
                    root.after(0, lambda: _canvas_set_tip(
                        "ComfyUI(:8188) 还没就绪，画布先不启动。\n\n"
                        "等状态栏变绿「● 后端在线」后再点【🗺 画布】重试。"))
                    return
            # 2) 起画布 + 网关
            env = dict(os.environ)
            if AP.COMFY_PYTHON:
                env["COMFY_PYTHON"] = AP.COMFY_PYTHON
            env["PYTHONIOENCODING"] = "utf-8"
            env["COMFYUI_DIR"] = AP.CFG.get("comfyDir") or ""
            logp = _canvas_console_log()
            _CANVAS_SVC["log"] = logp
            f = open(logp, "w", encoding="utf-8", errors="ignore")
            NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            try:
                proc = subprocess.Popen(
                    [node, AP.CANVAS_LAUNCHER, "--no-open"],
                    cwd=AP.CANVAS_DIR, env=env,
                    stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | NO_WIN)
            finally:
                try:
                    f.close()
                except Exception:
                    pass
            _CANVAS_SVC["proc"] = proc
            _crash("无限画布 启动器 PID=%s cwd=%s" % (proc.pid, AP.CANVAS_DIR))
        except Exception as e:
            _CANVAS_SVC["starting"] = False
            _CANVAS_SVC["fail"] = repr(e)
            _crash("无限画布 启动异常: %r" % (e,))
            root.after(0, lambda: _canvas_set_tip("无限画布启动失败：%r\n\n"
                                                 "可手动双击 %s\\start-venv.bat 看报错。"
                                                 % (e, AP.CANVAS_DIR)))
            return
        # 3) 等它把 :3000 挂起来（最多 90 秒）
        for _ in range(60):
            time.sleep(1.5)
            p = _canvas_probe()
            if p:
                _CANVAS_SVC["starting"] = False
                _canvas_set_url(p)
                _crash("无限画布 就绪 :%d" % p)
                root.after(0, lambda: _canvas_on_ready(p, on_done))
                return
            if _CANVAS_SVC["proc"] and _CANVAS_SVC["proc"].poll() is not None:
                break
        _CANVAS_SVC["starting"] = False
        tail = _canvas_log_tail(8)
        _CANVAS_SVC["fail"] = tail or "timeout"
        _crash("无限画布 启动超时/退出: %s" % (tail[-400:] or "no log"))
        msg = "无限画布没能起来（:3000）。"
        if tail:
            msg += "\n\n启动器最后输出：\n" + tail
        msg += "\n\n可手动双击 %s\\start-venv.bat 看报错。" % AP.CANVAS_DIR
        root.after(0, lambda: _canvas_set_tip(msg))

    threading.Thread(target=work, daemon=True).start()


def _canvas_on_ready(port, on_done=None):
    """画布服务就绪：把 WebView2 导过去（只导一次），再跑回调。"""
    try:
        _canvas_set_tip("")
        cwv = _canvas.get("wv")
        if cwv and cwv.ready and not _canvas["nav"]:
            cwv.navigate(CANVAS_URL)
            _canvas["nav"] = True
    except Exception as e:
        _crash("无限画布 导航异常: %r" % (e,))
    if on_done:
        try:
            on_done()
        except Exception:
            pass


def _canvas_set_tip(msg):
    try:
        if msg:
            _canvas_tip.configure(text=msg)
            _canvas_tip.place(relx=.5, rely=.5, anchor="center")
        else:
            _canvas_tip.place_forget()
    except Exception:
        pass


def _canvas_toggle():
    """收起 / 展开左侧无限画布（等同于切到「只显示 ComfyUI」）。"""
    try:
        _vp_apply("web" if _canvas.get("open") else "split")
    except Exception:
        pass


def _canvas_ensure():
    """懒加载：第一次要点开画布时才启动一个独立的 Edge 内核。"""
    if _canvas["wv"] is not None:
        return
    try:
        ok, why = WV.WebView2Host.supported()
        if not ok:
            _canvas_set_tip("这台机器嵌不了网页：%s\n\n"
                            "可点【🗺 浏览器打开画布】在外部浏览器看。" % why)
            return
        _canvas_set_tip("正在启动 Edge 内核加载无限画布…")
        # 默认底色用白：画布页（画图工作台/图生视频）是浅色页面，窗口还原/重绘
        # 的那一瞬间露出 WebView2 自带的纯黑底就会"闪黑"。钉成白色后那一帧只是
        # 白一下，肉眼基本看不出，和用户报的闪黑现象对得上。
        # 另外关掉 GPU 合成（config: wm_disable_gpu_compositing，默认开），
        # 从根本上消除还原时 GPU 合成层露出的那一帧黑表面。
        wv = WV.WebView2Host(root, canvas_web, bg=(255, 255, 255),
                             disable_gpu_compositing=bool(
                                 AP.CFG.get("wm_disable_gpu_compositing", True)))
        _canvas["wv"] = wv

        def _on_ready():
            try:
                _crash("画布面板 内核就绪，此刻网页区=%dx%d"
                       % (canvas_web.winfo_width(), canvas_web.winfo_height()))
                wv.fit()
                # 服务已在跑就直接导过去；没跑就现场拉起来（起来后由回调导航）
                start_canvas_service()
                for _d in (300, 900, 1800):
                    root.after(_d, lambda: _canvas["wv"] and _canvas["wv"].fit())
            except Exception as e:
                _canvas_set_tip("无限画布加载失败：%r" % (e,))

        if not wv.ensure_created(_on_ready):
            _canvas_set_tip(wv.error or "WebView2 初始化失败")
    except Exception as e:
        _canvas_set_tip("无限画布初始化异常：%r" % (e,))


def _canvas_apply_w(w, save=True):
    """改左侧画布列宽度（拖内部分隔条 / 点「⇤ 复位」）。"""
    global _CANVAS_INNER_W
    try:
        # 独占模式（只有画布或只有 ComfyUI）时视口里只有一个 pane，没有分隔条；
        # 这时候canvas_title 上那个「⇤ 复位」按钮还能点，点了就去 sash_place
        # 一个不存在的 index，日志里会刷 TclError: invalid sash index。
        if len(web_split.panes()) < 2:
            return
        sw = web_split.winfo_width()
        w = max(_CANVAS_INNER_W_MIN,
                min(int(w), _CANVAS_INNER_W_MAX, sw - 200))
        _CANVAS_INNER_W = w
        _ue_sash_set(web_split, 0, w)
        cwv = _canvas.get("wv")
        if cwv:
            cwv.fit()
        wv = _web.get("wv")
        if wv:
            wv.fit()
        # 记住「画布 / ComfyUI」这条内部分界：以前只存了面板宽度，
        # 用户拖好的比例一关程序就没了，下次回到默认 780 —— 感觉「没记住」。
        if save:
            try:
                _web_write_state(canvas_w=w)
            except Exception:
                pass
    except Exception:
        pass


def _c_sash_down(e):
    _canvas["drag_x"] = e.x_root
    _canvas["drag_w"] = _CANVAS_INNER_W
    _canvas["drag_ev"] = e.x_root
    _canvas["drag_pending"] = False


def _c_sash_move(e):
    if _canvas["drag_x"] is None:
        return
    _canvas["drag_ev"] = e.x_root
    if not _canvas.get("drag_pending"):
        _canvas["drag_pending"] = True
        root.after(16, _c_sash_tick)


def _c_sash_tick():
    _canvas["drag_pending"] = False
    if _canvas["drag_x"] is None:
        return
    xr = _canvas.get("drag_ev")
    if xr is None:
        return
    # 【2026-10-30 修】这一条是画布(左)与 ComfyUI(右)之间的分隔条，
    # 和外面那条正好相反：画布在左边，往右拖 = 画布变宽 = 当前 - 按下。
    # 以前照抄外层 web_sash 的 (按下 - 当前)，于是拖右反而变窄 —— 手感是反的。
    _canvas_apply_w(_canvas["drag_w"] + (xr - _canvas["drag_x"]))


def _c_sash_up(e):
    _canvas["drag_x"] = None
    _canvas["drag_w"] = None
    _canvas["drag_pending"] = False
    _canvas_apply_w(_CANVAS_INNER_W)


_web_tip = tk.Label(web_host, text="", font=TH.FONT_S, justify="left",
                    wraplength=_WEB_W - 90, background="#1e1e1e",
                    foreground="#c8c8c8")
_web_tip.place(relx=.5, rely=.5, anchor="center")


def _web_set_tip(msg):
    try:
        if msg:
            _web_tip.configure(text=msg)
            _web_tip.place(relx=.5, rely=.5, anchor="center")
        else:
            _web_tip.place_forget()
    except Exception:
        pass


def _web_state_file():
    return os.path.join(AP.OUTDIR, ".ui_state.json")


def _web_read_state():
    try:
        import json
        with open(_web_state_file(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _web_write_state(**kw):
    try:
        import json
        d = _web_read_state()
        d.update(kw)
        with open(_web_state_file(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def _web_update_bar():
    try:
        # 视口工具条改成一排图标之后，这里只换箭头方向，别再把长文字塞回去
        _web_btn_collapse.configure(text="⟨" if _web["open"] else "⟩")
        _web_btn_full.configure(text="⛗",)
        try:
            _web_top_btn.configure(
                text=("🌐 收起界面" if (_web["open"] or _web["full"])
                      else "🌐 ComfyUI 界面"))
        except Exception:
            pass
        # 「🎬 AI漫剧工坊」按钮文案随常驻列的显隐联动
        try:
            _manju_update_btn()
        except Exception:
            pass
    except Exception:
        pass


# ---- 视口工具条：UE5 那种细条图标按钮（一行放完，不再占两行） ----
# 以前这里是两行文字按钮（"⟨ ComfyUI" "⛶ 全屏" "🌐 浏览器打开" "⇄ 同步左侧参数"…），
# 加上标签栏一共三行 chrome，把视口挤得很矮。改成小方块图标 + 悬停提示，
# 分组之间用 1px 竖线隔开 —— 和虚幻编辑器视口上方那条一样。
def _vpbtn(parent, text, cmd, tip="", side="left", pad=2):
    b = tk.Label(parent, text=text, background=TH.HL, foreground=TH.TEXT2,
                 font=(TH.FONT[0], 9), padx=7, pady=1, cursor="hand2")
    b.pack(side=side, padx=(0, pad))

    def _go(_e=None):
        try:
            cmd()
        except Exception as _ex:
            _crash("视口按钮失败(%s): %r" % (text, _ex))

    def _on(_e=None):
        b.configure(background=TH.ON, foreground=TH.TEXT)
        if tip:
            try:
                set_status(tip, "ok")
            except Exception:
                pass

    b.bind("<Button-1>", _go)
    b.bind("<Enter>", _on)
    b.bind("<Leave>", lambda e: b.configure(background=TH.HL,
                                            foreground=TH.TEXT2))
    return b


def _vpsep(parent):
    f = tk.Frame(parent, background=TH.LINE, width=1, height=16)
    f.pack(side="left", padx=6, pady=3)
    return f


_web_btn_collapse = _vpbtn(web_bar, "⟨", lambda: _web_toggle(),
                           "收起 / 展开 ComfyUI 视口")
_web_btn_full = _vpbtn(web_bar, "⛶", lambda: _web_fullscreen(),
                       "视口最大化（收起左右栏和底部，只留视口）")
_vpbtn(web_bar, "↻", lambda: _web["wv"] and _web["wv"].reload(), "刷新网页")
_vpbtn(web_bar, "←", lambda: _web["wv"] and _web["wv"].go_back(), "后退")
_vpbtn(web_bar, "→", lambda: _web["wv"] and _web["wv"].go_forward(), "前进")
_vpsep(web_bar)

# 左右打通：把左侧参数写进右边画布的节点里
_vpbtn(web_bar, "⇄ 同步参数", lambda: _web_sync_params(),
       "把左侧界面上的参数直接写进右边画布的节点")
_vpbtn(web_bar, "⇢ 推给画布", lambda: _canvas_push_params(),
       "把整部剧的分镜推成画布上的视频节点（画布空会自动建）")
_vpbtn(web_bar, "出图", lambda: _gw_generate("image"), "用画布网关出一张图")
_vpbtn(web_bar, "视频", lambda: _gw_generate("video"), "用画布网关出一段视频")
_vpsep(web_bar)

_web_auto = tk.IntVar(value=0)


def _canvas_open_in_browser():
    """外部浏览器打开画布：没起来先拉起来，起来再开，别让用户对着拒绝连接。"""
    def _go():
        try:
            os.startfile(CANVAS_URL)
        except Exception as e:
            messagebox.showerror("打开画布失败", str(e))
    start_canvas_service(on_done=_go)


# 右侧：自动同步开关（做成可点的小标签，比复选框更贴工具条）+ 帮助 + 地址
_web_sync_auto = tk.IntVar(value=0)


def _toggle_sync_auto():
    v = 0 if _web_sync_auto.get() else 1
    _web_sync_auto.set(v)
    try:
        _web_write_state(web_sync_auto=v)
    except Exception:
        pass
    _paint_sync_auto()


def _paint_sync_auto():
    try:
        on = bool(_web_sync_auto.get())
        _sync_btn.configure(
            text=("☑ 自动同步" if on else "☐ 自动同步"),
            foreground=(TH.ACCENT if on else TH.TEXT2))
    except Exception:
        pass


_sync_btn = _vpbtn(web_bar, "☐ 自动同步", _toggle_sync_auto,
                   "改了就自动把参数同步到画布", side="right", pad=8)
_vpbtn(web_bar, "?", lambda: _web_help(), "画布怎么用", side="right", pad=6)
_vpbtn(web_bar, "🌐", lambda: os.startfile(COMFY_URL),
       "用外部浏览器打开 ComfyUI", side="right", pad=4)
_vpbtn(web_bar, "🗺", _canvas_open_in_browser,
       "用外部浏览器打开无限画布", side="right", pad=4)
tk.Label(web_bar, text=COMFY_URL, background=TH.HL, foreground=TH.TEXT3,
         font=(TH.FONT[0], 8)).pack(side="right", padx=(0, 8))

# 共用后端的约束提示：以前单独占视口一整行，太浪费竖向空间，改到状态条上。
# 左侧一开始生成就变红加强警告，结束恢复琥珀色（见 set_gen_running）。
# 状态条本来就窄：这段话要短，否则会被窗口右边缘切掉（截图里就断在半截）。
_web_conflict_lbl = tk.Label(
    _ue_status,
    text="⚠ 左右共用后端：同时跑会冲突",
    background=TH.HL, foreground=TH.WARN, font=(TH.FONT[0], 8), anchor="w")
_web_conflict_lbl.pack(side="left", padx=(14, 0))


# ===================== AI漫剧工坊：常驻左列 =====================
# 相关代码已挪到上方「manju_col / _manju_col_toggle」处（与 ComfyUI 画布同时可见）。


def _web_fit_window():
    """UE5 布局下不再为了塞下网页面板去改窗口尺寸 —— 视口是中间那一栏，
    用户拖分隔条就够了。保留函数名给外部调用点。"""
    return


def _web_apply_width(w=900, grow=True):
    """面板宽度现在由用户拖分隔条决定，这里只记住想要的宽度并让两块网页贴边。"""
    global _WEB_W, _WEB_W_PREF
    try:
        _WEB_W = max(_WEB_W_MIN, min(int(w or _WEB_W_PREF), _WEB_W_MAX))
        _WEB_W_PREF = _WEB_W
        _ue_refit_webs()
        _web_write_state(web_w=_WEB_W_PREF)
    except Exception:
        pass


def _web_apply_width_smooth(w):
    """拖动中的平滑版本 —— 现在没有自绘分割条了，等同直接应用。"""
    _web_apply_width(w, grow=False)


def _sash_down(e):
    return


def _sash_move(e):
    return


def _sash_tick():
    return


def _sash_up(e):
    return


# 页面刚打开那几秒 ComfyUI 会自己 fitView（加载工作流），会把缩放又改回去，
# 所以校正不是「成功一次就收工」，而是在导航后的一段时间里一直盯着。
_COMFY_ZOOM = {"done": False, "until": 0.0}

_COMFY_ZOOM_JS = """(function(){
  try {
    var a = window.app || (window.comfyAPI && comfyAPI.app && comfyAPI.app.app);
    if (!a || !a.canvas) return 'wait';
    var c = a.canvas, ds = c.ds;
    if (!ds) return 'no-ds';
    var el = c.canvas || {};
    var dpr = window.devicePixelRatio || 1;
    var cx = (el.width || 800) / dpr / 2;
    var cy = (el.height || 600) / dpr / 2;
    var moved = false;
    if (Math.abs(ds.scale - 1) >= 0.01) {
      if (ds.changeScale) { ds.changeScale(1, [cx, cy]); } else { ds.scale = 1; }
      moved = true;
    }
    var nodes = (a.graph && a.graph._nodes) || [];
    if (moved && nodes.length) {
      var x1 = 1e9, y1 = 1e9, x2 = -1e9, y2 = -1e9;
      for (var i = 0; i < nodes.length; i++) {
        var p = nodes[i].pos, sz = nodes[i].size || [120, 60];
        if (p[0] < x1) x1 = p[0];
        if (p[1] < y1) y1 = p[1];
        if (p[0] + sz[0] > x2) x2 = p[0] + sz[0];
        if (p[1] + sz[1] > y2) y2 = p[1] + sz[1];
      }
      var mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      if (ds.centerOn) { ds.centerOn(mx, my); }
      else { ds.offset[0] = cx - mx; ds.offset[1] = cy - my; }
    }
    if (c.setDirty) { c.setDirty(true, true); }
    return (moved ? 'ok:' : 'already:') + ds.scale + '/' + nodes.length;
  } catch (e) { return 'ERR:' + e; }
})()"""


def _comfy_force_zoom():
    """ComfyUI 画布缩放拉回 100%。

    它每次打开都按上次的缩放（或者自动 fit）恢复，窗口一窄就能缩到 10%，
    节点小得看不清（用户反馈「默认改成 100%」）。这里等页面里 app.canvas
    真的有了再改一次；改成功就不再管，之后用户自己缩放不会被抢。
    """
    wv = _web.get("wv")
    if wv is None:
        return
    now = time.time()
    if _COMFY_ZOOM["done"] and now > _COMFY_ZOOM.get("until", 0.0):
        return

    def _done(val, err):
        try:
            v = str(val or "")
            _COMFY_ZOOM["n"] = _COMFY_ZOOM.get("n", 0) + 1
            if not v.startswith('"wait') and v != _COMFY_ZOOM.get("lastv"):
                _COMFY_ZOOM["lastv"] = v
                _crash("缩放校正：#%d %s %s"
                       % (_COMFY_ZOOM["n"], v[:120], (err or "")[:60]))
            if v.startswith('"ok') or v.startswith('"already'):
                _COMFY_ZOOM["last"] = now
                if now > _COMFY_ZOOM.get("until", 0.0):
                    # 观察期过了还没被改回去，才真正收工
                    _COMFY_ZOOM["done"] = True
                    _crash("ComfyUI 缩放已稳定在 100%（%s）" % v)
        except Exception:
            pass

    try:
        wv.eval_js(_COMFY_ZOOM_JS, _done)
    except Exception:
        pass


def _web_ensure():
    """懒加载：真正要点开看的时候才去启动 Edge 内核。"""
    if _web["wv"] is not None:
        _web_loop()
        return
    ok, why = WV.WebView2Host.supported()
    if not ok:
        _web_set_tip("这台机器嵌不了网页：%s\n\n"
                     "可以点【🌐 浏览器打开】在外部浏览器里看，功能一样。" % why)
        return
    _web_set_tip("正在启动 Edge 内核…")
    # 底色给白色：ComfyUI 页面是浅色的，还原瞬间没画出第一帧时露白不露黑。
    # 同时关掉 GPU 合成（config: wm_disable_gpu_compositing，默认开），
    # 消除还原时 GPU 合成层露出的那一帧黑表面。
    wv = WV.WebView2Host(root, web_host, bg=(255, 255, 255),
                         disable_gpu_compositing=bool(
                             AP.CFG.get("wm_disable_gpu_compositing", True)))
    _web["wv"] = wv

    def _on_ready():
        _crash("网页面板 内核就绪，此刻网页区=%dx%d（面板=%d）"
               % (web_host.winfo_width(), web_host.winfo_height(),
                  web_panel.winfo_width()))
        if _ONLINE[0]:
            wv.navigate(COMFY_URL)
            _web["nav"] = True
            _COMFY_ZOOM["done"] = False
            _COMFY_ZOOM["until"] = time.time() + 25
            _web_set_tip("")
        else:
            _web_set_tip("后端还没启动。\n点右上角【⚡ 启动后端】，"
                         "起来后这里会自动加载 ComfyUI。")
        wv.fit()
        # 刚就绪时 Tk 的几何可能还没定完，补几次重排，免得网页只画半截、右边留黑边
        for _d in (300, 900, 1800):
            root.after(_d, lambda: _web["wv"] and _web["wv"].fit())
        _web_loop()

    if not wv.ensure_created(_on_ready):
        _web_set_tip(wv.error or "WebView2 初始化失败")


def _web_loop():
    """后端起来了就自动把 ComfyUI / 无限画布 都加载进来，不用手动刷新。"""
    if not (_web["open"] or _web["full"]):
        return
    try:
        _canvas_ensure()
    except Exception:
        pass
    wv = _web["wv"]
    if wv and wv.ready:
        if _ONLINE[0] and not _web["nav"]:
            wv.navigate(COMFY_URL)
            _web["nav"] = True
            _COMFY_ZOOM["done"] = False
            _COMFY_ZOOM["until"] = time.time() + 25
            _web_set_tip("")
        wv.fit()          # 顺带兜住重排：万一上次 fit 时几何还没定完
        if _web["nav"] and not _COMFY_ZOOM["done"]:
            try:
                _comfy_force_zoom()
            except Exception:
                pass
    cwv = _canvas.get("wv")
    if cwv and cwv.ready:
        if not _canvas["nav"] and not _CANVAS_SVC["starting"]:
            # :3000 没起来就自己去拉（后台线程，不卡界面）；起来后回调里导航
            start_canvas_service(quiet=True)
        cwv.fit()
    root.after(1500, _web_loop)


def _web_top_click():
    """工具栏「🌐 视口」：显示/隐藏视口里的 ComfyUI 网页。"""
    try:
        _web_toggle()
    except Exception:
        pass


def _web_dock_size():
    """面板宽度由 PanedWindow 决定，不再写回固定值。"""
    _ue_refit_webs()


def _web_toggle(force=None):
    """显示 / 隐藏视口里的 ComfyUI（隐藏=切到「无限画布独占」）。

    标签订阅同一套状态：无论从工具栏按钮、视图菜单还是标签点，都走 _vp_apply。
    """
    on = (not _web["open"]) if force is None else force
    try:
        _vp_apply("split" if on else "canvas")
    except Exception as _e:
        _crash("视口切换失败: %r" % (_e,))


def _web_fullscreen():
    """视口最大化：收起左右两栏和底部面板，只留视口；再点还原（等同 UE5 的 F11 视口）。"""
    try:
        if not _web["full"]:
            _web["saved_sash"] = (_ue_sash_get(_ue_row, 0),
                                  _ue_sash_get(_ue_row, 1),
                                  _ue_sash_get(_ue_body, 0))
            _web["full"] = True
            _ue_sash_set(_ue_row, 0, 0)
            _ue_sash_set(_ue_row, 1, _ue_row.winfo_width())
            _ue_sash_set(_ue_body, 0, _ue_body.winfo_height())
        else:
            ss = _web.get("saved_sash")
            _web["full"] = False
            if ss:
                try:
                    _ue_sash_set(_ue_row, 0, ss[0])
                    _ue_sash_set(_ue_row, 1, ss[1])
                    _ue_sash_set(_ue_body, 0, ss[2])
                except Exception:
                    _ue_sash_init()
            else:
                _ue_sash_init()
        if not _web["open"]:
            _web_toggle(True)
        _web_update_bar()
        root.after(120, _ue_refit_webs)
    except Exception as _e:
        _crash("视口最大化失败: %r" % (_e,))


def _web_help():
    """「? 画布怎么用」—— ComfyUI 画布对新手门槛高，把操作说明放回界面里。"""
    w = tk.Toplevel(root)
    TH.paint_toplevel(w)
    w.title("右边的画布怎么用")
    center_win(w, 640, 600)
    w.transient(root)
    try:
        w.grab_set()
    except Exception:
        pass
    txt = scrolledtext.ScrolledText(w, wrap="word", font=("Microsoft YaHei", 10))
    txt.pack(fill="both", expand=True, padx=10, pady=10)
    txt.insert("end", _WEB_HELP)
    txt.configure(state="disabled")


def _web_sync_params(auto=False):
    """把左边界面上正在用的参数，写进右边画布上对应的节点。

    auto=True 时用的是「自动同步」路径：不弹框、不打断操作，结果只进日志。
    """
    import json as _js
    if not (_web["open"] or _web["full"]):
        if not auto:
            messagebox.showinfo("先展开面板",
                                "先点顶栏【🌐 ComfyUI 界面】把画布展开，再点同步。")
        return False
    wv = _web["wv"]
    if not wv or not wv.ready:
        if not auto:
            messagebox.showinfo("画布还没就绪",
                                "ComfyUI 页面还在加载，等界面出来后再点同步。")
        return False
    try:
        res, steps_override = SPEED_PRESETS.get(speed_var.get(), ((448, 800), 4))
        landscape = ASPECTS.get(aspect_var.get(), "portrait") == "landscape"
        w_, h_ = (res[1], res[0]) if landscape else res
        try:
            length = int(length_var.get())
        except Exception:
            length = 22
        # 提示词用第 1 镜，按左边同样的规则拼（自动补景别/光线/防崩词）
        prompt = ""
        try:
            shots = collect_shots()
            if shots:
                s = shots[0]
                style = style_var.get().strip()
                _szs = (s.get("size") or "").strip()
                _suf = (("，" + SIZE_TEXT[_szs])
                        if (_szs in SIZE_TEXT and not auto_prompt_var.get()) else "")
                prompt = (_auto_prompt(s["scene"], style, size=_szs,
                                       direct=s.get("direct", ""))
                          if auto_prompt_var.get()
                          else (s["scene"] + _suf + style))
        except Exception:
            prompt = ""
        params = {"prompt": prompt, "width": int(w_), "height": int(h_),
                  "length": int(length), "steps": int(steps_override),
                  "unet": MODEL_PRESETS.get(model_var.get(), "")}
        if not prompt and not auto:
            log_q.put("[同步] 第 1 镜还没写画面描述，提示词这次先跳过\n")
    except Exception as e:
        if not auto:
            messagebox.showerror("读参数失败", str(e))
        return False

    def done(val, err):
        if "--sync-selftest" in sys.argv:      # 自检时也留一份到崩溃日志，方便无人值守时看
            _crash("同步自检 同步返回 err=%s val=%s" % (err, (val or "")[:700]))
        if err:
            log_q.put("[同步] 没能写进画布：%s\n" % err)
            return
        try:
            d = _js.loads(val or "{}")
        except Exception:
            log_q.put("[同步] 画布返回看不懂：%s\n" % (val or "")[:120])
            return
        if not d.get("ok"):
            log_q.put("[同步] %s\n" % d.get("msg", "失败"))
            return
        for line in d.get("log", []):
            log_q.put("   · %s\n" % line)
        log_q.put("[同步] 共写入 %d 处（画质 %dx%d · %d 帧 · %d 步）\n"
                  % (d.get("hits", 0), params["width"], params["height"],
                     params["length"], params["steps"]))
        if not d.get("hits") and not auto:
            messagebox.showinfo(
                "画布上没有对得上的节点",
                "没找到能写入的节点。同步是按节点类型找的，需要画布上有这类节点：\n\n"
                "· 提示词 / 尺寸 / 帧数：ManjuLatentRelay、MiniMaxH3ImageToVideo、\n"
                "  CLIPTextEncode、EmptyLatentImage …\n"
                "· 采样步数：BasicScheduler、KSampler\n"
                "· 扩散模型：UnetLoaderGGUF、UNETLoader\n\n"
                "在画布上打开 H3 之类的工作流后再点一次就行。")

    wv.eval_js(_JS_SYNC.replace("__PARAMS__",
                                _js.dumps(params, ensure_ascii=False)), done)
    return True


# ============ 主界面 → 画布网关（:8190）：不点画布节点也能出图/出视频 ============
# 无限画布自己就是「OpenAI 兼容客户端 + 本地网关」，接口全公开，所以左边参数
# 可以直接打过去 —— 产物和画布出的落在同一个 output。省得为了跑一次工作流
# 还得在画布上手点节点、找参数框。
GW_URL = "http://127.0.0.1:8190"
GW_IMAGE_MODEL = "image-z-image-turbo"
GW_VIDEO_MODEL = "video-h3-t2v"


def _gw_req(path, payload=None, timeout=60, raw=False):
    import urllib.request
    import json as _js
    url = GW_URL + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=_js.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else _js.loads(data.decode("utf-8"))


def _gw_params():
    """把左侧当前参数整理成网关要的形状（按第 1 镜来）。"""
    res, steps = SPEED_PRESETS.get(speed_var.get(), ((448, 800), 4))
    landscape = ASPECTS.get(aspect_var.get(), "portrait") == "landscape"
    w, h = (res[1], res[0]) if landscape else res
    try:
        length = int(length_var.get())
    except Exception:
        length = 22
    prompt = ""
    try:
        shots = collect_shots()
        if shots:
            s = shots[0]
            style = style_var.get().strip()
            szs = (s.get("size") or "").strip()
            suf = ("，" + SIZE_TEXT[szs]) if (szs in SIZE_TEXT
                                              and not auto_prompt_var.get()) else ""
            prompt = (_auto_prompt(s["scene"], style, size=szs,
                                   direct=s.get("direct", ""))
                      if auto_prompt_var.get() else (s["scene"] + suf + style))
    except Exception:
        prompt = ""
    sec = max(4, min(10, int(round(length / 24.0))))     # H3 按 24fps 出片
    return {"prompt": prompt, "width": int(w), "height": int(h),
            "length": int(length), "steps": int(steps), "seconds": sec,
            "size": "%dx%d" % (int(w), int(h)),
            "unet": MODEL_PRESETS.get(model_var.get(), "")}


def _gw_models():
    """网关当前注册到的模型（画布「配置」里没装齐模型的不会出现在这里）。"""
    try:
        d = _gw_req("/v1/models", timeout=8)
        return {m.get("id"): m for m in (d.get("data") or [])}
    except Exception:
        return None


def _gw_fasth3_problem():
    """画布的 H3 极速草稿：静态自检会报「可用」，但运行期还有两道硬门槛。

    实测（2026-11-05）：网关把 video-h3-fast 标成 available，提交后 ComfyUI 才在
    FastH3OptimizedLoader 里抛 "B1 requires PyTorch CUDA 13.0+; found 12.6"，
    而且这台机器连所有预设共用的 fastvideo_fasth3_* 权重都没装。
    与其让用户白跑一次任务，不如在提交前把话说清楚。
    """
    import glob
    roots = []
    for r in (getattr(MM, "MODELS_ROOT", ""), getattr(AP, "WORKDIR", "")):
        if r:
            roots.append(r)
            roots.append(os.path.join(r, "models"))
    hit = []
    for r in roots:
        for pat in ("*fastvideo*", "*fasth3*"):
            try:
                hit += glob.glob(os.path.join(r, "**", pat), recursive=True)
            except Exception:
                pass
    if hit:
        return ""
    return ("画布的「H3 极速草稿」静态自检说可用，但这台机器真跑不了：\n\n"
            "· 缺 FastH3 专用权重（fastvideo_fasth3_*），所有预设都加载不到；\n"
            "· 它的 B1 预设还要求 PyTorch CUDA 13.0+，本机 ComfyUI 是 cu126。\n\n"
            "别在画布里白提交一次任务。要出视频用主界面「▶ 开始生成」走 r2v 路线；"
            "画布这边点「▶ 出图」可以用 z-image-turbo 出分镜关键帧。")


def _gw_generate(kind="image"):
    p = _gw_params()
    if not (p["prompt"] or "").strip():
        messagebox.showinfo("第 1 镜还没写画面描述",
                            "先把第 1 镜的「画面描述」写上，再让网关出图/出视频。")
        return
    models = _gw_models()
    if models is None:
        messagebox.showinfo("连不上网关",
                            "网关 :8190 没起来。先把右侧面板展开，让无限画布服务启动，再点。")
        return
    # 视频模型不能写死：原常量 GW_VIDEO_MODEL="video-h3-t2v" 早就不在网关里，
    # 结果这个按钮永远弹「工作流不可用」。改成从网关当前扫描结果里挑可用的。
    want = GW_IMAGE_MODEL
    if kind == "video":
        vids = []
        try:
            st = (_gw_req("/api/setup/status", timeout=8).get("status") or {})
            vids = [w.get("id") for w in (st.get("workflows") or [])
                    if w.get("available") and w.get("type") == "video"]
        except Exception:
            pass
        if not vids:
            vids = [m for m in (models or {}) if str(m).startswith("video-")]
        want = next((v for v in vids if v in models), "")
    if not want or want not in models:
        avail = "、".join(models.keys()) or "（一个也没有）"
        messagebox.showinfo(
            "这个工作流现在不可用",
            "网关里没有%s的视频/图片工作流。\n\n当前可见：%s\n\n"
            "无限画布注册了十几个工作流，但只有模型装齐的才会出现在这里；"
            "画布右上角「配置」里能看到各自缺什么。"
            % ("" if kind == "image" else "可用", avail))
        return
    if kind == "video" and want == "video-h3-fast":
        why = _gw_fasth3_problem()
        if why:
            messagebox.showinfo("画布视频工作流跑不了", why)
            return
    log_q.put("[网关] 提交%s · %s · %s · %s\n"
              % ("视频" if kind == "video" else "图片", want, p["size"], p["prompt"][:50]))
    threading.Thread(target=_gw_worker, args=(kind, p, want), daemon=True).start()


def _gw_worker(kind, p, model=""):
    import base64
    stamp = time.strftime("%H%M%S")
    try:
        if kind == "image":
            body = {"model": model or GW_IMAGE_MODEL, "prompt": p["prompt"],
                    "size": p["size"], "n": 1, "response_format": "b64_json"}
            r = _gw_req("/v1/images/generations", body, timeout=1800)
            items = r.get("data") or []
            if not items:
                raise RuntimeError("网关没返回图片")
            out = os.path.join(OUTDIR, "gw_img_%s.png" % stamp)
            with open(out, "wb") as f:
                f.write(base64.b64decode(items[0]["b64_json"]))
            log_q.put("[网关] 出图完成 → %s\n" % out)
            root.after(0, lambda: set_status("网关出图完成", "green"))
            return
        body = {"model": model or GW_VIDEO_MODEL, "prompt": p["prompt"],
                "size": p["size"], "seconds": p["seconds"]}
        r = _gw_req("/v1/videos", body, timeout=180)
        vid = r.get("id")
        if not vid:
            raise RuntimeError("网关没返回任务 id：%r" % (r,))
        log_q.put("[网关] 视频任务 %s 已提交（%d 秒），等结果…\n" % (vid, p["seconds"]))
        st = {}
        for i in range(400):
            time.sleep(3)
            st = _gw_req("/v1/videos/%s" % vid, timeout=30)
            s = str(st.get("status") or "").lower()
            if s in ("completed", "succeeded", "success", "done"):
                break
            if s in ("failed", "error", "canceled", "cancelled"):
                raise RuntimeError("任务失败：%s" % (st.get("error") or s))
            if i % 10 == 0:
                log_q.put("[网关] %s %s%%\n" % (s or "running", st.get("progress") or ""))
        else:
            raise RuntimeError("等太久了，任务 %s 还在跑" % vid)
        data = _gw_req("/v1/videos/%s/content" % vid, timeout=300, raw=True)
        out = os.path.join(OUTDIR, "gw_vid_%s.mp4" % stamp)
        with open(out, "wb") as f:
            f.write(data)
        log_q.put("[网关] 视频完成 → %s\n" % out)
        root.after(0, lambda: set_status("网关出视频完成", "green"))
    except Exception as e:
        msg = str(e)
        try:
            import urllib.error
            if isinstance(e, urllib.error.HTTPError):
                msg = e.read().decode("utf-8", "ignore")[:300]
            elif isinstance(e, urllib.error.URLError):
                msg = "连不上网关 :8190 —— 先把右侧面板展开，让画布服务起来"
        except Exception:
            pass
        log_q.put("[网关] 失败：%s\n" % msg)
        root.after(0, lambda: set_status("网关任务失败", "red"))


# ============ 主界面 → 无限画布（:3000）的节点 ============
# 画布前端没暴露任何 API（没有 window.store，也没有 postMessage 通道），
# 数据存在 IndexedDB(infinite-canvas / app_state / infinite-canvas:canvas_store)。
# 所以这里直接改那份持久化数据、再让页面重载 —— 稳，代价是画面会刷新一下。
_PUSH_JS = r"""
(function(){
  var P = __P__;
  window.__pushResult = null;
  (async function(){
    try{
      var db = await new Promise(function(res, rej){
        var r = indexedDB.open("infinite-canvas");
        r.onsuccess = function(){ res(r.result); };
        r.onerror = function(){ rej(r.error); };
      });
      var st = db.transaction("app_state","readwrite").objectStore("app_state");
      var raw = await new Promise(function(res, rej){
        var r = st.get("infinite-canvas:canvas_store");
        r.onsuccess = function(){ res(r.result); };
        r.onerror = function(){ rej(r.error); };
      });
      var data = raw ? ((typeof raw === "string") ? JSON.parse(raw) : raw)
                     : {state:{projects:[]}, version:0};
      data.state = data.state || {};
      var projects = data.state.projects || [];
      if (!projects.length){
        var now = new Date().toISOString();
        projects.push({id:"manju-"+Date.now(), title:P.title||"\u6f2b\u5267\u5206\u955c",
          createdAt:now, updatedAt:now, nodes:[], connections:[], chatSessions:[],
          activeChatId:null, backgroundMode:"lines", showImageInfo:false,
          viewport:{x:0,y:0,k:1}});
        data.state.projects = projects;
      }
      // 旧版把 image/video 等内置类型全跳过了，只写插件节点；新版画布的生成节点
      // 恰恰就是内置 image/video，结果永远 hits=0。现在按「是不是生成类」判断，
      // 并且画布空的时候直接按主界面的分镜建节点，不再要求用户先手动拖。
      var shots = Array.isArray(P.shots) ? P.shots : [P];
      var proj = projects[0];
      proj.nodes = proj.nodes || [];
      function isGen(n){
        var t = String((n && n.type) || "");
        return t === "video" || t === "image" || t === "free-card" || t.indexOf(":") >= 0;
      }
      var gens = proj.nodes.filter(isGen);
      var hits = 0, names = [];
      for (var i=0;i<shots.length;i++){
        var s = shots[i] || {}, n;
        if (i < gens.length){
          n = gens[i];
        } else {
          n = {id:"video-"+Date.now()+"-"+i+"-"+Math.random().toString(36).slice(2,7),
               type:"video", title:"", position:{x:80+i*470, y:120},
               width:420, height:236, metadata:{content:"", status:"idle"}};
          proj.nodes.push(n);
        }
        n.metadata = n.metadata || {};
        n.title = s.title || ("\u7b2c"+(i+1)+"\u955c");
        n.metadata.prompt = s.prompt || "";
        n.metadata.size = P.size || s.size || "";
        n.metadata.generationMode = "video";
        if (P.seconds || s.seconds) n.metadata.seconds = String(P.seconds || s.seconds);
        if (s.line) n.metadata.content = s.line;
        hits++; names.push(n.title);
      }
      proj.updatedAt = new Date().toISOString();
      await new Promise(function(res, rej){
        var r = st.put(JSON.stringify(data), "infinite-canvas:canvas_store");
        r.onsuccess = function(){ res(); };
        r.onerror = function(){ rej(r.error); };
      });
      window.__pushResult = {ok:true, hits:hits, names:names.slice(0, 6), created:gens.length===0};
    }catch(e){ window.__pushResult = {ok:false, msg:String(e)}; }
  })();
  return "started";
})()
"""


def _canvas_eval_once(cwv, js, key, on_done, tries=25):
    """跑一段 JS，然后轮询 window[key]，取到结果后回主线程调 on_done。"""
    import json as _js
    box = {}

    def tick(n=0):
        cwv.eval_js("JSON.stringify(window.%s||null)" % key,
                    lambda v, e: box.update(v=v))

        def check():
            v = box.get("v")
            if v and v != "null":
                try:
                    d = _js.loads(v)
                except Exception:
                    d = None
                if d is not None:
                    on_done(d)
                    return
            if n < tries:
                tick(n + 1)
            else:
                on_done(None)
        root.after(130, check)

    cwv.eval_js(js, lambda v, e: None)
    tick()


def _canvas_push_params():
    """把左侧参数写进无限画布上的生成节点（写的是持久化数据，写完页面重载）。"""
    import json as _js
    # JS 是注入到画布 WebView2 的页面里，跟「哪块面板当前可见」无关。
    # 以前这里查 _web["open"]、后来改查 _canvas["open"]，两种视口下都会误拦；
    # 只要 WebView2 就绪就让它推。
    cwv = _canvas.get("wv")
    if not cwv or not cwv.ready:
        messagebox.showinfo("画布还没就绪", "等无限画布加载出来再点。")
        return
    p = _gw_params()
    # 以前只推第 1 镜；现在把整部剧的分镜按顺序推成画布上的一排节点，
    # 主界面写什么，画布里就能看到什么（空画布会自动建项目/节点）。
    try:
        _raw = collect_shots() or []
    except Exception:
        _raw = []
    _style = (style_var.get() or "").strip()
    items = []
    for _i, _s in enumerate(_raw, 1):
        _scene = (_s.get("scene") or "").strip()
        _line = ""
        for _k in ("line", "say", "dialog", "text"):
            _line = (_s.get(_k) or "").strip()
            if _line:
                break
        _p = _scene + (("，" + _style) if (_style and _scene) else "")
        items.append({"title": "第%d镜" % _i, "prompt": _p.strip("，"),
                      "line": _line})
    if not items:
        items = [{"title": "第1镜", "prompt": p["prompt"], "line": ""}]
    if not any((x["prompt"] or "").strip() for x in items):
        messagebox.showinfo("还没写画面描述", "先在第 1 镜写一句画面描述。")
        return
    payload = {"title": (title_var.get() or "漫剧分镜").strip(),
               "size": p["size"], "seconds": p["seconds"],
               "video": True, "shots": items}
    js = _PUSH_JS.replace("__P__", _js.dumps(payload, ensure_ascii=False))

    def done(d):
        if d is None:
            log_q.put("[画布] 没拿到回执，可能页面正在重载，稍后看一眼画布\n")
            return
        if d.get("ok"):
            log_q.put("[画布] 已写入 %d 个节点：%s\n"
                      % (d.get("hits", 0), "、".join(d.get("names") or [])[:80]))
            log_q.put("[画布] 正在刷新画布让新参数生效…\n")
            try:
                cwv.navigate(CANVAS_URL)
            except Exception:
                pass
        else:
            msg = d.get("msg") or "未知原因"
            log_q.put("[画布] 没写进去：%s\n" % msg)
            if "项目" in str(msg):
                log_q.put("[画布] 现在的画布是空的。先在画布上点「开始使用」建一块，"
                          "把要用的生成节点拖进去，再点「⇢ 推给无限画布」。\n")

    _canvas_eval_once(cwv, js, "__pushResult", done)


# 首次运行预填示例；之后从空白开始（不想要示例的人不用每次手删）
if AP.CFG.get("prefilled_done"):
    add_shot()
else:
    for sc, sa in EXAMPLE:
        add_shot(sc, sa, defer=True)
    _shots_refresh()
    # 记一笔：以后启动就从空白开始，不再强行塞示例
    try:
        _c = dict(AP.CFG)
        _c["prefilled_done"] = True
        AP.save_config(_c)
        AP.CFG["prefilled_done"] = True
    except Exception:
        pass


def _startup_checks():
    """启动自检：对齐模型路径 → 缺模型就提示（可一键自动下载）。"""
    if MM.ensure_extra_yaml():
        log_q.put("[环境] extra_model_paths.yaml 已按当前目录改写，模型路径已对齐。\n")
    s = MM.summary()
    log_q.put("[环境] 模型目录：%s\n" % MM.MODELS_ROOT)

    if "--ui-selftest" in sys.argv:      # 自检时不弹模态框
        return
    if s["ready"]:
        log_q.put("[环境] 模型齐全 ✅\n")
        return
    miss = s["need"]
    log_q.put("[环境] 缺 %d 项模型，约 %.1f GB —— 点右上角「📦 模型」可下载。\n"
              % (len(miss), s["need_mb"] / 1024.0))
    detail = "\n".join("· " + e["desc"] for e in miss[:6])
    msg = ("这台电脑上还缺 %d 个模型，约 %.1f GB：\n\n%s\n\n"
           "注意：模型不打包在 exe 里（整套约 20GB，塞不进 70MB 的程序）。\n\n"
           "· 点「是」：打开模型管理，可一键自动下载（国内镜像，支持断点续传）\n"
           "· 模型已经在别的盘：点「否」，再用「📁 换个模型目录」指过去，不用重下"
           % (len(miss), s["need_mb"] / 1024.0, detail))
    if messagebox.askyesno("缺少模型（exe 里不带模型）", msg):
        open_models(auto=True)


if "--ue-progdemo" in sys.argv:
    # 只为验证「节点图能跟着进度走」：假造一条进度，不碰显卡、不提交任何任务
    def _ue_progdemo():
        try:
            _wipe_shots()
            load_example()
            _vp_apply("story")
            _GEN_RUNNING[0] = True
            _GEN_T0[0] = time.time() - 754
            import progress as _PG
            _PG.emit("视频", 0.42, "第 2/5 镜 · 4 步")
            _STEP.update(cur=2, total=4, at=time.time(), avg=180.0)
            _story_refresh()
        except Exception as _e:
            _crash("progdemo: %r" % (_e,))
    root.after(3500, _ue_progdemo)


if "--ue-probe" in sys.argv:
    def _ue_probe():
        out = []
        try:
            _wipe_shots()
            _filter_var.set("")
            add_shot("雨夜长街，剑客与黑衣人对面而立，中景",
                     "秀兰：这一剑，我等了十年。", size="中景")
            out.append("CHAR_REFS=%r" % (list(CHAR_REFS.items())[:3],))
            out.append("加镜后 ref_var=%r" % (shot_rows[0]["ref_var"].get(),))
            n = _fill_refs_from_line()
            out.append("补齐 %d 镜 → ref_var=%r" % (n, shot_rows[0]["ref_var"].get()))
            cs = collect_shots()
            out.append("collect_shots[0]['ref']=%r" % (cs[0].get("ref"),))
            out.append("collect_shots[0].keys=%r" % (sorted(cs[0].keys()),))
            out.append("aspect_var=%r  _cb_aspect 显示=%r" % (
                aspect_var.get(), _cb_aspect.get()))
            out.append("speed_var=%r 显示=%r" % (speed_var.get(), _cb_speed.get()))
            out.append("style_var=%r preset=%r 显示=%r" % (
                style_var.get(), preset_var.get(), _cb_preset.get()))
            out.append("model_var=%r 显示=%r" % (model_var.get(), model_cb.get()))
        except Exception as e:
            out.append("EXC %r" % (e,))
        _crash("=== PROBE ===\n" + "\n".join(out))
        with open(os.path.join(AP.OUTDIR, "probe.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        try:
            root.destroy()
        except Exception:
            pass
    root.after(2500, _ue_probe)


if "--ue-dlg" in sys.argv:
    # 只为看一眼暗色弹窗长什么样
    root.after(2500, lambda: messagebox.showwarning(
        "有镜头没挂参考角色",
        "「⑥ 参考角色」是必选项，这 1 镜还空着。\n\n"
        "先在左栏点一下那一镜，再到右栏「细节 → ⑥ 参考角色」里选；"
        "纯空镜（画面里没人）就选「无人」。"))


if "--ue-e2e" in sys.argv:
    # 端到端实测：清空 → 建一镜 → 真跑一次生成，全过程写日志（改版后回归用）
    def _ue_e2e():
        try:
            _wipe_shots()
            _filter_var.set("")
            add_shot("雨夜长街，剑客与黑衣人对面而立，中景",
                     "秀兰：这一剑，我等了十年。", size="中景")
            try:
                title_var.set("E2E测试")
            except Exception:
                pass
            _crash("[E2E] 建镜完成，共 %d 镜；开始生成" % len(shot_rows))
            _vp_apply("story")
            root.after(800, generate)
        except Exception as _e:
            _crash("[E2E] 准备失败: %r" % (_e,))

    root.after(4000, _ue_e2e)


if "--ue-visual" in sys.argv:
    # 改版面时用：直接载入示例 5 镜并停在界面上，方便看满数据时的排版
    def _ue_visual():
        try:
            _wipe_shots()
            load_example()
            _select(0)
        except Exception as _e:
            _crash("visual: %r" % (_e,))
    root.after(1600, _ue_visual)


if "--ue-selftest" in sys.argv:
    # UE5 布局改造后的数据链自检：分镜不再是「控件持有数据」，必须验证
    # 增删改 / 自动识别 / 大纲条目 / 取值 全链路都还对。
    def _ue_selftest():
        out = []

        def ck(name, fn):
            try:
                r = fn()
                out.append("OK   %-30s %s" % (name, "" if r is None else r))
            except Exception as e:
                out.append("FAIL %-30s %r" % (name, e))

        def eq(got, want, tag=""):
            if got != want:
                raise AssertionError("%s 期望 %r 实得 %r" % (tag, want, got))
            return got

        ck("清空", lambda: (_wipe_shots(), eq(len(shot_rows), 0)))
        ck("加一镜", lambda: (add_shot("雨夜长街，剑客与黑衣人对面而立，中景",
                                     "林野：这一剑，我等了十年。"),
                            eq(len(shot_rows), 1), eq(_SEL["i"], 0)))
        ck("详情表单已建", lambda: (
            shot_rows[0]["size_cb"] is not None and
            shot_rows[0]["ref_cb"] is not None) or
            (_ for _ in ()).throw(AssertionError("详情控件缺失")))
        ck("自动识别景别", lambda: eq(shot_rows[0]["size_var"].get(), "中景"))
        ck("自动识别场景", lambda: shot_rows[0]["loc_var"].get() or "（空）")
        ck("自动识别运镜", lambda: shot_rows[0]["motion_var"].get()[:6] or "（空）")
        ck("台词→时长", lambda: eq(shot_rows[0]["dur_var"].get(), "5.2"))

        def _edit_scene():
            rec = shot_rows[0]
            rec["scene"].set("破庙里，少女举剑挡在门前，特写")
            rec["auto_size"]()
            rec["auto_motion"]()
            rec["refresh_loc"]()
            _outline_touch(0)
            eq(rec["size_var"].get(), "特写")
            return "改描述 → 景别=%s 场景=%s" % (rec["size_var"].get(),
                                                rec["loc_var"].get())
        ck("改描述重识别", _edit_scene)

        ck("复制一镜", lambda: (_dup_shot(0), eq(len(shot_rows), 2)))
        ck("上移下移", lambda: (move_shot(1), eq(_SEL["i"], 1)))
        ck("删除一镜", lambda: (del_shot_selected(), eq(len(shot_rows), 1)))
        ck("取值不丢", lambda: (collect_shots()[0]["scene"].startswith("破庙里"))
           or (_ for _ in ()).throw(AssertionError(
               "实得 %r" % collect_shots()[0]["scene"])))

        def _outline_count():
            n = sum(1 for r in shot_rows if r.get("frame") is not None)
            eq(n, len(shot_rows))
            return "%d 条" % n
        ck("大纲条目数", _outline_count)

        def _filter():
            _filter_var.set("不存在的关键字zzz")
            _outline_refresh()
            n = sum(1 for r in shot_rows if r.get("frame") is not None)
            eq(n, 0)
            _filter_var.set("")
            _outline_refresh()
            return "过滤生效"
        ck("大纲过滤", _filter)

        ck("载入示例", lambda: (_wipe_shots(), load_example(),
                             eq(len(shot_rows), len(EXAMPLE)),
                             "%d 镜" % len(shot_rows)))
        ck("角色头像条", lambda: (_update_char_stat(), "ok"))
        ck("补参考角色", lambda: (_fill_refs_from_line(), "ok"))
        ck("一键重识别", lambda: ("%d 镜" % len(shot_rows), _reidentify_all())[0])
        ck("清空", lambda: (clear_shots(False), eq(len(shot_rows), 1)))
        ck("主题", lambda: eq(TH.current_key(), "unreal"))
        ck("三栏宽度", lambda: "%d/%d/%d 底=%d" % (
            _ue_left.winfo_width(), _ue_center.winfo_width(),
            _ue_right.winfo_width(), _ue_bottom.winfo_height()))

        txt = "\n".join(out)
        try:
            os.makedirs(AP.OUTDIR, exist_ok=True)
            with open(os.path.join(AP.OUTDIR, "ue_selftest.log"), "w",
                      encoding="utf-8") as f:
                f.write(txt + "\n")
        except Exception:
            pass
        _crash("=== UE 布局自检 ===\n" + txt)

    root.after(1200, _ue_selftest)


# 启动后默认选中第一镜，右栏「细节」直接显示它的表单 ——
# UE5 里打开编辑器默认就有一个选中对象，不然右栏是一片空白（用户会以为坏了）。
def _startup_select():
    try:
        _rt_show("detail")
        if shot_rows:
            _select(0)
        else:
            _detail_show(-1)
    except Exception as _e:
        _crash("启动选中失败: %r" % (_e,))


root.after(500, _startup_select)
root.after(700, _sync_all_combos)
if os.environ.get("MANJU_LAYOUT_DEBUG") == "1":
    root.after(3000, _ue_dump_tree)
root.after(1400, _ue_sash_init)

root.after(900, _startup_checks)

# 开机自连后端：程序一起来就自己把后端弄起来（在线就直接绿灯），
# 不再要求用户先点「⚡ 启动后端」再回来点「开始生成」。
# 各种 --xxx-selftest 下不自动起（自检不该拉起十几 G 的后端）。
if not any(a.endswith("-selftest") for a in sys.argv):
    root.after(1500, _auto_connect)

# 主题巡检：12 套配色逐个套到真实界面上截图，确认没有哪套是「配出来很脏」的
if "--theme-selftest" in sys.argv:
    def _theme_selftest():
        from PIL import ImageGrab
        for key, name, desc, light in TH.palette_list():
            try:
                TH.apply(root, key)
                TH.restyle(root)
                try:
                    status_lbl.config(background=TH.PANEL)
                    set_status(status_lbl.cget("text"), _status_kind)
                except Exception:
                    pass
                root.update_idletasks()
                time.sleep(0.05)
                x, y = root.winfo_rootx(), root.winfo_rooty()
                ImageGrab.grab((x, y, x + root.winfo_width(),
                                y + root.winfo_height())).save(
                    os.path.join(AP.OUTDIR, "theme_%s.png" % key))
                print("OK  %-8s %s%s" % (key, name, "（浅色）" if light else ""))
            except Exception as e:
                import traceback
                print("FAIL", key, e)
                print(traceback.format_exc())
        try:
            TH.apply(root, THEME_KEY)
            TH.restyle(root)
        except Exception:
            pass
        root.destroy()

    root.after(1000, _theme_selftest)


if "--left-selftest" in sys.argv:
    # 左侧收起自检：收起 → 截图 → 展开 → 截图，确认布局和窗口宽度都对。
    # （无人值守机器上模拟鼠标点竖条不可靠，这里直接调 _left_toggle）
    def _left_selftest():
        lines = []
        out = os.path.join(AP.OUTDIR, "left_selftest.txt")

        def _write():
            with open(out, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

        def _pump(n=25):
            for _ in range(n):
                try:
                    root.update()
                except Exception:
                    break
                time.sleep(0.04)

        def _grab(tag):
            try:
                from PIL import ImageGrab
                _pump(6)
                x, y = root.winfo_rootx(), root.winfo_rooty()
                ImageGrab.grab((x, y, x + root.winfo_width(),
                                y + root.winfo_height())).save(
                    os.path.join(AP.OUTDIR, "left_%s.png" % tag))
            except Exception as e:
                lines.append("截图失败 %s: %r" % (tag, e))

        try:
            root.lift()
            root.attributes("-topmost", True)
            _pump(10)
            _web_toggle(True)          # 真实展开右侧面板（含 WebView2）
            _pump(200)                 # 等内核和画布挂起来
            _grab("0_open")
            lines.append("open    %dx%d  state=%s 画布=%d ComfyUI=%d 面板=%d"
                         % (root.winfo_width(), root.winfo_height(),
                            root.state(), canvas_host.winfo_width(),
                            web_host.winfo_width(), web_panel.winfo_width()))

            _left_toggle()
            _pump(30)
            _grab("1_closed")
            lines.append("closed  %dx%d state=%s 画布=%d ComfyUI=%d 面板=%d"
                         % (root.winfo_width(), root.winfo_height(),
                            root.state(), canvas_host.winfo_width(),
                            web_host.winfo_width(), web_panel.winfo_width()))

            # 模拟用户把窗口往右拉宽：面板必须自己吃掉多出来的宽度，不能留白板
            try:
                wide = min(root.winfo_width() + 300, root.winfo_screenwidth() - 20)
                root.geometry("%dx%d+%d+%d" % (wide, root.winfo_height(),
                                               root.winfo_x(), root.winfo_y()))
                _pump(40)
                _grab("1b_wide")
                pw = web_panel.winfo_width()
                lw = _ue_sash_get(_ue_row, 0)
                lines.append("wide    %dx%d  面板宽=%d 竖条宽=%d 缝隙=%d"
                             % (root.winfo_width(), root.winfo_height(), pw, lw,
                                web_panel.winfo_x() - (lw + root.winfo_rootx()
                                                       - root.winfo_rootx())))
            except Exception as e:
                lines.append("拉宽测试失败: %r" % (e,))

            _left_toggle()
            _pump(30)
            _grab("2_restored")
            lines.append("restored %dx%d 画布=%d ComfyUI=%d 内宽=%d"
                         % (root.winfo_width(), root.winfo_height(),
                            canvas_host.winfo_width(),
                            web_host.winfo_width(), _CANVAS_INNER_W))

            # 复位按钮：回到默认分界，并且要落盘
            _canvas_apply_w(_CANVAS_INNER_W_DEF)
            _pump(25)
            _grab("3_reset")
            lines.append("reset   %dx%d 画布=%d ComfyUI=%d 存盘=%s"
                         % (root.winfo_width(), root.winfo_height(),
                            canvas_host.winfo_width(),
                            web_host.winfo_width(),
                            _web_read_state().get("canvas_w")))
            lines.append("OK")
        except Exception as e:
            import traceback
            lines.append("FAIL %r" % (e,))
            lines.append(traceback.format_exc())
        try:
            _write()
        except Exception:
            pass
        if "--keep-open" not in sys.argv:
            root.after(200, root.destroy)

    root.after(2500, _left_selftest)


# 探针：读一遍画布(:3000)存在 IndexedDB 里的节点数据，用来确认字段名。
# 只在 --canvas-probe 下跑，产出 output/canvas_probe.json。
_PROBE_JS = r"""
(function(){
  window.__probe = null;
  (async function(){
    try{
      var db = await new Promise(function(res, rej){
        var r = indexedDB.open("infinite-canvas");
        r.onsuccess = function(){ res(r.result); };
        r.onerror = function(){ rej(r.error); };
      });
      var names = [];
      for (var i=0;i<db.objectStoreNames.length;i++) names.push(db.objectStoreNames[i]);
      if (names.indexOf("app_state") < 0){ window.__probe = {error:"no app_state", stores:names}; return; }
      var tx = db.transaction("app_state","readonly");
      var st = tx.objectStore("app_state");
      var keys = await new Promise(function(res, rej){
        var r = st.getAllKeys(); r.onsuccess = function(){ res(r.result); }; r.onerror = function(){ rej(r.error); };
      });
      var raw = await new Promise(function(res, rej){
        var r = st.get("infinite-canvas:canvas_store"); r.onsuccess = function(){ res(r.result); }; r.onerror = function(){ rej(r.error); };
      });
      var data = (typeof raw === "string") ? JSON.parse(raw) : raw;
      var projects = (data && data.state && data.state.projects) || [];
      window.__probe = {
        stores: names, keys: keys, version: data && data.version,
        projects: projects.map(function(p){
          return {id:p.id, title:p.title, count:(p.nodes||[]).length,
            nodes:(p.nodes||[]).map(function(n){
              var m = n.metadata || {};
              return {id:n.id, type:n.type, title:n.title, metaKeys:Object.keys(m),
                      prompt:(m.prompt||"").slice(0,60), model:m.model||"",
                      size:m.size||"", seconds:m.seconds||"", genMode:m.generationMode||"",
                      videoMode:m.videoMode||""};
            })};
        })
      };
    }catch(e){ window.__probe = {error: String(e)}; }
  })();
  return "started";
})()
"""


if "--canvas-probe" in sys.argv:
    def _canvas_probe_run():
        import json as _json      # 这个文件里没有全局 json，只有局部 import
        out = os.path.join(AP.OUTDIR, "canvas_probe.json")
        res = {"stage": "start"}

        def dump():
            txt = ""
            try:
                txt = _json.dumps(res, ensure_ascii=False, indent=2, default=str)
            except Exception as e:
                txt = '{"dump_error": "%r"}' % (e,)
            try:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(txt)
            except Exception:
                pass

        def pump(n=20):
            for _ in range(n):
                try:
                    root.update()
                except Exception:
                    break
                time.sleep(0.05)

        try:
            _web_toggle(True)
            res["stage"] = "toggled"
            dump()
            pump(230)
            res["stage"] = "pumped"
            dump()
            cwv = _canvas.get("wv")
            res["ready"] = bool(cwv and cwv.ready)
            if not (cwv and cwv.ready):
                res["stage"] = "no webview"
                dump()
                root.after(200, root.destroy)
                return
            cwv.eval_js(_PROBE_JS, lambda v, e: None)
            res["stage"] = "js_sent"
            dump()
            box = {}
            for _ in range(30):
                pump(5)
                cwv.eval_js("JSON.stringify(window.__probe||null)",
                            lambda v, e: box.update(v=v, e=e))
                pump(5)
                res["stage"] = "poll"
                res["last"] = str(box.get("v"))[:120]
                dump()
                if box.get("v") and box["v"] != "null":
                    res["probe"] = _json.loads(box["v"])
                    res["stage"] = "ok"
                    break
            else:
                res["stage"] = "timeout"
            dump()
        except Exception as e:
            res["error"] = repr(e)
            dump()
        root.after(300, root.destroy)

    root.after(2500, _canvas_probe_run)


if "--canvas-push" in sys.argv:
    # 自检：空跑一次「推给无限画布」，确认 IndexedDB 通道和回执都正常
    def _canvas_push_run():
        import json as _json
        out = os.path.join(AP.OUTDIR, "canvas_push.json")
        res = {"stage": "start"}

        def dump():
            try:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(_json.dumps(res, ensure_ascii=False, indent=2, default=str))
            except Exception:
                pass

        def pump(n=20):
            for _ in range(n):
                try:
                    root.update()
                except Exception:
                    break
                time.sleep(0.05)

        try:
            _web_toggle(True)
            pump(230)
            cwv = _canvas.get("wv")
            res["ready"] = bool(cwv and cwv.ready)
            if not (cwv and cwv.ready):
                res["stage"] = "no webview"
                dump()
                root.after(200, root.destroy)
                return
            if "--canvas-live" in sys.argv:
                # 带剧本启动 + --canvas-live：推的是真实分镜，不是自检占位。
                # 用来验证「主界面 → 无限画布」一次能把整部剧摆成一排节点。
                _items = []
                for _i, _s in enumerate(collect_shots() or [], 1):
                    _items.append({
                        "title": "第%d镜" % _i,
                        "prompt": (_s.get("scene") or "").strip(),
                        "line": (_s.get("line") or "").strip()})
                payload = {"title": (title_var.get() or "漫剧分镜").strip(),
                           "size": "448x800", "seconds": 5, "video": True,
                           "shots": _items}
            else:
                payload = {"prompt": "自检用提示词：雨夜长街",
                           "size": "448x800", "seconds": 5, "video": False}
            res["payload"] = {
                "shots": len(payload.get("shots") or [1]),
                "titles": [x["title"] for x in (payload.get("shots") or [])][:6]}
            js = _PUSH_JS.replace("__P__", _json.dumps(payload, ensure_ascii=False))
            cwv.eval_js(js, lambda v, e: None)
            box = {}
            for _ in range(25):
                pump(5)
                cwv.eval_js("JSON.stringify(window.__pushResult||null)",
                            lambda v, e: box.update(v=v))
                pump(5)
                if box.get("v") and box["v"] != "null":
                    res["result"] = _json.loads(box["v"])
                    res["stage"] = "ok"
                    break
            else:
                res["stage"] = "timeout"
            dump()
        except Exception as e:
            res["error"] = repr(e)
            dump()
        root.after(300, root.destroy)

    root.after(2500, _canvas_push_run)


if "--demo-wuxia" in sys.argv:
    if "--resume" in sys.argv:
        _GEN_RESUME[0] = True
    # 自带一段三镜武侠，载入界面后直接点「开始生成」——用来端到端验证，
    # 不用手填表（界面上跑的就是同一条 generate() → M.run()）。
    _DEMO_SHOTS = [
        ("全景镜头，雨夜长街，年轻剑客提剑独行，雨水顺着剑鞘往下淌，"
         "两侧店铺门板斑驳，灯笼在风里晃", "十年了，我回来了。", "长街", "全景"),
        ("中景镜头，破庙里烛火摇曳，剑客与黑衣人对面而立，"
         "雨水从破窗斜着飘进来，地上积着水洼", "你欠我一条命。", "破庙", "中景"),
        ("近景镜头，剑光乍起，雨珠停在半空，剑客已收剑入鞘，"
         "脸上没有表情", "今日，两清。", "破庙", "近景"),
    ]

    def _demo_wuxia():
        try:
            for _r in list(shot_rows):
                del_shot_rec(_r, defer=True)
            shot_rows.clear()
            title_var.set("雨夜断剑")
            # 角色/场景都从当前库里取，别硬编码名字 —— 源码目录和 dist 目录的
            # config.json 是两份，角色库并不一样（源码那份就没有"林野"），
            # 写死会导致生成前被"参考角色是必选项"直接拦下。
            _hero = next(iter(CHAR_REFS.keys()), "")
            for _sc, _sa, _loc, _sz in _DEMO_SHOTS:
                add_shot(_sc, _sa, ref=_hero, size=_sz, defer=True)
                _row = shot_rows[-1]
                if _loc in SCENE_REFS:
                    try:
                        _row["loc_var"].set(_loc)
                        _row["loc_manual"][0] = True
                    except Exception:
                        pass
            _shots_refresh()
            # 只有显式带 --demo-run 才自动开跑：以前一带 --demo-wuxia 就自己开始生成，
            # 用户只是想打开看看也会被"自动跑起来"，很吓人。
            if "--demo-run" not in sys.argv:
                log_q.put("[演示] 剧本已载入，没有自动开跑（要自动跑请加 --demo-run）\n")
                return
            log_q.put("[演示] 角色库=%s；每镜参考=%s\n"
                      % (list(CHAR_REFS.keys()),
                         [r["ref_var"].get() for r in shot_rows]))
            log_q.put("[演示] 已载入《雨夜断剑》3 镜，2.5 秒后开跑\n")
            root.after(2500, generate)
        except Exception as _e:
            _crash("demo 剧本载入失败: %r" % (_e,))

    root.after(3000, _demo_wuxia)


if "--ui-selftest" in sys.argv:
    # 界面自检：依次打开各窗口，确认没有 Tcl/Tk 异常后退出（用于打包后验证）
    def _ui_selftest():
        lines = []
        for fn, name in ((open_models, "模型管理"), (open_voice_preview, "旁白试听"),
                         (open_story_help, "剧情引导"), (open_theme_picker, "主题配色"),
                         (open_settings, "设置"), (open_help, "说明"),
                         (open_chars, "角色定妆照"), (open_scenes, "场景照")):
            try:
                fn()
                root.update()
                lines.append("OK   %s" % name)
            except Exception as e:
                import traceback
                lines.append("FAIL %s: %s" % (name, e))
                lines.append(traceback.format_exc())
            for w in list(root.winfo_children()):
                if isinstance(w, tk.Toplevel):
                    try:
                        w.lift()
                        w.attributes("-topmost", True)
                        w.update()
                        root.update()
                        lines.append("     %s 位置 %d,%d 尺寸 %dx%d" %
                                     (name, w.winfo_rootx(), w.winfo_rooty(),
                                      w.winfo_width(), w.winfo_height()))
                        from PIL import ImageGrab
                        x, y = w.winfo_rootx(), w.winfo_rooty()
                        ImageGrab.grab((x, y, x + w.winfo_width(),
                                        y + w.winfo_height())).save(
                            os.path.join(AP.OUTDIR, "ui_%s.png" % name))
                    except Exception as e:
                        lines.append("  (截图失败 %s)" % e)
                    w.destroy()
            root.update()
        try:
            root.lift()
            root.attributes("-topmost", True)
            root.update()
            from PIL import ImageGrab
            ImageGrab.grab((root.winfo_rootx(), root.winfo_rooty(),
                            root.winfo_rootx() + root.winfo_width(),
                            root.winfo_rooty() + root.winfo_height())).save(
                os.path.join(AP.OUTDIR, "ui_主窗口.png"))
        except Exception as e:
            lines.append("(主窗口截图失败 %s)" % e)
        # 右键菜单自检：临时拦下 tk_popup 只记录菜单项（真弹出会卡住事件循环）
        try:
            popped = []
            _orig_popup = tk.Menu.tk_popup

            def _fake_popup(self, x, y, entry=None):
                popped.append([self.entrycget(i, "label")
                               for i in range(self.index("end") + 1)
                               if self.type(i) != "separator"])

            tk.Menu.tk_popup = _fake_popup
            _tmp = tk.Text(root, height=1)
            _tmp.pack()
            _tmp.insert("1.0", "右键自检")
            root.update()
            _tmp.event_generate("<Button-3>", x=6, y=6)
            root.update()
            _tmp.destroy()
            tk.Menu.tk_popup = _orig_popup
            lines.append("右键菜单：%s" % (popped or "❌ 没弹出来"))
        except Exception as e:
            lines.append("右键菜单自检异常：%s" % e)
        lines.append("屏幕：%dx%d   主窗口：%dx%d+%d+%d"
                     % (root.winfo_screenwidth(), root.winfo_screenheight(),
                        root.winfo_width(), root.winfo_height(),
                        root.winfo_rootx(), root.winfo_rooty()))
        lines.append("主窗口控件数：%d" % len(root.winfo_children()))
        for ch in root.winfo_children():
            lines.append("  %-22s 高=%3d 需求=%3d 位置y=%4d"
                         % (str(ch.winfo_class()) + ":" + str(ch.winfo_name()),
                            ch.winfo_height(), ch.winfo_reqheight(),
                            ch.winfo_y()))
        lines.append("模型下拉项：%s" % list(MODEL_PRESETS.keys()))
        txt = "\n".join(lines)
        open(os.path.join(AP.OUTDIR, "ui_selftest.log"), "w",
             encoding="utf-8").write(txt + "\n")
        print(txt)
        root.destroy()

    root.after(1200, _ui_selftest)


if "--calib-selftest" in sys.argv:
    """换新剧后跑一次：把所有生效参数和校准资产打出来，缺什么当场报。

    用法：漫剧工作室.exe --calib-selftest [一张测试图]
    """
    def _calib_selftest():
        lines = ["== 校准自检 %s ==" % time.strftime("%Y-%m-%d %H:%M")]
        try:
            import make_drama as _M
            import gen_fl2v as _F
        except Exception as e:
            lines.append("❌ 引擎加载失败：%s" % e)
            _write(lines); return

        # ① 动作量阈值（换剧不用动：绝对下限 + 本片中位数自适应）
        lines.append("① 动作量阈值：动作≥%.1f / 可能≥%.1f / 静态≥%.1f / 凝固线%.1f"
                     % (_M.MOTION_ACTION_MIN, _M.MOTION_MAYBE_MIN, 0.9,
                        _M.MOTION_HARD_STILL))
        # ② 帧数边界（H3 训练范围，写死的物理边界，不随剧变）
        lines.append("② 帧数边界：%d~%d 帧（%.1f~%.1f 秒，H3 训练范围）"
                     % (_M.NATIVE_MIN_FRAMES, _M.NATIVE_MAX_FRAMES,
                        _M.NATIVE_MIN_FRAMES/24.0, _M.NATIVE_MAX_FRAMES/24.0))
        # ③ 候选数 / 重跑次数
        try:
            lines.append("③ 关键帧候选 %d 张、动作不达标重跑 %d 次"
                         % (_M.KEY_CANDIDATES, _M._R2V_RETRY))
        except Exception as e:
            lines.append("③ 候选/重跑读取失败：%s" % e)
        # ④ 校准资产
        def _first_exists(paths):
            for q in paths:
                if q and os.path.exists(q):
                    return q
            return ""

        _assets = [
            ("YuNet 人脸检测", _M._asset_path("face_detection_yunet_2023mar.onnx")),
            ("SDPose 骨架模型", os.path.join(AP.MODELSDIR, "diffusion_models",
                                             "sdpose_wholebody_fp16_full.safetensors")),
            ("RT-DETR 人物检测", os.path.join(AP.MODELSDIR, "diffusion_models",
                                              "rt_detr_v4-x-hgnet_fp16.safetensors")),
            ("SD 标准 VAE", os.path.join(AP.MODELSDIR, "vae",
                                          "vae-ft-mse-840000.safetensors")),
            ("fl2va 底模", _first_exists([
                os.path.join(AP.MODELSDIR, "diffusion_models",
                             "minimax_h3_fl2va_pruned-Q4_K.gguf"),
                os.path.join(AP.MODELSDIR, "unet",
                             "minimax_h3_fl2va_pruned-Q4_K.gguf"),
                os.path.join(AP.MODELSDIR, "diffusion_models",
                             "minimax_h3_fl2va_pruned_int8_convrot.safetensors"),
            ])),
        ]
        lines.append("④ 校准资产：")
        for nm, p2 in _assets:
            ok = p2 and os.path.exists(p2)
            lines.append("   %s %s" % ("✅" if ok else "❌ 缺失", nm))
        # ⑤ 实测：给一张图跑脸检测
        _img = (sys.argv[sys.argv.index("--calib-selftest") + 1]
                if len(sys.argv) > sys.argv.index("--calib-selftest") + 1 else "")
        _probe = _img or os.path.join(AP.OUTDIR, "试镜打斗2_key_1_00001_.png")
        if os.path.exists(_probe):
            b = _M._face_boxes(_probe, maxn=8)
            eng = "YuNet" if _M._YUNET else "Haar（YuNet 不可用，侧脸/小脸会漏）"
            lines.append("⑤ 人脸检测实测：%s → %d 张（引擎 %s）"
                         % (os.path.basename(_probe), len(b), eng))
        # ⑥ 音频噪音线
        lines.append("⑥ 音轨噪音报警线：频谱平坦度 > 0.55")
        # ⑦ 首尾帧参数
        try:
            lines.append("⑦ 骨架链路：SDPose+RT-DETR 已就绪（arm_reach=0.42、"
                         "mask x≥0.55、denoise 0.9 —— 尾帧位置锁死，只改手臂）")
        except Exception:
            pass
        _write(lines)

    def _write(lines):
        txt = "\n".join(lines)
        try:
            open(os.path.join(AP.OUTDIR, "calib_selftest.log"), "w",
                 encoding="utf-8").write(txt + "\n")
        except Exception:
            pass
        print(txt)
        root.destroy()

    root.after(600, _calib_selftest)

# ============================ 校准资产自动修复 ============================
# 用户要求：光检测有什么用，要能自己修。所以缺什么自动下载什么。
# 源按速度排：modelscope（实测 12~19MB/s）> hf-mirror（1~2MB/s）> github。
# 每项带 min_size 校验：下了一半的残缺文件不算数，会自动重下。
_CALIB_ASSETS = [
    {"name": "YuNet 人脸检测",
     "min_size": 200_000,
     "urls": ["https://github.com/opencv/opencv_zoo/raw/main/models/"
              "face_detection_yunet/face_detection_yunet_2023mar.onnx"]},
    {"name": "SDPose 骨架模型",
     "min_size": 1_500_000_000,
     "dst": ("diffusion_models", "sdpose_wholebody_fp16_full.safetensors"),
     "urls": ["https://modelscope.cn/models/Comfy-Org/SDPose/resolve/master/"
              "checkpoints/sdpose_wholebody_fp16.safetensors"]},
    {"name": "RT-DETR 人物检测",
     "min_size": 100_000_000,
     "dst": ("diffusion_models", "rt_detr_v4-x-hgnet_fp16.safetensors"),
     "urls": ["https://modelscope.cn/models/Comfy-Org/SDPose/resolve/master/"
              "diffusion_models/rt_detr_v4-x-hgnet_fp16.safetensors"]},
    {"name": "SD 标准 VAE（骨架提取用）",
     "min_size": 250_000_000,
     "dst": ("vae", "vae-ft-mse-840000.safetensors"),
     "urls": ["https://hf-mirror.com/stabilityai/sd-vae-ft-mse/resolve/main/"
              "diffusion_pytorch_model.safetensors"]},
]


def _calib_download(item, log):
    """从 urls 逐个尝试，下载到 .part 再原子改名，min_size 校验。"""
    import urllib.request as _ur
    if item.get("dst"):
        sub, fn = item["dst"]
        dst = os.path.join(AP.MODELSDIR, sub, fn)
    else:
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
        dst = os.path.join(base, "assets", "face_detection_yunet_2023mar.onnx")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    for url in item["urls"]:
        try:
            log("   ↓ 从 %s 下载 %s …" % (url.split("/")[2], item["name"]))
            req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _ur.urlopen(req, timeout=90) as r, open(dst + ".part", "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        log("   ↑ %d/%d MB（%.0f%%）"
                            % (done >> 20, total >> 20, done * 100.0 / total))
            if os.path.getsize(dst + ".part") < item["min_size"]:
                log("   ⚠ 文件不完整（%d 字节 < %d），丢弃重试下一个源"
                    % (os.path.getsize(dst + ".part"), item["min_size"]))
                os.remove(dst + ".part")
                continue
            os.replace(dst + ".part", dst)
            log("   ✅ 已修复 -> %s" % dst)
            return True
        except Exception as e:
            log("   ⚠ 该源失败：%s" % str(e)[:120])
            try:
                if os.path.exists(dst + ".part"):
                    os.remove(dst + ".part")
            except Exception:
                pass
    log("   ❌ 所有源都失败，请手动下载（%s）" % item["name"])
    return False


def _calib_repair(lines, log):
    """资产缺失/损坏就自动修复，返回修复后的状态。"""
    import make_drama as _M
    assets = [
        ("YuNet 人脸检测", _M._asset_path("face_detection_yunet_2023mar.onnx"),
         200_000, _CALIB_ASSETS[0]),
        ("SDPose 骨架模型",
         os.path.join(AP.MODELSDIR, "diffusion_models",
                      "sdpose_wholebody_fp16_full.safetensors"),
         1_500_000_000, _CALIB_ASSETS[1]),
        ("RT-DETR 人物检测",
         os.path.join(AP.MODELSDIR, "diffusion_models",
                      "rt_detr_v4-x-hgnet_fp16.safetensors"),
         100_000_000, _CALIB_ASSETS[2]),
        ("SD 标准 VAE", os.path.join(AP.MODELSDIR, "vae",
                                      "vae-ft-mse-840000.safetensors"),
         250_000_000, _CALIB_ASSETS[3]),
    ]
    fixed = 0
    for nm, path, minsz, item in assets:
        ok = path and os.path.exists(path) and os.path.getsize(path) >= minsz
        if ok:
            lines.append("   ✅ %s" % nm)
            continue
        if path and os.path.exists(path):
            lines.append("   ⚠ %s 文件不完整（%d 字节），自动重下…"
                         % (nm, os.path.getsize(path)))
        else:
            lines.append("   ⚠ %s 缺失，自动下载…" % nm)
        if _calib_download(item, log):
            fixed += 1
            lines.append("   ✅ %s 已自动修复" % nm)
        else:
            lines.append("   ❌ %s 修复失败（见上方日志）" % nm)
    return fixed

if "--calib-fix" in sys.argv:
    """校准 + 自动修复：缺什么下什么，下完复检并跑一次完整自检。"""
    def _calib_fix():
        lines = ["== 校准修复 %s ==" % time.strftime("%Y-%m-%d %H:%M")]
        log = lambda t: (lines.append(t), print(t, flush=True))
        log("① 检查校准资产…")
        try:
            import make_drama as _M
        except Exception as e:
            log("❌ 引擎加载失败：%s" % e); _write(lines); return
        fixed = _calib_repair(lines, log)
        log("② 修复了 %d 项，跑完整自检…（结果见 calib_selftest.log）" % fixed)
        _write(lines)
        # 复检：直接调用完整自检
        try:
            _calib_selftest()
        except Exception:
            pass

    root.after(600, _calib_fix)


if "--face-selftest" in sys.argv:
    # 打包后验证"人脸检测器真的带进来了"：给一张图，打印检出几个脸。
    # 只测这个不起来整个界面，命令行几秒出结果。
    def _face_selftest():
        import make_drama as _M
        _img = (sys.argv[sys.argv.index("--face-selftest") + 1]
                if len(sys.argv) > sys.argv.index("--face-selftest") + 1 else "")
        out = ["检测器：%s" % (_M._haar_path() or "（找不到！）")]
        try:
            import cv2
            out.append("cv2：%s" % cv2.__version__)
        except Exception as e:
            out.append("cv2 不可用：%s" % e)
        try:
            import numpy as _np
            out.append("numpy：%s" % _np.__version__)
        except Exception as e:
            out.append("numpy 不可用：%s" % e)
        if _img and os.path.exists(_img):
            out.append("%s → 检出 %d 张脸"
                       % (os.path.basename(_img), len(_M._face_boxes(_img, maxn=8))))
        txt = "\n".join(out)
        open(os.path.join(AP.OUTDIR, "face_selftest.log"), "w",
             encoding="utf-8").write(txt + "\n")
        print(txt)
        root.destroy()

    root.after(600, _face_selftest)

if "--recog-selftest" in sys.argv:
    # 景别/场景自动识别自检：导入一份真实剧本 → 检查每镜的
    # ⑦ 景别、⑧ 场景有没有自动填好，场景文案有没有自动起草。
    # 结果写到 output/recog_selftest.log（打包后也能验证）。
    def _recog_selftest():
        lines = []
        try:
            sp = os.path.join(AP.OUTDIR, "_uidrv", "script.txt")
            if not os.path.exists(sp):
                sp = os.path.join(AP.OUTDIR, "recog_sample.txt")
            raw = open(sp, encoding="utf-8").read()
            parsed = parse_script(raw)
            if not parsed:
                parsed = parse_table(raw)
            lines.append("剧本：%s → 解析 %d 镜" % (os.path.basename(sp), len(parsed)))
            for r in shot_rows[:]:
                del_shot_rec(r, defer=True)
            for p in parsed:
                add_shot(scene=p.get("scene", ""), say=p.get("line", ""),
                         motion=p.get("motion", ""), dur=p.get("dur", ""),
                         sfx=p.get("sfx", ""), ref=p.get("ref", ""),
                         dn=p.get("dn", ""), size=p.get("size", ""), defer=True)
            _shots_refresh()
            _refresh_all_locs()   # 跟真实导入路径一致
            root.update()
            lines.append("")
            lines.append("每镜自动识别结果：")
            try:
                import make_drama as _MD
            except Exception:
                _MD = None
            for i, r in enumerate(shot_rows, 1):
                # 动作档位也打出来：新剧本导入后立刻能看出"这一镜会不会被要求动、
                # 不动会不会自动重跑"。以前只有景别/运镜/场景，动作是盲区。
                _act = ""
                if _MD is not None:
                    try:
                        _nm2, _th2, _cap2 = _MD.motion_need(
                            (r["scene"].get("1.0", "end") or "") + " "
                            + (r["say_var"].get() or ""),
                            r["size_var"].get() or "")
                        _act = "动作=%s≥%.1f/重跑%d" % (_nm2, _th2, _cap2)
                    except Exception:
                        _act = "动作=(判不出)"
                lines.append("  第%d镜 景别=%-6s 运镜=%-8s 场景=%-8s %s | %s | %s | %s"
                             % (i, r["size_var"].get() or "(空)",
                                _motion_short(r["motion_var"].get()),
                                r["loc_var"].get() or "(空)", _act,
                                r["size_hint_lbl"].cget("text"),
                                r["motion_hint_lbl"].cget("text"),
                                r["loc_hint_lbl"].cget("text")))
            lines.append("")
            lines.append("场景候选：%s" % _scene_choices())
            # 自动建场景 + 自动起草文案（不真的出图，后端可能没起）
            names = _auto_scenes_in_play()
            lines.append("本剧场景：%s" % names)
            for n in names:
                d = draft_scene_desc(n)
                SCENE_DESCS[n] = d
                lines.append("  自动起草文案 → %s：%s" % (n, d))
            _save_scene_refs()
            lines.append("")
            lines.append("collect_shots（生成时实际用的值）：")
            for i, s in enumerate(collect_shots(), 1):
                lines.append("  第%d镜 size=%r motion=%r loc=%r"
                             % (i, s["size"], _motion_short(s["motion"]), s["loc"]))
            lines.append("")
            try:
                _f, _m, _n, _sec = _estimate_video(_rows_alive())
                lines.append("成片预估：%d 段 / 成片 %.1f 秒 / %d 帧 / 渲染约 %d 分钟"
                             "（按当前 %s 档＋实测单帧耗时算）"
                             % (_n, _sec, _f, _m, speed_var.get()))
            except Exception as e:
                lines.append("成片预估失败：%s" % e)
            lines.append("")
            lines.append("场景照窗口：")
            try:
                open_scenes()
                root.update()
                lines.append("  OK   场景照")
                for w in list(root.winfo_children()):
                    if isinstance(w, tk.Toplevel):
                        w.destroy()
            except Exception as e:
                import traceback
                lines.append("  FAIL 场景照: %s" % e)
                lines.append(traceback.format_exc())
        except Exception as e:
            import traceback
            lines.append("FAIL: %s" % e)
            lines.append(traceback.format_exc())
        txt = "\n".join(lines)
        open(os.path.join(AP.OUTDIR, "recog_selftest.log"), "w",
             encoding="utf-8").write(txt + "\n")
        print(txt)
        root.destroy()

    root.after(1200, _recog_selftest)

# ---- 尺寸保护（2026-10-29）----
# 主窗口是整条 pack 下来的：日志区 fill="x" 高度写死、分镜区 expand 吸收余量，
# 窗口一缩小 Tk 就把各区一起压扁 —— 分镜行挤成一条、日志只剩两行、长文本被水平裁断
# 还互相重叠。这里按内容自然尺寸锁住最小窗口，拖不小 = 不会显示不全。
def _fit_min_size():
    """最小窗口尺寸。

    旧版是「按内容需求反推」：把所有控件的 reqwidth 加起来，结果工具栏一长就锁成
    1860x698 —— 小屏（1366/1500%缩放）上根本放不下。UE5 布局里三栏都能拖、都能折叠，
    所以直接给一个能看全四区的最小值就够。
    """
    try:
        _w = min(1040, max(880, root.winfo_screenwidth() - 120))
        _h = min(680, max(560, root.winfo_screenheight() - 140))
        root.minsize(_w, _h)
        _crash("最小窗口锁定 %dx%d（屏幕 %dx%d）"
               % (_w, _h, root.winfo_screenwidth(), root.winfo_screenheight()))
    except Exception as _e:
        _crash("设置最小尺寸失败：%s" % _e)


_fit_min_size()
_web["base_min"] = root.minsize()      # 面板展开/收起时按这个换算最小宽度

root.after(200, poll_log)
def _init_backend_status():
    """启动 0.3 秒后刷一次后端状态：放进后台线程，界面一出来就不会被 HTTP 等住。

    原来这里在主线程里连着探了两次（两个三元表达式各调一次），后端没起时每次
    都要等满 3 秒 —— 表现就是窗口刚弹出来就卡住好几秒、状态区像假死。
    """
    def work():
        try:
            ok = check_online()
            _ONLINE[0] = ok
            root.after(0, lambda: set_status(
                "● 后端在线" if ok else "○ 后端未连接",
                "green" if ok else "red"))
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()


root.after(300, _init_backend_status)


# 画布(:3000)预热：上一个版本这里是空的 —— 用户双击 exe、点开画布面板，
# 面板只 navigate 不去拉服务，于是永远停在「127.0.0.1 拒绝连接」。
# 后端本来就在跑的时候（最常见）直接把 launcher 拉起来，画布面板秒进。
def _canvas_prewarm():
    try:
        if check_online(1.5):
            start_canvas_service(quiet=True)
    except Exception as e:
        _crash("画布预热失败: %r" % (e,))


root.after(2000, lambda: threading.Thread(target=_canvas_prewarm,
                                          daemon=True).start())

# 上次拖出来的面板宽度，这次继续用
try:
    _sw = _web_read_state().get("web_w")
    if isinstance(_sw, int) and _WEB_W_MIN <= _sw <= _WEB_W_MAX:
        # 恢复上次宽度时也得上同一把尺子：面板再宽也不能把左边挤到最小宽度以下
        _cap = max(_WEB_W_MIN,
                   root.winfo_screenwidth() - _LEFT_FLOOR - 40)
        _sw = min(_sw, _cap)
        _WEB_W = _sw
        _WEB_W_PREF = _sw
        web_panel.configure(width=_sw)
        _web_tip.configure(wraplength=max(200, _sw - 90))
except Exception:
    pass
# 上次拖好的「画布 / ComfyUI」内部分界也接着用（不然每次启动都回到 780，
# 用户会觉得「没记住我开始调的那样」）
# 视口里「画布 / ComfyUI」的分界现在由 PanedWindow 的分隔条决定（标签栏那三个按钮
# 和拖动手势都改它）。以前这里会把 canvas_host 的 width 写死成上次的值 ——
# 那个宽度会和分隔条打架（实测开局变成 73/27），所以不再恢复。
try:
    _cw = _web_read_state().get("canvas_w")
    if isinstance(_cw, int) and _CANVAS_INNER_W_MIN <= _cw <= _CANVAS_INNER_W_MAX:
        _CANVAS_INNER_W = _cw
except Exception:
    pass
# 开机要不要展开「ComfyUI + 无限画布」两屏：以用户上次勾的为准。
# 之前这里是 `_web_auto.set(1)` + 无条件 `_web_toggle(True)`：
# 用户把「开机自动打开」取消掉，下次开机又把它勾回来、面板照弹 ——
# 取消勾选完全不起作用（用户反馈："我取消了，但是重新打开软件，还是要打开"）。
# 现在：存过就照存的办；从没勾过（新装 / 旧配置没这个键）才默认展开一次并记下来。
_wa_saved = _web_read_state().get("web_auto")
if _wa_saved is None:
    _wa_saved = 1                       # 首次使用：默认展开，省得用户找不到入口
    _web_write_state(web_auto=1)
_web_auto.set(1 if int(_wa_saved) else 0)
if int(_wa_saved):
    # 视口现在由标签栏决定显示方式（分屏 / 独占），这里只负责把 WebView2 内核先拉起来，
    # 否则这行会在 1.5 秒后把用户上次选的「只显示 ComfyUI」硬拉回分屏。
    root.after(1500, _web_ensure)
# 面板没开机展开时，检查一下状态：万一上次是崩溃退出的，别连带把宽高也记坏

# 上次勾了「改了就自动同步」→ 这次继续
if int(_web_read_state().get("web_sync_auto", 0)):
    _web_sync_auto.set(1)
    try:
        _paint_sync_auto()      # 工具条上的小开关要跟着亮
    except Exception:
        pass

# 左边参数一改就同步到画布。防抖 1.5 秒：拖下拉框会连着触发好几次写，
# 不加防抖会把一堆 ExecuteScript 排队打到页面上。
_sync_job = {"id": None}


def _schedule_sync(*_):
    if not _web_sync_auto.get():
        return
    j = _sync_job["id"]
    if j:
        try:
            root.after_cancel(j)
        except Exception:
            pass
    _sync_job["id"] = root.after(1500, lambda: _web_sync_params(auto=True))


for _v in (speed_var, aspect_var, model_var, style_var, length_var,
           auto_prompt_var):
    try:
        _v.trace_add("write", _schedule_sync)
    except Exception:
        pass
# ---------------- 命令行：带剧本文件启动 ----------------
# 用法：漫剧工作室.pyw 剧本.tsv [--run]
#   只给文件 → 打开界面并把分镜载好；再给 --run → 载好后直接开始生成
if "--chars" in sys.argv[1:]:
    root.after(1500, open_chars)      # 想直接看定妆照/三视图，就带 --chars 启动

_cli = [a for a in sys.argv[1:] if not a.startswith("--")]
# 也认 --import 剧本.txt（等价于直接把文件路径写在后面，习惯不同而已）
if "--import" in sys.argv[1:]:
    try:
        _cli = [sys.argv[sys.argv.index("--import") + 1]] + _cli
    except IndexError:
        pass
_cli_run = "--run" in sys.argv[1:]
# 命令行跑片指定画质档：--speed 极速 / --speed 1（序号）
# 不加就只能用默认「高清」，跑一条 40 秒的片子要两三个小时，命令行试用根本等不起。
if "--speed" in sys.argv[1:]:
    try:
        _sp = sys.argv[sys.argv.index("--speed") + 1]
        _hit = ""
        if _sp.isdigit() and 1 <= int(_sp) <= len(_SPEED_KEYS):
            _hit = _SPEED_KEYS[int(_sp) - 1]
        else:
            _hit = next((k for k in _SPEED_KEYS if _sp in k), "")
        if _hit:
            speed_var.set(_hit)
            log_q.put("[CLI] 画质档：%s\n" % _hit)
        else:
            log_q.put("[CLI] 没有这个画质档「%s」（极速档已下线），"
                      "继续用默认 %s\n" % (_sp, speed_var.get()))
    except Exception as _e:
        log_q.put("[CLI] --speed 解析失败：%s\n" % _e)
# 相对路径兜底：exe 起来后工作目录可能不是用户敲命令时的目录，
# 直接把 "../剧本.txt" 喂给 os.path.exists 会判不存在 → 明明给了文件却什么都不发生。
if _cli and not os.path.exists(_cli[0]):
    for _cand in (os.path.abspath(_cli[0]),
                  os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])),
                               _cli[0]),
                  os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])),
                               "..", _cli[0])):
        if os.path.exists(_cand):
            _cli[0] = os.path.normpath(_cand)
            break

if _cli and os.path.exists(_cli[0]):
    def _cli_boot():
        try:
            # 不传剧名 → 由 import_from_file 按文件名自动起（鞋_9镜_对白版 → 鞋）
            _n = import_from_file(_cli[0])
            log_q.put("[CLI] 已载入 %d 镜：%s\n" % (_n, _cli[0]))
            if _cli_run and _n:
                root.after(1500, generate)
        except Exception as e:
            log_q.put("[CLI] 载入失败：%s\n" % e)
    root.after(900, _cli_boot)


# 自检：自动展开网页面板并截图，用来确认嵌入在真机上真的画出来了
if "--web-selftest" in sys.argv:
    def _webtest_shot():
        wv = _web["wv"]
        try:
            from PIL import ImageGrab
            x, y = root.winfo_rootx(), root.winfo_rooty()
            ImageGrab.grab((x, y, x + root.winfo_width(),
                            y + root.winfo_height())).save(
                os.path.join(AP.OUTDIR, "web_selftest.png"))
            _crash("网页面板自检 截图OK ready=%s error=%s "
                   "面板宽=%d 网页区=%dx%d 窗口=%dx%d "
                   "工具栏 高=%d 子控件=%d"
                   % (wv.ready if wv else None, wv.error if wv else "",
                      web_panel.winfo_width(), web_host.winfo_width(),
                      web_host.winfo_height(),
                      root.winfo_width(), root.winfo_height(),
                      web_bar.winfo_height(), len(web_bar.winfo_children())))
            _crash("    实际下发给 WebView2 的 Bounds=%s  "
                   "网页区在屏上的绝对位置 x=%d y=%d"
                   % (getattr(wv, "_last", None),
                      web_host.winfo_rootx(), web_host.winfo_rooty()))
        except Exception as _e:
            _crash("网页面板自检 截图失败 %r" % (_e,))
        root.destroy()

    root.after(1200, lambda: _web_toggle(True))
    root.after(10000, _webtest_shot)


# 自检：验证 ExecuteScript 这条通路在真机上真的能跑通（左右打通的前提）
# 先看页面里有没有 ComfyUI 的 app / 画布节点，再真的调一次参数同步。
if "--sync-selftest" in sys.argv:
    def _sync_test():
        wv = _web["wv"]
        try:
            _crash("同步自检 内核=%s 错误=%s"
                   % (wv.ready if wv else None, wv.error if wv else ""))
            probe = ("(function(){ try{"
                     "var a = window.app || (window.comfyAPI && window.comfyAPI.app);"
                     "var n = (a && a.graph && a.graph.nodes) ? a.graph.nodes : [];"
                     "return JSON.stringify({ok:true, ping:1+1, hasApp:!!a,"
                     " nodes:n.length, types:n.slice(0,40).map(function(x){return x.type})"
                     "});}catch(e){ return JSON.stringify({ok:false, msg:String(e)}); } })()")

            def _done(val, err):
                _crash("同步自检 ExecuteScript 返回 err=%s val=%s"
                       % (err, (val or "")[:700]))
                try:
                    _web_sync_params(auto=False)
                except Exception as e:
                    _crash("同步自检 调用同步异常 %r" % (e,))
                try:
                    _web_help()          # 顺手验证帮助窗口不会炸
                    _crash("同步自检 帮助窗口 OK，子窗口数=%d"
                           % len([w for w in root.winfo_children()
                                  if isinstance(w, tk.Toplevel)]))
                except Exception as e:
                    _crash("同步自检 帮助窗口异常 %r" % (e,))
                root.after(7000, root.destroy)

            wv.eval_js(probe, _done)
        except Exception as e:
            _crash("同步自检 异常 %r" % (e,))
            root.after(500, root.destroy)

    root.after(1200, lambda: _web_toggle(True))
    root.after(11000, _sync_test)


# 最小化时要不要把 WebView2 藏起来（config: wm_hide_webview，默认开）。
# 留着这个开关是为了 A/B：有些机器上"藏起来"反而让还原那一帧重建表面、黑得更明显。
try:
    # 实测：把 WebView2 藏起来再放出来，还原那一帧反而更黑（要重建表面）。
# A/B 数据见 memory：藏=diff 31.6 一帧；不藏=diff 11.6 两帧更轻。默认不藏。
    _HIDE_WV_ON_MIN = bool(AP.CFG.get("wm_hide_webview", False))
except Exception:
    _HIDE_WV_ON_MIN = True


def _webs_visible(on):
    """最小化时把两块网页藏起来，还原时再放出来。

    只靠"还原后补一帧"是不够的：窗口还原的一瞬间，DWM 会先把浏览器那块旧表面
    合上来，黑一下就露出来了。先把 controller 的 IsVisible 关掉（浏览器自己就停画、
    不再维护表面），还原时再打开，它必须重建表面 —— 那一帧黑就没有了。
    """
    for _h in (_canvas.get("wv"), _web.get("wv")):
        try:
            if _h:
                _h.set_visible(bool(on))
        except Exception:
            pass


def _repaint_webs():
    """让两块网页各补一帧 —— 从任务栏还原时用。"""
    for _h in (_canvas.get("wv"), _web.get("wv")):
        try:
            if _h:
                _h.on_restore()
        except Exception:
            pass


def _on_root_map(ev=None):
    """窗口还原（<Map>）：先把浏览器设回可见（最小化时我们把它设成了不可见、
    让它停渲染），再重下边界 + 补一帧白底，避免 DWM 在还原瞬间露一块黑。

    多补几次是因为还原动画期间窗口尺寸还可能变（最大化/还原之间），
    0/60/180ms 三次覆盖了"立即 + 动画结束"两个时点。
    """
    try:
        if ev is not None and ev.widget is not root:
            return
    except Exception:
        pass
    try:
        _webs_visible(True)        # 还原：重新可见、重建表面（底色已钉白，只露白不露黑）
    except Exception:
        pass
    for _d in (0, 60, 180):
        try:
            root.after(_d, _repaint_webs)
        except Exception:
            break


def _on_root_unmap(ev=None):
    """最小化到任务栏：把两块浏览器先设为不可见。

    这样浏览器就停渲染、不再维护那块合成表面；DWM 在还原动画期间不会去合成
    它那个还没就绪的黑色表面，还原时（见 _on_root_map）再打开即可。
    config: wm_hide_webview（默认开，可关来回退到只重边界的方案）。
    """
    try:
        if ev is not None and ev.widget is not root:
            return
        if bool(AP.CFG.get("wm_hide_webview", True)) and str(root.state()) == "iconic":
            _webs_visible(False)
    except Exception:
        pass


root.bind("<Map>", _on_root_map)
root.bind("<Unmap>", _on_root_unmap)
root.protocol("WM_DELETE_WINDOW", _on_app_close)

# AI漫剧工坊：把 WorkBuddy 技能市场里的漫剧/视频 skill 能力内置进主程序
# （动态漫剧合成 / 条漫长图拼接 / AI Hive 云端生成 / 🎬 AI 剧情出片）。
# 做成主界面左侧一根常驻列，与 ComfyUI 画布同时可见；顶栏「🎬 AI漫剧工坊」
# 只控制这根列的显隐，不会把 ComfyUI 换掉。
try:
    # 不再调 manju_studio_ui.attach_menu()：它会给窗口挂一条**原生 Windows 菜单栏**，
    # 里面只有一个「工具」项，在标题栏下面画出一条白条 —— 和自绘的暗色菜单条撞在一起。
    # 工坊入口现在有三处：工具栏「🎬 工坊」、视图菜单、右栏页签。
    import manju_studio_ui
    _ensure_manju_col()
except Exception as _e:
    print("AI漫剧工坊加载失败:", _e, flush=True)


# ===== 临时布局探针（env: MANJU_SMOKE=1，仅用于改版面时量真实几何）=====
if os.environ.get("MANJU_SMOKE") == "1":
    def _smoke_layout():
        try:
            import json as _json
            root.update_idletasks()
            root.update()
            d = {
                "state": str(root.state()),
                "win": [root.winfo_width(), root.winfo_height()],
                "pos": [root.winfo_x(), root.winfo_y()],
                "screen": [root.winfo_screenwidth(), root.winfo_screenheight()],
                "tk_scaling": root.tk.call("tk", "scaling"),
                "left_shrink": bool(_LEFT_SHRINK["on"]),
                "_WEB_W": _WEB_W,
                "_MANJU_W": _MANJU_W,
                "_LEFT_FLOOR": _LEFT_FLOOR,
                "manju_w": manju_col.winfo_width(),
                "manju_mapped": bool(manju_col.winfo_ismapped()),
                "web_w": web_panel.winfo_width(),
                "web_mapped": bool(web_panel.winfo_ismapped()),
                "web_open": bool(_web["open"]),
                "web_auto": int(_web_auto.get()),
                "canvas_w": canvas_host.winfo_width(),
                "shot_canvas_h": canvas.winfo_height(),
                "cfg_w": cfg.winfo_width(),
                "cfg_h": cfg.winfo_height(),
                "comfy_host_w": web_host.winfo_width(),
                "pack_slaves": [],
            }
            for w in root.pack_slaves():
                try:
                    d["pack_slaves"].append({
                        "cls": w.winfo_class(),
                        "name": str(w),
                        "w": w.winfo_width(),
                        "h": w.winfo_height(),
                        "x": w.winfo_x(),
                        "y": w.winfo_y(),
                        "rootx": w.winfo_rootx(),
                        "rooty": w.winfo_rooty(),
                        "reqw": w.winfo_reqwidth(),
                        "side": str(w.pack_info().get("side")),
                        "fill": str(w.pack_info().get("fill")),
                    })
                except Exception:
                    pass
            try:
                root.lift()
                root.attributes("-topmost", True)
                root.update()
                time.sleep(0.45)
            except Exception:
                pass
            with open(os.path.join(AP.APP_DIR, "output", "_layout_probe.json"),
                      "w", encoding="utf-8") as f:
                _json.dump(d, f, ensure_ascii=False, indent=1)
            try:
                from PIL import ImageGrab, ImageDraw
                _im = ImageGrab.grab(all_screens=True)
                _dr = ImageDraw.Draw(_im)
                _colors = [(255, 0, 0), (0, 160, 0), (0, 0, 255), (255, 0, 255),
                           (0, 180, 180), (200, 120, 0)]
                for _i, _s in enumerate(d["pack_slaves"]):
                    _c = _colors[_i % len(_colors)]
                    _dr.rectangle([_s["rootx"], _s["rooty"],
                                   _s["rootx"] + _s["w"], _s["rooty"] + _s["h"]],
                                  outline=_c, width=3)
                    _dr.text((_s["rootx"] + 6, _s["rooty"] + 4),
                             "%s w=%d" % (_s["name"], _s["w"]), fill=_c)
                _im.save(os.path.join(AP.APP_DIR, "output", "_layout_probe.png"))
            except Exception:
                pass

            # 第二组：把窗口拖小，模拟用户缩小窗口时三栏是否还能优雅压缩
            try:
                root.attributes("-topmost", False)
                root.state("normal")
                root.geometry("1280x820+40+60")
                root.update()
                time.sleep(0.9)
                root.update()
                _d2 = dict(d)
                _d2["win"] = [root.winfo_width(), root.winfo_height()]
                _d2["manju_w"] = manju_col.winfo_width()
                _d2["web_w"] = web_panel.winfo_width()
                _d2["canvas_w"] = canvas_host.winfo_width()
                _d2["shot_canvas_h"] = canvas.winfo_height()
                _d2["cfg_w"] = cfg.winfo_width()
                with open(os.path.join(AP.APP_DIR, "output", "_layout_probe_small.json"),
                          "w", encoding="utf-8") as f:
                    _json.dump(_d2, f, ensure_ascii=False, indent=1)
                from PIL import ImageGrab as _IG
                _IG.grab(all_screens=True).save(
                    os.path.join(AP.APP_DIR, "output", "_layout_probe_small.png"))
            except Exception as _e2:
                _crash("小窗探针失败: %r" % (_e2,))
        finally:
            try:
                root.after(200, root.destroy)
            except Exception:
                pass
    root.after(5000, _smoke_layout)

# ---- 自动存档：先把上次的表接回来，再开始周期落盘（断电只丢最后 3 秒） ----
try:
    _autosave_restore()
except Exception as _e:
    _crash("恢复自动存档失败: %r" % (_e,))
try:
    root.after(3000, _autosave_tick)
except Exception:
    pass

root.mainloop()

