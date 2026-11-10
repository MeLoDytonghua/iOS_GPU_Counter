# iOS GPU Counter 采集工具

在 Windows 上采集 iOS **全设备 GPU Counter**，保存为 CSV 或 JSON。**运行后持续采集，按 Ctrl+C 结束并保存。**

已验证设备：**iPhone 13 Pro / A15 / iOS 18.5**。其他设备会标记为未验证，实际可用字段由设备提供。

## 快速开始

### 1. 准备环境

使用 Windows x64、Python 3.12。在手机上开启开发者模式，通过 USB 连接电脑，保持解锁并信任电脑。

安装可用的 Apple 设备通信组件，确认 iTunes 或 Apple Devices 能识别手机。

先下载仓库 ZIP 并解压，或使用 Git：

~~~powershell
git clone https://github.com/MeLoDytonghua/iOS_GPU_Counter.git
cd iOS_GPU_Counter
~~~

在项目目录打开 PowerShell，首次使用执行；已有可用的 .venv 可跳过安装：

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe ios_gpu_counter.py doctor
~~~

检查失败时，按终端“下一步”提示处理，详见 [环境准备与排错](docs/USAGE.md)。

### 2. 开始采集

~~~powershell
.\.venv\Scripts\python.exe ios_gpu_counter.py capture
~~~

在手机上运行需要观察的游戏或场景。工具默认每秒汇总一行，终端显示重点指标，文件保存全部指标。

终端每行按 `key: value` 展示时间、状态和指标（JSON 形式，指标位于 values 中）；等待和收到数据时各提示一次，不定时刷屏。文件仍按所选 CSV/JSON 格式保存。

**首批数据可能需要等待几十秒。** 设备分批回传，等待提示不代表采集失败。

### 3. 停止并查看文件

按 **Ctrl+C**，等待收尾完成和保存路径提示。默认文件：

~~~text
captures/<本次时间戳>/gpu.csv
~~~

CSV 首列为 **time_ms**，表示统计窗口起点的设备相对时间（毫秒，如 0、1000、2000）；后续为 Counter 原名列，无额外前缀。缺失值留空，不补零。结束状态为 partial/failed 时，请按 [异常结果说明](docs/USAGE.md) 查看处理方式。

## 常用命令

~~~powershell
.\.venv\Scripts\python.exe ios_gpu_counter.py devices
.\.venv\Scripts\python.exe ios_gpu_counter.py counters
.\.venv\Scripts\python.exe ios_gpu_counter.py capture --format json --console all
~~~

devices 查看连接的设备；counters 查看当前设备实际提供的字段；最后一条保存 JSON 并显示全部指标，仍用 Ctrl+C 结束。

采集选项通常都可省略；排查原始数据时才开启 Debug：

| 选项 | 用途 |
|---|---|
| `--udid <UDID>` | 多设备时指定目标；UDID 从 devices 获取 |
| `--output <路径>` | 指定保存路径，不覆盖旧文件；后缀须匹配格式 |
| `--format csv\|json` | 默认 CSV；JSON 另含质量统计、指标定义和会话信息 |
| `--console key\|all\|none` | 终端显示重点、全部或不显示数据；文件始终保存全部指标 |
| `--debug` | 额外保存设备元数据、JS 和原始采样字节，默认关闭 |

需要查看设备返回的原始数据时运行：

~~~powershell
.\.venv\Scripts\python.exe ios_gpu_counter.py capture --debug
~~~

正常结果仍写入 CSV；原始数据额外保存在 `captures/<本次时间戳>/debug/`，包括 `metadata/*.json`、`scripts/*.js`、`samples/*.bin` 和 `messages.jsonl`。即使通过 `--output` 将结果保存到其他位置，Debug 数据仍在 captures 中，实际路径会打印到终端。原始数据可能明显增大磁盘用量，格式和边界见 [Debug 使用说明](docs/USAGE.md#debug-原始数据保存)。

## GPU Counter 字段说明

以下是 **iPhone 13 Pro / A15 / iOS 18.5** 实际返回的 **30 项字段**，名称与导出列一致。其他机型不保证具有相同字段，请以 counters 命令为准。

每行是窗口内有效样本的均值，**Count 字段也不是该秒事件总数**。Limiter 是设备定义的限制相关指标，不能仅凭名称判定性能瓶颈；Occupancy 描述并发执行占用，不是显存占用。单位与口径边界见 [字段来源和统计口径](docs/OUTPUT_FORMAT.md#字段与单位)。

| 字段名 | 说明 |
|---|---|
| `ALU Limiter` | 尝试执行算术逻辑单元（ALU）运算的时间占比，按峰值 ALU 能力归一化。 |
| `ALU Utilization` | ALU 运算资源的利用程度。 |
| `F32 Utilization` | 32 位浮点运算资源的利用程度。 |
| `F16 Utilization` | 16 位浮点运算资源的利用程度。 |
| `Texture Sample Limiter` | 尝试执行纹理采样的时间占比，按峰值采样能力归一化。 |
| `Texture Sample Utilization` | 纹理采样资源的利用程度。 |
| `Texture Filtering Limiter` | 尝试执行纹理过滤的时间占比，按峰值过滤能力归一化。 |
| `Texture Cache Limiter` | 纹理缓存相关限制指标；设备未提供更细的文字定义。 |
| `Texture Write Limiter` | 纹理写出相关限制指标；设备未提供更细的文字定义。 |
| `Texture Write Utilization` | 纹理写出资源的利用程度。 |
| `Buffer Read Limiter` | 尝试从 Buffer 读取数据的时间占比，按峰值读取能力归一化。 |
| `Buffer Load Utilization` | Buffer 加载资源的利用程度。 |
| `Buffer Write Limiter` | 尝试向 Buffer 写入数据的时间占比，按设备定义的峰值能力归一化。 |
| `Buffer Store Utilization` | Buffer 存储资源的利用程度。 |
| `Threadgroup/Imageblock Load Limiter` | 尝试读取线程组 / Imageblock（图像块）数据的时间占比，相对于该路径峰值能力。 |
| `Threadgroup/Imageblock Load Utilization` | 线程组 / Imageblock 数据加载资源的利用程度。 |
| `Threadgroup/Imageblock Store Limiter` | 尝试写入线程组 / Imageblock 数据的时间占比，相对于该路径峰值能力。 |
| `Threadgroup/Imageblock Store Utilization` | 线程组 / Imageblock 数据存储资源的利用程度。 |
| `Fragment Input Interpolation Limiter` | 尝试执行片元着色器输入插值的时间占比，相对于峰值插值能力。 |
| `Fragment Input Interpolation Utilization` | 片元着色器输入插值资源的利用程度。 |
| `GPU Last Level Cache Limiter` | GPU 末级缓存尝试处理读写请求的时间占比，相对于缓存峰值能力。 |
| `GPU Last Level Cache Utilization` | GPU 末级缓存的利用程度。 |
| `Vertex Occupancy` | 顶点着色器并发 SIMD 组数量，相对于 GPU 最大并发 SIMD 组容量的占比。 |
| `Fragment Occupancy` | 片元着色器并发 SIMD 组数量，相对于 GPU 最大并发 SIMD 组容量的占比。 |
| `Compute Occupancy` | 计算着色器并发 SIMD 组数量，相对于 GPU 最大并发 SIMD 组容量的占比。 |
| `GPU Read Bandwidth` | GPU 从外部内存（可能是主内存）读取数据的速率；设备文字说明为 GB/s。 |
| `GPU Write Bandwidth` | GPU 向外部内存（可能是主内存）写入数据的速率；设备文字说明为 GB/s。 |
| `MMU Limiter` | 末级缓存未命中后，GPU 尝试通过内存管理单元（MMU）执行读写的时间占比，相对于峰值 MMU 能力。 |
| `MMU Utilization` | 末级缓存未命中后，GPU 通过 MMU 实际执行读写的时间占比，相对于峰值 MMU 能力。 |
| `Partial Renders Count` | 设备编码器（encoder）内的部分渲染次数；导出值仍是窗口内样本均值。 |

## 更多说明

- [技术方案、实现原理与 GPU Counter 排查指南](docs/TECHNICAL_GUIDE.md)
- [详细使用、排错与异常恢复](docs/USAGE.md)
- [CSV / JSON 格式、字段单位与统计口径](docs/OUTPUT_FORMAT.md)
- [兼容性与已知限制](docs/USAGE.md#兼容性与已知限制)

## 适用范围

本工具只生成全设备 Counter 数据，不提供应用归因、FPS、图表或自动性能分析。设备分批推送可能造成显示延迟；缺失值不补零。异常样本输出 [ERROR] 后跳过，后续正常数据继续采集；不做机型专用数值修正。最终版本完整长稳及 Xcode 绝对数值对标尚未完成，详情见 [已知限制](docs/USAGE.md#兼容性与已知限制)。

## 许可证与依赖

本项目采用 **GPL-3.0-or-later**：你可以依照 GNU GPL 第 3 版或任何更新版本使用、修改和分发本项目，详见 [LICENSE](LICENSE)。项目按现状提供，不作担保。

运行依赖通过 pip 安装，不随仓库分发第三方库。直接依赖与许可证见 [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md)。反馈问题请使用 [GitHub Issues](https://github.com/MeLoDytonghua/iOS_GPU_Counter/issues)，附上版本与已脱敏错误信息。
