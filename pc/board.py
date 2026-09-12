# -*- coding: utf-8 -*-
"""
教室 PC 端桌面留言板（P1.8 壁纸采样磨砂玻璃版）：
- 桌面层停靠：普通顶层窗口 + WS_EX_TOOLWINDOW（Win+D 最小化风暴豁免工具窗口），
  每秒把 z 序停靠回 Progman（桌面）正上方一格 = 低于所有普通应用、高于桌面；
  Tk 窗口 SetParent 进 Progman/WorkerW 当子窗口 DWM 不合成像素（实测隐形），故不挂子窗口
- 液态玻璃（Win10/11 通用，不依赖 DWM 特效）：启动时采样窗口区域桌面内容，高斯模糊+暗化=磨砂底图；
  圆角外画壁纸原样=视觉真透明圆角；圆角外四角 WM_NCHITTEST 点击穿透
- 隐私保护：卡片默认只显示 姓名 + 红点(未读数) + "点击查看留言"，不显示任何内容
- 点击卡片 -> 展开留言内容，同时清零红点并发布已读回执（retained）；再点收起
- 新留言到达时自动收起已展开的卡片，内容不会直接暴露在公共屏幕上
- 老师大喇叭消息转发本机 ClassIsland（IslandMQ 插件 ZMQ/HTTP）全屏醒目通知

用法：
    python pc/board.py                  # 正常模式：挂桌面层，点击卡片展开+回执，Esc 退出
    python pc/board.py --no-pin         # 不挂到桌面层，普通无边框窗口（调试用）
    python pc/board.py --topmost        # 置顶显示（自动化测试用）
    python pc/board.py --autoack        # 收到留言 3 秒后自动发回执（自动化测试用）
    python pc/board.py --expandall      # 测试模式：所有卡片默认展开（截图验证用）
    python pc/board.py --duration N     # N 秒后自动退出（自动化测试用）
    python pc/board.py --pos X Y        # 窗口放在逻辑坐标 (X,Y)（默认右上角；被桌面软件挡住时换位置）
    python pc/board.py --shot PATH      # 收到第一条留言 6 秒后截取窗口区域
"""
import base64
import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import urllib.request
from ctypes import WINFUNCTYPE, byref, windll
from ctypes import wintypes
from datetime import datetime

import paho.mqtt.client as mqtt
import tkinter as tk

from PIL import Image, ImageDraw, ImageFilter, ImageGrab, ImageTk

if getattr(sys, "frozen", False):
    ROOT_DIR = os.path.dirname(sys.executable)   # PyInstaller 打包：config/logs 放 exe 旁（可写）
else:
    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "students.json")

# ---------- 版本 & 自动更新 ----------
# 教室端是装在碰不到的教室电脑上的桌面 exe，无法像网页那样"重新上传即升级"。
# 故做全自动更新：每次开机先拉 update.json（GitHub 仓库），比对版本号，有新版则
# 下载 release zip、校验 sha256、自替换 exe、重启。config/*.json 不随更新覆盖，班级配置保留。
VERSION = "1.0.0"
UPDATE_STATE_PATH = os.path.join(ROOT_DIR, "config", "update_state.json")
# 更新源：默认指向 GitHub 仓库 main 分支的 update.json（由 Actions 自动推回 main）。
# 国内访问慢，客户端下载会套 gh-proxy 前缀；也可用 config/update_url.json 覆盖。
UPDATE_URL = "https://raw.githubusercontent.com/dgkad/hsboard-updates/main/update.json"
# 下载加速（国内可达）：把 release zip 地址前面拼 gh-proxy 前缀（按 owner 规则）。
GH_PROXY_PREFIX = "https://gh-proxy.com/"


def _load_update_url_override():
    """允许用 config/update_url.json 覆盖 UPDATE_URL（免改代码即可换仓库）。
    形如 {"url": "https://raw.githubusercontent.com/OWNER/REPO/main/update.json"}。"""
    p = os.path.join(ROOT_DIR, "config", "update_url.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            u = json.load(f).get("url", "").strip()
            if u:
                return u
    except Exception:
        pass
    return UPDATE_URL


def read_update_state():
    """读取本地更新状态（上次检查时间/已知最新版），防同一版本反复弹提示。"""
    st = {"last_check": 0, "last_version": "", "pending_restart": False, "note": ""}
    try:
        with open(UPDATE_STATE_PATH, "r", encoding="utf-8") as f:
            st.update(json.load(f))
    except Exception:
        pass
    return st


def save_update_state(st):
    try:
        os.makedirs(os.path.dirname(UPDATE_STATE_PATH), exist_ok=True)
        with open(UPDATE_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[更新] 状态保存失败: {e}")


def _ver_tuple(v):
    """版本号比较用：'1.2.3' -> (1,2,3)；非规范串按 0 处理。"""
    out = []
    for part in str(v).split("."):
        try:
            out.append(int(part))
        except Exception:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])

# ---------- Broker（EMQX Cloud Serverless，MQTT over TLS + 账号认证）----------
# 凭据混淆 v2（_ver=2）：host/mqtt_port/wss_url/username/password 字段值为
# XOR(固定密钥)+base64 后的字符串，防止 exe 交付后用记事本直接翻看密码/端口。
# 解码失败一律回退按原值用（兼容 v1 明文；文件被改坏也不至于起不来）。
_BROKER_KEY = b"HsBoard@2026!x"


def _bro_decode(s):
    raw = base64.b64decode(s.encode("ascii"))
    return bytes(c ^ _BROKER_KEY[i % len(_BROKER_KEY)]
                 for i, c in enumerate(raw)).decode("utf-8")


def _bro_field(v, key):
    x = v.get(key)
    if v.get("_ver") == 2 and isinstance(x, str) and x:
        try:
            return _bro_decode(x)
        except Exception:
            return x
    return x


with open(os.path.join(ROOT_DIR, "config", "broker.json"), "r", encoding="utf-8") as f:
    _broker = json.load(f)
BROKER = _bro_field(_broker, "host") or ""
PORT = int(_bro_field(_broker, "mqtt_port") or 8883)
MQTT_USER = _bro_field(_broker, "username") or ""
MQTT_PASS = _bro_field(_broker, "password") or ""
# （TOPIC 四常量已移到下方班级解析处：hsdemo/{class_id}/msg|ack|hist/...，多班共用一个部署）

# ClassIsland 的 IslandMQ 插件接口（教室 PC 本机）。
# 老师"紧急通知"通过它触发 ClassIsland 全屏醒目提示；未安装/未开启时自动降级（仅日志）。
# 通道优先级：ZeroMQ REQ(5555，插件默认开启) -> HTTP(8080，需在插件设置里启用)。
CLASSISLAND_ZMQ = os.environ.get("CLASSISLAND_ZMQ", "tcp://127.0.0.1:5555")
CLASSISLAND_API = os.environ.get("CLASSISLAND_API", "http://127.0.0.1:8080/api")

autoack = "--autoack" in sys.argv
duration = None
if "--duration" in sys.argv:
    duration = int(sys.argv[sys.argv.index("--duration") + 1])
pin_enabled = "--no-pin" not in sys.argv
expand_all = "--expandall" in sys.argv           # 测试模式：全部卡片默认展开
use_acrylic = "--acrylic" in sys.argv            # 亚克力特效（挂桌面层时默认关）

# 窗口位置（逻辑坐标，默认屏幕右上角；右上角被桌面美化软件遮挡时可用 --pos X Y 换位置）
POS = None
if "--pos" in sys.argv:
    _i = sys.argv.index("--pos")
    POS = (int(sys.argv[_i + 1]), int(sys.argv[_i + 2]))

CLI_SCALE = None
if "--scale" in sys.argv:                          # 测试用：--scale 0.25 / 0.5 / 0.75 / 1.0
    _i = sys.argv.index("--scale")
    CLI_SCALE = float(sys.argv[_i + 1])

# 自动截图（自动化测试用）：收到第一条留言 6 秒后截取自己的窗口区域
shot_path = [None]
shot_done = [False]
if "--shot" in sys.argv:
    _i = sys.argv.index("--shot")
    shot_path[0] = sys.argv[_i + 1]

# -w 无控制台模式（PyInstaller）下 print 目标为 None，重定向到运行日志（行缓冲实时可查）
if getattr(sys, "frozen", False) and (sys.stdout is None or sys.stderr is None):
    os.makedirs(os.path.join(ROOT_DIR, "logs"), exist_ok=True)
    _runlog = open(os.path.join(ROOT_DIR, "logs", "board_run.log"), "a",
                   encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = _runlog
    sys.stderr = _runlog
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------- 日志：全部输出统一精确时间戳（frozen 时随 stdout 落入 logs/board_run.log）----------
_orig_print = print


def print(*args, **kwargs):                        # noqa: A001  覆盖内建 print，调用点零改动
    _orig_print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}]", *args, **kwargs)


def evt(tag, detail):
    """生产排查用结构化事件日志：[EVT][tag] detail。
    关键事件（连接/断连/更新/重置/崩溃前兆/降级）都走这里，grep '[EVT]' 即可定位一周内的异常。"""
    _orig_print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [EVT][{tag}] {detail}")

# 原生回调崩溃(0xC0000005/0xC000041D)时把 Python 调用栈写进日志，便于定位
import faulthandler
os.makedirs(os.path.join(ROOT_DIR, "logs"), exist_ok=True)
_crash_log = open(os.path.join(ROOT_DIR, "logs", "board_crash.log"), "a", encoding="utf-8")
faulthandler.enable(_crash_log)

# ---------- 设置（右键菜单可改，持久化到 config/board_settings.json）----------
SETTINGS_PATH = os.path.join(ROOT_DIR, "config", "board_settings.json")
settings = {"pos": None, "max_lines": 3, "scale": 1.0, "density": "cozy",
            "class": None}                     # pos=逻辑坐标[x,y]；max_lines: 1/2/3，0=完整；scale: 0.25~1.0；density: cozy/compact/grid；class: 班级 id（多班发行版）
try:
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        settings.update(json.load(f))
except Exception:
    pass


def save_settings():
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[设置] 保存失败: {e}")


if POS is None and settings.get("pos"):
    POS = tuple(settings["pos"])               # 菜单里调过的位置优先于默认右上角

# ---------- 班级解析（多班发行版：一份 exe 三班通用，settings["class"] 持久化）----------
CLASSES_PATH = os.path.join(ROOT_DIR, "config", "classes.json")
STU_DIR = os.path.join(ROOT_DIR, "config", "students")
LEGACY_ROSTER = os.path.join(ROOT_DIR, "config", "students.json")
CLASSES = []
if os.path.exists(CLASSES_PATH):
    try:
        CLASSES = json.load(open(CLASSES_PATH, "r", encoding="utf-8")).get("classes", [])
    except Exception as e:
        print(f"[班级] classes.json 解析失败（{e}），回退旧单文件名单")
if CLASSES:
    CLASS_ID = (os.environ.get("HSBOARD_CLASS")                 # 测试钩子（不落盘）
                or settings.get("class") or CLASSES[0]["id"])
    if CLASS_ID not in {c["id"] for c in CLASSES}:
        print(f"[班级] 设置的班级 {CLASS_ID} 不在清单里，回退 {CLASSES[0]['id']}")
        CLASS_ID = CLASSES[0]["id"]
    CLASS_NAME = next(c["name"] for c in CLASSES if c["id"] == CLASS_ID)
    CONFIG_PATH = os.path.join(STU_DIR, f"{CLASS_ID}.json")
else:                                          # 兼容回退：无 classes.json 时按旧单文件跑
    CLASS_ID, CLASS_NAME = "c1", None
    CONFIG_PATH = LEGACY_ROSTER
settings["class"] = CLASS_ID                   # 运行期记录（仅 set_class 菜单才写盘）
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    roster = json.load(f)
CLASS_NAME = roster.get("class", CLASS_NAME or "班级")
if CLASSES:
    print(f"[班级] 当前班级: {CLASS_NAME}（{CLASS_ID}），名单 {len(roster['students'])} 人")

# TOPIC 前缀带班级段：hsdemo/{class_id}/msg|ack|hist/...（三班共用部署，互不串扰）
BASE_PRE = f"hsdemo/{CLASS_ID}/"
TOPIC_MSG = BASE_PRE + "msg/#"
MSG_PREFIX = BASE_PRE + "msg/"
ACK_PREFIX = BASE_PRE + "ack/"
HIST_PREFIX = BASE_PRE + "hist/"

state = {}                                     # 学号 -> {name, messages, unread}
for stu in roster["students"]:
    state[stu["id"]] = {"name": stu["name"], "messages": [], "unread": 0}

# ---------- 开机前自动更新（教室端 exe 专用）----------
# 教室端装在教学电脑上，无法人工逐个升级。开机先拉 update.json 比对版本，
# 有新版则下载 zip、校验 sha256、自替换 exe、重启进程。config/*.json 不随包覆盖，
# 班级名单/设置/已读回执全部保留。下载走 gh-proxy 加速；任何失败一律静默降级，
# 绝不影响主功能（拉不到更新 = 照常启动旧版）。
def _download_bytes(url, dest_path, timeout=60):
    """流式下载到 dest_path，返回字节数；失败抛异常。"""
    req = urllib.request.Request(url, headers={"User-Agent": "HSBoard-Update/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
    return os.path.getsize(dest_path)


def _sha256_of(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _proxy_download_url(zip_url):
    """把 GitHub 直链套 gh-proxy 前缀（国内可达）；已是 proxy/非 GitHub 则原样返回。"""
    if "gh-proxy.com/" in zip_url or zip_url.startswith(GH_PROXY_PREFIX):
        return zip_url
    if "github.com" in zip_url:
        return GH_PROXY_PREFIX + zip_url
    return zip_url


def _download_with_fallback(zip_url, dest_path, timeout=60):
    """先走 gh-proxy（国内快），失败自动回退 GitHub 直连（双保险）。
    返回实际生效的下载源（'gh-proxy' / 'direct'），供日志排查。"""
    proxy_url = _proxy_download_url(zip_url)
    if proxy_url != zip_url:
        try:
            _download_bytes(proxy_url, dest_path, timeout=timeout)
            return "gh-proxy"
        except Exception as e:
            print(f"[更新] gh-proxy 下载失败（{e.__class__.__name__}），回退直连…")
            evt("UPDATE_DL", f"gh-proxy 失败，回退直连 {zip_url}")
    _download_bytes(zip_url, dest_path, timeout=timeout)
    return "direct"


def _spare_exe_name():
    """更新中转文件名（exe 同级目录，可写）。"""
    base = os.path.basename(sys.executable) if getattr(sys, "frozen", False) else "board_new.exe"
    return "board_update_new.exe"


def do_self_update(new_zip_url, expected_sha):
    """下载新版 zip -> 校验 -> 解压 -> 替换 exe -> 重启。返回 True=已触发重启（进程将退出）。
    非 frozen（开发机直接跑 .py）时只校验下载，不做替换，避免污染源码。"""
    import shutil
    import zipfile
    tmp_dir = os.path.join(ROOT_DIR, "config", ".update_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    zip_path = os.path.join(tmp_dir, "board_new.zip")
    print(f"[更新] 开始下载新版: {new_zip_url}")
    src = _download_with_fallback(new_zip_url, zip_path)
    evt("UPDATE_DL", f"新版下载完成 src={src} {new_zip_url}")
    got = _sha256_of(zip_path)
    if expected_sha and got.lower() != str(expected_sha).lower():
        print(f"[更新] sha256 校验失败（期望 {expected_sha} 实际 {got}），放弃更新")
        try:
            os.remove(zip_path)
        except Exception:
            pass
        return False
    print(f"[更新] 下载完成并通过 sha256 校验: {got[:12]}…")

    extract_dir = os.path.join(tmp_dir, "extracted")
    if os.path.isdir(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    # 找到新 exe（zip 内可能有一层目录包裹；递归找 .exe）
    new_exe = None
    for root_dir, _dirs, files in os.walk(extract_dir):
        for fn in files:
            if fn.lower().endswith(".exe"):
                new_exe = os.path.join(root_dir, fn)
                break
        if new_exe:
            break
    if not new_exe:
        print("[更新] zip 内未找到 exe，放弃自动更新（请人工检查发布包）")
        return False

    if not getattr(sys, "frozen", False):
        print("[更新] 开发机非打包运行，仅验证了下载+解压，不执行替换")
        return False

    cur_exe = os.path.abspath(sys.executable)
    spare = os.path.join(ROOT_DIR, _spare_exe_name())
    # 1) 旧 exe -> 中转名（腾出原路径）
    if os.path.exists(spare):
        os.remove(spare)
    os.replace(cur_exe, spare)
    # 2) 新 exe -> 原路径
    shutil.copy2(new_exe, cur_exe)
    # 3) 启动新进程，再清理中转旧 exe（用新进程继续跑，旧文件此刻可删）
    print(f"[更新] 已替换到 v（{new_exe}），正在重启…")
    try:
        os.startfile(cur_exe)     # Windows：启动新 exe（独立进程）
    except Exception:
        # 兜底：用 subprocess 启动
        import subprocess
        subprocess.Popen([cur_exe])
    # 4) 删除旧 exe（中转名），避免下次再被替换回来
    try:
        os.remove(spare)
    except Exception:
        pass
    return True


def check_for_update_on_startup():
    """开机前更新检查：拉 update.json，比版本；有新版则下载替换并返回 True（进程将重启）。
    全程 try 包裹，网络/格式异常一律静默降级，主程序照常启动。"""
    st = read_update_state()
    url = _load_update_url_override()
    if not url or "OWNER" in url:
        return False                       # 未配置真实更新源（占位），跳过
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HSBoard-Update/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            remote = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[更新] 拉取 update.json 失败（{e.__class__.__name__}），按当前版本启动")
        return False
    remote_ver = str(remote.get("version") or "")
    if _ver_tuple(remote_ver) <= _ver_tuple(VERSION):
        print(f"[更新] 已是最新版 {remote_ver}（本地 {VERSION}）")
        st.update(last_check=int(time.time()), last_version=remote_ver,
                  pending_restart=False, note="")
        save_update_state(st)
        return False
    zip_url = str(remote.get("release_zip") or "").strip()
    if not zip_url:
        print("[更新] 远端未提供 release_zip，跳过")
        return False
    print(f"[更新] 发现新版本 {remote_ver}（当前 {VERSION}）：{remote.get('changelog','')}")
    try:
        need_restart = do_self_update(zip_url, remote.get("sha256"))
    except Exception as e:
        print(f"[更新] 自动更新失败（{e.__class__.__name__}），按旧版启动：{e}")
        need_restart = False
    st.update(last_check=int(time.time()), last_version=remote_ver,
              pending_restart=bool(need_restart),
              note=remote.get("changelog", ""))
    save_update_state(st)
    return need_restart


# 开机前检查一次；返回 True 表示已触发替换+重启（下方 mainloop 不会真正跑起来）
_UPDATE_TRIGGERED = False
if os.environ.get("HSBOARD_DEMO_STATE") not in ("1", "bottom"):
    _UPDATE_TRIGGERED = check_for_update_on_startup()
    if _UPDATE_TRIGGERED:
        print("[更新] 自替换完成，即将重启进入新版")
        evt("UPDATE", f"自替换完成并触发重启 本地={VERSION}")

# ---------- 单实例保护 ----------
# 固定 client_id 重复启动会被 Broker 互踢，且旧实例窗口残留在桌面同位置
# （表现为圆角外露出深色残块、显示旧内容的"重影"），故直接禁止双开。
# 锁名带班级：同机不同班可并存，同班双开仍被拒。
_hmutex = windll.kernel32.CreateMutexW(None, False, f"hs-poc-board-{CLASS_ID}")
if windll.kernel32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
    print("[启动] 留言板已在运行，本实例退出（请勿重复开启）")
    sys.exit(1)

if os.environ.get("HSBOARD_DEMO_STATE") in ("1", "bottom"):
    # 本地演示数据（布局/界面自动化验证用）：不连 Broker、不收发消息。
    # 8 位未读（时间从近到远）+ 8 位已读，足以验证分区、位置提示与滚动条断言。
    # =bottom 时初始滚到底，验证"上方还有 N 位同学"提示与滚动条 thumb 位置。
    _now = int(time.time() * 1000)
    for _i, (_nm, _ago, _un) in enumerate([
            ("陈小明", 2, 2), ("刘雨桐", 5, 0), ("王子豪", 9, 1), ("赵一诺", 13, 0),
            ("钱思远", 21, 3), ("孙嘉懿", 34, 0), ("周芷若", 52, 1), ("吴思琪", 70, 0),
            ("郑楚仪", 95, 2), ("王振宇", 120, 0), ("冯雅婷", 150, 1), ("褚浩然", 180, 0),
            ("卫诗涵", 220, 4), ("蒋文轩", 260, 0), ("沈梦琪", 300, 1), ("韩明轩", 360, 0)]):
        _sid = "demo%02d" % _i
        _n = _un if _un > 0 else 1
        state[_sid] = {
            "name": _nm,
            "messages": [{"msgId": "demo%02d-%d" % (_i, j),
                          "text": "演示留言 %d：用于布局验证的测试消息（分区/提示/滚动条）。" % (j + 1),
                          "ts": _now - (_ago + j) * 60000} for j in range(_n)],
            "unread": _un,
        }

seen = set()                                   # msgId 去重
read_ids = {}                                  # 学号 -> 已读 msgId 累计列表（retained 回执携带，家长端恢复全部已读状态）
expanded = set()                               # 已展开（内容可见）的学生学号
scroll_y = [0]                     # 滚动偏移（内容坐标 -> 视口坐标；须在 MQTT 连接前定义——retained 推送早于界面初始化）
scroll_max = [0]                   # 最大滚动量
if os.environ.get("HSBOARD_DEMO_STATE") == "bottom":
    scroll_y[0] = 1 << 30          # 演示：初始滚到底（render 会夹紧到 scroll_max）
hist_topics_seen = set()           # 出现过的 hist retained 主题（每日清空用）
ack_topics_sent = set()            # 发过回执的 ack retained 主题（每日清空用）
read_pending = {}                  # 卡片键 -> 移入已读区的到期时间：点击即回执（家长端立见已读），
                                   # 卡片位置延迟 10 秒再变（阅读不被打断）；须在 MQTT 连接前定义
ui_queue = queue.Queue()                       # MQTT线程 -> UI线程

# ---------- MQTT ----------
def mark_read(client, sid, msg_ids):
    """批量标记已读并发布累计回执（retained 一条携带全部已读 msgId，家长端据此恢复状态）"""
    ids = read_ids.setdefault(sid, [])
    for m in msg_ids:
        if m not in ids:
            ids.append(m)
    if len(ids) > 200:
        del ids[:-200]
    payload = json.dumps(
        {"msgId": msg_ids[-1], "msgIds": ids[-200:],
         "readAt": datetime.now().isoformat(timespec="seconds"),
         "type": "ack"},
        ensure_ascii=False,
    )
    ack_topics_sent.add(ACK_PREFIX + sid)
    client.publish(ACK_PREFIX + sid, payload, qos=1, retain=True)
    disp = state.get(sid, {}).get("name", sid)
    print(f"[回执] 已读回执已发布 -> {disp}({sid}) 本次{len(msg_ids)}条（累计{len(ids)}条）")


def publish_ack(client, sid, msg_id):
    mark_read(client, sid, [msg_id])


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"[连接] 已连接 {BROKER}:{PORT} (reason_code={reason_code})")
    evt("CONN", f"MQTT 连接成功 {BROKER}:{PORT} reason={reason_code} class={CLASS_ID} roster={len(roster['students'])}")
    client.subscribe(TOPIC_MSG, qos=1)
    client.subscribe(HIST_PREFIX + "#", qos=1)   # retained 历史快照：开机恢复关机期间留言
    client.subscribe(ACK_PREFIX + "#", qos=1)    # ack retained 感知：每日零点全量清除用
    print(f"[订阅] {TOPIC_MSG}，等待家长留言…")
    evt("SUB", f"订阅 TOPIC_MSG={TOPIC_MSG} HIST#{HIST_PREFIX} ACK#{ACK_PREFIX}")


def _notice_payload(teacher, text):
    return json.dumps({
        "version": 0,
        "command": "notice",
        "args": [
            f"紧急通知 · {teacher}",
            f"--context={text}",
            "--allow-break=true",
            "--overlay-duration=15",
        ],
    }, ensure_ascii=False)


def teacher_notify(teacher, text, client=None, msg_id=""):
    """转发到本机 ClassIsland（IslandMQ 插件）触发全屏醒目提示（紧急通知）。
    先走 ZeroMQ REQ（插件默认开启），失败再试 HTTP。
    返回 True=全屏已展示 / False=降级（仅卡片）。降级时发布降级回执，老师端可区分。"""
    payload = _notice_payload(teacher, text)

    # 通道 1：ZeroMQ REQ（tcp://127.0.0.1:5555）
    try:
        import zmq
        ctx = zmq.Context()
        try:
            sock = ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, 5000)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(CLASSISLAND_ZMQ)
            sock.send_string(payload)
            ok = bool(json.loads(sock.recv_string()).get("success"))
            if ok:
                print(f"[喇叭] ClassIsland(ZMQ) 紧急通知已展示：{text}")
                return True
        finally:
            ctx.term()
    except Exception as e:
        print(f"[喇叭] ZMQ 通道失败（{e.__class__.__name__}），尝试 HTTP…")

    # 通道 2：HTTP（插件设置里需启用 HTTP 服务器，默认端口 8080）
    try:
        req = urllib.request.Request(
            CLASSISLAND_API, data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=4) as resp:
            ok = bool(json.loads(resp.read().decode("utf-8")).get("success"))
        print(f"[喇叭] ClassIsland(HTTP) 紧急通知{'已展示' if ok else '返回失败'}：{text}")
        if ok:
            return True
    except Exception as e:
        print(f"[喇叭] ClassIsland 未连接（{e.__class__.__name__}），本条仅记录：{text}")

    # 降级回执：老师端收到后可将状态从"紧急通知已发出"更新为"降级为卡片提示"
    if client and msg_id:
        try:
            degraded = json.dumps(
                {"msgId": msg_id, "type": "horn-degraded",
                 "at": datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False)
            client.publish(ACK_PREFIX + "horn-degraded", degraded, qos=1)
            evt("HORN_DEGRADE", f"紧急通知降级为卡片 msgId={msg_id} teacher={teacher}（ClassIsland 不可用）")
        except Exception as e:
            evt("HORN_DEGRADE_ERR", f"发布降级回执失败 msgId={msg_id} err={e.__class__.__name__}")
    else:
        evt("HORN_DEGRADE", f"紧急通知降级为卡片（无 client/msgId，未发回执）teacher={teacher}")
    return False


def _clean_str(v, cap):
    """消息字段清洗：转字符串、换行/制表符转空格、去掉其余控制字符、截断到上限"""
    out = []
    for ch in str(v):
        if ch in "\t\r\n":
            out.append(" ")
        elif ord(ch) >= 32:
            out.append(ch)
    return "".join(out)[:cap]


_hist_pending = {}                 # chan -> 快照数组（去抖，等 ack retained 先填 seen）
_hist_deadline = [0.0]             # 主线程 poll_queue 到点执行恢复


def _on_hist_snapshot(chan, arr):
    """从 retained 历史快照恢复留言（开机/重连时自动推送）。
    教室关机超过 Broker 会话保持（Serverless 2h）后，QoS 排队消息会被销毁，
    家长端发消息时上传的全量 hist 快照（retained 永不超期）是唯一可靠的找回途径。
    每日零点双端清空后，快照里只剩当天消息，恢复的每一条都值得亮未读红点。"""
    hist_topics_seen.add(HIST_PREFIX + chan)
    if not isinstance(arr, list) or not arr:
        return
    _hist_pending[chan] = arr
    _hist_deadline[0] = time.time() + 3.0    # 去抖：给 ack retained 推送留填充 seen 的时间
    ui_queue.put(("render", chan))           # 触发主线程轮询（到点 _flush_hist_pending）


def _restore_chan(chan, arr):
    n_new = 0
    for m in arr:
        if not isinstance(m, dict):
            continue
        mid = _clean_str(m.get("msgId") or "", 64)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        text = _clean_str(m.get("text") or "", 600)
        try:
            ts = int(m.get("ts") or 0)
            if not 0 < ts < int(time.time() * 1000) + 86400000:
                raise ValueError
        except Exception:
            ts = int(time.time() * 1000)
        # 7 天边界：以消息自身 ts 为基准，不依赖本地时钟（防教室/家长端时钟漂移导致
        # 边界消息两端判定不一致）。宽限 5 分钟窗口吸收 NTP 未同步时的少量偏差。
        if ts + 5 * 60000 < int(time.time() * 1000) - 7 * 86400000:
            # 7 天前的旧消息不再恢复（与家长端切片 7 天期限对齐）：兜底挡住
            # 历史遗留的残留快照（如早期测试消息），避免反复死灰复燃
            continue
        if m.get("horn"):
            s = state.setdefault("horn", {"name": _clean_str(m.get("teacher") or "老师", 30) + "（紧急）",
                                          "messages": [], "unread": 0, "teacher": True})
            s["name"] = _clean_str(m.get("teacher") or "老师", 30) + "（紧急）"
        elif chan.startswith("t-"):
            s = state.setdefault(chan, {"name": _clean_str(chan[2:], 30),
                                        "messages": [], "unread": 0, "teacher": True})
        else:
            name = next((stu["name"] for stu in roster["students"] if stu["id"] == chan),
                        "学号" + _clean_str(chan, 16))
            s = state.setdefault(chan, {"name": name, "messages": [], "unread": 0})
        s["messages"].append({"msgId": mid, "text": text, "ts": ts})
        if len(s["messages"]) > 200:
            del s["messages"][:-100]
        s["unread"] += 1
        n_new += 1
    if n_new:
        print(f"[恢复] 从服务端历史恢复 {chan} {n_new} 条留言（未读红点已亮出）")
        ui_queue.put(("render", chan))


def _flush_hist_pending():
    """主线程到点执行：pending 里全部快照按去抖后的 seen 恢复"""
    if not _hist_pending:
        return
    pend = dict(_hist_pending)
    _hist_pending.clear()
    for chan, arr in pend.items():
        _restore_chan(chan, arr)


def on_message(client, userdata, msg):
    if msg.topic.startswith(ACK_PREFIX):
        # 回执是本端发布的；收到推送用于收集通道清单 + 把已回执的 msgId 记入 seen，
        # 这样开机从 hist 快照恢复时会自动跳过"已经处理过（读过）"的消息。
        # 同时必须并入 read_ids 累计：否则启动后第一次回执会用"只含本次"的 msgIds
        # 覆盖掉服务器上的累计回执 -> 旧 msgId 丢失 -> 已读过的旧消息死灰复燃
        # （2026-09 生产实测：上周六的测试消息天天恢复，本处是根因之一）。
        ack_topics_sent.add(msg.topic)
        try:
            a = json.loads(msg.payload.decode("utf-8"))
            ids = a.get("msgIds") or ([a.get("msgId")] if a.get("msgId") else [])
            rids = read_ids.setdefault(msg.topic[len(ACK_PREFIX):], [])
            for x in ids:
                x = _clean_str(x, 64)
                if not x:
                    continue
                if x not in seen:
                    seen.add(x)
                if x not in rids:
                    rids.append(x)
            if len(rids) > 200:
                del rids[:-200]
        except Exception:
            pass
        return
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        return                                    # 畸形 JSON 直接忽略，不影响后续消息
    if msg.topic.startswith(HIST_PREFIX):         # hist 快照是 JSON 数组，必须先于 dict 检查分流
        _on_hist_snapshot(msg.topic[len(HIST_PREFIX):], data)
        return
    if not isinstance(data, dict):
        return
    if len(seen) > 20000:                         # 去重集防膨胀（熊孩子狂发场景）
        seen.clear()
    sid = msg.topic[len(MSG_PREFIX):]
    mid = _clean_str(data.get("msgId") or "", 64)
    if not mid or mid in seen:
        return
    seen.add(mid)
    t = datetime.now().strftime("%H:%M:%S")
    try:                                       # 时间戳容错（字符串/缺省/离谱值都用当前时间）
        ts = int(data.get("ts") or 0)
        if not 0 < ts < int(time.time() * 1000) + 86400000:
            raise ValueError
    except Exception:
        ts = int(time.time() * 1000)

    mtype = _clean_str(data.get("type") or "normal", 16)
    if sid == "horn" or mtype == "horn" or data.get("horn"):
        # 老师紧急通知 -> 转调 ClassIsland 全屏醒目提示，同时记入留言板卡片（可展开、有已读回执）
        teacher = _clean_str(data.get("teacher") or "老师", 30)
        text = _clean_str(data.get("text") or "", 600)
        print(f"[喇叭] 收到 {teacher} 的紧急通知：{text}")
        threading.Thread(target=teacher_notify, args=(teacher, text, client, mid), daemon=True).start()
        s = state.setdefault("horn", {"name": teacher + "（紧急）", "messages": [], "unread": 0, "teacher": True})
        s["name"] = teacher + "（紧急）"       # 卡片标题跟随最近一位发送的老师
        s["messages"].append({"msgId": mid, "text": text, "ts": ts, "teacher": teacher})
        if len(s["messages"]) > 200:
            del s["messages"][:-100]
        s["unread"] += 1
        expanded.discard("horn")               # 新通知到达时自动收起，内容不直接暴露在公共屏幕上
        print(f"[留言] {t}  {s['name']}(horn)  内容={text}  未读={s['unread']}")
        if autoack:
            threading.Timer(3.0, mark_read, args=(client, "t-" + teacher, [mid])).start()  # 回执并入该老师自己的通道
        ui_queue.put(("render", sid))
        return

    if sid not in state:                       # 名单外学号也能显示
        if data.get("role") == "teacher":
            name = _clean_str(data.get("teacher") or (sid[2:] if sid.startswith("t-") else sid), 30)
            state[sid] = {"name": name, "messages": [], "unread": 0, "teacher": True}
        else:
            state[sid] = {"name": "学号" + _clean_str(sid, 16), "messages": [], "unread": 0}
    s = state[sid]
    s["messages"].append({
        "msgId": mid,
        "text": _clean_str(data.get("text") or "", 600),   # 留言上限 600 字（家长端 140，防绕过刷屏）
        "ts": ts,
    })
    if len(s["messages"]) > 200:               # 每人最多留 200 条，防内存膨胀
        del s["messages"][:-100]
    s["unread"] += 1
    expanded.discard(sid)          # 新留言到达时自动收起，内容不直接出现在公共屏幕上
    print(f"[留言] {t}  {s['name']}({sid})  内容={s['messages'][-1]['text']}  未读={s['unread']}")
    if autoack:
        threading.Timer(3.0, publish_ack, args=(client, sid, mid)).start()
    if shot_path[0] and not shot_done[0]:
        shot_done[0] = True
        threading.Timer(6.0, lambda: ui_queue.put(("shot", None))).start()
    ui_queue.put(("render", sid))


# 持久会话：固定 client_id + clean_session=False。
# 教室电脑关机期间，家长的留言由 Broker 按会话排队（QoS1），
# 开机程序连上后自动收到离线消息，实现"关机信息也不丢"。
# 注意：同一 client_id 同时只能有一个连接（互踢），不要开两个实例。
client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id=os.environ.get("HSBOARD_CLIENT_ID", f"hs-board-{CLASS_ID}"),   # 每班独立（≤23 字符），三台白板可同时在线
    clean_session=False,
)
client.tls_set()                                   # EMQX Cloud 证书为公共可信 CA，系统信任库即可校验
client.username_pw_set(MQTT_USER, MQTT_PASS)
client.on_connect = on_connect
client.on_message = on_message


def on_disconnect(client, userdata, flags, reason_code, properties):
    """断连诊断：reason_code 非 0 说明网络/服务端问题，记入 [EVT] 便于回溯一周内异常。"""
    print(f"[连接] MQTT 断开 reason_code={reason_code}，paho 将按 keepalive 自动重连")
    evt("DISCONN", f"MQTT 断开 reason_code={reason_code} class={CLASS_ID}（paho 自动重连中）")


def on_reconnect(client, userdata, flags, reason_code, properties):
    print(f"[连接] MQTT 重连成功 reason_code={reason_code}")
    evt("RECONN", f"MQTT 重连成功 reason_code={reason_code} class={CLASS_ID}")


client.on_disconnect = on_disconnect
client.on_reconnect = on_reconnect
if os.environ.get("HSBOARD_DEMO_STATE") not in ("1", "bottom"):
    # 生产/常规模式：持久会话（教室电脑关机期间留言由 Broker 排队）。
    # 注意：同一 client_id 互踢——开发机测试时设 HSBOARD_CLIENT_ID 避开教室端正在用的会话。
    client.connect(BROKER, PORT, keepalive=30)
    client.loop_start()
else:
    print("[演示] 本地演示模式（不连 Broker）：仅供布局/界面自动化验证")


# ---------- DPI：Per-Monitor 感知 + 全局缩放系数（文字清晰的关键）----------
try:
    windll.shcore.SetProcessDpiAwareness(2)        # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        windll.user32.SetProcessDPIAware()
    except Exception:
        pass

_user32 = windll.user32
_shcore = windll.shcore
try:
    _DPI = _user32.GetDpiForSystem() or 96         # 感知模式下返回真实 DPI
except Exception:
    _DPI = 96
S = _DPI / 96.0                                    # 缩放系数（本机 175% => 1.75）
UI_SCALE = [float(settings.get("scale", 1.0))]     # 用户缩放档位（设置菜单 25%~100%，整窗生效）
if CLI_SCALE is not None:
    UI_SCALE[0] = CLI_SCALE


def Z(v):
    """逻辑坐标 -> 物理像素（DPI × 用户缩放）"""
    return int(v * S * UI_SCALE[0])


def PF(px):
    """逻辑字号 -> 像素字号（Tk 负数字号为像素；下限 11px 防小档位字号不可读）"""
    return -max(11, int(round(px * S * UI_SCALE[0])))


# ---------- 窗口样式 / 玻璃特效 ----------
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WCA_ACCENT_POLICY = 19
ACCENT_ACRYLIC_BLUR_BEHIND = 4


class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_uint),
                ("AccentFlags", ctypes.c_uint),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_uint)]


class COMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]


def apply_acrylic(hwnd, alpha=0x8C, rgb=(0x16, 0x1B, 0x26)):
    """亚克力模糊背景（Win10/1803+）。失败时静默降级为普通深色面板。"""
    try:
        r, g, b = rgb
        gc = (alpha << 24) | (b << 16) | (g << 8) | r     # AABBGGRR
        accent = ACCENT_POLICY(ACCENT_ACRYLIC_BLUR_BEHIND, 0, gc, 0)
        data = COMPATTRDATA(WCA_ACCENT_POLICY,
                            ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                            ctypes.sizeof(accent))
        return bool(_user32.SetWindowCompositionAttribute(hwnd, byref(data)))
    except Exception:
        return False


def apply_dwm_round(hwnd):
    """Win11 DWM 系统圆角（DWMWA_WINDOW_CORNER_PREFERENCE=33, DWMWCP_ROUND=2）。
    对 child 窗口可能无效，失败静默（面板退化为直角）。"""
    try:
        pref = ctypes.c_int(2)
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        return res == 0
    except Exception:
        return False


PROXY = [None]     # 1x1 隐形原生代理窗口（常驻 Progman 正上方，占住"隐形死位"）
_PROXY_CB = [None]  # WNDPROC 引用防 GC（原生窗口的窗口过程必须持久持有）


def _create_proxy():
    """创建 1x1 隐形原生顶层窗口。不能用 Tk Toplevel：Tk 会按内部堆叠管理自行
    把 Toplevel 重新置顶，破坏我们停靠的 z 序（实测）。"""
    WNDPROC = WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, ctypes.c_uint,
                          ctypes.c_ulonglong, ctypes.c_longlong)

    def _proc(h, msg, wp, lp):
        return _user32.DefWindowProcW(h, msg, wp, lp)

    cb = WNDPROC(_proc)
    _PROXY_CB[0] = cb
    _user32.DefWindowProcW.restype = ctypes.c_longlong
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                       ctypes.c_ulonglong, ctypes.c_longlong]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", ctypes.c_uint),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", ctypes.c_void_p),
                    ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]

    wc = WNDCLASSW()
    wc.lpfnWndProc = cb
    windll.kernel32.GetModuleHandleW.restype = wintypes.HMODULE   # 64 位句柄完整返回（打包后基址更高，默认 c_int 会溢出）
    wc.hInstance = windll.kernel32.GetModuleHandleW(None)
    wc.lpszClassName = "HSBoardProxyWnd"
    _user32.RegisterClassW(byref(wc))
    # argtypes 强类型化：dwStyle=DWORD 才能接受 0x80000000(WS_POPUP)；否则无声明时默认 c_int 溢出
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _user32.CreateWindowExW.restype = wintypes.HWND
    WS_POPUP = 0x80000000
    return _user32.CreateWindowExW(
        WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, "HSBoardProxyWnd", "",
        WS_POPUP, -32000, -32000, 1, 1, None, None, wc.hInstance, None)


def dock_to_desktop(root):
    """停靠到桌面层：普通顶层窗口 + z 序停靠到"桌面族整体"之上。

    关键教训（2026-09-05 实测本机）：
    - Tk 窗口 SetParent 进 Progman/WorkerW 当子窗口：DWM 不合成像素，永远隐形。
    - 紧贴 Progman 正上方一格是"隐形死位"：DWM 直接在该槽位合成壁纸位图，
      盖过此槽位的窗口内容（实测：下方=Progman 时壁纸盖脸，隔一个隐藏窗口就正常）。
    - HWND_BOTTOM 沉底会掉到 Progman 之下，被桌面整层盖住。
    - 正确姿势 = 顶层工具窗口（WS_EX_TOOLWINDOW，Win+D 最小化风暴豁免，实测）
      + 1x1 隐形代理常驻 Progman 正上方占住死位 + 面板停靠代理之上。
    """
    progman = _user32.FindWindowW("Progman", None)
    if not progman:
        return False, "Progman 未找到"

    SWP = 0x1 | 0x2 | 0x10                     # NOSIZE | NOMOVE | NOACTIVATE
    HWND_BOTTOM = 1
    hwnd = _user32.GetAncestor(root.winfo_id(), 2) or int(root.wm_frame(), 16)   # GA_ROOT 取顶层包装
    ex = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    _user32.SetWindowLongPtrW(
        hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & 0xFFFFFFFF)

    # 1) 原生代理窗口常驻 Progman 正上方（死位由它占，它 1x1 无内容无所谓被盖）。
    #    插入点 = 当前紧贴 Progman 上方的窗口之后（z 序语义：插到 X 后 = X 之下）。
    if not PROXY[0]:
        PROXY[0] = _create_proxy()
    ph = PROXY[0]
    above = _user32.GetWindow(progman, 3)              # GW_HWNDPREV = 紧贴 Progman 上方
    if above and above != ph:
        _user32.SetWindowPos(ph, above, 0, 0, 0, 0, SWP)   # 代理落到它后面 = 紧贴桌面上方
    elif not above:
        _user32.SetWindowPos(ph, HWND_BOTTOM, 0, 0, 0, 0, SWP)
        _user32.SetWindowPos(progman, ph, 0, 0, 0, 0, SWP)

    # 2) 面板停靠代理之上：从代理向上跳过桌面族/TOPMOST/自己，找第一个普通窗口插到它后面
    anchor = None
    w = _user32.GetWindow(ph, 3)                           # GW_HWNDPREV = 上一层
    _n = 0
    while w and _n < 256:      # 上限防 Win+D 瞬间 z 栈重排导致枚举成环 → 主线程死循环（UI 卡死）
        _n += 1
        if w == hwnd:                                      # 已在正确位置，跳过自己
            w = _user32.GetWindow(w, 3)
            continue
        b = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(w, b, 64)
        if b.value in ("WorkerW", "Progman"):              # 桌面族（正常不会出现在代理上方）
            w = _user32.GetWindow(w, 3)
            continue
        if _user32.GetWindowLongPtrW(w, GWL_EXSTYLE) & 0x00000008:   # WS_EX_TOPMOST
            w = _user32.GetWindow(w, 3)
            continue
        anchor = w
        break
    if anchor:
        _user32.SetWindowPos(hwnd, anchor, 0, 0, 0, 0, SWP)
        return True, f"已停靠桌面层（代理={ph}, 锚={anchor}）"
    # 没找到锚：保持原位（绝不能沉底——会掉到桌面之下被壁纸整层盖住）
    return True, f"停靠保持原位（代理={ph}）"


# ---------- 界面（液态玻璃：壁纸采样磨砂版）----------
# 原理：Win10 下 Tk 无原生圆角/亚克力，挂桌面层的 layered 窗口又不被 DWM 合成（实测）。
# 改用桌面挂件通用的"壁纸采样"方案：
#   启动时（窗口显示前）抓取窗口区域的屏幕内容 -> 高斯模糊 + 暗化 = 磨砂底图；
#   圆角外画壁纸原样 = 视觉上等于真透明；窗口本身不透明、无 layered，合成 100% 稳定。
KEY = "#010203"            # （保留兼容，未再使用）
PANEL = "#161C2A"          # 兜底底色（背景图不足时）
PANEL_EDGE = "#48597E"     # 面板描边（玻璃边）
CARD = "#3A4258"           # 卡片底（磨砂之上的浅一档）
CARD_EDGE = "#6A7490"      # 卡片描边
TXT_MAIN = "#F2F5FB"
TXT_SUB = "#A8B2C9"
TXT_DIM = "#76819B"
RED = "#FF5A5F"

W, H = Z(400), Z(560)          # 面板尺寸 400x560（物理像素；缩放档位变化后由 recompute_metrics 重算）
SAMPLE_H = Z(900)              # 背景采样高度上限（卡片增多面板变高也能覆盖）
R = Z(22)                      # 圆角半径

root = tk.Tk()
root.title("班级留言板")
root.overrideredirect(True)
root.config(bg=PANEL)


def _apply_geometry():
    """按 settings/CLI 位置与当前 W/SAMPLE_H 摆放窗口（x 钳制在屏幕内）"""
    sw = root.winfo_screenwidth()
    if POS:
        x, y = Z(POS[0]), Z(POS[1])
    else:
        x, y = sw - W - Z(24), Z(24)
    x = max(0, min(int(x), sw - W))
    root.geometry(f"{W}x{SAMPLE_H}+{x}+{int(y)}")


def recompute_metrics():
    """缩放档位变化后重算尺寸族（W/H/SAMPLE_H/R 都经 Z()）并重设画布与窗口"""
    global W, H, SAMPLE_H, R
    W, H = Z(400), Z(560)
    SAMPLE_H = Z(900)
    R = Z(22)
    canvas.config(width=W, height=SAMPLE_H)
    _apply_geometry()


_apply_geometry()
root.update_idletasks()

# ---- 壁纸采样磨砂背景（此刻窗口还未显示，抓屏是干净的壁纸/桌面）----
def make_wall_bg():
    x, y = root.winfo_rootx(), root.winfo_rooty()
    grab = ImageGrab.grab(bbox=(x, y, x + W, y + SAMPLE_H)).convert("RGB")
    frosted = grab.filter(ImageFilter.GaussianBlur(Z(10)))
    dark = Image.new("RGB", frosted.size, (15, 19, 28))
    frosted = Image.blend(frosted, dark, 0.52)          # 磨砂 + 暗化
    # 圆角 mask：4x 超采样再缩小，边缘平滑无锯齿（用户反馈的"圆角瑕疵"）
    SS = 4
    mask = Image.new("L", (W * SS, SAMPLE_H * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, W * SS - 1, SAMPLE_H * SS - 1], radius=R * SS, fill=255)
    mask = mask.resize((W, SAMPLE_H), Image.LANCZOS)
    out = grab.copy()
    out.paste(frosted, (0, 0), mask)
    # 面板描边（沿圆角，1 物理像素；不画顶部高光线——小圆角下会变形出瑕疵）
    edge = Image.new("RGBA", (W * SS, SAMPLE_H * SS), (0, 0, 0, 0))
    de = ImageDraw.Draw(edge)
    de.rounded_rectangle([SS // 2, SS // 2, W * SS - SS // 2, SAMPLE_H * SS - SS // 2],
                         radius=R * SS, outline=(140, 158, 198, 200), width=SS)
    edge = edge.resize((W, SAMPLE_H), Image.LANCZOS)
    out.paste(edge, (0, 0), edge)
    WALL_BG_PIL[0] = out            # 保留 PIL 版供可见性自检比对
    return ImageTk.PhotoImage(out)


WALL_BG = None      # render 里首次使用前已生成（见下）
WALL_BG_PIL = [None]                # 磨砂底图 PIL 版（可见性自检基准）
canvas = tk.Canvas(root, width=W, height=SAMPLE_H, bg=PANEL, highlightthickness=0, bd=0)
canvas.pack(fill="both", expand=True)
clock_id = [None]
pinned_hwnd = [None]               # 挂桌面层成功后的窗口句柄（用于 z 序保活）
_cards_meta = []                   # 点击命中用：[(x1, y1, x2, y2, sid)]
_date_str = [os.environ.get("HSBOARD_FAKE_DATE") or datetime.now().strftime("%Y%m%d")]


def rrect(x1, y1, x2, y2, r, **kw):
    """圆角矩形（弧点采样多边形，四角精确对称，smooth polygon 会有圆角不对称瑕疵）"""
    pts = []
    seg = 7                                  # 每个圆角的弧段数
    corners = [(x2 - r, y1 + r, -90, 0),     # 右上: 圆心, 起止角
               (x2 - r, y2 - r, 0, 90),      # 右下
               (x1 + r, y2 - r, 90, 180),    # 左下
               (x1 + r, y1 + r, 180, 270)]   # 左上
    for cx, cy, a0, a1 in corners:
        for i in range(seg + 1):
            a = (a0 + (a1 - a0) * i / seg) * math.pi / 180.0
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return canvas.create_polygon(*pts, fill=kw.pop("fill", ""), outline=kw.pop("outline", ""),
                                 width=kw.pop("width", 1), **kw)


def fmt_time(ts):
    d = datetime.fromtimestamp(ts / 1000)
    return f"{d.day}日 {d:%H:%M}"                  # 完整"X日 HH:MM"，隔天消息不再混淆


LINE_BUDGET = 52          # 每行显示宽度预算（全角=2，半角=1；约 26 个汉字）


def _ch_w(ch):
    return 2 if ord(ch) > 0x2E7F else 1


def wrap_lines(text, budget=LINE_BUDGET):
    """按显示宽度换行（中文任意断行；URL/英文超宽也直接断，保证内容完整可读）"""
    lines, cur, cw = [], "", 0
    for ch in text:
        w = _ch_w(ch)
        if cw + w > budget and cur:
            lines.append(cur)
            cur, cw = "", 0
            if ch == " ":
                continue
        cur += ch
        cw += w
    if cur or not lines:
        lines.append(cur)
    return lines


def message_display_lines(text):
    """留言展开后的显示行（受设置 max_lines 限制，0=完整显示不截断）"""
    ls = wrap_lines(text)
    ml = settings.get("max_lines", 3)
    if ml and len(ls) > ml:
        ls = ls[:ml]
        ls[-1] = (ls[-1][:-1] + "…") if len(ls[-1]) > 1 else "…"
    return ls


def msg_block_h(lines):
    """单条留言占高：首行 30，续行 18（与 render 保持一致）"""
    return Z(30) + Z(18) * (len(lines) - 1)


DENSITY = {
    "cozy":    (64, 14, 1, 88),    # (收起卡高, 间距, 列数, 内容起始 y) 标准：单列大卡
    "compact": (40, 6, 1, 76),     # 紧凑：单列单行卡，一屏约 2 倍
    "grid":    (64, 8, 2, 76),     # 双列网格：一屏约 4 倍（默认）；紧凑/双列 header 压缩到 76
}
DENSITY_LABEL = {"cozy": "标准（单列大卡）", "compact": "紧凑（单列单行）", "grid": "双列网格"}


def _density_key():
    """当前密度档：环境变量 HSBOARD_DENSITY（测试钩子，不落盘）优先于持久化设置"""
    return os.environ.get("HSBOARD_DENSITY") or settings.get("density", "grid")


def layout_cards():
    """统一卡片行进器（render/calc_height 共用，防止两处手算行进漂移）：
    按当前密度铺未读区（上）/已读区（下），返回 (卡片列表, 内容末尾 y_c, 未读区, 已读区)。
    卡片项 = (sid, s, open_, x1, x2, y, h)；单列与展开卡占整行，双列收起卡半宽偶左奇右，
    展开卡先收尾半行再跨整行。"""
    ch, gap, cols, y0 = DENSITY.get(_density_key(), DENSITY["grid"])
    now_ts = time.time()
    with_msgs = [(k, s) for k, s in state.items() if s["messages"]]
    unread_zone = sorted(
        (kv for kv in with_msgs
         if kv[1]["unread"] > 0 or read_pending.get(kv[0], 0) > now_ts),
        key=lambda kv: kv[1]["messages"][-1]["ts"], reverse=True)
    read_zone = sorted(
        (kv for kv in with_msgs
         if kv[1]["unread"] == 0 and read_pending.get(kv[0], 0) <= now_ts),
        key=lambda kv: kv[1]["messages"][-1]["ts"], reverse=True)
    x1, x2 = Z(24), W - Z(24)
    mid = (x1 + x2) // 2
    gut = Z(5)                                 # 双列中缝
    y_c = Z(y0)
    col = 0
    items = []
    for sid_key, s in unread_zone + read_zone:
        open_ = expand_all or (sid_key in expanded)
        if open_:
            shown = s["messages"][-6:]
            h = Z(50) + sum(msg_block_h(message_display_lines(m["text"])) for m in shown) + Z(16)
        else:
            h = Z(ch)
        if open_ or cols == 1:                 # 展开卡/单列：整行
            if col:                            # 半行收尾
                y_c += Z(ch) + Z(gap)
                col = 0
            items.append((sid_key, s, open_, x1, x2, y_c, h))
            y_c += h + Z(gap)
        else:                                  # 双列收起卡：偶左奇右
            xa, xb = (x1, mid - gut) if col == 0 else (mid + gut, x2)
            items.append((sid_key, s, False, xa, xb, y_c, h))
            col = 1 - col
            if col == 0:
                y_c += Z(ch) + Z(gap)
    if col:
        y_c += Z(ch) + Z(gap)
    return items, y_c, unread_zone, read_zone


def calc_height():
    """返回 (视口高, 内容总高)：留言多时内容可超视口，靠滚轮/触摸查看；
    视口高度不超过屏幕剩余空间（防面板底边探出屏幕）。内容高由 layout_cards 统一行进。"""
    if not any(s["messages"] for s in state.values()):
        return Z(240), Z(240)
    _, y_end, _, _ = layout_cards()
    content = min(y_end + Z(44), Z(4000))      # 内容预算上限（防极端膨胀）
    try:                                       # 视口 <= 屏幕底边 - 面板顶 - 任务栏余量
        avail = root.winfo_screenheight() - root.winfo_rooty() - Z(50)
    except Exception:
        avail = SAMPLE_H - Z(20)
    view = min(max(content, Z(240)), SAMPLE_H - Z(20), max(avail, Z(240)))
    return view, content


def render():
    global H
    view, content = calc_height()
    scroll_max[0] = max(0, content - view)
    if abs(view - H) > Z(6):
        H = view
        root.geometry(f"{W}x{H}+{root.winfo_x()}+{root.winfo_y()}")
    scroll_y[0] = max(0, min(scroll_y[0], scroll_max[0]))
    off = scroll_y[0]
    canvas.delete("all")
    _cards_meta.clear()
    if WALL_BG is not None:
        canvas.create_image(0, 0, image=WALL_BG, anchor="nw")   # 磨砂背景（含圆角透明区）

    canvas.create_text(Z(34), Z(40), text="班级留言板", anchor="w",
                       font=("Microsoft YaHei", PF(17), "bold"), fill=TXT_MAIN)
    clock_id[0] = canvas.create_text(W - Z(34), Z(40),
                                     text=datetime.now().strftime("%H:%M:%S"),
                                     font=("Consolas", PF(11)), fill=TXT_SUB, anchor="e")
    canvas.create_text(Z(34), Z(62), text=f"{CLASS_NAME} · 留言内容点击后才会显示",
                       anchor="w", font=("Microsoft YaHei", PF(10)), fill=TXT_SUB)

    cards = [s for s in state.values() if s["messages"]]
    if not cards:
        canvas.create_text(W // 2, Z(140), text="暂时没有留言\n等家长的第一条消息吧",
                           font=("Microsoft YaHei", PF(12)), fill=TXT_DIM, justify="center")

    hidden_n = 0                               # 视口下方的卡片数
    above_n = 0                                # 视口上方的卡片数
    below_unread = 0                           # 视口下方的未读消息条数（仅统计未读）
    items, _, uz, rz = layout_cards()          # 统一行进器：分区/密度/展开跨行全在此处理
    dkey = _density_key()
    for sid_key, s, open_, cx1, cx2, y_c, h in items:
        unread = s["unread"]
        if y_c + h < off + Z(60):              # 完全滚出视口上方
            above_n += 1
            continue
        if y_c > off + H - Z(96):              # 在视口下方：滚轮/触摸可看到，不绘制
            hidden_n += 1                      # （下方保留 96 逻辑像素给底部提示/页脚，防文字压卡片）
            below_unread += unread
            continue
        y = y_c - off                          # 内容坐标 -> 视口坐标
        tags = ("card", sid_key)
        # 卡片玻璃体（磨砂底上浅一档 + 描边）
        rrect(cx1, y, cx2, y + h, Z(16), fill=CARD, outline=CARD_EDGE,
              width=max(1, Z(1)), tags=tags)
        title = s["name"] if s.get("teacher") else s["name"] + "家长"
        if open_:
            # 展开态（任何密度都跨整行）：逐条显示留言（时间 + 内容，超宽自动换行不再截成一行）
            shown = s["messages"][-6:]
            canvas.create_text(cx1 + Z(16), y + Z(22), text=title, anchor="w",
                               font=("Microsoft YaHei", PF(12), "bold"), fill=TXT_MAIN, tags=tags)
            canvas.create_text(cx2 - Z(52), y + Z(22), text=fmt_time(s["messages"][-1]["ts"]),
                               anchor="e", font=("Consolas", PF(10)), fill=TXT_SUB, tags=tags)
            my = y + Z(50)
            for m in shown:
                lines = message_display_lines(m["text"])
                canvas.create_text(cx1 + Z(16), my, text=fmt_time(m["ts"]), anchor="nw",
                                   font=("Consolas", PF(9)), fill=TXT_SUB, tags=tags)
                for li, ln in enumerate(lines):
                    canvas.create_text(cx1 + Z(58), my + Z(18) * li, text=ln, anchor="nw",
                                       font=("Microsoft YaHei", PF(10)), fill="#C9D2E8", tags=tags)
                my += msg_block_h(lines)
            canvas.create_text(cx1 + Z(16), y + h - Z(10), text="点击收起", anchor="w",
                               font=("Microsoft YaHei", PF(8)), fill=TXT_DIM, tags=tags)
        elif dkey == "compact":
            # 紧凑单行卡：姓名 + 时间 + 红点同行（提示文字省略，红点即"有未读"）
            canvas.create_text(cx1 + Z(14), y + Z(20), text=title, anchor="w",
                               font=("Microsoft YaHei", PF(11), "bold"), fill=TXT_MAIN, tags=tags)
            canvas.create_text(cx2 - Z(40), y + Z(20), text=fmt_time(s["messages"][-1]["ts"]),
                               anchor="e", font=("Consolas", PF(9)), fill=TXT_SUB, tags=tags)
        else:
            # 收起态（cozy 单列 / grid 半宽）：绝不显示内容，只给标题与提示
            canvas.create_text(cx1 + Z(16), y + Z(22), text=title, anchor="w",
                               font=("Microsoft YaHei", PF(12 if dkey == "cozy" else 11), "bold"),
                               fill=TXT_MAIN, tags=tags)
            if dkey == "cozy":
                canvas.create_text(cx2 - Z(52), y + Z(22), text=fmt_time(s["messages"][-1]["ts"]),
                                   anchor="e", font=("Consolas", PF(10)), fill=TXT_SUB, tags=tags)
                canvas.create_text(cx1 + Z(16), y + Z(44), text="点击查看留言", anchor="w",
                                   font=("Microsoft YaHei", PF(10)), fill=TXT_SUB, tags=tags)
            else:                              # grid 半宽：第二行 = 时间 + 提示合并
                canvas.create_text(cx1 + Z(16), y + Z(44),
                                   text=f"{fmt_time(s['messages'][-1]['ts'])} · 点击查看",
                                   anchor="w", font=("Consolas", PF(8)), fill=TXT_SUB, tags=tags)
        # 红点（未读数，仅收起态会出现）
        if unread > 0:
            if dkey == "compact":
                r, cx, cy = Z(9), cx2 - Z(14), y + Z(20)
            else:
                r, cx, cy = Z(11), cx2 - Z(22), y + Z(22)
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=RED, outline="",
                               tags=tags)
            canvas.create_text(cx, cy, text=str(min(unread, 99)),
                               font=("Consolas", PF(8 if dkey == "compact" else 9), "bold"),
                               fill="#FFFFFF", tags=tags)

    if above_n > 0:                            # 顶部位置提示：视口上方还有几位同学
        canvas.create_text(W - Z(34), Z(62), text=f"↑ 上方还有 {above_n} 位同学",
                           anchor="e", font=("Microsoft YaHei", PF(9)), fill=TXT_SUB)

    thumb_y, thumb_h = 0, 0
    if scroll_max[0] > Z(10):                  # 右侧滚动条位置指示器（相对位置一目了然）
        track_t, track_b = Z(96), H - Z(96)
        track_h = track_b - track_t
        content_total = scroll_max[0] + H
        thumb_h = max(Z(24), int(track_h * H / max(1, content_total)))
        thumb_y = track_t + int((track_h - thumb_h) * scroll_y[0] / max(1, scroll_max[0]))
        tx = W - Z(12)
        rrect(tx, track_t, tx + Z(4), track_b, Z(2), fill="#2A3348", outline="")
        rrect(tx, thumb_y, tx + Z(4), thumb_y + thumb_h, Z(2), fill=CARD_EDGE, outline="")

    if scroll_max[0] > Z(10):                  # 有内容滚出视口：底部提示（计数仅含未读）
        tip = "留言较多，滚轮/触摸滑动查看更多"
        if below_unread > 0:
            tip += f" · 下方还有 {below_unread} 条消息"
        elif hidden_n > 0:
            tip += f" · 下方还有 {hidden_n} 位同学"
        tw = int(len(tip) * Z(9)) + Z(28)      # 底垫：滚动中卡片可延伸到提示行，垫底防文字压叠
        rrect(W // 2 - tw // 2, H - Z(78), W // 2 + tw // 2, H - Z(50), Z(8),
              fill=PANEL, outline="")
        canvas.create_text(W // 2, H - Z(64), text=tip,
                           font=("Microsoft YaHei", PF(9)), fill=TXT_SUB)

    if os.environ.get("HSBOARD_DEBUG_LAYOUT"):  # 自动化验证：结构化布局信息
        print(f"[布局] density={dkey} unread_zone={len(uz)} read_zone={len(rz)} "
              f"first_unread={uz[0][0] if uz else '-'} "
              f"first_read={rz[0][0] if rz else '-'} "
              f"above_n={above_n} below_unread={below_unread} below_cards={hidden_n} "
              f"scroll_y={scroll_y[0]} scroll_max={scroll_max[0]} "
              f"thumb=({thumb_y},{thumb_h}) scale={UI_SCALE[0]:.2f} W={W} H={H}")

    canvas.create_text(W // 2 - Z(16), H - Z(24), text="点击卡片查看留言（展开即已读） · 右键有设置菜单",
                       font=("Microsoft YaHei", PF(9)), fill=TXT_DIM)
    canvas.create_text(W - Z(18), H - Z(24), text="⚙ 设置", anchor="e",
                       font=("Microsoft YaHei", PF(9)), fill=TXT_SUB, tags="gear")


READ_MOVE_DELAY = 10.0                         # 点击后卡片移入已读区的延迟（秒）


def activate_card(sid):
    """点击卡片：展开/收起；展开且未读 -> 清红点 + 立即发布已读回执（家长端马上看到"已读"），
    卡片位置等 10 秒后再移入已读区（阅读不被打断）。"""
    s = state[sid]
    if sid in expanded:
        expanded.discard(sid)                  # 再点一次：收起（不发回执）
    else:
        expanded.add(sid)                      # 展开：内容可见
        if s["unread"] > 0:
            s["unread"] = 0
            if sid == "horn":      # 紧急卡：按老师分组回执，发到各自的 t- 主题（与普通留言共用已读流）
                groups = {}
                for m in s["messages"]:
                    groups.setdefault(m.get("teacher") or "老师", []).append(m["msgId"])
                for teacher, mids in groups.items():
                    mark_read(client, "t-" + teacher, mids)
            else:
                mark_read(client, sid, [m["msgId"] for m in s["messages"]])   # 点一次：整卡全部已读
            read_pending[sid] = time.time() + READ_MOVE_DELAY
    render()


# ---------- 触控/拖拽滚动（希沃白板）+ 点击"松开判定" ----------
# Windows 把单指触摸转成鼠标事件：按下 + B1-Motion 即覆盖触屏滑动，与滚轮同一条滚动通道。
# 点击改为松开判定：位移超阈值视为拖拽滚动（不触发点击），未超阈值才命中卡片/设置按钮。
_drag = [0, 0, False]                          # [上一事件 y, 累计位移 |dy|, 已进入拖拽态]
_drag_render_due = [False]                     # after_idle 合并高频 Motion 的重绘


def _on_press(event):
    _drag[0] = event.y
    _drag[1] = 0
    _drag[2] = False


def _on_motion(event):
    dy = event.y - _drag[0]
    _drag[0] = event.y
    _drag[1] += abs(dy)
    if not _drag[2] and _drag[1] > Z(8):       # 阈值（逻辑 8px）：触控抖动不误判
        _drag[2] = True
    if _drag[2] and dy:
        scroll_y[0] = max(0, min(scroll_y[0] - dy, scroll_max[0]))
        if not _drag_render_due[0]:            # 合并同一帧内的多次 Motion，只重绘一次
            _drag_render_due[0] = True

            def _do():
                _drag_render_due[0] = False
                try:
                    render()
                except Exception:
                    pass
            canvas.after_idle(_do)


def _on_release(event):
    if _drag[2]:
        return                                 # 拖拽滚动结束：不算点击
    items = canvas.find_withtag("current")
    if not items:
        return
    tags = canvas.gettags(items[0])
    if "gear" in tags:
        open_menu(event)
        return
    for t in tags:
        if t in state and state[t]["messages"]:
            activate_card(t)
            return


canvas.bind("<ButtonPress-1>", _on_press)
canvas.bind("<B1-Motion>", _on_motion)
canvas.bind("<ButtonRelease-1>", _on_release)


# ---------- 设置菜单（右键面板任意处，或点右下角"⚙ 设置"）----------
def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, "HSBoard")
            return True
    except Exception:
        return False


def set_autostart(on):
    try:
        import winreg
        if on:
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable             # 无 pythonw 时退化为 python（有控制台）
            cmd = f'"{pythonw}" "{os.path.abspath(__file__)}"'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "HSBoard", 0, winreg.REG_SZ, cmd)
            print(f"[自启] 开机自启已开启: {cmd}")
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as k:
                try:
                    winreg.DeleteValue(k, "HSBoard")
                except FileNotFoundError:
                    pass
            print("[自启] 开机自启已关闭")
    except Exception as e:
        print(f"[自启] 设置失败: {e}")


def resample_background():
    """重新采样壁纸生成磨砂背景（换壁纸/背景过时后用）。
    采样期间先隐藏窗口，避免把面板自己抓进背景图。"""
    global WALL_BG
    target = pinned_hwnd[0] or hwnd
    try:
        _user32.ShowWindow(target, 0)                # SW_HIDE
        root.update_idletasks()
        time.sleep(0.35)                             # 等桌面重绘出干净壁纸
        WALL_BG = make_wall_bg()
        print("[玻璃] 背景已重新采样")
    except Exception as e:
        print(f"[玻璃] 重新采样失败: {e}")
    finally:
        _user32.ShowWindow(target, 5)                # SW_SHOW
        render()


def move_to_corner(name):
    """位置预设：屏幕四角（保存进设置文件，重启仍生效）"""
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    x = (sw - W - Z(24)) if "右" in name else Z(24)
    y = Z(24) if "上" in name else max(Z(24), sh - H - Z(56))
    root.geometry(f"{W}x{H}+{x}+{y}")
    settings["pos"] = [int(x / S), int(y / S)]
    save_settings()
    resample_background()                            # 位置变了壁纸也变了，需重采样
    print(f"[位置] 已移到{name}并保存")


def set_max_lines(n):
    settings["max_lines"] = n
    save_settings()
    render()
    print(f"[设置] 每条留言最多显示 {'完整内容' if n == 0 else f'{n} 行'}")


def set_scale(p):
    """缩放档位（0.25/0.5/0.75/1.0）：整窗尺寸/字号/磨砂背景全部按比例重算，持久化"""
    settings["scale"] = p
    save_settings()
    UI_SCALE[0] = p
    scroll_y[0] = 0
    recompute_metrics()
    resample_background()                      # 尺寸变了壁纸区域也变了：隐藏->重采样->显示->render
    print(f"[设置] 缩放比例已设为 {int(p * 100)}%")


def set_density(k):
    """卡片密度档：cozy 标准 / compact 紧凑单行 / grid 双列网格；切换立即生效并持久化"""
    settings["density"] = k
    save_settings()
    scroll_y[0] = 0
    render()
    print(f"[设置] 卡片密度已切换为 {DENSITY_LABEL.get(k, k)}")


def set_class(c):
    """切换班级：持久化后自动重启进程（新班级名单 + 新 topic 前缀生效）"""
    if c["id"] == CLASS_ID:
        return
    settings["class"] = c["id"]
    save_settings()
    print(f"[班级] 切换到 {c['name']}（{c['id']}），正在重启…")
    restart_process()


def restart_process():
    """切班自动重启：先断 MQTT（防旧连接半开）+ 释放单实例锁，再替换进程映像。
    失败兜底：弹窗提示手动重启（设置已保存，手动重开即生效）。"""
    try:
        client.disconnect()
        client.loop_stop()
    except Exception:
        pass
    try:
        windll.kernel32.ReleaseMutex(_hmutex)
        windll.kernel32.CloseHandle(_hmutex)
    except Exception:
        pass
    save_settings()
    args = [sys.executable] + ([] if getattr(sys, "frozen", False) else sys.argv[1:])
    try:
        os.execv(sys.executable, args)         # Windows 上等效于重启进程
    except Exception as e:
        try:
            from tkinter import messagebox
            messagebox.showerror("切换班级", f"自动重启失败（{e}）\n请手动关闭留言板后重新打开（班级已保存）")
        except Exception:
            print(f"[班级] 自动重启失败: {e}")


menu = tk.Menu(root, tearoff=0)
class_menu = tk.Menu(menu, tearoff=0)
for _c in CLASSES:
    class_menu.add_command(label=("✔ " if _c["id"] == CLASS_ID else "") + _c["name"],
                           command=lambda c=_c: set_class(c))
menu.add_cascade(label="班级", menu=class_menu)
pos_menu = tk.Menu(menu, tearoff=0)
for _name in ("右上角", "左上角", "右下角", "左下角"):
    pos_menu.add_command(label=_name, command=lambda n=_name: move_to_corner(n))
menu.add_cascade(label="位置", menu=pos_menu)
line_menu = tk.Menu(menu, tearoff=0)
for _label, _n in (("每条 1 行", 1), ("每条 2 行", 2), ("每条 3 行", 3), ("完整显示（自动换行）", 0)):
    line_menu.add_command(label=_label, command=lambda n=_n: set_max_lines(n))
menu.add_cascade(label="留言显示长度", menu=line_menu)
scale_menu = tk.Menu(menu, tearoff=0)
for _label, _p in (("25%", 0.25), ("50%", 0.5), ("75%", 0.75), ("100%", 1.0)):
    scale_menu.add_command(label=_label, command=lambda q=_p: set_scale(q))
menu.add_cascade(label="缩放比例", menu=scale_menu)
dens_menu = tk.Menu(menu, tearoff=0)
for _label, _k in (("标准（单列大卡）", "cozy"), ("紧凑（单列单行）", "compact"),
                   ("双列网格（一屏最多）", "grid")):
    dens_menu.add_command(label=_label, command=lambda k=_k: set_density(k))
menu.add_cascade(label="卡片密度", menu=dens_menu)
_auto_var = tk.BooleanVar(value=autostart_enabled())
menu.add_checkbutton(label="开机自启", variable=_auto_var,
                     command=lambda: set_autostart(_auto_var.get()))
menu.add_command(label="重新获取背景图", command=resample_background)
menu.add_separator()
menu.add_command(label="退出", command=root.destroy)


def open_menu(event):
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()


canvas.bind("<Button-3>", open_menu)           # 左键（含触控）统一走 _on_release 判定


def on_wheel(event):
    """滚轮滚动卡片列表：上滚看上方内容，下滚看下方（每次约 64 逻辑像素）"""
    d = -1 if event.delta > 0 else 1
    scroll_y[0] = max(0, min(scroll_y[0] + d * Z(64), scroll_max[0]))
    try:
        render()
    except Exception:
        pass


canvas.bind("<MouseWheel>", on_wheel)


def _self_visible():
    """可见性自检：屏幕上自己矩形区域的空白磨砂区 vs 底图基准。
    返回 True=正常显示 / False=被桌面层异常覆盖 / None=被应用正常遮挡或采样失败。"""
    if not WALL_BG_PIL[0]:
        return None
    try:
        from PIL import ImageChops
        x, y = root.winfo_rootx(), root.winfo_rooty()
        # 空白磨砂区：面板中部偏右（避开标题/卡片/文字）
        sx, sy = int(W * 0.60), int(H * 0.42)
        ex, ey = int(W * 0.94), int(H * 0.60)
        grab = ImageGrab.grab(bbox=(x + sx, y + sy, x + ex, y + ey)).convert("RGB")
        ref = WALL_BG_PIL[0].crop((sx, sy, ex, ey)).convert("RGB")
        diff = ImageChops.difference(grab, ref).resize((1, 1))
        d = sum(diff.getpixel((0, 0)))
        # 命中测试：中心点被普通应用窗口命中 = 正常遮挡，不算异常；
        # 命中自己或 Shell 桌面族（图标层异常重排）则继续做像素判别
        r = wintypes.RECT()
        _user32.GetWindowRect(hwnd, byref(r))
        hit = _user32.WindowFromPoint(wintypes.POINT((r.left + r.right) // 2,
                                                     (r.top + r.bottom) // 2))
        if hit:
            hit_top = _user32.GetAncestor(hit, 2)
            my_top = _user32.GetAncestor(hwnd, 2)
            if hit_top != my_top:
                b = ctypes.create_unicode_buffer(64)
                _user32.GetClassNameW(hit_top, b, 64)
                if b.value not in ("WorkerW", "Progman", "SHELLDLL_DefView",
                                   "SysListView32", "DirectUIHWND"):
                    return None             # 普通应用盖住：桌面层正常语义
        return d < 60                       # 差异小 = 磨砂面确实画在屏幕上
    except Exception:
        return None


def _is_cloaked(w):
    """UWP/沉浸式应用被 DWM cloaked 时视觉上不可见，枚举桌面状态时需排除。"""
    try:
        val = wintypes.DWORD()
        windll.dwmapi.DwmGetWindowAttribute(w, 14, byref(val), ctypes.sizeof(val))  # DWMWA_CLOAKED
        return val.value != 0
    except Exception:
        return False


def _desktop_has_apps():
    """桌面是否处于"还原"状态：存在可见、未最小化、非 cloaked 的普通应用窗口。
    Win+D（显示桌面）状态下返回 False；任何枚举异常按 True 处理（走保守老逻辑）。"""
    try:
        my_top = pinned_hwnd[0]
        proxy = PROXY[0]
        w = _user32.GetWindow(_user32.GetDesktopWindow(), 5)       # GW_CHILD，z 顶向下
        n = 0
        while w and n < 512:                                       # 上限防 z 栈成环
            n += 1
            w2 = _user32.GetWindow(w, 2)                           # 先取 NEXT，防枚举中 z 变化
            if w in (my_top, proxy) or not _user32.IsWindow(w) \
                    or not _user32.IsWindowVisible(w) or _user32.IsIconic(w):
                w = w2
                continue
            ex = _user32.GetWindowLongPtrW(w, GWL_EXSTYLE) & 0xFFFFFFFF
            if ex & (0x00000008 | 0x00000080):                     # WS_EX_TOPMOST / TOOLWINDOW 豁免族
                w = w2
                continue
            b = ctypes.create_unicode_buffer(64)
            _user32.GetClassNameW(w, b, 64)
            if b.value in ("Progman", "WorkerW", "Shell_TrayWnd",
                           "Shell_SecondaryTrayWnd", "SHELLDLL_DefView",
                           "SysListView32", "DirectUIHWND"):
                w = w2
                continue
            if not _is_cloaked(w):
                return True
            w = w2
        return False
    except Exception:
        return True


_float = [False, 0]                # [浮动模式(置顶保活), 连续归位试验可见计数]
_invis_n = [0]                     # 连续不可见计数


def _float_tick():
    """浮动模式：置顶保活；桌面还原（有普通应用在前台）时做归位试验，
    连续 2 次试验可见才退出浮动；显示桌面（Win+D）期间纯置顶挂着、绝不归位。
    （关键修正：归位 SetWindowPos 会顺带清掉 TOPMOST，显示桌面状态下归位必然
    落进死位=消失，这正是旧版"出现一下又消失"震荡的根因。）"""
    try:
        render()
        hwnd = pinned_hwnd[0]
        if hwnd and _user32.IsWindow(hwnd):
            _user32.InvalidateRect(hwnd, None, False)
        if _desktop_has_apps():
            ok, _info = dock_to_desktop(root)      # 试验性归位（自动退出 TOPMOST band）
            vis = _self_visible()
            # True=像素可见 / None=被普通应用正常遮挡（停靠语义正确，也算成功）
            # 只有 False（像素异常且命中桌面族）才算归位失败
            if vis is False:
                _float[1] = 0
                if hwnd and _user32.IsWindow(hwnd):
                    _user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0,   # HWND_TOPMOST（64 位安全）
                                         0x1 | 0x2 | 0x10)                      # NOSIZE|NOMOVE|NOACTIVATE
                    _user32.InvalidateRect(hwnd, None, False)
                    print("[桌面] 归位试验失败（仍被桌面层覆盖），已恢复置顶")
            else:
                _float[1] += 1
                if _float[1] >= 2:
                    _float[0] = False
                    _float[1] = 0
                    root.attributes("-topmost", False)   # 清 TOPMOST，防止面板残留置顶盖住应用
                    print("[桌面] 停靠已稳定，退出浮动模式")
                    root.after(1000, tick)
                    return
        else:
            _float[1] = 0
            if hwnd and _user32.IsWindow(hwnd):
                # 显示桌面期间确保置顶仍在（防外部清除），面板在桌面上始终可见
                if not (_user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0x00000008):
                    _user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0,
                                         0x1 | 0x2 | 0x10)
                    _user32.InvalidateRect(hwnd, None, False)
    except Exception as e:
        print(f"[桌面] 浮动保活异常: {e}")
    root.after(1000, _float_tick)


def _enter_float():
    _float[0] = True
    _float[1] = 0
    _invis_n[0] = 0
    root.attributes("-topmost", True)
    root.update_idletasks()
    render()
    print("[桌面] 桌面层合成异常，进入浮动模式（置顶保活，自动尝试归位）")
    _float_tick()


def daily_reset():
    """每日零点（运行中跨天触发）：清空本地留言显示。
    注意：不再清服务端 retained——
    - hist 快照由家长端上传切片自收敛（当天+未读7天内），且清掉会毁掉"昨晚发、教室没开机"的未读找回途径；
    - ack retained 是挡住已读旧消息恢复的关键数据，清掉后旧消息会死灰复燃；
    - 本程序跨零点常不在运行（教室电脑每天关机），靠清 retained 兜底不可靠，
      生命周期交还给数据的生产方（家长端切片 + ack 累计）才是正解。"""
    state.clear()
    expanded.clear()
    read_pending.clear()
    scroll_y[0] = 0
    print("[清空] 跨天重置：本地留言显示已清（服务器 retained 保留：hist 自收敛 / ack 挡旧消息）")
    evt("DAILY_RESET", f"跨天重置 date={datetime.now().strftime('%Y-%m-%d')} 清本地显示，保留服务端 retained")


def tick():
    today = datetime.now().strftime("%Y%m%d")
    if today != _date_str[0]:                  # 跨天：整点清空（无论几点启动都按自然日）
        _date_str[0] = today
        try:
            daily_reset()
        except Exception as e:
            print(f"[清空] 每日重置异常: {e}")
    now_ts = time.time()
    for _k in [k for k, v in read_pending.items() if v <= now_ts]:
        read_pending.pop(_k, None)             # 清理已到期的"延迟移区"记录（render 判定同样按到期时间）
    # 每秒整帧重绘：
    # 1) 时钟走时；2) Win+D / 桌面切换动画后 DWM 会丢掉 Tk 子窗口内容（"空缺"），
    #    每秒全量重画即可在 1 秒内自动恢复显示；3) 桌面层停靠保活 + 可见性自检。
    try:
        render()
    except Exception as e:
        print(f"[UI] 渲染异常: {e}")       # tick 绝不能死，死了保活就全停了
    if pinned_hwnd[0]:
        try:
            hwnd = pinned_hwnd[0]
            if not _user32.IsWindow(hwnd):
                # Tk 偶发重建窗口导致句柄失效：重新取当前句柄
                hwnd = _user32.GetAncestor(root.winfo_id(), 2) or int(root.wm_frame(), 16)
                pinned_hwnd[0] = hwnd
                print(f"[桌面] 窗口句柄已刷新: {hwnd}")
            if _user32.IsIconic(hwnd):
                _user32.ShowWindow(hwnd, 9)            # SW_RESTORE：Win+D 若把窗口收进最小化，立即还原
                print("[桌面] 看门狗: 窗口被最小化，已还原")
            elif not _user32.IsWindowVisible(hwnd):
                _user32.ShowWindow(hwnd, 9)
                print("[桌面] 看门狗: 窗口被隐藏，已还原")
            elif not _float[0]:
                has_apps = _desktop_has_apps()
                if has_apps:
                    ok, info = dock_to_desktop(root)    # 只在桌面还原（有普通应用）时归位
                    if not ok:
                        print(f"[桌面] 停靠失败: {info}")
                # 关键：Win+D/桌面切换会让 DWM 丢弃窗口重定向表面（表面空=整窗透明），
                # 而 Tk 自认为无需重绘不会恢复。每秒强制整窗失效，逼 Tk 重建表面内容。
                _user32.InvalidateRect(hwnd, None, False)
                # 可见性自检：显示桌面(Win+D)是预期状态，1 秒内进浮动置顶（快速反应）；
                # 桌面还原状态下连续 3 秒不可见（Shell 图标层重排等异常）才进浮动
                vis = _self_visible()
                if not has_apps and vis is None:
                    vis = False         # 显示桌面时无正常遮挡可言，采样失败按异常覆盖兜底
                _invis_n[0] = _invis_n[0] + 1 if vis is False else 0
                if _invis_n[0] >= (1 if not has_apps else 3):
                    _enter_float()
                    return
        except Exception as e:
            print(f"[桌面] 保活异常: {e}")
    root.after(1000, tick)


def take_shot():
    try:
        from PIL import ImageGrab
        # 进程已 DPI 感知：winfo 与 ImageGrab 均为物理像素
        x, y = root.winfo_rootx(), root.winfo_rooty()
        img = ImageGrab.grab(bbox=(x, y, x + W, y + H))
        img.save(shot_path[0])
        print(f"[截图] 已保存 {shot_path[0]}")
    except Exception as e:
        print(f"[截图] 失败: {e}")


def poll_queue():
    try:
        while True:
            kind, _arg = ui_queue.get_nowait()
            if kind == "shot":
                take_shot()
            else:
                try:
                    render()
                except Exception as e:
                    print(f"[UI] 渲染异常: {e}")
    except queue.Empty:
        pass
    if _hist_pending and time.time() >= _hist_deadline[0]:
        try:
            _flush_hist_pending()
        except Exception as e:
            print(f"[恢复] 批量恢复异常: {e}")
    root.after(120, poll_queue)


root.bind("<Escape>", lambda e: root.destroy())
if duration:
    root.after(duration * 1000, root.destroy)

# 窗口显示前生成壁纸采样磨砂背景（此刻抓屏干净，没有被自己遮挡）
try:
    WALL_BG = make_wall_bg()
    print("[玻璃] 壁纸采样磨砂背景: 已生成（圆角+磨砂）")
except Exception as e:
    WALL_BG = None
    print(f"[玻璃] 壁纸采样失败({e})，退回纯色面板")

render()
tick()
poll_queue()
root.update()

hwnd = _user32.GetAncestor(root.winfo_id(), 2) or int(root.wm_frame(), 16)   # GA_ROOT 取顶层包装
print(f"[玻璃] 亚克力模糊: {'已启用' if (not pin_enabled or use_acrylic) and apply_acrylic(hwnd) else '关闭（--acrylic 可开）'}")

_hit_keep = [None]                 # 必须持有 WNDPROC 回调引用，被 GC 后消息循环会崩溃(0xC000041D)


def install_hit_test(hwnd):
    """圆角外四个小角区域点击穿透（WM_NCHITTEST -> HTTRANSPARENT），
    让角落下的桌面图标可以被点到。子类化窗口过程，异常时静默放弃。"""
    try:
        WNDPROC = WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, ctypes.c_uint,
                              ctypes.c_ulonglong, ctypes.c_longlong)

        def proc(h, msg, wp, lp):
            if msg == 0x0084:                       # WM_NCHITTEST
                try:
                    x = ctypes.c_short(lp & 0xFFFF).value
                    y = ctypes.c_short((lp >> 16) & 0xFFFF).value
                    pt = wintypes.POINT(x, y)
                    _user32.ScreenToClient(h, byref(pt))
                    px, py = pt.x, pt.y
                    rr = R
                    for cx, cy in ((rr, rr), (W - rr, rr), (rr, H - rr), (W - rr, H - rr)):
                        if (px < rr or px > W - rr) and (py < rr or py > H - rr):
                            dx, dy = px - cx, py - cy
                            if dx * dx + dy * dy > rr * rr:
                                return -1           # HTTRANSPARENT
                            break
                except Exception:
                    pass
            return _user32.CallWindowProcW(_hit_keep[0]["old"], h, msg, wp, lp)

        _user32.GetWindowLongPtrW.restype = ctypes.c_longlong
        _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        _user32.SetWindowLongPtrW.restype = ctypes.c_longlong
        _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
        _user32.CallWindowProcW.restype = ctypes.c_longlong
        _user32.CallWindowProcW.argtypes = [ctypes.c_longlong, wintypes.HWND, ctypes.c_uint,
                                            ctypes.c_ulonglong, ctypes.c_longlong]
        cb = WNDPROC(proc)                                           # 注册进窗口的回调实例
        proc_addr = ctypes.cast(cb, ctypes.c_void_p).value
        old = _user32.SetWindowLongPtrW(hwnd, -4, proc_addr)         # GWLP_WNDPROC
        _hit_keep[0] = {"old": old, "cb": cb}   # 必须持有"注册的那个"实例，临时对象被 GC 会致崩溃
        print("[玻璃] 圆角外点击穿透: 已启用")
    except Exception as e:
        print(f"[玻璃] 点击穿透未启用: {e}")


if pin_enabled:
    ok, info = dock_to_desktop(root)
    print("[桌面] " + (f"{info}，Win+D 不隐藏（工具窗口豁免）；普通应用窗口可盖住本面板"
                       if ok else f"停靠失败({info})，改用普通窗口"))
    if ok:
        pinned_hwnd[0] = hwnd       # 开启每秒停靠保活（防新开窗口插入桌面上方）
        install_hit_test(hwnd)
if "--topmost" in sys.argv:
    root.attributes("-topmost", True)
    print("[桌面] 测试模式：窗口置顶显示（未停靠桌面层）")
root.update()
if use_acrylic:
    apply_acrylic(hwnd)  # 特效补一次，防样式修改后丢失
print(f"[圆角] DWM 系统圆角: {'已启用' if apply_dwm_round(hwnd) else '不可用（直角面板）'}")
render()                 # 改样式会丢掉画布内容，强制重画一遍
root.update_idletasks()
root.update()
if use_acrylic:
    apply_acrylic(hwnd)

try:
    root.mainloop()
finally:
    client.disconnect()
    print("[退出] 桌面留言板已关闭")
