import json
from pathlib import Path

import cv2
import numpy as np


def imread_unicode(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def imwrite_unicode(path, image):
    suffix = str(path).rsplit(".", 1)[-1]
    ok, buf = cv2.imencode("." + suffix, image)
    if not ok:
        raise RuntimeError(f"图片编码失败: {path}")
    buf.tofile(str(path))


def to_gray(image):
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def load_template_meta(path):
    """读取 crop 时写入的同名 json 里的参考窗口宽度；没有就返回 None。"""
    try:
        data = json.loads(Path(path).with_suffix(".json").read_text(encoding="utf-8"))
        ref = int(data.get("ref_width") or 0)
    except (OSError, ValueError, TypeError):
        return None
    return ref if ref > 0 else None


def resize_template(template_gray, scale):
    if abs(scale - 1.0) < 0.005:
        return template_gray
    width = max(4, int(round(template_gray.shape[1] * scale)))
    height = max(4, int(round(template_gray.shape[0] * scale)))
    return cv2.resize(template_gray, (width, height), interpolation=cv2.INTER_AREA)


def is_flat(template_gray, threshold=2.0):
    return float(template_gray.std()) < threshold


def locate(frame_gray, template_gray):
    th, tw = template_gray.shape[:2]
    fh, fw = frame_gray.shape[:2]
    if th > fh or tw > fw:
        return 0.0, None

    if is_flat(template_gray):
        result = cv2.matchTemplate(frame_gray, template_gray, cv2.TM_SQDIFF_NORMED)
        min_val, _, min_loc, _ = cv2.minMaxLoc(result)
        return 1.0 - float(min_val), min_loc

    result = cv2.matchTemplate(frame_gray, template_gray, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(result)
    return float(score), loc


def center_of(loc, template_gray):
    th, tw = template_gray.shape[:2]
    return loc[0] + tw // 2, loc[1] + th // 2
