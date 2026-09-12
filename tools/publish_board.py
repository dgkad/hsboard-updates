# -*- coding: utf-8 -*-
"""
发布教室端更新包：把 PyInstaller 产物（班级留言板 目录）打成 zip，
算 sha256，生成可直接推 GitHub 的 update.json 模板。

用法：
    # 1) 先跑 PyInstaller 生成 dist/班级留言板/
    pyinstaller 班级留言板.spec
    # 2) 打包 + 生成 update.json
    python tools/publish_board.py --dist dist/班级留言板 \
        --owner OWNER --repo REPO --version 1.0.1 \
        --changelog "修复紧急通知降级回执"
    # 3) 把 update.json 推到 GitHub 仓库 main 分支 + 把 zip 传到 Release，
    #    教室端下次开机拉 update.json 比版本，自动下载替换重启。

产物：
    dist/班级留言板_v{version}.zip       上传到 GitHub Release 当附件
    dist/update.json                      推到仓库 main 分支（update_url 指向它）
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="发布教室端更新包 + 生成 update.json")
    ap.add_argument("--dist", default="dist/班级留言板",
                    help="PyInstaller 输出目录（含 班级留言板.exe）")
    ap.add_argument("--owner", required=True, help="GitHub 仓库 owner")
    ap.add_argument("--repo", required=True, help="GitHub 仓库名")
    ap.add_argument("--version", required=True, help="语义化版本，如 1.0.1")
    ap.add_argument("--changelog", default="", help="本次更新说明（写进 update.json）")
    ap.add_argument("--release", default="main",
                    help="Release tag 前缀段，默认 main（tag 为 v{version}）")
    args = ap.parse_args()

    dist_dir = args.dist
    if not os.path.isdir(dist_dir):
        print(f"[错误] 找不到 PyInstaller 输出目录: {dist_dir}\n"
              f"        请先运行: pyinstaller 班级留言板.spec")
        sys.exit(1)

    exe = None
    for fn in os.listdir(dist_dir):
        if fn.lower().endswith(".exe"):
            exe = fn
            break
    if not exe:
        print(f"[错误] {dist_dir} 下没找到 .exe（PyInstaller 输出异常）")
        sys.exit(1)
    print(f"[打包] 主 exe: {exe}")

    base = os.path.dirname(dist_dir.rstrip("/"))
    
    # 0) 把 config/ + 使用说明.txt 整理进外层的 班级留言板-PC/ 目录
    bundle_dir = os.path.join(base, f"班级留言板-PC")
    if os.path.isdir(bundle_dir):
        shutil.rmtree(bundle_dir, ignore_errors=True)
    os.makedirs(bundle_dir, exist_ok=True)

    # 复制 exe + _internal
    for item in os.listdir(dist_dir):
        sp = os.path.join(dist_dir, item)
        dp = os.path.join(bundle_dir, item)
        if os.path.isdir(sp):
            shutil.copytree(sp, dp, dirs_exist_ok=True)
        else:
            shutil.copy2(sp, dp)
    print(f"[打包] 已复制 exe + _internal → {bundle_dir}")

    # 复制 config/（递归复制子目录，跳过被锁文件）
    src_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config")
    dst_config = os.path.join(bundle_dir, "config")
    if os.path.isdir(src_config):
        copied = 0
        skipped = 0
        for root, dirs, files in os.walk(src_config):
            rel = os.path.relpath(root, src_config)
            target_dir = os.path.join(dst_config, rel) if rel != "." else dst_config
            os.makedirs(target_dir, exist_ok=True)
            for fn in files:
                sp = os.path.join(root, fn)
                dp = os.path.join(target_dir, fn)
                try:
                    shutil.copy2(sp, dp)
                    copied += 1
                except PermissionError:
                    skipped += 1
        print(f"[打包] 已复制 config/ → {dst_config}（{copied} 个文件，{skipped} 个跳过）")

    # 复制 使用说明.txt
    readme_src = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "docs", "使用说明-教室端.txt"
    )
    readme_dst = os.path.join(bundle_dir, "使用说明.txt")
    if os.path.isfile(readme_src):
        shutil.copy2(readme_src, readme_dst)
        print(f"[打包] 已复制 使用说明.txt → {readme_dst}")

    # 1) 打 zip（整个 班级留言板-PC/ 目录）
    zipname = f"board_v{args.version}"
    zip_path = os.path.join(base, f"{zipname}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    shutil.make_archive(zip_path[:-4], "zip", root_dir=base, base_dir="班级留言板-PC")
    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"[打包] 已生成 {zip_path}（{size_mb:.1f} MB）")

    sha = sha256_of(zip_path)
    print(f"[sha256] {sha}")

    # 2) 生成 update.json（指向 GitHub Release 附件的 raw 直链）
    release_asset = f"{zipname}.zip"
    download_url = (f"https://github.com/{args.owner}/{args.repo}"
                    f"/releases/download/v{args.version}/{release_asset}")
    update_json = {
        "version": args.version,
        "channel": "stable",
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "release_zip": download_url,
        "sha256": sha,
        "changelog": args.changelog,
        # 客户端实际用的 update.json 地址（写进 config/update_url.json 或代码默认值）
        "self_url": (f"https://raw.githubusercontent.com/{args.owner}/{args.repo}"
                     f"/main/update.json"),
    }
    out_json = os.path.join(base, "update.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(update_json, f, ensure_ascii=False, indent=2)
    print(f"[update.json] 已生成 {out_json}")

    print("\n=== 下一步（人工）===")
    print(f"1. 上传 zip 附件：{zip_path}  ->  GitHub Release tag v{args.version}")
    print(f"2. 推送 update.json 到 {args.owner}/{args.repo} main 分支")
    print(f"3. 教室端开机拉 {update_json['self_url']} 自动比版本 -> 下载 -> 替换 -> 重启")
    print("\n建议把这段写进 GitHub Actions（push 到 main 且带 release 标签时自动发包），")
    print("或本地每次手动跑本脚本 + gh 上传，见 tools/publish_board_workflow.yml 示例。")
    print(f"   gh release create v{args.version} {zip_path} -t '教室端更新 v{args.version}' "
          f"-F changelog.txt")


if __name__ == "__main__":
    main()
