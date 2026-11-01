"""User settings, persisted next to the app data folder."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


# 尺寸档位（用户 2026-10-28 定的四档）：倍率是相对"满血版"的。
# 渲染倍率 scale 越大 → 腔室（仿真像素）越小 → 人偶在屏幕上越大。
SIZE_PRESETS = {
    "portable": (0.40, "便携版（40%）"),
    "youth": (0.75, "青春版（75%）"),
    "full": (1.00, "满血版（100%）"),
    "pro": (1.50, "PRO（150%）"),
}
BASE_SCALE = 1.5          # "满血版"=100% 对应的渲染倍率（本机调出来的值）
# 2：用户把 token 预算放宽到 1000 万 / 天，旧配置里保存的保守默认值（30 万、180 s）作废
# 3、4：监视者长文滚动速度默认值 8 → 16 → 32 字/秒；save() 会把当时的默认值落进 config.json，不重置的话新默认永远生效不了
CONFIG_VERSION = 4
_RESET_ON_UPGRADE = {2: ("llm_daily_token_cap", "llm_plan_refresh_seconds"), 3: ("overseer_scroll_speed",), 4: ("overseer_scroll_speed",)}
DEFAULT_SIZE = "full"


def scale_for(size: str) -> float:
    return round(BASE_SCALE * SIZE_PRESETS.get(size, (1.0, ""))[0], 3)


def nearest_size(scale: float) -> str:
    """老配置里只有 scale（没有 size）时，按最接近的档位补齐。"""
    return min(SIZE_PRESETS, key=lambda key: abs(scale_for(key) - scale))


@dataclass
class Config:
    margin: float = 34.0
    radius: float = 64.0
    click_through: bool = False
    always_on_top: bool = True
    fps: int = 60
    scale: float = BASE_SCALE
    # 尺寸档位：portable / youth / full / pro（托盘菜单里切；scale 由它派生）
    size: str = DEFAULT_SIZE
    # Rain World 安装目录（启动器第一次运行时自动探测并写入；空 = 还没初始化）
    install: str = ""
    # 电光频率档位：off / eco / computing（开发者菜单里切，见 constants.HALO_SPARK_RATES）
    spark_rate: str = "eco"
    # 杀鼠标时的光标特效（档 2：换忙光标 0.3 s）。默认开，可用托盘开关 / 环境变量关掉
    cursor_fx: bool = True
    # 对话气泡开关（托盘「对话」）。关掉后不抽池、不绘制。
    dialogue_enabled: bool = True
    # 角色：pebbles（五块卵石）/ past_moon（过去之月，倾盆大雨 DLC 那位）。
    # 外观参数在 simulation/appearance.py；托盘「角色」菜单切换。
    character: str = "pebbles"
    # ---- LLM 导演（brain/）：托盘「AI 驱动」开关；Key 也可以用环境变量 DEEPSEEK_API_KEY ----
    llm_enabled: bool = False
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    # 日程做完后最多等多久就再请求一次（秒；平时靠显著度/表达欲触发，这只是兜底）
    # （v0.1 的 llm_idle_seconds=45 语义不同，换了名字，旧值自动作废）
    llm_plan_refresh_seconds: float = 75.0
    # 每天主动请求的 token 上限（超了只回应用户说话）；0 = 不限
    llm_daily_token_cap: int = 10_000_000
    # 话量 0..1：只管"自言自语"的频率（用户找它、拖它、提醒照常说）
    llm_talkativeness: float = 0.5
    # 「今天别说话」：到这个时刻（本地时间戳，次日 0 点）之前不主动开口、不主动请求；0 = 没开
    quiet_until: float = 0.0
    # 估算花费用的单价（元 / 百万 token：输入命中缓存、输入未命中、输出）——以官网为准，自己改
    llm_price_hit: float = 0.2
    llm_price_miss: float = 2.0
    llm_price_out: float = 3.0
    # 读屏幕：off / title（窗口标题 + 空闲）/ ocr（再加本地文字识别，截图不出本机）
    perception: str = "ocr"
    # 黑名单：进程名或标题含这些词的窗口打码、不截图（空 = 用内置默认表）
    perception_blacklist: list = field(default_factory=list)
    # 明确要求看屏幕时可发图：off / roi（焦点或变化区）/ full（当前前台整窗）
    # 默认关闭；启用后图片会发给 llm_vision_model
    screen_vision: str = "off"
    # 地盘：left / right / top / bottom；空 = 满屏自由（右键「待在」或模型的 park 改它）
    territory: str = ""
    # 上次退出时人偶在屏幕上的位置（屏幕比例 [x, y]），下次从这里出来；空 = 默认位置
    last_position: list = field(default_factory=list)
    # 像素风：按游戏像素画、最近邻放大（原作的观感，也更省）；pixel_snap = 倍率吸附到整数
    pixel_art: bool = True
    pixel_snap: bool = False
    # 分层落影（画在桌面上的影子；觉得像污渍可以关）
    drop_shadow: bool = True
    # 收着的珍珠绕着人偶漂（最多 5 颗）
    show_pearls: bool = True
    # 界面风格：overseer = 监视者全息投影（心情 / 任务进度等），windows = 一律走普通窗口（dev/监视者/overseer-design.md §3.5）
    ui_style: str = "overseer"
    overseer_scroll_speed: float = 32.0     # 长结果投影的滚动速度（字/秒）
    # 原版音效（platform/sound.py）：迭代器说话的哔哔啵啵、字幕轻响、机械臂 / 底座 / 电光 / 放电
    sound_enabled: bool = True
    sound_speech: bool = True
    sound_actions: bool = True
    sound_volume: float = 0.6
    # 珍珠第二期：看图用的模型（读图片 / 视频分镜；失败就只用本地认出的字）
    llm_vision_model: str = "deepseek-flash"
    # 委托任务里一眼只读、只在任务文件夹里的命令（Get-ChildItem、Select-String、git status……）不逐条批准
    auto_approve_readonly: bool = True
    auto_approve_all: bool = False       # 自动模式：委托任务里的命令都直接运行，只有命中危险规则的还会问
    # 本机接口：编程 agent（Claude Code / Codex / MCP）跑完了直接告诉它（brain/bridge.py；只听 127.0.0.1，要口令）
    bridge_enabled: bool = True
    # 外部工具（空 = 在 PATH 里找）：ffmpeg 处理音视频，yt-dlp 读视频网站的链接
    ffmpeg_path: str = ""
    ytdlp_path: str = ""
    # 没字幕的音视频用的语音识别（OpenAI 兼容的 /audio/transcriptions，比如 SiliconFlow 的 SenseVoice）；空 = 不用
    asr_base_url: str = ""
    asr_model: str = ""
    asr_api_key: str = ""
    # 委托任务联网（brain/web.py）：每个任务在确认卡上单独勾「联网」才有，默认不勾。
    # 配了搜索服务才有 web_search，没配只能读网页
    # （search_provider: tavily / brave / searxng；key 只在本机；searxng 填 search_base_url）。
    search_provider: str = ""
    search_api_key: str = ""
    search_base_url: str = ""
    # 全屏程序（游戏/视频/演示）时自动退到边上、闭嘴
    auto_quiet: bool = True
    # 电脑忙（CPU > 60%）时光环电光自动切到 computing；用户把电光关了就不动
    cpu_halo: bool = True
    # 委托任务：dsh.cmd 的路径（空 = 自动找）与默认任务文件夹（空 = 文档\IteratorPet-workspace）
    dsh_path: str = ""
    task_workspace: str = ""
    # 启动 dsh 的控制台方式（见 brain/tasks.LAUNCH_MODES；tools/dsh_probe.py 帮你选）
    dsh_launch: str = "hidden-console"
    # 调 dsh 的方式（见 brain/tasks.WIRINGS）
    dsh_wiring: str = "cmd-stdin"
    # 对话窗口的全局快捷键（Ctrl/Alt/Shift/Win + 字母/数字/F1-F12/Space）
    chat_hotkey: str = "Ctrl+Alt+I"
    # 习惯系统（brain/habits/）：工作区（空 = %USERPROFILE%\IteratorWS）；便宜时段 "00:30-08:30"（空 = 准点跑）；
    # 习惯没写预算时的默认值（每次运行 token / 步数，每天 token）
    workspace_dir: str = ""
    cheap_hours: str = ""
    habit_default_budget: dict = field(default_factory=lambda: {"run_tokens": 30000, "run_steps": 20, "day_tokens": 80000})
    # 更新提示：一天最多查一次 GitHub 最新 Release（匿名 GET，不带本机信息）
    update_check: bool = True
    update_checked_at: float = 0.0
    config_version: int = CONFIG_VERSION


def llm_api_key(cfg: Config) -> str:
    """环境变量优先（不想把 Key 写进 config.json 的人用这个）。"""
    return os.environ.get("DEEPSEEK_API_KEY") or getattr(cfg, "llm_api_key", "") or ""


def config_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "IteratorPet"


def config_path() -> Path:
    return config_dir() / "config.json"


def load() -> Config:
    path = config_path()
    if not path.is_file():
        return Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Config()
    known = {f.name for f in fields(Config)}
    if isinstance(raw, dict):
        old = int(raw.get("config_version", 1) or 1)
        for version, keys in _RESET_ON_UPGRADE.items():
            if old < version:
                for key in keys:
                    raw.pop(key, None)
        raw["config_version"] = CONFIG_VERSION
    if isinstance(raw, dict) and "size" not in raw and "scale" in raw:
        # 老配置：只有 scale，没有档位 → 反推最接近的那一档（不改它的 scale）
        try:
            raw["size"] = nearest_size(float(raw["scale"]))
        except (TypeError, ValueError):
            pass
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    # size 是唯一真相：scale 永远由档位派生（老配置刚在上面补完 size，这里顺手对齐）
    cfg.scale = scale_for(cfg.size)
    return cfg


def save(cfg: Config) -> None:
    import os
    import traceback

    try:
        config_dir().mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    except OSError:
        if os.environ.get("ITERATORPET_DEBUG"):
            print(f"[debug] save() failed for {config_path()}:\n{traceback.format_exc()}")
        else:
            pass
