#!/bin/bash
# 从 ModelScope 镜像下载 MiniMax H3 模型（支持断点续传，中断后重跑即可继续）。
#
# 注意：下面是项目早期「6GB 显存 / fl2va INT8 + 32B NVFP4」路线的清单。
# 当前默认 r2v 路线需要的权重见 README.md，地址清单维护在 model_map.py；
# 按自己的路线替换下面的 dl 行即可。仓库不分发任何模型权重。
#
# 可用环境变量覆盖：
#   MODELS_DIR    模型目录（默认脚本所在目录下的 models/）
#   DOWNLOAD_LOG  日志路径（默认脚本所在目录下的 download.log）
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="${MODELS_DIR:-$HERE/models}"
LOG="${DOWNLOAD_LOG:-$HERE/download.log}"
REPO="https://modelscope.cn/models/Comfy-Org/MiniMax-H3/resolve/master"

echo "=== H3 model download (ModelScope) start $(date) ===" | tee -a "$LOG"

dl() {
  local rel="$1" dest="$BASE/$1"
  mkdir -p "$(dirname "$dest")"
  echo "[$(date +%T)] START $rel" | tee -a "$LOG"
  curl -L -C - --max-time 14400 -o "$dest" "$REPO/$rel" >>"$LOG" 2>&1 \
    && echo "[$(date +%T)] DONE $rel" | tee -a "$LOG" \
    || echo "[$(date +%T)] FAIL $rel" | tee -a "$LOG"
}

dl "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors" &
dl "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors" &
dl "vae/minimax_h3_video_vae_fp16.safetensors" &
dl "loras/minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors" &
wait
echo "=== ALL tasks finished $(date) ===" | tee -a "$LOG"
