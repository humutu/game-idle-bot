"""按卡名选择升级卡牌：OCR 读标题，再按用户配置的优先级挑一张。"""

import re
from dataclasses import dataclass, field

import cv2
import numpy as np

MIN_SCORE = 0.75
UPSCALE = 3

# 额外机会界面的卡背会随剩余张数换尺寸，多尺度匹配才能都覆盖到。
CARD_SCALES = tuple(round(0.75 + i * 0.05, 2) for i in range(15))


@dataclass
class Choice:
    index: int
    name: str | None
    score: float
    readings: list[str] = field(default_factory=list)
    ready: bool = False
    point: tuple[float, float] | None = None


def clean(text):
    return re.sub(r"[\s\W_]+", "", text or "", flags=re.UNICODE)


def _distance(a, b):
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def similarity(text, name):
    """只容忍 OCR 认错一个字，避免把相近的卡名当成同一张。"""
    a, b = clean(text), clean(name)
    if not a or not b:
        return 0.0
    return 1.0 - _distance(a, b) / max(len(a), len(b))


_WILDCARD_CACHE = {}


def _wildcard_regex(pattern):
    """把 电磁* / *力场* 这类条目编译成正则；不是通配条目则返回 None。"""
    if pattern in _WILDCARD_CACHE:
        return _WILDCARD_CACHE[pattern]
    raw = pattern.strip()
    parts = [clean(part) for part in raw.split("*")]
    parts = [part for part in parts if part]
    regex = None
    if "*" in raw and parts:
        body = ".*".join(re.escape(part) for part in parts)
        if not raw.startswith("*"):
            body = "^" + body
        if not raw.endswith("*"):
            body = body + "$"
        regex = re.compile(body)
    _WILDCARD_CACHE[pattern] = regex
    return regex


def ocr_text(frame, box):
    x0, y0, x1, y1 = box
    h, w = frame.shape[:2]
    left, right = sorted((int(x0 * w), int(x1 * w)))
    top, bottom = sorted((int(y0 * h), int(y1 * h)))
    crop = frame[max(0, top):min(h, bottom), max(0, left):min(w, right)]
    if crop.size == 0:
        return ""

    import winocr
    from PIL import Image

    crop = cv2.resize(crop, None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    result = winocr.recognize_pil_sync(Image.fromarray(rgb), "zh-Hans")
    return clean(" ".join(line.get("text", "") for line in result.get("lines", [])))


def ocr_lines(frame, scale=None):
    """整帧 OCR，返回 (文字, (x0, y0, x1, y1))，坐标已换算回原图像素。"""
    import winocr
    from PIL import Image

    if scale is None:
        # 窄窗口字很小，放得更大一些；宽窗口 2 倍就够，避免识别太慢。
        scale = 3 if frame.shape[1] < 700 else 2
    if scale != 1:
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    result = winocr.recognize_pil_sync(Image.fromarray(rgb), "zh-Hans")

    lines = []
    for line in result.get("lines", []):
        rects = [word["bounding_rect"] for word in line.get("words") or []
                 if "bounding_rect" in word]
        if not rects:
            continue
        x0 = min(rect["x"] for rect in rects) / scale
        y0 = min(rect["y"] for rect in rects) / scale
        x1 = max(rect["x"] + rect["width"] for rect in rects) / scale
        y1 = max(rect["y"] + rect["height"] for rect in rects) / scale
        lines.append((clean(line.get("text", "")), (x0, y0, x1, y1)))
    return lines


def _looks_like_label(text):
    """建造提示、界面按钮等不是卡名；卡名本身可能以「炮台」结尾，不能按后缀拦。"""
    return any(word in text for word in ("建造", "组合列表", "刷新", "等级", "关卡"))


def _match_name(text, priority, min_score=MIN_SCORE):
    """取最像的卡名；分数相同才按优先级表顺序决定。"""
    if _looks_like_label(text):
        return None
    best = None
    for rank, name in enumerate(priority):
        if "*" in name:
            regex = _wildcard_regex(name)
            if regex is None or not regex.search(text):
                continue
            score = 1.0
        else:
            score = similarity(text, name)
            if score < min_score:
                continue
        if best is None or score > best[2]:
            best = (rank, name, score)
            if score >= 1.0:
                break
    return best


def choose_located(frame, priority, hint_y=0.48):
    """整帧找卡名：先找出「一排三张卡」的标题行，再在这行里按优先级挑一张。

    窗口尺寸变化后固定比例框会飘，整帧 OCR 不依赖具体布局；找不到成排的标题时
    返回 None，交给 choose() 用配置里的 title_boxes 兜底。
    """
    h, w = frame.shape[:2]
    if w <= 0 or h <= 0:
        return None

    auto = 3 if w < 700 else 2
    for scale in (auto, 2 if auto == 3 else 3):
        choice = _locate_at(frame, priority, hint_y, scale)
        if choice is not None:
            return choice
    return None


def _locate_at(frame, priority, hint_y, scale):
    h, w = frame.shape[:2]
    titles = []
    for text, (x0, y0, x1, y1) in ocr_lines(frame, scale):
        if not (2 <= len(text) <= 8) or _looks_like_label(text):
            continue
        height = y1 - y0
        if height < h * 0.008 or height > h * 0.06:
            continue
        titles.append((y0, y1, x0, x1, text))
    if not titles:
        return None

    # 卡名在同一排、高度相近；炮台标签有 5 个，卡名这一排只有 3 个。
    titles.sort(key=lambda item: (item[0] + item[1]) / 2)
    rows = []
    for item in titles:
        center = (item[0] + item[1]) / 2
        height = max(4.0, item[1] - item[0])
        for row in rows:
            if abs(row["center"] - center) <= height * 0.8:
                row["items"].append(item)
                break
        else:
            rows.append({"center": center, "items": [item]})

    candidates = []
    for row in rows:
        items = sorted(row["items"], key=lambda item: item[2])
        columns = {min(2, int((item[2] + item[3]) / 2 / w * 3)) for item in items}
        spread = items[-1][2] - items[0][2]
        # 卡名固定在标题行附近；炮台标签在上面一排，用纵向位置排除。
        if abs(row["center"] / h - hint_y) > 0.06:
            continue
        # 只有三列都读到才敢用整帧结果；漏读一张就交给固定框兜底，免得忽略那张卡。
        if len(columns) != 3 or spread < w * 0.4:
            continue
        matches = []
        for item in items:
            match = _match_name(item[4], priority)
            if match is not None:
                matches.append((item, match))
        if not matches:
            continue
        avg_len = sum(len(item[4]) for item in items) / len(items)
        score = (len(columns), -abs(row["center"] / h - hint_y), -avg_len)
        candidates.append((score, matches))

    if not candidates:
        return None
    _, matches = max(candidates, key=lambda entry: entry[0])
    (y0, y1, x0, x1, _), (_, name, score) = min(
        matches, key=lambda entry: entry[1][0])
    index = min(2, int((x0 + x1) / 2 / w * 3))
    point = ((x0 + x1) / 2, y1 + (y1 - y0))
    readings = [item[4] for item, _ in matches]
    return Choice(index=index, name=name, score=score, readings=readings,
                  ready=True, point=point)


def _dedupe(hits):
    """同一张卡在不同尺度/偏移下会重复命中，只保留最像的那一个。"""
    accepted = []
    for score, x, y, w, h in sorted(hits, key=lambda item: -item[0]):
        cx, cy = x + w / 2, y + h / 2
        duplicate = False
        for _, ax, ay, aw, ah in accepted:
            mx, my = max(4, aw * 0.15), max(4, ah * 0.15)
            inside = (ax - mx <= cx <= ax + aw + mx
                      and ay - my <= cy <= ay + ah + my)
            ix = max(0, min(x + w, ax + aw) - max(x, ax))
            iy = max(0, min(y + h, ay + ah) - max(y, ay))
            inter = ix * iy
            if inside or (inter and inter / (w * h + aw * ah - inter) > 0.1):
                duplicate = True
                break
        if not duplicate:
            accepted.append((score, x, y, w, h))
    accepted.sort(key=lambda item: (item[2], item[1]))
    return accepted


def find_card_backs(frame, template_gray, threshold=0.62):
    """找出所有还没翻开的卡背，返回按从上到下、从左到右排序的 (score, x, y, w, h)。"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    fh, fw = gray.shape[:2]
    th0, tw0 = template_gray.shape[:2]
    hits = []
    for scale in CARD_SCALES:
        tw, th = int(round(tw0 * scale)), int(round(th0 * scale))
        if tw < 24 or th < 24 or tw > fw or th > fh:
            continue
        scaled = cv2.resize(template_gray, (tw, th), interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(gray, scaled, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(result >= threshold)
        for y, x in zip(ys, xs):
            hits.append((float(result[y, x]), int(x), int(y), tw, th))
    return _dedupe(hits)


def fallback_index(fallback, count):
    if isinstance(fallback, int):
        return min(max(fallback, 0), count - 1)
    if str(fallback).lower() == "left":
        return 0
    if str(fallback).lower() == "right":
        return count - 1
    return (count - 1) // 2


def choose(frame, title_boxes, priority, fallback="middle"):
    readings = [ocr_text(frame, box) for box in title_boxes]
    index = fallback_index(fallback, len(readings))

    best = None
    for slot, text in enumerate(readings):
        match = _match_name(text, priority)
        if match is None:
            continue
        rank, name, score = match
        candidate = (rank, -score, slot, name, score)
        if best is None or candidate[:2] < best[:2]:
            best = candidate

    ready = sum(1 for text in readings if text) >= len(readings) - 1
    if best is None:
        return Choice(index=index, name=None, score=0.0, readings=readings, ready=ready)
    rank, _, slot, name, score = best
    return Choice(index=slot, name=name, score=score, readings=readings, ready=ready)
