@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
REM 优先跑打包好的 exe；没有就用当前 Python 环境直接跑源码。
REM 源码模式要先复制 config.example.json 为 config.json 并填好 ComfyUI 路径。
if exist "dist\漫剧工作室.exe" (
    start "" "dist\漫剧工作室.exe"
    goto :eof
)
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "漫剧工作室.pyw"
) else (
    python "漫剧工作室.pyw"
)
