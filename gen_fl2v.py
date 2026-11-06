# -*- coding: utf-8 -*-
"""FL2VA 首尾帧通道：给起点和终点两张图，让模型在中间插值。

为什么需要这条通道（2026-10-30）：
  现在的 r2v 只有 first_frame，没有终点约束。官方文档的说法很明确：
  「单图生视频只有起点没有终点，模型会让画面朝着它觉得合理的方向自由演化」——
  表现就是慢动作、动作做不完、人物漂移。给它一个终点，位移才有保证。
  节点名 fl2va 本身就是 First-Last-to-Video 的缩写。

代价：MiniMaxH3ImageToVideo 没有 audio_vae 入口，这条通道**不生成音频**。
      所以它只用于打斗/动作镜（本来就该后期配音效），有台词的镜仍走 r2v。
"""
import json
import os
import time

from PIL import Image

import app_paths as AP
import gen_ref2va as R
import gen_zimage_keys as Z

UNET_FL2V = "minimax_h3_fl2va_pruned-Q4_K.gguf"
LORA_FL2V = "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"

W, H = 576, 1024


def build_wf(prompt, first, last, length=124, seed=1, prefix="fl2v",
             steps=4, width=None, height=None):
    """first / last: ComfyUI input 目录下的文件名。"""
    w = int(width or W)
    h = int(height or H)
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": UNET_FL2V}},
        # 和 ref2va 一样要挂 ClipProj：4B 编码器只有 2560 维，模型要 5120 维
        "2": {"class_type": "ClipProjLoader", "inputs": {
            "clip_name": R.TE_NAME, "type": "auto",
            "projection": "mmh3-4b-ClipProj-v3.1-mlp.safetensors",
            "device": "cuda:0", "mode": "streaming"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": R.VAE_VIDEO}},
        "12": {"class_type": "MiniMaxH3TurboLoRA", "inputs": {
            "model": ["1", 0], "lora_name": LORA_FL2V, "strength": 1.0,
            "low_vram": True}},
        "20": {"class_type": "LoadImage", "inputs": {"image": first}},
        "21": {"class_type": "LoadImage", "inputs": {"image": last}},
        "136": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
            "width": w, "height": h, "length": int(length),
            "first_frame": ["20", 0], "last_frame": ["21", 0]}},
        "7": {"class_type": "RandomNoise",
              "inputs": {"noise_seed": int(seed) + R._RUN_SALT}},
        "6": {"class_type": "BasicScheduler", "inputs": {
            "model": ["12", 0], "scheduler": "simple", "steps": int(steps),
            "denoise": 1.0}},
        "13": {"class_type": "MiniMaxH3TurboSampler", "inputs": {}},
        "8": {"class_type": "BasicGuider", "inputs": {
            "model": ["12", 0], "conditioning": ["136", 0]}},
        "9": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["7", 0], "guider": ["8", 0], "sampler": ["13", 0],
            "sigmas": ["6", 0], "latent_image": ["136", 1]}},
        "5": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "60": {"class_type": "SaveImage", "inputs": {
            "images": ["5", 0], "filename_prefix": prefix}},
    }


def frame_prompt(shot, start_desc, end_desc, seconds):
    """官方 FL2VA 写法：开头一行指令说明两张图对齐的时间点，
    后面必须是**一条连续路径**的描述，不能是两张静态图的描述。
    """
    act = shot.get("action_en") or ""
    return (
        "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
        "Picture 2 (from Shot 1) aligns with the %.2f-second mark. "
        "Between these two frames the motion is continuous and physically connected, "
        "not a cut: %s\n"
        "At the start: %s. By the end: %s. "
        "Camera, framing, clothing and background stay identical between the two frames; "
        "only the body pose and the weapon position change.\n"
        "The action plays at real-time speed, fast and explosive, with hard motion blur "
        "on the moving blade. Live-action, cinematic, photorealistic, 35mm film look, "
        "rain at night, wet stone floor.\n"
        "overall_soundscape: none.\n"
        "non_diegetic_music: None."
        % (float(seconds), act or "the attack follows through to its end",
           start_desc, end_desc)
    )
