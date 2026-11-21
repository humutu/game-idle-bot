import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import win32gui
import yaml

from . import cards, inputs, vision, winutil
from .capture import Capturer

DEFAULTS = {
    "interval": 0.6,
    "click_delay": 0.5,
    "threshold": 0.85,
    "jitter_px": 2,
    "stall_warn_sec": 60,
}


class ConfigError(Exception):
    pass


@dataclass
class Rule:
    name: str
    template_path: Path
    action: str
    point: tuple | None
    point_rel: tuple | None
    key: str | None
    threshold: float
    cooldown: float
    stage_end: bool = False
    slots: list | None = None
    title_boxes: list | None = None
    priority: list | None = None
    fallback: str | int = "middle"
    timeout: float = 180.0
    card_threshold: float = 0.62
    max_picks: int = 5
    pick_delay: float = 1.0


def load_config(path):
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在：{config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        raise ConfigError("配置文件根节点必须是键值映射")
    return cfg, config_path.parent


class Engine:
    def __init__(self, cfg, base_dir, hwnd, *, dry_run=False, debug=False,
                 foreground_only=True, background=False, logger=None):
        self.cfg = cfg
        self.base_dir = Path(base_dir)
        self.hwnd = hwnd
        self.dry_run = dry_run
        self.debug = debug
        self.background = background
        self.foreground_only = foreground_only and not background
        self.log = logger or logging.getLogger("gamebot")
        self.defaults = {**DEFAULTS, **(cfg.get("defaults") or {})}
        self.mode = cfg.get("mode", "rules")
        self.max_runtime = float(cfg.get("max_runtime_min", 240)) * 60
        self.stage_end = str(cfg.get("stage_end") or "continue").strip().lower()
        if self.stage_end not in ("continue", "stop", "pause"):
            raise ConfigError("stage_end 只能是 continue / stop / pause，现在是：" + self.stage_end)
        self._stage_held = None
        self._pause_requested = False
        self._stage_stop = False
        self.stop_event = threading.Event()
        self.hits = Counter()
        self._last_hit = time.monotonic()
        self._stall_logged = False

        self.rules = []
        self.disabled_rules = []
        self.steps = list(cfg.get("steps") or [])
        self.templates = {}
        self.template_refs = {}
        self._scaled_templates = {}
        self._draw_templates = {}
        self._rule_last = {}
        self._card_misses = {}
        self._manual_rule = None
        self._manual_deadline = 0.0
        self._grab_error = None
        self._step_idx = 0
        self._wait_until = 0.0
        self._load()
        self.capturer = Capturer(background=background)

    def _load(self):
        if self.mode not in ("rules", "sequence"):
            raise ConfigError(f"mode 只能是 rules 或 sequence，当前是 {self.mode!r}")
        ui_ref = int(self.defaults.get("ui_width") or 0) or None

        if self.mode == "sequence":
            if not self.steps:
                raise ConfigError("sequence 模式需要在配置里写 steps 列表")
            for index, step in enumerate(self.steps, 1):
                if not isinstance(step, dict):
                    raise ConfigError(f"第 {index} 步必须是映射，例如 - click: [0.5, 0.9]")
                kinds = [k for k in ("click", "key", "wait") if k in step]
                if len(kinds) != 1:
                    raise ConfigError(f"第 {index} 步必须且只能包含 click / key / wait 之一")
                if "click" in step:
                    point = step["click"]
                    if not (isinstance(point, (list, tuple)) and len(point) == 2):
                        raise ConfigError(f"第 {index} 步 click 必须是 [x, y] 比例，如 [0.5, 0.9]")
                if "key" in step:
                    try:
                        inputs.parse_key(str(step["key"]))
                    except ValueError as exc:
                        raise ConfigError(f"第 {index} 步：{exc}") from exc
            return

        raw_rules = self.cfg.get("rules") or []
        if not raw_rules:
            raise ConfigError(
                "rules 模式还没有任何规则。\n"
                "1) 先截屏：python -m gamebot.tools snapshot --window \"小游戏\"\n"
                "2) 裁按钮模板：python -m gamebot.tools crop --name btn_xxx\n"
                "3) 在 config.yaml 的 rules 里引用模板"
            )

        seen = set()
        for index, item in enumerate(raw_rules, 1):
            if not isinstance(item, dict):
                raise ConfigError(f"第 {index} 条规则必须是映射")
            name = str(item.get("name") or f"规则{index}")
            if name in seen:
                raise ConfigError(f"规则名重复：{name}")
            seen.add(name)

            if item.get("enabled", True) is False:
                self.disabled_rules.append(name)
                continue

            template = item.get("template")
            if not template:
                raise ConfigError(f"规则 [{name}] 缺少 template")
            template_path = (self.base_dir / template).resolve()
            if not template_path.is_file():
                raise ConfigError(
                    f"规则 [{name}] 的模板文件不存在：{template_path}\n"
                    "先运行 snapshot 截屏，再用 crop 裁出模板。"
                )
            image = vision.imread_unicode(template_path)
            if image is None:
                raise ConfigError(f"规则 [{name}] 的模板无法读取：{template_path}")

            action = item.get("action", "click")
            if action not in ("click", "key", "wait", "manual", "pick_card", "draw_cards"):
                raise ConfigError(f"规则 [{name}] 的 action 只能是 click / key / wait / manual / pick_card / draw_cards")

            timeout = float(item.get("timeout", 180))
            if timeout <= 0:
                raise ConfigError(f"规则 [{name}] 的 timeout 必须大于 0 秒")

            slots = title_boxes = priority = None
            fallback = item.get("fallback", "middle")
            card_threshold = 0.62
            max_picks = 5
            pick_delay = 1.0
            if action == "draw_cards":
                card_template = item.get("card_template")
                if not card_template:
                    raise ConfigError(f"规则 [{name}] 的 draw_cards 需要 card_template（卡背模板）")
                card_path = (self.base_dir / card_template).resolve()
                if not card_path.is_file():
                    raise ConfigError(f"规则 [{name}] 的卡背模板不存在：{card_path}")
                card_image = vision.imread_unicode(card_path)
                if card_image is None:
                    raise ConfigError(f"规则 [{name}] 的卡背模板无法读取：{card_path}")
                self._draw_templates[name] = vision.to_gray(card_image)
                self.template_refs["draw:" + name] = vision.load_template_meta(card_path) or ui_ref
                card_threshold = float(item.get("card_threshold", 0.62))
                if not 0 < card_threshold <= 1:
                    raise ConfigError(f"规则 [{name}] 的 card_threshold 必须在 0 到 1 之间")
                max_picks = int(item.get("max_picks", 5))
                if max_picks < 1:
                    raise ConfigError(f"规则 [{name}] 的 max_picks 至少是 1")
                pick_delay = float(item.get("pick_delay", 1.0))
                if pick_delay < 0:
                    raise ConfigError(f"规则 [{name}] 的 pick_delay 不能是负数")
            elif action == "pick_card":
                slots = self._parse_regions(name, "slots", item.get("slots"), dims=2, min_count=2)
                title_boxes = self._parse_regions(name, "title_boxes", item.get("title_boxes"),
                                                  dims=4, min_count=len(slots))
                if len(title_boxes) != len(slots):
                    raise ConfigError(
                        f"规则 [{name}] 的 slots 和 title_boxes 数量必须一致"
                        f"（现在 {len(slots)} 对 {len(title_boxes)}）"
                    )
                priority = item.get("priority")
                if not (isinstance(priority, list) and priority):
                    raise ConfigError(f"规则 [{name}] 的 pick_card 需要 priority 列表，比如 [穿甲炮, 强化子弹]")
                priority = [str(v).strip() for v in priority if str(v).strip()]
                if not priority:
                    raise ConfigError(f"规则 [{name}] 的 priority 列表不能为空")
                if not (isinstance(fallback, int) or str(fallback).lower() in ("left", "middle", "right")):
                    raise ConfigError(f"规则 [{name}] 的 fallback 只能是 left / middle / right 或序号")

            point = item.get("point")
            if point is not None:
                if not (isinstance(point, (list, tuple)) and len(point) == 2):
                    raise ConfigError(f"规则 [{name}] 的 point 必须是 [x, y] 比例，如 [0.5, 0.88]")
                point = (float(point[0]), float(point[1]))

            point_rel = item.get("point_rel")
            if point_rel is not None:
                if not (isinstance(point_rel, (list, tuple)) and len(point_rel) == 2):
                    raise ConfigError(f"规则 [{name}] 的 point_rel 必须是 [x, y] 模板倍数，如 [2.4, 4.6]")
                point_rel = (float(point_rel[0]), float(point_rel[1]))

            key = item.get("key")
            if action == "key":
                if not key:
                    raise ConfigError(f"规则 [{name}] 的 action 是 key，但没有写 key")
                try:
                    inputs.parse_key(str(key))
                except ValueError as exc:
                    raise ConfigError(f"规则 [{name}]：{exc}") from exc

            self.rules.append(
                Rule(
                    name=name,
                    template_path=template_path,
                    action=action,
                    point=point,
                    point_rel=point_rel,
                    key=str(key) if key else None,
                    threshold=float(item.get("threshold", self.defaults["threshold"])),
                    cooldown=float(item.get("cooldown", 0)),
                    stage_end=bool(item.get("stage_end", False)),
                    slots=slots,
                    title_boxes=title_boxes,
                    priority=priority,
                    fallback=fallback,
                    timeout=timeout,
                    card_threshold=card_threshold,
                    max_picks=max_picks,
                    pick_delay=pick_delay,
                )
            )
            self.templates[name] = vision.to_gray(image)
            self.template_refs[name] = vision.load_template_meta(template_path) or ui_ref
            if vision.is_flat(self.templates[name]):
                self.log.warning("规则 [%s] 的模板接近纯色，匹配容易误判；建议裁入按钮文字或图标", name)

    @staticmethod
    def _parse_regions(rule_name, key, value, *, dims, min_count):
        if not (isinstance(value, list) and len(value) >= min_count):
            raise ConfigError(f"规则 [{rule_name}] 的 {key} 至少需要 {min_count} 组")
        regions = []
        for item in value:
            if not (isinstance(item, (list, tuple)) and len(item) == dims):
                shape = "[x, y]" if dims == 2 else "[x0, y0, x1, y1]"
                raise ConfigError(f"规则 [{rule_name}] 的 {key} 每一项必须是 {shape} 比例")
            regions.append(tuple(float(v) for v in item))
        return regions

    def template_for(self, name, frame_width, *, draw=False):
        """按当前窗口宽度缩放模板；裁剪工具会把裁剪时的窗口宽度写进同名 json。"""
        table = self._draw_templates if draw else self.templates
        ref = self.template_refs.get(("draw:" if draw else "") + name)
        base = table[name]
        if not ref or frame_width <= 0:
            return base
        scale = max(0.4, min(3.0, frame_width / ref))
        if abs(scale - 1.0) < 0.005:
            return base
        key = (("draw:" if draw else "") + name, int(round(frame_width)))
        scaled = self._scaled_templates.get(key)
        if scaled is None:
            scaled = vision.resize_template(base, scale)
            self._scaled_templates[key] = scaled
        return scaled

    def _sleep_with_stop(self, seconds):
        self.stop_event.wait(max(0.0, seconds))

    def run(self):
        if self.mode == "rules":
            self.log.info("规则模式：%s", "、".join(rule.name for rule in self.rules))
            if self.disabled_rules:
                self.log.info("已禁用规则：%s", "、".join(self.disabled_rules))
        else:
            self.log.info("顺序宏模式：共 %d 步", len(self.steps))
        for rule in self.rules:
            if rule.action == "pick_card":
                self.log.info("%s 优先级：%s（兜底 %s）",
                              rule.name, " > ".join(rule.priority), rule.fallback)
        self.log.info("F8 暂停/继续，F9 停止；最长运行 %.0f 分钟", self.max_runtime / 60)
        if self.stage_end != "continue":
            self.log.info("关卡结束设置：%s",
                          "停在结算页并退出挂机" if self.stage_end == "stop"
                          else "停在结算页等你，按 F8 继续下一关")
        if self.dry_run:
            self.log.info("dry-run：只识别、不点击")

        if self.background:
            winutil.ensure_visible(self.hwnd)
            self.log.info("后台模式：窗口可以被遮挡，但不要最小化")
        else:
            winutil.focus_window(self.hwnd)

        refs = sorted({int(v) for v in self.template_refs.values() if v})
        if refs:
            try:
                width = winutil.client_box(self.hwnd)[2]
            except Exception:
                width = 0
            if width:
                low = min(width / r for r in refs)
                high = max(width / r for r in refs)
                if abs(low - 1.0) > 0.005 or abs(high - 1.0) > 0.005:
                    span = str(refs[0]) if refs[0] == refs[-1] else f"{refs[0]}~{refs[-1]}"
                    self.log.info("模板按窗口缩放：窗口宽度 %d，参考宽度 %s，缩放 %.3f~%.3f",
                                  width, span, low, high)
        started = time.monotonic()
        hotkeys = inputs.Hotkeys()
        paused = False
        away_logged = False
        reason = "收到停止指令"

        try:
            while not self.stop_event.is_set():
                if hotkeys.pressed(inputs.VK_F9):
                    reason = "F9 停止"
                    break
                if hotkeys.pressed(inputs.VK_F8):
                    paused = not paused
                    if not paused:
                        self._stage_held = None
                    self.log.info("已暂停" if paused else "继续运行")
                if self._stage_stop:
                    reason = "本关结束，按设置停止挂机"
                    break
                if self._pause_requested:
                    self._pause_requested = False
                    paused = True
                if time.monotonic() - started > self.max_runtime:
                    reason = "达到最长运行时间"
                    break
                if paused:
                    self._sleep_with_stop(0.08)
                    continue
                if not win32gui.IsWindow(self.hwnd):
                    reason = "游戏窗口已关闭"
                    break
                if self.foreground_only and not winutil.is_foreground(self.hwnd):
                    if not away_logged:
                        self.log.info("游戏窗口不在前台，暂停点击（切回窗口后自动继续）")
                        away_logged = True
                    self._sleep_with_stop(0.3)
                    continue
                away_logged = False

                if self._manual_rule is not None and self._manual_wait():
                    continue

                now = time.monotonic()
                if now < self._wait_until:
                    self._sleep_with_stop(min(0.05, self._wait_until - now))
                    continue

                try:
                    frame = self.capturer.grab(self.hwnd)
                except Exception as exc:
                    if str(exc) != self._grab_error:
                        self.log.warning("截屏失败：%s", exc)
                        self._grab_error = str(exc)
                    self._sleep_with_stop(0.5)
                    continue
                self._grab_error = None

                stall_sec = float(self.defaults["stall_warn_sec"])
                if (stall_sec > 0 and not self._stall_logged
                        and time.monotonic() - self._last_hit > stall_sec):
                    path = self._save_stall(frame)
                    self._stall_logged = True
                    self.log.warning("已经 %.0f 秒没有命中任何规则，可能卡在未配置的界面；截图：%s",
                                     stall_sec, path)

                self._wait_until = time.monotonic() + self._tick(frame)
        except KeyboardInterrupt:
            reason = "Ctrl+C"
        finally:
            self.capturer.close()
            self.stop_event.set()
            self.log.info("挂机结束：%s", reason)
            if self.hits:
                self.log.info("统计：%s", "，".join(f"{name} {count} 次" for name, count in self.hits.items()))
            else:
                self.log.info("统计：本轮没有命中任何规则")

    def _tick(self, frame):
        if self.mode == "sequence":
            return self._tick_sequence()
        return self._tick_rules(frame)

    def _tick_rules(self, frame):
        gray = vision.to_gray(frame)
        frame_h, frame_w = gray.shape[:2]
        now = time.monotonic()

        for rule in self.rules:
            if now < self._rule_last.get(rule.name, 0.0) + rule.cooldown:
                continue
            template_gray = self.template_for(rule.name, frame_w)
            score, loc = vision.locate(gray, template_gray)
            if loc is None or score < rule.threshold:
                continue

            center = vision.center_of(loc, template_gray)
            if (rule.stage_end and self.stage_end != "continue"
                    and self._stage_held != rule.name):
                # 结算页出现 = 这关打完；按设置停在这里，先不点结算按钮。
                self._stage_held = rule.name
                self._last_hit = now
                if self.stage_end == "stop":
                    self.log.info("检测到 [%s]：这关已结束，按设置停止挂机（不点结算按钮）", rule.name)
                    self._stage_stop = True
                else:
                    self.log.info("检测到 [%s]：这关已结束，已暂停（按 F8 继续下一关）", rule.name)
                    self._pause_requested = True
                return float(self.defaults["interval"])

            self.log.info("命中 [%s]，相似度 %.3f", rule.name, score)
            performed = self._fire_rule(rule, center, (frame_w, frame_h), frame, template_gray)
            if performed:
                self.hits[rule.name] += 1
                self._last_hit = time.monotonic()
                self._stall_logged = False
                if self.debug:
                    self._save_debug(frame, rule, loc, template_gray, score)
            self._rule_last[rule.name] = time.monotonic()
            return float(self.defaults["click_delay"] if performed else self.defaults["interval"])

        return float(self.defaults["interval"])

    def _click_at(self, target):
        if self.background:
            inputs.click_bg(
                self.hwnd,
                target[0],
                target[1],
                jitter_px=int(self.defaults["jitter_px"]),
            )
            return
        left, top, _, _ = winutil.client_box(self.hwnd)
        inputs.click(
            left + target[0],
            top + target[1],
            jitter_px=int(self.defaults["jitter_px"]),
        )

    @staticmethod
    def _beep():
        try:
            import winsound
            winsound.Beep(880, 250)
        except Exception:
            pass

    def _manual_wait(self):
        """暂停点击，直到用户自己选完卡（模板消失）；超时则按 point 兜底点一下。"""
        rule = self._manual_rule
        if time.monotonic() >= self._manual_deadline:
            hint = "，先点一下中间继续" if rule.point is not None else ""
            self.log.warning("等待手动选卡超过 %.0f 秒%s", rule.timeout, hint)
            if rule.point is not None and not self.dry_run:
                _, _, width, height = winutil.client_box(self.hwnd)
                self._click_at((rule.point[0] * width, rule.point[1] * height))
            self._manual_rule = None
            self._rule_last[rule.name] = time.monotonic()
            return False

        try:
            frame = self.capturer.grab(self.hwnd)
        except Exception as exc:
            self.log.warning("截屏失败：%s", exc)
            self._sleep_with_stop(0.5)
            return True

        score, _ = vision.locate(vision.to_gray(frame),
                                 self.template_for(rule.name, frame.shape[1]))
        if score < rule.threshold:
            self.log.info("选卡完成，继续挂机")
            self._manual_rule = None
            self._last_hit = time.monotonic()
            self._stall_logged = False
            return False
        self._sleep_with_stop(0.4)
        return True

    def _fire_rule(self, rule, center, frame_size, frame, template_gray):
        if rule.action == "pick_card":
            hint_y = sum((box[1] + box[3]) / 2 for box in rule.title_boxes) / len(rule.title_boxes)
            choice = cards.choose_located(frame, rule.priority, hint_y)
            located = choice is not None
            if choice is None:
                choice = cards.choose(frame, rule.title_boxes, rule.priority, rule.fallback)
            misses = self._card_misses.get(rule.name, 0)
            readings_empty = not any(text.strip() for text in choice.readings)
            unreadable = not choice.ready or (not located and readings_empty)
            if unreadable and misses < 12:
                self._card_misses[rule.name] = misses + 1
                if readings_empty and misses == 0:
                    path = self._save_pick_fail(frame)
                    self.log.info("选卡识别为空，画面已保存：%s", path)
                self.log.info("选卡界面还没渲染完，稍后重试（%d/12，识别：%s）",
                              misses + 1, " / ".join(choice.readings))
                return False
            self._card_misses[rule.name] = 0

            if not located:
                path = self._save_pick_fallback(frame)
                self.log.info("选卡走固定框兜底，画面已保存：%s", path)

            if unreadable:
                self.log.warning("选卡连续识别为空，仍按兜底位置点击（识别：%s）",
                                 " / ".join(choice.readings))

            if choice.name:
                self.log.info("选卡：第 %d 张「%s」，匹配度 %.2f（识别：%s）",
                              choice.index + 1, choice.name, choice.score,
                              " / ".join(choice.readings))
            else:
                self.log.info("选卡：没有命中优先名单，点第 %d 张（识别：%s）",
                              choice.index + 1, " / ".join(choice.readings))
            if self.debug:
                self.log.info("选卡识别明细：%s", " / ".join(choice.readings))
            if self.dry_run:
                return True
            if choice.point is not None:
                self._click_at(choice.point)
            else:
                x, y = rule.slots[choice.index]
                self._click_at((x * frame_size[0], y * frame_size[1]))
            return True

        if rule.action == "draw_cards":
            return self._draw_cards(rule, frame)

        if rule.action == "manual":
            if self.dry_run:
                self.log.info("dry-run：检测到 [%s]，正式运行时会暂停等你手动操作", rule.name)
                return True
            self._manual_rule = rule
            self._manual_deadline = time.monotonic() + rule.timeout
            self.log.info("已暂停自动操作，请手动选择卡牌；选完自动继续（最长等 %.0f 秒）", rule.timeout)
            self._beep()
            return True

        if self.dry_run:
            return True
        if rule.action == "click":
            if rule.point_rel is not None:
                # 相对命中模板左上角，按模板宽高的倍数偏移；列表滚动时也能点准。
                th, tw = template_gray.shape[:2]
                target = (
                    center[0] - tw / 2 + rule.point_rel[0] * tw,
                    center[1] - th / 2 + rule.point_rel[1] * th,
                )
            elif rule.point is not None:
                target = (rule.point[0] * frame_size[0], rule.point[1] * frame_size[1])
            else:
                target = center
            self._click_at(target)
        elif rule.action == "key":
            if self.background:
                self.log.warning("后台模式下不发送按键（会落到前台窗口），规则 [%s] 已跳过", rule.name)
            else:
                inputs.press(rule.key)
        return True

    def _draw_cards(self, rule, frame):
        """额外机会：逐张点开着的卡背，每点一张重新截图识别（剩下的卡会重排）。"""
        picks = 0
        stuck = 0
        previous = None
        while picks < rule.max_picks and not self.stop_event.is_set():
            if picks:
                self._sleep_with_stop(rule.pick_delay)
                try:
                    frame = self.capturer.grab(self.hwnd)
                except Exception as exc:
                    self.log.warning("额外机会：截屏失败，停止抽取（%s）", exc)
                    break

            score, _ = vision.locate(vision.to_gray(frame),
                                     self.template_for(rule.name, frame.shape[1]))
            if score < rule.threshold:
                if picks:
                    self.log.info("额外机会：界面已关闭，本轮共抽 %d 张", picks)
                break

            hits = cards.find_card_backs(
                frame, self.template_for(rule.name, frame.shape[1], draw=True),
                rule.card_threshold,
            )
            if self.dry_run:
                self.log.info("dry-run：额外机会检测到 %d 张未翻开的卡（带紫色图标的也算）：%s",
                              len(hits),
                              "、".join(f"({h[1] + h[3] // 2},{h[2] + h[4] // 2})" for h in hits))
                return True
            if not hits:
                self.log.info("额外机会：没有可点的未翻开卡牌了（已抽 %d 张）", picks)
                break

            hit = hits[0]
            signature = (round(hit[1] / 12), round(hit[2] / 12), len(hits))
            if signature == previous:
                stuck += 1
                if stuck >= 2:
                    self.log.warning("额外机会：卡牌没有翻动，停止继续点（已抽 %d 张）", picks)
                    break
            else:
                stuck = 0
            previous = signature

            target = (hit[1] + hit[3] / 2, hit[2] + hit[4] * 0.72)
            self.log.info("额外机会：点第 %d 张（第 %d 抽），位置 (%.0f, %.0f)，卡背相似度 %.2f",
                          len(hits), picks + 1, target[0], target[1], hit[0])
            self._click_at(target)
            picks += 1

        if picks:
            self.log.info("额外机会：本轮抽了 %d 张", picks)
        return picks > 0

    def _tick_sequence(self):
        step = self.steps[self._step_idx % len(self.steps)]
        index = self._step_idx % len(self.steps) + 1

        if "wait" in step:
            self._step_idx += 1
            return float(step["wait"])

        if "click" in step:
            x, y = (float(v) for v in step["click"])
            self.log.info("步骤 %d：点击 (%.2f, %.2f)", index, x, y)
            if not self.dry_run:
                left, top, width, height = winutil.client_box(self.hwnd)
                inputs.click(
                    left + x * width,
                    top + y * height,
                    jitter_px=int(step.get("jitter", self.defaults["jitter_px"])),
                )
        else:
            self.log.info("步骤 %d：按键 %s", index, step["key"])
            if not self.dry_run:
                inputs.press(str(step["key"]))

        self.hits["步骤"] += 1
        self._last_hit = time.monotonic()
        self._stall_logged = False
        self._step_idx += 1
        return float(self.defaults["click_delay"])

    def _save_stall(self, frame):
        captures = Path("captures")
        captures.mkdir(exist_ok=True)
        path = captures / f"stall_{time.strftime('%Y%m%d_%H%M%S')}.png"
        vision.imwrite_unicode(path, frame)
        return path

    def _save_pick_fail(self, frame):
        captures = Path("captures")
        captures.mkdir(exist_ok=True)
        path = captures / f"pick_empty_{time.strftime('%Y%m%d_%H%M%S')}.png"
        vision.imwrite_unicode(path, frame)
        return path

    def _save_pick_fallback(self, frame):
        captures = Path("captures")
        captures.mkdir(exist_ok=True)
        path = captures / f"pick_fallback_{time.strftime('%Y%m%d_%H%M%S')}.png"
        vision.imwrite_unicode(path, frame)
        return path

    def _save_debug(self, frame, rule, loc, template_gray, score):
        import cv2

        th, tw = template_gray.shape[:2]
        annotated = frame.copy()
        cv2.rectangle(annotated, loc, (loc[0] + tw, loc[1] + th), (0, 255, 0), 2)
        cv2.putText(annotated, f"{score:.3f}", (loc[0], max(12, loc[1] - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
        debug_dir = Path("captures") / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.png"
        vision.imwrite_unicode(debug_dir / name, annotated)
