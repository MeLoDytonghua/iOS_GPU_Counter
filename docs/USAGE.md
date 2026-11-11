# 详细使用、排错与恢复

[返回快速上手](../README.md) · [技术说明](TECHNICAL_GUIDE.md)

## 环境与兼容范围

使用 Windows x64 / Python 3.12。服务连通不代表已经完成该设备的采样验收，实际验证范围统一见文末。全部 Counter 是设备级数据，不代表某个 App 的独占消耗。

运行依赖固定为 pymobiledevice3==11.39.0、quickjs-ng==0.18.0.1；后者的 Python 导入名为 quickjs。使用 requirements.txt 安装运行依赖。系统 python 可能不是项目要求的版本，命令始终使用 .venv/Scripts/python.exe。

设备通信、配对、隧道及 DTX 由 pymobiledevice3 实现；设备公式由 quickjs-ng 执行。工具不启动 Graphics、Sysmontap、CoreProfile；DeviceInfo 仅用于初始化查询。

手机需要 USB 连接、解锁、信任电脑并具备可用开发者服务。驱动、配对、DDI 等准备沿用 pymobiledevice3 既有能力，本工具不自动安装系统组件或下载镜像。

## 检查命令

```powershell
.\.venv\Scripts\python.exe ios_gpu_counter.py devices
.\.venv\Scripts\python.exe ios_gpu_counter.py doctor
.\.venv\Scripts\python.exe ios_gpu_counter.py counters
```

- devices 列出 usbmux 可见的 USB/Network 连接，包含名称、型号、系统、连接方式、UDID 和配对状态；同一手机的不同连接可分别列出。单台信息不可读仍保留其 UDID。它不发起新配对。
- doctor 检查本机依赖、现有信任及开发者服务，不启动性能采样、不发起新配对，也不自动修复环境。必要检查失败退出码为 1；版本偏离基线会显示 WARNING。
- counters 短暂启动 GPU 服务取得指标定义后停止；检查停止宽限期内的异步错误，不执行设备公式、不取回样本尾包、不保存采样文件。
- doctor/counters 多设备时只需增加 --udid；devices 无业务参数。当前用户入口自动连接设备，不能自动确定唯一 GPU 或不支持固定 profile 的设备会报错。

## 保存格式与路径

默认创建 captures/<本次时间戳>/gpu.csv；单次只保存所选格式。目标文件或对应 .part 已存在时拒绝覆盖，请使用新路径。

```powershell
.\.venv\Scripts\python.exe ios_gpu_counter.py capture --output captures\run\gpu.csv
.\.venv\Scripts\python.exe ios_gpu_counter.py capture --format json --output captures\run_json\gpu.json
.\.venv\Scripts\python.exe ios_gpu_counter.py capture --udid '<UDID>' --console all
```

CSV 后缀应为 .csv，JSON 后缀应为 .json。CSV 使用 UTF-8 BOM 和标准转义，只输出 time_ms 与 Counter 原名；JSON 另有设备摘要、指标定义、尾窗、样本数、有效分母、质量和结束状态。JSON 不含原始采样、设备公式或 raw journal。

CSV 的 time_ms 为设备相对窗口起点，单位毫秒；JSON/终端的 start_s、end_s 仍为秒。默认每秒一行，通常对应 0、1000、2000…毫秒。缺失为 CSV 空单元格 / JSON null，正常零值保留。详细定义见 [输出契约](OUTPUT_FORMAT.md)。

## Debug 原始数据保存

```powershell
.\.venv\Scripts\python.exe ios_gpu_counter.py capture --debug
.\.venv\Scripts\python.exe ios_gpu_counter.py capture --debug --format json --output result.json
```

Debug 默认关闭，只能用于 capture。开启后，正常 CSV/JSON 的字段与计算规则不变，额外创建 `captures/<本次时间戳>/debug/`。默认结果与 debug 同在一次运行目录；自定义 `--output` 只改变解析结果的位置，原始文件仍留在 captures，终端会打印路径。

```text
captures/<本次时间戳>/
  gpu.csv                       默认解析结果
  debug/
    session.json                记录状态与文件统计
    messages.jsonl              每条收到的载荷及二进制文件索引
    metadata/00000004_001.json  元数据快照，编号仅为示例
    scripts/00000004_001.js     对应设备 JS，UTF-8 原内容
    samples/00000006_001.bin    对应二进制载荷，逐字节保存
```

保存范围是 pymobiledevice3 解开 DTX 后交给工具的 GPU 服务载荷：GPU 信息、配置/启动/停止/flush 回复、元数据、采样批次、控制通知和未知分发消息，以及初始化 Mach 时间查询结果。采样在拆分、差分、异常过滤和公式计算前保存；不只保存有效样本。停止后取回的尾包也记录。它不是 USB/TCP 抓包，不含原始 DTX 帧头，不录制其他性能服务或配对过程。

JSON 使用类型标记保留整数键、大整数和二进制引用；元数据文件也使用相同编码。直接阅读原文脚本用 scripts 下的 JS，检查采样字节用 samples 下的 bin，通知与回复的关联关系看 messages.jsonl。详细字段见 [Debug 格式](OUTPUT_FORMAT.md#debug-附加文件)。

`session.json.state=closed` 仅表示已接受的原始记录完成写入，不表示本次采集质量为 completed；采集质量仍以正常结果和退出码为准。状态为 recording、failed，或终端出现 debug_* 错误时，不应把原始记录当作完整。强制结束、磁盘写入失败、队列溢出或设备断连都可能留下不完整数据；手机尚未发送的数据无法保存。

Debug 使用额外工作进程和有界队列，会增加 CPU、内存及磁盘消耗，原始数据量通常明显大于秒级结果。写入失败会明确报错并停止正常采集流程，不静默丢失；保留已经完成的原始文件和可用解析结果。无关排查时不要开启。captures 默认被 Git 忽略，不会随代码提交。

## 终端状态与结束

Counter 数据输出到 stdout，每行一个 JSON 风格的 key: value 对象，无 CSV 表头；包含 start_s、end_s、status 和 values，指标位于 values 内，缺失值为 null。状态、错误和保存路径输出到 stderr，均为 UTF-8 普通文本，不使用 ANSI 控制符。--console key/all/none 只影响数据展示；none 仍会显示状态，文件始终保存全部指标。

设备按批次回传，批量与等待时间由设备决定，可能等待几十秒；不足一批的数据在停止后取回。进入等待时只输出一次“等待数据”，开始处理一批收到的采样时只输出一次“收到数据”和该批声明的样本数，不定时刷新、不区分首批/下一批。元数据、控制通知和主机内部子批不会反复触发提示；停止收尾也不再刷新进度。

输出示例（数字仅为格式示意）：

```text
[等待数据 / Waiting for data]
[收到数据 / Data received] samples: 20000
{"start_s": 0, "end_s": 1, "status": "valid", "values": {"ALU Utilization": 13.949116559813778}}
...
[等待数据 / Waiting for data]
```

每个窗口仍只打印一行，数值不截断；CSV/JSON 文件格式及聚合规则不受终端展示变化影响。

状态日志管道关闭或不可写时，工具继续尝试停止、取尾及保存；结果质量以文件和退出码为准。若关闭的是数据输出管道，文件仍继续保存；磁盘写入失败则会停止采集。

正常录制一直持续到 Ctrl+C。收到停止请求后，先等在途处理结束，再按 stop → 排空早期通知 → 最后 flush → 排空尾部 → 关闭连接 → 完成文件收尾。请等待保存路径与最终状态提示。

连接超时、断连、元数据变化、写盘失败等保护使用内部固定策略，不是定时录制。工具不会自动重连拼接不同会话，不用周期 flush 模拟实时刷新。工作进程超时可被终止并保留已写结果；主进程中的底层同步调用完全挂起不属于异步超时可强制打断的保证范围。

可识别的异常样本采用“[ERROR] 提示＋跳过”：计数器负差分、时间异常、无效周期等跳过整条样本；单个公式失败只跳过该项。工具不按型号、公式指纹或特定字段猜测回绕并修正。后续正常样本继续采集，缺失不补零。同类异常首次和每累计 1000 条提示一次，结束时汇总；即使 --console none 也会输出错误。含异常的结果保留质量标记，不能当作完整采集。

## 常见问题

| 现象 | 处理方向 |
|---|---|
| 没有设备或 Apple 通信入口不可达 | 检查 USB、Apple Mobile Device Service，以及 iTunes/Apple Devices 能否识别手机，再运行 devices。 |
| 未信任、未配对或手机锁定 | 解锁并确认信任；按 pymobiledevice3 提示完成配对，再运行 doctor。 |
| 开发者服务 / DDI 不可用 | 检查开发者模式及库给出的 DDI 前提；需要挂载时使用库的 mounter auto-mount。 |
| 多台设备 | 从 devices 输出复制 UDID，增加 --udid。 |
| 无法自动连接或选定 GPU、profile 不支持 | 提供设备型号、iOS 版本和 doctor 输出进行适配，不猜测协议参数。 |
| 很久没有显示指标 | 先看 stderr 状态；等待首批或正在解析不等于断流。保持手机解锁，并关注最终是否出现超时/失败。 |
| 文件已存在或被占用 | 选择新 --output，或关闭占用目标文件的编辑器；工具不会覆盖旧文件。 |
| 磁盘空间不足 | 保留已写文件并释放空间；JSON .part 可按下面的方法恢复。 |
| 元数据变化或中途断连 | 本次会话已停止，先检查已有结果和结束状态，再新开采集。 |

本工具的“下一步”提示是检查方向，不把候选原因当成已经确认的根因。无法解决时保留错误阶段、设备信息和已有解析结果。

## 结束状态与退出码

| 状态 | 含义 | 退出码 |
|---|---|---|
| completed | 有有效结果，且未发现不完整错误 | 0 |
| partial | 有可用结果，但采集或收尾有异常 | 1 |
| failed | 没有确认的有效结果或初始化失败；可能无文件 | 1 |
| 初始化时 Ctrl+C | 在正式录制前中断 | 130 |
| 参数错误 | 命令或参数无效，未开始正常采集 | 2 |

CSV 不带质量统计列，需要结合 stderr 和退出码判断整场是否正常；JSON 提供更完整的明细。极晚资源清理失败可能使退出码非零，已完成的文件不被重写。

## JSON 中断恢复

正常结束会将 .part 完成为 JSON 文件；异常中断留下的完整解析行可以恢复到新文件：

```powershell
.\.venv\Scripts\python.exe tools\recover_results.py captures\run\gpu.json.part --output captures\recovered\gpu.json
```

恢复不修改源文件，跳过最后不完整行，拒绝中间损坏；恢复结果仍为 partial/failed，退出码 1。逐行 flush 不等于断电保护，CSV 强制结束后的半行尚无自动修复功能。

## 兼容性与已知限制

- 已在 iPhone 13 Pro / A15 / iOS 18.5 上验证采集、人工 Ctrl+C 和断连后保留结果。其他设备仅作能力探测。
- 计数器回绕、重置或其他负差分统一报错并跳过，不进行机型专用修正。因此长时间采集可能出现 partial，这是被过滤异常的明确提示，不代表工具已经修复硬件计数器问题。
- CSV 已通过 WPS 读取核对；Microsoft Excel 和其他表格软件未逐项验证。
- 尚未完成最终版本的完整长时间验收，也未与 Xcode 做同条件绝对数值对标。不提供其他机型、iOS 版本或传输方式的通用保证。
- 拔线后只能保存主机已接收的数据，设备尚未发送的批次无法补取；不会自动重连拼接不同会话。

## 问题反馈

在 [GitHub Issues](https://github.com/MeLoDytonghua/iOS_GPU_Counter/issues) 提供 Windows/Python 版本、设备型号、iOS 版本、命令和错误阶段。先移除 UDID、设备名称、用户名、本机路径及其他私人信息；不要上传配对文件、凭证或整场采集数据。需要讨论某个指标时，可提供已脱敏的小段输出和复现步骤。
