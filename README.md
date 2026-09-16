# EPD42

[English](README_EN.md)

4.2 寸电子墨水屏固件 + 三种上位机：把 AI 订阅额度、番茄钟或任意图片推到一块从价签 hack 来的墨水屏上。上位机负责合成整幅 400x300 位图并压缩，固件只把 packed bits 直写面板 RAM——设备不存字体、不存帧缓冲、不做排版。

[网页版上位机](https://no010.github.io/epd42/)开箱即用（Chrome/Edge，Web Bluetooth），无需安装任何东西；重度使用可以选桌面版或命令行工具（见[上位机](#上位机)）。

理论上支持所有 nRF51 系列 MCU，内置 3 个微雪 4.2 寸墨水屏驱动（可切换），同时支持自定义墨水屏到 MCU 的引脚映射，支持睡眠唤醒（NFC 检波 / 无线充电上电信号等外部事件）。

## 网页版上位机

地址：<https://no010.github.io/epd42/>（本仓库 GitHub Pages,随 main 分支自动部署）

一个页面四种内容,通过 Web Bluetooth 推送：

- **番茄钟**：沙漏进度 + 剩余分钟 + 每轮圆点 + 今日完成数；计时状态存 localStorage,操作与阶段变化时自动推送。
- **LLM 用量卡**：最多 3 张卡（DeepSeek / Kimi / 百炼 TokenPlan 的排布），数值手填或从命令行工具抄写；所有百分比统一读作"用量"。
- **图片**：上传 + 抖动（Bayer / Floyd–Steinberg），压缩后推送。
- **调试**：切换驱动、清屏、原始命令、电源模式开关。

页面把画面 PackBits 压缩后按新流式协议（`0xB0`–`0xB5`）分帧推送,约 190 包 / 6 秒一帧。

## 上位机

| 工具 | 形态 | 用途 | 文档 |
|---|---|---|---|
| 网页版 | 浏览器（Web Bluetooth） | 番茄钟 / 用量卡 / 图片 / 调试,零安装 | 本页上方链接 |
| [desktop](desktop/) | Tauri 桌面应用（Windows） | 番茄钟：托盘常驻、通知、自动推送、统计 | [desktop/README.md](desktop/README.md) |
| [tools/epd-monitor](tools/epd-monitor/) | Python CLI | LLM 订阅额度监测：从控制台登录态抓取 DeepSeek / Kimi / 百炼 TokenPlan 等数据,定时推送 | [tools/epd-monitor/README.md](tools/epd-monitor/README.md) |
| [tools/epd-pomodoro](tools/epd-pomodoro/) | Python CLI | 终端里的番茄钟,按需推屏 | [tools/epd-pomodoro/README.md](tools/epd-pomodoro/README.md) |

## 电源模式

用 `EPD_CMD_SET_POWER (0x93)` 选择,持久保存在设备配置页：

- **常驻**（默认）：刷新后 MCU 保持 System ON、持续广播——适合番茄钟、额度监控这类周期推送的应用。
- **深睡**：推送成功后布防；**蓝牙断连且空闲达到宽限期**（默认 60 秒,0–255 秒可设）后面板休眠、MCU 进入 System OFF。窗口期内可以再连上来继续推图。图像在墨水屏上常显,功耗降到最低。
- **唤醒**：复位键,或已配置的 `wakeup` 脚（GPIO SENSE）——可外接 NFC 检波输出、无线充电上电信号或干簧管。唤醒即冷启动,下一帧照常初始化面板。

切换方式:`epd_monitor.py setmode --mode deep --sleep-after 60`,或网页版调试页。

## 支持设备

硬件是对电商平台上4.2寸墨水屏价签hack而来，可以[点此购买](https://item.taobao.com/item.htm?ft=t&id=874071462547)，支持黑白双色和黑白红三色两种版本。

- 黑白双色版本

  ```
  MCU：nRF51822
  RAM：16K
  ROM：128K

  驱动：UC8176 (EPD_4in2)
  屏幕引脚：0508090A0B0C0D
  线圈引脚：07
  ```

  ![](html/images/1.jpg)

- 黑白红三色版本

  ```
  MCU：nRF51802
  RAM：16K
  ROM：256K

  驱动：UC8276C (EPD_4in2b_V2)
  屏幕引脚：0A0B0C0D0E0F10
  线圈引脚：09
  LED引脚：03/04/05 （有三个 LED，任选一个使用）
  ```

  ![](html/images/2.jpg)

默认驱动和引脚映射为黑白双色版本，其它版本需要切换驱动并修改引脚映射。

## 协议

图像走流式协议 `0xB0`–`0xB5`（BEGIN/DATA/END/ABORT/GET_STATUS）,完整说明见
[tools/epd-monitor/README.md](tools/epd-monitor/README.md#protocol)。要点：15000 字节平面先做
PackBits 压缩（典型界面压到 ~23%）,每包 19 字节,校验和解码后整平面比对,失败不刷新;
ACK 靠 notify 而非写响应。旧版原始面板命令（`0x00`–`0x06`）仍然保留,供点亮排查。

## 开发

> **注意:**
> - 本地继续使用 [Keil 5.36](https://img.anfulai.cn/bbs/96992/MDK536.EXE) 或以下版本进行开发；仓库新增了基于 `arm-none-eabi-gcc` 的构建脚本，供 GitHub Actions 自动构建使用。

项目配置有几个 `Target`：

- `nRF51822_xxAB`: 用于编译 nRF51822 固件, 内置黑白双色版本配置
- `nRF51802_xxAA`: 用于编译 nRF51802 固件, 内置黑白红三色版本配置
- `flash_softdevice` 结尾的 `Target`: 刷蓝牙协议栈用（只需刷一次）

### GitHub Actions / GCC 构建

- 工作流：`.github/workflows/firmware.yml`
- 构建脚本：`python3 tools/build_firmware.py`
- 产物目录：`build/<target>/`

示例：

```bash
python3 tools/build_firmware.py --target nRF51822_xxAB
python3 tools/build_firmware.py --target nRF51802_xxAA
```

对应产物：

- `build/nRF51822_xxAB/epd42-bw.{elf,hex,bin,map}`
- `build/nRF51802_xxAA/epd42-bwr.{elf,hex,bin,map}`
- `build/nRF51822_xxAB/epd42-bw-merged.hex`
- `build/nRF51802_xxAA/epd42-bwr-merged.hex`

说明：

- 普通 `*.hex` / `*.bin` 仍然只包含应用固件。
- `*-merged.hex` 会把应用固件和 Nordic SoftDevice (`components/softdevice/s130/hex/s130_nrf51_2.0.1_softdevice.hex`) 合并到同一个可烧录文件中。
- 当前 nRF51 目标依赖的是 `S130`，不是 `s132`。

烧录器可以使用 J-Link 或者 DAPLink（可使用 [RTTView](https://github.com/XIVN1987/RTTView) 查看 RTT 日志）。

**刷机流程:**

> 如不修改代码，建议到 [Releases](https://github.com/no010/epd42/releases) 下载二进制固件，开箱即用。

1. 全部擦除 (Keil 擦除后刷不了的话，使用烧录器的上位机软件擦除试试)
2. 二选一：
   - 切换到 MCU 对应的 `flash_softdevice` `Target`，**不要编译直接下载**（只需刷一次），然后再刷普通应用 `*.hex` / `*.bin`
   - 直接刷对应的 `*-merged.hex`
3. 如使用普通 `Target`，先编译再下载

## 致谢

- 本项目基于 [EPD-nRF51](https://github.com/tsl0922/EPD-nRF51) 分叉而来，感谢原作者的分享和贡献。
