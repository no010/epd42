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

前置：Rust（MSVC target）、Node.js（前端编译 + tauri CLI）。**所有构建命令都在
`desktop/` 目录执行**（`beforeBuildCommand` 的路径按该目录解析）。

```bash
cd desktop
npm install                # 装 @tauri-apps/cli（预编译二进制，不用从源码编）

# 后端检查/测试（离线）
cargo check -p epd42-pomodoro
cargo test  -p epd42-core

# 出安装包 / exe（自动先编译前端 web/dist）
npm run build              # 等价 tauri build（Windows 默认产出 NSIS 安装包）
# 或只出裸 exe：
npx tauri build --no-bundle
```

产物位置：

* `target/release/epd42-pomodoro.exe` —— 免安装绿色版（~10MB）
* `target/release/bundle/nsis/epd42-pomodoro_0.1.0_x64-setup.exe` —— NSIS 安装包
* 开发调试：`npm run dev`（等价 `tauri dev`）

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

## IPC 命名约定（防回归）

Rust ↔ 前端跨语言传值有**两种驼峰转换机制**，历史上各踩过坑，改接口时务必对照：

1. **Tauri 命令参数**（`invoke(...)` 的 key）：Tauri 自动把 Rust 蛇形参数转成
   驼峰，前端必须发**同名驼峰键**，否则报 `missing required key ...`。
   - `scan_devices(timeout_secs)` → 前端发 `{ timeoutSecs }`
   - `push_frame(..., scan_timeout_secs)` → 前端发 `{ scanTimeoutSecs }`
   - 单词参数（`state` / `driver` / `text` ...）两种写法相同，无需处理。
2. **返回前端的 serde 结构体**：必须加 `#[serde(rename_all = "camelCase")]`，
   否则前端按驼峰读不到字段、显示 `undefined`。
   - 已加：`PushReport`（`ble.rs`）、`FaceState`（`core/face.rs`）。
   - 新增结构体请同样加，并补一个序列化测试（参照 `ble.rs` 的
     `push_report_serializes_camel_case`）。

**改任何命令/结构体后自查两点**：前端 `invoke` 的每个 key 是否等于 Rust 参数的
驼峰形式；返回结构体是否带 `rename_all = "camelCase"`。

## 桌面端特性

* **系统托盘**：关窗驻托盘（tray 菜单才能真正退出）；左键单击/菜单"显示窗口"
  唤回；菜单含"暂停/继续""立即推送到墨水屏"；气泡提示实时显示阶段与剩余时间。
* **单实例**：重复启动会唤回已有窗口，避免两个计时器同时推同一块屏。
* **设置持久化**：时长/自动推送/间隔/驱动/开机自启都存 localStorage，重启不丢。
* **开机自启**（可选，Windows 写入注册表 Run）。
* **沙漏画面由 Rust 生成**（`epd42-core::face`）：预览与推送到墨水屏是同一份
  渲染代码，内置位图字体（`font_data.rs` 由 `tools/gen_font_rs.py` 从系统 TTF
  烘焙，运行时零字体依赖）；对应 Python 版 face.py 的两处几何修正（沙面下沉、
  沙不出轮廓）都有单测锁定。
* **周统计**：每天完成的番茄数记在 localStorage，近 7 天纯 CSS 条形图。
* **推送自动重试**：失败最多 4 次尝试（重试 3 次、1.5s 退避），多次失败提示"设备可能离线"；扫描窗口用设置里的"扫描时长"（默认 10s）。
* **全局快捷键**：Ctrl+Alt+P 暂停/继续，Ctrl+Alt+S 立即推送（tauri-plugin-global-shortcut）。
* **标题栏实时倒计时**：任务栏可见 `▶ 专注 23:41 · EPD42 番茄钟`。
* 推送节奏沿用"每 3 分钟一次／切换时一次"的墨水屏习惯。

## 已知限制 / 后续

* Python CLI 与桌面版画面仍是两套实现（face.py / rust face），各自经单测锁定；
  若想彻底单源，可把桌面版 face 模块作为 C 接口给 Python 侧调用（暂未做）。