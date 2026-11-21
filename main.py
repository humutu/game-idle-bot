import argparse
import logging
import sys
import time
from pathlib import Path

from gamebot import winutil
from gamebot.engine import ConfigError, Engine, load_config


def setup_logging():
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"run_{time.strftime('%Y%m%d_%H%M%S')}.log"

    formatter = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger = logging.getLogger("gamebot")
    logger.setLevel(logging.INFO)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger, path


def trim_logs(log_dir, keep, current):
    if keep <= 0:
        return 0
    logs = sorted(log_dir.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for path in logs[keep:]:
        if path.resolve() == current.resolve():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def pick_window(args, cfg):
    if args.hwnd:
        return winutil.get_window(args.hwnd)

    pattern = args.window or cfg.get("window")
    if not pattern:
        raise ConfigError("配置里缺少 window（窗口标题正则），也可以用 --window 指定")

    matches = winutil.list_windows(pattern)
    if not matches:
        raise ConfigError(
            f"没有找到标题匹配 {pattern!r} 的窗口。\n"
            "用 python -m gamebot.tools windows 看看当前窗口标题。"
        )
    if len(matches) > 1:
        lines = [f"  0x{w['hwnd']:08X}  {w['process']:<16} {w['title']}" for w in matches]
        raise ConfigError(
            "标题匹配到多个窗口，请用 --hwnd 指定：\n" + "\n".join(lines)
        )
    return matches[0]


def _hwnd(value):
    try:
        return winutil.parse_hwnd(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="小游戏挂机助手：截屏识别按钮 + 模拟点击/按键",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "首次使用：\n"
            "  1. python -m gamebot.tools windows\n"
            "  2. python -m gamebot.tools snapshot --window \"小游戏\"\n"
            "  3. python -m gamebot.tools crop --name btn_xxx\n"
            "  4. 在 config.yaml 里写好规则，然后运行 python main.py --dry-run 校验\n"
            "热键：F8 暂停/继续，F9 停止"
        ),
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--window", help="窗口标题正则，覆盖配置文件里的 window")
    parser.add_argument("--hwnd", type=_hwnd, help="直接指定窗口句柄（匹配到多个窗口时用，支持 0x 写法）")
    parser.add_argument("--max-min", type=float, help="最长运行分钟数，覆盖配置")
    parser.add_argument("--dry-run", action="store_true", help="只识别、不点击，用于校准")
    parser.add_argument("--debug", action="store_true", help="每次命中保存标注截图到 captures/debug")
    parser.add_argument("--no-foreground-check", action="store_true",
                        help="不要求游戏窗口在前台（不推荐，可能点错窗口）")
    parser.add_argument("--background", action="store_true",
                        help="后台模式：窗口被遮挡也能截图和点击，不需要切前台，但不能最小化")
    args = parser.parse_args(argv)

    logger, log_path = setup_logging()
    try:
        cfg, base_dir = load_config(args.config)
        if args.max_min is not None:
            cfg["max_runtime_min"] = args.max_min
        if cfg.get("mode_name"):
            logger.info("挂机模式：%s", cfg["mode_name"])
        keep = int(cfg.get("log_keep", 20))
        removed = trim_logs(log_path.parent, keep, log_path)
        if removed:
            logger.info("已清理 %d 份旧日志，保留最近 %d 份", removed, keep)
        win = pick_window(args, cfg)
        logger.info("目标窗口：0x%08X  %s  (%s)", win["hwnd"], win["title"], win["process"])

        background = args.background or bool(cfg.get("background"))
        engine = Engine(
            cfg,
            base_dir,
            win["hwnd"],
            dry_run=args.dry_run,
            debug=args.debug,
            foreground_only=not args.no_foreground_check,
            background=background,
            logger=logger,
        )
        engine.run()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2
    except LookupError as exc:
        logger.error("%s", exc)
        return 2
    logger.info("日志已保存：%s", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
