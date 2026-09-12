# hsboard-updates

教室端自动更新仓库。

- `update.json`：客户端开机时拉取的版本清单
- Releases 附件：`board_v{version}.zip`（含 exe + config + 使用说明）

## 发布流程

1. PyInstaller 打包 → `dist/board.exe`
2. `python tools/publish_board.py --dist dist --owner dgkad --repo hsboard-updates --version X.Y.Z --changelog "..."`
3. `gh release create vX.Y.Z pc/board_vX.Y.Z.zip -t "教室端 vX.Y.Z" -F changelog.txt`
4. 推送 `update.json` 到 main 分支
