"""本地挂机启动器：浏览器里点卡牌，就启动 main.py，并把运行日志回显到网页。"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from gamebot import winutil
from gamebot.engine import load_config

FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
TITLE = "小游戏挂机助手"


def _data_dir():
    if not FROZEN:
        return Path(__file__).resolve().parent
    exe_dir = Path(sys.executable).resolve().parent
    probe = exe_dir / f".write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return exe_dir
    except OSError:
        fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / TITLE
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


BASE = _data_dir()
INDEX = BASE / "launcher.html"
if not INDEX.is_file():
    INDEX = BUNDLE / "launcher.html"
CONFIG = BASE / "config.yaml"
RUN_CONFIG = BASE / ".run-config.yaml"
PYTHON = sys.executable
DEFAULT_PORT = 8765
BOT_CMD = [PYTHON, "--run-bot"] if FROZEN else [PYTHON, "-u", "main.py"]


def _seed():
    if not FROZEN:
        return
    source = BUNDLE / "config.yaml"
    if not CONFIG.exists() and source.is_file():
        shutil.copy2(source, CONFIG)
    src = BUNDLE / "templates"
    if not src.is_dir():
        return
    marker = BASE / ".templates-tag"
    try:
        tag = str(int(Path(sys.executable).stat().st_mtime))
    except OSError:
        tag = "0"
    try:
        if marker.read_text(encoding="utf-8").strip() == tag:
            return
    except OSError:
        pass
    dst = BASE / "templates"
    dst.mkdir(exist_ok=True)
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, dst / item.name)
    try:
        marker.write_text(tag, encoding="utf-8")
    except OSError:
        pass


_seed()


def _mode_defs(cfg):
    raw = cfg.get("modes")
    modes = []
    if isinstance(raw, list):
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                continue
            mode_id = str(item.get("id") or f"mode{index}").strip()
            name = str(item.get("name") or mode_id).strip()
            modes.append({
                **item,
                "id": mode_id,
                "name": name,
                "desc": str(item.get("desc") or "").strip(),
            })
    if not modes:
        modes = [{"id": "default", "name": "默认", "desc": ""}]
    return modes


def _find_mode(modes, mode_id):
    if mode_id:
        for mode in modes:
            if mode["id"] == mode_id:
                return mode
        raise ValueError(f"没有这个挂机模式：{mode_id}")
    return modes[0]


def _apply_mode(cfg, mode):
    merged = {key: value for key, value in cfg.items() if key != "modes"}
    merged["mode_name"] = mode["name"]
    for key in ("window", "background", "max_runtime_min"):
        if key in mode:
            merged[key] = mode[key]
    if isinstance(mode.get("defaults"), dict):
        merged["defaults"] = {**(cfg.get("defaults") or {}), **mode["defaults"]}

    remove = {str(name) for name in (mode.get("remove_rules") or [])}
    rules = []
    for rule in cfg.get("rules") or []:
        if isinstance(rule, dict) and str(rule.get("name")) in remove:
            continue
        rules.append(rule)
    add = mode.get("add_rules") or []
    if not isinstance(add, list):
        raise ValueError(f"模式「{mode['name']}」的 add_rules 必须是列表")
    rules.extend(add)
    merged["rules"] = rules
    return merged


def _write_run_config(cfg):
    text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=1000)
    fd, tmp = tempfile.mkstemp(dir=str(BASE), prefix=".run-config-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, RUN_CONFIG)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _modes_state():
    try:
        cfg, _ = load_config(CONFIG)
    except Exception as exc:
        return 500, {"ok": False, "error": f"读取配置失败：{exc}"}
    modes = _mode_defs(cfg)
    return 200, {
        "ok": True,
        "modes": [
            {
                "id": mode["id"],
                "name": mode["name"],
                "desc": mode["desc"],
                "background": bool(mode.get("background", cfg.get("background", False))),
            }
            for mode in modes
        ],
        "default": modes[0]["id"],
        "stage_end": str(cfg.get("stage_end") or "continue"),
        "current": _current_mode,
        "running": _running(),
    }


_proc = None
_started_at = 0.0
_stopped = False
_lines = deque(maxlen=400)
_lock = threading.Lock()
_start_lock = threading.Lock()
_save_lock = threading.Lock()
_mem_lock = threading.Lock()
_job = None
_current_mode = None
MEM_INTERVAL = 30
_mem = {"launcher": 0, "bot": 0, "peak": 0, "at": ""}
_mem_running = False


def _mem_mb(pid):
    if os.name != "nt" or not pid:
        return 0
    try:
        import ctypes
        import ctypes.wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        handle = ctypes.windll.kernel32.OpenProcess(0x0410, False, pid)
        if not handle:
            return 0
        try:
            info = PMC()
            info.cb = ctypes.sizeof(PMC)
            if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(info), info.cb):
                return 0
            return int(info.WorkingSetSize / 1048576)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return 0


def _mem_tree_mb(root_pid):
    """进程树总内存：venv/单文件壳会再起子进程，只看外壳会少算。"""
    total = _mem_mb(root_pid)
    if os.name != "nt" or not root_pid:
        return total
    try:
        import ctypes
        import ctypes.wintypes as wt

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            return total
        children = {}
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            ok = kernel32.Process32First(snap, ctypes.byref(entry))
            while ok:
                children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
                ok = kernel32.Process32Next(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)

        seen = set()
        stack = [root_pid]
        while stack:
            for child in children.get(stack.pop(), []):
                if child in seen or child == root_pid:
                    continue
                seen.add(child)
                stack.append(child)
                total += _mem_mb(child)
    except Exception:
        pass
    return total


def _sample_mem():
    running = _running()
    bot = _mem_tree_mb(_proc.pid) if running else 0
    with _mem_lock:
        if bot > _mem["peak"]:
            _mem["peak"] = bot
        _mem.update(launcher=_mem_mb(os.getpid()), bot=bot, at=time.strftime("%H:%M:%S"))
        return dict(_mem)


def _mem_log(line):
    path = BASE / "logs" / "memory.log"
    try:
        path.parent.mkdir(exist_ok=True)
        if path.is_file() and path.stat().st_size > 300_000:
            os.replace(path, path.with_name("memory.log.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _mem_loop():
    global _mem_running
    _mem_log(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} 监视开始 launcher pid={os.getpid()}")
    while True:
        time.sleep(MEM_INTERVAL)
        try:
            info = _sample_mem()
            running = _running()
            if running or _mem_running:
                _mem_log(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}  running={1 if running else 0}"
                    f"  launcher={info['launcher']}MB  bot={info['bot']}MB  peak={info['peak']}MB"
                )
            _mem_running = running
        except Exception:
            pass


def _bind_job(proc):
    global _job
    if os.name != "nt":
        return
    try:
        import win32job

        if _job is None:
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation, info)
            _job = job
        win32job.AssignProcessToJobObject(_job, proc._handle)
    except Exception:
        pass


def _reader(pipe):
    try:
        for line in pipe:
            _lines.append(line.rstrip("\n"))
    finally:
        pipe.close()


def _spawn(cmd):
    global _proc, _started_at, _stopped
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONUNBUFFERED": "1"}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with _lock:
        _lines.clear()
        _stopped = False
        with _mem_lock:
            _mem["peak"] = 0
        _proc = subprocess.Popen(
            cmd,
            cwd=str(BASE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=flags,
        )
        _bind_job(_proc)
        _started_at = time.time()
        threading.Thread(target=_reader, args=(_proc.stdout,), daemon=True).start()
        if FROZEN:
            _lines.append("正在解压并启动挂机，通常需要几秒钟…")
        return _proc


def _running():
    return _proc is not None and _proc.poll() is None


def _status():
    running = _running()
    return {
        "running": running,
        "pid": _proc.pid if _proc else None,
        "exit": None if running or _proc is None else _proc.returncode,
        "stopped": (not running) and _stopped,
        "mode": _current_mode,
        "stage_end": (_current_mode or {}).get("stage_end"),
        "uptime": round(time.time() - _started_at) if running else 0,
        "mem": _sample_mem(),
        "lines": list(_lines)[-40:],
    }


def _start(mode_id=None, stage_end=None):
    global _current_mode
    with _start_lock:
        if _running():
            return 409, {"ok": False, "error": "挂机已经在运行中"}

        if stage_end is not None:
            stage_end = str(stage_end).strip().lower()
            if stage_end not in ("continue", "stop", "pause"):
                return 400, {"ok": False, "error": f"没有这个关卡结束选项：{stage_end}"}

        try:
            cfg, _ = load_config(CONFIG)
        except Exception as exc:
            return 500, {"ok": False, "error": f"读取配置失败：{exc}"}

        try:
            mode = _find_mode(_mode_defs(cfg), mode_id)
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}

        try:
            merged = _apply_mode(cfg, mode)
            if stage_end:
                merged["stage_end"] = stage_end
            _write_run_config(merged)
        except Exception as exc:
            return 500, {"ok": False, "error": f"生成模式配置失败：{exc}"}

        pattern = merged.get("window")
        if pattern:
            try:
                matches = winutil.list_windows(str(pattern))
            except Exception as exc:
                return 500, {"ok": False, "error": f"查找游戏窗口失败：{exc}"}
            if not matches:
                return 400, {"ok": False,
                             "error": f"没找到标题匹配「{pattern}」的游戏窗口，先把游戏打开再点卡牌"}

        proc = _spawn(BOT_CMD + ["--config", RUN_CONFIG.name])
        _current_mode = {"id": mode["id"], "name": mode["name"],
                         "stage_end": merged.get("stage_end", "continue")}
        return 200, {"ok": True, "pid": proc.pid, "mode": _current_mode,
                     "stage_end": _current_mode["stage_end"]}


def _stop():
    global _stopped
    proc = _proc
    if proc is None or proc.poll() is not None:
        return 200, {"ok": False, "error": "当前没有在运行的挂机"}
    _stopped = True
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)
    return 200, {"ok": True}


GROUPS_HEADER = (
    "# 选卡优先级：每个炮台一条线，就是游戏里那几套炮台的完整卡牌名单。\n"
    "# 在启动网页里勾选 / 拖拽卡名排序 / 单卡开关，保存后写回这里。\n"
    "# cards 的顺序就是组内优先级；skip 里的卡保留在名单里但不参与选择。\n"
)
OLD_HEADER = (
    "# 四条炮台线的完整卡牌名单（炮台本体 + 星级 1~3 全部升级卡）。\n"
    "# 升级选卡和精英BOSS选卡共用这一份；越靠前优先级越高，改顺序即改优先级。\n"
)
PRIORITY_HEADER = (
    "# 由上面的 card_groups 自动生成：启用组的卡牌按组顺序拼成优先级表。\n"
    "# 升级选卡 / 精英BOSS选卡 都通过 *card_priority 引用它。\n"
)
MAX_GROUPS = 30
MAX_CARDS = 80
BAD_CHARS = set(",:[]{}#&*!|>'\"%@`\\\n\r\t")


def _priority_state():
    try:
        cfg, _ = load_config(CONFIG)
    except Exception as exc:
        return 500, {"ok": False, "error": f"读取配置失败：{exc}"}

    raw_groups = cfg.get("card_groups")
    groups = []
    if isinstance(raw_groups, list):
        for item in raw_groups:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            cards = [str(c).strip() for c in (item.get("cards") or []) if str(c).strip()]
            skip = [str(c).strip() for c in (item.get("skip") or []) if str(c).strip()]
            groups.append({
                "name": name,
                "enabled": bool(item.get("enabled", True)),
                "cards": cards,
                "skip": [c for c in skip if c in cards],
            })
    return 200, {
        "ok": True,
        "groups": groups,
        "priority": [str(c) for c in (cfg.get("card_priority") or [])],
        "running": _running(),
    }


def _validate_groups(payload):
    raw_groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("没有收到分组数据")
    if len(raw_groups) > MAX_GROUPS:
        raise ValueError(f"分组最多 {MAX_GROUPS} 组")

    groups = []
    seen = set()
    for index, item in enumerate(raw_groups, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 组格式不对")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"第 {index} 组没有名字")
        if len(name) > 24:
            raise ValueError(f"分组名太长：{name}")
        if name in seen:
            raise ValueError(f"分组名重复：{name}")
        if any(ch in BAD_CHARS for ch in name):
            raise ValueError(f"分组名里有特殊字符：{name}")
        seen.add(name)

        raw_cards = item.get("cards")
        if not isinstance(raw_cards, list) or not raw_cards:
            raise ValueError(f"分组「{name}」没有卡牌")
        if len(raw_cards) > MAX_CARDS:
            raise ValueError(f"分组「{name}」的卡牌最多 {MAX_CARDS} 张")
        cards = []
        for raw in raw_cards:
            card = str(raw).strip()
            if not card:
                raise ValueError(f"分组「{name}」里有空卡名")
            if len(card) > 24:
                raise ValueError(f"卡名太长：{card}")
            if any(ch in BAD_CHARS for ch in card):
                raise ValueError(f"卡名里有特殊字符：{card}")
            if card in cards:
                raise ValueError(f"分组「{name}」里有重复卡名：{card}")
            cards.append(card)

        raw_skip = item.get("skip") or []
        if not isinstance(raw_skip, list):
            raise ValueError(f"分组「{name}」的 skip 必须是列表")
        skip = []
        for raw in raw_skip:
            card = str(raw).strip()
            if card in cards and card not in skip:
                skip.append(card)

        groups.append({
            "name": name,
            "enabled": bool(item.get("enabled", True)),
            "cards": cards,
            "skip": skip,
        })

    flat = []
    for group in groups:
        if not group["enabled"]:
            continue
        for card in group["cards"]:
            if card not in group["skip"] and card not in flat:
                flat.append(card)
    if not flat:
        raise ValueError("至少要启用一组并留一张卡，不然升级时没得选了")
    return groups, flat


def _render_priority(groups):
    group_lines = [GROUPS_HEADER.rstrip("\n"), "card_groups:"]
    for group in groups:
        group_lines.append(f"  - name: {group['name']}")
        group_lines.append(f"    enabled: {'true' if group['enabled'] else 'false'}")
        group_lines.append(f"    cards: [{', '.join(group['cards'])}]")
        group_lines.append(f"    skip: [{', '.join(group['skip'])}]")

    priority_lines = [PRIORITY_HEADER.rstrip("\n"), "card_priority: &card_priority"]
    for group in groups:
        if not group["enabled"]:
            continue
        cards = [c for c in group["cards"] if c not in group["skip"]]
        if not cards:
            continue
        priority_lines.append(f"  # {group['name']}")
        priority_lines.extend(f"  - {card}" for card in cards)
    return group_lines, priority_lines


def _find_block(lines, prefix):
    for start, line in enumerate(lines):
        if line.startswith(prefix):
            end = start + 1
            while end < len(lines):
                current = lines[end]
                if current.strip() and not current.startswith((" ", "\t", "#", "-")):
                    break
                end += 1
            return start, end
    return None


def _write_priority(groups, flat):
    with _save_lock:
        return _write_priority_file(groups, flat)


def _write_priority_file(groups, flat):
    text = CONFIG.read_text(encoding="utf-8")
    if OLD_HEADER in text:
        text = text.replace(OLD_HEADER, GROUPS_HEADER)
    while GROUPS_HEADER + GROUPS_HEADER in text:
        text = text.replace(GROUPS_HEADER + GROUPS_HEADER, GROUPS_HEADER)

    lines = text.splitlines()
    group_lines, priority_lines = _render_priority(groups)

    group_span = _find_block(lines, "card_groups:")
    priority_span = _find_block(lines, "card_priority:")
    if group_span:
        header = GROUPS_HEADER.rstrip("\n").splitlines()
        start, end = group_span
        if start >= len(header) and lines[start - len(header):start] == header:
            group_span = (start - len(header), end)

    if group_span and priority_span:
        lines[priority_span[0]:priority_span[1]] = priority_lines + [""]
        lines[group_span[0]:group_span[1]] = group_lines + [""]
    elif priority_span:
        start, end = priority_span
        lines[start:end] = group_lines + [""] + priority_lines + [""]
    else:
        at = next((i for i, line in enumerate(lines) if line.startswith("rules:")), None)
        if at is None:
            raise ValueError("config.yaml 里找不到 rules:，没法写入优先级")
        if group_span:
            lines[at:at] = priority_lines + [""]
        else:
            lines[at:at] = group_lines + [""] + priority_lines + [""]

    new_text = "\n".join(lines).rstrip("\n") + "\n"
    cfg = yaml.safe_load(new_text)
    if not isinstance(cfg, dict) or [str(c) for c in (cfg.get("card_priority") or [])] != flat:
        raise ValueError("写入后校验失败，配置文件没有改动")
    rules_updated = []
    for rule in cfg.get("rules") or []:
        if isinstance(rule, dict) and rule.get("action") == "pick_card":
            if [str(c) for c in (rule.get("priority") or [])] != flat:
                raise ValueError(f"规则「{rule.get('name')}」的 priority 没有跟着更新")
            rules_updated.append(rule.get("name"))

    backup = CONFIG.with_name("config.yaml.bak")
    backup.write_bytes(CONFIG.read_bytes())
    fd, tmp = tempfile.mkstemp(dir=str(BASE), prefix=".config-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_text)
        os.replace(tmp, CONFIG)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return {
        "ok": True,
        "groups": len(groups),
        "priority_count": len(flat),
        "rules": rules_updated,
        "running": _running(),
        "backup": backup.name,
    }


def _save_priority(payload):
    try:
        groups, flat = _validate_groups(payload)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    try:
        result = _write_priority(groups, flat)
    except Exception as exc:
        return 500, {"ok": False, "error": f"保存失败：{exc}"}
    return 200, result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            except OSError as exc:
                self._json(500, {"ok": False, "error": f"页面文件读取失败：{exc}"})
        elif self.path == "/api/status":
            self._json(200, _status())
        elif self.path == "/api/priority":
            code, payload = _priority_state()
            self._json(code, payload)
        elif self.path == "/api/modes":
            code, payload = _modes_state()
            self._json(code, payload)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path == "/api/start":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            payload = {}
            if length > 0:
                if length > 20_000:
                    self._json(400, {"ok": False, "error": "请求内容太大"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._json(400, {"ok": False, "error": f"提交的数据不是合法 JSON：{exc}"})
                    return
            mode_id = payload.get("mode") if isinstance(payload, dict) else None
            stage_end = payload.get("stage_end") if isinstance(payload, dict) else None
            code, result = _start(
                str(mode_id) if mode_id else None,
                str(stage_end) if stage_end else None,
            )
            self._json(code, result)
        elif self.path == "/api/stop":
            code, payload = _stop()
            self._json(code, payload)
        elif self.path == "/api/priority":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 200_000:
                self._json(400, {"ok": False, "error": "请求内容为空或太大"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json(400, {"ok": False, "error": f"提交的数据不是合法 JSON：{exc}"})
                return
            code, result = _save_priority(payload)
            self._json(code, result)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt, *args):
        pass


def _pick_port(preferred):
    for port in range(preferred, preferred + 30):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            return server, port
        except OSError:
            continue
    raise SystemExit("8765-8794 端口都被占用了")


def _is_launcher(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.8) as resp:
            return resp.status == 200 and b'"running"' in resp.read()
    except Exception:
        return False


def _selftest():
    import cv2
    import mss
    import numpy as np
    import win32gui
    import win32job
    import winocr
    from PIL import Image
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine

    print("python:", sys.version.split()[0])
    print("frozen:", FROZEN)
    print("bundle:", BUNDLE)
    print("data:", BASE)
    print("cv2:", cv2.__version__, "numpy:", np.__version__, "mss:", mss.__version__)
    print("config:", CONFIG.is_file(), "templates:", len(list((BASE / "templates").glob("*.png"))))
    print("ocr zh-Hans:", OcrEngine.is_language_supported(Language("zh-Hans")))
    img = Image.new("RGB", (40, 20), "white")
    print("winocr:", type(winocr.recognize_pil_sync(img, "zh-Hans")).__name__)
    print("win32gui/win32job: ok")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(TITLE)
        except Exception:
            pass
    if argv and argv[0] == "--selftest":
        return _selftest()
    if argv and argv[0] == "--run-bot":
        os.chdir(BASE)
        if not sys.stdout.isatty():
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        import main as bot
        return bot.main(argv[1:])

    parser = argparse.ArgumentParser(description="小游戏挂机助手启动网页")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    if _is_launcher(args.port):
        url = f"http://127.0.0.1:{args.port}/"
        print(f"启动网页已经在运行：{url}", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        return

    server, port = _pick_port(args.port)
    threading.Thread(target=_mem_loop, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"打开 {url} 点卡牌启动挂机；关闭这个窗口即停止网页服务。", flush=True)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if _running():
            _stop()


if __name__ == "__main__":
    raise SystemExit(main())
