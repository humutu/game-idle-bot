# 小游戏挂机助手

Windows 上的小游戏自动化小工具：**窗口截图 + 模板匹配 + Windows OCR + 模拟点击**。
不改游戏、不读内存、不注入进程，只做普通用户能看到和点到的事。

- 后台模式：点击通过窗口消息发送，不抢鼠标键盘，挂机时可以正常用电脑。
- 规则引擎：画面里出现哪个模板，就执行哪个动作（点击 / 按键 / 选卡 / 连抽 / 等待）。
- 自动选卡：OCR 读三张卡的名字，按你配置的优先级选；识别不了就点兜底位置。
- 本地网页控制台：切换模式、设置关卡结束行为、拖拽调整选卡优先级、看日志。

> **使用前必读**：[DISCLAIMER.md](DISCLAIMER.md)。自动化可能违反游戏用户协议，账号风险自担；本项目与任何游戏官方无关。

> **仓库不含模板图。** 模板必须从你自己的游戏画面里裁。
> 第一次使用请先按 [templates/README.md](templates/README.md) 和 [docs/模板制作.md](docs/模板制作.md) 制作模板（默认规则需要 11 张；启用「额外机会-获得」时再裁第 12 张），否则启动时会提示找不到模板。
> 裁剪工具会在模板旁边写一个同名 `.json` 记录当时的窗口宽度，之后改窗口大小会自动按比例缩放模板。

---

## 功能

| 功能 | 说明 |
| --- | --- |
| 模板匹配 | `cv2.matchTemplate`，阈值、冷却时间按规则单独配置 |
| 窗口自适应 | 模板记录裁剪时的参考窗口宽度，窗口缩放后按比例自动缩放模板，不必重裁 |
| 后台点击 | `PostMessage` 发送鼠标消息，窗口可以被遮挡（不能最小化、不能完全盖住） |
| 自动选卡 | 整帧 OCR 找到一排三张卡的卡名，按优先级表选择；支持分组启用/停用、单卡停用 |
| 额外机会连抽 | 动态找卡背，点一张重新截图再找，最多 `max_picks` 张 |
| 多挂机模式 | 在公共规则上增删规则，示例给了「战役」「全球联防」两种 |
| 关卡结束策略 | 继续下一关 / 打完停止 / 打完暂停 |
| 网页控制台 | 本地 `127.0.0.1`，看日志、停止、改优先级、显示内存占用 |
| 安全限制 | 默认最长 4 小时；F8 暂停/继续，F9 停止；日志自动轮转 |

## 环境要求

- Windows 10 / 11，64 位
- Python 3.12（3.10+ 理论可用，本项目只在 3.12 上验证过）
- 游戏窗口：窗口化运行，**不要最小化**，不要被其它窗口完全遮挡
- 窗口大小可以变：模板会按裁剪时的参考宽度自动缩放；但差距过大或 DPI 变化后，建议重跑 `probe` 确认分数
- 显示缩放（DPI）保持固定，例如一直是 100% 或一直是 125%
- 首次安装依赖需要联网

依赖见 [requirements.txt](requirements.txt)：`pywin32`、`mss`、`opencv-python`、`numpy`、`pyyaml`、`winocr`、`pillow`。

## 快速开始

### 1. 安装运行环境（只做一次）

双击 `首次安装.bat`，它会创建 `.venv` 并安装依赖。也可以手动：

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 打开游戏，固定窗口

窗口化，调到一个顺手的尺寸（不要最大化）。后续改大小也能用：裁剪工具会记下当时的窗口宽度，运行时按比例缩放模板。

先确认能找到窗口：

```bat
.venv\Scripts\python.exe -m gamebot.tools windows --filter 小游戏
```

标题不是「小游戏」的话，改 `config.yaml` 第一行的 `window`（支持正则）。

### 3. 制作模板

仓库不附带游戏截图。按 [templates/README.md](templates/README.md) 的清单，逐个界面执行：

```bat
.venv\Scripts\python.exe -m gamebot.tools snapshot --window 小游戏
.venv\Scripts\python.exe -m gamebot.tools crop --name levelup_banner
```

详细步骤和每个界面裁什么，见 [docs/模板制作.md](docs/模板制作.md)。

### 4. 校准

站在目标界面运行：

```bat
.venv\Scripts\python.exe -m gamebot.tools probe
```

会打印每条规则的分数和阈值，并生成带标注框的 `captures\probe_*.png`。
分数不够就重裁模板或微调阈值，别急着正式跑。

### 5. 启动

网页控制台（推荐）：

```bat
启动挂机网页.bat
```

自动打开 <http://127.0.0.1:8765/>，点页面上的卡牌开始挂机。

命令行直接跑：

```bat
.venv\Scripts\python.exe main.py
.venv\Scripts\python.exe main.py --dry-run      # 只看识别，不点击
```

游戏里按 **F8** 暂停/继续，**F9** 停止。

---

## 命令行

### main.py

| 参数 | 说明 |
| --- | --- |
| `--config PATH` | 配置文件，默认 `config.yaml` |
| `--window 正则` | 覆盖配置里的窗口标题 |
| `--hwnd 0x...` | 直接指定窗口句柄（标题匹配到多个窗口时用） |
| `--max-min N` | 覆盖最长运行分钟数 |
| `--dry-run` | 只识别、不点击 |
| `--debug` | 每次命中保存一张标注截图到 `captures/debug` |
| `--background` | 强制后台模式 |
| `--no-foreground-check` | 不要求窗口在前台（不推荐，可能点错窗口） |

日志写在 `logs/run_*.log`，默认只保留最近 20 份。

### gamebot.tools

| 命令 | 用途 |
| --- | --- |
| `windows` | 列出可见窗口（`--filter 关键词`） |
| `snapshot` | 截取窗口客户区到 `captures/frame_*.png` |
| `crop` | 从截图裁模板到 `templates/*.png`（`--image`、`--name`） |
| `probe` | 对当前画面跑一遍全部规则并打分，生成标注图 |

### web.py

| 参数 | 说明 |
| --- | --- |
| `--port 8765` | 网页端口；被占用时会自动换一个 |
| `--no-open` | 不自动打开浏览器 |

网页只监听本机回环地址，不对外网开放。

## 网页控制台

- **挂机模式**：切换 `config.yaml` 里 `modes` 定义的模式（示例是「战役模式」「全球联防」）。
- **这关结束后**：继续下一关 / 打完停止 / 打完暂停，对应 `stage_end`。
- **选卡优先级**：按组展示所有卡名。组越靠前越优先，组内从左到右是顺序；拖拽卡名排序，勾掉组表示整套不参与，点卡名单独停用某张卡。点「保存优先级」写回 `config.yaml`，**下次启动生效**。
- **运行日志**：显示最近 40 行，同时落盘到 `logs/`。
- **停止挂机**：结束当前挂机进程。
- **内存**：显示启动器和挂机进程的内存占用。

## 配置说明

完整示例见 [config.yaml](config.yaml)，注释里写了每一项的作用。核心结构：

```yaml
window: "小游戏"        # 窗口标题正则
mode: rules             # rules = 规则引擎；sequence = 固定坐标宏
background: true        # 后台模式（PostMessage 点击）

defaults:
  interval: 0.6         # 没命中时多久看一帧
  click_delay: 0.5      # 点击后多久再识别
  threshold: 0.85       # 默认模板阈值
  jitter_px: 2          # 点击随机偏移，避免每次同一个像素
  stall_warn_sec: 60    # 连续多少秒没命中就截图告警
  ui_width: 545         # 老模板没带参考宽度时，按这个窗口宽度兜底缩放

max_runtime_min: 240
stage_end: continue     # continue / stop / pause
log_keep: 20
```

### modes（挂机模式）

每个模式在公共 `rules` 的基础上增删规则：

```yaml
modes:
  - id: campaign
    name: 战役模式
    desc: 从当前关卡继续打
  - id: global
    name: 全球联防
    desc: 自动进入大厅并开始挑战
    remove_rules: [主界面-开始战斗]
    add_rules:
      - name: 全球联防-进入
        template: templates/global_enter.png
        action: click
        point_rel: [2.41, 4.63]
        threshold: 0.95
        cooldown: 5
```

模式还可以覆盖 `window`、`background`、`max_runtime_min` 和 `defaults`。

### rules（规则）

一条规则 = 一个模板 + 一个动作。每帧按顺序匹配，第一条达到阈值且不在冷却中的规则被执行。

| action | 作用 | 关键字段 |
| --- | --- | --- |
| `click` | 点击模板中心 | `point`（窗口比例坐标）或 `point_rel`（相对模板左上角的倍数）可覆盖点击位置 |
| `key` | 发送按键 | `key`；后台模式下会跳过（按键会落到前台窗口） |
| `wait` | 只识别，不做动作 | 适合「停在这里」的界面 |
| `manual` | 暂停自动操作，等你手动选 | 模板消失后自动继续；`timeout` 超时后按 `point` 兜底点一下 |
| `pick_card` | OCR 读三张卡名并按优先级点击 | `slots`、`title_boxes`、`priority`、`fallback` |
| `draw_cards` | 额外机会：动态找卡背连续点 | `card_template`、`card_threshold`、`max_picks`、`pick_delay` |

通用字段：

- `threshold`：模板相似度阈值，0–1，越接近 1 越严格。
- `cooldown`：同一条规则两次触发之间的最短间隔（秒）。
- `timeout`：`manual` 等待上限；其它动作暂未使用。
- `stage_end: true`：出现该界面算「一关结束」，配合 `stage_end` 决定继续/停止/暂停。

### 选卡优先级

`card_groups` 按组维护卡名，`card_priority` 是所有启用组拼成的优先级表（YAML 锚点 `*card_priority`）。可以手改，也可以在网页里编辑后保存。

`pick_card` 的选卡逻辑：

1. 用 OCR 读三个 `title_boxes` 里的卡名；
2. 从 `priority` 顶部往下找第一张出现的卡；
3. 三张都不在名单里，就点 `fallback`（`left` / `middle` / `right` 或序号）。

### 固定坐标宏（可选）

不想用模板时，可以把 `mode` 改成 `sequence`：

```yaml
mode: sequence
steps:
  - click: [0.50, 0.88]
  - wait: 2.0
  - key: space
  - wait: 3.0
```

它不看画面，切到别的界面会瞎点，只适合非常固定的流程。

## 运行机制与限制

- **截图**：前台模式用 `mss` 抓客户区；后台模式用 `PrintWindow`（`PW_RENDERFULLCONTENT`）。
- **点击**：前台模式移动系统鼠标；后台模式向顶层窗口 `PostMessage` 鼠标消息，不移动鼠标。
- **按键**：仅前台模式有效。
- **窗口状态**：最小化时无法截图；被完全遮挡时部分客户端会输出黑帧或停止刷新。
- **匹配**：模板按「当前窗口宽度 ÷ 参考宽度」缩放后再匹配；参考宽度来自 `crop` 写的同名 `.json`，老模板用 `defaults.ui_width` 兜底。卡背查找本身也是多尺度的。DPI 或游戏内分辨率变化仍会影响分数。
- **安全上限**：默认 240 分钟后自动停止；日志保留 20 份。
- **联网**：没有联网功能，所有数据都在本机。

## 常见问题

**启动报「模板文件不存在」**
仓库不带模板。先按 `templates/README.md` 裁剪模板（默认 11 张），或者把用不到的规则从 `config.yaml` 删掉。

**提示找不到游戏窗口**
确认游戏已打开；运行 `python -m gamebot.tools windows --filter 小游戏` 看真实标题，再改 `window`。
标题匹配到多个窗口时，用 `--hwnd 0x...` 指定。

**脚本跑起来但一直没动作**
先看它当前在什么界面。对局进行中本来就没有可点的规则，这是正常的；只有横幅/按钮出现时才动作。
如果停在某个界面超过 60 秒，会输出「已经 60 秒没有命中任何规则」，并把画面存到 `captures\stall_*.png`，拿这张图和模板对比就能看出问题。

**识别不准、点错位置**
先跑 `probe`，看打印的「缩放」是不是接近 1。窗口宽度和裁剪时差很多时，模板被插值缩放过，分数会略降；把阈值下调 0.02–0.03，或者按当前尺寸重裁最关键的几张模板。
另外常见原因是模板太「纯色」，重裁时把文字或图标框进去。

**改了窗口大小，需要重裁模板吗**
一般不用。`crop` 会在模板旁边写 `同名.json`（`{"ref_width": 裁剪时窗口宽度}`），运行时自动按比例缩放。宽度变化特别大（例如 400 和 900 之间来回换）会因为插值变模糊，建议重裁；DPI 缩放变了也建议重裁。老模板没有 `.json` 时按 `defaults.ui_width` 处理。

**额外机会界面停住不动**
示例配置默认禁用「额外机会-获得」，因为该按钮通常会触发激励视频广告。需要自动点就在 `config.yaml` 里把它改成 `enabled: true`，后果自负。

**OCR 认不出卡名**
Windows 设置 → 时间和语言 → 语言 → 中文(简体) → 可选功能，安装 OCR 语言包。
OCR 失败时程序会自动重试几次，仍失败就点兜底位置。

**选卡没按优先级，总是点中间那张**
看日志里的「识别：…」：如果三个位置都是空的或读到炮台名，说明固定标题框在那个窗口尺寸下跑偏了。
当前版本会先用整帧 OCR 找出一排三张卡的标题，再按优先级选，不依赖标题框；只有整帧也找不到时才退回固定框。
如果仍然不对，把当时的界面截图、日志里的「识别」行和窗口宽度发出来。

**点了保存提示「保存失败」**
网页服务进程被关掉了。关掉旧页面，重新运行 `启动挂机网页.bat`。

**后台点击没反应**
部分客户端不接受 `PostMessage` 的点击；被完全遮挡时也可能不刷新。让窗口保持可见（可以被部分盖住），或者用前台模式。

## 目录结构

```
game-idle-bot/
├─ main.py                 # 命令行入口
├─ web.py                  # 本地网页控制台（启动 main.py）
├─ launcher.html           # 网页界面
├─ config.yaml             # 配置：窗口、模式、规则、选卡优先级
├─ requirements.txt
├─ gamebot/
│  ├─ engine.py            # 规则引擎：匹配、点击、选卡、连抽
│  ├─ vision.py            # 模板匹配
│  ├─ capture.py           # 窗口截图（mss / PrintWindow）
│  ├─ inputs.py            # 鼠标、按键、后台消息
│  ├─ winutil.py           # 窗口查找、DPI、客户区坐标
│  ├─ cards.py             # OCR 读卡名 + 优先级选择
│  └─ tools.py             # windows / snapshot / crop / probe
├─ templates/              # 你的模板图（默认不进 Git）
│  ├─ *.json               # crop 自动写的参考窗口宽度，用于窗口缩放
│  └─ README.md            # 12 个模板的清单
├─ docs/模板制作.md
├─ 首次安装.bat
├─ 启动挂机网页.bat
└─ LICENSE / DISCLAIMER.md
```

## 上传到 GitHub

仓库里没有游戏截图、日志、视频和 exe，`.gitignore` 已经排除了这些内容。确认没有个人信息后：

```bat
git init
git add .
git commit -m "init: 小游戏挂机助手"
git branch -M main
git remote add origin https://github.com/<你的账号>/<仓库名>.git
git push -u origin main
```

建议仓库名用中性描述（例如 `game-idle-bot`），不要带游戏商标。README 已经说明本项目与任何游戏官方无关。

## 许可证

代码使用 [MIT License](LICENSE)。游戏内容、商标归各自权利人所有；本仓库不包含游戏素材。

使用前请阅读 [DISCLAIMER.md](DISCLAIMER.md)。