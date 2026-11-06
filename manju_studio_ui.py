# -*- coding: utf-8 -*-
"""
漫剧工作室 · AI漫剧工坊 UI 层
===========================
把 WorkBuddy 技能市场里几个漫剧/视频 skill 的能力，做成主程序里的一块常驻面板：
  · 动态漫剧合成  —— manju_skills.drama_video（分镜图 + 推镜/呼吸 → mp4，纯本地）
  · 条漫长图拼接  —— manju_skills.manga（带对白气泡/旁白框的长图，纯本地）
  · 云端视频生成  —— manju_skills.cloud（AI Hive / Seedance 文生/图生/参考生视频）
  · 🎬 AI 剧情出片 —— 驱动画布网关(:8190) 全链路（编剧→出图→配音→合成）

扩展方式（以后加新工具只改这里）：
  1) 写一个 def _build_xxx_tab(nb, root): ...  （nb 是 ttk.Notebook，root 是主窗口）
  2) 在文件末尾 register_manju_tab(" 标签名 ", _build_xxx_tab) 一行
  主界面会自动把它作为一个新 Tab 显示出来，无需动主程序布局代码。

所有函数都在主程序进程内直接调用（打包后没有独立 python，不能 subprocess 调 .py）。
ffmpeg 走 imageio_ffmpeg，运行时复制成 ffmpeg.exe 桥接进 PATH。
"""
import os
import sys
import json
import glob
import shutil
import tempfile
import threading
import contextlib
import subprocess
import urllib.request
import urllib.error
import tkinter as tk
import tkinter.ttk as ttk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
import tkinter.scrolledtext as scrolledtext

# 让同目录的 manju_skills 包可 import（开发/打包都覆盖）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from manju_skills import drama_video, manga, cloud
except Exception as _e:  # 让 attach_menu 即使出错也不崩主程序
    drama_video = manga = cloud = None
    _IMPORT_ERR = _e
else:
    _IMPORT_ERR = None

try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None

try:
    import app_paths as AP
except Exception:
    AP = None


# ----------------------------------------------------------------------------
# Tab 注册表：主程序 build_manju_panel 会按注册顺序把它们变成 Notebook 的一页。
# 以后加新工具，只需写一个 _build_xxx_tab 并 register_manju_tab(...) 即可。
# ----------------------------------------------------------------------------
MANJU_TABS = []   # 元素：(label, builder)


def register_manju_tab(label, builder):
    """注册一个工坊 Tab。label=标签页标题，builder(nb, root) 负责往 Notebook 里加内容。"""
    if callable(builder):
        MANJU_TABS.append((label, builder))


# ----------------------------------------------------------------------------
# ffmpeg 桥接：imageio_ffmpeg 的二进制名不是 ffmpeg.exe，复制一份让脚本能找到
# ----------------------------------------------------------------------------
def ensure_ffmpeg():
    if imageio_ffmpeg is None:
        return False
    try:
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        d = os.path.join(tempfile.gettempdir(), "manju_ffmpeg")
        os.makedirs(d, exist_ok=True)
        tgt = os.path.join(d, "ffmpeg.exe")
        if not os.path.exists(tgt):
            shutil.copy(ff, tgt)
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------------
# 画布网关对接（manju_studio/server.py，端口 8190）
# 网关早已实现「剧情 → 分镜 → ComfyUI 出图 → TTS → 合成 mp4」全链路：
#   · 编剧：云端大模型 / 本地大模型（OpenAI 兼容）/ 本地词库兜底（覆盖你说的云端+本地）
#   · 出图 + 配音 + 合成：本机 ComfyUI（MiniMax H3）
# 这里只做原生 tkinter 入口去「驱动」它，不重复造 LLM / 出图逻辑。
# ----------------------------------------------------------------------------
GW_URL = "http://127.0.0.1:8190"
_GW_TIMEOUT = 25


def _gw_get(path):
    try:
        with urllib.request.urlopen(GW_URL + path, timeout=_GW_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _gw_post(path, body):
    try:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(GW_URL + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_GW_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _locate_out(name):
    """按文件名定位网关成品（output 目录），找不到就全盘兜底搜。"""
    if not name:
        return None
    candidates = []
    try:
        import manju_studio.server as _svr
        candidates.append(_svr.OUT)
    except Exception:
        pass
    # output 目录由 AP.WORKDIR 决定，不写死机器路径
    if AP is not None:
        candidates.append(os.path.join(AP.WORKDIR, "output"))
    for c in candidates:
        p = os.path.join(c, name)
        if os.path.exists(p):
            return p
    roots = [os.getcwd()]
    if AP is not None:
        roots.append(AP.WORKDIR)
    for root_dir in roots:
        for hit in glob.glob(os.path.join(root_dir, "**", name), recursive=True):
            return hit
    return None


def _open_out(name):
    if not name:
        messagebox.showinfo("提示", "还没有成片。先点『一键出片』等它跑完。")
        return
    p = _locate_out(name)
    if p and os.path.exists(p):
        try:
            os.startfile(p)
        except Exception:
            try:
                subprocess.Popen(["explorer", "/select,", p])
            except Exception:
                pass
    else:
        messagebox.showwarning("未找到", "找不到成片文件：%s\n（应在画布网关的 output 目录）" % name)


def _launch_gw():
    try:
        server_py = os.path.join(os.path.dirname(_HERE), "manju_studio", "server.py")
        if not os.path.exists(server_py):
            messagebox.showwarning("未找到", "找不到网关服务：%s" % server_py)
            return
        subprocess.Popen([sys.executable, server_py],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        messagebox.showinfo("已尝试启动",
                           "已在后台启动画布网关(:8190)，几秒后点『检测网关』确认。\n"
                           "若失败，请先展开右侧『无限画布』面板，或手动运行 server.py。")
    except Exception as e:
        messagebox.showerror("启动失败", "启动网关失败：%s" % e)


def _poll_story(root, logbox, state):
    """轮询网关 /status，把新日志/进度/成片回显到 logbox。state 跨轮保存。"""
    def _tick():
        try:
            st = _gw_get("/status")
            if st is None:
                logbox.insert("end", "…网关连接中断，2s 后重试\n"); logbox.see("end")
                root.after(2000, _tick); return
            logs = st.get("log") or []
            for line in logs[state.get("log_n", 0):]:
                logbox.insert("end", line + "\n")
            state["log_n"] = len(logs)
            logbox.see("end")
            if st.get("error"):
                logbox.insert("end", "❌ " + st["error"] + "\n"); logbox.see("end"); return
            res = st.get("result")
            if res and res.get("mp4_name"):
                state["name"] = res["mp4_name"]
                logbox.insert("end", "✅ 成片完成：" + res["mp4_name"] + "\n"); logbox.see("end")
                return
            if not st.get("busy"):
                logbox.insert("end", "· 网关空闲（任务可能已结束但未产出成片）\n"); logbox.see("end")
                return
            root.after(2000, _tick)
        except Exception as e:
            logbox.insert("end", "轮询异常：" + str(e) + "\n"); logbox.see("end")
            root.after(2000, _tick)
    root.after(0, _tick)


def _build_story_tab(nb, root):
    """🎬 AI 剧情出片：原生入口驱动画布网关的全链路（编剧→出图→配音→合成）。"""
    f = ttk.Frame(nb)
    nb.add(f, text=" 🎬 剧情出片 ")
    f.columnconfigure(0, weight=1)

    _steps = tk.IntVar(value=25)
    _cfg = tk.DoubleVar(value=1.0)
    _turbo = tk.BooleanVar(value=False)
    _consist = tk.StringVar(value="off")

    info = ttk.Label(f, wraplength=380, justify="left", font=("Microsoft YaHei", 9),
                     foreground="#666")
    info.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
    info.config(text="把一段剧情直接出成漫剧成片：编剧（云端/本地大模型，或本地词库兜底）→ ComfyUI 出图 → 配音 → 合成。\n"
                    "需先启动画布网关(:8190) 且本机 ComfyUI(:8188) 在线。大模型/本地词库在网关「编剧」设置里配。")

    trow = ttk.Frame(f)
    trow.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
    ttk.Label(trow, text="剧情 / 主题：").pack(anchor="w")
    topic_box = scrolledtext.ScrolledText(trow, height=6, wrap="word", font=("Microsoft YaHei", 10))
    topic_box.pack(fill="x", padx=0, pady=2)

    prow = ttk.LabelFrame(f, text="出图参数（传给 ComfyUI）")
    prow.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
    prow.columnconfigure(2, weight=1)
    _kv(prow, "采样步数", _steps, 4, 40, 1)
    _kv(prow, "CFG", _cfg, 1.0, 10.0, 0.5)
    ttk.Checkbutton(prow, text="Turbo 加速", variable=_turbo).grid(row=0, column=3, padx=6)
    ttk.Label(prow, text="一致性：").grid(row=1, column=0, padx=6, pady=3, sticky="w")
    ttk.Combobox(prow, textvariable=_consist, values=["off", "relay", "fixed"],
                 width=10, state="readonly").grid(row=1, column=1, padx=6, pady=3, sticky="w")

    # 工坊列只有 430px：状态左对齐独占一行，4 个按钮 2×2 铺开，
    # 一行挤 5 个的话「打开成品 / 启动网关」会被右边界切掉。
    btns = ttk.Frame(f)
    btns.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
    btns.columnconfigure(0, weight=1, uniform="b")
    btns.columnconfigure(1, weight=1, uniform="b")
    status = ttk.Label(btns, text="网关状态：检测中…", foreground="#666")
    status.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
    logbox = scrolledtext.ScrolledText(f, wrap="word", font=("Microsoft YaHei", 9))
    logbox.grid(row=4, column=0, sticky="nsew", padx=8, pady=4)
    f.rowconfigure(4, weight=1)

    def _health():
        st = _gw_get("/status")
        if st is None:
            status.configure(text="网关状态：未连接(:8190)", foreground="#c00")
            return None
        comfy = st.get("comfy")
        status.configure(text="网关状态：在线（ComfyUI %s）" % ("在线" if comfy else "未就绪"),
                         foreground="#080" if comfy else "#c80")
        return st

    def _go():
        topic = topic_box.get("1.0", "end").strip()
        if not topic:
            messagebox.showwarning("提示", "请先填写剧情 / 主题")
            return
        st = _health()
        if st is None:
            if not messagebox.askyesno("网关未启动",
                                       "画布网关(:8190)没起来。\n是否尝试启动它？（需本机已装好依赖；"
                                       "若失败请先展开右侧『无限画布』面板或手动运行 server.py）"):
                return
            _launch_gw()
            root.after(3500, _go)
            return
        if not st.get("comfy"):
            if not messagebox.askyesno("ComfyUI 未就绪",
                                       "本机 ComfyUI(:8188) 未在线，出图会失败。仍要提交吗？"):
                return
        state = {"log_n": 0, "name": None}
        logbox.insert("end", "① 编剧中（大模型 / 本地词库）…\n"); logbox.see("end")
        scr = _gw_post("/api/script", {"topic": topic})
        if not scr or not scr.get("shots"):
            logbox.insert("end", "❌ 编剧失败：%s\n" % (scr.get("error") if scr else "网关无返回，可能未启动")); logbox.see("end")
            return
        logbox.insert("end", "✅ 编剧完成：《%s》共 %d 镜（来源：%s）\n" % (
            scr.get("title", "?"), len(scr["shots"]), scr.get("source", "?"))); logbox.see("end")
        logbox.insert("end", "② 提交全链路出片（ComfyUI 出图 + 配音 + 合成）…\n"); logbox.see("end")
        params = {"shots": scr["shots"], "steps": _steps.get(), "cfg": _cfg.get(),
                  "turbo": _turbo.get(), "consist": _consist.get()}
        if _turbo.get():
            params["turbo_steps"] = 6
        res = _gw_post("/generate", params)
        if not res or not res.get("ok"):
            logbox.insert("end", "❌ 提交失败：%s\n" % (res.get("error") if res else "网关无返回")); logbox.see("end")
            return
        logbox.insert("end", "⏳ 已开始，自动轮询进度…（成片出来后点『打开成品』）\n"); logbox.see("end")
        _poll_story(root, logbox, state)
        _open_out_btn.configure(command=lambda: _open_out(state["name"]))

    ttk.Button(btns, text="🎬 一键出片", command=_go).grid(
        row=1, column=0, sticky="ew", padx=(0, 3), pady=(0, 3))
    ttk.Button(btns, text="🔄 检测网关", command=_health).grid(
        row=1, column=1, sticky="ew", padx=(3, 0), pady=(0, 3))
    _open_out_btn = ttk.Button(btns, text="📂 打开成品", command=lambda: _open_out(None))
    _open_out_btn.grid(row=2, column=0, sticky="ew", padx=(0, 3))
    ttk.Button(btns, text="🚀 启动网关", command=_launch_gw).grid(
        row=2, column=1, sticky="ew", padx=(3, 0))

    root.after(500, _health)


# ----------------------------------------------------------------------------
# 后台线程 + 日志转发
# ----------------------------------------------------------------------------
class _LogWrite:
    def __init__(self, widget, root):
        self.w = widget
        self.r = root

    def write(self, s):
        if s:
            self.r.after(0, lambda s=s: (self.w.insert("end", s), self.w.see("end")))

    def flush(self):
        pass


def _run_thread(root, logbox, work, done_text="完成"):
    """在后台线程跑 work()，所有 print 实时转发到 logbox；结束更新状态。"""
    def _tick(msg):
        root.after(0, lambda: (logbox.insert("end", msg + "\n"), logbox.see("end")))

    def _job():
        try:
            with contextlib.redirect_stdout(_LogWrite(logbox, root)):
                work()
            root.after(0, lambda: (logbox.insert("end", "\n✅ %s\n" % done_text),
                                   logbox.see("end")))
        except SystemExit as e:
            root.after(0, lambda: (logbox.insert("end", "\n⚠ 终止：%s\n" % e),
                                   logbox.see("end")))
        except Exception as e:
            root.after(0, lambda: (logbox.insert("end", "\n❌ 出错：%s\n" % e),
                                   logbox.see("end")))

    threading.Thread(target=_job, daemon=True).start()


# ----------------------------------------------------------------------------
# 菜单入口
# ----------------------------------------------------------------------------
def attach_menu(root, on_open=None):
    """挂「工具 → 🎬 AI漫剧工坊」菜单。

    on_open 可选：传入一个回调( root ) 时，点菜单就调它（主程序用来把工坊
    嵌进主界面左侧常驻列，而不是弹独立窗口）；不传则退回弹 Toplevel 的旧行为。
    """
    try:
        menubar = tk.Menu(root)
        tool = tk.Menu(menubar, tearoff=0)
        target = on_open if callable(on_open) else open_manju_studio
        tool.add_command(label="🎬 AI漫剧工坊", command=lambda: target(root))
        menubar.add_cascade(label="工具", menu=tool)
        root.config(menu=menubar)
    except Exception as e:
        print("AI漫剧工坊菜单失败:", e)


def build_manju_panel(parent, root):
    """把已注册的工坊 Tab 全部建到 parent（一个 Frame）里，返回 Notebook。

    主程序用它把工坊嵌进左侧常驻列：parent 来自主界面，本函数只负责
    往里塞 Notebook + 各 Tab，不新建任何 Toplevel。
    每个 Tab 用 grid + rowconfigure(weight=1) 撑满，避免内容堆在顶部留大片空白。
    """
    nb = ttk.Notebook(parent)
    nb.pack(fill="both", expand=True, padx=6, pady=6)
    for label, builder in MANJU_TABS:
        try:
            builder(nb, root)
        except Exception as e:
            ef = ttk.Frame(nb)
            nb.add(ef, text=label.strip() or "Tab")
            ttk.Label(ef, text="该 Tab 加载失败：%s" % e, foreground="#c00").pack(padx=12, pady=12)
    return nb


def open_manju_studio(root):
    if _IMPORT_ERR is not None:
        messagebox.showerror("模块加载失败",
                             "manju_skills 未能加载：%s\n请确认 manju_skills/ 目录存在。" % _IMPORT_ERR)
        return
    w = tk.Toplevel(root)
    w.title("AI漫剧工坊")
    w.geometry("860x640")
    w.transient(root)

    nb = ttk.Notebook(w)
    nb.pack(fill="both", expand=True, padx=8, pady=8)
    for label, builder in MANJU_TABS:
        try:
            builder(nb, root)
        except Exception as e:
            ef = ttk.Frame(nb)
            nb.add(ef, text=label.strip() or "Tab")
            ttk.Label(ef, text="该 Tab 加载失败：%s" % e, foreground="#c00").pack(padx=12, pady=12)


# ----------------------------------------------------------------------------
# Tab1 动态漫剧合成
# ----------------------------------------------------------------------------
def _build_drama_tab(nb, root):
    f = ttk.Frame(nb)
    nb.add(f, text=" 动态漫剧 ")
    f.columnconfigure(0, weight=1)
    _dir = tk.StringVar()
    _dur = tk.DoubleVar(value=3.0)
    _fps = tk.IntVar(value=24)
    _pan = tk.BooleanVar(value=True)
    _scale = tk.DoubleVar(value=1.15)
    _breath = tk.BooleanVar(value=False)
    _out = tk.StringVar(value=os.path.join(
        (AP.WORKDIR if AP else os.getcwd()), "output", "manju_drama"))

    row = ttk.Frame(f)
    row.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
    ttk.Label(row, text="分镜图目录：").pack(side="left")
    ttk.Entry(row, textvariable=_dir, width=36).pack(side="left", padx=4, fill="x", expand=True)
    ttk.Button(row, text="选择", command=lambda: _dir.set(
        filedialog.askdirectory(title="选择分镜图目录"))).pack(side="left")

    p = ttk.LabelFrame(f, text="参数")
    p.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
    p.columnconfigure(2, weight=1)
    _kv(p, "每格时长(秒)", _dur, 0.5, 10, 0.5)
    _kv(p, "帧率", _fps, 12, 30, 1)
    _kv(p, "推镜强度", _scale, 1.02, 1.5, 0.01)
    ttk.Checkbutton(p, text="启用推镜(pan_zoom)", variable=_pan).grid(row=0, column=3, padx=6)
    ttk.Checkbutton(p, text="启用呼吸(breathe)", variable=_breath).grid(row=1, column=3, padx=6)

    orow = ttk.Frame(f)
    orow.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
    ttk.Label(orow, text="输出目录：").pack(side="left")
    ttk.Entry(orow, textvariable=_out, width=36).pack(side="left", padx=4, fill="x", expand=True)
    ttk.Button(orow, text="选择", command=lambda: _out.set(
        filedialog.askdirectory(title="输出目录"))).pack(side="left")

    btns = ttk.Frame(f)
    btns.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
    logbox = scrolledtext.ScrolledText(f, wrap="word", font=("Microsoft YaHei", 9))
    logbox.grid(row=4, column=0, sticky="nsew", padx=8, pady=4)
    f.rowconfigure(4, weight=1)

    def _go():
        d = _dir.get().strip()
        if not d or not os.path.isdir(d):
            messagebox.showwarning("提示", "请先选择分镜图目录")
            return
        imgs = sorted([os.path.join(d, x) for x in os.listdir(d)
                       if x.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))])
        if not imgs:
            messagebox.showwarning("提示", "目录里没有图片")
            return
        if not ensure_ffmpeg():
            messagebox.showwarning("提示", "找不到 ffmpeg（imageio_ffmpeg 缺失）")
            return
        out_dir = _out.get().strip() or os.path.join(d, "drama_out")
        os.makedirs(out_dir, exist_ok=True)
        story = [{"id": "P%d" % i, "image": os.path.basename(p)} for i, p in enumerate(imgs)]
        effects = []
        for i, p in enumerate(imgs):
            eff = []
            if _pan.get():
                eff.append({"type": "pan_zoom", "direction": "in", "scale": _scale.get()})
            if _breath.get():
                eff.append({"type": "breathe", "amplitude": 0.02, "speed": 1.0})
            effects.append({"id": "P%d" % i,
                            "effects": eff,
                            "duration": _dur.get()})
        sb_path = os.path.join(out_dir, "_storyboard.json")
        eff_path = os.path.join(out_dir, "_effects.json")
        json.dump(story, open(sb_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        json.dump(effects, open(eff_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        mp4 = os.path.join(out_dir, "drama.mp4")
        logbox.insert("end", "开始合成，共 %d 张分镜…\n" % len(imgs))

        def _work():
            drama_video.create_drama_video(sb_path, d, eff_path, mp4,
                                          fps=_fps.get(), default_duration=_dur.get())
            root.after(0, lambda: _open_btn(f, mp4))
        _run_thread(root, logbox, _work, "动态漫剧已生成：" + mp4)

    ttk.Button(btns, text="🎬 合成动态漫剧", command=_go).pack(side="left", padx=4)
    ttk.Button(btns, text="📂 打开输出目录", command=lambda: _open_dir(_out.get())).pack(side="left", padx=4)


def _open_dir(path):
    p = os.path.abspath(path or ".")
    if not os.path.isdir(p):
        p = os.path.dirname(p) or "."
    try:
        os.startfile(p)
    except Exception:
        pass


def _open_btn(parent, mp4):
    if os.path.exists(mp4):
        _mp4_ref["last"] = mp4


_mp4_ref = {}


def _kv(parent, label, var, frm, to, res):
    r = parent.grid_size()[1]
    ttk.Label(parent, text=label).grid(row=r, column=0, padx=6, pady=3, sticky="w")
    ttk.Spinbox(parent, textvariable=var, from_=frm, to=to, increment=res,
                width=10).grid(row=r, column=1, padx=6, pady=3, sticky="w")


# ----------------------------------------------------------------------------
# Tab2 条漫长图拼接
# ----------------------------------------------------------------------------
def _build_manga_tab(nb, root):
    f = ttk.Frame(nb)
    nb.add(f, text=" 条漫长图 ")
    f.columnconfigure(0, weight=1)
    _mode = tk.StringVar(value="目录")
    _dir = tk.StringVar()
    _files = tk.StringVar()
    _sb = tk.StringVar()
    _width = tk.IntVar(value=1080)
    _gap = tk.IntVar(value=10)
    _margin = tk.IntVar(value=24)
    _out = tk.StringVar(value=os.path.join(
        (AP.WORKDIR if AP else os.getcwd()), "output", "manju_manga.png"))

    row = ttk.Frame(f)
    row.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
    ttk.Label(row, text="来源：").pack(side="left")
    ttk.Combobox(row, textvariable=_mode, values=["目录", "多文件"], width=10,
                 state="readonly").pack(side="left", padx=4)
    ttk.Entry(row, textvariable=_dir, width=34).pack(side="left", padx=4, fill="x", expand=True)
    ttk.Button(row, text="选目录", command=lambda: _dir.set(
        filedialog.askdirectory(title="选择分镜图目录"))).pack(side="left")
    ttk.Button(row, text="选文件", command=lambda: _files.set(";".join(
        filedialog.askopenfilenames(title="选择分镜图", filetypes=[("图片", "*.png;*.jpg;*.jpeg;*.webp;*.bmp")])))).pack(side="left")

    srow = ttk.Frame(f)
    srow.grid(row=1, column=0, sticky="ew", padx=8, pady=2)
    ttk.Label(srow, text="storyboard.json(可选)：").pack(side="left")
    ttk.Entry(srow, textvariable=_sb, width=34).pack(side="left", padx=4, fill="x", expand=True)
    ttk.Button(srow, text="选择", command=lambda: _sb.set(
        filedialog.askopenfilename(title="选择 storyboard.json", filetypes=[("JSON", "*.json")]))).pack(side="left")

    p = ttk.LabelFrame(f, text="参数")
    p.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
    p.columnconfigure(2, weight=1)
    _kv(p, "长图宽度", _width, 480, 2000, 20)
    _kv(p, "格间距", _gap, 0, 60, 1)
    _kv(p, "左右边距", _margin, 0, 80, 1)

    orow = ttk.Frame(f)
    orow.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
    ttk.Label(orow, text="输出：").pack(side="left")
    ttk.Entry(orow, textvariable=_out, width=34).pack(side="left", padx=4, fill="x", expand=True)
    ttk.Button(orow, text="选择", command=lambda: _out.set(
        filedialog.asksaveasfilename(title="保存长图", defaultextension=".png", filetypes=[("PNG", "*.png")]))).pack(side="left")

    btns = ttk.Frame(f)
    btns.grid(row=4, column=0, sticky="ew", padx=8, pady=4)
    logbox = scrolledtext.ScrolledText(f, wrap="word", font=("Microsoft YaHei", 9))
    logbox.grid(row=5, column=0, sticky="nsew", padx=8, pady=4)
    f.rowconfigure(5, weight=1)

    def _go():
        out = _out.get().strip()
        if not out:
            messagebox.showwarning("提示", "请设置输出路径")
            return
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        sb = _sb.get().strip()
        logbox.insert("end", "开始拼接…\n")

        def _work():
            if sb and os.path.exists(sb):
                d = _dir.get().strip()
                if not d:
                    d = os.path.dirname(sb)
                manga.assemble_manga(sb, d, out, _width.get(), _gap.get(), _margin.get())
            else:
                if _mode.get() == "目录":
                    d = _dir.get().strip()
                    if not d or not os.path.isdir(d):
                        raise RuntimeError("请选择分镜图目录")
                    files = sorted([os.path.join(d, x) for x in os.listdir(d)
                                    if x.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))])
                else:
                    files = [x for x in _files.get().split(";") if x]
                if not files:
                    raise RuntimeError("没有可用的图片")
                manga.assemble_from_images(files, out, _width.get(), _gap.get(), _margin.get())
            root.after(0, lambda: _open_btn(f, out))
        _run_thread(root, logbox, _work, "长图已保存：" + out)

    ttk.Button(btns, text="🖼 拼接长图", command=_go).pack(side="left", padx=4)
    ttk.Button(btns, text="📂 打开输出目录", command=lambda: _open_dir(_out.get())).pack(side="left", padx=4)


# ----------------------------------------------------------------------------
# Tab3 云端视频生成（AI Hive / Seedance）
# ----------------------------------------------------------------------------
def _build_cloud_tab(nb, root):
    f = ttk.Frame(nb)
    nb.add(f, text=" 云端视频 ")
    f.columnconfigure(0, weight=1)
    _key = tk.StringVar()
    _mode = tk.StringVar(value="i2v")
    _prompt = tk.StringVar()
    _imgs = tk.StringVar()
    _out = tk.StringVar(value=os.path.join(
        (AP.WORKDIR if AP else os.getcwd()), "output", "manju_cloud"))
    if AP is not None:
        _key.set(AP.CFG.get("ai_hive_api_key", "") or "")

    info = ttk.Label(f, wraplength=380, justify="left", font=("Microsoft YaHei", 9),
                     foreground="#666")
    info.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
    info.config(text="调用 AI Hive（Seedance 2.5）云端生成视频，需要自己的 sk-api-* 密钥。"
                    "申请：https://ai-hive.iclip.cn/chat　密钥仅保存在本机 config.json。")

    krow = ttk.Frame(f)
    krow.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
    ttk.Label(krow, text="API Key：").pack(side="left")
    ttk.Entry(krow, textvariable=_key, width=34, show="*").pack(side="left", padx=4, fill="x", expand=True)
    ttk.Button(krow, text="保存", command=lambda: _save_key(_key.get())).pack(side="left")

    mrow = ttk.Frame(f)
    mrow.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
    ttk.Label(mrow, text="模式：").pack(side="left")
    ttk.Combobox(mrow, textvariable=_mode, values=["t2v", "i2v", "r2v"], width=10,
                 state="readonly").pack(side="left", padx=4)
    ttk.Label(mrow, text="（t2v 文生视频 / i2v 图生视频 / r2v 参考生视频）").pack(side="left")

    irow = ttk.Frame(f)
    irow.grid(row=3, column=0, sticky="ew", padx=8, pady=2)
    ttk.Label(irow, text="参考图(i2v/r2v，可多张)：").pack(side="left")
    ttk.Entry(irow, textvariable=_imgs, width=34).pack(side="left", padx=4, fill="x", expand=True)
    ttk.Button(irow, text="选择", command=lambda: _imgs.set(";".join(
        filedialog.askopenfilenames(title="选择参考图", filetypes=[("图片", "*.png;*.jpg;*.jpeg;*.webp;*.bmp")])))).pack(side="left")

    prow = ttk.Frame(f)
    prow.grid(row=4, column=0, sticky="ew", padx=8, pady=2)
    ttk.Label(prow, text="提示词：").pack(anchor="w", padx=8)
    prompt_box = scrolledtext.ScrolledText(prow, height=4, wrap="word", font=("Microsoft YaHei", 9))
    prompt_box.pack(fill="x", padx=8, pady=2)

    orow = ttk.Frame(f)
    orow.grid(row=5, column=0, sticky="ew", padx=8, pady=2)
    ttk.Label(orow, text="输出目录：").pack(side="left")
    ttk.Entry(orow, textvariable=_out, width=34).pack(side="left", padx=4, fill="x", expand=True)
    ttk.Button(orow, text="选择", command=lambda: _out.set(
        filedialog.askdirectory(title="输出目录"))).pack(side="left")

    btns = ttk.Frame(f)
    btns.grid(row=6, column=0, sticky="ew", padx=8, pady=4)
    logbox = scrolledtext.ScrolledText(f, wrap="word", font=("Microsoft YaHei", 9))
    logbox.grid(row=7, column=0, sticky="nsew", padx=8, pady=4)
    f.rowconfigure(7, weight=1)

    def _go():
        key = _key.get().strip()
        if not key:
            messagebox.showwarning("提示", "请先填写并保存 API Key")
            return
        if _mode.get() != "t2v" and not _imgs.get().strip():
            messagebox.showwarning("提示", "i2v/r2v 模式需要至少一张参考图")
            return
        prompt = prompt_box.get("1.0", "end").strip()
        if not prompt:
            messagebox.showwarning("提示", "请填写提示词")
            return
        os.makedirs(_out.get().strip() or ".", exist_ok=True)
        _save_key(key)
        logbox.insert("end", "提交云端任务（可能需要几分钟）…\n")

        def _work():
            cfg = cloud.Config(api_key=key)
            client = cloud.AiHiveClient(cfg)
            pmid = cloud.SKILL_CONFIG["models"][_mode.get()]
            model_entry = client.find_model(pmid, "VIDEO")
            pricing = client.get_pricing_snapshot(model_entry, "COST_FIRST")
            img_ids = []
            if _imgs.get().strip():
                for p in _imgs.get().split(";"):
                    p = p.strip()
                    if p:
                        img_ids.append(cloud.upload_media(client, p))
            result = client.generate_video(
                pmid, "COST_FIRST", prompt, pricing,
                image_media_ids=img_ids, video_media_ids=[], audio_media_ids=[])
            task_id = result.get("taskId")
            if not task_id:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            print("任务已提交：taskId = %s" % task_id)
            cloud.poll_task(client, task_id, output_dir=_out.get().strip())
        _run_thread(root, logbox, _work, "云端生成完成（结果见输出目录）")

    ttk.Button(btns, text="☁ 云端生成视频", command=_go).pack(side="left", padx=4)
    ttk.Button(btns, text="📂 打开输出目录", command=lambda: _open_dir(_out.get())).pack(side="left", padx=4)


def _save_key(key):
    if AP is None:
        return
    try:
        AP.CFG["ai_hive_api_key"] = key
        AP.save_config(AP.CFG)
    except Exception as e:
        print("保存密钥失败:", e)


# ----------------------------------------------------------------------------
# 默认 Tab 注册（顺序即显示顺序）。以后加新工具，复制一行 register_manju_tab 即可。
# ----------------------------------------------------------------------------
register_manju_tab(" 🎬 剧情出片 ", _build_story_tab)
register_manju_tab(" 动态漫剧 ", _build_drama_tab)
register_manju_tab(" 条漫长图 ", _build_manga_tab)
register_manju_tab(" 云端视频 ", _build_cloud_tab)


if __name__ == "__main__":
    r = tk.Tk()
    attach_menu(r)
    r.mainloop()
