# -*- coding: utf-8 -*-
"""界面主题：多套配色 + 自定义 ttk 样式。

用法（在 root = tk.Tk() 之后立刻调一次）：

    import ui_theme as TH
    TH.apply(root, "amber")      # 换主题就换第二个参数
    TH.restyle(root)             # 运行中切换主题后再调一次

设计原则：只让「主要动作」用亮色，其余压低；层次靠明度 + 细描边，
不靠大色块 —— Tk 上大面积饱和色最容易脏。

每套配色只需定义 18 个色值，样式定义只写一份，所以加新主题就是加一段字典。
"""

import tkinter as tk
from tkinter import ttk

# ================================================================ 配色包
PALETTES = {
    # ---------------------------------------------------------- 精致（默认）
    # 底色走暖白瓷，强调色用「雨过天青」的青瓷绿——不抢眼但有质感，
    # 主按钮不再是一块死黑（用户反馈纯黑按钮看着闷）。
    "celadon": dict(
        name="青瓷", desc="暖白瓷底 + 雨过天青，细描边、低饱和，久看不累", light=True,
        BG="#F7F6F2", DEEP="#FFFFFF", PANEL="#EFEDE7",
        FIELD="#FFFFFF", HL="#F4F2EC", ON="#E7E3D9",
        LINE="#DFDACE", LINE_HL="#C3BCAC", SEL="#DCEAE4",
        TEXT="#23272B", TEXT2="#5E656C", TEXT3="#949AA0",
        ACCENT="#2F6F62", ACC_HI="#3D8878", ACC_LO="#245751", ON_ACC="#F8FCFA",
        OK="#1E8A63", ERR="#C0453A", WARN="#A97A1C",
    ),
    # ------------------------------------------------------------ 无色（旧默认）
    "plain": dict(
        name="无色·白", desc="纯中性白底 + 纯黑按钮，不带任何色调", light=True,
        BG="#FBFBFC", DEEP="#FFFFFF", PANEL="#F1F2F4",
        FIELD="#FFFFFF", HL="#F4F5F7", ON="#E9EBEE",
        LINE="#DBDEE4", LINE_HL="#BFC4CC", SEL="#D8DCE3",
        TEXT="#141518", TEXT2="#666B76", TEXT3="#9AA0AA",
        ACCENT="#1A1C20", ACC_HI="#32353C", ACC_LO="#000000", ON_ACC="#FFFFFF",
        OK="#12855C", ERR="#C62828", WARN="#B4740A",
    ),
    # ---------------------------------------------- UE5 / 虚幻编辑器同款
    # 2026-11-01：按用户要求把界面做成虚幻引擎编辑器的停靠布局，配色也照它来。
    # 取色参考 UE5 编辑器暗色主题：窗口底 #1e1e1e、面板 #252526、标题条 #2d2d30、
    # 输入框 #1a1a1a、选中蓝 #094771、强调蓝 #0b7fd4。
    # BTN_R 让 RoundButton 用 3px 小圆角（UE5 的按钮是方的，大圆角会变成 App 风）。
    "unreal": dict(
        name="UE5 暗灰", desc="虚幻引擎编辑器同款炭灰 + 电弧蓝，方块面板、细描边", light=False,
        BG="#1e1e1e", DEEP="#141414", PANEL="#252526",
        FIELD="#1a1a1a", HL="#2d2d30", ON="#3a3a3d",
        LINE="#333333", LINE_HL="#4d4d52", SEL="#094771",
        TEXT="#d6d6d6", TEXT2="#9a9a9a", TEXT3="#6e6e6e",
        ACCENT="#0b7fd4", ACC_HI="#2f9be8", ACC_LO="#075f9e", ON_ACC="#FFFFFF",
        OK="#63d68b", ERR="#ff6b6b", WARN="#e2b93b",
        BTN_R=3,
    ),
    "graphite": dict(
        name="无色·黑", desc="中性炭灰 + 白按钮，无色但压得住暗环境", light=False,
        BG="#1B1C1F", DEEP="#141517", PANEL="#232529",
        FIELD="#2A2C31", HL="#33363C", ON="#3D4047",
        LINE="#34373D", LINE_HL="#4B4F57", SEL="#35383F",
        TEXT="#F1F2F4", TEXT2="#A2A7B0", TEXT3="#70757E",
        ACCENT="#EDEFF2", ACC_HI="#FFFFFF", ACC_LO="#C9CCD2", ON_ACC="#16181B",
        OK="#3DCF95", ERR="#FF6B70", WARN="#E0A93B",
    ),

    # ---------------------------------------------------------- 深色
    "amber": dict(
        name="琥珀暗夜", desc="深灰蓝 + 琥珀金，影视调色板的经典搭配", light=False,
        BG="#15171D", DEEP="#101216", PANEL="#1B1F27",
        FIELD="#22262F", HL="#2C313C", ON="#343A47",
        LINE="#333A47", LINE_HL="#4A5364", SEL="#3B3220",
        TEXT="#E8EAF0", TEXT2="#9AA2B1", TEXT3="#69707E",
        ACCENT="#F0A93B", ACC_HI="#FFC46B", ACC_LO="#C9861F", ON_ACC="#1A1206",
        OK="#3FCF8E", ERR="#FF6B70",
    ),
    "jade": dict(
        name="青玉", desc="墨绿底 + 薄荷青，安静耐看，适合久坐", light=False,
        BG="#101614", DEEP="#0B100E", PANEL="#161E1B",
        FIELD="#1D2723", HL="#26332D", ON="#2E3D35",
        LINE="#2E3D35", LINE_HL="#445A4E", SEL="#1F3A30",
        TEXT="#E4EFE9", TEXT2="#93A99E", TEXT3="#64786D",
        ACCENT="#4FD1A5", ACC_HI="#78E7C0", ACC_LO="#33AB83", ON_ACC="#05130E",
        OK="#4FD1A5", ERR="#FF7C7C",
    ),
    "deepsea": dict(
        name="深海", desc="深蓝底 + 电光蓝，冷静、有科技感", light=False,
        BG="#0E1520", DEEP="#080D15", PANEL="#131C2A",
        FIELD="#1A2538", HL="#223146", ON="#2A3C55",
        LINE="#25344A", LINE_HL="#3A4F6E", SEL="#1B3450",
        TEXT="#E3ECF7", TEXT2="#8DA2BE", TEXT3="#5F7490",
        ACCENT="#4C9EFF", ACC_HI="#7CBCFF", ACC_LO="#2E7DD6", ON_ACC="#04101F",
        OK="#3FE0A8", ERR="#FF6B70",
    ),
    "violet": dict(
        name="紫夜", desc="深紫底 + 丁香紫，偏创作/音乐类氛围", light=False,
        BG="#14111C", DEEP="#0D0A14", PANEL="#1B1726",
        FIELD="#231D31", HL="#2C2540", ON="#362D4C",
        LINE="#2F2740", LINE_HL="#473A62", SEL="#31254A",
        TEXT="#EBE6F6", TEXT2="#A997C4", TEXT3="#6E5F88",
        ACCENT="#B47CFF", ACC_HI="#C99CFF", ACC_LO="#8F55DE", ON_ACC="#0E0618",
        OK="#3FCF8E", ERR="#FF6B70",
    ),
    # ---------------------------------------------------------- 浅色
    "paper": dict(
        name="宣纸", desc="米白底 + 朱砂红，中式、打印出来也好看", light=True,
        BG="#F5F2EC", DEEP="#FFFFFF", PANEL="#EDE8DF",
        FIELD="#FFFFFF", HL="#F2EDE4", ON="#E4DCCD",
        LINE="#DCD5C7", LINE_HL="#C0B6A3", SEL="#FBE3DC",
        TEXT="#2A2722", TEXT2="#6B6357", TEXT3="#9A9184",
        ACCENT="#C0492E", ACC_HI="#DA5C3F", ACC_LO="#9C3820", ON_ACC="#FFF8F2",
        OK="#2E8B62", ERR="#C0392B",
    ),
    "sakura": dict(
        name="樱雾", desc="粉白底 + 樱粉，柔和明亮，适合白天投屏", light=True,
        BG="#FBF6F7", DEEP="#FFFFFF", PANEL="#F6EBEE",
        FIELD="#FFFFFF", HL="#FAEDF0", ON="#F0D8DF",
        LINE="#EEDDE2", LINE_HL="#D9BFC7", SEL="#FCE0E7",
        TEXT="#2E2328", TEXT2="#7A646B", TEXT3="#A08B92",
        ACCENT="#E4567E", ACC_HI="#F5728F", ACC_LO="#C33F64", ON_ACC="#FFFFFF",
        OK="#2E9E74", ERR="#D94352",
    ),

    # ---------------------------------------------------------- 更跳的
    "coral": dict(
        name="珊瑚海", desc="深海蓝绿 + 珊瑚红，撞色但压得住", light=False,
        BG="#0D1A1F", DEEP="#071114", PANEL="#122329",
        FIELD="#172C33", HL="#1F383F", ON="#27454E",
        LINE="#24404A", LINE_HL="#37606E", SEL="#12414A",
        TEXT="#E4F2F5", TEXT2="#8FB0B8", TEXT3="#63838C",
        ACCENT="#FF6B6B", ACC_HI="#FF9090", ACC_LO="#DB4F4F", ON_ACC="#2A0606",
        OK="#35D6B0", ERR="#FF8A8A",
    ),
    "cyber": dict(
        name="赛博霓虹", desc="黑紫底 + 荧光青，夜里最抓眼", light=False,
        BG="#0A0710", DEEP="#05030A", PANEL="#120C1C",
        FIELD="#1A1128", HL="#241736", ON="#2E1D45",
        LINE="#2C1C44", LINE_HL="#4A2E6E", SEL="#35174A",
        TEXT="#F0E9FF", TEXT2="#A992C9", TEXT3="#6E5A8A",
        ACCENT="#00E5C0", ACC_HI="#3CFFD8", ACC_LO="#00B89A", ON_ACC="#00110D",
        OK="#00E5C0", ERR="#FF4D8D",
    ),
    "sunset": dict(
        name="日落", desc="暖棕底 + 落日橙，温度最高的一套", light=False,
        BG="#1A1210", DEEP="#130C0A", PANEL="#241816",
        FIELD="#2E1E1A", HL="#3B2721", ON="#492F27",
        LINE="#43302A", LINE_HL="#61453A", SEL="#4A2A20",
        TEXT="#FBEDE6", TEXT2="#C4A497", TEXT3="#8C7266",
        ACCENT="#FF7A3D", ACC_HI="#FF9A66", ACC_LO="#DB5F26", ON_ACC="#1F0A02",
        OK="#4FD1A5", ERR="#FF5D62",
    ),
    "matrix": dict(
        name="荧光黑", desc="近黑 + 荧光绿，极客 / 代码流", light=False,
        BG="#0A0F0A", DEEP="#050A05", PANEL="#0F160F",
        FIELD="#141D14", HL="#1C281C", ON="#243224",
        LINE="#24331F", LINE_HL="#3A5030", SEL="#1E3A12",
        TEXT="#D8F5D0", TEXT2="#8FB585", TEXT3="#5F7F58",
        ACCENT="#7CFF3D", ACC_HI="#A6FF75", ACC_LO="#5CD41C", ON_ACC="#0A1602",
        OK="#7CFF3D", ERR="#FF5D62",
    ),
    "mint": dict(
        name="薄荷汽水", desc="浅薄荷底 + 青绿，清爽、夏天感", light=True,
        BG="#F2FBF8", DEEP="#FFFFFF", PANEL="#E3F5EF",
        FIELD="#FFFFFF", HL="#E9F8F3", ON="#D5EFE7",
        LINE="#CDE8DF", LINE_HL="#A9D6C8", SEL="#CFF0E5",
        TEXT="#12332B", TEXT2="#4E7768", TEXT3="#86A99C",
        ACCENT="#00A882", ACC_HI="#12C79C", ACC_LO="#00876A", ON_ACC="#FFFFFF",
        OK="#00A882", ERR="#D94352",
    ),
    "lemon": dict(
        name="柠檬糖", desc="米黄底 + 柠檬橙，明亮又暖", light=True,
        BG="#FFFDF2", DEEP="#FFFFFF", PANEL="#FBF5DC",
        FIELD="#FFFFFF", HL="#FDF8E8", ON="#F7EFC9",
        LINE="#EDE4C2", LINE_HL="#D8CA9B", SEL="#FBF0C0",
        TEXT="#3A3220", TEXT2="#7D7150", TEXT3="#A89A72",
        ACCENT="#E0A100", ACC_HI="#F5BA1A", ACC_LO="#B88400", ON_ACC="#2A1E00",
        OK="#2E9E74", ERR="#D94352",
    ),
}

DEFAULT = "celadon"        # 默认「青瓷」：暖白底 + 青瓷绿，不用死黑按钮
ORDER = ["unreal", "celadon", "plain", "graphite", "amber", "jade", "deepsea", "violet",
         "coral", "cyber", "sunset", "matrix", "paper", "sakura",
         "mint", "lemon"]

_current = PALETTES[DEFAULT]
_prev_palette = {}          # 上一次 _publish 之前的那套：切主题时用来反查控件现在是什么色

# 下面这些名字会被 apply() 动态写成本套配色的值，主程序直接用 TH.TEXT 之类即可
BG = BG_DEEP = PANEL = FIELD = FIELD_HL = FIELD_ON = LINE = LINE_HL = ""
# HL / ON 是 FIELD_HL / FIELD_ON 的短别名：UE5 布局的面板标题条、工具栏用它们
HL = FIELD_HL
ON = FIELD_ON
TEXT = TEXT2 = TEXT3 = ACCENT = ACCENT_HI = ACCENT_LO = ON_ACCENT = ""
OK = WARN = ERR = SEL_BG = ""
IS_LIGHT = False

# ---------------------------------------------------------------- 字体
_UI = "Microsoft YaHei"
FONT = (_UI, 10)
FONT_B = (_UI, 10, "bold")
FONT_S = (_UI, 9)
FONT_XS = (_UI, 8)
FONT_H = (_UI, 16, "bold")
FONT_MONO = ("Consolas", 9)


def palette_list():
    """给设置窗口用：[(key, 显示名, 说明, 是否浅色), ...]"""
    return [(k, PALETTES[k]["name"], PALETTES[k]["desc"], PALETTES[k]["light"])
            for k in ORDER if k in PALETTES]


def current_key():
    return _current.get("_key", DEFAULT)


def _publish(key):
    """把配色写进模块全局，主程序用 TH.TEXT 读到的就是当前主题的颜色。"""
    global _current, BG, BG_DEEP, PANEL, FIELD, FIELD_HL, FIELD_ON
    global LINE, LINE_HL, TEXT, TEXT2, TEXT3, ACCENT, ACCENT_HI, ACCENT_LO
    global HL, ON
    global ON_ACCENT, OK, WARN, ERR, SEL_BG, IS_LIGHT, STATUS
    global _prev_palette
    _prev_palette = dict(_current) if _current else {}
    p = dict(PALETTES.get(key) or PALETTES[DEFAULT])
    p["_key"] = key if key in PALETTES else DEFAULT
    _current = p
    BG, BG_DEEP, PANEL = p["BG"], p["DEEP"], p["PANEL"]
    FIELD, FIELD_HL, FIELD_ON = p["FIELD"], p["HL"], p["ON"]
    HL, ON = p["HL"], p["ON"]
    LINE, LINE_HL, SEL_BG = p["LINE"], p["LINE_HL"], p["SEL"]
    TEXT, TEXT2, TEXT3 = p["TEXT"], p["TEXT2"], p["TEXT3"]
    ACCENT, ACCENT_HI, ACCENT_LO = p["ACCENT"], p["ACC_HI"], p["ACC_LO"]
    ON_ACCENT = p["ON_ACC"]
    OK, ERR = p["OK"], p["ERR"]
    # 无彩色主题的 ACCENT 是黑白，状态橙要单独给，否则「运行中」会变灰
    WARN = p.get("WARN") or p["ACCENT"]
    IS_LIGHT = p["light"]
    STATUS = {"green": OK, "red": ERR, "gray": TEXT2, "orange": WARN}
    return p


STATUS = {"green": "#3FCF8E", "red": "#FF6B70", "gray": "#9AA2B1", "orange": "#F0A93B"}


def _safe_cfg(st, style, **kw):
    """逐个设置，遇到当前主题不支持的选项就跳过（不同 ttk 主题支持项不一样）。"""
    for k, v in kw.items():
        try:
            st.configure(style, **{k: v})
        except Exception:
            pass


def _safe_map(st, style, **kw):
    for k, v in kw.items():
        try:
            st.map(style, **{k: v})
        except Exception:
            pass


# ---------------------------------------------------------------- 原生控件
def _dark_titlebar(win):
    """Windows 标题栏跟随主题深浅（Win10 1809+）。"""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            return
        val = ctypes.c_int(1 if not IS_LIGHT else 0)
        for attr in (20, 19):          # DWMWA_USE_IMMERSIVE_DARK_MODE
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr,
                                                       ctypes.byref(val), 4)
    except Exception:
        pass


def paint_toplevel(win):
    try:
        win.configure(background=BG)
    except Exception:
        pass
    _dark_titlebar(win)


def paint_text(widget, bg=None, fg=None, pad=(8, 6)):
    """给 tk.Text / ScrolledText 上色（含光标色、选区色、细描边）。"""
    px, py = pad
    try:
        widget.configure(
            background=bg or BG_DEEP,
            foreground=fg or TEXT,
            insertbackground=ACCENT,
            selectbackground=ACCENT,
            selectforeground=ON_ACCENT,
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=ACCENT,
            relief="flat", borderwidth=0, padx=px, pady=py,
        )
    except Exception:
        pass


def style_menu(menu):
    try:
        menu.configure(background=FIELD, foreground=TEXT,
                       activebackground=ACCENT, activeforeground=ON_ACCENT,
                       disabledforeground=TEXT3,
                       borderwidth=0, relief="flat", selectcolor=ACCENT)
    except Exception:
        pass


# ---------------------------------------------------------------- 主入口
def apply(root, key=None):
    """套用配色 + 建全套样式。key 为空则沿用上一次（默认琥珀暗夜）。"""
    p = _publish(key or current_key())
    light = p["light"]

    root.configure(background=BG)

    # ---- tk 原生控件默认色（对之后创建的控件生效）
    root.option_add("*Font", FONT)
    root.option_add("*Background", BG)
    root.option_add("*Foreground", TEXT)
    root.option_add("*Text.background", BG_DEEP)
    root.option_add("*Text.foreground", TEXT)
    root.option_add("*Text.insertBackground", ACCENT)
    root.option_add("*Text.selectBackground", ACCENT)
    root.option_add("*Text.selectForeground", ON_ACCENT)
    root.option_add("*Text.highlightThickness", 1)
    root.option_add("*Text.highlightBackground", LINE)
    root.option_add("*Text.highlightColor", ACCENT)
    root.option_add("*Text.relief", "flat")
    root.option_add("*Text.borderWidth", 0)
    root.option_add("*Canvas.background", BG)
    root.option_add("*Canvas.highlightThickness", 0)
    root.option_add("*Frame.background", BG)
    root.option_add("*Label.background", BG)
    root.option_add("*Label.foreground", TEXT)
    # ScrolledText 里面那个滚动条是原生 tk.Scrollbar，只能这么压平
    root.option_add("*Scrollbar.background", FIELD_HL)
    root.option_add("*Scrollbar.troughColor", BG_DEEP)
    root.option_add("*Scrollbar.activeBackground", LINE_HL)
    root.option_add("*Scrollbar.highlightThickness", 0)
    root.option_add("*Scrollbar.borderWidth", 0)
    root.option_add("*Scrollbar.relief", "flat")
    root.option_add("*Scrollbar.width", 10)
    root.option_add("*Menu.background", FIELD)
    root.option_add("*Menu.foreground", TEXT)
    root.option_add("*Menu.activeBackground", ACCENT)
    root.option_add("*Menu.activeForeground", ON_ACCENT)
    root.option_add("*Menu.selectColor", ACCENT)
    root.option_add("*Menu.borderWidth", 0)
    root.option_add("*Menu.activeBorderWidth", 0)
    # 下拉框弹出的列表是原生 Listbox，得单独喂色
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", ON_ACCENT)
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    st = ttk.Style(root)
    try:
        st.theme_use("clam")      # vista/xpnative 不认自定义底色，必须换 clam
    except Exception:
        pass

    # ---- 全局默认
    _safe_cfg(st, ".",
              background=BG, foreground=TEXT, fieldbackground=FIELD,
              bordercolor=LINE, lightcolor=FIELD, darkcolor=FIELD,
              troughcolor=BG_DEEP, focuscolor=ACCENT,
              selectbackground=ACCENT, selectforeground=ON_ACCENT,
              insertcolor=ACCENT, font=FONT)

    # ---- 容器 / 文字
    _safe_cfg(st, "TFrame", background=BG, borderwidth=0)
    _safe_cfg(st, "TLabel", background=BG, foreground=TEXT)
    _safe_cfg(st, "Muted.TLabel", background=BG, foreground=TEXT2, font=FONT_S)
    _safe_cfg(st, "Hint.TLabel", background=BG, foreground=TEXT3, font=FONT_XS)
    _safe_cfg(st, "H1.TLabel", background=BG, foreground=TEXT, font=FONT_H)
    _safe_cfg(st, "H2.TLabel", background=BG, foreground=TEXT, font=(_UI, 11, "bold"))
    _safe_cfg(st, "Accent.TLabel", background=BG, foreground=ACCENT, font=FONT_B)
    _safe_cfg(st, "Badge.TLabel", background=FIELD, foreground=ACCENT,
              font=FONT_B, padding=(8, 4), anchor="center")
    _safe_cfg(st, "Status.TLabel", background=PANEL, foreground=TEXT2,
              font=FONT_B, padding=(12, 5))

    # 分组框：细描边 + 强调色标题（Tk 里实心卡片容易出色块，这样最干净）
    _safe_cfg(st, "TLabelframe", background=BG, bordercolor=LINE,
              borderwidth=1, relief="solid")
    _safe_cfg(st, "TLabelframe.Label", background=BG, foreground=ACCENT,
              font=(_UI, 10, "bold"))

    # ---- 按钮：三种语义
    _safe_cfg(st, "TButton", background=FIELD, foreground=TEXT,
              bordercolor=LINE, lightcolor=FIELD, darkcolor=FIELD,
              focuscolor=FIELD, relief="flat", borderwidth=1,
              padding=(12, 6), font=FONT, anchor="center")
    _safe_map(st, "TButton",
              background=[("disabled", PANEL), ("pressed", FIELD_ON),
                          ("active", FIELD_HL)],
              foreground=[("disabled", TEXT3)],
              bordercolor=[("active", LINE_HL)],
              lightcolor=[("active", FIELD_HL)], darkcolor=[("active", FIELD_HL)])

    # 主动作：实心强调色
    _safe_cfg(st, "Accent.TButton", background=ACCENT, foreground=ON_ACCENT,
              bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
              focuscolor=ACCENT, relief="flat", borderwidth=1,
              padding=(16, 7), font=FONT_B)
    _safe_map(st, "Accent.TButton",
              background=[("disabled", PANEL), ("pressed", ACCENT_LO),
                          ("active", ACCENT_HI)],
              foreground=[("disabled", TEXT3)],
              bordercolor=[("active", ACCENT_HI)],
              lightcolor=[("active", ACCENT_HI)], darkcolor=[("active", ACCENT_HI)])

    # 危险：只给红字描边
    _safe_cfg(st, "Danger.TButton", background=BG, foreground=ERR,
              bordercolor=LINE, lightcolor=BG, darkcolor=BG,
              focuscolor=BG, relief="flat", borderwidth=1, padding=(12, 6), font=FONT)
    _safe_map(st, "Danger.TButton",
              background=[("disabled", PANEL), ("active", FIELD)],
              foreground=[("disabled", TEXT3)],
              bordercolor=[("active", LINE_HL)])

    # 轻按钮：无底色，只有文字（说明 / 设置 这类）
    _safe_cfg(st, "Quiet.TButton", background=BG, foreground=TEXT2,
              bordercolor=BG, lightcolor=BG, darkcolor=BG,
              focuscolor=BG, relief="flat", borderwidth=1, padding=(10, 5), font=FONT)
    _safe_map(st, "Quiet.TButton",
              background=[("disabled", BG), ("active", FIELD)],
              foreground=[("disabled", TEXT3), ("active", ACCENT)],
              lightcolor=[("active", FIELD)], darkcolor=[("active", FIELD)])

    # ---- 输入框
    _safe_cfg(st, "TEntry", fieldbackground=FIELD, foreground=TEXT,
              bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
              insertcolor=ACCENT, padding=(8, 5), relief="flat", borderwidth=1)
    _safe_map(st, "TEntry",
              fieldbackground=[("disabled", PANEL), ("focus", FIELD_HL)],
              foreground=[("disabled", TEXT3)],
              bordercolor=[("focus", ACCENT), ("active", LINE_HL)])
    # 占位灰字：用样式切换而不是 widget.config(foreground=)，换主题才能跟着走
    _safe_cfg(st, "Placeholder.TEntry", fieldbackground=FIELD, foreground=TEXT3,
              bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
              insertcolor=ACCENT, padding=(8, 5), relief="flat", borderwidth=1)
    _safe_map(st, "Placeholder.TEntry",
              foreground=[("focus", TEXT3)],
              fieldbackground=[("focus", FIELD_HL)],
              bordercolor=[("focus", ACCENT)])

    _safe_cfg(st, "TCombobox", fieldbackground=FIELD, background=FIELD,
              foreground=TEXT, arrowcolor=TEXT2, arrowsize=14,
              bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
              padding=(8, 5), relief="flat", borderwidth=1)
    _safe_map(st, "TCombobox",
              fieldbackground=[("readonly", FIELD), ("focus", FIELD_HL)],
              background=[("readonly", FIELD), ("active", FIELD_HL)],
              foreground=[("readonly", TEXT)],
              arrowcolor=[("active", ACCENT), ("readonly", TEXT2)],
              selectbackground=[("readonly", FIELD)],
              selectforeground=[("readonly", TEXT)],
              bordercolor=[("focus", ACCENT), ("active", LINE_HL)])

    _safe_cfg(st, "TSpinbox", fieldbackground=FIELD, background=FIELD,
              foreground=TEXT, arrowcolor=TEXT2, bordercolor=LINE,
              insertcolor=ACCENT, padding=(8, 4))
    _safe_map(st, "TSpinbox",
              fieldbackground=[("readonly", FIELD)],
              foreground=[("readonly", TEXT)])

    # ---- 勾选框
    _safe_cfg(st, "TCheckbutton", background=BG, foreground=TEXT,
              indicatorbackground=FIELD, indicatorforeground=ACCENT,
              bordercolor=LINE, focuscolor=BG, padding=(2, 3), font=FONT)
    _safe_map(st, "TCheckbutton",
              background=[("active", BG)],
              foreground=[("active", TEXT), ("disabled", TEXT3)],
              indicatorbackground=[("selected", ACCENT), ("active", FIELD_HL)],
              indicatorforeground=[("selected", ON_ACCENT)])

    # ---- 进度条：加厚 + 强调色
    _safe_cfg(st, "Horizontal.TProgressbar", troughcolor=PANEL,
              background=ACCENT, bordercolor=LINE,
              lightcolor=ACCENT, darkcolor=ACCENT,
              thickness=12, borderwidth=1)
    _safe_cfg(st, "TProgressbar", troughcolor=PANEL, background=ACCENT,
              bordercolor=LINE, lightcolor=ACCENT, darkcolor=ACCENT,
              thickness=12, borderwidth=1)

    # ---- 滚动条：细、无凸起
    _safe_cfg(st, "Vertical.TScrollbar", background=FIELD_HL, troughcolor=BG_DEEP,
              bordercolor=BG_DEEP, arrowcolor=TEXT3, relief="flat",
              borderwidth=0, arrowsize=12, width=11)
    _safe_map(st, "Vertical.TScrollbar",
              background=[("active", LINE_HL), ("pressed", ACCENT)])
    _safe_cfg(st, "Horizontal.TScrollbar", background=FIELD_HL, troughcolor=BG_DEEP,
              bordercolor=BG_DEEP, arrowcolor=TEXT3, relief="flat", borderwidth=0)

    # ---- 表格（模型管理）
    _safe_cfg(st, "Treeview", background=BG_DEEP, fieldbackground=BG_DEEP,
              foreground=TEXT, bordercolor=LINE, borderwidth=0,
              rowheight=26, font=FONT_S)
    _safe_cfg(st, "Treeview.Heading", background=FIELD, foreground=TEXT2,
              relief="flat", borderwidth=0, padding=(6, 6), font=FONT_B)
    _safe_map(st, "Treeview.Heading", background=[("active", FIELD_HL)],
              foreground=[("active", ACCENT)])
    _safe_map(st, "Treeview",
              background=[("selected", SEL_BG)],
              foreground=[("selected", ACCENT)])

    # ---- 标签页（模型管理）
    _safe_cfg(st, "TNotebook", background=BG, bordercolor=LINE,
              borderwidth=0, tabmargins=(2, 6, 2, 0))
    _safe_cfg(st, "TNotebook.Tab", background=PANEL, foreground=TEXT2,
              bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL,
              padding=(16, 7), font=FONT)
    _safe_map(st, "TNotebook.Tab",
              background=[("selected", FIELD)], foreground=[("selected", ACCENT)],
              lightcolor=[("selected", FIELD)], darkcolor=[("selected", FIELD)])

    _safe_cfg(st, "TSeparator", background=LINE)

    try:
        root.after(60, lambda: _dark_titlebar(root))
    except Exception:
        pass
    return st



# ================================================================ 圆角按钮
# ttk 的按钮在 clam 主题下永远是直角方块，想做出「按钮」的质感只能自己画。
# 这里用 Canvas 画圆角矩形，补齐 hover / 按下 / 禁用 / 焦点反馈，
# 对外接口尽量跟 ttk.Button 一致（config(state=...)、config(text=...)）。
def _col(name, fallback="#808080"):
    return globals().get(name, fallback)


def _round_rect(cv, x1, y1, x2, y2, r, steps=12, **kw):
    """真正的圆角矩形：四个角按圆弧逐点采样。

    不能用 Tk 的 smooth=True —— 它是对多边形做样条平滑，半径一大圆角就被
    "抽"成歪的曲线，看着像切角。这里点足够密，直接连成多边形就是标准圆弧。
    """
    import math
    r = max(0.0, min(float(r), (x2 - x1) / 2, (y2 - y1) / 2))
    if r < 0.5:
        return cv.create_rectangle(x1, y1, x2, y2, **kw)
    pts = []
    for cx, cy, a0, a1 in ((x2 - r, y1 + r, -90.0, 0.0),     # 右上
                           (x2 - r, y2 - r, 0.0, 90.0),      # 右下
                           (x1 + r, y2 - r, 90.0, 180.0),    # 左下
                           (x1 + r, y1 + r, 180.0, 270.0)):  # 左上
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return cv.create_polygon(pts, **kw)


class RoundButton(tk.Canvas):
    """圆角按钮。kind: accent(主) / normal(次) / danger(危险) / quiet(无底色)。"""

    _all = []

    KINDS = {
        "accent": ("ACCENT", "ON_ACCENT", "ACCENT_HI", "ACCENT_LO", "ACCENT"),
        "normal": ("FIELD", "TEXT", "FIELD_HL", "FIELD_ON", "LINE_HL"),
        "danger": ("BG", "ERR", "FIELD", "FIELD_ON", "LINE_HL"),
        "quiet":  ("BG", "TEXT2", "FIELD", "FIELD_ON", "BG"),
    }

    def __init__(self, master, text="", command=None, kind="normal",
                 padx=16, pady=7, radius=None, font=None, width=None, height=None,
                 state="normal", icon=None, gap=5, **kw):
        from tkinter import font as tkfont
        f = tkfont.Font(font=font or FONT)
        # emoji 必须单独用一个字体画：Canvas 的 create_text 不像 Label 那样
        # 会自动 fallback，混在正文里会变成黑白符号。
        ef = tkfont.Font(family="Segoe UI Emoji", size=f.cget("size"))
        iw = ef.measure(icon) if icon else 0
        tw = f.measure(text)
        w = width or (iw + (gap if icon else 0) + tw + padx * 2 + 2)
        h = height or max(30, f.metrics("linespace") + pady * 2)
        super().__init__(master, width=w, height=h, highlightthickness=0,
                         bd=0, bg=_col("BG"), cursor="hand2", **kw)
        self._label, self._cmd, self._kind = text, command, kind
        self._font, self._efont = f, ef
        self._padx = padx          # 改文字时要用它重算宽度，否则长文字会被裁掉
        self._icon, self._gap = icon, gap
        # 圆角半径 = 高度 × 0.45，且不小于 14px —— 参照 App 图标那种圆角：
        # 弧要够大才看得出"圆"，小尺寸按钮（28-36 高）尤其明显。
        # 0.45 仍保留一小段直边，不会变成纯胶囊。传了值就按值，上限 h/2。
        self._bw, self._bh = w, h
        # 圆角半径：默认 = 高度 × 0.45（不小于 14px，App 图标那种大圆角）。
        # 但 UE5 主题在配色里给了 BTN_R（如 3），那种主题要的是**方角**按钮 ——
        # 暗色 + 大圆角在这套布局里会显得像手机 App，和虚幻编辑器的方块面板不搭。
        if radius is not None:
            self._br = min(float(radius), h / 2.0)
        else:
            _rcfg = _current.get("BTN_R")
            if _rcfg is not None:
                self._br = min(float(_rcfg), h / 2.0)
            else:
                self._br = min(max(14.0, h * 0.45), h / 2.0)
        self._state = state
        self._hover = self._press = False
        self._wid = None
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        RoundButton._all.append(self)
        self.refresh()

    # ---- 绘制
    def refresh(self):
        if not self.winfo_exists():
            return
        fk, gk, hk, pk, bk = self.KINDS.get(self._kind, self.KINDS["normal"])
        try:
            # 注意：这里必须用 tk.Canvas.configure，不能写 self.configure ——
            # 后者会再进 refresh()，递归到爆栈、窗口直接卡死。
            tk.Canvas.configure(self, bg=_col("BG"))
        except Exception:
            pass
        if self._state == "disabled":
            fill, fg, bd = _col("PANEL"), _col("TEXT3"), _col("PANEL")
        elif self._press:
            fill, fg, bd = _col(pk), _col(gk), _col(bk)
        elif self._hover:
            fill, fg, bd = _col(hk), _col(gk), _col(bk)
        else:
            fill, fg, bd = _col(fk), _col(gk), _col(bk)
        self.delete("all")
        # 有边框的按钮描边稍微加粗：1px 在圆弧上会被抗锯齿"吃"成断线
        ow = 1.4 if self._kind in ("normal", "danger") else 1
        _round_rect(self, 0.5, 0.5, self._bw - 0.5, self._bh - 0.5, self._br,
                    fill=fill, outline=bd, width=ow)
        # 按下时文字下沉 1px，比单纯变色更有「按到」的实感
        cy = self._bh / 2 + (1 if self._press else 0)
        iw = self._efont.measure(self._icon) if self._icon else 0
        tw = self._font.measure(self._label)
        gap = self._gap if self._icon else 0
        x = (self._bw - (iw + gap + tw)) / 2
        if self._icon:
            self.create_text(x, cy + 1, text=self._icon, anchor="w",
                             fill=fg, font=self._efont)
        self._wid = self.create_text(x + iw + gap, cy, text=self._label,
                                     anchor="w", fill=fg, font=self._font)

    # ---- 交互
    def _on_enter(self, _=None):
        self._hover = True
        if self._state != "disabled":
            try:
                tk.Canvas.configure(self, cursor="hand2")
            except Exception:
                pass
        self.refresh()

    def _on_leave(self, _=None):
        self._hover = self._press = False
        self.refresh()

    def _on_press(self, _=None):
        if self._state == "disabled":
            return
        self._press = True
        self.refresh()

    def _on_release(self, e=None):
        if self._state == "disabled":
            return
        was = self._press
        self._press = False
        inside = True
        if e is not None:
            inside = 0 <= e.x <= self._bw and 0 <= e.y <= self._bh
        self.refresh()
        if was and inside and callable(self._cmd):
            self._cmd()

    # ---- 兼容 ttk.Button 的调用方式
    def configure(self, *args, **kw):
        if args:
            return super().configure(*args, **kw)
        if "state" in kw:
            self._state = kw.pop("state")
            try:
                tk.Canvas.configure(self, cursor="arrow" if self._state == "disabled"
                                    else "hand2")
            except Exception:
                pass
        if "text" in kw:
            self._label = kw.pop("text")
            # 文字变长/变短都要重算画布宽度：本类是 Canvas 自绘，
            # 只换 _label 不重量宽的话，「开始生成」这种长字会被右边缘裁掉。
            try:
                tw = self._font.measure(self._label)
                iw = self._efont.measure(self._icon) if self._icon else 0
                gap = self._gap if self._icon else 0
                self._bw = iw + gap + tw + self._padx * 2 + 2
                tk.Canvas.configure(self, width=self._bw)
            except Exception:
                pass
        if "command" in kw:
            self._cmd = kw.pop("command")
        if kw:
            try:
                super().configure(**kw)
            except Exception:
                pass
        self.refresh()
        return None

    config = configure


def refresh_buttons():
    """换主题后把所有圆角按钮重画一遍。"""
    for b in list(RoundButton._all):
        try:
            if b.winfo_exists():
                b.refresh()
        except Exception:
            pass


def _make_remap(old, new):
    """旧配色色值 → 角色名 → 新配色色值。

    主题包里角色名（BG/PANEL/FIELD…）是固定的，所以只要知道某个控件现在
    用的是旧主题的哪个颜色，就能换成新主题对应的那个。这比给每个控件记
    「我是什么角色」省事得多 —— 那要改几百处创建代码。
    """
    rev = {}
    for role, val in (old or {}).items():
        if isinstance(val, str) and val.startswith("#") and len(val) == 7:
            rev.setdefault(val.upper(), role)
    if not rev:
        return None

    def remap(c):
        if not isinstance(c, str) or len(c) != 7 or not c.startswith("#"):
            return None
        role = rev.get(c.upper())
        if not role:
            return None
        return new.get(role)
    return remap


_RECOLOR_OPTS = {
    "Label": ("background", "foreground"),
    "Button": ("background", "foreground", "highlightbackground"),
    "Checkbutton": ("background", "foreground", "highlightbackground"),
    "Radiobutton": ("background", "foreground", "highlightbackground"),
    "Entry": ("background", "foreground", "highlightbackground"),
    "Listbox": ("background", "foreground"),
    "Frame": ("background", "highlightbackground"),
    "Labelframe": ("background", "foreground"),
    "Toplevel": ("background",),
    "Canvas": ("background", "highlightbackground"),
}


def _recolor(w, remap, cls):
    if remap is None:
        return
    for opt in _RECOLOR_OPTS.get(cls, ()):
        try:
            cur = w.cget(opt)
        except Exception:
            continue
        nv = remap(cur)
        if not nv:
            continue
        try:
            w.configure(**{opt: nv})
        except Exception:
            pass


def _recolor_canvas_items(cv, remap):
    """Canvas 里手画的线/矩形也要换色。

    节点图、进度条、状态点这些是 create_line/create_rectangle 画出来的，
    控件本身换个底色没用 —— 得连 item 的 fill/outline 一起换。
    认不出来的颜色（比如节点卡自带的紫/绿）原样留着。
    """
    if remap is None:
        return
    try:
        items = cv.find_all()
    except Exception:
        return
    for it in items:
        for opt in ("fill", "outline"):
            try:
                cur = cv.itemcget(it, opt)
            except Exception:
                continue
            nv = remap(cur)
            if not nv:
                continue
            try:
                cv.itemconfigure(it, **{opt: nv})
            except Exception:
                pass


def restyle(root):
    """运行中切换主题：把已经建好的控件重刷一遍（ttk 会自动跟样式）。"""
    # 先抓住"上一套"：paint() 里可能已经 apply 过新配色了，这里再 apply 会把
    # _prev_palette 覆盖成新主题，反查就退化成恒等变换。
    old = dict(_prev_palette or {})
    if old.get("_key") == current_key():
        old = {}
    apply(root, current_key())

    refresh_buttons()
    remap = _make_remap(old, _current)

    def walk(w):
        try:
            cls = w.winfo_class()
        except Exception:
            return
        try:
            if isinstance(w, RoundButton):
                w.refresh()
            elif isinstance(w, tk.Text):
                paint_text(w)
                _recolor(w, remap, "Frame")
            elif isinstance(w, tk.Canvas):
                w.configure(highlightthickness=0)
                _recolor(w, remap, "Canvas")
                _recolor_canvas_items(w, remap)
            elif isinstance(w, tk.Toplevel):
                paint_toplevel(w)
            elif isinstance(w, tk.Menu):
                style_menu(w)
            else:
                _recolor(w, remap, cls)
        except Exception:
            pass
        try:
            kids = w.winfo_children()
        except Exception:
            return
        for c in kids:
            walk(c)

    walk(root)
    root.configure(background=BG)


def preview_palette(key, w=190, h=64):
    """给设置窗口画一个配色小样（返回 Canvas，调用方自己 pack）。"""
    p = PALETTES.get(key) or PALETTES[DEFAULT]
    cv = tk.Canvas(w, height=h, highlightthickness=0, bd=0,
                   background=p["BG"], width=w)
    cv.create_rectangle(0, 0, w - 1, h - 1, fill=p["BG"], outline=p["LINE"])
    cv.create_rectangle(9, 11, 13, h - 11, fill=p["ACCENT"], outline="")
    x = 24
    cv.create_rectangle(x, 10, w - 10, 26, fill=p["FIELD"], outline=p["LINE"])
    cv.create_text(x + 8, 18, text="输入框", anchor="w",
                   fill=p["TEXT"], font=(_UI, 8))
    cv.create_rectangle(x, 30, w - 10, 46, fill=p["ACCENT"], outline="")
    cv.create_text(x + 8, 38, text="主按钮", anchor="w",
                   fill=p["ON_ACC"], font=(_UI, 8, "bold"))
    cv.create_text(x + 8, 56, text="正文 / 次要", anchor="w",
                   fill=p["TEXT2"], font=(_UI, 7))
    return cv
