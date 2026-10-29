# -*- coding: utf-8 -*-
"""
语音转文字 —— 离线实时识别（Vosk）

对着麦克风说话，识别出的文字实时显示在窗口中。
全程离线，语音数据不出本机。
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------- 依赖
DEPS_OK = True
DEPS_ERR = ""
try:
    import numpy as np
    import sounddevice as sd
    import vosk
except Exception as exc:  # 缺库时给出友好提示，而不是直接抛栈
    DEPS_OK = False
    DEPS_ERR = f"{type(exc).__name__}: {exc}"

try:
    import soxr

    HAS_SOXR = True
except Exception:
    HAS_SOXR = False

try:
    import winsound  # Windows 自带，用来给「按住/松开」配提示音

    HAS_WINSOUND = True
except Exception:
    HAS_WINSOUND = False

try:
    import pypinyin  # 声调标注用：把识别出的汉字转成带声调的拼音，方便核对同音字

    HAS_PYPINYIN = True
except Exception:
    HAS_PYPINYIN = False

# --------------------------------------------------------------------- 常量
def _app_dir() -> Path:
    """程序所在目录：打包成 exe 后是 exe 所在目录，源码运行时是脚本所在目录。

    不能用 __file__：PyInstaller 里它指向解包出来的临时目录，
    那样模型、设置、日志会被写进临时目录，下次启动就找不到、也留不住。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
MODELS_DIR = APP_DIR / "models"


def _official_url(model_name: str) -> str:
    """Vosk 官方发布地址。国内经常连不上，只作最后兜底。"""
    return f"https://alphacephei.com/vosk/models/{model_name}.zip"


# 一个几乎收录了所有轻量模型的公开仓库，经 hf-mirror 国内可下（实测约 340 KB/s）
HF_MODELS_REPO = "localstack/vosk-models"


def _hf_url(model_name: str) -> str:
    return f"https://hf-mirror.com/{HF_MODELS_REPO}/resolve/main/{model_name}.zip"


# 可识别的语言。small 是轻量模型（几十 MB，实时性好，默认用它）；
# large 是可选的「高精度模型」（更大更准，按需下载），为 None 表示该语言只有轻量模型。
# large_sources 每条是 (标签, 地址, 期望字节数或 None, 重试上限, 卡死秒数, 连续零进展上限)。
LANGUAGES: dict[str, dict] = {
    "中文": {
        "small": "vosk-model-small-cn-0.22",
        "small_size": "约 42 MB",
        "large": "vosk-model-cn-0.22",
        "large_size": "约 1.3 GB",
        "large_sources": [
            # 源按实测可下性排序，全部走镜像，官方站只作最后兜底：
            #   hf-mirror.com  实测可下，支持断点续传
            #   aifasthub.com  回源到 hf.co 的 AWS CDN，曾经是唯一能跑起来的镜像；时通时断
            #   官方站         国内只有十几 KB/s，下 1.3 GB 要二十多个小时，基本等于不可用
            # 末位数字是「连续几次重连零进展就放弃这个源」：死源几轮就被跳过，慢但在推进的源不受影响
            ("HF 镜像",
             "https://hf-mirror.com/LiangJingyi/vosk-model-cn-0.22/resolve/main/model-cn.zip",
             1357192385, 200, 25, 4),
            ("aifasthub 镜像",
             "https://aifasthub.com/LiangJingyi/vosk-model-cn-0.22/resolve/main/model-cn.zip",
             1357192385, 200, 25, 4),
            ("官方站点", _official_url("vosk-model-cn-0.22"), 1358736686, 60, 60, 3),
        ],
        "tone": True,   # 只有中文有「声调」这个概念
    },
    "英语（美式）": {
        "small": "vosk-model-small-en-us-0.15",
        "small_size": "约 40 MB",
        "large": "vosk-model-en-us-0.22-lgraph",
        "large_size": "约 128 MB",
        "large_sources": [
            # 这个 lgraph 模型（128 MB）在 GitHub 上有完整镜像，仓库根就是模型（am/conf/graph/ivector）。
            # 注意 GitHub 归档不支持 Range，断了只能重下；官方站支持续传但很慢，作兜底。
            ("GitHub 镜像",
             "https://codeload.github.com/sekpona1-crypto/vosk-model-en-us-0.22-lgraph/zip/refs/heads/master",
             None, 10, 30, 3),
            ("官方站点", _official_url("vosk-model-en-us-0.22-lgraph"), None, 30, 60, 3),
        ],
        "tone": False,
    },
    "英语（英式）": {
        "small": "vosk-model-small-en-gb-0.15",
        "small_size": "约 40 MB",
        "large": None,
        "tone": False,
    },
    "日语": {
        "small": "vosk-model-small-ja-0.22",
        "small_size": "约 48 MB",
        "large": None,
        "tone": False,
    },
}
LANGUAGE_ORDER = tuple(LANGUAGES)
DEFAULT_LANGUAGE = "中文"


def lang_spec(language: str) -> dict:
    """取某语言的模型配置；不认识的语言回落到默认（中文）。"""
    return LANGUAGES.get(language, LANGUAGES[DEFAULT_LANGUAGE])


def small_model_dir(language: str) -> Path:
    return MODELS_DIR / lang_spec(language)["small"]


def large_model_dir(language: str) -> Path | None:
    name = lang_spec(language).get("large")
    return MODELS_DIR / name if name else None


def all_model_dirs() -> set[Path]:
    """所有已知语言的模型目录，解压时用来避免把已有的模型误改名。"""
    dirs = set()
    for spec in LANGUAGES.values():
        dirs.add(MODELS_DIR / spec["small"])
        if spec.get("large"):
            dirs.add(MODELS_DIR / spec["large"])
    return dirs


LOG_PATH = APP_DIR / "运行日志.txt"
SETTINGS_PATH = APP_DIR / "设置.json"

# 中文小模型的备用下载源：GitHub 上的分片镜像，国内访问速度远好于官方站
GITHUB_ARCHIVE = "https://codeload.github.com/jimy7945/vosk-model-cn/zip/refs/heads/main"
GITHUB_PARTS = ("zip.part0", "zip.part1", "zip.part2")

TARGET_RATE = 16000  # Vosk 中文模型要求 16 kHz 单声道
BLOCK_SECONDS = 0.25  # 每次送进识别器的音频长度

MIN_RECORD_SECONDS = 0.4  # 按住短于这个时长就提醒「太短了」，多半是误触
MIC_STALL_SECONDS = 3.0  # 录音中连续这么久收不到音频数据，判定麦克风异常
# 多语言仲裁：同一段语音，各语言的 final 不会同时到达，等这么久把候选攒齐再挑最优
FINAL_GRACE_SECONDS = 0.6
SOUND_START_HZ, SOUND_STOP_HZ, SOUND_MS = 920, 620, 70  # 按住/松开的提示音

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{CJK}])\s+(?=[{CJK}])")

# 口述标点：说到这些词就转成对应符号（Vosk 本身不会加标点）。
# 替换时按关键词长度从长到短处理，避免「感叹号」被「叹号」抢先吃掉一部分。
PUNCTUATION_MAP = {
    "逗号": "，",
    "句号": "。",
    "问号": "？",
    "感叹号": "！",
    "叹号": "！",
    "冒号": "：",
    "分号": "；",
    "顿号": "、",
    "省略号": "……",
    "破折号": "——",
    "左括号": "（",
    "右括号": "）",
    "双引号": "“”",
    "换行": "\n",
    "回车": "\n",
    "换段": "\n",
}

DICT_FILE_NAME = "替换词典.txt"
DICT_PATH = APP_DIR / DICT_FILE_NAME

HINT_TITLE = "对着麦克风说话，文字会出现在这里"

# ------------------------------------------------------------------- 自动标点
# Vosk 只吐词、不带标点。这里靠「说话停顿的长短」判断在哪儿断句，再按句意挑符号：
# 停顿久（≥ AUTO_PAUSE_SENTENCE）当成一句说完了，补「。」「？」或「！」；
# 停顿中等（≥ AUTO_PAUSE_CLAUSE）当成句中换气，补「，」；更短的停顿什么都不加。
# 停顿时长由 AudioEngine 在音频流上量（见 AudioEngine._track_pause），随 final 结果一起送上来。
AUTO_PAUSE_SENTENCE = 1.8   # 秒；停顿超过它认为一句说完了
AUTO_PAUSE_CLAUSE = 0.8     # 秒；停顿超过它认为句中停顿
# 「说话 / 静音」门限不用固定值：不同麦克风、不同增益下音量差很多，固定阈值要么把
# 说话当静音（安静麦克风），要么把底噪当说话（高增益）。改成跟着实测音量自适应：
#   _floor 缓慢跟随底噪，_peak 缓慢跟随说话峰值，门限取「峰值的一定比例」并卡在底噪之上。
SPEECH_GATE_FRAC = 0.2      # 门限 = 说话峰值的这个比例
SPEECH_GATE_MIN = 0.0035    # 归一化 RMS 绝对下限（≈115/32768）
SPEECH_FLOOR_DOWN = 0.15    # 底噪向下跟随速度
SPEECH_FLOOR_UP = 0.0005    # 底噪向上跟随速度（极慢，免得把一句话的开头当噪声抬高门限）
SPEECH_PEAK_DECAY = 0.985   # 说话峰值回落速度（约 16 秒时间常数）
# 识别结果区是窗口里唯一可伸缩的控件：窗口比内容矮时 pack 会把它压到 1px，结果完全看不见。
# 用这个保底高度推算窗口的最小高度，保证任何情况下结果区都留得下一段可用空间。
MIN_RESULT_HEIGHT = 120     # 设计像素（会按 DPI 缩放）

# 语气判断用的关键词：只在句尾一小段里找，减少「我不知道怎么做」被误判成问句
_TAIL_WINDOW = 10
_QUESTION_TAILS = ("吗", "呢", "么")
_QUESTION_TAGS = (
    "是不是", "有没有", "对不对", "好不好", "行不行", "能不能",
    "可不可以", "要不要", "会不会", "该不该",
)
_QUESTION_WORDS = (
    "什么", "为什么", "怎么", "怎样", "如何", "哪里", "哪儿", "哪个",
    "哪位", "谁", "多少", "多久", "多大", "多远", "几点", "是否", "难道",
)
_EXCLAIM_TAILS = ("啊", "呀", "哇", "啦", "喽")
_EXCLAIM_WORDS = (
    "太好了", "太棒", "真棒", "加油", "天哪", "糟糕", "哇塞", "厉害", "漂亮",
)
# 已经带标点时就别再自动补，避免出现「。。」或「，。」
END_PUNCT = "，。？！；：、…—～!?,.;:"


def auto_end_punct(sentence: str) -> str:
    """按句意挑结尾标点：问句「？」，感叹「！」，其余「。」。

    纯启发式（Vosk 不带标点，也没有语气模型）：只在句尾一小段里找关键词，
    猜错也只是标点不对，不影响文字本身。
    """
    text = sentence.strip()
    if not text:
        return "。"
    window = text[-_TAIL_WINDOW:]
    if text.endswith(_QUESTION_TAILS):
        return "？"
    if any(word in window for word in _QUESTION_TAGS + _QUESTION_WORDS):
        return "？"
    if text.endswith(_EXCLAIM_TAILS):
        return "！"
    if any(word in window for word in _EXCLAIM_WORDS):
        return "！"
    return "。"


def split_final(payload) -> tuple[str, float, str]:
    """拆开 final 消息：新格式是 {"text","pause","lang"}，旧格式/测试里可能是纯字符串。"""
    if isinstance(payload, dict):
        pause = payload.get("pause", 0.0)
        return (
            str(payload.get("text", "")),
            float(pause or 0.0),
            str(payload.get("lang", "")),
        )
    return str(payload), 0.0, ""


def result_text_conf(raw: str) -> tuple[str, float]:
    """从 Vosk 结果 JSON 里取「文本 + 每词平均置信度」。

    置信度要先对识别器调 SetWords(True) 才有；取不到时返回 0，多语言仲裁就退化成
    「先比置信度、再比文本长度」，不会因为缺字段直接崩掉。
    """
    try:
        data = json.loads(raw)
    except Exception:
        return "", 0.0
    text = str(data.get("text", ""))
    words = data.get("result") or []
    if not words:
        return text, 0.0
    try:
        conf = sum(float(w.get("conf", 0.0)) for w in words) / len(words)
    except Exception:
        conf = 0.0
    return text, conf


# ------------------------------------------------------------------- 日志 / 设置
def log(message: str) -> None:
    try:
        stamp = datetime.now().strftime("%H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def start_log() -> None:
    """每次启动重写日志，只保留最近一次运行，便于排查「为什么没起来」。"""
    try:
        LOG_PATH.write_text(
            "语音转文字 · 运行日志\n"
            f"启动时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"Python：{sys.version.split()[0]}\n"
            f"解释器：{sys.executable}\n"
            f"管理员权限：{'是' if is_elevated() else '否'}\n"
            f"程序目录：{APP_DIR}\n"
            + "-" * 56 + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        log(f"保存设置失败：{type(exc).__name__}: {exc}")


# ----------------------------------------------------------- 全局语音输入
# 用 SendInput 把 Unicode 字符直接发给当前拥有焦点的窗口，
# 从而在记事本、浏览器、聊天框等任意程序里实现“语音打字”。
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

VK_F6, VK_F7, VK_F8, VK_F9, VK_F10 = 0x75, 0x76, 0x77, 0x78, 0x79
VK_RETURN = 0x0D

# 热键可选值：名字 -> (修饰键, 虚拟键码)。默认 F9；
# F9 被游戏/其他软件占用时，换 F8、F10 或 Ctrl+F9 即可。
HOTKEYS = {
    "F9": (0, VK_F9),
    "F8": (0, VK_F8),
    "F10": (0, VK_F10),
    "F7": (0, VK_F7),
    "F6": (0, VK_F6),
    "Ctrl+F9": (MOD_CONTROL, VK_F9),
}
DEFAULT_HOTKEY = "F9"

SINGLE_INSTANCE_MUTEX = "VoiceToText_Offline_Recognizer_SingleInstance"
_single_instance_handle = None

# 开机自启动：写进 HKCU 的 Run 项，登录后自动打开本程序（普通权限，不弹 UAC）
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "语音转文字"


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def send_unicode_text(text: str) -> bool:
    """把一段文字作为键盘输入发给当前焦点窗口（绕过输入法，不占用剪贴板）。"""
    if not text:
        return False

    events = []
    for char in text:
        if char in "\r\n":  # 换行要按回车键，直接发 0x0A 很多程序不认
            events.append(_key_event(VK_RETURN))
            events.append(_key_event(VK_RETURN, True))
            continue
        for code in _utf16_units(char):
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                events.append(_INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(0, code, flags, 0, None))))

    array = (_INPUT * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
    return sent == len(events)


def is_elevated() -> bool:
    """是否以管理员身份运行。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def system_scale() -> float:
    """系统 DPI 缩放系数（96 DPI 记为 1.0，150% 缩放返回 1.5）。

    tkinter 默认按 96 DPI 布局，在高分屏上会显得又小又挤；
    这里读出真实 DPI，后面用它同时放大窗口尺寸和字号。
    """
    dpi = 0
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        dpi = 0
    if not dpi:
        try:
            dc = ctypes.windll.user32.GetDC(0)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(dc, 88))  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, dc)
        except Exception:
            dpi = 0
    if dpi < 72 or dpi > 480:  # 明显不合理就退回 100%
        return 1.0
    return dpi / 96.0


def acquire_single_instance() -> bool:
    """同一时间只允许运行一个实例。

    多开会有两个问题：只有先启动的那个能抢到热键，后启动的会一直提示「注册失败」；
    而且每次启动都会重写运行日志，导致排查问题时看不到真正的那个实例的日志。
    用一个命名互斥体挡住重复启动，进程退出时互斥体由系统自动释放。
    """
    global _single_instance_handle
    ERROR_ALREADY_EXISTS = 183

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]

    handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    if not handle:  # 创建失败就不拦，宁可多开也别让人打不开
        return True
    _single_instance_handle = handle
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def release_single_instance() -> None:
    """主动释放单实例锁。

    「以管理员身份重启」时会先关掉自己再拉起新进程，如果不先释放，
    新进程会撞上这个互斥体、以为已经有实例在跑，直接退出。
    """
    global _single_instance_handle
    handle, _single_instance_handle = _single_instance_handle, None
    if not handle:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        pass


def autostart_command() -> str:
    """登录自启动时执行的命令：用带窗口的 pythonw / 打包后的 exe，避免弹出黑色控制台。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
            return bool(value)
    except Exception:
        return False


def set_autostart(enable: bool) -> bool:
    """开关开机自启动（只写当前用户的 Run 项，不动系统级设置）。"""
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            if enable:
                winreg.SetValueEx(
                    key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command()
                )
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:
        log(f"设置开机自启动失败：{type(exc).__name__}: {exc}")
        return False


# ------------------------------------------------ 多种「把文字送出去」的方式
# 不同目标程序吃的方式不一样：
#   常规程序（记事本/浏览器/微信）→ SendInput 模拟按键最稳
#   游戏聊天框 → 常走输入法通道，需要直接投递 WM_CHAR / WM_IME_CHAR
#   实在不行 → 剪贴板 + Ctrl+V 兜底
# 注意：SendInput 和 PostMessage 都受 UIPI 限制，只能注入到权限同级或更低的进程，
#       所以往以管理员身份运行的游戏注入，本程序也必须以管理员身份运行。
WM_CHAR = 0x0102
WM_IME_CHAR = 0x0286
VK_CONTROL = 0x11
VK_V = 0x56

INJECT_KEY = "模拟按键（通用）"
INJECT_MSG = "消息注入（WM_CHAR）"
INJECT_IME = "IME 消息（游戏常用）"
INJECT_CLIP = "剪贴板粘贴（Ctrl+V）"
INJECT_MODES = (INJECT_KEY, INJECT_MSG, INJECT_IME, INJECT_CLIP)

INJECT_HINTS = {
    INJECT_KEY: "记事本 / 浏览器 / 微信 / Office 用这个",
    INJECT_MSG: "目标换成游戏聊天框时，先试这个",
    INJECT_IME: "上一条没反应就试这个（游戏走输入法通道）",
    INJECT_CLIP: "前两条都不行时用；会短暂占用剪贴板",
}

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [ctypes.c_uint]
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_kernel32.GetCurrentProcessId.restype = wintypes.DWORD


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def focused_window(hwnd: int):
    """取目标线程里真正持有键盘焦点的子窗口；引擎自绘的游戏没有子窗口，返回 None。"""
    thread_id = _user32.GetWindowThreadProcessId(hwnd, None)
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    if _user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)) and info.hwndFocus:
        return info.hwndFocus
    return None


def foreground_is_self() -> bool:
    """当前前台窗口是不是本程序自己的（用来判断用户有没有切到目标程序）。"""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == _kernel32.GetCurrentProcessId()


def _key_event(vk: int, up: bool = False) -> _INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    return _INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(vk, 0, flags, 0, None)))


def _send_vk_combo(modifier: int, key: int) -> bool:
    events = [_key_event(modifier), _key_event(key), _key_event(key, True), _key_event(modifier, True)]
    array = (_INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
    return sent == len(events)


def _utf16_units(text: str):
    raw = text.encode("utf-16-le")
    for index in range(0, len(raw), 2):
        yield int.from_bytes(raw[index:index + 2], "little")


def post_message_text(text: str, message: int = WM_CHAR) -> bool:
    """把字符直接投递进目标窗口的消息队列，绕开输入队列和「模拟输入」标记。"""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return False
    target = focused_window(hwnd) or hwnd
    ok = True
    for code in _utf16_units(text):
        if not _user32.PostMessageW(target, message, code, 0):
            ok = False
    return ok


def _get_clipboard_text():
    CF_UNICODETEXT = 13
    if not _user32.OpenClipboard(None):
        return None
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def _set_clipboard_text(text: str) -> bool:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    if not _user32.OpenClipboard(None):
        return False
    try:
        _user32.EmptyClipboard()
        buffer = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buffer)
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return False
        ctypes.memmove(pointer, buffer, size)
        _kernel32.GlobalUnlock(handle)
        return bool(_user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        _user32.CloseClipboard()


def paste_text(text: str, restore_delay: float = 0.8) -> bool:
    """把文字塞进剪贴板再按 Ctrl+V，用完把原剪贴板内容还回去。"""
    backup = _get_clipboard_text()
    if not _set_clipboard_text(text):
        return False
    ok = _send_vk_combo(VK_CONTROL, VK_V)
    if backup is not None:
        threading.Timer(restore_delay, _set_clipboard_text, args=(backup,)).start()
    return ok


class PushToTalkListener(threading.Thread):
    """按住热键说话、松开结束。

    用 RegisterHotKey 拿到「按下」（同时把按键吞掉，目标程序收不到该键），
    再用 GetAsyncKeyState 轮询检测「松开」。两条路径都不需要键盘钩子，
    避免与带反作弊的游戏冲突。
    """

    WM_HOTKEY = 0x0312
    HOTKEY_ID = 0xA1
    POLL_SECONDS = 0.02
    REGISTER_RETRY_SECONDS = 3.0
    REGISTER_RETRY_INTERVAL = 0.5

    def __init__(self, name: str, modifiers: int, vk: int, msg_queue: queue.Queue):
        super().__init__(daemon=True)
        self.name = name
        self.modifiers = modifiers
        self.vk = vk
        self.msg = msg_queue
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        # 先触碰一次消息函数，确保本线程拥有消息队列
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        # 上一个实例可能刚退出、还没释放热键，这里重试几次再报失败
        deadline = time.monotonic() + self.REGISTER_RETRY_SECONDS
        while True:
            registered = user32.RegisterHotKey(
                None, self.HOTKEY_ID, MOD_NOREPEAT | self.modifiers, self.vk
            )
            if registered:
                self.msg.put(("hotkey_ready", self.name))
                break
            if self._stop.is_set() or time.monotonic() >= deadline:
                self.msg.put(("hotkey_error", self.name))
                return
            time.sleep(self.REGISTER_RETRY_INTERVAL)

        pressed = False
        while not self._stop.is_set():
            # 清掉 WM_HOTKEY 消息：按键已在系统层被吞掉，按下/松开统一由轮询判断
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                pass

            down = bool(user32.GetAsyncKeyState(self.vk) & 0x8000)
            if down != pressed:
                pressed = down
                self.msg.put(("ptt", "down" if down else "up"))
            time.sleep(self.POLL_SECONDS)

        user32.UnregisterHotKey(None, self.HOTKEY_ID)


def tidy(text: str) -> str:
    """Vosk 的中文结果用空格分隔每个词，这里把中文字之间的空格去掉。"""
    text = " ".join(text.split())
    return SPACE_BETWEEN_CJK.sub("", text)


def apply_mapping(text: str, mapping: dict) -> str:
    """按映射表做字符串替换；长关键词优先，避免短词抢先匹配。"""
    if not text or not mapping:
        return text
    for key in sorted(mapping, key=len, reverse=True):
        if key:
            text = text.replace(key, mapping[key])
    return text


def load_dict_file() -> dict:
    """从「替换词典.txt」读取纠错映射，每行格式：识别到的词=想要的词。"""
    mapping: dict = {}
    try:
        if not DICT_PATH.exists():
            return mapping
        for raw in DICT_PATH.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            left, right = line.split("=", 1)
            left, right = left.strip(), right.strip()
            if left:
                mapping[left] = right
    except Exception as exc:
        log(f"读取替换词典失败：{type(exc).__name__}: {exc}")
    return mapping


def save_dict_file(mapping: dict) -> bool:
    try:
        lines = ["# 替换词典：每行一条「识别到的词=想要的词」，改完保存即可生效。", "# 以 # 开头的行会被忽略。"]
        lines += [f"{key}={value}" for key, value in mapping.items()]
        DICT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except Exception as exc:
        log(f"保存替换词典失败：{type(exc).__name__}: {exc}")
        return False


def smart_join(left: str, right: str) -> str:
    """拼接两段结果：英文之间补空格，中文直接相连。"""
    if not left:
        return right
    if not right:
        return left
    if left[-1].isascii() and right[0].isascii() and left[-1].isalnum() and right[0].isalnum():
        return f"{left} {right}"
    return left + right


# ------------------------------------------------------------------- 声调标注
# Vosk 的中文模型只输出汉字、不带声调，识别时无法靠声调去挑字（选哪个字由语言模型定）。
# 这里是在识别完成之后用 pypinyin 补上标准声调，方便一眼看出有没有同音字写错
# （「在/再」「的/得/地」这类），再配合「替换词典」把常错的词纠正过来。
_TIGHT_PUNCT = "，。？！；：、…—～!?,.;:)）】》”’"
_TIGHT_PUNCT_RE = re.compile(rf"\s+([{_TIGHT_PUNCT}])")
# 中文标点后面也不该留空格（pypinyin 会把每个音节和标点都用空格隔开）
_CJK_PUNCT = "，。？！；：、…—～（）【】《》“”‘’"
_AFTER_CJK_PUNCT_RE = re.compile(rf"([{_CJK_PUNCT}])\s+")


def tone_pinyin(text: str) -> str:
    """把中文转成带声调符号的拼音（如「你好」→「nǐ hǎo」）；缺库或出错时返回空串。"""
    if not (HAS_PYPINYIN and text):
        return ""
    try:
        from pypinyin import Style, lazy_pinyin

        result = " ".join(t for t in lazy_pinyin(text, style=Style.TONE) if t)
        # 标点两侧都不留空格，读起来更贴近原文
        result = _TIGHT_PUNCT_RE.sub(r"\1", result)
        return _AFTER_CJK_PUNCT_RE.sub(r"\1", result)
    except Exception as exc:
        log(f"声调标注失败：{type(exc).__name__}: {exc}")
        return ""


# ------------------------------------------------------------------- 模型管理
def _is_model_dir(path: Path) -> bool:
    return (path / "conf").is_dir() or (path / "am").is_dir()


def model_ready(language: str | None = None) -> bool:
    lang = language or DEFAULT_LANGUAGE
    large = large_model_dir(lang)
    return _is_model_dir(small_model_dir(lang)) or bool(large and _is_model_dir(large))


def active_model_dir(language: str | None = None) -> Path:
    """该语言下两个模型都在时优先用高精度大模型，没有才退回轻量模型。"""
    lang = language or DEFAULT_LANGUAGE
    large = large_model_dir(lang)
    if large and _is_model_dir(large):
        return large
    return small_model_dir(lang)


def _ascii_link_candidates(model_dir: Path):
    """能用来放 ASCII 联接的目录：优先临时目录，其次模型所在盘根目录。"""
    import os

    bases = [os.environ.get("TEMP"), os.environ.get("TMP")]
    if model_dir.drive:
        bases.append(model_dir.drive + "\\")

    seen = set()
    for base in bases:
        if not base:
            continue
        path = Path(base)
        try:
            str(path).encode("ascii")
        except UnicodeEncodeError:
            continue
        if str(path).lower() in seen:
            continue
        seen.add(str(path).lower())
        yield path


def _replace_junction(link: Path, target: Path) -> None:
    # 目录联接用 rmdir 删除，只删链接本身，不会动到它指向的真实目录
    subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if not _is_model_dir(link):
        raise RuntimeError("创建 ASCII 联接路径失败")


def ascii_model_path(model_dir: Path) -> str:
    """Vosk 底层（Kaldi）读不了含中文等非 ASCII 字符的路径，
    遇到这种路径时用 NTFS 目录联接映射成纯 ASCII 路径再交给 Vosk。"""
    try:
        str(model_dir).encode("ascii")
        return str(model_dir)
    except UnicodeEncodeError:
        pass

    for base in _ascii_link_candidates(model_dir):
        # 联接名带上模型目录名：同时启用多个语言时，各模型各自一个联接，不会互相覆盖
        link = base / f"vosk_{model_dir.name}"
        try:
            _replace_junction(link, model_dir)
            return str(link)
        except Exception:
            continue
    raise RuntimeError("模型目录路径含非 ASCII 字符，且无法创建 ASCII 联接路径")


def extract_model(zip_path: Path, expected: Path | None = None) -> None:
    """解压模型包；镜像包 / 仓库包的内部目录名常和官方不一致（如 model-cn/、
    xxx-master/），所以解压后按内容找出真正的模型目录，再改成 expected 期望的名字。"""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(MODELS_DIR)
    if expected is None or _is_model_dir(expected):
        return
    # 已经存在的（其它语言的）模型目录不能被误改名
    existing = all_model_dirs() - {expected}
    found: Path | None = None
    for child in MODELS_DIR.iterdir():
        if child.is_dir() and child not in existing and _is_model_dir(child):
            found = child
            break
    if found is None:
        # 有些仓库压缩包会把模型放在「仓库名/」这一层，再往下一层找一次
        for top in MODELS_DIR.iterdir():
            if not top.is_dir() or top in existing:
                continue
            for child in top.iterdir():
                if child.is_dir() and _is_model_dir(child):
                    found = child
                    break
            if found is not None:
                break
    if found is None:
        return
    if expected.exists():
        shutil.rmtree(expected, ignore_errors=True)
    found.rename(expected)


class DownloadCancelled(Exception):
    """用户主动取消下载。已下部分留在 .part 文件里，下次点下载会接着下。"""


def _fmt_eta(seconds: float) -> str:
    """把剩余秒数说成人话，给 1.3 GB 的大模型下载估个时间。"""
    if seconds < 1:
        return "不到 1 秒"
    if seconds < 60:
        return f"约 {int(seconds)} 秒"
    if seconds < 3600:
        return f"约 {int(seconds // 60)} 分 {int(seconds % 60)} 秒"
    return f"约 {int(seconds // 3600)} 小时 {int((seconds % 3600) // 60)} 分"


# 下载底层：优先用系统自带的 curl.exe，而不是 Python 的 urllib。
# 原因很具体：Python 自带的 OpenSSL 在部分网络下 TLS 握手会被间歇性丢弃 ——
# 实测同一个镜像地址，curl（走 Windows 系统 TLS）稳定约 5 MB/s，而 urllib
# 频繁「handshake operation timed out」，偶尔连上也只能读到一个 64 KB 块。
# curl 不可用或失败时，再回退到 urllib，保证换台机器也能下。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _curl_path() -> str | None:
    """找系统自带的 curl.exe（Windows 10/11 都有）；找不到返回 None。"""
    found = shutil.which("curl")
    if found:
        return found
    candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    return str(candidate) if candidate.exists() else None


def _curl_remote_size(url: str, curl: str, timeout: int = 20) -> int:
    """问一下远端文件多大，用来算进度百分比；拿不到就返回 0，不影响下载。"""
    try:
        proc = subprocess.run(
            [curl, "-sIL", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return 0
    total = 0
    for line in proc.stdout.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            try:
                total = int(value.strip())
            except ValueError:
                pass
    return total


def _curl_fetch(url: str, dest: Path, progress_cb, stall_timeout: int, curl: str) -> int:
    """用 curl.exe 下载到 dest：断点续传、卡死自动断开、取消即杀进程。"""
    total = _curl_remote_size(url, curl)
    cmd = [
        curl, "-L", "-sS", "--fail",
        "--connect-timeout", "15",
        "--speed-limit", "1024", "--speed-time", str(stall_timeout),
        "-o", str(dest),
    ]
    if dest.exists() and dest.stat().st_size:
        cmd += ["-C", "-"]  # 接着已经下到的部分续传
    cmd.append(url)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    last_size = dest.stat().st_size if dest.exists() else 0
    last_change = time.time()
    try:
        while proc.poll() is None:
            time.sleep(0.2)
            size = dest.stat().st_size if dest.exists() else 0
            if size != last_size:
                last_size = size
                last_change = time.time()
            progress_cb(size, total)
            # 除了 curl 自己的 --speed-time，再加一道文件大小停滞兜底，
            # 免得极端情况下迟迟不返回，界面看着像卡死
            if time.time() - last_change > stall_timeout + 15:
                proc.kill()
                raise ConnectionError("下载停滞，已断开，稍后从断点续传")
        if proc.returncode != 0:
            detail = (proc.stderr.read() or b"").decode("utf-8", "ignore").strip()
            raise ConnectionError(f"curl 退出码 {proc.returncode}：{detail[:200]}")
    except DownloadCancelled:
        proc.kill()
        proc.wait(timeout=5)
        raise
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stderr.close()
    return total


def _fetch(url: str, dest: Path, progress_cb, timeout: int = 60) -> int:
    """下载到 dest，支持断点续传；返回这次拿到的「期望总字节数」（未知时 0）。

    timeout 是「多久收不到数据就判定断线」——镜像源首字节要等十几秒，所以别设太小；
    中途卡死超过这个时间会抛异常，交给调用方断开重连、从断点接着下。

    progress_cb 由调用方提供，它可以在用户取消时抛 DownloadCancelled，
    异常会顺着 urlopen / open 的 with 块一路传出去，文件已写入的部分会正常落盘。

    实现上优先用系统 curl.exe（见 _NO_WINDOW 处的说明）。只有 curl 这次一个字节都
    没传下来时，才回退 urllib —— 说明 curl 在这个环境里可能根本不好使。反过来，
    只要 curl 已经传下过数据（哪怕随后断流超时），就说明它本身没问题，应该直接把
    异常抛给上层断点续传；否则每次抖动都要白扔一次更慢的 urllib 尝试。
    """
    curl = _curl_path()
    if curl is not None:
        before = dest.stat().st_size if dest.exists() else 0
        try:
            return _curl_fetch(url, dest, progress_cb, timeout, curl)
        except DownloadCancelled:
            raise
        except Exception as exc:
            after = dest.stat().st_size if dest.exists() else 0
            if after > before:
                raise
            log(f"curl 没传下任何数据，改用内置方式重试：{type(exc).__name__}: {exc}")
    return _fetch_urllib(url, dest, progress_cb, timeout)


def _fetch_urllib(url: str, dest: Path, progress_cb, timeout: int = 60) -> int:
    """内置下载实现（urllib），作为 curl 不可用或失败时的回退。"""
    have = dest.stat().st_size if dest.exists() else 0
    headers = {"User-Agent": "Mozilla/5.0"}
    if have:
        headers["Range"] = f"bytes={have}-"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if have and status == 206:
            mode, done = "ab", have
        else:
            mode, done = "wb", 0
        length = int(response.headers.get("Content-Length") or 0)
        total = done + length if length else 0

        with open(dest, mode) as handle:
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                progress_cb(done, total)
    return total


class _PartInvalid(Exception):
    """下完了但内容不对（大小不符 / 不是有效压缩包）。这种必须删掉重来，
    否则续传会一直卡在坏文件上。"""


def _backoff_seconds(attempt: int) -> float:
    """重试间隔逐步拉长，最多 20 秒，免得一直猛敲服务器。"""
    return min(20.0, 2.0 + attempt * 1.5)


def _fmt_size(num_bytes: float) -> str:
    """把字节数说成人话，用于「已清理 xx MB」这类提示。"""
    if num_bytes >= 1048576:
        return f"{num_bytes / 1048576:.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{int(num_bytes)} 字节"


def _download_temp_files() -> list[Path]:
    """models 目录里的下载临时文件：断点文件（.part）和没解压成功的压缩包（.zip）。"""
    if not MODELS_DIR.is_dir():
        return []
    return [
        path
        for path in MODELS_DIR.iterdir()
        if path.is_file() and path.name.endswith((".part", ".zip"))
    ]


def cleanup_download_junk() -> tuple[int, int]:
    """只清「一定没用」的下载残留，返回（删除个数, 释放字节数）。

    判定标准很保守：0 字节、或整包下完却不是有效压缩包。有希望续传的 .part
    一律保留 —— 已下的几百 MB 不该因为一次失败就被白扔掉。
    """
    removed = freed = 0
    for path in _download_temp_files():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        bad = size == 0
        if not bad and path.name.endswith(".zip"):
            bad = not zipfile.is_zipfile(path)
        if not bad:
            continue
        try:
            path.unlink()
            removed += 1
            freed += size
        except OSError:
            pass
    return removed, freed


def clear_all_download_files() -> tuple[int, int]:
    """删掉所有下载临时文件（含可续传的断点），返回（删除个数, 释放字节数）。

    用户明确要「删除下载失败的文件」时用它：断点也一并清掉，下次从 0 重来。
    """
    removed = freed = 0
    for path in _download_temp_files():
        try:
            size = path.stat().st_size
            path.unlink()
            removed += 1
            freed += size
        except OSError:
            pass
    return removed, freed


def pending_download_bytes() -> int:
    """当前残留的下载临时文件总大小，用于在界面上提示「有 xx MB 待续传」。"""
    total = 0
    for path in _download_temp_files():
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def _fetch_from_github(zip_path: Path, progress_cb) -> None:
    """GitHub 分片镜像：下载仓库压缩包后把三个分片拼回模型 zip。"""
    archive = MODELS_DIR / "_vosk_parts.zip"
    _fetch(GITHUB_ARCHIVE, archive, progress_cb)
    try:
        with zipfile.ZipFile(archive) as zf:
            names = sorted(
                (n for n in zf.namelist() if Path(n).name in GITHUB_PARTS),
                key=lambda n: Path(n).name,
            )
            if len(names) != len(GITHUB_PARTS):
                raise RuntimeError("镜像仓库中的分片文件不完整")
            with open(zip_path, "wb") as out:
                for name in names:
                    out.write(zf.read(name))
    finally:
        archive.unlink(missing_ok=True)


def acquire_model(progress_cb, status_cb, language: str) -> None:
    """确保所选语言的轻量模型可用：已存在直接返回，否则依次尝试各个下载源。"""
    if model_ready(language):
        return

    spec = lang_spec(language)
    name = spec["small"]
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_download_junk()  # 顺手清掉上次留下的坏文件
    zip_path = MODELS_DIR / f"{name}.zip"
    part = zip_path.with_suffix(".zip.part")

    # 每条源是 (标签, 直接地址) 或 (标签, "github_parts")。中文用 GitHub 分片镜像最稳，
    # 其余语言走 hf-mirror 上的公共模型仓库，官方站只作最后兜底。
    sources: list[tuple[str, str]] = []
    if language == DEFAULT_LANGUAGE:
        sources.append(("GitHub 镜像", "github_parts"))
    sources.append(("HF 镜像", _hf_url(name)))
    sources.append(("官方站点", _official_url(name)))

    errors = []
    for label, source in sources:
        try:
            status_cb(f"正在从{label}下载{language}模型（{spec['small_size']}）…")
            if source == "github_parts":
                _fetch_from_github(zip_path, progress_cb)
            else:
                _download_to(source, part, progress_cb, status_cb, label,
                             None, attempts=8, stall_timeout=25, stall_rounds=4)
                part.replace(zip_path)
            status_cb("正在解压模型…")
            extract_model(zip_path, small_model_dir(language))
            if not model_ready(language):
                raise RuntimeError("解压后模型目录不完整")
            zip_path.unlink(missing_ok=True)
            return
        except DownloadCancelled:
            raise
        except Exception as exc:
            errors.append(f"{label}：{type(exc).__name__}: {exc}")
            cleanup_download_junk()

    raise RuntimeError("所有下载源都失败了\n\n" + "\n".join(errors))


def _download_to(url: str, part: Path, progress_cb, status_cb,
                 label: str, expect: int | None, attempts: int = 10,
                 stall_timeout: int = 25, stall_rounds: int = 5) -> None:
    """带断点续传和耐心重试地下载到 part，并校验大小与压缩包完整性。

    镜像源时通时断，所以策略是「断了就重连续传」而不是「一次失败就判死」：
    连续 stall_timeout 秒收不到数据就断开重连，从已下的字节接着下。

    判定「该不该放弃这个源」看的是**有没有进展**，不是总共花了多久。原因很实在：
    镜像速度会在 2.8 MB/s 和 0.5 MB/s 之间大幅波动，1.3 GB 慢起来要几十分钟；
    用固定时间预算会把「正在正常推进」的源当成失败扔掉，然后回退到已经挂掉的源，
    最后报「所有下载源都失败了」——可断点文件明明一直在涨。
    所以只要每次重连都比上次多下到字节就继续续传；只有连续 stall_rounds 次重连
    都一个字节没涨，才认定这个源没救了，交给下一个源。这样死源也会很快被跳过
    （它每次都是零进展），不用靠 attempts 慢慢耗。
    """
    last_error: Exception | None = None
    best = part.stat().st_size if part.exists() else 0
    stagnant = 0
    for attempt in range(1, attempts + 1):
        try:
            total = _fetch(url, part, progress_cb, timeout=stall_timeout)
            size = part.stat().st_size if part.exists() else 0
            want = expect or total
            if want and size < want:
                # 连接提前断了：留着，下次从断点接着下
                raise ConnectionError(f"连接提前中断（{size}/{want} 字节）")
            if want and size > want:
                part.unlink(missing_ok=True)
                raise _PartInvalid(f"文件比预期大（{size} > {want}）")
            if not zipfile.is_zipfile(part):
                part.unlink(missing_ok=True)
                raise _PartInvalid("下到的内容不是有效压缩包")
            return
        except DownloadCancelled:
            raise
        except _PartInvalid as exc:
            # 坏文件已丢弃，从 0 重来；这是「重来」不是「停滞」
            last_error = exc
            best = 0
            stagnant = 0
            if attempt < attempts:
                status_cb(f"{label}下到的文件不完整，已丢弃，重新下载…")
                time.sleep(2)
        except Exception as exc:
            last_error = exc
            size = part.stat().st_size if part.exists() else 0
            stagnant = 0 if size > best else stagnant + 1
            best = max(best, size)
            if attempt < attempts and stagnant < stall_rounds:
                delay = _backoff_seconds(attempt)
                status_cb(
                    f"{label}连接中断，{delay:.0f} 秒后自动重连续传"
                    f"（第 {attempt + 1}/{attempts} 次，已下 {size / 1048576:.0f} MB）…"
                )
                time.sleep(delay)
            else:
                break
    raise RuntimeError(
        f"重试 {attempts} 次仍失败：{type(last_error).__name__}: {last_error}"
    )


def download_large_model(progress_cb, status_cb, language: str,
                         mirror_url: str = "") -> None:
    """下载并解压所选语言的高精度模型（中文约 1.3 GB，英语约 128 MB）。

    源按「快且稳」排序：自定义镜像（若填了）→ 该语言内置的镜像源 → 官方站兜底。
    不做探活，直接按这个顺序依次试（探活会把抖动的快源误判成死源）；
    每个源各自保留断点文件，断线会自动重连续传，中途关掉程序下次也接着下。
    """
    spec = lang_spec(language)
    large_name = spec.get("large")
    if not large_name:
        raise RuntimeError(f"「{language}」暂时没有高精度模型，用轻量模型即可")
    expected_dir = large_model_dir(language)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    # 先把明确损坏的残留清掉，免得续传卡在坏文件上（可续传的断点会保留）
    removed, freed = cleanup_download_junk()
    if removed:
        status_cb(f"已清理 {removed} 个损坏的下载残留（{_fmt_size(freed)}）")
    zip_path = MODELS_DIR / f"{large_name}.zip"

    # 每条源是 (标签, url, 断点文件, 期望字节数, 重试上限, 卡死秒数, 连续零进展上限)。
    # 最后一项是「连续几次重连都一个字节没涨就放弃这个源」——不是时间预算，
    # 所以慢但一直在推进的源不会被误判失败（见 _download_to 的说明）。
    # 断点文件按源分别命名，各源互不干扰，下次续传时能接着自己那份。
    sources = []
    if mirror_url:
        sources.append((
            "自定义镜像", mirror_url,
            MODELS_DIR / f"{large_name}.custom.part", None, 20, 30, 5,
        ))
    for index, entry in enumerate(spec.get("large_sources", [])):
        label, url, expect, attempts, stall, stall_rounds = entry
        sources.append((
            label, url,
            MODELS_DIR / f"{large_name}.src{index}.part",
            expect, attempts, stall, stall_rounds,
        ))
    if not sources:
        raise RuntimeError(f"「{language}」没有可用的高精度模型下载源")

    # 这里不做探活排序：镜像的连通性时好时坏，一次 1 KB 的小请求经常探不到，
    # 会把明明能跑满速的镜像误判成死源，排到又慢又稳的官方站后面，然后就
    # 一直卡在几十 KB/s。直接按上面的固定优先级依次试，死源交给 _download_to 的
    # 「连续零进展」逻辑快速跳过（它每次都是零进展，几轮就换下一个）。

    errors = []
    for label, url, part, expect, attempts, stall, stall_rounds in sources:
        try:
            status_cb(f"正在从{label}下载{language}高精度模型（{spec.get('large_size', '')}）…")
            _download_to(url, part, progress_cb, status_cb, label, expect,
                         attempts, stall, stall_rounds)
            part.replace(zip_path)
            errors = []
            break
        except DownloadCancelled:
            raise
        except Exception as exc:
            errors.append(f"{label}：{exc}")
            cleanup_download_junk()  # 这个源留下的坏文件顺手清掉，别拖累下一个源

    if errors:
        raise RuntimeError("所有下载源都失败了\n\n" + "\n".join(errors))

    status_cb(f"正在解压模型（{spec.get('large_size', '')}，需要一会儿）…")
    extract_model(zip_path, expected_dir)
    zip_path.unlink(missing_ok=True)
    if not _is_model_dir(expected_dir):
        raise RuntimeError("解压后没有找到模型目录，压缩包可能不完整")


# ------------------------------------------------------------------- 识别引擎
class AudioEnhancer:
    """为提高识别率做的轻度音频前处理。

    1. 高通滤波：滤掉 85 Hz 以下的低频轰鸣（风扇、桌面震动、电流声）。
       这些成分对识别没有帮助，却会干扰声学特征，环境嘈杂时影响很明显。
    2. 自动增益：麦克风增益偏低时把音量抬到合适电平（只放大、不衰减）。
       声音太小是中文识别出错的主要原因之一。

    滤波器的状态跨音频块保留，避免块与块之间产生咔哒声。
    """

    def __init__(self, rate: int, cutoff: float = 85.0, q: float = 0.707):
        w0 = 2.0 * math.pi * cutoff / rate
        cos_w0 = math.cos(w0)
        alpha = math.sin(w0) / (2.0 * q)
        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        self._b0, self._b1, self._b2 = b0 / a0, b1 / a0, b0 / a0
        self._a1, self._a2 = a1 / a0, a2 / a0
        self._x1 = self._x2 = 0.0
        self._y1 = self._y2 = 0.0
        self._noise = 1e-4   # 缓慢跟随的底噪估计
        self._gain = 1.0

    def process(self, samples: np.ndarray) -> np.ndarray:
        b0, b1, b2, a1, a2 = self._b0, self._b1, self._b2, self._a1, self._a2
        x1, x2, y1, y2 = self._x1, self._x2, self._y1, self._y2
        out = np.empty_like(samples)
        for index in range(samples.size):
            x0 = float(samples[index])
            y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            out[index] = y0
            x2, x1 = x1, x0
            y2, y1 = y1, y0
        self._x1, self._x2, self._y1, self._y2 = x1, x2, y1, y2

        rms = float(np.sqrt(np.mean(out * out))) if out.size else 0.0
        # 底噪只允许缓慢下降，回升则非常慢，免得把一句话的开头当成噪声
        if rms < self._noise:
            self._noise = 0.8 * self._noise + 0.2 * rms
        else:
            self._noise = 0.9995 * self._noise + 0.0005 * rms

        target = 0.08
        gate = max(self._noise * 2.5, 3e-4)
        desired = min(5.0, max(1.0, target / rms)) if rms > gate else 1.0
        self._gain += (desired - self._gain) * 0.12  # 增益平滑变化，避免忽大忽小

        out *= self._gain
        np.clip(out, -1.0, 1.0, out=out)
        return out


class AudioEngine:
    """采集麦克风音频并实时送入 Vosk 识别。"""

    def __init__(self, models: dict, msg_queue: queue.Queue, enhance: bool = True):
        # 多语言：同时驱动多个识别器，每段语音挑「平均置信度最高」的语言作为结果
        self.models = models
        self._current_lang = next(iter(models), "")  # 实时预览用哪个语言，跟着最近一次定稿走
        self.msg = msg_queue
        self.enhance = enhance
        self.audio_q: queue.Queue = queue.Queue()
        self.rate = TARGET_RATE
        self.enhancer: AudioEnhancer | None = None
        self._stop = threading.Event()
        self._thread = None
        self._stream = None
        self._last_audio = 0.0
        self._warn_logged_at = 0.0
        # 自动标点要用「停顿时长」：在原始音频上量说话/静音，记录每段语音之前的停顿
        self._silence_start: float | None = None  # 当前这段静音是什么时候开始的
        self._pending_pause = 0.0                 # 最近一段语音之前的停顿秒数
        self._floor = 0.004                       # 缓慢跟随的底噪（归一化 RMS）
        self._peak = 0.04                         # 缓慢跟随的说话峰值（归一化 RMS）

    def start(self, device) -> None:
        self.rate = self._pick_rate(device)
        # 增强放在重采样之后（统一在 16 kHz 上做），样本更少、开销更低
        self.enhancer = AudioEnhancer(TARGET_RATE) if self.enhance else None
        self._stop.clear()
        self._drain()
        self._silence_start = None
        self._pending_pause = 0.0
        self._floor = 0.004
        self._peak = 0.04

        self._stream = sd.RawInputStream(
            samplerate=self.rate,
            blocksize=int(self.rate * BLOCK_SECONDS),
            dtype="int16",
            channels=1,
            device=device,
            callback=self._on_audio,
        )
        self._stream.start()

        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self.audio_q.put(None)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # -------------------------------------------------------------- 内部实现
    def _pick_rate(self, device) -> int:
        try:
            sd.check_input_settings(
                device=device, samplerate=TARGET_RATE, channels=1, dtype="int16"
            )
            return TARGET_RATE
        except Exception:
            pass

        native = int(sd.query_devices(device)["default_samplerate"])
        if native != TARGET_RATE and not HAS_SOXR:
            raise RuntimeError("该设备不支持 16000 Hz，且缺少 soxr 重采样库。")
        return native

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if self._stop.is_set():
            return
        self._last_audio = time.monotonic()
        if status:
            # 溢出之类的警告会疯狂刷屏，最多 5 秒记一条
            now = time.monotonic()
            if now - self._warn_logged_at > 5.0:
                self._warn_logged_at = now
                log(f"音频流警告：{status}")
        data = bytes(indata)
        self.audio_q.put(data)
        try:
            samples = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) if samples.size else 0.0
        except Exception:
            rms = 0.0
        self.msg.put(("level", rms))

    def _prepare(self, data: bytes) -> bytes:
        """原始音频 → 重采样到 16 kHz → 降噪/增益 → 交回 int16 字节。"""
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if self.rate != TARGET_RATE:
            samples = np.clip(soxr.resample(samples, self.rate, TARGET_RATE), -1.0, 1.0)
        if self.enhancer is not None:
            samples = self.enhancer.process(samples)
        return (samples * 32767.0).astype(np.int16).tobytes()

    def _thread_main(self) -> None:
        """识别线程的兜底：万一识别/重采样抛异常，别让线程悄悄死掉、界面还显示在聆听。"""
        try:
            self._run()
        except Exception as exc:
            log(f"识别线程异常退出：{type(exc).__name__}: {exc}")
            self.msg.put(("engine_error", f"{type(exc).__name__}: {exc}"))

    def _run(self) -> None:
        # 每个启用的语言一个识别器，同一路音频同时喂进去
        recognizers: dict[str, object] = {}
        for lang, model in self.models.items():
            rec = vosk.KaldiRecognizer(model, TARGET_RATE)
            rec.SetWords(True)  # 要每词置信度，多语言仲裁靠它
            recognizers[lang] = rec
        self._last_audio = time.monotonic()

        pending: dict[str, tuple[str, float]] = {}  # 本段语音各语言给出的候选结果
        pending_at = 0.0                            # 收到最近一个候选的时刻
        seg_pause = 0.0                             # 本段语音之前的停顿（自动标点用）

        while True:
            timed_out = False
            try:
                data = self.audio_q.get(timeout=0.3)
            except queue.Empty:
                timed_out = True
                data = None
                if self._stop.is_set():
                    break
                # 麦克风被拔掉/被别的程序独占时，回调会彻底不再触发，这里兜住
                if time.monotonic() - self._last_audio > MIC_STALL_SECONDS:
                    self.msg.put(("mic_stall", None))
                    break
            if data is None and not timed_out:
                break  # stop() 放进来的结束哨兵

            if data is not None:
                self._track_pause(data)
                prepared = self._prepare(data)
                got = False
                for lang, rec in recognizers.items():
                    if rec.AcceptWaveform(prepared):
                        text, conf = result_text_conf(rec.Result())
                        if text.strip():
                            if not pending:
                                # 这段语音的第一个候选：把「它之前的停顿」取走并清空
                                seg_pause, self._pending_pause = self._pending_pause, 0.0
                            pending[lang] = (text, conf)
                            got = True
                if got:
                    pending_at = time.monotonic()
                elif not pending:
                    # 实时预览只跟「当前语言」走，免得别的语言的中间结果乱入、文字闪跳
                    rec = recognizers.get(self._current_lang)
                    if rec is not None:
                        partial = json.loads(rec.PartialResult()).get("partial", "")
                        self.msg.put(("partial", partial))

            # 候选攒够了就定稿：同一段语音各语言的 final 会在这段窗口里一起比出高下
            if pending and time.monotonic() - pending_at >= FINAL_GRACE_SECONDS:
                self._emit_best(pending, seg_pause)
                pending = {}

        # 收尾：各识别器把尾巴吐出来，同样挑最可信的
        for lang, rec in recognizers.items():
            try:
                text, conf = result_text_conf(rec.FinalResult())
            except Exception:
                continue
            if text.strip():
                pending.setdefault(lang, (text, conf))
        if pending:
            self._emit_best(pending, self._pending_pause)
            self._pending_pause = 0.0
        self.msg.put(("partial", ""))

    def _emit_best(self, candidates: dict, pause: float) -> None:
        """多个语言的候选里挑最可信的一个（先比平均置信度，再比文本长度）。"""
        lang, (text, _conf) = max(
            candidates.items(), key=lambda kv: (kv[1][1], len(kv[1][0]))
        )
        self._current_lang = lang
        self.msg.put(("final", self._final_payload(text, lang, pause)))

    def _track_pause(self, raw: bytes) -> None:
        """在原始音频上量「说话 / 停顿」，给自动标点提供停顿时长。

        Vosk 的每段 final 都发生在静音处，所以一段语音之前往往刚有过静音。这里记录
        「最近一段语音开始前的静音有多长」，等该段的 final 送上去时一起带上，
        界面据此判断两句之间该不该补标点、补哪种。

        门限跟着实测音量走（底噪 / 说话峰值各自缓慢跟随），所以安静麦克风也能判出
        「在说话」，高增益麦克风也不会把底噪当说话——固定阈值两头都会翻车。
        """
        samples = np.frombuffer(raw, dtype=np.int16)
        if not samples.size:
            return
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 32768.0

        if rms < self._floor:
            self._floor += SPEECH_FLOOR_DOWN * (rms - self._floor)
        else:
            self._floor += SPEECH_FLOOR_UP * (rms - self._floor)
        if rms > self._peak:
            self._peak = rms
        else:
            self._peak = SPEECH_PEAK_DECAY * self._peak + (1.0 - SPEECH_PEAK_DECAY) * rms
        gate = max(
            SPEECH_GATE_FRAC * self._peak,
            self._floor * 2.0,
            SPEECH_GATE_MIN,
        )

        now = time.monotonic()
        if rms > gate:
            if self._silence_start is not None:
                self._pending_pause = now - self._silence_start
                self._silence_start = None
        elif self._silence_start is None:
            self._silence_start = now

    def _final_payload(self, text: str, lang: str, pause: float) -> dict:
        """把这段文字、它被判定的语言、以及「它之前的停顿」打包送出去。"""
        if pause >= AUTO_PAUSE_CLAUSE:
            log(f"识别到停顿 {pause:.2f} 秒（用于自动标点）")
        return {"text": text, "pause": pause, "lang": lang}

    def _drain(self) -> None:
        while True:
            try:
                self.audio_q.get_nowait()
            except queue.Empty:
                return


# ------------------------------------------------------------------- 界面
# 配色集中放这里，主窗口和弹窗共用一套；浅色/深色两套方案，切换后重建界面即可
THEMES = {
    # 参照 TraeWork 的视觉：浅灰画布（#f5f5f5）+ 纯白面板 + 几乎看不见的描边，
    # 只用一种靛蓝强调色（#4b3fe3），文字近黑、次级文字中性灰。
    "浅色": {
        "bg": "#f5f5f5", "card": "#ffffff", "border": "#e8e8e8",
        "text": "#171717", "muted": "#8a8a8a",
        "accent": "#4b3fe3", "accent_dark": "#3a2fd0", "accent_fg": "#ffffff",
        "header_bg": "#ffffff", "header_fg": "#171717", "header_sub": "#8a8a8a",
        "badge_bg": "#f0f0f0", "badge_fg": "#5a5a5a",
        "input_bg": "#ffffff", "input_disabled": "#f5f5f5",
        "scroll_bg": "#d4d4d4", "scroll_trough": "#f5f5f5",
        "scroll_arrow": "#8a8a8a", "scroll_active": "#b0b0b0",
        "select_bg": "#e6e2ff",
        "sec_bg": "#ffffff", "sec_hover": "#f5f5f5", "sec_active": "#ececec",
        "sec_disabled_bg": "#fafafa", "btn_disabled_fg": "#c4c4c4",
        "ghost_hover": "#f0f0f0", "ghost_active": "#e6e2ff",
        "primary_disabled_bg": "#c7c2f5", "primary_disabled_fg": "#ffffff",
        "warn_bg": "#fff7e6", "warn_border": "#ffe0a3", "warn_fg": "#8a5a00",
        "partial": "#a1a1a1", "hint_title": "#8a8a8a", "hint_sub": "#b0b0b0",
        "level_bg": "#ededed", "ok": "#16a34a", "over": "#e5484d",
        "dot_ok": "#22c55e", "dot_warn": "#f5a623", "dot_err": "#e5484d",
        "note": "#8a8a8a", "faint": "#b0b0b0",
    },
    "深色": {
        "bg": "#1a1a1a", "card": "#242424", "border": "#333333",
        "text": "#ededed", "muted": "#9a9a9a",
        "accent": "#6c63f5", "accent_dark": "#5a51e0", "accent_fg": "#ffffff",
        "header_bg": "#202020", "header_fg": "#f5f5f5", "header_sub": "#9a9a9a",
        "badge_bg": "#2e2e2e", "badge_fg": "#b0b0b0",
        "input_bg": "#1a1a1a", "input_disabled": "#242424",
        "scroll_bg": "#3d3d3d", "scroll_trough": "#242424",
        "scroll_arrow": "#8a8a8a", "scroll_active": "#555555",
        "select_bg": "#3a3560",
        "sec_bg": "#2e2e2e", "sec_hover": "#383838", "sec_active": "#424242",
        "sec_disabled_bg": "#242424", "btn_disabled_fg": "#5a5a5a",
        "ghost_hover": "#2e2e2e", "ghost_active": "#3a3560",
        "primary_disabled_bg": "#3a3560", "primary_disabled_fg": "#9a9a9a",
        "warn_bg": "#3a2e10", "warn_border": "#5a4718", "warn_fg": "#f0d48a",
        "partial": "#8a8a8a", "hint_title": "#9a9a9a", "hint_sub": "#6a6a6a",
        "level_bg": "#333333", "ok": "#3dd68c", "over": "#f2555a",
        "dot_ok": "#3dd68c", "dot_warn": "#f5a623", "dot_err": "#f2555a",
        "note": "#9a9a9a", "faint": "#6a6a6a",
    },
}
THEME_ORDER = ("浅色", "深色")
DEFAULT_THEME = "浅色"


def round_rect(canvas: tk.Canvas, x0, y0, x1, y1, r, **kwargs):
    """在 Canvas 上画圆角矩形。

    tkinter 没有原生圆角，用「多边形 + 平滑」近似：每个角放两个控制点，
    smooth=True 会把折线拟合成圆角，观感接近 TraeWork 的圆润卡片。
    """
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    points = [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def _set_btn_enabled(btn, enabled: bool) -> None:
    """统一切换按钮可用状态；自绘按钮的底色要跟着变，否则看起来仍像可点。"""
    btn.config(state="normal" if enabled else "disabled")
    palette = getattr(btn, "_palette", None)
    if palette:
        btn.config(bg=palette["bg"] if enabled else palette["disabled_bg"])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # 高分屏适配：窗口尺寸和字号一起按系统 DPI 放大，避免界面又小又挤
        self.scale = system_scale()
        self.tk.call("tk", "scaling", self.scale * 96.0 / 72.0)
        self.title("语音转文字")
        self.geometry(f"{int(980 * self.scale)}x{int(900 * self.scale)}")
        self.minsize(int(860 * self.scale), int(640 * self.scale))

        # 主题：浅色/深色两套配色，先按设置里的选择加载，之后可随时切换
        self._settings = load_settings()
        saved_theme = self._settings.get("theme")
        self.theme = tk.StringVar(
            value=saved_theme if saved_theme in THEMES else DEFAULT_THEME
        )
        self.palette = dict(THEMES[self.theme.get()])
        self.configure(bg=self.palette["bg"])
        self._apply_window_icon()

        # 上次选用的麦克风（按名字记住：设备序号会随插拔变化，名字才稳定）
        # 以及设置面板是否折叠（折叠后把空间让给识别结果）
        self._mic_pref = str(self._settings.get("mic", "") or "")
        self._settings_collapsed = bool(self._settings.get("compact", False))

        self.msg: queue.Queue = queue.Queue()
        self.engine: AudioEngine | None = None
        self._models: dict = {}      # 已加载的语言模型 {语言: vosk.Model}
        self._model_names: dict[str, str] = {}  # 各语言实际用的模型目录名，用于状态展示
        self.model_loaded = False
        self.running = False

        self.final_text = ""
        self.partial_text = ""
        # 声调拼音的缓存：partial 每来一次就会重画界面，不能每次都把整段文字重算一遍拼音。
        # 键里带上「声调标注当前是否生效」，否则关掉开关后旧拼音仍会显示、打开后又不出现。
        self._pinyin_cache: tuple[tuple[str, bool], str] = (("", False), "")
        self._devices: list[tuple[int, str]] = []
        self._device_names: list[str] = []
        self._last_typed = ""
        self._countdown_job = None
        self._enter_job = None
        self._session_typed = False   # 本次录音有没有成功打出过字
        self._session_had_text = False  # 本次录音有没有识别出过文字
        self._rec_started_at = 0.0
        # 自动标点：按住说话模式每次松手都会重建引擎，段内量不到停顿，所以跨录音的
        # 「上次说完到这次开口」这段静音在界面侧补量（见 _start / _poll）。
        self._last_final_at: float | None = None  # 上一条识别结果是什么时候定稿的
        self._session_gap = 0.0                   # 本次录音开始前，距上次说完过了多久
        self._first_final_pending = False         # 本次录音的第一条结果还没用上跨录音停顿
        self._fallback_tried: set[str] = set()
        self._downloading = False
        self._dl_thread = None  # 当前下载线程；用来判断「下载中」是真在跑还是状态残留
        self._dl_cancel = threading.Event()  # 下载中置位表示用户点了「取消下载」
        self._dl_at = 0.0       # 上次采样下载速度的时刻
        self._dl_done = 0       # 上次采样时已下载的字节数
        self._dl_speed = 0.0    # 最近一次算出的下载速度（字节/秒）
        self._dl_ui_at = 0.0    # 上次刷新进度 UI 的时刻（限流用）
        self._dl_mirror = ""    # 本次下载使用的自定义镜像地址（空则用内置源）
        self._dl_language = DEFAULT_LANGUAGE  # 本次下载针对的语言
        self._model_token = 0   # 模型加载代次：语言切来切去时用来丢弃过期的加载结果
        # 语言勾选快速连点时会有多个加载线程并存，它们会抢同一个 .part 下载文件；
        # 用一把锁把模型准备串行化，避免并发下载互相踩坏断点文件
        self._model_lock = threading.Lock()
        # 已加载模型缓存 {语言: (模型目录名, vosk.Model)}：换语言时只加载新增的，
        # 已加载过的直接复用，否则勾一下别的语言就要把 1.3 GB 的中文模型重读一遍
        self._model_cache: dict[str, tuple[str, object]] = {}
        self._dict_window = None
        self.replacements = load_dict_file()
        saved_mode = self._settings.get("inject_mode")
        saved_hotkey = self._settings.get("hotkey")
        self.global_input = tk.BooleanVar(
            value=bool(self._settings.get("global_input", True))
        )
        self.auto_enter = tk.BooleanVar(
            value=bool(self._settings.get("auto_enter", False))
        )
        self.sound_on = tk.BooleanVar(
            value=bool(self._settings.get("sound_on", True))
        )
        self.topmost = tk.BooleanVar(
            value=bool(self._settings.get("topmost", False))
        )
        self.enhance = tk.BooleanVar(
            value=bool(self._settings.get("enhance", True))
        )
        self.punctuation = tk.BooleanVar(
            value=bool(self._settings.get("punctuation", True))
        )
        self.auto_punct = tk.BooleanVar(
            value=bool(self._settings.get("auto_punct", True))
        )
        # 可识别的语言（多选）：勾上的语言会各自下载/加载模型，识别时自动判断说的是哪一种
        saved_languages = self._settings.get("languages")
        if not isinstance(saved_languages, list):
            # 兼容旧设置：以前是单选，存在 language 键里
            old = self._settings.get("language")
            saved_languages = [old] if old in LANGUAGES else [DEFAULT_LANGUAGE]
        saved_languages = [lang for lang in LANGUAGE_ORDER if lang in saved_languages]
        if not saved_languages:
            saved_languages = [DEFAULT_LANGUAGE]
        self.lang_vars = {
            lang: tk.BooleanVar(value=lang in saved_languages) for lang in LANGUAGE_ORDER
        }
        self.tone = tk.BooleanVar(value=bool(self._settings.get("tone", True)))
        # 以注册表实际状态为准，避免设置文件和真实状态不一致
        self.autostart = tk.BooleanVar(value=autostart_enabled())
        self.inject_mode = tk.StringVar(
            value=saved_mode if saved_mode in INJECT_MODES else INJECT_KEY
        )
        self.hotkey = tk.StringVar(
            value=saved_hotkey if saved_hotkey in HOTKEYS else DEFAULT_HOTKEY
        )
        # 自定义下载源：默认留空，走内置的 HF 镜像；填了就先试它（可用代理或内网镜像）
        self.model_mirror = tk.StringVar(
            value=str(self._settings.get("模型镜像", "") or "")
        )

        self._build_ui()
        # 上次关窗时的大小/位置优先；没有或已失效就按内容自适应
        if not self._restore_geometry():
            self._fit_window()
        self.after(0, self._clamp_to_work_area)
        self.after(150, self._apply_window_style)
        self.attributes("-topmost", bool(self.topmost.get()))
        self._set_status("正在准备识别模型…")
        self._refresh_devices()  # 没有麦克风时这里会覆盖成更明确的提示
        self._set_buttons(running=False, ready=False)
        self.after(60, self._poll)
        self._start_model_load()

        self.ptt = None
        self._start_listener()
        self._bind_shortcuts()
        log(
            f"界面就绪｜热键={self.hotkey.get()}｜输入方式={self.inject_mode.get()}"
            f"｜全局输入={self.global_input.get()}｜音频增强={self.enhance.get()}"
        )

    # ---------------------------------------------------------------- 界面搭建
    def _px(self, value: int) -> int:
        """把按 96 DPI 设计的尺寸换算成当前 DPI 下的像素。"""
        return max(1, int(round(value * self.scale)))

    def _make_button(self, parent, text, command, kind="secondary", width=None,
                     font=None, big=False):
        """自绘扁平按钮，保证各处按钮配色/悬停手感一致（ttk 在 Windows 上做不到）。"""
        px = self._px
        p = self.palette
        palette = {
            "primary": dict(
                bg=p["accent"], fg=p["accent_fg"], hover=p["accent_dark"],
                active=p["accent_dark"],
                disabled_bg=p["primary_disabled_bg"], disabled_fg=p["primary_disabled_fg"],
                border=p["accent"],
            ),
            "secondary": dict(
                bg=p["sec_bg"], fg=p["text"], hover=p["sec_hover"], active=p["sec_active"],
                disabled_bg=p["sec_disabled_bg"], disabled_fg=p["btn_disabled_fg"],
                border=p["border"],
            ),
            "ghost": dict(
                bg=p["card"], fg=p["accent"], hover=p["ghost_hover"],
                active=p["ghost_active"],
                disabled_bg=p["card"], disabled_fg=p["btn_disabled_fg"],
                border=p["card"],
            ),
        }[kind]
        btn = tk.Button(
            parent, text=text, command=command, relief="flat", bd=0, cursor="hand2",
            font=font or ("Microsoft YaHei UI", 10),
            padx=px(20 if big else 12), pady=px(8 if big else 5),
            bg=palette["bg"], fg=palette["fg"],
            activebackground=palette["active"], activeforeground=palette["fg"],
            disabledforeground=palette["disabled_fg"],
            highlightthickness=1, highlightbackground=palette["border"],
            highlightcolor=palette["border"], takefocus=False,
        )
        if width:
            btn.config(width=width)
        btn._palette = palette

        def hover_on(_event):
            if str(btn["state"]) != "disabled":
                btn.config(bg=palette["hover"])

        def hover_off(_event):
            if str(btn["state"]) != "disabled":
                btn.config(bg=palette["bg"])

        btn.bind("<Enter>", hover_on)
        btn.bind("<Leave>", hover_off)
        return btn

    def _work_area(self) -> tuple[int, int, int, int]:
        """桌面工作区（已扣掉任务栏）的 left/top/right/bottom，单位与 tkinter 一致。

        不能用 winfo_screenheight()：那是整屏高度，窗口底栏会被任务栏盖住。
        """
        try:
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0  # SPI_GETWORKAREA
            ):
                if rect.right > rect.left and rect.bottom > rect.top:
                    return rect.left, rect.top, rect.right, rect.bottom
        except Exception as exc:
            log(f"读取工作区失败：{type(exc).__name__}: {exc}")
        return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def _deco_height(self) -> int:
        """标题栏+上下边框占用的高度；窗口还没映射时退回估算值。"""
        try:
            hwnd = int(self.wm_frame(), 16)
            rect = wintypes.RECT()
            if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                deco = (rect.bottom - rect.top) - self.winfo_height()
                if 0 < deco < self._px(200):
                    self._deco_hint = deco
                    return deco
        except Exception:
            pass
        return getattr(self, "_deco_hint", self._px(38))

    def _min_usable_height(self) -> int:
        """保证识别结果区至少留得下 MIN_RESULT_HEIGHT 时，窗口内容区所需的最小高度。

        窗口比内容矮时，pack 会把唯一可伸缩的识别结果区压到 1px（结果彻底看不见）。
        这里按「固定控件高度 + 结果区保底高度」推算下限，再卡在工作区内，
        供还原窗口 / 设置最小尺寸时使用。折叠设置后固定控件变矮，下限自然跟着放松。
        """
        try:
            self.update_idletasks()
            fixed = max(0, self.winfo_reqheight() - self.text.winfo_reqheight())
            need = fixed + self._px(MIN_RESULT_HEIGHT)
        except Exception:
            need = self._px(840)
        wl, wt, wr, wb = self._work_area()
        deco = self._deco_height()
        avail = max(self._px(420), wb - wt - deco)
        return max(self._px(420), min(need, avail))

    def _fit_window(self) -> None:
        """把窗口调到刚好容纳全部控件，并完整落在工作区内（不被任务栏遮挡）。"""
        try:
            self.update_idletasks()
            wl, wt, wr, wb = self._work_area()
            avail_w, avail_h = wr - wl, wb - wt
            margin = self._px(10)
            deco = self._deco_height()
            # 需要的尺寸 = 设计尺寸与控件实际请求尺寸的较大者，再限制在工作区内
            need_w = max(int(980 * self.scale), self.winfo_reqwidth())
            need_h = max(int(840 * self.scale), self.winfo_reqheight())
            w = min(need_w, max(self._px(600), avail_w - margin * 2))
            # 结果区不需要占满整屏：按工作区的 91% 收一收，四周留出桌面留白
            comfy_h = int((avail_h - deco) * 0.91)
            h = min(need_h, max(self._px(420), min(avail_h - deco - margin * 2, comfy_h)))
            # 再兜一层：别让结果区被压没
            h = max(h, self._min_usable_height())
            x = wl + max(margin, (avail_w - w) // 2)
            y = wt + max(margin, (avail_h - deco - h) // 2)
            self.geometry(f"{w}x{h}+{x}+{y}")
            # 最小尺寸不能超过窗口本身，否则窗口会被强行撑大、控件反而被挤掉
            self.minsize(
                min(int(860 * self.scale), w), min(self._min_usable_height(), h)
            )
        except Exception as exc:
            log(f"调整窗口尺寸失败：{type(exc).__name__}: {exc}")

    def _restore_geometry(self) -> bool:
        """还原上次关窗时的大小与位置；没有记录或已经越界就返回 False。

        越界的情况要挡掉：换了显示器、分辨率变小之后，旧坐标可能整个跑到屏幕外，
        那样窗口就「打开看不见」了，不如退回自适应。
        """
        saved = self._settings.get("window")
        if not isinstance(saved, dict):
            return False
        try:
            w, h = int(saved["w"]), int(saved["h"])
            x, y = int(saved["x"]), int(saved["y"])
        except Exception:
            return False
        wl, wt, wr, wb = self._work_area()
        deco = self._deco_height()
        min_w, min_h = self._px(600), self._px(420)
        if w < min_w or h < min_h:
            return False
        # 超出工作区就收回来，保证标题栏和底栏都在可见范围内
        w = min(w, wr - wl)
        h = min(h, wb - wt - deco)
        # 旧版本存下的窗口可能比现在的内容矮（比如后来加了设置卡片），
        # 直接还原会把识别结果区压到 1px —— 抬到结果区还能用的高度。
        h = max(h, self._min_usable_height())
        x = max(wl, min(x, wr - w))
        y = max(wt, min(y, wb - deco - h))
        try:
            self.geometry(f"{w}x{h}+{x}+{y}")
            self.minsize(
                min(int(860 * self.scale), w), min(self._min_usable_height(), h)
            )
        except Exception as exc:
            log(f"还原窗口位置失败：{type(exc).__name__}: {exc}")
            return False
        return True

    def _save_geometry(self) -> None:
        """记住窗口大小和位置，下次启动原样还原。"""
        try:
            self.update_idletasks()
            w, h = self.winfo_width(), self.winfo_height()
            if w <= 1 or h <= 1:  # 窗口没真正映射过，存了也没意义
                return
            data = load_settings()
            data["window"] = {
                "w": int(w), "h": int(h),
                "x": int(self.winfo_x()), "y": int(self.winfo_y()),
            }
            save_settings(data)
        except Exception as exc:
            log(f"保存窗口位置失败：{type(exc).__name__}: {exc}")

    def _toggle_theme(self) -> None:
        """浅色/深色切换：换一套配色后重建界面，并记住选择。"""
        new = "深色" if self.theme.get() == "浅色" else "浅色"
        self.theme.set(new)
        self.palette = dict(THEMES[new])
        self._save_settings()
        log(f"切换主题：{new}")
        self._rebuild_ui()
        self._set_status(f"已切换到{new}主题")

    def _toggle_settings(self) -> None:
        """折叠/展开设置卡片，把纵向空间让给识别结果。"""
        self._settings_collapsed = not self._settings_collapsed
        self._apply_settings_visibility()
        self._save_settings()
        self._set_status(
            "设置已折叠，识别结果区更大" if self._settings_collapsed else "设置已展开"
        )

    def _apply_settings_visibility(self) -> None:
        """按当前折叠状态显示/隐藏设置区；重新 pack 时插回横幅之前，保持顺序不变。"""
        area = getattr(self, "settings_area", None)
        btn = getattr(self, "settings_btn", None)
        if area is None or not area.winfo_exists():
            return
        if self._settings_collapsed:
            area.pack_forget()
        else:
            before = getattr(self, "_banner", None) or self.result_frame
            area.pack(fill="x", before=before)
        if btn is not None and btn.winfo_exists():
            btn.config(text="显示设置" if self._settings_collapsed else "隐藏设置")
        # 折叠后内容变矮，最小高度跟着放松；展开则收紧，免得用户把窗口缩到结果区看不见。
        # 若当前窗口比新下限还矮，窗口管理器会把它撑到下限。
        try:
            self.update_idletasks()
            self.minsize(self.minsize()[0], self._min_usable_height())
        except Exception:
            pass

    def _rebuild_ui(self) -> None:
        """按当前配色重建界面。

        主题切换只需换一套颜色，但控件颜色是建的时候就定死的，
        与其逐个改属性，不如整体重建 —— 界面状态（文本、设置、模型）都在属性上，重建后补回来即可。
        """
        status = self.status_var.get() if hasattr(self, "status_var") else ""
        for child in self.winfo_children():
            child.destroy()
        self._dict_window = None
        self.configure(bg=self.palette["bg"])
        self._build_ui()
        self._refresh_devices()
        if self.model_loaded:
            self.model_label.config(text=f"模型：已就绪 · {self._models_label()}")
        self._render()
        self._set_buttons(self.running, self.model_loaded)
        self._set_status(status or "就绪")
        self.after(30, self._apply_window_style)

    def _clamp_to_work_area(self) -> None:
        """窗口映射后用真实标题栏高度再校正一次，兜住残留的越界。"""
        if not self.winfo_ismapped():
            self.after(80, self._clamp_to_work_area)
            return
        try:
            self.update_idletasks()
            wl, wt, wr, wb = self._work_area()
            deco = self._deco_height()
            if self.winfo_height() + deco > wb - wt:
                self.geometry(
                    f"{self.winfo_width()}x{max(self._px(420), wb - wt - deco)}"
                )
                self.update_idletasks()
            total_h = self.winfo_height() + deco
            x = max(wl, min(self.winfo_x(), wr - self.winfo_width()))
            y = max(wt, min(self.winfo_y(), wb - total_h))
            self.geometry(f"+{x}+{y}")
            log(f"窗口定位：{self.winfo_width()}x{self.winfo_height()}+{x}+{y}"
                f"（标题栏 {deco}px，工作区 {wb - wt}px）")
        except Exception as exc:
            log(f"校正窗口位置失败：{type(exc).__name__}: {exc}")

    def _apply_window_icon(self) -> None:
        """给窗口和任务栏设置图标；图标文件不在就跳过，不影响启动。"""
        icon = APP_DIR / "app.ico"
        if not icon.exists():
            return
        try:
            self.iconbitmap(default=str(icon))
        except Exception as exc:
            log(f"设置窗口图标失败：{type(exc).__name__}: {exc}")

    def _apply_window_style(self) -> None:
        """Win11 窗口圆角 + 跟随主题的深色标题栏（DWM），贴近 TraeWork 的圆润观感。

        只是观感增强：非 Win11 或不支持时静默跳过，绝不影响启动。
        """
        try:
            hwnd = int(self.wm_frame(), 16)
        except Exception:
            return
        if not hwnd:
            return
        try:
            # DWMWA_WINDOW_CORNER_PREFERENCE(33) = DWMWCP_ROUND(2)
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)
            )
            # DWMWA_USE_IMMERSIVE_DARK_MODE(20)：深色主题时把标题栏一并变深
            dark = ctypes.c_int(1 if self.theme.get() == "深色" else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark)
            )
        except Exception:
            pass

    def _build_ui(self) -> None:
        ui_font = ("Microsoft YaHei UI", 10)
        bold_font = ("Microsoft YaHei UI", 10, "bold")
        small_font = ("Microsoft YaHei UI", 9)
        # 配色统一取自当前主题（浅色/深色），重建界面时同一段代码就能换肤
        p = self.palette
        BG, CARD, BORDER = p["bg"], p["card"], p["border"]
        TEXT, MUTED = p["text"], p["muted"]
        ACCENT, ACCENT_DARK = p["accent"], p["accent_dark"]
        HEADER_BG = p["header_bg"]
        FAINT = p["faint"]

        def px(value: int) -> int:
            """把按 96 DPI 设计的间距换算成当前 DPI 下的像素。"""
            return max(1, int(round(value * self.scale)))

        make_button = self._make_button
        PAD = px(18)

        # 统一用 clam 主题：vista 主题下无法自定义按钮配色，会和自绘的主按钮风格打架
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TLabel", font=ui_font)
        style.configure("TCheckbutton", font=ui_font)
        style.configure(
            "TCombobox", font=ui_font, padding=(px(6), px(3)), arrowsize=px(12),
            fieldbackground=p["input_bg"], background=p["input_bg"],
            foreground=TEXT, bordercolor=BORDER,
            lightcolor=p["input_bg"], darkcolor=p["input_bg"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", p["input_bg"]), ("disabled", p["input_disabled"])],
            foreground=[("readonly", TEXT), ("disabled", MUTED)],
            bordercolor=[("focus", ACCENT)],
            arrowcolor=[("disabled", p["btn_disabled_fg"])],
        )
        style.configure(
            "Vertical.TScrollbar", background=p["scroll_bg"], troughcolor=p["scroll_trough"],
            bordercolor=p["scroll_trough"], arrowcolor=p["scroll_arrow"],
            relief="flat", arrowsize=px(12),
        )
        style.map("Vertical.TScrollbar", background=[("active", p["scroll_active"])])
        # 下拉列表的配色不受 ttk 样式控制，得走 option 数据库
        self.option_add("*TCombobox*Listbox.font", ui_font)
        self.option_add("*TCombobox*Listbox.background", p["input_bg"])
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", p["accent_fg"])

        def card(title: str, subtitle: str = "") -> tk.Frame:
            """带标题的设置卡片，返回可放内容的容器。放进 settings_area，便于整体折叠。"""
            outer = tk.Frame(
                self.settings_area, bg=CARD, highlightbackground=BORDER, highlightthickness=1
            )
            outer.pack(fill="x", padx=PAD, pady=(0, px(7)))
            head = tk.Frame(outer, bg=CARD)
            head.pack(fill="x", padx=px(16), pady=(px(9), 0))
            tk.Label(head, text=title, bg=CARD, fg=TEXT, font=bold_font).pack(side="left")
            if subtitle:
                tk.Label(head, text=subtitle, bg=CARD, fg=MUTED, font=small_font).pack(
                    side="left", padx=(px(8), 0)
                )
            inner = tk.Frame(outer, bg=CARD)
            inner.pack(fill="x", padx=px(16), pady=(px(7), px(11)))
            return inner

        def check(parent, text, var, command=None):
            return tk.Checkbutton(
                parent, text=text, variable=var, command=command, bg=CARD, fg=TEXT,
                activebackground=CARD, activeforeground=TEXT, selectcolor=CARD,
                font=ui_font, anchor="w", highlightthickness=0, bd=0, cursor="hand2",
            )

        def muted(parent, text, **kw):
            return tk.Label(parent, text=text, bg=CARD, fg=MUTED, font=small_font, **kw)

        # ---------------------------------------------------------- 顶栏
        # TraeWork 的顶部条是浅色而非深色：白底 + 一条发丝线分隔，标题前放一个应用标识
        header = tk.Frame(self, bg=HEADER_BG)
        header.pack(fill="x")
        left = tk.Frame(header, bg=HEADER_BG)
        left.pack(side="left", padx=PAD, pady=px(11))

        mark = tk.Canvas(
            left, width=px(26), height=px(26), bg=HEADER_BG, highlightthickness=0
        )
        mark.pack(side="left", padx=(0, px(9)))
        round_rect(mark, px(1), px(1), px(25), px(25), px(7), fill=ACCENT, outline="")
        mark.create_text(
            px(13), px(14), text="语", fill=p["accent_fg"],
            font=("Microsoft YaHei UI", 11, "bold"),
        )

        tk.Label(
            left, text="语音转文字", bg=HEADER_BG, fg=p["header_fg"],
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left")
        self.status_dot = tk.Canvas(
            left, width=px(12), height=px(12), bg=HEADER_BG, highlightthickness=0
        )
        self.status_dot.pack(side="left", padx=(px(16), px(7)))
        self._dot_id = self.status_dot.create_oval(
            px(1), px(1), px(11), px(11), fill=FAINT, outline=""
        )
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(
            left, textvariable=self.status_var, bg=HEADER_BG, fg=p["header_sub"], font=ui_font
        ).pack(side="left")

        # 模型信息做成小徽章，和左侧标题区在视觉上分开
        badge = tk.Frame(header, bg=p["badge_bg"])
        badge.pack(side="right", padx=PAD, pady=px(11))
        self.model_label = tk.Label(
            badge, text="模型：准备中…", bg=p["badge_bg"], fg=p["badge_fg"], font=small_font
        )
        self.model_label.pack(padx=px(11), pady=px(4))

        # 主题切换放在顶栏右侧：随手就能换，切换后立刻生效并记住
        self.theme_btn = make_button(
            header, "深色" if self.theme.get() == "浅色" else "浅色",
            self._toggle_theme, "ghost",
        )
        self.theme_btn.pack(side="right", padx=(0, px(10)), pady=px(12))

        # 折叠/展开设置面板：折叠后把纵向空间让给识别结果，界面更清爽
        self.settings_btn = make_button(
            header, "隐藏设置" if not self._settings_collapsed else "显示设置",
            self._toggle_settings, "ghost",
        )
        self.settings_btn.pack(side="right", padx=(0, px(2)), pady=px(12))

        # 顶栏与下方内容之间的一条发丝线（1px），比整块深色顶栏更接近 TraeWork
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        # ---------------------------------------------------------- 工具条
        toolbar = tk.Frame(self, bg=BG)
        toolbar.pack(fill="x", padx=PAD, pady=(px(10), px(8)))
        tk.Label(toolbar, text="麦克风", bg=BG, fg=MUTED, font=small_font).pack(side="left")
        self.device_combo = ttk.Combobox(toolbar, width=30, state="readonly")
        self.device_combo.pack(side="left", padx=(px(8), px(6)))
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_change)
        self.refresh_btn = make_button(toolbar, "刷新", self._refresh_devices, "secondary")
        self.refresh_btn.pack(side="left")

        # 只把「开始/停止」留在这里：它们是主操作，不该和文本操作按钮挤在一起
        self.start_btn = make_button(
            toolbar, "开始识别", self._start, "primary", big=True,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.start_btn.pack(side="right")
        self.stop_btn = make_button(toolbar, "停止", self._stop, "secondary")
        self.stop_btn.pack(side="right", padx=(0, px(8)))

        # 设置卡片统一放进这个容器：折叠时整体 pack_forget，把高度让给下面的识别结果区
        self.settings_area = tk.Frame(self, bg=BG)
        self.settings_area.pack(fill="x")

        # ---------------------------------------------------------- 输入卡片
        inp = card("语音输入", "说话的文字直接打进你正在用的程序")

        row1 = tk.Frame(inp, bg=CARD)
        row1.pack(fill="x")
        self.global_check = check(
            row1, "把识别结果直接打进其他程序（当前光标处）",
            self.global_input, self._on_global_toggle,
        )
        self.global_check.pack(side="left")
        self.hotkey_combo = ttk.Combobox(
            row1, width=9, state="readonly", values=list(HOTKEYS)
        )
        self.hotkey_combo.current(list(HOTKEYS).index(self.hotkey.get()))
        self.hotkey_combo.pack(side="right")
        self.hotkey_combo.bind("<<ComboboxSelected>>", self._on_hotkey_change)
        tk.Label(row1, text="热键", bg=CARD, fg=MUTED).pack(side="right", padx=(0, px(6)))
        self.hotkey_label = muted(row1, f"按住 {self.hotkey.get()} 说话，松开结束")
        self.hotkey_label.pack(side="right", padx=(0, px(12)))

        row2 = tk.Frame(inp, bg=CARD)
        row2.pack(fill="x", pady=(px(7), 0))
        tk.Label(row2, text="输入方式", bg=CARD, fg=TEXT).pack(side="left")
        self.inject_combo = ttk.Combobox(
            row2, width=18, state="readonly", values=list(INJECT_MODES)
        )
        self.inject_combo.current(INJECT_MODES.index(self.inject_mode.get()))
        self.inject_combo.pack(side="left", padx=(px(8), px(10)))
        self.inject_combo.bind("<<ComboboxSelected>>", self._on_inject_change)
        self.inject_hint = muted(row2, INJECT_HINTS[self.inject_mode.get()])
        self.inject_hint.pack(side="left")
        self.test_btn = make_button(row2, "测试输入", self._test_inject, "secondary")
        self.test_btn.pack(side="right")
        self.log_btn = make_button(row2, "打开日志", self._open_log, "secondary")
        self.log_btn.pack(side="right", padx=(0, px(6)))

        row3 = tk.Frame(inp, bg=CARD)
        row3.pack(fill="x", pady=(px(7), 0))
        check(row3, "说完自动回车", self.auto_enter, self._on_option_change).pack(side="left")
        check(row3, "按键提示音", self.sound_on, self._on_option_change).pack(side="left", padx=(px(14), 0))
        check(row3, "窗口置顶", self.topmost, self._on_option_change).pack(side="left", padx=(px(14), 0))
        check(row3, "开机自启动", self.autostart, self._on_autostart_change).pack(side="left", padx=(px(14), 0))

        # ---------------------------------------------------------- 识别语言卡片
        # 语言与精度合成一张卡：分开两张会多出一整套卡片边距，把结果区挤没
        acc = card("识别语言与精度", "可勾选多种语言，识别时自动判断说的是哪一种")
        lrow = tk.Frame(acc, bg=CARD)
        lrow.pack(fill="x")
        tk.Label(lrow, text="可识别的语言", bg=CARD, fg=TEXT).pack(side="left")
        self.lang_checks: dict[str, tk.Widget] = {}
        for lang in LANGUAGE_ORDER:
            cb = check(
                lrow, lang, self.lang_vars[lang],
                lambda l=lang: self._on_language_change(l),
            )
            cb.pack(side="left", padx=(px(12), 0))
            self.lang_checks[lang] = cb
        self.language_hint = muted(lrow, self._language_hint())
        self.language_hint.pack(side="right")

        lrow2 = tk.Frame(acc, bg=CARD)
        lrow2.pack(fill="x", pady=(px(6), 0))
        self.tone_check = check(
            lrow2, "声调标注（结果下方显示带声调拼音，便于核对同音字）",
            self.tone, self._on_tone_change,
        )
        self.tone_check.pack(side="left")
        self.tone_hint = muted(lrow2, "")
        self.tone_hint.pack(side="right", padx=(px(10), 0))
        self._update_tone_state()

        arow = tk.Frame(acc, bg=CARD)
        arow.pack(fill="x", pady=(px(6), 0))
        self.accuracy_label = tk.Label(arow, text=self._accuracy_text(), bg=CARD, fg=TEXT)
        self.accuracy_label.pack(side="left")
        # 先 pack 增强开关（占最右），再 pack 下载按钮（落在它左边）：
        # 这样之后切换语言重新 pack 下载按钮时，它也不会跑到开关右边去
        check(arow, "增强音频（降噪+增益）", self.enhance, self._on_option_change).pack(
            side="right", padx=(px(12), 0)
        )
        self.download_btn = make_button(
            arow, self._download_btn_text(), self._download_large_model, "secondary",
        )
        if self._large_download_visible():
            self.download_btn.pack(side="right")

        # 自定义下载镜像：内置镜像不可用时，可填自己的加速地址（代理 / 内网镜像）
        mrow = tk.Frame(acc, bg=CARD)
        mrow.pack(fill="x", pady=(px(6), 0))
        tk.Label(mrow, text="下载镜像", bg=CARD, fg=TEXT).pack(side="left")
        self.mirror_entry = tk.Entry(
            mrow, textvariable=self.model_mirror, bg=p["input_bg"], fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, font=small_font,
        )
        self.mirror_entry.pack(side="left", fill="x", expand=True, padx=(px(8), px(6)))
        # 输入框失焦或按回车时记住，免得改了没保存
        self.mirror_entry.bind("<FocusOut>", lambda _e: self._save_settings())
        self.mirror_entry.bind("<Return>", lambda _e: self._save_settings())
        self.clean_btn = make_button(mrow, "清理下载文件", self._clear_downloads, "secondary")
        self.clean_btn.pack(side="right")
        self.mirror_hint = muted(acc, "")
        self.mirror_hint.pack(fill="x", pady=(px(4), 0))
        self._refresh_mirror_hint()

        # ---------------------------------------------------------- 文本处理卡片
        txt = card("文本处理")
        trow = tk.Frame(txt, bg=CARD)
        trow.pack(fill="x")
        check(
            trow, "口述标点（说“逗号 / 句号 / 换行”自动转成标点）",
            self.punctuation, self._on_option_change,
        ).pack(side="left")
        self.dict_btn = make_button(
            trow, "替换词典", self._open_dict_editor, "secondary"
        )
        self.dict_btn.pack(side="right")
        self.dict_label = muted(trow, self._dict_summary())
        self.dict_label.pack(side="right", padx=(0, px(10)))

        trow2 = tk.Frame(txt, bg=CARD)
        trow2.pack(fill="x", pady=(px(7), 0))
        check(
            trow2, "自动标点（说完停一下，自动补「，/。/？/！」）",
            self.auto_punct, self._on_option_change,
        ).pack(side="left")
        muted(trow2, "停顿越长越可能断句；问句补问号、感叹补叹号").pack(
            side="right", padx=(px(10), 0)
        )

        # ---------------------------------------------------------- 权限提示
        # 记下横幅，供折叠设置时把设置区插回它前面（保持卡片始终在提示之上）
        self._banner = None
        if not is_elevated():
            banner = tk.Frame(self, bg=p["warn_bg"], highlightbackground=p["warn_border"], highlightthickness=1)
            banner.pack(fill="x", padx=PAD, pady=(0, px(6)))
            self._banner = banner
            tk.Label(
                banner,
                text=("当前是普通权限运行，无法把文字输入到「以管理员身份运行」的程序"
                      "（如一梦江湖等游戏）。"),
                bg=p["warn_bg"], fg=p["warn_fg"], anchor="w", justify="left",
                wraplength=px(620), font=small_font,
            ).pack(side="left", padx=(px(10), px(8)), pady=px(6))
            make_button(
                banner, "以管理员身份重启", self._restart_as_admin, "secondary"
            ).pack(side="right", padx=(0, px(10)), pady=px(4))

        # ---------------------------------------------------------- 底栏
        # 先占住底部：底栏若在会撑开的文本区之后 pack，窗口不够高时会被挤出可视区域
        footer = tk.Frame(self, bg=BG)
        footer.pack(side="bottom", fill="x", padx=PAD, pady=(px(4), px(8)))

        tk.Label(footer, text="音量", bg=BG, fg=MUTED, font=small_font).pack(side="left")
        self._level_h = px(8)
        self.level_canvas = tk.Canvas(
            footer, height=self._level_h, bg=p["level_bg"], highlightthickness=0,
            width=px(200), bd=0,
        )
        self.level_canvas.pack(side="left", padx=(px(7), 0))
        self.level_bar = self.level_canvas.create_rectangle(
            0, 0, 0, self._level_h, fill=p["ok"], width=0
        )

        tk.Label(
            footer, text="语音全程在本机识别，不上传", bg=BG, fg=FAINT, font=small_font
        ).pack(side="right")

        # ---------------------------------------------------------- 识别结果
        result = tk.Frame(self, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        result.pack(fill="both", expand=True, padx=PAD, pady=(0, px(8)))
        self.result_frame = result  # 折叠设置时用它做 pack(before=…) 的锚点

        rhead = tk.Frame(result, bg=CARD)
        rhead.pack(fill="x", padx=px(16), pady=(px(12), px(8)))
        tk.Label(rhead, text="识别结果", bg=CARD, fg=TEXT, font=bold_font).pack(side="left")
        self.count_label = tk.Label(rhead, text="0 字符", bg=CARD, fg=MUTED, font=small_font)
        self.count_label.pack(side="left", padx=(px(9), 0))
        # 文本操作按钮跟着结果走，放在结果卡片自己的标题栏里
        self.save_btn = make_button(rhead, "保存为 TXT", self._save, "ghost")
        self.save_btn.pack(side="right")
        self.clear_btn = make_button(rhead, "清空", self._clear, "ghost")
        self.clear_btn.pack(side="right", padx=(0, px(2)))
        self.copy_btn = make_button(rhead, "复制", self._copy, "ghost")
        self.copy_btn.pack(side="right", padx=(0, px(2)))

        tk.Frame(result, bg=BORDER, height=1).pack(fill="x")

        # 文本区和空状态提示叠在同一容器里：提示用 place 居中，出字后隐藏
        body = tk.Frame(result, bg=CARD)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(
            body, wrap="word", relief="flat", borderwidth=0, padx=px(16), pady=px(14),
            bg=CARD, fg=TEXT, insertbackground=TEXT,
            selectbackground=p["select_bg"], selectforeground=TEXT,
            font=("Microsoft YaHei UI", 14), spacing1=px(3), spacing3=px(5),
            height=10,  # 请求高度放小：文本区靠 expand 撑满，避免把底栏挤出去
        )
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(body, command=self.text.yview)
        scrollbar.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=scrollbar.set)

        self.text.tag_configure("final", foreground=TEXT)
        self.text.tag_configure("partial", foreground=p["partial"])
        # 声调拼音行：小一号、灰一点，和正文区分开，看着像给正文做的注音
        self.text.tag_configure(
            "pinyin", foreground=p["muted"], font=("Microsoft YaHei UI", 11),
            spacing1=px(5), spacing3=px(2),
        )

        # 空状态：真正的占位符，垂直居中在文本区上，比塞进文本框更像「还没开始」
        self.hint_box = tk.Frame(body, bg=CARD)
        tk.Label(
            self.hint_box, text=HINT_TITLE, bg=CARD, fg=p["hint_title"],
            font=("Microsoft YaHei UI", 13), justify="center",
        ).pack()
        # 提示里带上当前热键，换热键后 _refresh_hint 会更新它
        self.hint_line = tk.Label(
            self.hint_box, text=self._hint_text(), bg=CARD, fg=p["hint_sub"],
            font=("Microsoft YaHei UI", 11), justify="center", wraplength=px(620),
        )
        self.hint_line.pack(pady=(px(8), 0))

        self._render()
        self._apply_settings_visibility()

    # ---------------------------------------------------------------- 模型准备
    def _start_model_load(self) -> None:
        """在后台准备所有已勾选语言的模型。改动勾选会再调一次，旧的加载结果按代次作废。"""
        languages = self._enabled_languages()
        self._model_token += 1
        token = self._model_token
        self.model_loaded = False
        self._models = {}
        self._model_names = {}
        self.model_label.config(text="模型：准备中…")
        self._set_buttons(self.running, False)
        if len(languages) == 1:
            self._set_status(f"正在准备「{languages[0]}」识别模型…")
        else:
            self._set_status(f"正在准备 {len(languages)} 种语言的识别模型…")
        self._model_thread = threading.Thread(
            target=self._prepare_models, args=(languages, token), daemon=True
        )
        self._model_thread.start()

    def _prepare_models(self, languages: list[str], token: int) -> None:
        """逐个准备语言模型：某个语言失败只跳过它，其余照常可用。

        同一时刻只允许一个准备线程在跑（_model_lock）：快速连点语言勾选会同时拉起多个
        线程，它们会争抢同一个模型的 .part 下载文件，轻则白下重则踩坏断点。抢到锁后
        若发现自己的代次已经过期，就直接退出，把活让给最新的那次。
        """
        with self._model_lock:
            if token != self._model_token:
                log("语言勾选已变化，放弃这次过期的模型准备")
                return
            loaded: dict[str, tuple[str, object]] = {}
            failures: list[tuple[str, str]] = []
            for language in languages:
                if token != self._model_token:
                    log("语言勾选已变化，放弃这次过期的模型准备")
                    return
                try:
                    log(f"开始准备识别模型（语言：{language}）…")
                    if not model_ready(language):
                        acquire_model(
                            lambda done, total: self.msg.put(("progress", (done, total))),
                            lambda text: self.msg.put(("status", text)),
                            language,
                        )
                    model_dir = active_model_dir(language)
                    cached = self._model_cache.get(language)
                    if cached and cached[0] == model_dir.name:
                        # 这个语言用的还是同一个模型，直接复用，不再读盘
                        loaded[language] = cached
                        log(f"复用已加载的模型（{model_dir.name}）")
                        continue
                    self.msg.put(("status", f"正在加载「{language}」模型…"))
                    model_path = ascii_model_path(model_dir)
                    log(f"加载模型：{model_dir.name} → {model_path}")
                    model = vosk.Model(model_path)
                    log(f"模型加载完成（{model_dir.name}）")
                    loaded[language] = (model_dir.name, model)
                except Exception as exc:
                    log(f"「{language}」模型准备失败：{type(exc).__name__}: {exc}")
                    failures.append((language, f"{type(exc).__name__}: {exc}"))
            if token != self._model_token:
                log("语言勾选已变化，放弃这次过期的模型准备")
                return
            # 缓存收敛到这次真正用到的语言：被取消勾选的语言模型引用随之释放
            self._model_cache = dict(loaded)
            self.msg.put(("models_ready", (token, loaded, failures)))

    # ---------------------------------------------------------------- 消息轮询
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.msg.get_nowait()
                if kind == "level":
                    self._set_level(payload)
                elif kind == "partial":
                    text = self._decorate(payload)
                    if text != self.partial_text:
                        self.partial_text = text
                        self._render()
                elif kind == "final":
                    raw, pause, _lang = split_final(payload)
                    chunk = self._decorate(raw)
                    if self._first_final_pending:
                        # 本次录音的第一条结果：段内量到的停顿可能只是「开口前的延迟」，
                        # 按住说话模式下真正的停顿是「上次松手到这次开口」，取两者较大者
                        self._first_final_pending = False
                        pause = max(pause, self._session_gap)
                    self._last_final_at = time.monotonic()
                    if chunk:
                        self._session_had_text = True
                    # 这一句和上一句之间停顿够久的话，先给上一句补个标点再接下句
                    sep = self._auto_boundary(pause) if chunk else ""
                    if sep:
                        self.final_text += sep
                    self.final_text = smart_join(self.final_text, chunk)
                    self.partial_text = ""
                    self._render()
                    if self.global_input.get():
                        if sep:
                            self._type_out(sep)
                        self._type_out(chunk)
                elif kind == "status":
                    self._set_status(payload)
                elif kind == "progress":
                    done, total = payload
                    mb = done / 1048576
                    if total:
                        self.model_label.config(
                            text=f"模型：下载中 {done * 100.0 / total:.1f}%（{mb:.0f} MB）"
                        )
                    else:
                        self.model_label.config(text=f"模型：已下载 {mb:.1f} MB")
                elif kind == "models_ready":
                    token, loaded, failures = payload
                    if token != self._model_token:
                        continue  # 语言勾选已变，这次加载结果作废
                    if not loaded:
                        # 一种都没起来：没有可用的识别模型，报错并给出各语言的失败原因
                        self.model_loaded = False
                        self.model_label.config(text="模型：加载失败")
                        self._set_status("识别模型加载失败")
                        detail = "\n\n".join(
                            f"「{lang}」：{err}\n可手动下载 {lang_spec(lang)['small']}，"
                            f"解压后放进：\n{MODELS_DIR}"
                            for lang, err in failures
                        )
                        messagebox.showerror("模型准备失败", detail or "没有可用的识别模型")
                        self._refresh_accuracy_card()
                        continue
                    self._models = {lang: model for lang, (_n, model) in loaded.items()}
                    self._model_names = {lang: name for lang, (name, _m) in loaded.items()}
                    self.model_loaded = True
                    self.model_label.config(text=f"模型：已就绪 · {self._models_label()}")
                    self._set_status(
                        f"模型已就绪（{len(loaded)} 种语言），点击「开始识别」"
                    )
                    self._set_buttons(running=self.running, ready=True)
                    self._refresh_accuracy_card()
                    if failures:
                        # 部分语言没起来：提示一次，但已加载的语言仍可正常识别
                        detail = "\n".join(f"「{lang}」：{err}" for lang, err in failures)
                        log(f"部分语言模型不可用：{detail}")
                        messagebox.showwarning(
                            "部分语言模型不可用",
                            f"以下语言的模型没能准备好，识别时会跳过它们：\n\n{detail}",
                        )
                elif kind == "dl_progress":
                    self._dl_progress(*payload)
                elif kind == "dl_done":
                    self._downloading = False
                    log(f"{self._dl_language}高精度模型下载完成")
                    self._refresh_accuracy_card()
                    if not self.running:
                        # 下好了就重新加载所有已启用语言，大模型立即生效
                        self._set_status("高精度模型已就绪，正在自动切换…")
                        messagebox.showinfo(
                            "下载完成",
                            f"「{self._dl_language}」高精度模型已就绪，程序会自动切换过去。\n\n"
                            "识别更准，代价是启动慢一些、内存占用高一些。",
                        )
                        self._start_model_load()
                    else:
                        self._set_status(
                            f"「{self._dl_language}」高精度模型已下载完成，停止识别后重新开始即生效"
                        )
                        messagebox.showinfo(
                            "下载完成",
                            f"「{self._dl_language}」高精度模型已就绪。\n\n"
                            "停止识别后重新开始（或重启程序）即可使用它。",
                        )
                elif kind == "dl_error":
                    self._downloading = False
                    _set_btn_enabled(self.download_btn, True)
                    # 失败后别让标签停在「高精度模型：12.3%」那种进度文案上，看着像还在下
                    self._refresh_accuracy_card()
                    self.model_label.config(text=self._accuracy_text())
                    self._set_status(
                        "高精度模型下载失败，可以稍后再点一次（支持断点续传）"
                    )
                    messagebox.showerror("下载失败", payload)
                    # 失败后顺手把损坏残留清掉；剩下可续传的部分问一下要不要删
                    cleanup_download_junk()
                    left = pending_download_bytes()
                    if left and messagebox.askyesno(
                        "删除下载失败的临时文件？",
                        f"models 目录里还留着没下完的临时文件，共 {_fmt_size(left)}。\n\n"
                        "保留它们，下次点「下载」可以断点续传；\n"
                        "删除则释放磁盘空间，但下次要从头下载。\n\n现在删除吗？",
                    ):
                        count, freed = clear_all_download_files()
                        log(f"删除下载失败的文件：{count} 个，{_fmt_size(freed)}")
                        self._set_status(f"已删除下载临时文件，释放 {_fmt_size(freed)}")
                    self._refresh_mirror_hint()
                elif kind == "dl_cancel":
                    self._downloading = False
                    _set_btn_enabled(self.download_btn, True)
                    self._refresh_accuracy_card()
                    self.model_label.config(text=self._accuracy_text())
                    self._set_status(
                        "已取消下载，已下的部分保留着，下次点「下载」会接着下"
                    )
                    self._refresh_mirror_hint()
                elif kind == "ptt":
                    if payload == "down":
                        self._start(from_ptt=True)
                    else:
                        self._stop(from_ptt=True)
                elif kind == "mic_stall":
                    log("麦克风连续无数据，判定为异常，已停止识别")
                    self._stop()
                    self._set_status(
                        "麦克风没有音频数据，已停止（设备可能被拔出，或被其他程序独占）"
                    )
                elif kind == "engine_error":
                    log(f"识别引擎异常：{payload}")
                    self._stop()
                    self._set_status("识别引擎出错了，已停止（详情见「运行日志.txt」）")
                elif kind == "hotkey_ready":
                    log(f"{payload} 热键注册成功")
                    if payload == self.hotkey.get():
                        self.hotkey_label.config(
                            text=f"按住 {payload} 说话，松开结束", fg=self.palette["note"]
                        )
                elif kind == "hotkey_error":
                    log(f"{payload} 热键注册失败（可能被其他程序占用）")
                    if payload != self.hotkey.get():
                        continue  # 旧监听线程的迟到消息，忽略
                    if self._use_next_hotkey(payload):
                        continue
                    self.hotkey_label.config(
                        text="所有热键都被占用了", fg=self.palette["dot_err"]
                    )
                    self._set_status(
                        "热键全部注册失败：请关掉占用这些键的程序，再重启本程序"
                    )
        except queue.Empty:
            pass
        except Exception as exc:
            # 这里是整个界面的消息泵，任何一条消息处理出错都不能让它停摆：
            # 一旦 after() 没排上，进度、状态、热键、识别结果就全部再也不刷新了
            # （曾表现为「下载失败后按钮卡在正在取消，再也点不动」）。
            log(f"处理界面消息时出错（已跳过这条，界面继续运行）：{type(exc).__name__}: {exc}")
        finally:
            try:
                self.after(60, self._poll)
            except Exception:
                pass  # 窗口已经销毁，不必再排下一次

    # ---------------------------------------------------------------- 文本渲染
    def _render(self) -> None:
        # 用户往上翻着看/复制旧内容时，别被每次 partial 刷新硬拽回底部；
        # 只有原本就停在底部（或还没有内容）时才自动滚到底
        try:
            at_bottom = self.text.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        if self.final_text:
            self.text.insert("end", self.final_text, "final")
        if self.partial_text:
            self.text.insert("end", self.partial_text, "partial")
        # 声调标注：在结果下方补一行带声调的拼音，方便核对同音字（不影响复制/保存/注入）
        pinyin = self._pinyin_line()
        if pinyin:
            self.text.insert("end", "\n" + pinyin, "pinyin")
        if not self.final_text and not self.partial_text:
            self.hint_box.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.hint_box.place_forget()
        if at_bottom:
            self.text.see("end")
        self.text.config(state="disabled")
        self.count_label.config(text=f"{len(self.final_text)} 字符")
        self._set_buttons(self.running, self.model_loaded)

    def _pinyin_line(self) -> str:
        """带声调的拼音行；按「文本 + 声调开关是否生效」缓存，避免每个 partial 都重算整段拼音。"""
        key = (self.final_text, self._tone_on())
        cached_key, cached_value = self._pinyin_cache
        if cached_key == key:
            return cached_value
        value = tone_pinyin(key[0]) if key[1] else ""
        self._pinyin_cache = (key, value)
        return value

    def _hint_text(self) -> str:
        """空状态里的提示文案，带上当前热键，换热键后跟着变。"""
        return (
            f"点「开始识别」或按住 {self.hotkey.get()} 说话（F5 也能开始/停止）；"
            "识别全程在本机完成，断网也能用"
        )

    def _refresh_hint(self) -> None:
        label = getattr(self, "hint_line", None)
        if label is not None and label.winfo_exists():
            label.config(text=self._hint_text())

    def _tone_on(self) -> bool:
        """声调标注是否生效：开关打开、装了 pypinyin，且勾选的语言里有中文。"""
        return bool(self.tone.get() and HAS_PYPINYIN and self._has_tone_language())

    def _set_level(self, rms: float) -> None:
        width = self.level_canvas.winfo_width()
        if width <= 1:  # 窗口尚未完成布局时 winfo_width() 会返回 1
            width = self.level_canvas.winfo_reqwidth()
        width = max(1, width)
        if rms <= 1:
            frac = 0.0
        else:
            # 对数刻度：环境底噪(rms≈10~50)占一小段，说话(rms≈300~3000)占中高段
            frac = min(1.0, max(0.0, (math.log10(rms) - 1.0) / 3.0))
        self.level_canvas.coords(self.level_bar, 0, 0, int(width * frac), self._level_h)
        self.level_canvas.itemconfig(
            self.level_bar, fill=self.palette["over"] if frac > 0.97 else self.palette["ok"]
        )

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)
        # 状态指示灯：绿=就绪/进行中正常，黄=忙碌/等待，红=出错/需要注意
        p = self.palette
        color = p["dot_ok"]
        if any(word in text for word in (
            "失败", "错误", "异常", "没有", "无法", "占用", "没能", "太短", "出错", "取消",
        )):
            color = p["dot_err"]
        elif any(word in text for word in (
            "正在", "下载", "聆听", "准备", "注册", "稍候", "等待", "…", "请",
        )):
            color = p["dot_warn"]
        try:
            self.status_dot.itemconfig(self._dot_id, fill=color)
        except Exception:
            pass

    def _set_buttons(self, running: bool, ready: bool) -> None:
        has_mic = bool(self._devices)
        _set_btn_enabled(self.start_btn, bool(ready and not running and has_mic))
        _set_btn_enabled(self.stop_btn, running)
        self.device_combo.config(state="disabled" if running else "readonly")
        _set_btn_enabled(self.refresh_btn, not running)
        has_text = bool(self.final_text)
        _set_btn_enabled(self.copy_btn, has_text)
        _set_btn_enabled(self.save_btn, has_text)
        _set_btn_enabled(self.clear_btn, has_text)

    # ---------------------------------------------------------------- 设备
    def _refresh_devices(self) -> None:
        self._devices = []
        self._device_names = []
        labels: list[str] = []
        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = None

        for index, info in enumerate(sd.query_devices()):
            if info["max_input_channels"] > 0:
                name = str(info["name"])
                self._devices.append((index, f"[{index}] {name}"))
                self._device_names.append(name)
                labels.append(f"[{index}] {name}")

        self.device_combo["values"] = labels
        if labels:
            # 优先还原上次选用的麦克风（按名字匹配，序号会变）；它不在了才退回系统默认设备
            selected = None
            if self._mic_pref:
                for position, name in enumerate(self._device_names):
                    if name == self._mic_pref:
                        selected = position
                        break
            if selected is None:
                for position, (index, _label) in enumerate(self._devices):
                    if index == default_in:
                        selected = position
                        break
            self.device_combo.current(selected or 0)
        else:
            log("没有检测到任何麦克风输入设备")
            self._set_status(
                "没有检测到麦克风：请插好麦克风后点「刷新」，"
                "并确认「Windows 设置 → 隐私和安全性 → 麦克风」已允许桌面应用访问"
            )
        if not self.running:
            self._set_buttons(self.running, self.model_loaded)

    def _on_device_change(self, _event=None) -> None:
        """记住用户选的麦克风，下次启动自动还原到它。"""
        position = self.device_combo.current()
        if 0 <= position < len(self._device_names):
            self._mic_pref = self._device_names[position]
            log(f"切换麦克风：{self._mic_pref}")
            self._set_status(f"已选择麦克风：{self._mic_pref}")
            self._save_settings()

    def _selected_device(self):
        position = self.device_combo.current()
        if position < 0 or position >= len(self._devices):
            return None
        return self._devices[position][0]

    # ---------------------------------------------------------------- 操作
    def _on_global_toggle(self) -> None:
        if self.global_input.get():
            self._set_status("已开启：识别结果会直接输入到当前光标处")
        else:
            self._set_status("已关闭：识别结果只显示在本窗口")
        self._save_settings()

    def _on_inject_change(self, _event=None) -> None:
        mode = self.inject_mode.get()
        self.inject_hint.config(text=INJECT_HINTS.get(mode, ""))
        self._set_status(f"输入方式已切换为「{mode}」")
        self._save_settings()

    def _on_hotkey_change(self, _event=None) -> None:
        name = self.hotkey.get()
        log(f"切换热键：{name}")
        self._fallback_tried.clear()  # 手动选了就重新开始试，别受之前自动回退影响
        self.hotkey_label.config(text=f"正在注册热键 {name}…", fg=self.palette["note"])
        self._refresh_hint()
        self._save_settings()
        self._start_listener()
        self._set_status(f"热键已切换为 {name}")

    def _on_language_change(self, lang: str | None = None, _event=None) -> None:
        """切换可识别语言：至少保留一种；停掉识别、记住选择，再后台准备模型。"""
        if (
            lang is not None
            and not self.lang_vars[lang].get()
            and not self._selected_languages()
        ):
            # 一种都不剩了：把刚取消的那种恢复回来，保证至少有一种可识别语言
            self.lang_vars[lang].set(True)
            self._set_status("至少要保留一种可识别的语言")
            return
        languages = self._enabled_languages()
        log(f"可识别语言：{'、'.join(languages)}")
        if self.running:
            self._stop()
        self._save_settings()
        self._update_tone_state()
        hint = getattr(self, "language_hint", None)
        if hint is not None and hint.winfo_exists():
            hint.config(text=self._language_hint())
        self._refresh_accuracy_card()
        self._start_model_load()

    def _on_tone_change(self) -> None:
        # 声调行是叠在结果区里的，改开关后要重画一次才会立刻显示/隐藏
        self._render()
        self._on_option_change()

    def _on_option_change(self) -> None:
        self.attributes("-topmost", bool(self.topmost.get()))
        self._save_settings()
        self._set_status("设置已保存")

    def _on_autostart_change(self) -> None:
        want = bool(self.autostart.get())
        if set_autostart(want) and autostart_enabled() == want:
            log(f"开机自启动：{'开启' if want else '关闭'}")
            self._set_status("已设为开机自启动（下次登录自动打开）" if want else "已取消开机自启动")
        else:
            self.autostart.set(autostart_enabled())  # 失败就回到真实状态，避免界面骗人
            self._set_status("开机自启动设置失败（可能被安全软件拦截）")
        self._save_settings()

    def _open_log(self) -> None:
        """打开运行日志，出问题时方便直接查看或发给别人。"""
        import os

        try:
            if LOG_PATH.exists():
                os.startfile(str(LOG_PATH))
            else:
                self._set_status("还没有日志文件")
        except Exception as exc:
            log(f"打开日志失败：{type(exc).__name__}: {exc}")
            self._set_status("打开日志失败，可手动打开程序目录里的「运行日志.txt」")

    def _selected_languages(self) -> list[str]:
        """当前勾选的语言（可能为空，按界面顺序）。"""
        return [lang for lang in LANGUAGE_ORDER if self.lang_vars[lang].get()]

    def _enabled_languages(self) -> list[str]:
        """可识别语言：勾选的集合；一种都没勾时回落到默认语言，保证至少一种。"""
        return self._selected_languages() or [DEFAULT_LANGUAGE]

    def _has_tone_language(self) -> bool:
        """勾选的语言里有没有带声调的（中文），决定声调标注能不能用。"""
        return any(lang_spec(lang).get("tone") for lang in self._enabled_languages())

    def _models_label(self) -> str:
        """已加载模型的名字串，用于状态栏展示（如「vosk-model-small-cn-0.22、…」）。"""
        return "、".join(
            self._model_names.get(lang, lang) for lang in self._models
        ) or "已就绪"

    def _next_large_language(self) -> str | None:
        """下一个「有高精度模型、但还没下载」的已启用语言（按界面顺序），没有则 None。"""
        for lang in self._enabled_languages():
            spec = lang_spec(lang)
            large = large_model_dir(lang)
            if spec.get("large") and large and not _is_model_dir(large):
                return lang
        return None

    def _accuracy_text(self) -> str:
        parts = []
        for lang in self._enabled_languages():
            spec = lang_spec(lang)
            large = large_model_dir(lang)
            if large and _is_model_dir(large):
                parts.append(f"{lang}：高精度（{spec.get('large_size', '')}）")
            else:
                parts.append(f"{lang}：轻量（{spec.get('small_size', '')}）")
        return "当前：" + "｜".join(parts)

    def _language_hint(self) -> str:
        missing = [lang for lang in self._enabled_languages() if not model_ready(lang)]
        if not missing:
            return "模型已就绪"
        return f"首次使用需下载 {len(missing)} 个模型"

    def _large_download_visible(self) -> bool:
        """有已启用语言可下高精度模型（且还没下）时，才显示「下载高精度模型」按钮。"""
        return self._downloading or self._next_large_language() is not None

    def _download_btn_text(self) -> str:
        if self._downloading:
            return "取消下载"
        lang = self._next_large_language()
        if lang:
            return f"下载高精度模型（{lang} {lang_spec(lang).get('large_size', '')}）"
        return "下载高精度模型"

    def _refresh_accuracy_card(self) -> None:
        """语言切换/下载结束后刷新精度卡片：文案、下载按钮的显隐与标题。"""
        label = getattr(self, "accuracy_label", None)
        btn = getattr(self, "download_btn", None)
        if label is None or not label.winfo_exists() or btn is None or not btn.winfo_exists():
            return
        label.config(text=self._accuracy_text())
        btn.config(text=self._download_btn_text())
        if self._large_download_visible():
            if not btn.winfo_manager():
                btn.pack(side="right")
        elif btn.winfo_manager():
            btn.pack_forget()
        self._refresh_mirror_hint()

    def _update_tone_state(self) -> None:
        """声调标注只在勾选了中文时可用：换语言后把开关置灰并说明原因。"""
        chk = getattr(self, "tone_check", None)
        hint = getattr(self, "tone_hint", None)
        if chk is None or not chk.winfo_exists():
            return
        has_tone = self._has_tone_language()
        available = has_tone and HAS_PYPINYIN
        chk.config(state="normal" if available else "disabled")
        if hint is None or not hint.winfo_exists():
            return
        if not has_tone:
            hint.config(text="勾选的语言里没有中文，无法标注")
        elif not HAS_PYPINYIN:
            hint.config(text="缺少 pypinyin 库，无法标注")
        else:
            hint.config(text="只补注音，不改动识别结果本身")

    def _refresh_mirror_hint(self) -> None:
        """在输入框下方说明当前镜像设置和待续传的临时文件大小。"""
        widget = getattr(self, "mirror_hint", None)
        # 切主题会整体重建界面，旧控件可能已被销毁
        if widget is None or not widget.winfo_exists():
            return
        mirror = self.model_mirror.get().strip()
        if mirror:
            text = "已填自定义镜像，下载时会优先尝试它；内置镜像不可用时很有用。"
        else:
            text = ("留空则用内置源（镜像源 → 官方站）。内置源都下不动时，"
                    "可在此填入自己的加速地址（需指向该语言的模型压缩包）。")
        pending = pending_download_bytes()
        if pending:
            text += f"　待续传临时文件：{_fmt_size(pending)}。"
        self.mirror_hint.config(text=text)

    def _clear_downloads(self) -> None:
        """删除下载失败/未完成的临时文件，把 models 目录清干净。"""
        if self._downloading:
            self._set_status("正在下载，先取消下载再清理")
            return
        paths = _download_temp_files()
        if not paths:
            self._set_status("没有需要清理的下载文件")
            self._refresh_mirror_hint()
            return
        total = 0
        for path in paths:
            try:
                total += path.stat().st_size
            except OSError:
                pass
        if not messagebox.askyesno(
            "清理下载文件",
            f"将删除 models 目录下 {len(paths)} 个下载临时文件，"
            f"共 {_fmt_size(total)}。\n\n"
            "其中可能包含可续传的断点文件 —— 删掉后下次要从头下载。\n\n确定删除吗？",
        ):
            return
        count, freed = clear_all_download_files()
        log(f"清理下载文件：删除 {count} 个，释放 {_fmt_size(freed)}")
        self._set_status(f"已清理 {count} 个下载文件，释放 {_fmt_size(freed)}")
        self._refresh_mirror_hint()

    def _decorate(self, raw: str) -> str:
        """识别结果后处理：去空格 → 口述标点 → 用户替换词典。"""
        text = tidy(raw)
        if self.punctuation.get():
            text = apply_mapping(text, PUNCTUATION_MAP)
        if self.replacements:
            text = apply_mapping(text, self.replacements)
        return text

    def _auto_boundary(self, pause: float) -> str:
        """这一句之前停顿了多久 → 上一句末尾该补什么标点（没有就返回空串）。

        先看上一句是不是已经带标点了（用户可能自己口述了「句号」），带了就不再补；
        再按停顿长短判断是「一句话说完」还是「句中换气」，前者按句意挑 。/？/！，后者补逗号。
        """
        if not self.auto_punct.get():
            return ""
        prev = self.final_text.rstrip()
        if not prev or prev[-1] in END_PUNCT:
            return ""
        if pause >= AUTO_PAUSE_SENTENCE:
            return auto_end_punct(prev)
        if pause >= AUTO_PAUSE_CLAUSE:
            return "，"
        return ""

    def _dict_summary(self) -> str:
        count = len(self.replacements)
        if not count:
            return "词典：未设置"
        sample = "、".join(list(self.replacements)[:2])
        return f"词典：{count} 条（{sample}…）" if count > 2 else f"词典：{count} 条（{sample}）"

    def _open_dict_editor(self) -> None:
        """打开替换词典编辑窗口：每行「识别到的词=想要的词」，保存后立即生效。"""
        win = self._dict_window
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return

        win = tk.Toplevel(self)
        self._dict_window = win
        px = self._px
        p = self.palette
        win.title("替换词典")
        win.configure(bg=p["bg"])
        win.geometry(f"{px(620)}x{px(500)}")
        win.transient(self)

        tk.Label(
            win, text="替换词典", bg=p["bg"], fg=p["text"],
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor="w", padx=px(18), pady=(px(16), px(3)))
        tk.Label(
            win,
            text="每行一条，格式：识别到的词=想要的词（等号两边都支持空格）。\n"
                 "例如：一梦江户=一梦江湖　　zhongguo=中国\n"
                 "改完点「保存」立即生效，不需要重启；以 # 开头的行会被忽略。",
            bg=p["bg"], fg=p["muted"], justify="left",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", padx=px(18), pady=(0, px(10)))

        box = tk.Frame(win, bg=p["card"], highlightbackground=p["border"], highlightthickness=1)
        box.pack(fill="both", expand=True, padx=px(18))
        editor = tk.Text(
            box, wrap="none", relief="flat", borderwidth=0,
            padx=px(12), pady=px(10), bg=p["card"], fg=p["text"],
            insertbackground=p["text"], font=("Consolas", 11), undo=True,
            selectbackground=p["accent"], selectforeground=p["accent_fg"],
        )
        editor.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(box, command=editor.yview)
        bar.pack(side="right", fill="y")
        editor.configure(yscrollcommand=bar.set)
        editor.insert("1.0", "\n".join(f"{k}={v}" for k, v in self.replacements.items()))

        def save() -> None:
            mapping: dict = {}
            for line in editor.get("1.0", "end").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                left, right = line.split("=", 1)
                left, right = left.strip(), right.strip()
                if left:
                    mapping[left] = right
            self.replacements = mapping
            if save_dict_file(mapping):
                self._set_status(f"替换词典已更新（{len(mapping)} 条），立即生效")
                log(f"替换词典已更新：{len(mapping)} 条")
            else:
                self._set_status("替换词典保存失败，请检查文件是否被占用")
            self.dict_label.config(text=self._dict_summary())
            win.destroy()

        btns = tk.Frame(win, bg=p["bg"])
        btns.pack(fill="x", padx=px(18), pady=px(14))
        self._make_button(btns, "保存", save, "primary").pack(side="right")
        self._make_button(btns, "取消", win.destroy, "secondary").pack(
            side="right", padx=(0, px(8))
        )
        self._make_button(
            btns, "打开词典文件", lambda: self._open_path(DICT_PATH), "ghost",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left")

    def _open_path(self, path: Path) -> None:
        import os

        try:
            if path.exists():
                os.startfile(str(path))
            else:
                self._set_status(f"文件还不存在：{path.name}")
        except Exception as exc:
            log(f"打开文件失败：{type(exc).__name__}: {exc}")
            self._set_status(f"打开失败，可手动打开：{path}")

    def _download_large_model(self) -> None:
        """下载已启用语言里下一个「有高精度模型但还没下」的模型。体积大，先问一声再下；下载中再点一次就是取消。"""
        if self._downloading:
            worker = self._dl_thread
            if worker is not None and worker.is_alive():
                # 同一个按钮，下载中变成「取消下载」：置位事件，下载线程下一块数据就会退出
                self._dl_cancel.set()
                self.download_btn.config(text="正在取消…")
                _set_btn_enabled(self.download_btn, False)
                self._set_status("正在取消下载…")
                return
            # 线程早就结束了、界面却还停在「下载中」：这是状态残留（结束消息丢失或处理出错）。
            # 不复位的话按钮会一直卡在「正在取消…」并且是禁用状态，用户再也点不动 —— 当残留清掉重来。
            log("下载线程已结束但界面仍停在下载中，自动复位下载状态后重新开始")
            self._downloading = False
            self._dl_cancel.clear()
            _set_btn_enabled(self.download_btn, True)
            self._refresh_accuracy_card()
            self.model_label.config(text=self._accuracy_text())
        language = self._next_large_language()
        if language is None:
            messagebox.showinfo(
                "暂时没有可下载的高精度模型",
                "已勾选的语言目前都没有可用的高精度模型（或都已下载完成），"
                "轻量模型日常使用已经够用。",
            )
            return
        spec = lang_spec(language)
        large_name = spec.get("large")
        if not large_name:
            messagebox.showinfo(
                "暂时没有高精度模型",
                f"「{language}」目前只有轻量模型，日常使用已经够用，无需额外下载。",
            )
            return
        mirror = self.model_mirror.get().strip()
        if mirror and not mirror.lower().startswith(("http://", "https://")):
            messagebox.showwarning(
                "下载镜像地址不对",
                "自定义镜像要填完整的 http/https 地址，例如：\n"
                f"{_official_url(large_name)}\n\n"
                "留空则使用内置下载源。",
            )
            return
        pending = pending_download_bytes()
        resume_hint = (
            f"检测到上次没下完的临时文件（{_fmt_size(pending)}），会从断点接着下。\n\n"
            if pending else ""
        )
        if not messagebox.askyesno(
            "下载高精度模型",
            f"「{language}」高精度模型约 {spec.get('large_size', '')}，"
            "下载和解压都要花些时间。\n"
            f"{resume_hint}"
            "支持断点续传：中途失败、取消或关掉程序，下次点这个按钮会接着下。\n\n"
            "下载期间程序照常可用，下载完会自动切换到它。\n\n现在开始下载吗？",
        ):
            return
        self._downloading = True
        self._dl_cancel.clear()
        self._dl_at = 0.0
        self._dl_done = 0
        self._dl_speed = 0.0
        self._dl_ui_at = 0.0
        self._dl_mirror = mirror
        self._dl_language = language
        self.download_btn.config(text="取消下载")
        _set_btn_enabled(self.download_btn, True)
        log(f"开始下载「{language}」高精度模型（镜像={mirror or '内置'}）")
        self._dl_thread = threading.Thread(
            target=self._download_worker, args=(language,), daemon=True
        )
        self._dl_thread.start()

    def _dl_progress_cb(self, done: int, total: int) -> None:
        """下载线程里每下完一块就回调一次；用户点了取消就抛异常中断下载。"""
        if self._dl_cancel.is_set():
            raise DownloadCancelled()
        self.msg.put(("dl_progress", (done, total)))

    def _dl_progress(self, done: int, total: int) -> None:
        """刷新下载进度：显示已下 MB、实时速度和一位小数的百分比。

        百分比取整时，1.3 GB 的模型要下十几 MB 才跳到 1%，看着像「卡在 0%」；
        改成 MB + 速度后，一眼就能看出在动。
        """
        now = time.time()
        if not self._dl_at:
            self._dl_at, self._dl_done = now, done
        elif now - self._dl_at >= 0.5:
            self._dl_speed = max(0.0, (done - self._dl_done) / (now - self._dl_at))
            self._dl_at, self._dl_done = now, done
        if now - self._dl_ui_at < 0.2:  # 事件很密，限流一下免得白刷
            return
        self._dl_ui_at = now

        mb = done / 1048576
        # 速度没采样出来（刚开始那 0.5 秒）时别显示 0 KB/s，免得看着像卡住
        if self._dl_speed >= 1048576:
            speed_text = f"约 {self._dl_speed / 1048576:.1f} MB/s"
        elif self._dl_speed > 0:
            speed_text = f"约 {self._dl_speed / 1024:.0f} KB/s"
        else:
            speed_text = "测速中…"
        if total:
            percent = done * 100.0 / total
            eta = ""
            if self._dl_speed > 0:
                eta = f"，{_fmt_eta(max(0.0, (total - done) / self._dl_speed))}后完成"
            self.model_label.config(
                text=f"高精度模型：{percent:.1f}%（{mb:.0f} MB）"
            )
            self._set_status(
                f"正在下载高精度模型… {percent:.1f}%"
                f"（{mb:.1f}/{total / 1048576:.0f} MB，{speed_text}{eta}）"
            )
        else:
            self.model_label.config(text=f"高精度模型：已下载 {mb:.1f} MB")
            self._set_status(
                f"正在下载高精度模型… 已下 {mb:.1f} MB，{speed_text}"
            )

    def _download_worker(self, language: str) -> None:
        try:
            download_large_model(
                self._dl_progress_cb,
                lambda text: self.msg.put(("status", text)),
                language,
                self._dl_mirror,
            )
            self.msg.put(("dl_done", None))
        except DownloadCancelled:
            log(f"用户取消下载「{language}」高精度模型（已下部分已保留）")
            self.msg.put(("dl_cancel", None))
        except Exception as exc:
            log(f"下载「{language}」高精度模型失败：{type(exc).__name__}: {exc}")
            self.msg.put(("dl_error", f"{type(exc).__name__}: {exc}"))

    def _use_next_hotkey(self, failed: str) -> bool:
        """当前热键被别的程序占用时，自动换一个能用的，免得用户干瞪眼。"""
        self._fallback_tried.add(failed)
        for name in HOTKEYS:
            if name in self._fallback_tried:
                continue
            log(f"{failed} 被占用，自动改用 {name}")
            self.hotkey.set(name)
            self.hotkey_combo.current(list(HOTKEYS).index(name))
            self.hotkey_label.config(text=f"正在注册热键 {name}…", fg=self.palette["note"])
            self._refresh_hint()
            self._set_status(f"{failed} 被别的程序占用，已自动改用 {name}")
            self._save_settings()
            self._start_listener()
            return True
        return False

    def _start_listener(self) -> None:
        """(重新)启动热键监听线程；切换热键时先停掉旧的，确保旧热键被注销。"""
        old = getattr(self, "ptt", None)
        if old is not None:
            old.stop()
            old.join(timeout=2.0)  # 等旧线程注销热键，否则新线程注册不上
        modifiers, vk = HOTKEYS[self.hotkey.get()]
        self.ptt = PushToTalkListener(self.hotkey.get(), modifiers, vk, self.msg)
        self.ptt.start()

    def _beep(self, freq: int) -> None:
        """按住/松开的提示音，让你不看屏幕也知道有没有按到。"""
        if not (HAS_WINSOUND and self.sound_on.get()):
            return
        try:
            winsound.Beep(freq, SOUND_MS)
        except Exception:
            pass

    def _send_enter(self) -> None:
        """模拟按一下回车，聊天框/游戏里用来把刚说的话发出去。"""
        self._enter_job = None
        events = [_key_event(VK_RETURN), _key_event(VK_RETURN, True)]
        array = (_INPUT * len(events))(*events)
        if _user32.SendInput(len(events), array, ctypes.sizeof(_INPUT)) != len(events):
            log("自动回车失败（目标程序权限可能比本程序高）")
            self._set_status("文字已输入，但自动回车失败（权限不足）")
        else:
            log("已自动回车发送")
            self._set_status("已输入并自动回车发送")

    def _save_settings(self) -> None:
        # 读进来再合并：窗口位置等其它键不能被这里覆盖掉
        data = load_settings()
        data.update({
            "global_input": bool(self.global_input.get()),
            "inject_mode": self.inject_mode.get(),
            "hotkey": self.hotkey.get(),
            "auto_enter": bool(self.auto_enter.get()),
            "sound_on": bool(self.sound_on.get()),
            "topmost": bool(self.topmost.get()),
            "autostart": bool(self.autostart.get()),
            "enhance": bool(self.enhance.get()),
            "punctuation": bool(self.punctuation.get()),
            "auto_punct": bool(self.auto_punct.get()),
            "languages": self._enabled_languages(),
            "language": self._enabled_languages()[0],  # 兼容旧版本的单选键
            "tone": bool(self.tone.get()),
            "theme": self.theme.get(),
            "模型镜像": self.model_mirror.get().strip(),
            "mic": self._mic_pref,
            "compact": bool(self._settings_collapsed),
        })
        save_settings(data)
        self._refresh_mirror_hint()

    def _test_inject(self) -> None:
        """不录音，直接用当前输入方式发一段测试文字，验证目标程序吃不吃这一种。"""
        if self._countdown_job is not None:
            self.after_cancel(self._countdown_job)
            self._countdown_job = None
        log(f"测试输入｜方式={self.inject_mode.get()}")
        self._countdown(3, "语音输入测试ABC123")

    def _countdown(self, seconds: int, sample: str) -> None:
        if seconds > 0:
            self._set_status(
                f"{seconds} 秒后用「{self.inject_mode.get()}」发送测试文字，请切到目标窗口…"
            )
            self._countdown_job = self.after(1000, lambda: self._countdown(seconds - 1, sample))
            return
        self._countdown_job = None
        if foreground_is_self():
            # 焦点还停在本程序上，打出去的字只会被自己吞掉，测了也是白测
            log("测试输入｜焦点仍在本程序，已跳过")
            self._set_status("焦点还在本程序窗口上，请先点到目标程序里再试一次")
            return
        self._last_typed = ""
        self._type_out(sample)
        self._set_status(f"已用「{self.inject_mode.get()}」发送测试文字，看看有没有出现")

    def _restart_as_admin(self) -> None:
        """关掉自己并以管理员身份重新启动，用于向游戏等提权程序输入文字。"""
        if getattr(sys, "frozen", False):
            exe, args = sys.executable, ""  # 打包后 exe 本身就是入口，不用再带脚本路径
        else:
            exe, args = sys.executable, f'"{Path(__file__).resolve()}"'
        log("用户点击「以管理员身份重启」")
        self.on_close()
        release_single_instance()  # 让即将启动的新实例能拿到单实例锁
        time.sleep(0.5)  # 等热键释放，避免新实例注册热键失败
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, args, str(APP_DIR), 1
        )
        if result <= 32:
            log(f"提权启动失败（ShellExecuteW 返回 {result}）")
            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning(
                "没能以管理员身份启动",
                "管理员权限请求被取消或失败了，程序已退出。\n\n"
                "需要给游戏输入文字时，请右键程序图标 →「以管理员身份运行」。",
            )
            root.destroy()
        else:
            log("已以管理员身份重新启动")

    def _type_out(self, chunk: str) -> None:
        """把识别结果打进当前拥有焦点的窗口。"""
        if not chunk:
            return
        text = chunk
        # 相邻两段若都是英文/数字，补一个空格，避免单词粘连
        if (
            self._last_typed
            and self._last_typed[-1].isascii()
            and self._last_typed[-1].isalnum()
            and text[0].isascii()
            and text[0].isalnum()
        ):
            text = " " + text

        mode = self.inject_mode.get()
        if mode == INJECT_MSG:
            ok = post_message_text(text)
        elif mode == INJECT_IME:
            ok = post_message_text(text, WM_IME_CHAR)
        elif mode == INJECT_CLIP:
            ok = paste_text(text)
        else:
            ok = send_unicode_text(text)

        if ok:
            self._last_typed = text
            self._session_typed = True
        else:
            log(f"注入失败｜方式={mode}｜文字={text!r}")

    def _start(self, from_ptt: bool = False) -> None:
        if self.running:
            return
        if not self._devices:
            self._set_status(
                "没有检测到麦克风设备：请插好麦克风后点「刷新」，"
                "并确认「Windows 设置 → 隐私和安全性 → 麦克风」已允许桌面应用访问"
            )
            if not from_ptt:  # 热键触发时不弹窗，免得抢走目标程序的焦点
                messagebox.showwarning(
                    "没有麦克风",
                    "没有检测到可用的麦克风输入设备。\n\n"
                    "1. 确认麦克风已插好、系统能识别到\n"
                    "2. 点界面的「刷新」重新扫描设备\n"
                    "3. Windows 设置 → 隐私和安全性 → 麦克风，允许桌面应用访问",
                )
            return
        if not self.model_loaded:
            self._set_status("模型还没准备好，请稍候再试…")
            return
        if self._enter_job is not None:
            self.after_cancel(self._enter_job)  # 上一次的自动回车还没发就又被按住了
            self._enter_job = None

        device = self._selected_device()
        self._beep(SOUND_START_HZ)  # 先响再开麦克风，免得提示音被录进去
        engine = AudioEngine(self._models, self.msg, enhance=bool(self.enhance.get()))
        try:
            engine.start(device)
        except Exception as exc:
            log(f"打开麦克风失败：{type(exc).__name__}: {exc}")
            self._set_status("打开麦克风失败：设备可能被占用，请检查麦克风设置")
            if not from_ptt:
                # 热键触发时弹窗会把游戏的焦点抢走，只在点按钮时才弹
                messagebox.showerror(
                    "无法开始录音",
                    f"打开麦克风失败：\n{exc}\n\n请检查：\n"
                    f"1. 是否选对了麦克风设备\n"
                    f"2. Windows 设置 → 隐私和安全性 → 麦克风，允许桌面应用访问",
                )
            return

        self.engine = engine
        self.running = True
        self._last_typed = ""
        self._session_typed = False
        self._session_had_text = False
        now = time.monotonic()
        self._rec_started_at = now
        # 距上一条识别结果过了多久：按住说话时，这就是两句之间的停顿
        self._session_gap = (now - self._last_final_at) if self._last_final_at else 0.0
        self._first_final_pending = True
        self._set_buttons(running=True, ready=True)
        self._set_status(f"正在聆听…（采样率 {engine.rate} Hz，全部本地处理）")
        log(f"开始识别｜设备={device}｜采样率={engine.rate}")

    def _stop(self, from_ptt: bool = False) -> None:
        if not self.running:
            return
        self.running = False
        elapsed = time.monotonic() - self._rec_started_at if self._rec_started_at else 0.0
        if self.engine is not None:
            self.engine.stop()
            self.engine = None
        self._set_level(0.0)
        self.partial_text = ""
        self._render()
        self._set_buttons(running=False, ready=self.model_loaded)
        self._beep(SOUND_STOP_HZ)
        log(f"停止识别｜本次时长={elapsed:.2f}s")

        if from_ptt and elapsed < MIN_RECORD_SECONDS:
            self._set_status(
                f"按住时间太短（{elapsed:.1f} 秒）——要一直按住热键说话，说完再松开"
            )
            return

        # 识别出了字、却一个字都没送进目标程序 —— 多半是权限或输入方式不对
        if self.global_input.get() and self._session_had_text and not self._session_typed:
            log("识别有结果但注入失败，提示用户换输入方式或提权")
            self._set_status(
                "文字没能送进目标程序：换一个「输入方式」再试，或用「测试输入」验证；"
                "游戏类程序需以管理员身份运行本程序"
            )
            return

        if self.global_input.get() and self.auto_enter.get() and self._session_typed:
            self._session_typed = False
            # 最后一段识别结果是刚定稿的，等它先打进去再回车
            self._enter_job = self.after(350, self._send_enter)
            self._set_status("已停止，稍后自动回车发送…")
            return

        self._set_status("已停止")

    def _clear(self) -> None:
        self.final_text = ""
        self.partial_text = ""
        self._last_typed = ""
        self._render()
        self._set_status("已清空")

    def _copy(self, use_selection: bool = False) -> None:
        """复制识别结果。

        走系统剪贴板（_set_clipboard_text）而不是 Tk 的 clipboard_append：Tk 的剪贴板
        内容归本进程所有，程序一关就没了，用户「复制完顺手关掉程序」再粘贴会扑空。
        """
        text = self.final_text
        if use_selection:
            try:
                if self.text.tag_ranges("sel"):
                    selected = self.text.get("sel.first", "sel.last")
                    if selected.strip():
                        text = selected
            except Exception:
                pass
        if not text:
            return
        if not _set_clipboard_text(text):
            # 系统剪贴板被别的程序占着时，退回 Tk 自带的方式，至少有内容
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()
        self._set_status("已复制到剪贴板")

    def _bind_shortcuts(self) -> None:
        """窗口聚焦时的快捷键，减少来回点按钮。

        F5 开始/停止、Esc 停止、Ctrl+S 保存、Ctrl+L 清空、Ctrl+C 复制。
        绑定在窗口上而不是 bind_all：这样「替换词典」等子窗口里的输入不受影响。
        """
        self.bind("<F5>", self._on_shortcut_toggle)
        self.bind("<Escape>", lambda _e: self._stop())
        self.bind("<Control-s>", lambda _e: self._save())
        self.bind("<Control-S>", lambda _e: self._save())
        self.bind("<Control-l>", lambda _e: self._clear())
        self.bind("<Control-L>", lambda _e: self._clear())
        self.bind("<Control-c>", self._on_shortcut_copy)
        self.bind("<Control-C>", self._on_shortcut_copy)

    def _on_shortcut_toggle(self, _event=None):
        if self.running:
            self._stop()
        else:
            self._start()
        return "break"

    def _on_shortcut_copy(self, _event=None):
        """Ctrl+C：可编辑控件里交回控件自己处理；结果区有选区就复制选区，否则复制全文。"""
        widget = self.focus_get()
        if isinstance(widget, (tk.Entry, tk.Text)):
            try:
                if str(widget.cget("state")) != "disabled":
                    return None
            except Exception:
                return None
        self._copy(use_selection=True)
        return "break"

    def _save(self) -> None:
        if not self.final_text:
            return
        default_name = f"语音转文字_{datetime.now():%Y%m%d_%H%M%S}.txt"
        path = filedialog.asksaveasfilename(
            title="保存识别结果",
            defaultextension=".txt",
            initialfile=default_name,
            initialdir=str(APP_DIR),
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        Path(path).write_text(self.final_text, encoding="utf-8-sig")
        self._set_status(f"已保存：{path}")

    def on_close(self) -> None:
        self.running = False
        self._save_geometry()  # 记下窗口大小和位置，下次启动原样还原
        for attr in ("_countdown_job", "_enter_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        if self.engine is not None:
            try:
                self.engine.stop()
            except Exception:
                pass
            self.engine = None
        listener, self.ptt = getattr(self, "ptt", None), None
        if listener is not None:
            listener.stop()
            listener.join(timeout=1.5)  # 等热键真正注销，否则新实例注册不上同一个热键
        # 模型还在加载时退出，等它收尾，避免 Vosk 原生代码被强杀导致崩溃弹窗
        thread = getattr(self, "_model_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.5)
        # 下载中退出：通知下载线程收手，让已下的数据正常落盘，方便下次续传
        if self._downloading:
            self._dl_cancel.set()
        log("程序退出")
        self.destroy()


def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    # 先判断是否已有实例在跑：否则第二次启动会重写日志，把真正在跑的那个实例的日志覆盖掉
    if not acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "语音转文字已经在运行",
            "程序已经打开过了（可能在任务栏或最小化了）。\n\n"
            "同时开两个的话，只有一个能抢到热键，另一个会一直提示「热键注册失败」，"
            "所以这里就不重复启动了。",
        )
        root.destroy()
        return

    start_log()

    if not DEPS_OK:
        log(f"依赖导入失败：{DEPS_ERR}")
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "缺少运行库",
            f"依赖库导入失败：\n{DEPS_ERR}\n\n请先双击运行「安装依赖.bat」。",
        )
        root.destroy()
        return

    def report_exception(exc_type, exc_value, exc_tb):
        import traceback

        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log(f"未捕获异常：\n{detail.strip()}")
        try:
            messagebox.showerror("程序出错", detail[-1500:])
        except Exception:
            pass

    sys.excepthook = report_exception

    log("正在创建界面…")
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    log("进入主循环")
    app.mainloop()


if __name__ == "__main__":
    main()