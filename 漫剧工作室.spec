# -*- mode: python ; coding: utf-8 -*-
# 开源版：所有路径都相对 spec 所在目录（SPECPATH 由 PyInstaller 提供），
# 不带任何机器专属绝对路径。把仓库 clone 到任意目录都能直接打包。
import os
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = SPECPATH

datas = []
binaries = []
datas += collect_data_files('imageio_ffmpeg')
# 人脸检测器 xml：属于 cv2 的"数据文件"，PyInstaller 的 cv2 hook 不会自动收，
# 漏了它贴脸/候选打分/质检在打包版里全部静默失效（全报 0 张脸）。
datas += [(os.path.join(ROOT, 'assets', 'haarcascade_frontalface_default.xml'), 'assets')]
# YuNet 人脸检测器（230KB）：有就用，没有就退回 Haar（不阻断打包）。
_yunet = os.path.join(ROOT, 'assets', 'face_detection_yunet_2023mar.onnx')
if os.path.exists(_yunet):
    datas += [(_yunet, 'assets')]
binaries += collect_dynamic_libs('imageio_ffmpeg')
# 内嵌 ComfyUI 网页用的 WebView2 引导库是微软的二进制，仓库里不分发；
# 本机装了 WebView2 Runtime 后可从 SDK 另放一份，存在才打包。
_wv2 = os.path.join(ROOT, 'WebView2Loader_x64.dll')
if os.path.exists(_wv2):
    binaries += [(_wv2, '.')]
# 内置 AI 漫剧 skill 能力包（动态漫剧合成 / 条漫长图 / 云端生成）
datas += [(os.path.join(ROOT, 'manju_skills'), 'manju_skills')]


a = Analysis(
    [os.path.join(ROOT, '漫剧工作室.pyw')],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=['edge_tts', 'build_manju_mp4', 'app_paths', 'make_drama', 'gen_ref2va', 'gen_zimage_keys', 'gen_manju_shots', 'gen_banana_voice', 'stopflag', 'progress', 'model_map', 'ui_theme', 'webview2_host', 'comtypes', 'PIL', 'PIL.Image', 'PIL.ImageTk', 'PIL.ImageDraw', 'numpy', 'requests', 'manju_studio_ui', 'manju_skills', 'manju_skills.drama_video', 'manju_skills.manga', 'manju_skills.cloud'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pygments 不是本项目依赖，但其 pre-built hook 会启隔离子进程收集子模块，
    # 在本非控制台环境下会触发 PyInstaller 的
    # "Cannot open console input buffer for writing" 崩溃，故直接排除。
    excludes=['pygments'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='漫剧工作室',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(ROOT, 'icon.ico')],
)
