@echo off
REM ============================================
REM  MiniMax H3 + ComfyUI launcher (NVIDIA GPU)
REM  用环境变量指向你自己的 ComfyUI / venv / 模型目录；
REM  也可以在 config.json 里配好，让工作室自己启动后端。
REM ============================================
cd /d "%~dp0"
if not defined COMFY_PYTHON set "COMFY_PYTHON=python"
if not defined COMFY_MAIN (
    echo 请设置 COMFY_MAIN 指向 ComfyUI 的 main.py，例如：
    echo   set COMFY_MAIN=C:\ComfyUI\main.py
    pause
    goto :eof
)
"%COMFY_PYTHON%" -s "%COMFY_MAIN%" ^
  --port 8188 ^
  --input-directory "%~dp0input" ^
  --output-directory "%~dp0output" ^
  --extra-model-paths-config "%~dp0extra_model_paths.yaml" ^
  --lowvram --reserve-vram 1 --preview-method none ^
  --disable-pinned-memory --cache-ram 2 16
pause
