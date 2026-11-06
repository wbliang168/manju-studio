# 漫剧工作室（Manju Studio）

把一份剧本，变成能直接看的 AI 漫剧视频。默认全在本地跑：ComfyUI 负责出图/出视频，不填任何 Key 也能用。仓库里另带一个可选的云端生成模块（`manju_skills/cloud.py`），不用它就不联网。

导入剧本 → 自动拆镜、排景别/运镜/时长 → 生成角色定妆照锁脸 → 逐镜生成 → 质检 → 自动拼成 mp4。

## 先说清楚

- 这是给**已经有 ComfyUI 环境**的人用的工具，不是一键安装包。
- 仓库里**没有模型权重、没有 exe、没有自定义节点**。模型要自己下载（几十 GB），并遵守各自许可。
- 在 Windows + NVIDIA GPU 上验证过。12GB 显存能跑，但慢，长片按小时算。
- 主程序是一个 Python/Tk 桌面窗口（`漫剧工作室.pyw`），`dist/漫剧工作室.exe` 只是它的 PyInstaller 打包产物。

## 能做什么

- 剧本导入：表格 TSV（镜号 / 时长 / 画面描述 / 台词·旁白 / 音效 / 运镜 / 参考角色 / 景别），或直接粘自然语言剧本
- 自动分镜：景别、角度、运镜、构图、时长；超长台词自动拆段
- 角色一致性：定妆照 + 三视图 + 脸部板 + 固定外貌文案，跨镜锁同一张脸
- 场景一致性：同一场景的多镜共用一张场景照
- 声音：旁白 / 对白配音（edge-tts），或 H3 模型原生发声（音画同源）
- 出片：逐镜渲染 → 动作量 / 亮度 / 人脸质检 → 自动拼接 mp4
- 可选：内嵌无限画布，把分镜推成节点、单独出图预览

## 环境

- Windows 10/11 + NVIDIA GPU（建议 ≥12GB 显存）
- Python 3.10+（推荐 3.12）
- 一份能跑起来的 ComfyUI（含 MiniMax H3 所需自定义节点）
- 可选：Node.js 20+ 与 ComfyUI-Local-Infinite-Canvas（无限画布，默认只绑 127.0.0.1）
- 可选：Microsoft Edge WebView2 Runtime（内嵌网页用）

## 快速开始

1. 克隆仓库，装依赖

   ```
   python -m pip install -r requirements.txt
   ```

2. 复制配置模板并改成自己的路径

   ```
   copy config.example.json config.json
   copy extra_model_paths.example.yaml extra_model_paths.yaml
   ```

   至少填 `comfy_main`（ComfyUI 的 main.py）、`comfy_python`（ComfyUI 用的 python.exe）、`models_dir`（模型目录；留空则用工程下 `models/`）。也可以改用环境变量 `COMFY_MAIN` / `COMFY_PYTHON` / `CANVAS_DIR`。

3. 起 ComfyUI 后端（8188 端口）。可以先设好环境变量再用 `run_nvidia.bat`，或直接用你自己的启动脚本；关键是带上 `extra_model_paths.yaml`。

4. 启动工作台

   ```
   python 漫剧工作室.pyw
   ```

   或双击 `启动漫剧工作室.bat`。

5. 导入 `examples/示例_2镜.tsv`，点「开始生成」。

## 打包成 exe

```
pyinstaller 漫剧工作室.spec --noconfirm --distpath dist --workpath build
```

spec 里所有路径都相对 spec 所在目录，clone 到任何位置都能打包。`WebView2Loader_x64.dll` 是微软的二进制，仓库不分发：本机有就自动打进去，没有也不影响打包（运行时用系统 WebView2 Runtime）。

## 模型

仓库不含权重。r2v 路线主要需要（文件名以你下载到的版本为准）：

- `minimax_h3_ref2va_pruned-Q4_K.gguf`（底模）
- `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`（4 步 Turbo LoRA）
- `qwen3vl_4b_int8_convrot.safetensors`（4B 文本编码器）
- `mmh3-4b-ClipProj-v3.1-mlp.safetensors`（投影）
- `minimax_h3_video_vae_fp16.safetensors`、`minimax_h3_audio_vae_fp32.safetensors`

常见来源是 Comfy-Org/MiniMax-H3（HuggingFace / ModelScope 镜像）。**下载前看清模型作者许可**，本项目不替你授权。

## 已知限制

- 12GB 卡 + `--lowvram` 很慢。关掉「自动定帧」、把每镜帧数调小能明显提速。
- 无限画布网关里的「H3 极速草稿」工作流要求 PyTorch CUDA 13+ 且需要 FastH3 专用权重，多数机器跑不了；本项目主界面的 r2v 路线不依赖它。
- 主界面 → 画布的桥接是**单向**的：分镜会推成画布节点，画布里生成的结果不会自动回流分镜表。
- `BUGS.md` 记着一些历史坑，遇到问题可以先翻一眼。

## 许可

本项目代码：MIT（见 `LICENSE`）。第三方组件与模型各有各的许可，见 `THIRD_PARTY_NOTICES.md`。本项目与这些上游项目没有隶属关系。

## 致谢

ComfyUI、Comfy-Org MiniMax-H3 模型、ComfyUI-Local-Infinite-Canvas、comfystreamerh3、basketikun/infinite-canvas、edge-tts、OpenCV、PyInstaller。
