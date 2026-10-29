# -*- coding: utf-8 -*-
"""语音转文字 · 安装程序

双击「安装程序.exe」打开安装页面：安装须知 + 自定义安装位置 + 一键安装。
安装做的事：把同目录 app\\ 里的应用文件复制到目标目录，并按需创建桌面快捷方式。
"""

from __future__ import annotations

import ctypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

APP_NAME = "语音转文字"
EXE_NAME = "语音转文字.exe"
PAYLOAD_DIR = "app"
ICON_NAME = "app.ico"
DESKTOP_LINK = "语音转文字"

NO_WINDOW = 0x08000000

# 与应用本体一致的 TraeWork 风格配色
P = {
    "bg": "#f5f5f5", "card": "#ffffff", "border": "#e8e8e8",
    "text": "#171717", "muted": "#8a8a8a",
    "accent": "#4b3fe3", "accent_dark": "#3a2fd0", "accent_fg": "#ffffff",
    "sec_bg": "#ffffff", "sec_hover": "#f5f5f5", "sec_active": "#ececec",
    "sec_disabled_bg": "#fafafa", "btn_disabled_fg": "#c4c4c4",
    "input_bg": "#ffffff", "input_disabled": "#f5f5f5",
    "primary_disabled_bg": "#c7c2f5", "primary_disabled_fg": "#ffffff",
    "ok": "#16a34a", "err": "#e5484d", "note": "#8a8a8a",
}

NOTES = [
    "免安装绿色版：装好后双击桌面「语音转文字」快捷方式即可使用，无需安装 Python、无需联网。",
    "首次启动较慢：程序要加载语音识别模型，第一次打开请耐心等 10~30 秒（窗口像是没反应，其实在加载）。",
    "支持多语言与声调：默认中文，可在界面「识别语言与精度」里勾选多种可识别的语言"
    "（中文 / 英语（美式、英式）/ 日语），识别时自动判断说的是哪一种；"
    "勾选后会联网下载对应模型（约 40~48 MB，首次勾选需要联网）。中文还可打开「声调标注」，"
    "在识别结果下方显示带声调的拼音，方便核对同音字。",
    "输入到游戏需管理员权限：往微信 / 记事本 / 浏览器打字直接双击即可；要往「以管理员身份运行」的游戏里打字，"
    "请右键快捷方式 → 以管理员身份运行。",
    "首次运行会弹安全提示：本程序没有数字签名，Windows SmartScreen 可能提示「已保护你的电脑」，"
    "点「更多信息」→「仍要运行」即可；部分杀毒软件也可能误报。",
    "安装位置：普通文件夹即可，不要装到 C:\\Program Files —— 程序要在自己的目录里保存设置和日志；"
    "路径用英文 / 数字最稳妥。",
    "建议装到本地磁盘：不要放在网络盘或只读 U 盘上，模型从网络盘加载会非常慢。",
    "运行环境：64 位 Windows 10 / 11。",
    "卸载方式：直接删除安装目录，再删掉桌面快捷方式即可（绿色软件，不写系统注册表；"
    "只有勾选过「开机自启动」才会留下一条启动项，取消勾选即清除）。",
]


class InstallCancelled(Exception):
    pass


def system_scale() -> float:
    """系统 DPI 缩放；顺便声明 DPI 感知，避免界面被系统拉伸得发虚。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def work_area() -> tuple[int, int, int, int]:
    """桌面工作区（已扣任务栏）的 left/top/right/bottom。"""
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            if rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, 1280, 800


def round_rect(canvas: tk.Canvas, x0, y0, x1, y1, r, **kwargs):
    """tkinter 没有原生圆角，用「多边形 + 平滑」近似。"""
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    points = [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def base_dir() -> Path:
    """安装程序自己所在的目录（源码运行 / 打包运行都适用）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def payload_dir() -> Path:
    return base_dir() / PAYLOAD_DIR


def default_target() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "Programs" / APP_NAME
    return Path.home() / APP_NAME


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def app_running(target: Path) -> bool:
    """目标目录里的程序是不是正在跑（占用文件会导致覆盖失败，先提醒用户关掉）。"""
    if not (target / EXE_NAME).exists():
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}", "/FO", "CSV"],
            capture_output=True, text=True, creationflags=NO_WINDOW,
        )
        return EXE_NAME.lower() in (result.stdout or "").lower()
    except Exception:
        return False


def create_shortcut(exe: Path, folder: Path | None = None) -> None:
    """在桌面（或指定目录）创建快捷方式；路径里的单引号要转义，否则 PowerShell 会报错。"""
    def q(text: str) -> str:
        return str(text).replace("'", "''")

    if folder is None:
        folder_expr = "[Environment]::GetFolderPath('Desktop')"
    else:
        folder_expr = f"'{q(folder)}'"

    script = (
        "$ws=New-Object -ComObject WScript.Shell;"
        f"$p=Join-Path {folder_expr} '{DESKTOP_LINK}.lnk';"
        "$s=$ws.CreateShortcut($p);"
        f"$s.TargetPath='{q(exe)}';"
        f"$s.WorkingDirectory='{q(exe.parent)}';"
        f"$s.IconLocation='{q(exe.parent / ICON_NAME)},0';"
        f"$s.Description='{APP_NAME}';"
        "$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, creationflags=NO_WINDOW,
    )


def copy_tree(src: Path, dst: Path, on_progress, is_cancelled) -> int:
    """按字节进度复制整个目录，支持中途取消（已复制的文件保留）。"""
    files = [p for p in src.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files) or 1
    done = 0
    for path in files:
        if is_cancelled():
            raise InstallCancelled()
        out = dst / path.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "rb") as fi, open(out, "wb") as fo:
            while True:
                if is_cancelled():
                    raise InstallCancelled()
                chunk = fi.read(4 * 1024 * 1024)
                if not chunk:
                    break
                fo.write(chunk)
                done += len(chunk)
                on_progress(done, total)
        try:
            shutil.copystat(path, out)
        except Exception:
            pass
    return len(files)


class Installer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.scale = system_scale()
        self.tk.call("tk", "scaling", self.scale * 96.0 / 72.0)
        self.title(f"{APP_NAME} 安装程序")
        self.configure(bg=P["bg"])
        self.resizable(False, False)
        self._apply_icon()

        self.payload = payload_dir()
        self.msg: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._busy = False
        self._installed: Path | None = None

        self.target_var = tk.StringVar(value=str(default_target()))
        self.make_shortcut = tk.BooleanVar(value=True)
        self.run_after = tk.BooleanVar(value=True)

        self._build_ui()
        self._center(int(760 * self.scale), int(740 * self.scale))
        self._autofit()
        # 窗口映射后宽度才最终确定，再量一次换行行数并贴合高度（第一次是预估）
        self.after(180, self._autofit)
        self._apply_round_corners()
        self._on_target_change()
        self.after(60, self._poll)

    # ------------------------------------------------------------ 界面
    def _px(self, value: int) -> int:
        return max(1, int(round(value * self.scale)))

    def _apply_icon(self) -> None:
        icon = base_dir() / ICON_NAME
        if icon.exists():
            try:
                self.iconbitmap(default=str(icon))
            except Exception:
                pass

    def _apply_round_corners(self) -> None:
        try:
            hwnd = int(self.wm_frame(), 16)
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)
            )
        except Exception:
            pass

    def _center(self, w: int, h: int) -> None:
        wl, wt, wr, wb = work_area()
        # h 是客户区高度，窗口外框还要占标题栏和边框：不减掉它，窗口底边会落到任务栏后面
        deco = self._px(40)
        w = min(w, wr - wl)
        h = min(h, max(self._px(320), wb - wt - deco))
        x = wl + max(0, (wr - wl - w) // 2)
        y = wt + max(0, (wb - wt - deco - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _autofit(self) -> None:
        """把须知文本框收紧到真实显示行数，再让窗口高度贴合内容，避免留白或卡片被裁。"""
        try:
            self.update_idletasks()
            counted = self.notes_text.count("1.0", "end-1c", "displaylines")
            lines = counted[0] if isinstance(counted, (tuple, list)) else counted
            if lines:
                self.notes_text.configure(height=int(lines))
                self.update_idletasks()
        except Exception:
            pass
        self._center(int(760 * self.scale),
                     max(self.winfo_reqheight(), int(600 * self.scale)))

    def _make_button(self, parent, text, command, kind="secondary", width=None, font=None):
        palette = {
            "primary": dict(
                bg=P["accent"], fg=P["accent_fg"], hover=P["accent_dark"],
                disabled_bg=P["primary_disabled_bg"], disabled_fg=P["primary_disabled_fg"],
                border=P["accent"],
            ),
            "secondary": dict(
                bg=P["sec_bg"], fg=P["text"], hover=P["sec_hover"],
                disabled_bg=P["sec_disabled_bg"], disabled_fg=P["btn_disabled_fg"],
                border=P["border"],
            ),
        }[kind]
        btn = tk.Button(
            parent, text=text, command=command, relief="flat", bd=0, cursor="hand2",
            font=font or ("Microsoft YaHei UI", 10),
            padx=self._px(20), pady=self._px(8),
            bg=palette["bg"], fg=palette["fg"],
            activebackground=palette["hover"], activeforeground=palette["fg"],
            disabledforeground=palette["disabled_fg"],
            highlightthickness=1, highlightbackground=palette["border"],
            highlightcolor=palette["border"], takefocus=False,
        )
        if width:
            btn.config(width=width)
        btn._palette = palette
        btn.bind("<Enter>", lambda _e: str(btn["state"]) != "disabled"
                 and btn.config(bg=palette["hover"]))
        btn.bind("<Leave>", lambda _e: str(btn["state"]) != "disabled"
                 and btn.config(bg=palette["bg"]))
        return btn

    def _set_enabled(self, btn, enabled: bool) -> None:
        btn.config(state="normal" if enabled else "disabled")
        pal = getattr(btn, "_palette", None)
        if pal:
            btn.config(bg=pal["bg"] if enabled else pal["disabled_bg"])

    def _card(self, title: str, subtitle: str = "") -> tk.Frame:
        px = self._px
        outer = tk.Frame(self, bg=P["card"], highlightbackground=P["border"],
                         highlightthickness=1)
        outer.pack(fill="x", padx=px(18), pady=(0, px(10)))
        head = tk.Frame(outer, bg=P["card"])
        head.pack(fill="x", padx=px(16), pady=(px(12), 0))
        tk.Label(head, text=title, bg=P["card"], fg=P["text"],
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        if subtitle:
            tk.Label(head, text=subtitle, bg=P["card"], fg=P["muted"],
                     font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(px(8), 0))
        inner = tk.Frame(outer, bg=P["card"])
        inner.pack(fill="x", padx=px(16), pady=(px(8), px(14)))
        return inner

    def _build_ui(self) -> None:
        px = self._px

        # ---------------------------------------------------------- 顶栏
        header = tk.Frame(self, bg=P["card"])
        header.pack(fill="x")
        left = tk.Frame(header, bg=P["card"])
        left.pack(side="left", padx=px(18), pady=px(13))
        mark = tk.Canvas(left, width=px(30), height=px(30), bg=P["card"],
                         highlightthickness=0)
        mark.pack(side="left", padx=(0, px(10)))
        round_rect(mark, px(1), px(1), px(29), px(29), px(8), fill=P["accent"], outline="")
        mark.create_text(px(15), px(16), text="语", fill=P["accent_fg"],
                         font=("Microsoft YaHei UI", 13, "bold"))
        tk.Label(left, text=f"{APP_NAME} · 安装程序", bg=P["card"], fg=P["text"],
                 font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        tk.Label(header, text="绿色免安装 · 全程离线", bg=P["card"], fg=P["muted"],
                 font=("Microsoft YaHei UI", 9)).pack(side="right", padx=px(18))
        tk.Frame(self, bg=P["border"], height=1).pack(fill="x")

        # ---------------------------------------------------------- 底栏（先占住底部）
        footer = tk.Frame(self, bg=P["bg"])
        footer.pack(side="bottom", fill="x", padx=px(18), pady=(px(6), px(14)))

        self.bar = tk.Canvas(footer, height=px(6), bg=P["bg"], highlightthickness=0)
        self.bar.pack(fill="x", pady=(0, px(10)))
        self._bar_bg = round_rect(self.bar, 0, 0, 10, px(6), px(3),
                                  fill="#e8e8e8", outline="")
        self._bar_fill = None

        row = tk.Frame(footer, bg=P["bg"])
        row.pack(fill="x")
        self.status = tk.Label(row, text="确认安装位置后点「开始安装」", bg=P["bg"],
                               fg=P["muted"], font=("Microsoft YaHei UI", 9), anchor="w")
        self.status.pack(side="left")
        self.install_btn = self._make_button(row, "开始安装", self._on_install, "primary",
                                             font=("Microsoft YaHei UI", 10, "bold"))
        self.install_btn.pack(side="right")
        self.open_btn = self._make_button(row, "打开安装目录", self._open_installed,
                                          "secondary")
        self.cancel_btn = self._make_button(row, "退出", self._on_close, "secondary")
        self.cancel_btn.pack(side="right", padx=(0, px(8)))

        # ---------------------------------------------------------- 安装须知
        notes = self._card("安装须知", "请先读一遍，能少踩很多坑")
        # 高度按须知条数给足，避免最后几条被裁掉；万一用户字体更大而超高，还能用滚轮翻看
        text = tk.Text(
            notes, wrap="word", relief="flat", borderwidth=0, height=13,
            bg=P["card"], fg=P["text"], font=("Microsoft YaHei UI", 10),
            padx=px(2), pady=0, spacing1=px(2), spacing3=px(6), cursor="arrow",
        )
        text.pack(side="left", fill="both", expand=True)
        for line in NOTES:
            # 项目符号用不换行空格连接：整段中文没有空格时，普通空格会让「·」被单独挤成一行
            text.insert("end", f"·\u00a0\u00a0{line}\n")
        text.configure(state="disabled")
        text.bind(
            "<MouseWheel>",
            lambda e: text.yview_scroll(-1 if e.delta > 0 else 1, "units"),
        )
        self.notes_text = text

        # ---------------------------------------------------------- 安装位置
        loc = self._card("安装位置", "默认装在当前用户目录，无需管理员权限")
        line = tk.Frame(loc, bg=P["card"])
        line.pack(fill="x")
        self.target_entry = tk.Entry(
            line, textvariable=self.target_var, bg=P["input_bg"], fg=P["text"],
            insertbackground=P["text"], relief="flat", highlightthickness=1,
            highlightbackground=P["border"], highlightcolor=P["accent"],
            font=("Microsoft YaHei UI", 10),
        )
        self.target_entry.pack(side="left", fill="x", expand=True, ipady=px(5))
        self.browse_btn = self._make_button(line, "浏览…", self._on_browse, "secondary")
        self.browse_btn.pack(side="right", padx=(px(8), 0))
        self.target_hint = tk.Label(loc, text="", bg=P["card"], fg=P["muted"],
                                    font=("Microsoft YaHei UI", 9), anchor="w",
                                    justify="left", wraplength=px(660))
        self.target_hint.pack(fill="x", pady=(px(7), 0))
        self.target_var.trace_add("write", lambda *_: self._on_target_change())

        # ---------------------------------------------------------- 安装选项
        opt = self._card("安装选项")
        tk.Checkbutton(
            opt, text="在桌面创建「语音转文字」快捷方式", variable=self.make_shortcut,
            bg=P["card"], fg=P["text"], activebackground=P["card"],
            activeforeground=P["text"], selectcolor=P["card"],
            font=("Microsoft YaHei UI", 10), anchor="w", highlightthickness=0,
            bd=0, cursor="hand2",
        ).pack(anchor="w")
        tk.Checkbutton(
            opt, text="安装完成后立即运行", variable=self.run_after,
            bg=P["card"], fg=P["text"], activebackground=P["card"],
            activeforeground=P["text"], selectcolor=P["card"],
            font=("Microsoft YaHei UI", 10), anchor="w", highlightthickness=0,
            bd=0, cursor="hand2",
        ).pack(anchor="w", pady=(px(6), 0))

        # 安装包本体缺失时直接禁用按钮，避免用户白点
        if not (self.payload / EXE_NAME).exists():
            self._set_enabled(self.install_btn, False)
            self.status.config(
                text=f"找不到应用文件：{self.payload}（请确认 app 文件夹与本程序在同一目录）",
                fg=P["err"],
            )

    # ------------------------------------------------------------ 交互
    def _on_target_change(self) -> None:
        raw = self.target_var.get().strip().strip('"')
        if not raw:
            self.target_hint.config(text="请填写或选择安装位置", fg=P["err"])
            return
        try:
            raw.encode("ascii")
            tip = "将安装到：" + raw
            self.target_hint.config(text=tip, fg=P["muted"])
        except UnicodeEncodeError:
            self.target_hint.config(
                text="将安装到：" + raw
                + "　（路径含中文，程序会自动处理；若能改用英文 / 数字路径更稳妥）",
                fg=P["note"],
            )

    def _on_browse(self) -> None:
        current = self.target_var.get().strip()
        initial = current if current and Path(current).exists() else str(Path.home())
        chosen = filedialog.askdirectory(title="选择安装位置", initialdir=initial,
                                         mustexist=False)
        if chosen:
            # 选到的是父目录时，自动补一层应用名，避免把文件散到用户选的目录里
            path = Path(chosen)
            if path.name != APP_NAME:
                path = path / APP_NAME
            self.target_var.set(str(path))

    def _on_install(self) -> None:
        if self._busy:
            self._cancel.set()
            self.status.config(text="正在取消…", fg=P["note"])
            return

        raw = self.target_var.get().strip().strip('"')
        if not raw:
            messagebox.showerror("安装位置为空", "请先填写或选择安装位置。")
            return
        try:
            target = Path(raw).expanduser()
            target_res = target.resolve()
            payload_res = self.payload.resolve()
        except Exception as exc:
            messagebox.showerror("安装位置无效", f"无法解析该路径：\n{exc}")
            return

        if not (self.payload / EXE_NAME).exists():
            messagebox.showerror(
                "缺少应用文件",
                f"没有找到：\n{self.payload / EXE_NAME}\n\n"
                "请确认「app」文件夹和「安装程序.exe」在同一个目录下。",
            )
            return
        if target_res == payload_res or payload_res in target_res.parents:
            messagebox.showerror("安装位置无效", "不能装到安装包自己的目录里，请另选一个位置。")
            return
        if target_res in payload_res.parents:
            messagebox.showerror("安装位置无效", "安装位置不能包含安装包目录，请另选一个位置。")
            return
        if app_running(target):
            messagebox.showwarning(
                "程序正在运行",
                f"检测到「{APP_NAME}」正在运行，请先关闭它再安装（否则文件被占用会覆盖失败）。",
            )
            return
        if target.exists() and any(target.iterdir()):
            if not messagebox.askyesno(
                "目录已存在",
                f"目标目录已存在且不为空：\n{target}\n\n继续会覆盖同名文件，是否继续？",
            ):
                return

        self._busy = True
        self._cancel.clear()
        self._installed = None
        self.install_btn.config(text="取消安装")
        self._set_enabled(self.browse_btn, False)
        self.target_entry.config(state="disabled")
        self.status.config(text="正在准备…", fg=P["muted"])
        self._draw_bar(0.0)
        # tkinter 变量不能在工作线程里读，先在主线程取好再交给线程
        make_shortcut = bool(self.make_shortcut.get())
        threading.Thread(target=self._worker, args=(self.payload, target, make_shortcut),
                         daemon=True).start()

    def _open_installed(self) -> None:
        if self._installed and self._installed.exists():
            try:
                os.startfile(str(self._installed))
            except Exception as exc:
                messagebox.showerror("打开失败", f"{type(exc).__name__}: {exc}")

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno("正在安装", "安装还没完成，确定要退出吗？"):
                return
            self._cancel.set()
        self.destroy()

    # ------------------------------------------------------------ 安装线程
    def _worker(self, payload: Path, target: Path, make_shortcut: bool) -> None:
        try:
            target.mkdir(parents=True, exist_ok=True)
            self.msg.put(("status", "正在复制应用文件…"))
            count = copy_tree(
                payload, target,
                lambda done, total: self.msg.put(("progress", (done, total))),
                self._cancel.is_set,
            )
            if make_shortcut:
                self.msg.put(("status", "正在创建桌面快捷方式…"))
                create_shortcut(target / EXE_NAME)
            self.msg.put(("done", (target, count)))
        except InstallCancelled:
            self.msg.put(("cancelled", None))
        except Exception as exc:
            self.msg.put(("error", f"{type(exc).__name__}: {exc}"))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.msg.get_nowait()
                if kind == "status":
                    self.status.config(text=payload, fg=P["muted"])
                elif kind == "progress":
                    done, total = payload
                    self._draw_bar(done / total)
                    self.status.config(
                        text=f"正在复制应用文件…  {human(done)} / {human(total)}"
                             f"（{done * 100 / total:.0f}%）",
                        fg=P["muted"],
                    )
                elif kind == "done":
                    target, count = payload
                    self._finish_ok(target, count)
                elif kind == "cancelled":
                    self._finish_cancelled()
                elif kind == "error":
                    self._finish_error(payload)
        except queue.Empty:
            pass
        finally:
            self.after(60, self._poll)

    def _draw_bar(self, frac: float) -> None:
        frac = max(0.0, min(1.0, frac))
        self.bar.delete("fill")
        width = self.bar.winfo_width() or self._px(700)
        height = self._px(6)
        self.bar.delete(self._bar_bg)
        self._bar_bg = round_rect(self.bar, 0, 0, width, height, height / 2,
                                  fill="#e8e8e8", outline="")
        if frac > 0:
            round_rect(self.bar, 0, 0, max(height, width * frac), height, height / 2,
                       fill=P["accent"], outline="", tags="fill")

    def _reset_busy(self) -> None:
        self._busy = False
        self.install_btn.config(text="开始安装")
        self._set_enabled(self.install_btn, True)
        self._set_enabled(self.browse_btn, True)
        self.target_entry.config(state="normal")

    def _finish_ok(self, target: Path, count: int) -> None:
        self._reset_busy()
        self._installed = target
        self._draw_bar(1.0)
        self.status.config(text=f"安装完成，共 {count} 个文件 → {target}", fg=P["ok"])
        self.install_btn.config(text="完成", command=self._on_done_click)
        self._set_enabled(self.install_btn, True)
        self.open_btn.pack(side="right", padx=(0, self._px(8)))
        self.cancel_btn.pack_forget()
        if self.run_after.get():
            try:
                os.startfile(str(target / EXE_NAME))
            except Exception as exc:
                messagebox.showwarning("启动失败", f"可以手动打开：\n{target / EXE_NAME}\n\n{exc}")

    def _on_done_click(self) -> None:
        self.destroy()

    def _finish_cancelled(self) -> None:
        self._reset_busy()
        self._draw_bar(0.0)
        self.status.config(text="已取消安装（已复制的文件保留，可重新安装覆盖）", fg=P["note"])

    def _finish_error(self, detail: str) -> None:
        self._reset_busy()
        self._draw_bar(0.0)
        self.status.config(text=f"安装失败：{detail}", fg=P["err"])
        messagebox.showerror(
            "安装失败",
            f"{detail}\n\n常见原因：目标目录没有写入权限、磁盘空间不足、"
            "或程序正在运行占用了文件。",
        )


def main() -> None:
    Installer().mainloop()


if __name__ == "__main__":
    main()