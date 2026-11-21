import argparse
import json
import sys
import time
from pathlib import Path

import cv2

from . import vision, winutil
from .capture import Capturer
from .engine import ConfigError, Engine, load_config

sys.stdout.reconfigure(errors="replace")

CAPTURES = Path("captures")
TEMPLATES = Path("templates")


def _hwnd(value):
    try:
        return winutil.parse_hwnd(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _resolve_window(args, cfg=None):
    if args.hwnd:
        return winutil.get_window(args.hwnd)
    pattern = args.window or (cfg or {}).get("window")
    if not pattern:
        raise SystemExit("请用 --window 指定窗口标题正则（先用 windows 命令查看）")
    matches = winutil.list_windows(pattern)
    if not matches:
        raise SystemExit(f"没有窗口标题匹配 {pattern!r}，先运行 windows 命令查看")
    if len(matches) > 1:
        lines = [f"  0x{w['hwnd']:08X}  {w['process']:<16} {w['title']}" for w in matches]
        raise SystemExit("匹配到多个窗口，改用 --hwnd 指定：\n" + "\n".join(lines))
    return matches[0]


def cmd_windows(args):
    windows = winutil.list_windows(args.filter)
    if not windows:
        print("没有可见窗口" + (f"匹配 {args.filter!r}" if args.filter else ""))
        return
    print(f"{'句柄':<12}{'进程':<22}{'尺寸':<14}标题")
    for win in windows:
        width, height = win["rect"][2], win["rect"][3]
        flag = " (最小化)" if win["minimized"] else ""
        print(f"0x{win['hwnd']:08X}  {win['process']:<22}{width}x{height:<8}  {win['title']}{flag}")
    if not args.filter:
        print("\n提示：加上 --filter 关键词缩小范围，例如 --filter 小游戏")


def cmd_snapshot(args):
    win = _resolve_window(args)
    winutil.focus_window(win["hwnd"])
    time.sleep(0.8)
    capturer = Capturer()
    try:
        frame = capturer.grab(win["hwnd"])
    finally:
        capturer.close()

    CAPTURES.mkdir(exist_ok=True)
    out = CAPTURES / f"frame_{time.strftime('%Y%m%d_%H%M%S')}.png"
    vision.imwrite_unicode(out, frame)
    print(f"已保存 {out}  ({frame.shape[1]}x{frame.shape[0]})")
    print(f"下一步：python -m gamebot.tools crop --image {out} --name btn_xxx")


def cmd_crop(args):
    if args.image:
        source = Path(args.image)
    else:
        shots = sorted(CAPTURES.glob("frame_*.png"), key=lambda p: p.stat().st_mtime)
        if not shots:
            raise SystemExit("captures 里还没有截图，先运行 snapshot")
        source = shots[-1]

    image = vision.imread_unicode(source)
    if image is None:
        raise SystemExit(f"无法读取图片：{source}")

    print(f"截图：{source}，在弹出窗口里框住按钮，按 Enter 保存，按 C 取消")
    roi = cv2.selectROI("Drag the button, press Enter to save", image,
                        showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()
    x, y, w, h = (int(v) for v in roi)
    if w < 4 or h < 4:
        raise SystemExit("选区太小，已取消")

    name = (args.name or f"tpl_{time.strftime('%H%M%S')}").strip()
    if name.endswith(".png"):
        name = name[:-4]
    TEMPLATES.mkdir(exist_ok=True)
    out = TEMPLATES / f"{name}.png"
    template = image[y:y + h, x:x + w]
    vision.imwrite_unicode(out, template)
    out.with_suffix(".json").write_text(
        json.dumps({"ref_width": int(image.shape[1])}, ensure_ascii=False),
        encoding="utf-8",
    )

    score, loc = vision.locate(vision.to_gray(image), vision.to_gray(template))
    print(f"已保存模板 {out}（{w}x{h}，自匹配分数 {score:.3f}，参考宽度 {image.shape[1]}）")
    if vision.is_flat(vision.to_gray(template)):
        print("注意：这块区域接近纯色，容易匹配到别处，重裁时把按钮文字或图标一起框进来")
    print(f"在 config.yaml 的 rules 里写：template: templates/{name}.png")


def cmd_probe(args):
    cfg, base_dir = load_config(args.config)
    win = _resolve_window(args, cfg)
    winutil.focus_window(win["hwnd"])
    time.sleep(0.5)

    engine = Engine(cfg, base_dir, win["hwnd"], dry_run=True)
    try:
        frame = engine.capturer.grab(win["hwnd"])
    finally:
        engine.capturer.close()

    gray = vision.to_gray(frame)
    annotated = frame.copy()
    results = []
    for rule in engine.rules:
        template_gray = engine.template_for(rule.name, frame.shape[1])
        native_width = engine.templates[rule.name].shape[1]
        scale = template_gray.shape[1] / native_width
        score, loc = vision.locate(gray, template_gray)
        passed = loc is not None and score >= rule.threshold
        results.append((rule, score, passed, scale))
        if loc is None:
            continue
        th, tw = template_gray.shape[:2]
        color = (0, 200, 0) if passed else (0, 140, 255)
        cv2.rectangle(annotated, loc, (loc[0] + tw, loc[1] + th), color, 2)
        cv2.putText(annotated, f"{score:.2f}", (loc[0], max(12, loc[1] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    CAPTURES.mkdir(exist_ok=True)
    out = CAPTURES / f"probe_{time.strftime('%Y%m%d_%H%M%S')}.png"
    vision.imwrite_unicode(out, annotated)

    print(f"截图尺寸 {frame.shape[1]}x{frame.shape[0]}，标注图：{out}\n")
    for rule, score, passed, scale in sorted(results, key=lambda r: r[1], reverse=True):
        mark = "命中" if passed else "未达阈值"
        note = "" if abs(scale - 1.0) < 0.005 else f"  缩放 {scale:.3f}"
        print(f"  [{mark}] {rule.name:<24} 分数 {score:.3f}  阈值 {rule.threshold:.2f}{note}")
    hits = sum(1 for _, _, passed, _ in results if passed)
    print(f"\n当前画面命中 {hits} 条规则。调不好的话，用 crop 重新裁模板，或在配置里调阈值。")


def build_parser():
    parser = argparse.ArgumentParser(description="小游戏挂机校准工具")
    sub = parser.add_subparsers(dest="command", required=True)

    windows = sub.add_parser("windows", help="列出可见窗口和标题")
    windows.add_argument("--filter", help="标题关键词或正则，如 小游戏")
    windows.set_defaults(func=cmd_windows)

    snapshot = sub.add_parser("snapshot", help="截取游戏窗口客户区保存到 captures")
    snapshot.add_argument("--window", help="窗口标题正则")
    snapshot.add_argument("--hwnd", type=_hwnd, help="窗口句柄，支持 0x 写法")
    snapshot.set_defaults(func=cmd_snapshot)

    crop = sub.add_parser("crop", help="交互式框选按钮，保存为模板")
    crop.add_argument("--image", help="截图路径，默认用 captures 里最新的 frame_*.png")
    crop.add_argument("--name", help="模板名，默认 tpl_时间")
    crop.set_defaults(func=cmd_crop)

    probe = sub.add_parser("probe", help="用当前画面跑一遍所有规则，输出分数")
    probe.add_argument("--config", default="config.yaml")
    probe.add_argument("--window", help="窗口标题正则，覆盖配置")
    probe.add_argument("--hwnd", type=_hwnd, help="窗口句柄，支持 0x 写法")
    probe.set_defaults(func=cmd_probe)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ConfigError, LookupError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
