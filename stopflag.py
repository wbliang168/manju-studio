#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
stopflag.py —— 全局"停止"开关（GUI 与渲染管线共用一个进程，靠它联动）
=====================================================================
用法：
    import stopflag
    stopflag.clear()                 # 开始任务前清掉旧标记
    stopflag.set()                   # 用户点「停止」
    if stopflag.is_set(): ...        # 各处循环里检查
    raise stopflag.Stopped()         # 需要就地中止时抛这个异常
"""
import threading


class Stopped(Exception):
    """用户主动停止（区别于真错误，上层只提示"已停止"）。"""


STOP = threading.Event()


def set():
    STOP.set()


def clear():
    STOP.clear()


def is_set():
    return STOP.is_set()


def sleep(seconds, slice_=0.4):
    """可被停止打断的 sleep：返回 True 表示期间被要求停止。"""
    import time
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        if STOP.is_set():
            return True
        time.sleep(min(slice_, max(0.0, end - time.time())))
    return STOP.is_set()


def check():
    """被要求停止就抛 Stopped。"""
    if STOP.is_set():
        raise Stopped("user stop")
