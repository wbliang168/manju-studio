#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Ref2VA 通道：参考图 + 参考音频 → 视频 + 原生音频（音色克隆 + 口型同步）。

为什么要这条通道：
  fl2va（首帧驱动，我们一直在用的 ManjuI2VFast）**没有音频输入口** ——
  音色只能靠 prompt 的文字描述去"猜"，所以同一角色在不同镜里声音完全不同。

  官方 Ref2VA 底模 + MiniMaxH3ReferenceToVideo 节点支持：
    · ref_images  最多 9 张  → 定妆照 + 场景照 + 三视图一起喂，人脸/服装/环境一起锁
    · ref_audios  最多 3 段  → **零样本音色克隆**：给它一段某人说话的录音，
      它就用那个音色念我们新写的台词，而且口型对着新台词的波形走。

  这一步把"音色"从"prompt 里猜"变成"给样本"，
  是 H3 上唯一能真正控制音色的办法。

模型（本机已就位）：
  models/unet/minimax_h3_ref2va_pruned-Q4_K.gguf
  models/loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors
"""
import json
import os
import re
import subprocess
import time
import urllib.request

import app_paths as AP

COMFY = AP.COMFY_URL
URL = COMFY
CLIENT = "workbuddy_ref2va"
OUTDIR = AP.OUTDIR
INPUTDIR = AP.INPUTDIR

# ref2va 专用底模（GGUF Q4_K，11.4G，12G 卡可整装）
UNET = "minimax_h3_ref2va_pruned-Q4_K.gguf"
# 参考生视频专用的 turbo LoRA —— 注意不是 fl2v 那个，别混用
LORA = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
TE_NAME = "qwen3vl_4b_int8_convrot.safetensors"      # 4B 轻量编码器
VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"

W, H = 800, 448

# 从 ComfyUI-Local-Infinite-Canvas 的 h3-ref2va-drama-vdn.json 移植过来的加速链。
# 这几个节点需要先装进 ComfyUI 的 custom_nodes，且不需要额外权重：
#   TESpeedMiniMaxH3 / MiniMaxH3MemoryEfficientSageAttentionPatch / MiniMaxChunkFeedForward
# 他那条链里另外两个本机缺权重，暂时不接（补完模型再开）：
#   ApplyVDNH3Advanced     需要 models/vdn/stage-dmd-step-250
#   SelfLiftH3Sampler      需要 minimax_h3_latent_upscaler_3d_fp16.safetensors
# 想退回原来的慢链路，把这里改成 False 即可（gen_ref2va.py.bak_20261105_h3speed 是原版）。
USE_H3_ACCEL = True

# KJNodes 的 MiniMaxH3MemoryEfficientSageAttentionPatch 在本机不能开：
# 它注册的 minimax_sageattn_forward 要调 SageAttention 的 _qattn_sm80 CUDA 扩展，
# 本机（torch 2.7.1+cu126 / 3060）加载不到该扩展，_qattn_sm80 是 None，
# 采样时直接抛 'NoneType' has no attribute 'qk_int8_sv_f16_accum_f32_attn'。
# 2026-11-05 真跑一镜复现，故默认关闭；TESpeed / ChunkFeedForward 不受影响。
USE_SAGE_PATCH = False

# 再往下一刀：把采样器换成 ComfyUI-Local-Infinite-Canvas 的 SelfLift H3。
# 前 N-1 步在 0.6× 低分辨率跑，只有最后 1 步上全分辨率，省的是像素平方级的算力。
# 依赖（2026-11-05 已补齐）：
#   models/latent_upscale_models/minimax_h3_latent_upscaler_3d_fp16.safetensors
#   SelfLiftH3Sampler / H3SigmaRefiner / MiniMaxH3AVDecodeT8 节点
# SelfLift 的 vae 入参是标准 VAE 类型，用现有 VAELoader 即可，不需要 TRT engine。
# 改 False 即退回原来的 SamplerCustomAdvanced 链路。
USE_SELFLIFT = True

# ComfyUI 执行缓存：prompt+seed 一模一样会直接复用上次结果、**不落盘**，
# 但 history 里照样有文件名 → 调用方拿到一堆不存在的路径。
# 进程级盐让每次运行的 seed 都不同，绕开缓存。
_RUN_SALT = int(time.time()) % 100003


def post(path, data):
    req = urllib.request.Request(URL + path,
                                 data=json.dumps(data).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(URL + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def build_wf(prompt, ref_images, ref_audio="", length=73, seed=1, prefix="r2v",
             steps=4, width=None, height=None, audio_prefix=None,
             ref_image_size="match"):
    """ref_images: ComfyUI input 目录下的文件名列表（最多 9 张）
    ref_audio:   音色参考音频（wav/mp3 文件名），空 = 不使用音色克隆
    """
    w = int(width or W)
    h = int(height or H)
    wf = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": UNET}},
        # 4B 编码器只有 2560 维，ref2va 底模要 5120 维 → 必须挂 ClipProj 投影，
        # 否则会在采样器里报 "mat1 and mat2 shapes cannot be multiplied (146x2560 and 5120x5376)"。
        # 注意：要用 ClipProj 自己的加载节点，普通 CLIPLoader 没有这些参数。
        "2": {"class_type": "ClipProjLoader", "inputs": {
            "clip_name": TE_NAME, "type": "auto",
            "projection": "mmh3-4b-ClipProj-v3.1-mlp.safetensors",
            "device": "cuda:0", "mode": "streaming"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_VIDEO}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_AUDIO}},
        # 参考生视频专用 turbo LoRA（官方 README：strength 固定 1.0）
        "12": {"class_type": "MiniMaxH3TurboLoRA", "inputs": {
            "model": ["1", 0], "lora_name": LORA, "strength": 1.0, "low_vram": True}},
        # 官方 R2V 模板里**没有** SigmaShift：模型从 LoRA 直连调度器和 guider。
        # 之前照 fl2va 的经验加了 shift_video=6，结果整条片子出力全是黑帧
        # （latent 被压平了）。这里保持和模板一致。
    }

    # 参考图：每张单独一个 LoadImage，再按 autogrow 的格式串起来
    cond = {
        "clip": ["2", 0], "prompt": prompt, "width": w, "height": h,
        "length": int(length), "ref_image_size": ref_image_size,
        "vae": ["3", 0], "audio_vae": ["4", 0],
    }
    for k, fn in enumerate((ref_images or [])[:9]):
        nid = "30%d" % (k + 1)
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": fn}}
        # 自动增长输入（COMFY_AUTOGROW_V3）在 API 里的键名是 "ref_images.ref_image_0"，
        # 不是 "ref_images": [...]。写成列表不会报错，但参考图会被完全忽略 ——
        # 表现就是出图全黑，且日志里一句错都没有。
        cond["ref_images.ref_image_%d" % k] = [nid, 0]
    if ref_audio:
        wf["401"] = {"class_type": "LoadAudio", "inputs": {"audio": ref_audio}}
        cond["ref_audios.ref_audio_0"] = ["401", 0]

    wf["136"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": cond}

    # 移植自 ComfyUI-Local-Infinite-Canvas/h3-ref2va-drama-vdn.json 的 model 链：
    # TurboLoRA → TESpeed → SageAttention → ChunkFeedForward → 采样器。
    # 只搬不需要额外权重的节点，缺权重的 VDN / SelfLift 暂缓。
    model_out = ["12", 0]
    if USE_H3_ACCEL:
        wf["A1"] = {"class_type": "TESpeedMiniMaxH3", "inputs": {
            "model": model_out, "processing_control_value": 0.08,
            "processing_percent_1": 0.1, "processing_percent_2": 0.9,
            "mcs": 2, "device": "auto", "mode": "standard"}}
        prev = ["A1", 0]
        if USE_SAGE_PATCH:
            wf["A2"] = {"class_type": "MiniMaxH3MemoryEfficientSageAttentionPatch", "inputs": {
                "model": prev}}
            prev = ["A2", 0]
        wf["A3"] = {"class_type": "MiniMaxChunkFeedForward", "inputs": {
            "model": prev, "chunks": 2, "seq_threshold": 4096}}
        model_out = ["A3", 0]

    if USE_SELFLIFT:
        # ---- 移植自 h3-ref2va-drama-vdn.json 的 SelfLift 采样段 ----
        # 本机 ref2v 的 Turbo LoRA 是 4 步版，所以保持 steps 不变，
        # 把前 steps-1 步放低清、最后 1 步全清（他的原版是 8 步中 6 步低清）。
        n_steps = max(2, int(steps))
        wf["B1"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
        wf["B2"] = {"class_type": "BasicScheduler", "inputs": {
            "model": model_out, "scheduler": "simple",
            "steps": n_steps, "denoise": 1.0}}
        wf["B3"] = {"class_type": "H3SigmaRefiner", "inputs": {
            "sigmas": ["B2", 0], "extra_steps": 0,
            "start_at_sigma": 0.7, "end_at_sigma": 0, "spacing": "cosine"}}
        wf["B4"] = {"class_type": "ConditioningZeroOut", "inputs": {
            "conditioning": ["136", 0]}}
        wf["B5"] = {"class_type": "SelfLiftH3Sampler", "inputs": {
            "model": model_out, "positive": ["136", 0], "negative": ["B4", 0],
            "vae": ["3", 0], "latent_image": ["136", 1],
            "sampler": ["B1", 0], "sigmas": ["B3", 0],
            "seed": int(seed) + _RUN_SALT, "cfg": 1,
            "transition_step": n_steps - 1, "lowres_scale": 0.6,
            "rho": 0, "w_min": 0.3, "w_max": 1,
            "upscaler_model": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
            "upscaler_unload": True, "highres_tiling": False}}
        # T8 的 AV 解码一次吐出画面帧 + 原生音轨（输出 0=IMAGE, 1=AUDIO），
        # 正好接上原来 SaveImage / SaveAudio 的下游约定。
        wf["B6"] = {"class_type": "MiniMaxH3AVDecodeT8", "inputs": {
            "av_latent": ["B5", 0], "video_vae": ["3", 0], "audio_vae": ["4", 0]}}
        frames_out, audio_out = ["B6", 0], ["B6", 1]
    else:
        # ---- 原链路（回退用）----
        wf["7"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed) + _RUN_SALT}}
        wf["6"] = {"class_type": "BasicScheduler", "inputs": {
            "model": model_out, "scheduler": "simple", "steps": int(steps), "denoise": 1.0}}
        wf["13"] = {"class_type": "MiniMaxH3TurboSampler", "inputs": {}}
        wf["8"] = {"class_type": "BasicGuider", "inputs": {
            "model": model_out, "conditioning": ["136", 0]}}
        wf["9"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["7", 0], "guider": ["8", 0], "sampler": ["13", 0],
            "sigmas": ["6", 0], "latent_image": ["136", 1]}}
        wf["5"] = {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}}
        wf["14"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["9", 0], "vae": ["4", 0]}}
        frames_out, audio_out = ["5", 0], ["14", 0]

    wf["60"] = {"class_type": "SaveImage", "inputs": {
        "images": frames_out, "filename_prefix": prefix}}
    # 音频前缀单独给：成片管线按 "{prefix}_a{镜号}_*" 找音轨（stage_native_map）
    wf["61"] = {"class_type": "SaveAudio", "inputs": {
        "audio": audio_out, "filename_prefix": audio_prefix or prefix}}
    return wf


# ---------------- 参考音频：用 edge-tts 造一段"这个角色的声音" ----------------
def make_ref_audio(text, voice, out_name, rate="+0%", pitch="+0Hz"):
    """合成一段参考音频并转成 wav。

    这段录音不参与成片，只作为"音色样本"喂给 Ref2VA ——
    模型用它克隆音色，再念我们新写的台词（口型对新台词同步）。
    """
    import asyncio
    try:
        import edge_tts
    except Exception as e:
        print("[r2v] 没有 edge-tts：", e, flush=True)
        return ""
    mp3 = os.path.join(OUTDIR, "_ref_%s.mp3" % out_name)
    wav = os.path.join(INPUTDIR, out_name)

    async def _run():
        c = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        await c.save(mp3)

    try:
        asyncio.run(_run())
    except Exception as e:
        print("[r2v] 语音合成失败：", e, flush=True)
        return ""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([exe, "-y", "-i", mp3, "-ar", "32000", "-ac", "2", wav],
                       capture_output=True, timeout=120)
    except Exception as e:
        print("[r2v] 转 wav 失败：", e, flush=True)
        return ""
    try:
        os.remove(mp3)
    except Exception:
        pass
    return out_name if os.path.exists(wav) else ""


def submit(wf):
    try:
        d = post("/prompt", {"prompt": wf, "client_id": CLIENT})
        return (d or {}).get("prompt_id", "")
    except Exception as e:
        print("[r2v] 提交失败：", e, flush=True)
        try:
            import urllib.error
            if isinstance(e, urllib.error.HTTPError):
                print(e.read().decode("utf-8", "ignore")[:1500], flush=True)
        except Exception:
            pass
        return ""


def wait(pid, prefix, timeout=3600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = get("/history/" + pid)
            if pid in d:
                outs = d[pid].get("outputs", {})
                imgs, auds = [], []
                for nid, o in outs.items():
                    for f in o.get("images", []) or []:
                        imgs.append(os.path.join(OUTDIR, f["filename"]))
                    for f in o.get("audio", []) or []:
                        auds.append(os.path.join(OUTDIR, f["filename"]))
                if imgs:
                    return imgs, auds
                return [], []
        except Exception:
            pass
        time.sleep(3)
    return [], []
