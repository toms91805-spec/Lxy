# -*- coding: utf-8 -*-
"""把「安装程序 + 应用本体」组装成可分享的压缩包。

前置（由 打包成安装包.bat 自动准备）：
  dist\\语音转文字\\          应用本体，由「打包成应用.bat」生成
  dist_installer\\安装程序\\  安装程序，由 PyInstaller 打包 installer.py 生成

产物：
  发布\\语音转文字安装包\\     可直接双击 安装程序.exe 的目录
  发布\\语音转文字安装包.zip  分享给别人的压缩包（解压后双击 安装程序.exe）
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).parent
APP_SRC = BASE / "dist" / "语音转文字"
INSTALLER_SRC = BASE / "dist_installer" / "安装程序"
OUT_DIR = BASE / "发布"
REL_DIR = OUT_DIR / "语音转文字安装包"
ZIP_PATH = OUT_DIR / "语音转文字安装包.zip"

# 只带小模型：大模型体积大（中文 1.3 GB、英语 128 MB）会让压缩包大到没法分享，
# 需要更高精度时可在程序里自行下载
EXCLUDE_DIRS = {
    "vosk-model-cn-0.22",
    "vosk-model-en-us-0.22-lgraph",
}
# 非默认语言的小模型也一并剔除：对方默认用中文，换成英语/日语时程序会自动下载
# （这样压缩包只含一个中文小模型，体积最小、最好分享）
KEEP_ONLY_SMALL = {"vosk-model-small-cn-0.22"}
# 剔除本机使用痕迹，对方装到的是干净副本
EXCLUDE_FILES = {"运行日志.txt", "设置.json"}

ZIP_ROOT = "语音转文字安装包"


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def folder_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def ignore(directory: str, names: list[str]) -> set[str]:
    skipped = set()
    in_models = Path(directory).name == "models"
    for name in names:
        if name in EXCLUDE_DIRS or name in EXCLUDE_FILES:
            skipped.add(name)
        elif in_models and name.startswith("vosk-model") and name not in KEEP_ONLY_SMALL:
            skipped.add(name)
    return skipped


def assemble() -> None:
    for src, hint in ((APP_SRC, "先运行「打包成应用.bat」"),
                      (INSTALLER_SRC, "本脚本应由「打包成安装包.bat」调用")):
        if not src.exists():
            sys.exit(f"[错误] 找不到：{src}\n        {hint}")

    if not (APP_SRC / "语音转文字.exe").exists():
        sys.exit(f"[错误] 应用本体不完整，缺少：{APP_SRC / '语音转文字.exe'}")

    if REL_DIR.exists():
        shutil.rmtree(REL_DIR)
    REL_DIR.mkdir(parents=True)

    print("[1/3] 复制应用本体（剔除大模型 / 运行日志 / 设置）…")
    shutil.copytree(APP_SRC, REL_DIR / "app", ignore=ignore)

    print("[2/3] 复制安装程序（含 _internal）…")
    for item in INSTALLER_SRC.iterdir():
        dst = REL_DIR / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)

    icon = BASE / "app.ico"
    if icon.exists():
        shutil.copy2(icon, REL_DIR / "app.ico")
    readme = BASE / "README.md"
    if readme.exists():
        shutil.copy2(readme, REL_DIR / "使用说明.md")


def make_zip() -> None:
    print("[3/3] 压缩…")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    files = sorted(p for p in REL_DIR.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    done = 0
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for path in files:
            arc = (Path(ZIP_ROOT) / path.relative_to(REL_DIR)).as_posix()
            zf.write(path, arc)
            done += path.stat().st_size
            pct = done * 100 / total if total else 100
            print(f"\r      {pct:5.1f}%  {human(done)} / {human(total)}", end="", flush=True)
    print()


def main() -> None:
    assemble()
    make_zip()
    print()
    print("完成！")
    print(f"  目录：{REL_DIR}")
    print(f"  压缩包：{ZIP_PATH}  （{human(ZIP_PATH.stat().st_size)}）")
    print(f"  解压后双击「安装程序.exe」即可打开安装页面。")


if __name__ == "__main__":
    main()