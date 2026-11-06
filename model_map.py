# -*- coding: utf-8 -*-
"""
model_map.py —— 模型清单 / 检测 / 下载 / 节点自检
=====================================================
为什么需要这个模块：
  漫剧工作室.exe 只有 70MB，里面**不含模型**（整套模型约 20GB，塞不进 exe）。
  发给别人时，别人的电脑上可能一个模型都没有，或者模型放在别的盘/别的目录。
  所以要做三件事：
    1) 列出「必须下哪些模型」，并检查本机到底有没有（区分必装 / 二选一 / 可选）；
    2) 支持把模型目录指到任意位置（config.json 的 models_dir），并自动改写
       extra_model_paths.yaml，让 ComfyUI 跟着去那里读；
    3) 一键下载（ModelScope / hf-mirror 双源，断点续传）并给出进度。

给界面用的主入口：
    inventory()         -> 逐项状态列表
    summary()           -> 整体是否就绪 + 缺哪些 + 缺多少 GB
    download_entry(e, cb, stop)   -> 下载单项（阻塞，cb(已经MB, 总MB)）
    ensure_extra_yaml() -> 把 extra_model_paths.yaml 的 base_path 改成当前模型目录
    check_nodes()       -> 后端在线时检查缺哪些自定义节点
"""
import os
import sys
import shutil
import subprocess
import threading
import time

import app_paths as AP

# ---------------------------------------------------------------- 常量

# 模型根目录：config.json 里指定优先，否则用工程目录下的 models/
MODELS_ROOT = getattr(AP, "MODELSDIR", os.path.join(AP.WORKDIR, "models"))

COMFY_MODELS_DIR = os.path.join(os.path.dirname(AP.COMFY_MAIN), "models")

# cat = ComfyUI 的模型子目录名；fname = ComfyUI 里选的模型文件名
# need: core = 必装；pref = 我这套默认配置要装（有替代方案）；opt = 可选/更高画质
MODELS = [
    {
        "key": "unet_q3", "group": "unet", "cat": "unet",
        "fname": "minimax_h3_fl2va_pruned-Q3_K.gguf",
        "mb": 8353, "need": "pref",
        "desc": "扩散模型 Q3（8.4G，12G 显卡推荐）",
        "urls": [
            "https://modelscope.cn/models/unsloth/MiniMax-H3-GGUF/resolve/master/"
            "minimax_h3_fl2va_pruned-Q3_K.gguf",
            "https://hf-mirror.com/unsloth/MiniMax-H3-GGUF/resolve/main/"
            "minimax_h3_fl2va_pruned-Q3_K.gguf",
        ],
    },
    {
        "key": "unet_q4", "group": "unet", "cat": "unet",
        "fname": "minimax_h3_fl2va_pruned-Q4_K.gguf",
        "mb": 10891, "need": "opt",
        "desc": "扩散模型 Q4（10.9G，画质更好但要换页）",
        "urls": [
            "https://modelscope.cn/models/unsloth/MiniMax-H3-GGUF/resolve/master/"
            "minimax_h3_fl2va_pruned-Q4_K.gguf",
            "https://hf-mirror.com/unsloth/MiniMax-H3-GGUF/resolve/main/"
            "minimax_h3_fl2va_pruned-Q4_K.gguf",
        ],
    },
    {
        "key": "te4b", "group": "te", "cat": "text_encoders",
        "fname": "qwen3vl_4b_int8_convrot.safetensors",
        "mb": 4633, "need": "pref",
        "desc": "文本编码器 4B（4.6G，轻量方案）",
        "urls": [
            "https://modelscope.cn/models/Comfy-Org/Krea-2/resolve/master/"
            "text_encoders/qwen3vl_4b_int8_convrot.safetensors",
            "https://hf-mirror.com/Comfy-Org/Krea-2/resolve/main/"
            "text_encoders/qwen3vl_4b_int8_convrot.safetensors",
        ],
    },
    {
        "key": "clipproj", "group": "te", "cat": "clip_projections",
        "fname": "mmh3-4b-ClipProj-v3.1-mlp.safetensors",
        "mb": 480, "need": "pref",
        "desc": "4B→32B 投影矩阵（480M，配 4B 编码器）",
        "urls": [
            "https://hf-mirror.com/NicoLab28/ClipProj-MiniMax-H3/resolve/main/"
            "mmh3-4b-ClipProj-v3.1-mlp.safetensors",
        ],
    },
    {
        "key": "te32b", "group": "te", "cat": "text_encoders",
        "fname": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "mb": 14960, "need": "opt",
        "desc": "文本编码器 32B（15G，官方完整版，慢但最忠实）",
        "urls": [
            "https://modelscope.cn/models/Comfy-Org/MiniMax-H3/resolve/master/"
            "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        ],
    },
    {
        "key": "vae", "group": "vae", "cat": "vae",
        "fname": "minimax_h3_video_vae_fp16.safetensors",
        "mb": 4966, "need": "core",
        "desc": "视频 VAE（5G，必装）",
        "urls": [
            "https://modelscope.cn/models/Comfy-Org/MiniMax-H3/resolve/master/"
            "vae/minimax_h3_video_vae_fp16.safetensors",
        ],
    },
    {
        "key": "lora4", "group": "lora", "cat": "loras",
        "fname": "minimax_h3_turbo_4step_prunedfmt.safetensors",
        "mb": 1865, "need": "core",
        "desc": "4 步加速 LoRA（极速档用）",
        "convert_from": [
            "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
            "minimax_h3_turbo_4step.safetensors",
        ],
        "urls": [
            "https://modelscope.cn/models/Comfy-Org/MiniMax-H3/resolve/master/"
            "loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
        ],
    },
    {
        "key": "lora8", "group": "lora", "cat": "loras",
        "fname": "minimax_h3_turbo_8step_prunedfmt.safetensors",
        "mb": 1865, "need": "core",
        "desc": "8 步加速 LoRA（标准档用）",
        "convert_from": [
            "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
            "minimax_h3_turbo_8step.safetensors",
        ],
        "urls": [
            "https://modelscope.cn/models/Comfy-Org/MiniMax-H3/resolve/master/"
            "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        ],
    },
]

# 工作流实际用到的自定义节点（缺了就出不了片）
NEEDED_NODES = [
    ("UnetLoaderGGUF", "ComfyUI-GGUF", "GGUF 量化模型加载器（必装）",
     "https://github.com/city96/ComfyUI-GGUF"),
    ("MiniMaxH3TurboLoRA", "ComfyUI-MiniMax-H3-Turbo", "Turbo 4/8 步加速 LoRA（必装）",
     "https://github.com/larryvrh/ComfyUI-MiniMax-H3-Turbo"),
    ("MiniMaxH3ImageToVideo", "MiniMax-H3 本体", "H3 视频节点（需 ComfyUI 已更新到支持 H3 的版本）",
     "https://docs.comfy.org/tutorials/video/minimax/minimax-h3"),
    ("ManjuI2VFast", "ComfyUI-MiniMax-H3-LatentRelay", "本项目专用节点（必装，可一键安装）",
     "bundled"),
    ("ClipProjLoader", "ComfyUI-ClipProj", "4B 轻量文本编码器（用 4B 方案时必装）",
     "https://github.com/nicolab28/ComfyUI-ClipProj"),
]


# ---------------------------------------------------------------- 目录搜索

def search_roots():
    """按优先级返回可能放模型的目录（去重）。"""
    out = []
    for d in (MODELS_ROOT, os.path.join(AP.WORKDIR, "models"), COMFY_MODELS_DIR):
        d = os.path.normpath(d)
        if d not in out:
            out.append(d)
    return out


def _unet_label(fn, mb):
    """把文件名变成人能懂的档位名：Q3_K 8.2G（12G 卡推荐）"""
    up = fn.upper()
    tag = ""
    for q in ("UD-Q3_K_XL", "UD-Q2_K_XL", "Q8_0", "Q6_K", "Q5_0",
              "Q4_K", "Q3_K", "Q2_K"):
        if q in up:
            tag = q
            break
    if not tag:
        if "INT8" in up:
            tag = "INT8"
        elif "FP8" in up:
            tag = "FP8"
        elif "PRUNED" in up:
            tag = "BF16"
        else:
            tag = "模型"
    size = "%.1fG" % (mb / 1024.0) if mb > 1024 else "%dM" % max(mb, 0)
    hint = {"Q3_K": "（12G 卡推荐）", "Q2_K": "（最省显存）",
            "Q4_K": "（画质更好）", "UD-Q3_K_XL": "（画质接近 Q4）"}.get(tag, "")
    ext = "GGUF" if fn.lower().endswith(".gguf") else "safetensors"
    return "%s %s%s" % (tag, size, hint) if hint else "%s %s·%s" % (tag, size, ext)


def scan_unets():
    """扫描本机实际存在的扩散模型，返回 [{'label','fname','mb','path'}]。
    别人电脑上换成了别的量化版本，下拉里也能直接选到。"""
    out, seen = [], set()
    for root in search_roots():
        for sub in ("unet", "diffusion_models"):
            d = os.path.join(root, sub)
            if not os.path.isdir(d):
                continue
            try:
                names = sorted(os.listdir(d))
            except Exception:
                continue
            for fn in names:
                low = fn.lower()
                if not low.endswith((".gguf", ".safetensors")) or fn in seen:
                    continue
                # 只认 H3 的扩散模型，别把 Z-Image/Qwen-Image 混进来
                if "minimax_h3" not in low:
                    continue
                seen.add(fn)
                p = os.path.join(d, fn)
                mb = _mb(p)
                out.append({"label": _unet_label(fn, mb), "fname": fn,
                            "mb": mb, "path": p})
    out.sort(key=lambda x: -x["mb"])
    return out


def scan_text_encoders():
    """扫描轻量/完整文本编码器，返回 [(label, fname, kind)]；kind: 4b/32b/other。"""
    out, seen = [], set()
    for root in search_roots():
        d = os.path.join(root, "text_encoders")
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            low = fn.lower()
            if not low.endswith((".safetensors", ".gguf")) or fn in seen:
                continue
            if not ("minimax_h3" in low or "qwen3vl_4b" in low):
                continue          # 只认 H3 / 4B 这两类编码器
            seen.add(fn)
            mb = _mb(os.path.join(d, fn))
            kind = "32b" if "32b" in low else ("4b" if "4b" in low else "other")
            out.append(("%s %s" % (fn, "%.1fG" % (mb / 1024.0)), fn, kind))
    return out


def find(cat, fname):
    """在候选目录里找 cat/fname，返回第一个存在的完整路径。"""
    for root in search_roots():
        p = os.path.join(root, cat, fname)
        if os.path.isfile(p):
            return p
    return None


def find_any(cat, names):
    for n in names:
        p = find(cat, n)
        if p:
            return p
    return None


def _mb(path):
    try:
        return os.path.getsize(path) // 1048576
    except Exception:
        return 0


# ---------------------------------------------------------------- 状态盘点

def inventory():
    """逐项检查，返回带状态的列表。

    状态（st）：
      ok      已就绪
      conv    有官方原版、只差一步转换（Turbo LoRA 剥前缀）
      miss    完全没有
      opt     可选、没装也不影响出片
    """
    items = []
    for m in MODELS:
        e = dict(m)
        p = find(m["cat"], m["fname"])
        if p:
            e.update(path=p, st="ok", real_mb=_mb(p))
        else:
            src = find_any(m["cat"], m.get("convert_from", []))
            if src and m.get("convert_from"):
                e.update(path=src, st="conv", real_mb=_mb(src))
            else:
                e.update(path="", st="miss" if m["need"] != "opt" else "opt",
                         real_mb=0)
        items.append(e)

    # 同组只要有任意一项可用，整组就算可用（例如 Q3/Q4 二选一）
    groups = {}
    for e in items:
        ok = e["st"] in ("ok", "conv")
        groups[e["group"]] = groups.get(e["group"], False) or ok
    for e in items:
        if e["st"] in ("miss", "opt") and groups.get(e["group"]):
            e["group_ok"] = True
        else:
            e["group_ok"] = groups.get(e["group"], False)
    return items


def summary():
    """整体是否可就绪 + 具体缺哪几项（同组二选一的按可用性判定）。

    组规则（和实际出片链路一致）：
      unet : Q3 / Q4 / INT8 任意一个即可
      te   : (4B 编码器 + 投影矩阵) 或 (32B 完整编码器) —— 4B 少了投影也用不了
      vae  : 必须有
      lora : 4 步 / 8 步两个都要（缺了就下原版再自动转换）
    """
    items = inventory()
    by = {e["key"]: e for e in items}
    need = []

    if not any(e["path"] for e in items if e["group"] == "unet"):
        need.append(by["unet_q3"])
    te_ok = ((by["te4b"]["path"] and by["clipproj"]["path"]) or by["te32b"]["path"])
    if not te_ok:
        for k in ("te4b", "clipproj"):
            if not by[k]["path"]:
                need.append(by[k])
    if not by["vae"]["path"]:
        need.append(by["vae"])
    for k in ("lora4", "lora8"):
        if not by[k]["path"]:
            need.append(by[k])

    return {
        "ready": not need,
        "need": need,
        "need_mb": sum(e["mb"] for e in need),
        "items": items,
        "roots": search_roots(),
    }


def missing_for_generation(te4b=True):
    """生成前的最小检查：返回缺失项描述列表（空 = 可以出片）。"""
    return [e["desc"] for e in summary()["need"]]


# ---------------------------------------------------------------- extra_model_paths

_YAML_HEAD = """# 由漫剧工作室自动生成 —— 让 ComfyUI 从本部署目录读取 H3 模型
# 换电脑 / 换盘后不用手改，程序启动时会按当前配置重写这个文件
"""


def yaml_text(root=None):
    root = (root or MODELS_ROOT).replace("\\", "/").rstrip("/")
    return (_YAML_HEAD +
            "MiniMaxH3:\n"
            f"    base_path: {root}\n"
            "    checkpoints: checkpoints\n"
            "    diffusion_models: diffusion_models\n"
            "    unet: unet\n"
            "    text_encoders: text_encoders\n"
            "    vae: vae\n"
            "    loras: loras\n"
            "    clip_projections: clip_projections\n")


def ensure_extra_yaml():
    """把 extra_model_paths.yaml 的 base_path 改成当前模型目录（内容不同才写）。

    返回 True = 写过（说明原来指向别处，换电脑时这是最容易翻车的地方）。
    """
    path = os.path.join(AP.WORKDIR, "extra_model_paths.yaml")
    txt = yaml_text()
    try:
        old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    except Exception:
        old = ""
    if old.strip() == txt.strip():
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8", newline="\n").write(txt)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 下载

def _which_curl():
    for name in ("curl.exe", "curl"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _download_with_curl(url, dest, cb=None, stop=None, total_mb=0):
    curl = _which_curl()
    if not curl:
        return False, "本机没有 curl"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    cmd = [curl, "-L", "-C", "-", "--retry", "5", "--retry-delay", "3",
           "--connect-timeout", "25", "--max-time", "7200",
           "-o", dest, url]
    NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=NO_WIN)
    except Exception as e:
        return False, "启动 curl 失败: %s" % e
    last = -1
    while p.poll() is None:
        if stop is not None and stop.is_set():
            try:
                p.kill()
            except Exception:
                pass
            return False, "已取消"
        if cb:
            got = _mb(dest)
            if got != last:
                cb(got, total_mb)
                last = got
        time.sleep(1.0)
    if cb:
        cb(_mb(dest), total_mb)
    if p.returncode == 0 and os.path.getsize(dest) > 1024 * 1024:
        return True, ""
    # curl 退出码 33/36 = 服务器不支持续传 / 文件已完整
    if p.returncode in (33, 36) and os.path.getsize(dest) > 1024 * 1024:
        return True, ""
    return False, "curl 退出码 %s" % p.returncode


def download_entry(e, cb=None, stop=None):
    """下载单项（阻塞）。成功返回 (True, 存放路径)；失败 (False, 原因)。"""
    dest = os.path.join(MODELS_ROOT, e["cat"], e["fname"])
    errs = []
    for url in e["urls"]:
        ok, msg = _download_with_curl(url, dest, cb, stop, e["mb"])
        if ok:
            return True, dest
        errs.append("%s -> %s" % (url.split("/")[-1], msg))
        if stop is not None and stop.is_set():
            break
    return False, "；".join(errs) or "无可用下载地址"


def convert_lora(e, cb=None):
    """把官方 LoRA 剥掉 diffusion_model. 前缀，生成 *_prunedfmt.safetensors。
    用 ComfyUI 的 python（有 torch/safetensors）跑 prep_lora.py。"""
    src = None
    for n in e.get("convert_from", []):
        p = find(e["cat"], n)
        if p:
            src = p
            break
    if not src:
        return False, "找不到可转换的原始 LoRA"
    dst = os.path.join(MODELS_ROOT, e["cat"], e["fname"])
    script = os.path.join(AP.WORKDIR, "prep_lora.py")
    if not os.path.exists(script):
        return False, "缺少 prep_lora.py"
    py = AP.COMFY_PYTHON if os.path.exists(AP.COMFY_PYTHON) else sys.executable
    if cb:
        cb(0, 0, "正在转换 %s …" % os.path.basename(src))
    NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        r = subprocess.run([py, script, src, dst], capture_output=True,
                           text=True, timeout=900, creationflags=NO_WIN)
    except Exception as ex:
        return False, "转换失败: %s" % ex
    if r.returncode == 0 and os.path.exists(dst):
        return True, dst
    return False, (r.stderr or r.stdout or "")[-400:]


# ---------------------------------------------------------------- 手动导入

def guess_cat(fname):
    """按文件名猜它该放进哪个模型子目录（用户手上已有的文件用）。"""
    low = (fname or "").lower()
    if "clipproj" in low or "clip_proj" in low or "clip-project" in low:
        return "clip_projections"
    if "vae" in low:
        return "vae"
    if "lora" in low or "turbo" in low:
        return "loras"
    if ("text_encoder" in low or "qwen3vl" in low or low.startswith("te_")
            or "t5xxl" in low or "clip_l" in low):
        return "text_encoders"
    return "unet"


def _copy_progress(src, dst, cb=None):
    """分块复制（大文件也能报进度）。"""
    total = os.path.getsize(src)
    done = 0
    with open(src, "rb") as f, open(dst, "wb") as g:
        while True:
            buf = f.read(8 << 20)
            if not buf:
                break
            g.write(buf)
            done += len(buf)
            if cb:
                cb(done, total)
    try:
        shutil.copystat(src, dst)
    except Exception:
        pass


def import_local_files(paths, move=False, cb=None, prog=None):
    """把用户已有的模型文件收进模型目录的对应子目录。

    cb(i, n, fname, cat)   开始处理某个文件
    prog(done_bytes, total) 复制进度
    返回 (ok_list, bad_list)；ok = [(dst, cat, note)]
    """
    ok, bad = [], []
    for i, p in enumerate(paths, 1):
        fn = os.path.basename(p)
        cat = guess_cat(fn)
        if cb:
            cb(i, len(paths), fn, cat)
        try:
            d = os.path.join(MODELS_ROOT, cat)
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, fn)
            if os.path.abspath(dst) == os.path.abspath(p):
                ok.append((dst, cat, "本来就在模型目录里"))
                continue
            if os.path.exists(dst):
                if os.path.getsize(dst) == os.path.getsize(p):
                    ok.append((dst, cat, "已有同名同大小文件，跳过"))
                else:
                    bad.append((p, "目标已存在同名但大小不同（%s），请先手动处理"
                                % os.path.basename(dst)))
                continue
            same_drive = (os.path.splitdrive(p)[0].lower()
                          == os.path.splitdrive(dst)[0].lower())
            if move and same_drive:
                os.replace(p, dst)          # 同盘移动是秒级
                ok.append((dst, cat, "已移动"))
            else:
                _copy_progress(p, dst, prog)
                ok.append((dst, cat, "已复制（原文件保留）"))
        except Exception as e:
            bad.append((p, "%s: %s" % (type(e).__name__, e)))
    return ok, bad


# ---------------------------------------------------------------- 自定义节点

def comfy_custom_nodes_dir():
    return os.path.join(os.path.dirname(AP.COMFY_MAIN), "custom_nodes")


def bundled_node_dir():
    """工程里自带的节点源码（不需要联网就能装）。"""
    return os.path.join(AP.WORKDIR, "bundled_nodes", "ComfyUI-MiniMax-H3-LatentRelay")


def install_bundled_node():
    src = bundled_node_dir()
    if not os.path.isdir(src):
        return False, "工程里没有自带节点目录：%s" % src
    dst = os.path.join(comfy_custom_nodes_dir(), "ComfyUI-MiniMax-H3-LatentRelay")
    try:
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        return True, dst
    except Exception as e:
        return False, str(e)


def check_nodes(timeout=15):
    """后端在线时检查缺哪些节点。返回 [(节点名, 说明, 仓库)]；后端不在线返回 None。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(AP.COMFY_URL + "/object_info", timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return None
    missing = []
    for cls, repo, desc, url in NEEDED_NODES:
        if cls not in data:
            missing.append((cls, repo, desc, url))
    return missing


def write_download_bat(path):
    """生成一个双击就能把「缺失模型」全部下完的 .bat（给不方便点界面的场景）。"""
    s = summary()
    lines = ["@echo off", "chcp 65001 >nul",
             "title 漫剧工作室 - 下载缺失模型",
             'echo 模型将下载到: %s' % MODELS_ROOT,
             'echo 共 %d 项，约 %.1f GB。支持断点续传，中断后重跑本文件即可继续。'
             % (len(s["need"]), s["need_mb"] / 1024.0), ""]
    for e in s["need"]:
        rel = e["cat"] + "/" + e["fname"]
        d = os.path.join("%MODELS%", e["cat"]).replace("/", "\\")
        lines.append('echo === 下载 %s ===' % e["desc"])
        lines.append('if not exist "%%MODELS%%\\%s" mkdir "%%MODELS%%\\%s"' % (d, d))
        lines.append('curl -L -C - --retry 5 -o "%%MODELS%%\\%s\\%s" "%s"'
                     % (e["cat"], e["fname"], e["urls"][0]))
    lines += ["", "echo 全部下载结束。回到漫剧工作室点「🔄 重新检测」即可。",
              "pause"]
    head = ['@echo off', 'chcp 65001 >nul',
            'set MODELS=%s' % MODELS_ROOT,
            'if not exist "%MODELS%" mkdir "%MODELS%"']
    txt = "\r\n".join(head + lines[3:]) + "\r\n"
    open(path, "w", encoding="utf-8", newline="").write(txt)
    return path


def export_list_txt(path, neutral=False):
    """导出「模型下载清单」。neutral=True 时导出通用清单（不含本机状态），
    适合直接发给别人。"""
    s = summary()
    out = ["漫剧工作室 · 模型清单（不含在 exe 内，需要单独下载）",
           "=" * 62,
           "模型存放目录：%s" % MODELS_ROOT,
           "（也可以把模型放到 ComfyUI 自己的 models 目录，程序会自动找到）",
           ""]
    for e in s["items"]:
        if neutral:
            st = "⭕ 可选" if e["need"] == "opt" else "⬇ 需要"
        else:
            st = {"ok": "✅ 已就绪", "conv": "🔧 需转换", "miss": "❌ 缺失",
                  "opt": "⭕ 可选"}[e["st"]]
        out.append("[%s] %s" % (st, e["desc"]))
        out.append("     放到：models/%s/%s   （约 %.1f GB）"
                   % (e["cat"], e["fname"], e["mb"] / 1024.0))
        for u in e["urls"]:
            out.append("     下载：%s" % u)
        out.append("")
    if neutral:
        total = sum(e["mb"] for e in s["items"] if e["need"] != "opt")
        out.append("—— 必装部分共约 %.1f GB；标「⭕ 可选」的不装也能出片 ——"
                   % (total / 1024.0))
    elif s["need"]:
        out.append("—— 当前一共还缺 %d 项，约 %.1f GB ——"
                   % (len(s["need"]), s["need_mb"] / 1024.0))
    else:
        out.append("—— 模型齐全，可以直接出片 ——")
    out.append("")
    out.append("自定义节点（缺了也出不了片，装在 ComfyUI/custom_nodes 下）：")
    for cls, repo, desc, url in NEEDED_NODES:
        out.append("  - %-26s %s" % (repo, url if url != "bundled" else "（程序自带，点「一键安装」）"))
    open(path, "w", encoding="utf-8", newline="\r\n").write("\n".join(out))
    return path


if __name__ == "__main__":
    s = summary()
    print("模型目录候选：")
    for r in s["roots"]:
        print("  ", r, "存在" if os.path.isdir(r) else "不存在")
    print()
    for e in s["items"]:
        print("%-8s %-52s %s" % (e["st"], e["fname"], e["path"] or "-"))
    print()
    print("就绪:", s["ready"], " 还缺 %d 项 %.1fGB" % (len(s["need"]), s["need_mb"] / 1024))
