#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用本机已有的 Z-Image-Turbo(8步)出《第一颗香蕉》5 镜统一风格关键帧。
模型引用改成本机实际文件名:
  UNET = z_image_turbo_int8_convrot.safetensors (models/unet/)
  CLIP = qwen_3_4b_fp8_mixed.safetensors      (models/clip/)
  VAE  = ae.safetensors                        (models/vae/)
输出: output/banana_key_1~5_00001_.png  (与 H3 同目录, 后续直接喂 --i2v)
统一影调约束: 暖金绿写实雨林 / 电影感 / 柔和自然光 / 高细节 / 禁止文字
"""
import urllib.request, json, os, sys, time

import app_paths as AP
COMFY = AP.COMFY_URL
CLIENT = "workbuddy_zimage"
OUTDIR = AP.OUTDIR
UNET = "z_image_turbo_int8_convrot.safetensors"
CLIP = "qwen_3_4b_fp8_mixed.safetensors"
VAE = "ae.safetensors"
# 关键帧比 H3 实际渲染尺寸大 1/3 左右(留细节), 由 make_drama.stage_keys 按画幅设置:
#   竖屏 576x1024 -> 768x1360 ; 横屏 1024x576 -> 1360x768
W, H = 768, 1360

# 5 镜统一风格关键帧 prompt(小猴贯穿、暖金绿写实、电影感、禁止文字)
KEYS = [
    "电影感写实摄影,远古热带雨林清晨,金色阳光穿透茂密树冠,薄雾在林间流动,一棵散发淡金色微光的巨大神树矗立中央,一只毛茸茸的小猴子停在藤蔓上仰望神树,宽大绿叶,丁达尔光束,暖金绿色调,柔和自然光,高细节,8k,电影级构图",
    "电影感写实微距,特写,一根弯弯的、表皮光滑带蜡质光泽的明黄色香蕉,顶端连着褐色果柄与花萼,明显挂在神树的枝条上、倚靠在宽大绿叶间,表面流转细密金色光纹,周围花粉般光点闪烁,自然投影落在叶片上,背景虚化的雨林绿意,暖金绿色调,柔和光,高细节",
    "电影感写实,一位温和的自然精灵,清晰五官,有五根手指的人手,双脚站立接地,半透明身躯环绕翠绿与金色光雾,在神树前轻轻伸手赐福,柔和光,暖金绿色调,写实身体结构,高细节",
    "电影感写实微距,一根成熟的弯月形明黄香蕉,自然垂挂在一串真实香蕉果串上,果串连着翠绿芭蕉植株的假茎与宽大叶片,表皮泛起温暖光泽,一只写实的小蜜蜂停在香蕉上,背景雨林在暖光中朦胧,暖金绿色调,柔和光线,高细节",
    "电影感写实全景,小猴子掰下那根香蕉转身分给林中伙伴,镜头缓缓拉远升高,俯瞰整片雨林,无数发着金光的香蕉树在绿海中铺展,壮丽日出霞光,宏大而温馨,暖金绿色调,电影级运镜,高细节",
]
NEG = "文字,字幕,标题,台词,字母,数字,符号,可读文本,招牌,标牌,假汉字,乱码,低质量,模糊,畸变,多余肢体,融化的身体"


def build_wf(prompt, seed, prefix, ref="", denoise=0.62, ref_fit="center"):
    """文生图关键帧工作流；ref 非空时改走 img2img，用角色定妆照锁住五官。

    ref     : ComfyUI input 目录下的文件名（如 "char_suyunzhou.png"），空=纯文生图
    denoise : img2img 强度。0.45~0.55 保留最多五官（构图变化小），
              0.60~0.70 能换场景/景别但仍带同一张脸，>0.8 基本就洗掉了
    ref_fit : ImageScale 的 crop 参数，只能是 "disabled"(拉伸) 或 "center"(居中裁切)。
              注意：老代码写的是 "crop"，ComfyUI 不认识 → /prompt 直接 400，补渲全挂。
    """
    wf = {
        "37": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "39": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "38": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "58": {"class_type": "EmptySD3LatentImage", "inputs": {"width": W, "height": H, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["38", 0], "text": prompt}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["38", 0], "text": NEG}},
        "3": {"class_type": "KSampler", "inputs": {
            "model": ["37", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["58", 0], "seed": seed, "steps": 8, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["39", 0]}},
        "60": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }
    if ref:
        # 参考图 -> 缩放到目标画幅 -> 编码成 latent 当起点，再按 denoise 重绘
        wf["90"] = {"class_type": "LoadImage", "inputs": {"image": ref}}
        _fit = ref_fit if ref_fit in ("disabled", "center") else "center"
        wf["92"] = {"class_type": "ImageScale", "inputs": {
            "image": ["90", 0], "upscale_method": "lanczos",
            "width": W, "height": H, "crop": _fit}}
        wf["91"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["92", 0], "vae": ["39", 0]}}
        wf["3"]["inputs"]["latent_image"] = ["91", 0]
        wf["3"]["inputs"]["denoise"] = float(denoise)
    return wf


def submit(wf):
    data = json.dumps({"prompt": wf, "client_id": CLIENT}).encode()
    req = urllib.request.Request(COMFY + "/prompt", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("prompt_id")
    except Exception as e:
        print("[submit fail]", e)
        return None


def wait(pid, prefix, timeout=900):
    t0 = time.time()
    last = 0
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(COMFY + "/history/" + pid, timeout=20) as r:
                d = json.loads(r.read())
            if pid in d:
                outs = d[pid]["outputs"]
                for nid, o in outs.items():
                    for f in o.get("images", []):
                        fp = os.path.join(OUTDIR, f["filename"])
                        if f["filename"].startswith(prefix):
                            return fp
                return True
        except Exception:
            pass
        time.sleep(3)
    return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只重出指定镜号, 逗号分隔, 如 2,4")
    a = ap.parse_args()
    only = set(int(x) for x in a.only.split(",") if x.strip()) if a.only else None
    done = 0
    for i, k in enumerate(KEYS, 1):
        if only and i not in only:
            continue
        prefix = f"banana_key_{i}"
        wf = build_wf(k, 990001 + i * 31337, prefix)
        pid = submit(wf)
        if not pid:
            print(f"[key {i}] 提交失败")
            sys.exit(1)
        print(f"[key {i}] 已提交 {pid}, 等待出图...")
        res = wait(pid, prefix)
        if res and os.path.exists(res):
            print(f"[key {i}] 完成 -> {res}")
            done += 1
        else:
            print(f"[key {i}] 未完成(超时或失败)")
    print(f"[全部完成] 成功 {done}/{len(KEYS)} 镜")
