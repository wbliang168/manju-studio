#!/bin/bash
# 从 ModelScope 镜像下载 MiniMax H3 必要模型 (HF 官网被墙 + Xet 问题, 改用 ModelScope)
# 支持断点续传 (-C -), 中断后重跑本脚本即可继续.
set -u
BASE="/d/ComfyUI_MiniMaxH3/models"
REPO="https://modelscope.cn/models/Comfy-Org/MiniMax-H3/resolve/master"
LOG="/d/ComfyUI_MiniMaxH3/download.log"

echo "=== H3 model download (ModelScope) start $(date) ===" | tee -a "$LOG"

dl() {
  local rel="$1" dest="$BASE/$1"
  mkdir -p "$(dirname "$dest")"
  echo "[$(date +%T)] START $rel" | tee -a "$LOG"
  curl -L -C - --max-time 14400 -o "$dest" "$REPO/$rel" >>"$LOG" 2>&1 \
    && echo "[$(date +%T)] DONE $rel" | tee -a "$LOG" \
    || echo "[$(date +%T)] FAIL $rel" | tee -a "$LOG"
}

# 6GB 显存路线: INT8 扩散 + NVFP4 编码器 + 视频VAE (+ 8步Turbo LoRA 提速)
dl "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors" &
dl "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors" &
dl "vae/minimax_h3_video_vae_fp16.safetensors" &
dl "loras/minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors" &
wait
echo "=== ALL tasks finished $(date) ===" | tee -a "$LOG"
