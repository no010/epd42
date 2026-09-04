# EPD42 番茄钟 桌面版（Tauri v2）

TS 前端绘制 400x300 沙漏画面并计时，Rust 侧负责 EPD42 的 BLE 流式推送和系统
通知。用户拿到的是**一个轻量桌面程序**（Windows 下几 MB 到十几 MB 的 exe，
无需装 Python），这是对 Python 命令行版 `tools/epd-pomodoro` 的"部署轻量化"
回答，也补齐了 GUI/通知体验。

```
web/   (TypeScript, 香草实现无框架)
  计时状态机   timer.ts   —— 与 tools/epd-pomodoro/state.py 同逻辑
  沙漏画面     face.ts    —— 与 face.py 同布局（含两处沙漏几何修正）
  界面/推送    main.ts    —— 调用 Rust 命令
src-tauri/ (Rust)
  core/   (epd42-core 独立 crate)
     位打包 / PackBits / 分帧 / STREAM_END 组包 —— 与 epd-monitor 协议一致，7 个单测
  ble.rs   btleplug 流式推送：BEGIN → 数据分块 → END，按设备通知做流控
  commands.rs   scan_devices / push_frame / notify 三个 Tauri 命令
```

## 构建与运行

前置：Rust（MSVC target）、Node.js（仅前端编译）。

```bash
cd desktop

# 后端检查/测试（离线）
cargo check -p epd42-pomodoro
cargo test  -p epd42-core

# 前端编译（生成 web/dist）
cd web && npm install && npm run build && cd ..

# 完成
# 方式一：tauri CLI（推荐）
cargo install tauri-cli --locked
cargo tauri dev          # 开发调试
cargo tauri build        # 产出安装包（Windows 为 NSIS/exe + MSI）
# 方式二：只编译 exe
cd src-tauri && cargo build --release
```

说明：

* `frontendDist` 指向 `web/dist`，安装包里只带编译后的前端，node_modules 不会
  被打进包。
* 首次连墨水屏：点"扫描"选择 NRF_EPD 设备（地址会记住）；也可以不选，推送时
  自动找广播名为 `NRF_EPD_*` 的设备。
* 蓝牙命令（扫描/推送）需要 `bleak` 时代的对应 Rust 依赖已内置（btleplug），
  用户侧零额外依赖。

## 与 Python 版的分工

| | Python CLI (`tools/epd-pomodoro`) | 桌面版 (`desktop/`) |
|---|---|---|
| 用途 | 终端/脚本/cron，轻量单文件分发 | 常驻桌面、托盘通知、GUI |
| 协议 | 同一套（bit 1=白、MSB 左、PackBits、20B 分包） | 与 `epd42-core` 单测锁定 |
| 部署 | Python + Pillow（可选 bleak） | 免 Python，一个安装包 |

## 已知限制 / 后续

* 沙漏画面与 Python 版同布局，但前端是独立实现，属跨语言复刻；
  如需严格一致可后续把 face 生成挪到 Rust 共用。
* 托盘图标、窗口最小化到托盘、开机自启尚未实现（下一步）。
* 推送节奏沿用"每 3 分钟一次／切换时一次"的墨水屏习惯。