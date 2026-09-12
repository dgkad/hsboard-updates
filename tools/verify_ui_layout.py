# -*- coding: utf-8 -*-
"""
教室端 UI 布局自动化验证（未读/已读分区 + 位置提示 + 滚动条 + 缩放）。

默认演示模式（零网络，随时可跑）：
    python tools/verify_ui_layout.py
  以 HSBOARD_DEMO_STATE=1 启动 board.py（本地演示数据、不连 Broker），
  解析 [布局] 结构化行，断言：
    1) 未读区在上/已读区在下，两区各自按最后消息时间降序（first_unread/first_read）
    2) 未读 8 位 / 已读 8 位（与演示数据一致）
    3) 内容溢出 -> scroll_max>0，滚动条 thumb 数值合法
    4) 视口下方未读计数 below_unread 只统计未读消息（<= 演示未读总数 15）
    5) 缩放 --scale 0.25/0.5 生效（W 按比例变化）

联网模式（会向 hsdemo/msg/zztXX 假学号发测试消息，务必在教室端未运行时使用）：
    python tools/verify_ui_layout.py --live
  用独立 client_id（HSBOARD_CLIENT_ID）启动 board.py 并发布 6 条假学号留言，
  断言 unread_zone>=6；结束后保留（消息非 retained，教室端每日清空自愈）。

人工/真机项（本脚本不覆盖）：希沃触屏滑动、点击 10 秒延迟移区、菜单切缩放档。
"""
import json
import os
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, "pc", "board.py")
UNREAD_DEMO, READ_DEMO = 8, 8
DEMO_UNREAD_TOTAL = 15
W50_REF = int(400 * 1.75 * 0.5)                # 本机 175% DPI 下 50% 档的 W 基准
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def run_board(args, extra_env, seconds):
    env = dict(os.environ, HSBOARD_DEBUG_LAYOUT="1", PYTHONUNBUFFERED="1", **extra_env)
    p = subprocess.Popen([sys.executable, BOARD] + args,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", env=env)
    time.sleep(seconds)
    p.terminate()
    out, _ = p.communicate(timeout=15)
    lines = re.findall(r"\[布局\] (\S.*)", out or "")
    kv = {}
    if lines:
        for pair in lines[-1].split():
            k, _, v = pair.partition("=")
            kv[k] = v
    return lines, kv


def demo_mode():
    print("[1/3] 演示模式（标准档）：分区/排序/提示/滚动条")
    lines, kv = run_board(["--no-pin", "--topmost", "--pos", "150", "150", "--duration", "8"],
                          {"HSBOARD_DEMO_STATE": "1", "HSBOARD_DENSITY": "cozy"}, 6.5)
    check("布局日志已输出", bool(lines), f"共 {len(lines)} 行")
    if not kv:
        return
    check("density 字段输出", kv.get("density") == "cozy", kv.get("density", "?"))
    check("未读区 8 位", kv.get("unread_zone") == str(UNREAD_DEMO), kv.get("unread_zone", "?"))
    check("已读区 8 位", kv.get("read_zone") == str(READ_DEMO), kv.get("read_zone", "?"))
    check("未读区在最上且最新最顶", kv.get("first_unread") == "demo00", kv.get("first_unread", "?"))
    check("已读区最早在最顶（升序：先发在上后发在下）", kv.get("first_read") == "demo15", kv.get("first_read", "?"))
    check("内容溢出可滚动", int(kv.get("scroll_max", "0")) > 0, kv.get("scroll_max", "?"))
    check("视口上方无卡片（滚动在顶部）", kv.get("above_n") == "0", kv.get("above_n", "?"))
    check("下方未读计数合法（仅未读）",
          0 <= int(kv.get("below_unread", "-1")) <= DEMO_UNREAD_TOTAL, kv.get("below_unread", "?"))
    ty, th = kv.get("thumb", "(0,0)").strip("()").split(",")
    check("滚动条 thumb 合法", int(ty) >= 0 and int(th) > 0, kv.get("thumb", "?"))


def scale_mode():
    print("[2/3] 缩放：--scale 0.5 / 0.25 尺寸族随比例变化")
    _, kv50 = run_board(["--no-pin", "--topmost", "--pos", "150", "150", "--duration", "6",
                         "--scale", "0.5"],
                        {"HSBOARD_DEMO_STATE": "1", "HSBOARD_DENSITY": "cozy"}, 5)
    _, kv25 = run_board(["--no-pin", "--topmost", "--pos", "150", "150", "--duration", "6",
                         "--scale", "0.25"],
                        {"HSBOARD_DEMO_STATE": "1", "HSBOARD_DENSITY": "cozy"}, 5)
    if kv50 and kv25:
        check("50% 档 W 减半", abs(int(kv50.get("W", "0")) - W50_REF) <= 4, kv50.get("W", "?"))
        check("25% 档 W 为 1/4", abs(int(kv25.get("W", "0")) - W50_REF // 2) <= 4, kv25.get("W", "?"))
        check("50% 档仍可滚动布局", int(kv50.get("scroll_max", "0")) > 0, kv50.get("scroll_max", "?"))
    else:
        check("缩放布局日志缺失", False, "kv50/kv25 为空")


def scrolled_mode():
    print("[2b/3] 滚到底：上方提示 / thumb 位置 / 下方未读清零")
    _, kv = run_board(["--no-pin", "--topmost", "--pos", "150", "150", "--duration", "6"],
                      {"HSBOARD_DEMO_STATE": "bottom", "HSBOARD_DENSITY": "cozy"}, 5)
    if not kv:
        check("滚底布局日志缺失", False)
        return
    check("视口上方有卡片", int(kv.get("above_n", "0")) >= 1, kv.get("above_n", "?"))
    check("滚到底后下方无未读", kv.get("below_unread") == "0", kv.get("below_unread", "?"))
    ty, th = kv.get("thumb", "(0,0)").strip("()").split(",")
    check("thumb 位于轨道下部", int(ty) > 0, f"thumb_y={ty}")
    check("分区排序不受滚动影响（未读最新最顶/已读最早最顶）",
      kv.get("first_unread") == "demo00" and kv.get("first_read") == "demo15",
      f"{kv.get('first_unread')}/{kv.get('first_read')}")


def density_mode():
    print("[2c/3] 三档密度：分区/排序不变，布局随档位（grid 默认档）")
    for d in ("cozy", "compact", "grid"):
        _, kv = run_board(["--no-pin", "--topmost", "--pos", "150", "150", "--duration", "6"],
                          {"HSBOARD_DEMO_STATE": "1", "HSBOARD_DENSITY": d}, 5)
        if not kv:
            check(f"[{d}] 布局日志缺失", False)
            continue
        check(f"[{d}] density 字段正确", kv.get("density") == d, kv.get("density", "?"))
        check(f"[{d}] 分区与排序不变",
              kv.get("unread_zone") == "8" and kv.get("read_zone") == "8"
              and kv.get("first_unread") == "demo00" and kv.get("first_read") == "demo15",
              f"{kv.get('unread_zone')}/{kv.get('read_zone')}/{kv.get('first_unread')}/{kv.get('first_read')}")
        check(f"[{d}] above_n=0（滚动在顶）", kv.get("above_n") == "0", kv.get("above_n", "?"))
        check(f"[{d}] below_unread 合法",
              0 <= int(kv.get("below_unread", "-1")) <= DEMO_UNREAD_TOTAL, kv.get("below_unread", "?"))
        if d != "grid":                        # grid 在大屏可能整屏放得下（scroll_max=0 属正常）
            check(f"[{d}] 内容溢出可滚动", int(kv.get("scroll_max", "0")) > 0, kv.get("scroll_max", "?"))


def live_mode():
    print("[3/3] 联网模式：假学号 zzt01-06 实时留言（请确认教室端未运行）")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from broker_config import load_broker
    import uuid
    import paho.mqtt.client as mqtt
    b = load_broker()
    cls = sys.argv[sys.argv.index("--class") + 1] if "--class" in sys.argv else "c8"
    env = {"HSBOARD_CLIENT_ID": "verify-ui-" + uuid.uuid4().hex[:6],
           "HSBOARD_CLASS": cls, "HSDEMO_PREFIX": f"hsdemo/{cls}/"}
    lines, kv = [], {}
    proc = subprocess.Popen(
        [sys.executable, BOARD, "--no-pin", "--topmost", "--pos", "150", "150", "--duration", "25"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ, HSBOARD_DEBUG_LAYOUT="1", **env))
    time.sleep(4)                              # 等 TLS 连接 + 订阅
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="verify-ui-pub-" + uuid.uuid4().hex[:6])
    c.tls_set()
    c.username_pw_set(b["username"], b["password"])
    c.connect(b["host"], int(b["mqtt_port"]), 30)
    c.loop_start()
    time.sleep(1.5)
    now = int(time.time() * 1000)
    pre = os.environ.get("HSDEMO_PREFIX", "hsdemo/c8/")     # 多班：环境变量切班级段
    for i in range(1, 7):
        c.publish(f"{pre}msg/zzt{i:02d}", json.dumps(
            {"msgId": f"vui-{now}-{i}", "sid": f"zzt{i:02d}",
             "text": f"布局验证测试留言 {i}（假学号，教室端每日清空自愈）",
             "ts": now - (7 - i) * 60000}), qos=1)
    deadline = time.time() + 14
    while time.time() < deadline:
        time.sleep(1)
        lines = re.findall(r"\[布局\] (\S.*)", _drain(proc))
        if lines:
            kv = dict(p.split("=", 1) for p in lines[-1].split() if "=" in p)
            if int(kv.get("unread_zone", "0")) >= 6:
                break
    _stop(proc, c)
    check("6 条实时留言全部进入未读区", int(kv.get("unread_zone", "0")) >= 6,
          kv.get("unread_zone", "?"))
    check("实时回执链路未破坏（无已读混入）", kv.get("read_zone") in ("0", None), kv.get("read_zone", "?"))


def _drain(proc):
    import threading
    if not hasattr(proc, "_buf"):
        proc._buf = []
        def _rd():
            for ln in iter(proc.stdout.readline, ""):
                proc._buf.append(ln)
        threading.Thread(target=_rd, daemon=True).start()
    return "".join(proc._buf)


def _stop(proc, client):
    try:
        proc.terminate()
        proc.communicate(timeout=10)
    except Exception:
        pass
    try:
        client.disconnect()
        client.loop_stop()
    except Exception:
        pass


if __name__ == "__main__":
    demo_mode()
    scale_mode()
    scrolled_mode()
    density_mode()
    if "--live" in sys.argv:
        live_mode()
    else:
        print("[3/3] 联网模式跳过（加 --live 启用；务必在教室端未运行时）")
    fails = [r for r in results if not r[1]]
    print(f"\n[结论] {'全部通过' if not fails else f'{len(fails)} 项失败'}"
          f"（共 {len(results)} 项断言）")
    sys.exit(1 if fails else 0)
