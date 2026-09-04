# EPD42 番茄钟（命令行上位机）

在**终端里倒计时**的番茄钟，需要时把当前状态画成 400x300 单色画面，通过
BLE 推送到 EPD42 墨水屏。参考 `tools/epd-monitor` 的架构：画面在 PC 端合成，
设备不存字体、不存帧缓冲，只接收打包好的位图流。

墨水屏刷新一次要几秒，显示秒没有意义，所以画面是**分钟级**的：进度用一只
**沙漏**表示——上半沙量随剩余时间减少、下半随已用时间堆积、运行中沙流下落；
数字只有一个"剩 X / Y 分钟"。秒级倒计时只在终端里看。

绘图原语和蓝牙协议直接复用 `../epd-monitor` 的 `render.py` / `protocol.py` /
`ble_client.py` / `config.py`（运行时通过 `sys.path` 导入，不复制代码），
所以两边对"屏幕极性、位序、行序、平面数量"的理解永远一致。要分发给别人时，
`build_single.py` 会把这几份代码连同本工具**内联成单个文件**（见"轻量化部署"），
不再依赖仓库目录。

## 安装（开发）

```bash
cd tools/epd-pomodoro
uv sync          # 也可以: python -m pip install -r requirements.txt
```

依赖：**Pillow**（渲染画面，~15MB）；**bleak**（可选，仅 BLE 推送/扫描/换驱动，
~4MB，不装也能计时和出预览图）。Python 3.10+。

## 轻量化部署

给别的机器用时，不用克隆仓库、不用 uv、不用建虚拟环境：

1. **单文件版**：生成一个自包含脚本，复制过去就能跑
   ```bash
   python build_single.py            # 生成 dist/pomodoro.py（约 72 KB）
   python dist/pomodoro.py render --demo   # 只需要 Python 3.10+ 和 Pillow
   ```
   它把 `epd-monitor` 的 render/protocol/ble_client/config 和本工具的
   state/face 全部内联，且 **bleak 是惰性的**——只计时/预览根本不会 import 它。
2. **依赖就两行**：`python -m pip install -r requirements.txt`；要用蓝牙推送再
   `python -m pip install bleak`。
3. **Windows 双击版**：把 `dist/pomodoro.py` 和 `pomodoro.cmd` 放同一目录，
   双击首次运行自动建 `.venv` 并装依赖，之后每次双击直接跑（命令行参数追加在
   `pomodoro.cmd` 后面，如 `pomodoro.cmd render --demo`）。

还没装 bleak 就运行蓝牙命令（`push / scan / describe / setdriver`）时，会提示
`蓝牙功能需要 bleak：pip install bleak`，不会甩一段堆栈。

### 更极端的选项（按需再做）

| 方案 | 装到用户机器上的东西 | 代价 |
|---|---|---|
| 去掉 Pillow（内嵌位图字体） | 只剩 Python 3.10+，pip 都不用 | 文字变成点阵字体，观感不如 TTF；要重写渲染层 |
| PyInstaller 打成单 exe | 一个 exe，连 Python 都不用装 | 体积 ~40-60MB；须在目标平台上打包 |
| 网页版（仓库已有 Web Bluetooth 上位机） | 零安装，浏览器打开即用 | 推送/重连逻辑要按浏览器重写一版 |
## 快速开始

```bash
cd tools/epd-pomodoro

# 1. （可选）配置设备地址，跳过每次扫描
cp config.example.toml config.toml
uv run python pomodoro.py scan          # 找到墨水屏的 BLE 地址
#   把 device_address 填进 config.toml

# 2. 先看画面长什么样（不连蓝牙）
uv run python pomodoro.py render --demo

# 3. 开始倒计时（交互式；空格 暂停/继续，r 重置，n 跳过，s 推送，q 退出）
uv run python pomodoro.py start

# 4. 把画面推到墨水屏：阶段切换时推一次，之后默认每 3 分钟推一次
uv run python pomodoro.py start --push

# 5. 另开一个终端查看/推送当前状态
uv run python pomodoro.py status
uv run python pomodoro.py push
```

## 命令

| 命令 | 说明 |
|---|---|
| `start` | 终端倒计时；默认 25 分钟专注 → 5 分钟短休息，每 4 个番茄一次 15 分钟长休息 |
| `push` | 把当前状态推一次到墨水屏（`--demo` 推示例状态） |
| `render` | 只合成画面：写 `preview.png` + `frame.bin`，不连蓝牙（`--demo` 用示例状态） |
| `status` | 打印当前阶段、剩余时间、番茄计数 |
| `reset` | 清除 state.json |
| `scan` | 扫描附近 BLE 设备，找墨水屏 |
| `describe` | 打印设备 GATT 服务 |
| `setdriver` | 把设备指向实际安装的屏幕驱动（`--driver 1\|2\|3`） |

### start 的常用参数

```bash
# 自定义时长与轮次
uv run python pomodoro.py start --work 50 --short 10 --long 20 --rounds 3

# 推送到墨水屏：阶段切换时推一次，另外每 60 秒推一次（默认 180 秒；0 = 只在切换时）
uv run python pomodoro.py start --push --push-interval 60

# 阶段结束后暂停等待按键（不自动开始下一阶段）
uv run python pomodoro.py start --manual

# 无人值守：不读键盘，默认每 3 分钟推送
uv run python pomodoro.py start --push --no-input
```

## 按键（start 交互模式）

| 按键 | 作用 |
|---|---|
| `空格` / `p` | 暂停 / 继续 |
| `r` | 重置当前阶段 |
| `n` | 跳到下一阶段（不计番茄） |
| `s` | 立即把当前画面推到墨水屏 |
| `q` | 保存并退出 |

## 墨水屏刷新注意事项

- 画面**只在 PC 决定时推送**：墨水屏刷新一次要几秒，频繁整屏刷新伤屏幕也伤
  电池。`--push` 默认每 180 秒（3 分钟）推一次，和画面的分钟级粒度匹配；
  一般 3-5 分钟一次即可，`--push-interval 0` 表示只在阶段切换时推。
- 设备睡眠后无法被计时器唤醒（nRF51 只能用 GPIO/NFC 唤醒），推送到睡眠中的
  设备会失败，先重启/唤醒设备再推。
- 系统重启、断电、关闭终端都不会丢失计时：状态实时存在 `state.json`，
  `start` 会从上次的位置继续（`start` 即开始/继续计时；要暂停按空格）。

## 配置（config.toml，可选）

不建配置文件也能跑，全部有默认值。字段说明见 `config.example.toml`：
设备名/地址、扫描超时、日志级别、`fast_write`、字体路径，
以及 `work_minutes` / `short_minutes` / `long_minutes` / `rounds` / `push_interval`。

## 测试与静态检查

```bash
uv run python test_face.py       # 离线自测，不碰蓝牙和硬件
ty check --project .             # 类型检查（pyright 内核）
ruff check . && ruff format --check .
```

离线自测覆盖：状态机流转、JSON 持久化、画面合成、位打包 roundtrip、PackBits
roundtrip。类型检查通过 `[tool.ty.environment]` 的 `extra-paths` 找到复用的
`epd-monitor` 模块；`--project .` 把 ty 的项目根固定在本目录（否则 ty 会一路
向上定位到仓库根，路径参数也随之错位）。