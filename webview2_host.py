# -*- coding: utf-8 -*-
"""把系统 Edge（WebView2）内核嵌进 Tkinter 的一个 Frame 里。

为什么不用 cefpython / pywebview：
  · cefpython 不支持 Python 3.12，而且要往 exe 里塞 ~150MB 的 Chromium；
  · pywebview 只能自己开窗口，塞不进已经画好的 Tk 界面。
WebView2 用的是系统已有的 Edge WebView2 Runtime（Win11 自带，Win10 大多也推送了），
所以我们只多带一个 166KB 的 WebView2Loader.dll，exe 体积基本不变。

用法：
    host = WebView2Host(root, frame)
    host.ensure_created(lambda: host.navigate("http://127.0.0.1:8188"))
    frame.bind("<Configure>", lambda e: host.fit())     # 跟着父窗口缩放
"""
import os
import sys
import ctypes
import os

# 调试开关：网页默认底色探测的日志（每次 fit 都会打，默认关掉）
_DBG_BG = os.environ.get("MANJU_WV_DEBUG") == "1"
from ctypes import POINTER, byref, c_void_p, c_wchar_p, c_long, wintypes

# 接口 IID 全部取自 Microsoft.Web.WebView2 的 WebView2.h，没有手敲
IID_ENV_HANDLER = "{4e8a3389-c9d8-4bd2-b6b5-124fee6cc14d}"
IID_CTRL_HANDLER = "{6c4819f3-c9b7-4260-8127-c9f5bde7f68c}"
IID_EXEC_HANDLER = "{49511172-cc67-4bca-9923-137112f4c4cc}"

# vtable 索引（IUnknown 的 3 个方法之后开始数）
VT_ENV_CREATE_CONTROLLER = 3      # ICoreWebView2Environment::CreateCoreWebView2Controller
VT_CTRL_PUT_BOUNDS = 6            # ICoreWebView2Controller::put_Bounds
VT_CTRL_PUT_VISIBLE = 4           # ICoreWebView2Controller::put_IsVisible
IID_CTRL2 = "{C979903E-D4CA-4228-92EB-47EE3FA96EAB}"   # ICoreWebView2Controller2
VT_CTRL2_GET_BG = 26              # Controller2::get_DefaultBackgroundColor
VT_CTRL2_PUT_BG = 27              # Controller2::put_DefaultBackgroundColor
VT_CTRL_GET_WEBVIEW = 25          # ICoreWebView2Controller::get_CoreWebView2
VT_CTRL_CLOSE = 24                # ICoreWebView2Controller::Close
VT_WV_NAVIGATE = 5                # ICoreWebView2::Navigate
VT_WV_RELOAD = 31                 # ICoreWebView2::Reload
VT_WV_GO_BACK = 40                # ICoreWebView2::GoBack
VT_WV_GO_FORWARD = 41             # ICoreWebView2::GoForward
VT_WV_STOP = 43                   # ICoreWebView2::Stop
# 下列三个索引是从 Microsoft.Web.WebView2 1.0.2903.40 的 WebView2.h 里按
# ICoreWebView2 的声明顺序数出来的（含 get_Settings / get_Source / get_CanGoBack
# 这几个 propget），跟上面已经在用的 5/31/40/41/43 同一把尺子，不要凭印象改。
VT_WV_EXEC_SCRIPT = 29            # ICoreWebView2::ExecuteScript
VT_WV_ADD_INIT_SCRIPT = 27        # ICoreWebView2::AddScriptToExecuteOnDocumentCreated
VT_WV_POST_JSON = 32              # ICoreWebView2::PostWebMessageAsJson

_RUNTIME_KEY = (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
                r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")


def _hr(v):
    return v & 0xFFFFFFFF


_PYAPI = ctypes.pythonapi
_PYAPI.PyGILState_Ensure.restype = ctypes.c_int
_PYAPI.PyGILState_Release.argtypes = [ctypes.c_int]


def _gil_acquire():
    """WebView2 的回调线程没有 Python 线程状态，进来先拿 GIL。"""
    try:
        return _PYAPI.PyGILState_Ensure()
    except Exception:
        return None


def _gil_release(state):
    if state is not None:
        try:
            _PYAPI.PyGILState_Release(state)
        except Exception:
            pass


class COREWEBVIEW2_COLOR(ctypes.Structure):
    """WebView2 的默认底色（A,R,G,B 各一字节）。"""
    _fields_ = [("A", ctypes.c_ubyte), ("R", ctypes.c_ubyte),
                ("G", ctypes.c_ubyte), ("B", ctypes.c_ubyte)]


class RECT(ctypes.Structure):
    _fields_ = [("left", c_long), ("top", c_long),
                ("right", c_long), ("bottom", c_long)]


def runtime_installed():
    """系统有没有 WebView2 Runtime（没有的话嵌不进去，只能走浏览器打开）。"""
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (_RUNTIME_KEY, _RUNTIME_KEY.replace("WOW6432Node\\", "")):
                try:
                    with winreg.OpenKey(root, sub):
                        return True
                except OSError:
                    continue
    except Exception:
        pass
    # 注册表查不到就退一步看目录在不在
    for d in (r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
              r"C:\Program Files\Microsoft\EdgeWebView\Application"):
        if os.path.isdir(d) and os.listdir(d):
            return True
    return False


def _loader_path():
    arch = "x64" if sys.maxsize > 2 ** 32 else "x86"
    name = "WebView2Loader_%s.dll" % arch
    for base in (getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(__file__)),
                 os.getcwd()):
        if not base:
            continue
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    return None


class WebView2Host(object):
    """一个 Frame 对应一个内嵌浏览器；懒创建，创建完回调通知。"""

    def __init__(self, root, frame, url=None, user_data_dir=None, bg=None,
                 disable_gpu_compositing=None):
        # bg：网页还没画出第一帧时露出的底色。默认 WebView2 是**纯黑**，
        # 窗口从任务栏还原的那一下就会闪一块黑（用户报的就是这个）。
        self.bg = bg or (30, 30, 30)
        # disable_gpu_compositing：关掉 GPU 合成。窗口从任务栏还原时，WebView2 的
        # GPU 合成层会有一帧露出黑色表面（实测还原后 ~150ms 有一帧约 14% 区域变黑）。
        # 关掉 GPU 合成改走软件合成后，这一帧黑就没了。默认开（除非显式传 False）。
        self.disable_gpu_compositing = (disable_gpu_compositing
                                       if disable_gpu_compositing is not None
                                       else True)
        self.root = root
        self.frame = frame
        self.url = url
        self.user_data_dir = user_data_dir
        self._ctrl = None            # ICoreWebView2Controller
        self._wv = None              # ICoreWebView2
        self._keep = []              # 别让 COM 回调对象被回收
        self._creating = False
        self.ready = False
        self.error = ""
        self._last = (0, 0, 0, 0)
        self._last_h = 0

    # ---------------------------------------------------------------- 生命周期
    @classmethod
    def supported(cls):
        if not runtime_installed():
            return False, "系统未安装 Edge WebView2 Runtime"
        if not _loader_path():
            return False, "找不到 WebView2Loader_x64.dll"
        return True, ""

    def ensure_created(self, on_ready=None):
        """第一次调用才真正创建浏览器内核；之后再调用直接回调。"""
        if self.ready:
            if on_ready:
                on_ready()
            return True
        if self._creating or self.error:
            return False
        ok, why = self.supported()
        if not ok:
            self.error = why
            return False

        import comtypes
        from comtypes import GUID, COMMETHOD, HRESULT, IUnknown, COMObject

        comtypes.CoInitialize()
        self._creating = True

        class EnvHandlerCls(IUnknown):
            _iid_ = GUID(IID_ENV_HANDLER)
            _methods_ = [COMMETHOD([], HRESULT, "Invoke",
                                   (["in"], HRESULT, "errorCode"),
                                   (["in"], POINTER(IUnknown), "env"))]

        class CtrlHandlerCls(IUnknown):
            _iid_ = GUID(IID_CTRL_HANDLER)
            _methods_ = [COMMETHOD([], HRESULT, "Invoke",
                                   (["in"], HRESULT, "errorCode"),
                                   (["in"], POINTER(IUnknown), "ctrl"))]

        def vcall(ptr, idx, argtypes, *args):
            vt = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
            fn = ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(vt[idx])
            return fn(ptr, *args)

        hwnd = self.frame.winfo_id()

        class CtrlHandler(COMObject):
            _com_interfaces_ = [CtrlHandlerCls]

            def Invoke(self, errorCode, ctrl):
                # 回调来自 WebView2 自己的线程，那里没有 Python 线程状态，
                # 不先拿 GIL 就会在解释器里直接崩（PyEval_RestoreThread 报错）。
                g = _gil_acquire()
                try:
                    if _hr(errorCode) != 0:
                        self_outer.error = "创建控制器失败 0x%08x" % _hr(errorCode)
                        return 0
                    cp = ctypes.cast(ctrl, c_void_p)
                    self_outer._ctrl = cp
                    out = c_void_p()
                    vcall(cp, VT_CTRL_GET_WEBVIEW, [POINTER(c_void_p)], byref(out))
                    self_outer._wv = out
                    self_outer.ready = True
                    self_outer._creating = False
                except Exception as e:
                    self_outer.error = "控制器回调异常：%r" % (e,)
                    self_outer._creating = False
                finally:
                    _gil_release(g)
                return 0

        class EnvHandler(COMObject):
            _com_interfaces_ = [EnvHandlerCls]

            def Invoke(self, errorCode, env):
                g = _gil_acquire()
                try:
                    if _hr(errorCode) != 0:
                        self_outer.error = "初始化 WebView2 环境失败 0x%08x" % _hr(errorCode)
                        self_outer._creating = False
                        return 0
                    ch = CtrlHandler()
                    self_outer._keep.append(ch)
                    ep = ctypes.cast(env, c_void_p)
                    vcall(ep, VT_ENV_CREATE_CONTROLLER,
                          [wintypes.HWND, c_void_p],
                          hwnd, ch._com_pointers_[GUID(IID_CTRL_HANDLER)])
                except Exception as e:
                    self_outer.error = "环境回调异常：%r" % (e,)
                    self_outer._creating = False
                finally:
                    _gil_release(g)
                return 0

        self_outer = self
        try:
            import comtypes  # noqa: F811
            loader = ctypes.WinDLL(_loader_path())
            fn = loader.CreateCoreWebView2EnvironmentWithOptions
            fn.restype = c_long
            fn.argtypes = [c_wchar_p, c_wchar_p, c_void_p, c_void_p]
            # 关 GPU 合成：在创建环境之前注入浏览器参数。WebView2 创建时会读这个
            # 环境变量，软件合成后窗口还原那一下就不会再露出一帧黑表面。
            if self.disable_gpu_compositing:
                _old = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
                _add = "--disable-gpu-compositing"
                if _add not in _old:
                    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = \
                        (_old + " " + _add).strip()
                print("[wv] 已注入浏览器参数:",
                      os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"], flush=True)
            eh = EnvHandler()
            self._keep.append(eh)
            r = fn(None, self.user_data_dir, None,
                   eh._com_pointers_[GUID(IID_ENV_HANDLER)])
            if _hr(r) != 0:
                self.error = "CreateCoreWebView2EnvironmentWithOptions 失败 0x%08x" % _hr(r)
                self._creating = False
                return False
        except Exception as e:
            self.error = "加载 WebView2 失败：%r" % (e,)
            self._creating = False
            return False
        # 就绪与否由主线程轮询确认：回调线程里不去碰 Tk，避免跨线程炸掉
        self._poll(on_ready)
        return True

    def _poll(self, on_ready, n=0):
        if self.ready:
            if self.url:
                self.navigate(self.url)
            self.fit()
            # 关键：把 WebView2 的「默认底色」设成页面接近色（白）。
            # 否则窗口还原/页面重绘的那一瞬间，浏览器还没画出第一帧，
            # 露出的就是 WebView2 自带的纯黑底 —— 肉眼看到的就是「闪黑」。
            # 设成白底后那一帧只是白一下，和浅色页面几乎分不出，就不闪了。
            try:
                self.set_default_bg()
            except Exception:
                pass
            if on_ready:
                on_ready()
            return
        if self.error or n > 150:          # 最多等约 12 秒
            if not self.error:
                self.error = "WebView2 初始化超时"
            return
        self.root.after(80, lambda: self._poll(on_ready, n + 1))

    # ---------------------------------------------------------------- 操作
    def _vcall(self, ptr, idx, argtypes, *args):
        vt = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(vt[idx])
        return fn(ptr, *args)

    def navigate(self, url):
        self.url = url
        if not self.ready or not self._wv:
            return False
        self._vcall(self._wv.value, VT_WV_NAVIGATE, [c_wchar_p], url)
        return True

    def reload(self):
        if self.ready and self._wv:
            self._vcall(self._wv.value, VT_WV_RELOAD, [])

    def go_back(self):
        if self.ready and self._wv:
            self._vcall(self._wv.value, VT_WV_GO_BACK, [])

    def go_forward(self):
        if self.ready and self._wv:
            self._vcall(self._wv.value, VT_WV_GO_FORWARD, [])

    def stop(self):
        if self.ready and self._wv:
            self._vcall(self._wv.value, VT_WV_STOP, [])

    def eval_js(self, js, callback=None):
        """在页面里跑一段 JS，把返回值（JSON 字符串）交给 callback。

        这是左右打通的关键口子：有了它，左边 Tk 才能真正去改右边画布上的节点。
        完成回调来自 WebView2 自己的工作线程（没有 Python 线程状态），所以只把
        结果存下来，由主线程轮询后回调 —— 跟创建时那套策略一致，不在 COM 线程碰 Tk。
        """
        st = {"done": False, "val": None, "err": ""}

        def fire():
            try:
                if callback:
                    callback(st["val"], st["err"])
            except Exception:
                pass

        if not self.ready or not self._wv:
            st["done"] = True
            st["err"] = "网页内核还没就绪"
            if self.root is not None:
                self.root.after(0, fire)
            else:
                fire()
            return False
        try:
            import comtypes
            from comtypes import GUID, COMMETHOD, HRESULT, IUnknown, COMObject

            class ExecHandlerCls(IUnknown):
                _iid_ = GUID(IID_EXEC_HANDLER)
                _methods_ = [COMMETHOD([], HRESULT, "Invoke",
                                       (["in"], HRESULT, "errorCode"),
                                       (["in"], c_wchar_p, "resultJson"))]

            class ExecHandler(COMObject):
                _com_interfaces_ = [ExecHandlerCls]

                def Invoke(self, errorCode, resultJson):
                    g = _gil_acquire()
                    try:
                        if _hr(errorCode) != 0:
                            st["err"] = "脚本执行失败 0x%08x" % _hr(errorCode)
                        else:
                            st["val"] = resultJson
                        st["done"] = True
                    except Exception as e:
                        st["err"] = "脚本回调异常：%r" % (e,)
                        st["done"] = True
                    finally:
                        _gil_release(g)
                    return 0

            h = ExecHandler()
            self._keep.append(h)      # 回调返回前不能被回收
            r = self._vcall(self._wv.value, VT_WV_EXEC_SCRIPT,
                            [c_wchar_p, c_void_p], js,
                            h._com_pointers_[GUID(IID_EXEC_HANDLER)])
            if _hr(r) != 0:
                st["done"] = True
                st["err"] = "ExecuteScript 调用失败 0x%08x" % _hr(r)
        except Exception as e:
            st["done"] = True
            st["err"] = "注入脚本失败：%r" % (e,)

        if st["done"]:
            self.root.after(0, fire)
            return False

        def pump(n=0):
            if st["done"] or n > 100:          # 最多等约 8 秒
                if not st["done"]:
                    st["err"] = st["err"] or "脚本执行超时"
                fire()
                return
            self.root.after(80, lambda: pump(n + 1))

        pump()
        return True

    def fit(self):
        """把浏览器铺满整个宿主 Frame（父窗口缩放后要调一次）。"""
        if not self.ready or not self._ctrl:
            return
        try:
            w = max(self.frame.winfo_width(), 1)
            h = max(self.frame.winfo_height(), 1)
        except Exception:
            return
        self._last_h = h
        if (0, 0, w, h) == self._last:
            return
        self._last = (0, 0, w, h)
        try:
            self._vcall(self._ctrl, VT_CTRL_PUT_BOUNDS, [RECT], RECT(0, 0, w, h))
        except Exception:
            pass

    def on_restore(self):
        """窗口还原时：重下边界 + 再保险设一次默认底色。

        还原那一帧 WebView2 会重画，可能短暂露出默认黑底。这里先重设边界，
        再调用 set_default_bg() 把底色钉成页面接近色（白），那一帧就只是白一下，
        肉眼不觉得闪。之前试过的 IsVisible 开关和"抖 1px"都会让浏览器重建表面、
        反而把闪黑放大成整块黑，已弃用。
        """
        if not (self.ready and self._ctrl):
            return
        try:
            w = max(int(self.frame.winfo_width()), 1)
            h = max(int(self.frame.winfo_height()), 1)
            self._last = (0, 0, 0, 0)
            self._vcall(self._ctrl, VT_CTRL_PUT_BOUNDS, [RECT], RECT(0, 0, w, h))
            self._last = (0, 0, w, h)
            self._last_h = h
        except Exception:
            pass
        try:
            self.set_default_bg()
        except Exception:
            pass

    def fit_w(self, w):
        """拖动分割条用的轻量版：直接按给定宽度贴边，不读 Tk 的 winfo。

        拖动是高频事件，Tk 的 winfo_width() 会比实际尺寸滞后一帧，读它去设
        WebView2 边界会让右侧原生窗口跟不上左侧 Tk 面板，产生「割裂感」。
        这里用计算出的新宽度直接下发 Bounds，高度用拖动期间不变的缓存值。
        """
        if not self.ready or not self._ctrl:
            return
        try:
            w = int(max(w, 1))
            h = self._last_h if getattr(self, "_last_h", None) else \
                max(self.frame.winfo_height(), 1)
        except Exception:
            return
        if (0, 0, w, h) == self._last:
            return
        self._last = (0, 0, w, h)
        try:
            self._vcall(self._ctrl, VT_CTRL_PUT_BOUNDS, [RECT], RECT(0, 0, w, h))
        except Exception:
            pass

    def set_default_bg(self, rgb=None):
        """把 WebView2 的默认底色改成页面的近似色（不再是纯黑）。

        原理：窗口还原/刚创建时，浏览器还没画出第一帧，露出的就是"默认底色"，
        WebView2 默认是黑色 —— 所以看着就是闪黑。改成面板色/白色后，
        同样的一瞬间只会看到"和页面差不多的颜色"，眼睛就不觉得闪了。
        """
        rgb = rgb or self.bg
        if not self._ctrl:
            return
        try:
            from comtypes import GUID as _G
            iid = _G(IID_CTRL2)
            out = c_void_p()
            # IUnknown::QueryInterface = vtable[0]
            hr = self._vcall(self._ctrl, 0,
                             [POINTER(_G), POINTER(c_void_p)],
                             byref(iid), byref(out))
            if hr != 0 or not out.value:
                print("[wv] QI Controller2 失败 hr=0x%08x" % (hr & 0xFFFFFFFF),
                      flush=True)
                return
            p2 = out.value
            # 先用 get 探一下这一格确实是 DefaultBackgroundColor（S_OK 才继续写）
            probe = COREWEBVIEW2_COLOR()
            hrg = self._vcall(p2, VT_CTRL2_GET_BG,
                              [POINTER(COREWEBVIEW2_COLOR)], byref(probe))
            # 这行每次 fit() 都会打一遍，实测把 studio.log 刷到两千多行
            # （用户在界面日志里根本看不见生成进度）—— 改成调试开关
            if _DBG_BG:
                print("[wv] 默认底色探测 hr=0x%08x 原值=(%d,%d,%d,%d)"
                      % (hrg & 0xFFFFFFFF, probe.A, probe.R, probe.G, probe.B),
                      flush=True)
            if hrg != 0:
                return
            hrm = self._vcall(p2, VT_CTRL2_PUT_BG, [COREWEBVIEW2_COLOR],
                              COREWEBVIEW2_COLOR(255, int(rgb[0]), int(rgb[1]),
                                                 int(rgb[2])))
            if _DBG_BG:
                print("[wv] 默认底色改 %s hr=0x%08x" % (rgb, hrm & 0xFFFFFFFF),
                      flush=True)
        except Exception as e:
            print("[wv] 设置默认底色失败：%r" % (e,), flush=True)

    def set_visible(self, on):
        if self.ready and self._ctrl:
            try:
                self._vcall(self._ctrl, VT_CTRL_PUT_VISIBLE, [ctypes.c_int], 1 if on else 0)
            except Exception:
                pass

    def destroy(self):
        if self.ready and self._ctrl:
            try:
                self._vcall(self._ctrl, VT_CTRL_CLOSE, [])
            except Exception:
                pass
        self.ready = False
        self._ctrl = None
        self._wv = None
        self._keep = []
