# -*- coding: utf-8 -*-
"""
app_paths.py —— 漫剧工作室 的路径与配置中心
============================================
解决两件事：
  1) onefile exe 运行时被解压到临时目录，不能再用 __file__ 定位工程；
     一律以「exe 真实所在目录」(sys.argv[0]) 为基准。
  2) 发给别人用时，别人机器上的 ComfyUI / Python / 端口都不同，
     一律从同目录的 config.json 读取，缺省回退到本机默认值。

WORKDIR（工程根）自动探测：从 exe 目录向上找含 extra_model_paths.yaml
或 models/ 的目录，找不到就用 exe 所在目录。
"""
import os
import sys
import json

# exe 真实所在目录（onefile 下 sys.argv[0] 是用户双击的原始路径）
APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# 默认配置：开源版不带任何机器专属路径，一律由 config.json 或环境变量给出。
# 复制 config.example.json 为 config.json，填上你自己 ComfyUI 的路径即可。
DEFAULTS = {
    "comfy_main": os.environ.get("COMFY_MAIN", ""),
    "comfy_python": os.environ.get("COMFY_PYTHON", ""),
    "port": 8188,
    "workdir": "",          # 空 = 自动探测
    "models_dir": "",       # 空 = 工程目录下的 models/；模型在别处时填这里
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        if os.path.exists(CONFIG_PATH):
            cfg.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
    except Exception:
        pass
    return cfg


CFG = load_config()


def find_workdir(app_dir):
    """从 exe 目录起向上找含 extra_model_paths.yaml 或 models/ 的目录。"""
    cands = [app_dir, os.path.dirname(app_dir),
             os.path.dirname(os.path.dirname(app_dir))]
    for c in cands:
        if (os.path.exists(os.path.join(c, "extra_model_paths.yaml"))
                or os.path.isdir(os.path.join(c, "models"))):
            return c
    return app_dir


def find_canvas_dir(app_dir, workdir):
    """找「ComfyUI-本地无限画布」目录（内含 launcher/start.mjs）。

    以前这里靠用户手点 start-local.bat，exe 里根本没拉起 :3000，
    结果画布面板永远停在「127.0.0.1 拒绝连接」。按 config → 工程内 → 上级盘 → 全盘默认找。
    """
    cands = []
    c = CFG.get("canvas_dir")
    if c:
        cands.append(c)
    for base in (workdir, app_dir):
        cands.append(os.path.join(base, "ComfyUI-Local-Infinite-Canvas"))
    for base in (workdir, app_dir):
        cands.append(os.path.join(os.path.dirname(base),
                                  "ComfyUI-Local-Infinite-Canvas"))
    # 画布项目位置用环境变量 CANVAS_DIR 指定，不再写死机器路径
    if os.environ.get("CANVAS_DIR"):
        cands.append(os.environ["CANVAS_DIR"])
    for c in cands:
        if c and os.path.exists(os.path.join(c, "launcher", "start.mjs")):
            return c
    return ""


WORKDIR = CFG.get("workdir") or find_workdir(APP_DIR)
COMFY_MAIN = CFG["comfy_main"]
COMFY_PYTHON = CFG["comfy_python"]
PORT = int(CFG.get("port", 8188))
COMFY_URL = "http://127.0.0.1:{}".format(PORT)

# 无限画布：:3000 起（被占用时启动器会退到 3001-3010），网关固定 8190
CANVAS_PORT = int(CFG.get("canvas_port", 3000))
CANVAS_PORTS = list(range(CANVAS_PORT, CANVAS_PORT + 11))
CANVAS_URL = "http://127.0.0.1:{}".format(CANVAS_PORT)
CANVAS_DIR = find_canvas_dir(APP_DIR, WORKDIR)
CANVAS_LAUNCHER = (os.path.join(CANVAS_DIR, "launcher", "start.mjs")
                   if CANVAS_DIR else "")
CANVAS_NODE = ""
for _n in (os.path.join(CANVAS_DIR, "node", "node.exe") if CANVAS_DIR else "",
           os.path.join(os.path.dirname(COMFY_PYTHON), "node.exe")
           if COMFY_PYTHON else ""):
    if _n and os.path.exists(_n):
        CANVAS_NODE = _n
        break

OUTDIR = os.path.join(WORKDIR, "output")
INPUTDIR = os.path.join(WORKDIR, "input")
SCRIPTDIR = os.path.join(WORKDIR, "manju_studio", "scripts")
MODELSDIR = CFG.get("models_dir") or os.path.join(WORKDIR, "models")
EXTRA = os.path.join(WORKDIR, "extra_model_paths.yaml")

# 兼容旧变量名
HERE = APP_DIR


def save_config(cfg):
    json.dump(cfg, open(CONFIG_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
