# 输出格式与字段口径

CSV 输出 time_ms 与 Counter 原名列；JSON 使用 ios_gpu_counter.rows.v2，保留质量统计与结束状态。

可直接读取的合成样例：[精简 CSV](examples/sample.csv)、[详细 JSON](examples/sample.json)。CSV 展示时间和指标值；JSON 另外展示质量计数、修正和尾窗信息。样例只用于说明格式，不代表设备测量。

## 1. 时间与聚合

- 时间单位是秒，原点沿用 Decoder 的首条设备时间戳；采用 Mach timebase 换算后的设备相对时间，与主机回包时间无关。
- 默认窗口宽度为 1 秒，按样本结束时间归属 `[k, k+1)`。恰好在边界的样本进入下一窗口；跨边界采样间隔不拆分权重。
- 每项 Counter 单独对有限数值求算术平均，分母为自己的 `valid_counts`。正常的数值 0 必须计入；基线、重复样本不计入分母。局部公式异常不影响其他有效 Counter。
- 计数型指标也是样本均值，不代表秒内事件总数；不额外转换为速率或时间加权利用率。
- 有后续样本落入更晚窗口时，中间没有任何样本的窗口输出 `missing`。在首条样本前、最后样本后及等待下一批期间不推测或补造空窗。
- 正常完整窗口的 `end_s` 为窗口右边界。结束时最后一行的 `end_s` 为最后实际观测样本时间，`partial_window=true`。若最后样本恰好落在新窗口起点，可产生 `start_s == end_s` 的尾行；它保留该边界样本，不表示覆盖了完整一秒。
- 倒序样本计入当前尚未输出窗口的异常数，不回写历史窗口，不参与计算，也不推进尾窗时间。跨包前值仍由原 Decoder 保持。
- 算术运算产生非有限结果时拒绝写入，不把异常转换为零。

窗口使用固定全量 Counter 键及质量计数；尾窗终点为最后实际观测时间。

## 2. 内部共同窗口对象与 JSON 明细

`WindowRecord` 是终端、CSV、JSON 的共同输入。文件先写入并 flush，终端随后展示相同数值，不再次聚合或使用 `.4g` 等精度截断。

下面是内部及 JSON 的完整字段，不是 CSV 列清单。CSV 将 start_s 乘以 1000 转为 time_ms，加各项 values；统计口径和异常判断不因简化列而改变。终端仍显示秒单位的 start_s/end_s/status 与所选 Counter，Counter 数值与文件相同。

每 5 秒状态只输出到 stderr，不属于 WindowRecord，也不写入数据表；统计来自最近一次工作进程确认的批次。正常录制持续到 Ctrl+C，收到停止请求后先完成在途处理再按既有顺序收尾；实际行数/尾窗以设备时间为准，无定时结束或时间裁剪。

| 字段 | 定义 |
|---|---|
| `start_s` / `end_s` | 窗口开始与结束时间，秒；尾窗规则见上文 |
| `partial_window` | 是否为结束时尚未闭合的窗口；与数据质量分开 |
| `sample_count` | 该窗口收到的样本记录数，含基线、重复、异常 |
| `baseline_count` / `duplicate_count` | 不参与均值的基线与重复样本数 |
| `invalid_sample_count` | 状态异常或至少一个 Counter 无效的样本数；该样本其他有效项仍可参与均值 |
| `adjusted_sample_count` | 包含显式修正的样本数，例如已验证条件下的 A15 回绕修正；不保存原始数值 |
| `valid_counts` | 每项 Counter 实际参加均值的数值数量 |
| `values` | 每项 Counter 的均值；无有效值时为 null |
| `status` | 以下四种窗口质量状态 |

质量状态：

- `valid`：存在有效值，且所有非基线/非重复样本的所有指标有效。
- `partial`：存在有效值，但有异常样本或某项有效数不足。
- `unavailable`：有样本记录，但所有指标都无有效值；单独的基线窗口也属于此状态。
- `missing`：后续样本已证明该完整窗口没有收到任何样本。

`partial_window=true` 不自动使 `status=partial`。正常结束时有不足一秒尾窗仍可整场 `completed`。只有基线/重复的窗口也不自动使其他有效窗口组成的会话变为 partial；整场没有任何有效值时为 failed。

## 3. Counter 定义与单位

`CounterDefinition` 只包含 `name / unit / description`，名称唯一、顺序固定。指标来自当前设备/profile，不固定为 30 项，不把整设备指标解释为 App 数据。

当前 Decoder 尚未证实设备定义中的单位，`ParsedPipeline` 传入的 unit 为 null，不依据名称猜测；写入器可保存后续能力探测提供的已确认单位。设备公式、原始字段、formula_values 和完整服务响应不进入新格式。

同一元数据重复通知不重置跨包基线；内容发生任何变化时 ParsedPipeline.process 报 metadata_changed，正式 collect_parsed 停止消费、请求 GPU stop 并完成已有结果，不再用旧定义解析尾包。禁止中途改列或忽略新增指标。

## 4. CSV

- 编码 UTF-8 BOM；标准 csv 转义，逗号、双引号、换行和中文名称均可回读。
- 第一列为 `time_ms`，值等于内部 `start_s × 1000`：首条设备样本为原点的相对毫秒数、当前统计窗口起点；不是主机接收时间，也不是窗口长度。
- 随后每项 Counter 一列，直接使用设备指标原名，例如 `ALU Utilization`、`GPU Read Bandwidth`，不添加 `counter:` 前缀。Counter 值仍为同一份有效样本均值，不改变精度和缩放。
- 不输出 end_s、partial_window、sample_count、status、baseline_count、duplicate_count、invalid_sample_count、adjusted_sample_count、valid_counts、counter_units。时间起点 start_s 换算为毫秒并命名为 time_ms。这些诊断统计仍用于计算与结束状态判断，并保留在 JSON 中；JSON 的 counters 定义保留单位，不在 CSV 每行重复。
- 缺失 Counter 值为空单元格，正常零值为 0 或 0.0。已确认的整秒缺数仍保留对应 time_ms 行，所有指标为空；不补假零。浮点使用 Python 可往返的数值表示。
- 每行是一个窗口，不添加说明行、诊断尾行或 sidecar。CSV 不再单独说明尾窗实际结束时间、单项有效分母或局部异常细节；需要这些信息时使用 --format json。
- 会话整体结束状态仍由 stderr/退出码报告；精简 CSV 的指标值不能单独证明整场数据完整。进程被强杀时可能留下不完整的最后一行，尚无 CSV 自动修复。
- time_ms 是唯一保留列名。如设备恰好提供同名 Counter，CSV 在创建文件前拒绝并提示使用 JSON，防止重名或静默丢列。

示例列头：`time_ms,ALU Utilization,Fragment Occupancy,GPU Read Bandwidth,...`。当前 30 项 Counter 的 CSV 从 41 列减少到 31 列。已有采集文件不自动改写；消费旧 CSV 的脚本需将 start_s/time_s 换算为毫秒并改名为 time_ms，并移除自行添加的 counter: 前缀。JSON schema 不变。

## 5. JSON

顶层 `schema="ios_gpu_counter.rows.v2"`。

| 字段 | 内容 |
|---|---|
| `aggregation` | 固定为 `arithmetic_mean_of_valid_samples` |
| `scope` | 固定为 device |
| `device` | 白名单设备摘要：name、model、ios_version、gpu_name、compatibility；有值才保存 |
| `source` / `started_at` / `report_interval` | 输入来源、会话开始时间、窗口宽度 |
| `counters` | 有序 CounterDefinition 数组 |
| `rows` | 逐个追加的 WindowRecord，null 表示无有效值 |
| `result` | 收尾时写入 status、stop_reason、row_count、ended_at；现场采集路径另有 capture 摘要 |

result.capture 包含 collection_elapsed_s、sample_count、sample_span_s、row_count、has_values、metadata_received、console_error、errors、lifecycle 和 receive_queue。仅保存固定统计与有界错误（最多 16 条，每条最多 512 字符）；lifecycle 仅记 start/stop/flush 状态与当时已处理样本数，不保存原始 RPC 回复。文件内计数在输出尾窗后更新。完整收尾耗时 shutdown_elapsed_s 由内存 RunResult.details/诊断报告记录，不回写已经完成的文件。

正式采集停止原因包括 user_stop、cancelled、capture_error。具体异常阶段在 errors 中；断连/取消/解析失败/收尾错误使会话非 completed。极晚资源清理失败会使退出状态非零，但不重写已完成文件，应结合 stderr/退出码判断。

会话状态：有有效值且无失败/质量缺失为 `completed`；有有效值但中断、缺数或计算异常为 `partial`；没有有效值为 `failed`。`stop_reason` 与窗口质量独立。恢复结果无论是否发现文件完整，都不会报告一次新的现场成功。

写入过程使用 `<output>.part`：首行是白名单 header 和 rows 数组起始，每个窗口独占一个物理行（字符串中的换行被 JSON 转义），最后追加合法 footer。正常/可控异常 finish 后关闭并重命名到目标，最终文件可由严格 JSON 解析器读取，不保留 `.part`。

## 6. 错误、恢复与资源约束

- 输出后缀必须匹配 csv/json。路径检查在写入前验证目录可创建临时文件、目标及 `.part` 不存在；实际打开仍使用排他创建。Windows 最终重命名拒绝覆盖运行期间出现的目标文件。
- 每个窗口 flush 到操作系统；不声称 fsync 级断电保护。只保存当前窗口和固定统计量，不积累整场 rows。
- 写入失败向调用方抛出，写入器关闭且拒绝继续写入/finish；已写 CSV 或 JSON `.part` 保留。磁盘耗尽时应先腾出空间或选择其他可写位置再恢复。
- 终端 BrokenPipe/编码失败只关闭本次终端输出，记录 WindowOutput.console_error；文件继续写入且数据质量不受其影响。工具将终端关闭原因输出到 stderr。
- Decoder/QuickJS 的创建和调用必须在同一线程。工具在独立工作进程内满足这一约束，IPC 队列最多一个在途请求；超时可终止该进程，保留 CSV/.part。设备通信仍在主进程，保持 stop/drain/flush 顺序。

恢复命令（不连接设备）：

```powershell
.\.venv\Scripts\python.exe tools\recover_results.py captures\run\rows.json.part --output captures\recovered\rows.json
```

恢复逐行读取，只保留完整且通过契约校验的行。缺失最后半行（包括半个中文 UTF-8 字符）可跳过；完整行中的损坏、非法数值、字段变化、倒序或重复窗口均拒绝。源文件不修改，新目标不得已存在。恢复输出标为 partial；没有有效值则 failed，工具退出码为 1，避免自动化把恢复误当成完整采集。恢复只适用于本工具的解析结果 JSON，不承担分析功能。

## 字段与单位

README 的 30 项字段来自 iPhone 13 Pro / A15 / iOS 18.5 profile 3；逐项用途见 [技术说明](TECHNICAL_GUIDE.md)。其他设备可能返回不同字段，以 counters 命令为准。

- time_ms 是工具加入的毫秒时间列，不是硬件 Counter。
- 当前 CounterDefinition.unit 为 null；counters 显示 unknown。本项目不根据字段名自动推断单位。
- 除读/写带宽和 Partial Renders Count 外，基线其余 27 项的定义为 scale=100、maximum=100；显示范围不能替代跨设备单位验收。
- 带宽的设备说明使用 GB/s，指 GPU 外部内存；未与 Xcode 做绝对对账，也不自动换算 GiB/s 或 MB/s。
- Limiter 是设备定义的限制相关指标，不能仅凭名称判定瓶颈；短描述字段不补出未经证实的硬件含义。Utilization 是对应路径利用程度，不将不同项相加成总利用率。
- Occupancy 是并发 SIMD 组相对最大容量的占比，不是显存占用；MMU 字段描述末级缓存未命中后的请求，不是页故障计数。
- Partial Renders Count 的设备描述为 encoder 内部分渲染次数；所有 Counter 导出仍是有效样本均值，不是秒内总数、每帧次数或 App 独占值。

兼容范围与已知限制见 [使用说明](USAGE.md#兼容性与已知限制)。字段含义以对应设备元数据为依据；文字说明不等于已完成单位识别或跨工具数值对标。
