"""把构建产物打成可直接发给玩家的 zip。

产物：``%LOCALAPPDATA%\\IteratorPet\\dist\\迭代器-<版本>-win64.zip``
解开是一个 ``迭代器/`` 文件夹，里面有 ``启动.exe``、``_internal/`` 和 ``使用说明.txt``。

**必须在干净目录里打包/验证**：仓库所在的 ``F:\\RainworldIteratorPet`` 带继承性的
Low 完整性标签，exe 在里面跑会被降级到 Low IL，任务栏拒绝它注册托盘图标
（详见 ``build.py`` 顶部）。所以这里默认跟 build 用同一个 ``%LOCALAPPDATA%`` 产物目录。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "dist"
FOLDER = "迭代器"
EXE_NAME = "启动"

README = """迭代器 · 桌面宠物
==================

怎么开始
--------
双击 启动.exe 就行。

第一次运行会自己找正版 Rain World（Steam 库、注册表都查一遍）。
找不到就先用内置图元跑起来（不会再弹窗问你）；想手动指定：
    启动.exe --setup
或者设环境变量 RAINWORLD_DIR 指向游戏根目录
（里面有 RainWorld.exe 和 RainWorld_Data 文件夹）。

之后每次运行都直接启动，不再问。

怎么退出
--------
三选一，随便哪个：
  1. 右键任务栏托盘里的图标 → 退出。
     Windows 11 默认会把新图标折进任务栏的 ^ 里，看不见就点一下 ^。
  2. 右键桌面上的迭代器本身 → 退出（开了「鼠标穿透」时这一条不可用）。
  3. 按 Ctrl+Alt+Q，任何时候都能退出。

右键迭代器（或托盘图标）的菜单里还能：**尺寸**（便携版 40% / 青春版 75% / 满血版 100% /
PRO 150%）、**角色**（五块卵石 / 仰望皓月）、**画面**（像素风 / 像素对齐 / 光影）、
**声音**（原版的迭代器说话声、机械臂和放电音效，第一次启动从你本机游戏里取；可分开关、调音量）、**待在**
（左 / 右 / 上 / 下 / 自由活动——人偶和滑车都缩到那条边，给你腾出屏幕）、电光频率、
暂停/继续、开关鼠标穿透。改完立刻生效并记住。

**可以拖着玩**：左键按住**人偶**（头或身体）拖着走，松手会顺着拖动方向滑一小段；
左键按住**滑车**拖着走，滑车沿屏幕边缘的轨道跟手。只有人偶和滑车能点，别的地方
（光环、柔光、放电的白光）鼠标都会穿过去，不挡你点下面的窗口。

全屏看视频 / 打游戏 / 放幻灯片时它会自己藏起来，退出全屏再出来。

AI（可选，用 DeepSeek）
-----------------------
右键 → AI → API Key… 填你的 DeepSeek Key（或设环境变量 DEEPSEEK_API_KEY），
再勾 AI → AI 驱动。之后它会自己决定去哪、看什么、什么时候开口；会读你前台窗口的
标题和（本地识别的）文字，有自己的记忆和日记。默认不开看图时截图不离开本机，识别出的文字会进模型请求；开启看图后截图也可能上传；
敏感窗口按规则屏蔽，但会漏判，处理隐私时请关闭读屏和看图。右键 → AI → 读屏幕 可以关掉或只看标题。

五块卵石和仰望皓月的记忆、对话记录各是各的，切换角色就像切换对话；它们知道彼此，
但不知道对方和你聊过什么。token 用量和提醒两边共用。

  Ctrl+Alt+I        人偶头顶弹出快捷输入条（config.json 的 chat_hotkey 可改）：
                    直接打字是聊天；/ 开头是命令，敲 / 列出全部、Tab 补全、↑↓ 历史、Esc 收起
                      /work -r 去读一下 D:\\notes 里的说明，列个大纲    长活（-w 可写，-C 换文件夹）
                      /remind 25m 休息一下     /stop  /status  /pearl  /char  /park 左  /chat  /help
                    Ctrl+Enter 或 /chat 打开完整对话窗口（右键 → 对话窗口… 也行）
  右键 → AI 状态…   余额、今日用量、它的心情 / 日程 / 记忆 / 反思，「现在反思」
  "半小时后提醒我喝水"  直接对它说就行
  直接说"读一下任务文件夹里的 xx，帮我找出……"  它自己判断要不要接活：任务文件夹里的只读任务直接开始，
                    要写文件或换文件夹先弹确认卡；命令按批准规则执行（/work 也还能用）
  右键 → AI → 接入编程 agent…   Claude Code / Codex / MCP 跑完了直接告诉它（给好了可复制的配置；只在本机、要口令）
  "看到下载好了告诉我"  心事：它自己去看是哪个窗口 / 进程 / 文件，定好怎么盯，等到了告诉你
                    （也可以 /watch 要等的事；/unwatch 全部放下；默认最多等 3 小时）

珍珠：把文本类文件（txt、md、代码、csv、json、log……）或一段文字拖到人偶身上，或者在对话窗口
点「递珍珠」，它会像游戏里读珍珠那样读给你听，读过的留在它身边绕着漂。珍珠里的内容只当数据，
里面写的"记住……""提醒我……"不会生效。右键 → 画面 → 珍珠 可以不显示。
以管理员身份运行时（比如从管理员终端启动）只能拖文件、拖不了文字，最好用普通权限启动。

右键 → 珍珠：每颗都能再读、扔掉、转给另一位迭代器，也能全部扔掉
（快捷输入条里 /pearls 列出、/drop 编号 扔掉、/give 编号 捎的话 转交）。
转过去的珍珠，另一位下次被切出来时会读，并且知道是谁给的。
有心事在盯时，光环上方有一圈慢慢转的琥珀色小弧；右键 → AI 状态… 的「心事」栏能看到它的打算，也能放下。

图片、截图、网页链接、音视频也能递：图交给看图模型（用不了就只读本地认出的字），视频压成一张 3×3 分镜 + 字幕。
音视频要装 ffmpeg（放进 PATH，或 config.json 的 ffmpeg_path）；B 站 / YouTube 链接还要装 yt-dlp。
没字幕的音视频可以配语音识别（config.json 的 asr_base_url / asr_model / asr_api_key）。

详细的能力、授权、命令与隐私风险说明见同目录 README.md（可用文本编辑器打开）。

对话窗口里的「让他/她去做…」：它只能读（可写模式下还能写）你选的那个任务文件夹；
简单只读命令可能直接运行，其余默认先请你批准；开启自动批准会扩大直接运行范围。
命令使用 PowerShell 5.1，以你的权限运行（
只读模式也管不住命令，看清楚再点）。装了 DeepSeek Harness 的话，还可以选「DSH 沙箱」只读执行。

**别把本文件夹放进带了"低完整性"标记的目录再运行**（极少见，一般是某些
沙箱/下载工具建的目录）。Windows 会拒绝 Low 完整性进程注册托盘图标，
表现就是"托盘里没有图标"。普通目录（桌面、下载、D 盘自建文件夹）都没问题。

数据放在哪
----------
  %LOCALAPPDATA%\\IteratorPet\\config.json    设置（含 API Key，别发给别人）
  %LOCALAPPDATA%\\IteratorPet\\lines.json     不开 AI 时迭代器说的话（可以自己加，两秒内生效）
  %LOCALAPPDATA%\\IteratorPet\\characters\\<角色>\\memory.json   记住的事、好感度、日记、最近经历（可以直接改）
  %LOCALAPPDATA%\\IteratorPet\\characters\\<角色>\\chat.jsonl    对话记录
  %LOCALAPPDATA%\\IteratorPet\\characters\\<角色>\\context.json  最近几轮、反思进度
  %LOCALAPPDATA%\\IteratorPet\\characters\\<角色>\\pearls\\      收着的珍珠（索引 + 正文副本）
  %LOCALAPPDATA%\\IteratorPet\\usage.json     token 用量
  %LOCALAPPDATA%\\IteratorPet\\reminders.json 提醒（两个角色共用）
  %LOCALAPPDATA%\\IteratorPet\\errands.json   心事：在等的事和它的打算（两个角色共用）
  %LOCALAPPDATA%\\IteratorPet\\bridge\\        本机接口的端口和口令（bridge.json）、给钩子用的 notify.ps1
  %LOCALAPPDATA%\\IteratorPet\\cache\\        从你本机游戏里切出来的图元
  %LOCALAPPDATA%\\IteratorPet\\launcher.log   启动过程日志（排障用）

卸载：删掉本文件夹，再删 %LOCALAPPDATA%\\IteratorPet 即可。

不会往你的游戏目录写任何东西
------------------------------
只读地读 resources.assets / palette26，不改动、不复制游戏资源到外面。

Rain World 及其全部美术、代码版权归 Videocult / Akupara Games 所有。
"""


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None, help="构建产物目录（与 build.py 一致）")
    args = parser.parse_args()

    out_dir = Path(args.out) if args.out else DEFAULT_OUT
    dist = out_dir / EXE_NAME
    exe = dist / f"{EXE_NAME}.exe"
    if not exe.is_file():
        print(f"先跑 packaging/build.py（找不到 {exe}）")
        return 2
    # 先在包里自检：构建中途失败（比如 exe 正被占用）会留下一个缺文件的 dist，照样能打成 zip（2026-11-02 真遇到过）
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "selftest.txt"
        code = subprocess.run([str(exe), "--selftest", str(report)], timeout=120).returncode
        text = report.read_text(encoding="utf-8") if report.is_file() else "（没有生成自检结果）"
    if code != 0:
        print(f"[pack] 自检没过，不打包：\n{text}")
        return 3
    print("[pack] 自检通过")

    stage = out_dir / FOLDER
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(dist, stage)
    (stage / "使用说明.txt").write_text(README, encoding="utf-8-sig")
    shutil.copy2(ROOT / "README.md", stage / "README.md")
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE")
    (stage / "docs").mkdir(exist_ok=True)
    for image in ("readme-pebbles.png", "preview-pixel-lighting.png"):
        shutil.copy2(ROOT / "docs" / image, stage / "docs" / image)

    target = out_dir / f"{FOLDER}-v{version()}-win64.zip"
    if target.exists():
        target.unlink()
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, Path(FOLDER) / path.relative_to(stage))
                count += 1
    size = target.stat().st_size / (1024 * 1024)
    print(f"[pack] {target}  ({count} 个文件, {size:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
