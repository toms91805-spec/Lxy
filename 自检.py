# -*- coding: utf-8 -*-
"""语音转文字 · 自检工具

一键检查运行环境：依赖、模型、麦克风、热键、文字注入、权限。
用法：双击「自检工具.bat」，或在本目录执行
    .venv\\Scripts\\python.exe 自检.py
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

OK = "[ OK ]"
WARN = "[警告]"
FAIL = "[失败]"
INFO = "[信息]"

_results: list[str] = []


def report(level: str, title: str, detail: str = "") -> None:
    _results.append(level)
    print(f"{level} {title}")
    if detail:
        print(f"       {detail}")


def check_python() -> None:
    version = sys.version.split()[0]
    if sys.version_info >= (3, 9):
        report(OK, f"Python 版本 {version}")
    else:
        report(WARN, f"Python 版本 {version}", "建议使用 3.9 及以上版本")


def check_deps() -> None:
    missing = []
    for name in ("vosk", "sounddevice", "numpy", "soxr", "pypinyin"):
        try:
            __import__(name)
            report(OK, f"依赖 {name} 可用")
        except Exception as exc:
            missing.append(name)
            report(FAIL, f"依赖 {name} 缺失", f"{type(exc).__name__}: {exc}")
    if missing:
        report(INFO, "缺少依赖", "双击「安装依赖.bat」即可自动安装")


def _is_model_dir(path: Path) -> bool:
    return (path / "conf").is_dir() or (path / "am").is_dir()


def check_model() -> None:
    """列出 models 目录里所有可用的模型（支持多语言：中文 / 英语 / 日语等）。"""
    models_dir = APP_DIR / "models"
    found: list[Path] = []
    if models_dir.is_dir():
        for child in sorted(models_dir.iterdir()):
            if child.is_dir() and _is_model_dir(child):
                found.append(child)
    if not found:
        report(FAIL, "没有找到任何识别模型",
               f"启动程序会自动下载，或手动解压到 {models_dir}")
        return
    for path in found:
        kind = "高精度模型" if "small" not in path.name else "轻量模型"
        report(OK, f"{kind}已就绪：{path.name}", str(path))
    report(INFO, "识别语言",
           "可在界面「识别语言与精度」里勾选多种可识别的语言（中文 / 英语（美式、英式）/ 日语）；"
           "识别时自动判断说的是哪一种，勾选后会下载对应模型")


def check_mic() -> None:
    try:
        import sounddevice as sd

        devices = list(sd.query_devices())
        inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
        if not inputs:
            report(FAIL, "没有检测到麦克风输入设备",
                   "请插好麦克风，并在「Windows 设置 → 隐私和安全性 → 麦克风」允许桌面应用访问")
            return
        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = None
        report(OK, f"检测到 {len(inputs)} 个输入设备")
        for index, info in inputs:
            mark = "（默认）" if index == default_in else ""
            report(INFO, f"  [{index}] {info['name']} {mark}")
    except Exception as exc:
        report(FAIL, "查询麦克风失败", f"{type(exc).__name__}: {exc}")


def check_hotkey() -> None:
    """尝试注册再注销每个热键，报告哪些当前可用。"""
    user32 = ctypes.windll.user32
    MOD_CONTROL, MOD_NOREPEAT = 0x0002, 0x4000
    hotkeys = {
        "F9": (0, 0x78),
        "F8": (0, 0x77),
        "F10": (0, 0x79),
        "F7": (0, 0x76),
        "F6": (0, 0x75),
        "Ctrl+F9": (MOD_CONTROL, 0x78),
    }
    available = []
    for name, (mods, vk) in hotkeys.items():
        if user32.RegisterHotKey(None, 0xB1, MOD_NOREPEAT | mods, vk):
            user32.UnregisterHotKey(None, 0xB1)
            available.append(name)
            report(OK, f"热键 {name} 可用")
        else:
            report(WARN, f"热键 {name} 已被其他程序占用")
    if not available:
        report(FAIL, "所有热键都被占用", "请关闭占用 F6~F10 / Ctrl+F9 的软件后再试")


def check_permission() -> None:
    try:
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        elevated = False
    if elevated:
        report(OK, "当前以管理员身份运行", "可以往游戏等提权程序输入文字")
    else:
        report(WARN, "当前是普通权限运行",
               "记事本/浏览器/微信够用；往游戏输入文字需要管理员权限")


def check_inject() -> None:
    try:
        user32 = ctypes.windll.user32
        if not hasattr(user32, "SendInput"):
            report(FAIL, "SendInput 接口不可用")
            return
        report(OK, "文字注入接口（SendInput / PostMessage）可用")
    except Exception as exc:
        report(FAIL, "文字注入接口检查失败", f"{type(exc).__name__}: {exc}")


def main() -> int:
    line = "=" * 60
    print(line)
    print("语音转文字 · 自检工具")
    print(line)

    for title, func in (
        ("运行环境", check_python),
        ("依赖库", check_deps),
        ("识别模型", check_model),
        ("麦克风", check_mic),
        ("全局热键", check_hotkey),
        ("权限与注入", lambda: (check_permission(), check_inject())),
    ):
        print(f"\n【{title}】")
        func()

    print("\n" + line)
    fails = _results.count(FAIL)
    warns = _results.count(WARN)
    print(f"检查结束：共 {len(_results)} 项，失败 {fails} 项，警告 {warns} 项")
    if fails == 0:
        print("结论：环境正常，可以正常使用。")
    else:
        print("结论：存在失败项，请按上面的提示处理后再启动程序。")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())