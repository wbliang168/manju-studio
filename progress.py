#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
progress.py —— 渲染进度通道（渲染线程 -> GUI 进度条）
=====================================================
渲染是跑在子线程里的，不能直接碰 tkinter 控件，
所以这里只做一件事：把 (阶段, 0~1 进度, 文字) 丢给注册好的回调，
GUI 端在自己的轮询里读出来刷新进度条。
"""
import threading

_LOCK = threading.Lock()
_CB = None
_LAST = {"stage": "", "frac": 0.0, "text": ""}


def set_cb(fn):
    """注册回调 fn(stage:str, frac:float, text:str)。传 None 取消。"""
    global _CB
    with _LOCK:
        _CB = fn


def emit(stage, frac, text=""):
    """上报进度。**同一轮里只增不减**，避免阶段切换时百分比往回跳（看着像重跑）。"""
    global _LAST
    frac = max(0.0, min(1.0, float(frac)))
    if _LAST["frac"] and frac < _LAST["frac"] - 0.001 and _LAST["stage"] != "完成":
        frac = _LAST["frac"]
    _LAST = {"stage": stage, "frac": frac, "text": text}
    cb = _CB
    if cb:
        try:
            cb(stage, frac, text)
        except Exception:
            pass


def last():
    """最近一次进度（GUI 轮询用）。"""
    return dict(_LAST)


def reset():
    global _LAST
    _LAST = {"stage": "", "frac": 0.0, "text": ""}
