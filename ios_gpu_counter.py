# 许可证 / License: GPL-3.0-or-later; see LICENSE.
"""Windows iOS GPU Counter 采集工具：运行 capture 开始，按 Ctrl+C 停止并保存。
Windows iOS GPU Counter collector: run capture, then press Ctrl+C to stop and save.

默认自动选择单台设备，每秒汇总，保存全部指标为 CSV；也可选择 JSON。
Automatically select a single device, summarize every second, and save all counters as CSV or JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import csv
import hashlib
import importlib.metadata
import json
import math
import multiprocessing
import platform
import queue
import signal
import struct
import sys
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DTX_IMPORT_ERROR = None
try:
    from pymobiledevice3.dtx import (
        DTXService, PInt32, PInt64, dtx_method, dtx_on_dispatch, dtx_on_notification,
    )
    from pymobiledevice3.dtx.ns_types import NSError
except (ImportError, OSError) as exc:
    # 仅为缺依赖时显示 help/doctor 保留定义；所有设备入口必须先检查依赖。
    # Definition stubs permit help/doctor without dependencies; device entrypoints must check them first.
    DTX_IMPORT_ERROR = str(exc)
    DTXService, PInt32, PInt64 = object, int, int

    class NSError(Exception):
        pass

    def dtx_method(*args):
        return lambda method: method

    def dtx_on_dispatch(method):
        return method

    def dtx_on_notification(method):
        return method

GPU_CHANNEL = "com.apple.instruments.server.services.gpu"
SAFE_INTEGER = 2**53 - 1
# 仅匹配实机验证过的公式版本，不将所有累计计数器一律当作 32 位。
# Match only the device-validated formula revision; do not assume all accumulators are 32-bit.
A15_FORMULA_SHA256 = "2e11c9890068f4c61fe694bd3a51beeff9518fe21c1a66859c319f18932a28cc"
A15_CLOCK_COUNTER = "_792173079ffc5aacc2cea817d8812166e71ea17309e294d24ee2cc88d2fb1e8e"
SPECIAL_FIELDS = {
    "GRC_TIMESTAMP", "GRC_GPU_CYCLES", "GRC_ENCODER_ID",
    "GRC_KICK_TRACE_ID", "GRC_SAMPLE_TYPE",
}
KEY_COUNTERS = {
    "Vertex Occupancy", "Fragment Occupancy", "Compute Occupancy",
    "ALU Utilization", "GPU Read Bandwidth", "GPU Write Bandwidth",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
    """无损表示解码后的 DTX 负载，不包含原始 DTX 帧。
    Lossless JSON representation of decoded DTX payloads (not raw DTX frames).
    """
    if isinstance(value, NSError):
        return {"$ns_error": {"code": value.code, "domain": value.domain,
                              "user_info": json_safe(value.user_info)}}
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > SAFE_INTEGER:
        return {"$integer": str(value)}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    return value


def from_json(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ns_error"}:
            error = value["$ns_error"]
            return NSError(error["code"], error["domain"], from_json(error["user_info"]))
        if set(value) == {"$bytes"}:
            return base64.b64decode(value["$bytes"], validate=True)
        if set(value) == {"$integer"}:
            return int(value["$integer"])
        return {k: from_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_json(v) for v in value]
    return value


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def ensure_transport():
    if DTX_IMPORT_ERROR is not None:
        raise ImportError("pymobiledevice3: " + DTX_IMPORT_ERROR)


def error_guidance(exc):
    """按实际异常给出下一步，不将候选原因说成已确认根因。
    Suggest next steps from actual errors without presenting possible causes as proven.
    """
    name, message = type(exc).__name__, str(exc)
    phase = getattr(exc, "gpu_stage", "")
    if "unsupported_python" in message:
        return "使用 Python 3.12 创建 .venv，再安装 requirements.txt / Create a Python 3.12 virtual environment."
    if "parsed_result_not_legacy_replay" in message:
        return "这是解析结果，不含回放所需原始数据；.part 恢复用 tools/recover_results.py / Parsed results are not legacy replay input."
    if "csv_time_column_conflict" in message:
        return "设备指标名与 CSV 的 time_ms 列冲突，请使用 --format json / Use JSON to preserve the conflicting counter name."
    if isinstance(exc, ImportError) or "PackageNotFoundError" in name:
        return '使用项目 Python 安装依赖 / Install dependencies: .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt'
    if isinstance(exc, FileExistsError) or "FileExistsError" in message:
        return "输出或 .part 已存在，请换新的 --output；旧数据不会覆盖 / Choose a new output path."
    if "debug_" in message:
        return "Debug 原始记录不完整；检查 captures 下的调试目录、磁盘空间和写入权限 / Check debug files, free space and write permissions."
    if isinstance(exc, PermissionError) or "PermissionError" in message:
        return "检查错误中的路径是否可写、是否被 Excel/编辑器占用；换可写 --output / Check path permissions and file locks."
    if "No space left" in message or "Errno 28" in message or "disk full" in message.lower():
        return "目标磁盘空间不足；保留现有文件，释放空间后恢复 JSON .part / Free disk space before recovery."
    if any(s in name + message for s in ("NotTrusted", "NotPaired", "Pairing", "PasswordRequired", "PasscodeRequired", "trust_required")):
        return "解锁手机并确认信任电脑；用 pymobiledevice3 lockdown pair 完成配对后重试 / Unlock, trust and pair."
    if "DeveloperMode" in name + message:
        return "在手机设置→隐私与安全中开启开发者模式，按设备提示完成重启 / Enable Developer Mode on the device."
    if any(s in name + message for s in ("NotMounted", "DiskImage", "InvalidService", "StartService")):
        return "检查开发者模式和 DDI；按库提示运行 pymobiledevice3 mounter auto-mount / Check developer services and DDI."
    if any(s in name + message for s in ("Usbmux", "MuxException", "ConnectionRefused", "WinError 10061")):
        return "检查 Apple Mobile Device Service、USB 连接和 iTunes/Apple Devices 是否能识别手机 / Check the Apple service and USB connection."
    if any(s in name + message for s in ("Tunneld", "TunnelUnavailable", "RSDRequired", "RoutableTunnel")):
        return "当前自动连接方式不可用；检查库的连接前提，并提供 doctor 输出和 iOS 版本进行适配 / Check automatic connection support with doctor."
    if "multiple_devices" in message:
        return "运行 devices 查看 UDID，再用 --udid 明确选择一台 / Select one device using --udid."
    if "no_device" in message or "DeviceNotFound" in name or "NoDeviceConnected" in name:
        return "连接并解锁手机，运行 devices 检查；多设备请指定 --udid / Connect a device and check devices."
    if "accelerator" in message or "multiple_gpu_candidates" in message:
        return "无法自动确定唯一 GPU；请提供 doctor 输出以适配此设备 / Report doctor output for automatic GPU selection support."
    if "unsupported_counter_profile" in message:
        return "此 GPU 未报告支持工具使用的 profile 3；请提供设备型号和 doctor 输出 / Report this device's unsupported profile 3."
    if "metadata_changed" in message:
        return "本次指标定义已变化，已停止混合解析；检查保存结果并新开一次采集 / Start a new capture after reviewing partial output."
    if "device_error" in message or "gpu_counter_" in message or "gpu_configuration_rejected" in message:
        return "GPU 服务拒绝或未完成请求；保持设备解锁，用默认 profile 3 重试，保留错误码与设备版本 / Retry unlocked with profile 3 and retain the error code."
    if "timeout" in message.lower() or isinstance(exc, TimeoutError):
        if phase == "connect" or "connect_timeout" in message:
            return "连接超时：检查 USB、手机信任/解锁和 Apple 通信服务，再运行 doctor / Check connection prerequisites with doctor."
        return "查看超时阶段，保持手机解锁；设备按批次回传，不要周期 flush。结果可能不完整 / Check the stage; never flush periodically."
    return "保留错误阶段和已有结果；先运行 doctor，再按失败项处理 / Run doctor and inspect the failed stage."


def print_error(exc):
    phase = getattr(exc, "gpu_stage", "")
    print(f"错误 / Error [{phase or type(exc).__name__}]: {type(exc).__name__}: {exc}", file=sys.stderr)
    print("下一步 / Next: " + error_guidance(exc), file=sys.stderr)


class CaptureProgress:
    """仅汇报主机状态，不生成或重复 Counter 数据行。
    Report host-side progress without generating or repeating counter rows.
    """
    def __init__(self, statistics, interval=5, stream=None):
        self.statistics = statistics
        self.interval = interval
        self.stream = stream if stream is not None else sys.stderr
        self.phase = "等待首批 / waiting for first batch"
        self.stopped = asyncio.Event()

    def text(self):
        stats = self.statistics()
        phase = self.phase
        if stats["sample_count"] and phase.startswith("等待首批"):
            phase = "等待下一批 / waiting for next batch"
        return (f"[{phase}] 已解析 / parsed={stats['sample_count']}，已写行 / rows={stats['row_count']}，"
                f"设备时间 / device span={stats['sample_span_s']:.3f}s；设备分批回传 / batched delivery")

    async def run(self):
        while not self.stopped.is_set():
            try:
                await asyncio.wait_for(self.stopped.wait(), self.interval)
            except asyncio.TimeoutError:
                try:
                    print(self.text(), file=self.stream, flush=True)
                except (OSError, ValueError):
                    return


class GpuEventQueue(asyncio.Queue):
    """同时限制消息数与负载字节，容纳大批样本计算期间的短通知。
    Bound both message count and payload bytes, allowing short notifications during large-batch evaluation.
    """
    def __init__(self, maxsize=2048, max_bytes=64 * 1024 * 1024):
        super().__init__(maxsize=maxsize)
        self.max_bytes = max_bytes
        self.payload_bytes = 0
        self.peak_bytes = 0
        self.peak_messages = 0

    @classmethod
    def weight(cls, item):
        if isinstance(item, NSError):
            return 64 + cls.weight(item.code) + cls.weight(item.domain) + cls.weight(item.user_info)
        if isinstance(item, (bytes, bytearray)):
            return len(item)
        if isinstance(item, str):
            return len(item) * 4
        if isinstance(item, (list, tuple)):
            return 64 + sum(cls.weight(v) for v in item)
        if isinstance(item, dict):
            return 64 + sum(cls.weight(k) + cls.weight(v) for k, v in item.items())
        return 64

    def put_nowait(self, item):
        size = self.weight(item)
        if self.payload_bytes + size > self.max_bytes:
            raise asyncio.QueueFull
        super().put_nowait(item)
        self.payload_bytes += size
        self.peak_bytes = max(self.peak_bytes, self.payload_bytes)
        self.peak_messages = max(self.peak_messages, self.qsize())

    def get_nowait(self):
        item = super().get_nowait()
        self.payload_bytes -= self.weight(item)
        return item


class DebugArchive:
    """仅在调试子进程写入未经解析的服务载荷。 / Write unparsed service payloads only in the debug child process."""
    def __init__(self, root, output):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)
        for name in ("samples", "metadata", "scripts"):
            (self.root / name).mkdir()
        self.session = {"schema": "ios_gpu_counter.debug.v1", "state": "recording", "created_at": utc_now(),
                        "parsed_output": str(Path(output).resolve()), "scope": "decoded_gpu_service_payloads",
                        "wire_capture": False, "messages": 0, "binary_bytes": 0}
        self.save_session()
        self.journal = (self.root / "messages.jsonl").open("x", encoding="utf-8", newline="\n")

    def save_session(self):
        staged = self.root / "session.tmp"
        staged.write_text(dumps(self.session), encoding="utf-8")
        staged.replace(self.root / "session.json")

    def write(self, event, payload, received_at):
        sequence = self.session["messages"] + 1
        binaries = 0

        def encode(value):
            nonlocal binaries
            if isinstance(value, NSError):
                return {"$ns_error": encode([value.code, value.domain, value.user_info])}
            if isinstance(value, (bytes, bytearray)):
                binaries += 1
                name = f"samples/{sequence:08d}_{binaries:03d}.bin"
                data = bytes(value)
                with (self.root / name).open("xb") as handle:
                    handle.write(data)
                self.session["binary_bytes"] += len(data)
                return {"$binary": {"path": name, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
            if isinstance(value, dict):
                # 键和值都编码，保留整数键并避免与标记字典碰撞。 / Encode keys and values to preserve integer keys and avoid tag collisions.
                return {"$map": [[encode(k), encode(v)] for k, v in value.items()]}
            if isinstance(value, tuple):
                return {"$tuple": [encode(v) for v in value]}
            if isinstance(value, list):
                return [encode(v) for v in value]
            if isinstance(value, int) and abs(value) > SAFE_INTEGER:
                return {"$integer": str(value)}
            if isinstance(value, float) and not math.isfinite(value):
                return {"$float": repr(value)}
            if isinstance(value, datetime):
                return {"$datetime": value.isoformat()}
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            raise TypeError(f"unsupported_debug_payload: {type(value).__name__}")

        encoded = encode(payload)
        metadata_files = []

        def metadata(value, encoded_value):
            if not isinstance(value, list):
                return
            if len(value) >= 5 and value[0] == 0 and isinstance(value[4], (dict, list)):
                stem = f"{sequence:08d}_{len(metadata_files) + 1:03d}"
                name = f"metadata/{stem}.json"
                (self.root / name).write_text(dumps(encoded_value[4]), encoding="utf-8")
                metadata_files.append(name)
                body = value[4]
                source = body.get(2, body.get("2")) if isinstance(body, dict) else None
                if isinstance(source, str):
                    # 用二进制写 UTF-8，不改变设备脚本的换行或内容。 / Write UTF-8 bytes without changing script content or newlines.
                    (self.root / f"scripts/{stem}.js").write_bytes(source.encode("utf-8"))
            else:
                for item, encoded_item in zip(value, encoded_value):
                    metadata(item, encoded_item)

        metadata(payload, encoded)
        self.journal.write(dumps({"sequence": sequence, "received_at": received_at, "event": event,
                                  "payload": encoded, "metadata_files": metadata_files}) + "\n")
        self.journal.flush()
        self.session["messages"] = sequence

    def close(self, state="closed", error=None):
        self.journal.close()
        self.session.update(state=state, ended_at=utc_now())
        if error:
            self.session["error"] = error
        self.save_session()


def _debug_worker_main(requests, replies, root, output):
    """隔离原始文件 I/O，避免阻塞设备事件循环。 / Isolate raw file I/O from the device event loop."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    archive = None
    try:
        archive = DebugArchive(root, output)
        replies.put(("ready", None))
        while True:
            item = requests.get()
            if len(item) == 2 and item[0] == "close":
                if archive.session["messages"] != item[1]:
                    raise RuntimeError("debug_message_count_mismatch")
                archive.close()
                replies.put(("closed", None))
                return
            event, payload, received_at, size = item
            archive.write(event, payload, received_at)
            replies.put(("written", size))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:512]
        replies.put(("error", error))
        if archive is not None:
            with contextlib.suppress(Exception):
                archive.close("failed", error)


class DebugRecorder:
    """有界原始记录队列；失败显式传播，不静默丢失。 / Bound raw recording and propagate failures without silent loss."""
    def __init__(self, root, output):
        self.root = Path(root)
        ctx = multiprocessing.get_context("spawn")
        self.requests, self.replies = ctx.Queue(maxsize=2048), ctx.Queue()
        self.requests.cancel_join_thread()
        self.process = ctx.Process(target=_debug_worker_main,
                                   args=(self.requests, self.replies, root, output), daemon=True)
        self.pending_bytes = 0
        self.record_count = 0
        self.max_bytes = 64 * 1024 * 1024
        self.ready = self.finished = self.disposed = False
        self.failure = None
        self.process.start()

    def check(self):
        while True:
            try:
                kind, value = self.replies.get_nowait()
            except queue.Empty:
                break
            if kind == "written":
                self.pending_bytes -= value
            elif kind == "ready":
                self.ready = True
            elif kind == "closed":
                self.finished = True
            else:
                self.failure = "debug_write_failed: " + value
        if not self.disposed and not self.process.is_alive() and not self.finished:
            self.failure = self.failure or "debug_worker_exited"
        if self.failure:
            raise RuntimeError(self.failure)

    async def start(self, timeout=10):
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.ready:
            self.check()
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("debug_start_timeout")
            await asyncio.sleep(.01)

    def record(self, event, payload):
        self.check()
        if self.finished or self.disposed:
            raise RuntimeError("debug_recorder_closed")
        size = GpuEventQueue.weight(payload)
        try:
            if self.pending_bytes + size > self.max_bytes:
                raise queue.Full
            self.requests.put_nowait((event, payload, utc_now(), size))
        except queue.Full:
            self.failure = "debug_queue_overflow"
            raise RuntimeError(self.failure) from None
        self.pending_bytes += size
        self.record_count += 1

    async def close(self, timeout=5):
        if self.disposed:
            return
        deadline = asyncio.get_running_loop().time() + timeout
        sent = False
        try:
            while not self.finished:
                self.check()
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("debug_close_timeout")
                if not sent:
                    try:
                        # 核对接收数，不能把 IPC 序列化丢失误报为完整归档。 / Verify the count so an IPC serialization loss cannot look complete.
                        self.requests.put_nowait(("close", self.record_count))
                        sent = True
                    except queue.Full:
                        pass
                await asyncio.sleep(.01)
        finally:
            if self.process.is_alive():
                # 成功确认后也等待进程退出；卡住的写入可被终止。 / Wait for acknowledged exit; terminate a stuck writer if necessary.
                until = asyncio.get_running_loop().time() + 1
                while self.process.is_alive() and asyncio.get_running_loop().time() < until:
                    await asyncio.sleep(.01)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=.2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=.2)
            if not self.process.is_alive():
                self.process.join(timeout=0)
                self.process.close()
            self.requests.close()
            self.replies.close()
            self.disposed = True


class GpuService(DTXService):
    IDENTIFIER = GPU_CHANNEL

    def __init__(self, ctx):
        super().__init__(ctx)
        self.events = GpuEventQueue()
        self.closed = asyncio.Event()
        self.failure: str | None = None
        self.debug = None

    @dtx_method("requestDeviceGPUInfo")
    async def device_info(self): ...

    @dtx_method("configureCounters:counterProfile:interval:windowLimit:tracingPID:")
    async def configure(self, accelerator: PInt64, profile: PInt64, interval: PInt64,
                        window: PInt64, pid: PInt32): ...

    @dtx_method("startCollectingCounters")
    async def start(self): ...

    @dtx_method("stopCollectingCounters")
    async def stop(self): ...

    @dtx_method("flushRemainingData")
    async def flush(self): ...

    def enqueue(self, payload):
        if self.debug is not None:
            try:
                self.debug.record("notification", payload)
            except Exception as exc:
                self.failure = str(exc)
                self.closed.set()
        try:
            self.events.put_nowait(payload)
        except asyncio.QueueFull:
            self.failure = "notification_queue_overflow"
            self.closed.set()

    @dtx_on_notification
    async def notification(self, payload):
        self.enqueue(payload)

    @dtx_on_dispatch
    async def dispatch(self, selector, *args):
        # 保留未知分发消息供诊断，不猜测其负载布局。
        # Preserve unknown dispatches for diagnostics; do not guess their payload layout.
        self.enqueue({"dispatch_selector": selector, "arguments": list(args)})

    def on_closed(self, reason=""):
        self.failure = self.failure or (reason or "connection_closed")
        # 不清除已经接收的样本。 / Never purge already received samples.
        self.closed.set()


class FormulaEngine:
    """仅在采集管线的专用工作线程中创建和使用。
    Created and used only on the pipeline's dedicated worker thread.
    """

    def __init__(self, source, gpu_info, counters, numer, denom):
        import quickjs
        self.context = quickjs.Context()
        self.context.set_memory_limit(64 * 1024 * 1024)
        self.context.set_time_limit(1.0)
        self.context.set_max_stack_size(1024 * 1024)
        globals_ = dict(gpu_info)
        globals_.update(TIMEBASE_NUMER=numer, TIMEBASE_DENOM=denom)
        self.context.eval("Object.assign(globalThis," + dumps(globals_) + ");")
        self.context.eval(source)
        # 通过 JSON 传入数值，避免绑定层的原生整数转换。
        # Numeric input enters via JSON, avoiding the binding's native integer conversion.
        self.run = self.context.eval("""(function(input) {
            const p = JSON.parse(input);
            for (const k of Object.keys(p.globals)) globalThis[k] = p.globals[k];
            return JSON.stringify(p.functions.map(function(name) {
                try {
                    const v = globalThis[name]();
                    return typeof v === 'number' && Number.isFinite(v)
                        ? {value:v,error:null} : {value:null,error:'non_finite_or_non_numeric'};
                } catch(e) { return {value:null,error:String(e)}; }
            }));
        })""")
        self.functions = [c[3] for c in counters]

    def evaluate(self, values, interval_ns, previous_ns):
        values = dict(values)
        values.update(MTLStat_nSec=interval_ns, SAMPLE_INTERVAL_USEC=interval_ns / 1000,
                      _LastTimestamp=previous_ns)
        return json.loads(self.run(dumps({"globals": values, "functions": self.functions})))


class Decoder:
    def __init__(self, numer: int, denom: int):
        if numer <= 0 or denom <= 0:
            raise ValueError("invalid Mach timebase")
        self.numer, self.denom = numer, denom
        self.version = 0
        self.counters = None
        self.raw_fields = None
        self.engine = None
        self.engine_error = None
        self.previous = None
        self.origin = None
        self.validated_clock_wrap = False

    def metadata(self, payload):
        self.version += 1
        self.previous = None
        self.engine = None
        self.engine_error = None
        self.validated_clock_wrap = False
        if isinstance(payload, dict):
            obj = {str(k): v for k, v in payload.items()}
            self.counters, self.raw_fields = obj["0"], obj["1"]
            source, gpu = obj["2"], obj.get("3", {})
            if not isinstance(source, str) or not isinstance(gpu, dict):
                raise ValueError("invalid formula source/GPU metadata")
        elif isinstance(payload, list):
            self.counters, self.raw_fields = payload, None
            source, gpu = None, {}
        else:
            raise ValueError("unknown metadata format")
        source_hash = hashlib.sha256(source.encode()).hexdigest() if source is not None else None
        self.validated_clock_wrap = (source_hash == A15_FORMULA_SHA256 and gpu.get("gpu_type") == "G14P"
                                     and self.numer * 3 == self.denom * 125)
        minimum = 5 if source is not None else 1
        if not self.counters or any(not isinstance(c, list) or len(c) < minimum
                                    or not isinstance(c[0], str) for c in self.counters):
            raise ValueError("invalid counter definitions")
        names = [c[0] for c in self.counters]
        if len(set(names)) != len(names):
            raise ValueError("duplicate counter names")
        if self.raw_fields is not None:
            if not self.raw_fields or any(len(c) < 2 or not isinstance(c[0], str)
                                         or not isinstance(c[1], bool) for c in self.raw_fields):
                raise ValueError("invalid raw field definitions")
            variables = [c[0].replace(" ", "_").replace(":", "_") for c in self.raw_fields]
            if len(set(variables)) != len(variables):
                raise ValueError("ambiguous normalized variable names")
            try:
                self.engine = FormulaEngine(source, gpu, self.counters, self.numer, self.denom)
            except Exception as exc:
                self.engine_error = f"formula_initialization: {exc}"
        return {"version": self.version, "definitions": json_safe(payload),
                "formula_sha256": source_hash,
                "formula_error": self.engine_error,
                "units": {name: None for name in names}}

    def samples(self, count, data):
        if self.counters is None:
            raise ValueError("samples_before_metadata")
        fields = self.raw_fields if self.raw_fields is not None else self.counters
        width = len(fields) + 1
        if not isinstance(count, int) or count < 0 or not isinstance(data, (bytes, bytearray)):
            raise ValueError("invalid sample header")
        if len(data) != count * width * 8:
            raise ValueError(f"sample_length_mismatch: expected {count * width * 8}, got {len(data)}")
        for row in struct.iter_unpack("<" + "Q" * width, data):
            timestamp, *raw = row
            previous = self.previous
            self.previous = row
            if self.origin is None:
                self.origin = timestamp
            elapsed_ns = (timestamp - self.origin) * self.numer / self.denom
            sample = {"metadata_version": self.version, "timestamp_ticks": str(timestamp),
                      "elapsed_ns": elapsed_ns, "raw_values": [str(v) for v in raw],
                      "interval_ns": None, "formula_values": {}, "values": {}, "errors": {}}
            error = None
            if previous is None:
                error = "baseline"
            elif row == previous:
                error = "duplicate"
            elif timestamp <= previous[0]:
                error = "non_increasing_timestamp"
            else:
                sample["interval_ns"] = (timestamp - previous[0]) * self.numer / self.denom
            if self.raw_fields is None and error in (None, "baseline"):
                # 旧格式已包含计算结果，因此首条样本也保留。
                # Older format is already computed: retain even its very first sample.
                sample["values"] = {field[0]: value if value <= SAFE_INTEGER else str(value)
                                    for field, value in zip(fields, raw)}
                sample["status"] = "valid"
                yield sample
                continue
            if error:
                sample["status"] = error
                yield sample
                continue
            values = {}
            for i, (name, instantaneous, *_) in enumerate(fields):
                value = raw[i]
                if name not in SPECIAL_FIELDS and not instantaneous:
                    value -= previous[i + 1]
                    if value < 0:
                        wrapped = value + 2**32
                        tick_delta = timestamp - previous[0]
                        if (self.validated_clock_wrap and name == A15_CLOCK_COUNTER
                                and raw[i] < 2**32 and previous[i + 1] < 2**32
                                and abs(wrapped - tick_delta) <= 8):
                            # 由独立 Mach 时钟校验回绕量；保留原始值并记录显式修正。
                            # Validate the wrap against the independent Mach clock; retain raw values and record the correction.
                            sample.setdefault("adjustments", []).append({
                                "field": name, "kind": "validated_32bit_clock_wrap",
                                "original_delta": str(value), "corrected_delta": str(wrapped),
                                "mach_tick_delta": str(tick_delta), "difference_ticks": wrapped - tick_delta})
                            value = wrapped
                        else:
                            error = "counter_reset_or_wrap"
                # 仅当公式引用绝对时间戳时，才需要将其传入 JS。
                # Absolute timestamp does not need to enter JS unless referenced by formula.
                values[name.replace(" ", "_").replace(":", "_")] = value
            if any(abs(v) > SAFE_INTEGER for v in values.values()):
                error = error or "unsafe_javascript_integer"
            if error:
                sample["status"] = error
                yield sample
                continue
            cycles = values.get("GRC_GPU_CYCLES")
            if cycles is None or cycles <= 0:
                sample["status"] = "missing_or_zero_gpu_cycles"
                yield sample
                continue
            values.update({k + "_norm": v / cycles for k, v in list(values.items())})
            try:
                if self.engine is None:
                    raise ValueError(self.engine_error or "formula engine unavailable")
                # 保留设备的绝对纳秒时间，以兼容设备公式。
                # Absolute device nanoseconds are retained for device formula compatibility.
                previous_ns = previous[0] * self.numer / self.denom
                if abs(previous_ns) > SAFE_INTEGER:
                    raise ValueError("unsafe_javascript_previous_timestamp")
                results = self.engine.evaluate(values, sample["interval_ns"], previous_ns)
                for counter, result in zip(self.counters, results):
                    name, maximum, _, _, scale, *_ = counter
                    value = result["value"]
                    sample["formula_values"][name] = value
                    if result["error"]:
                        sample["values"][name] = None
                        sample["errors"][name] = result["error"]
                        continue
                    try:
                        displayed = value * scale
                        if not math.isfinite(displayed):
                            raise ValueError("non_finite_scaled_value")
                        sample["values"][name] = min(max(displayed, 0), maximum)
                    except (ValueError, TypeError, OverflowError) as exc:
                        sample["values"][name] = None
                        sample["errors"][name] = str(exc)
            except Exception as exc:
                # 上下文超时或损坏后，避免每个样本都重复阻塞一秒。
                # A timed-out or broken context must not repeat a one-second stall per sample.
                if self.engine is not None:
                    self.engine_error = f"formula_execution_disabled: {exc}"
                    self.engine = None
                error_message = self.engine_error or str(exc)
                sample["values"] = {c[0]: None for c in self.counters}
                sample["errors"] = {c[0]: error_message for c in self.counters}
            sample["status"] = "invalid" if sample["errors"] else "valid"
            yield sample


RESULT_SCHEMA = "ios_gpu_counter.rows.v2"
AGGREGATION = "arithmetic_mean_of_valid_samples"


@dataclass(frozen=True)
class CounterDefinition:
    """仅保存解释结果所需的定义，不包含设备公式。 / Result definitions exclude device formulas."""
    name: str
    unit: str | None = None
    description: str = ""


def counter_names(counters):
    names = [c.name for c in counters]
    if not names or len(set(names)) != len(names):
        raise ValueError("empty or duplicate counter definitions")
    for c in counters:
        if (not isinstance(c.name, str) or not c.name or not isinstance(c.description, str)
                or (c.unit is not None and not isinstance(c.unit, str))):
            raise ValueError("invalid counter definition")
    return names


@dataclass(frozen=True)
class WindowRecord:
    """设备时间窗口；质量与尾窗完整性独立表示。 / Device-time window with separate quality and tail flags."""
    start_s: float
    end_s: float
    partial_window: bool
    sample_count: int
    status: str
    baseline_count: int
    duplicate_count: int
    invalid_sample_count: int
    adjusted_sample_count: int
    valid_counts: dict[str, int]
    values: dict[str, float | None]

    def validate(self, names):
        if (not all(isinstance(t, (int, float)) and not isinstance(t, bool)
                    and math.isfinite(t) for t in (self.start_s, self.end_s))
                or not 0 <= self.start_s <= self.end_s or type(self.partial_window) is not bool):
            raise ValueError("invalid window time")
        if set(self.values) != set(names) or set(self.valid_counts) != set(names):
            raise ValueError("window counter schema changed")
        counts = [self.sample_count, self.baseline_count, self.duplicate_count,
                  self.invalid_sample_count, self.adjusted_sample_count, *self.valid_counts.values()]
        if any(type(n) is not int or not 0 <= n <= self.sample_count for n in counts):
            raise ValueError("invalid window counts")
        if self.baseline_count + self.duplicate_count + self.invalid_sample_count > self.sample_count:
            raise ValueError("inconsistent sample counts")
        eligible = self.sample_count - self.baseline_count - self.duplicate_count
        for name, value in self.values.items():
            n = self.valid_counts[name]
            if n > eligible or (n == 0) != (value is None):
                raise ValueError("inconsistent counter count/null")
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value)):
                raise ValueError("non-finite or non-numeric counter")
        expected = self.quality(self.sample_count, eligible, self.invalid_sample_count, self.valid_counts)
        if self.status != expected:
            raise ValueError("inconsistent window quality")

    @staticmethod
    def quality(samples, eligible, invalid, counts):
        if samples == 0:
            return "missing"
        if not any(counts.values()):
            return "unavailable"
        return "partial" if invalid or any(n < eligible for n in counts.values()) else "valid"


class WindowAggregator:
    """仅保留当前窗口；收到后续样本后才确认中间空窗。
    Retain one window; only later samples establish intervening missing windows.
    """
    def __init__(self, counters, emit, interval=1.0):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("invalid report interval")
        self.names = counter_names(counters)
        self.interval, self.emit = interval, emit
        self.bucket = None
        self.last_time = None
        self.finished = False
        self._reset()

    def _reset(self):
        self.sums = dict.fromkeys(self.names, 0.0)
        self.counts = dict.fromkeys(self.names, 0)
        self.samples = self.baselines = self.duplicates = self.invalid = self.adjusted = 0

    def _emit(self, end, partial):
        eligible = self.samples - self.baselines - self.duplicates
        record = WindowRecord(
            self.bucket * self.interval, end, partial, self.samples,
            WindowRecord.quality(self.samples, eligible, self.invalid, self.counts),
            self.baselines, self.duplicates, self.invalid, self.adjusted,
            dict(self.counts), {n: self.sums[n] / self.counts[n] if self.counts[n] else None for n in self.names})
        record.validate(self.names)
        self.emit(record)
        self._reset()

    def add(self, sample):
        if self.finished:
            raise ValueError("aggregator already finished")
        timestamp = sample["elapsed_ns"] / 1e9
        if not math.isfinite(timestamp):
            raise ValueError("invalid sample timestamp")
        if set(sample["values"]) - set(self.names):
            raise ValueError("sample counter schema changed")
        # 倒序样本归入当前窗口的异常数，不回写已经输出的窗口。
        # Count backwards samples in the current window; never rewrite emitted windows.
        backwards = self.last_time is not None and timestamp < self.last_time
        if timestamp < 0 and not backwards:
            raise ValueError("negative initial timestamp")
        bucket = self.bucket if backwards else math.floor(timestamp / self.interval)
        if self.bucket is None:
            self.bucket = bucket
        while self.bucket < bucket:
            self._emit((self.bucket + 1) * self.interval, False)
            self.bucket += 1
        self.last_time = max(timestamp, self.last_time if self.last_time is not None else timestamp)
        self.samples += 1
        status = sample["status"]
        if backwards:
            self.invalid += 1
            return
        if status in ("baseline", "duplicate"):
            if status == "baseline":
                self.baselines += 1
            else:
                self.duplicates += 1
            return
        self.adjusted += bool(sample.get("adjustments"))
        has_invalid = status != "valid"
        for name in self.names:
            value = sample["values"].get(name)
            if (not isinstance(value, bool) and isinstance(value, (int, float))
                    and math.isfinite(value) and name not in sample.get("errors", {})):
                self.sums[name] += value
                self.counts[name] += 1
            else:
                has_invalid = True
        self.invalid += has_invalid

    def finish(self):
        if not self.finished and self.bucket is not None:
            self._emit(self.last_time, True)
        self.finished = True


def preflight_result_path(output, format):
    """连接前可调用的路径检查，不覆盖目标。 / Pre-connection path check without overwriting targets."""
    output = Path(output)
    if format not in ("csv", "json") or output.suffix.lower() != "." + format:
        raise ValueError("output suffix must match csv/json format")
    for path in (output, Path(str(output) + ".part")):
        if path.exists():
            raise FileExistsError(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=output.parent):
        pass
    return output


class ResultWriter:
    """流式保存解析结果；内存不保留历史窗口。 / Stream parsed results without retaining past windows."""

    def __init__(self, output, format, counters, device=None, session=None):
        self.names = counter_names(counters)
        if format == "csv" and "time_ms" in self.names:
            raise ValueError("csv_time_column_conflict: time_ms")
        self.counters, self.format = list(counters), format
        # 白名单阻止 header 中的原始消息、公式等意外落盘。
        # Allow lists prevent accidental persistence of raw metadata or formulas.
        device, session = device or {}, session or {}
        device_keys = ("name", "model", "ios_version", "gpu_name", "compatibility")
        self.header = {"schema": RESULT_SCHEMA, "aggregation": AGGREGATION,
                       "device": {k: device[k] for k in device_keys if isinstance(device.get(k), str)},
                       "scope": "device", "source": session.get("source", "unknown"),
                       "started_at": session.get("started_at", utc_now()),
                       "report_interval": session.get("report_interval", 1.0),
                       "counters": [asdict(c) for c in counters]}
        if (not isinstance(self.header["source"], str) or not isinstance(self.header["started_at"], str)
                or not isinstance(self.header["report_interval"], (int, float))
                or not math.isfinite(self.header["report_interval"]) or self.header["report_interval"] <= 0):
            raise ValueError("invalid result header")
        header_text = dumps(self.header)
        self.output = preflight_result_path(output, format)
        self.part = Path(str(self.output) + ".part")
        self.rows = 0
        self.last_end = None
        self.last_start = None
        self.last_partial = False
        self.has_values = self.bad = self.poisoned = self.finished = False
        self.handle = (self.part if format == "json" else self.output).open(
            "x", encoding="utf-8" if format == "json" else "utf-8-sig", newline="")
        try:
            if format == "json":
                self.handle.write(header_text[:-1] + ',"rows":[\n')
            else:
                self.csv = csv.writer(self.handle)
                self.csv.writerow(["time_ms", *self.names])
            self.handle.flush()
        except BaseException:
            self.close()
            raise

    def write(self, record):
        if self.finished or self.poisoned or self.handle.closed:
            raise ValueError("writer is closed or failed")
        record.validate(self.names)
        if self.last_end is not None and (record.start_s < self.last_end
                or record.start_s <= self.last_start or self.last_partial):
            raise ValueError("overlapping or out-of-order windows")
        try:
            if self.format == "json":
                text = dumps(asdict(record))
                self.handle.write(("," if self.rows else "") + text + "\n")
            else:
                # CSV 窗口起点使用毫秒；内部及 JSON 时间仍为秒，指标值不变。
                # CSV uses milliseconds; internal and JSON times remain seconds, with unchanged counter values.
                time_ms = record.start_s * 1000
                if not math.isfinite(time_ms):
                    raise ValueError("invalid_csv_time_ms")
                self.csv.writerow([time_ms, *[record.values[n] for n in self.names]])
            # 每个窗口刷入操作系统，批次到达时无需等待下一批才可读。
            # Flush each window to the OS so rows are readable before the next batch.
            self.handle.flush()
        except BaseException:
            self.poisoned = True
            self.close()
            raise
        self.rows += 1
        self.last_end = record.end_s
        self.last_start = record.start_s
        self.last_partial = record.partial_window
        self.has_values |= any(n > 0 for n in record.valid_counts.values())
        self.bad |= record.status != "valid" and not (
            record.status == "unavailable" and record.baseline_count + record.duplicate_count == record.sample_count)

    def finish(self, reason="finished", failed=False, details=None):
        if self.finished or self.poisoned or self.handle.closed:
            raise ValueError("writer is closed or failed")
        status = "failed" if not self.has_values else "partial" if failed or self.bad else "completed"
        result = {"status": status, "stop_reason": str(reason), "row_count": self.rows, "ended_at": utc_now()}
        if details is not None:
            result["capture"] = details
        try:
            if self.format == "json":
                self.handle.write('],"result":' + dumps(result) + '}\n')
            self.handle.flush()
        finally:
            self.close()
        if self.format == "json":
            if self.output.exists():
                raise FileExistsError(self.output)
            self.part.rename(self.output)
        self.finished = True
        return RunResult(self.output, status)

    def close(self):
        self.handle.close()


class WindowOutput:
    """先落文件再显示同一结果；终端故障不改变数据质量。
    Persist before displaying the same record; console failures do not change data quality.
    """
    def __init__(self, writer, console="key", stream=None):
        if console not in ("key", "all", "none"):
            raise ValueError("invalid console mode")
        self.writer, self.console = writer, console
        self.stream = stream if stream is not None else sys.stdout
        self.names = writer.names if console == "all" else sorted(KEY_COUNTERS)
        self.printed_header = False
        self.console_error = None

    def emit(self, record):
        self.writer.write(record)
        if self.console == "none" or self.console_error:
            return
        try:
            display = csv.writer(self.stream, lineterminator="\n")
            if not self.printed_header:
                display.writerow(["start_s", "end_s", "status", *self.names])
                self.printed_header = True
            display.writerow([record.start_s, record.end_s, record.status,
                              *[record.values.get(n) if record.values.get(n) is not None else "unavailable"
                                for n in self.names]])
            self.stream.flush()
        except (OSError, UnicodeError, ValueError) as exc:
            self.console_error = type(exc).__name__


class ParsedPipeline:
    """解析输出管线，负责指标解码与窗口写入；不写原始归档。
    Parsed output pipeline for metric decoding and window writing; no raw archive.
    """
    def __init__(self, output, header, format="csv", console="key", stream=None):
        self.path = preflight_result_path(output, format)
        self.header, self.format, self.console, self.stream = header, format, console, stream
        self.decoder = Decoder(**header["timebase"])
        self.writer = self.windows = self.output = None
        self.metadata_hash = None
        self.failed = False
        self.sample_count = 0
        self.sample_span_s = 0.0

    def process(self, payload):
        try:
            self._process(payload)
        except Exception:
            self.failed = True
            raise

    def _process(self, payload):
        if isinstance(payload, NSError):
            raise ValueError(f"device_error: {payload.domain}/{payload.code}")
        if isinstance(payload, list) and len(payload) == 2 and payload[0] in (2, 3) and isinstance(payload[1], int):
            return
        if not isinstance(payload, list) or len(payload) < 5:
            raise ValueError("unsupported_gpu_payload")
        kind, _, _, count, body = payload[:5]
        if kind == 0:
            signature = hashlib.sha256(dumps(json_safe(body)).encode()).hexdigest()
            if self.metadata_hash is not None:
                if signature != self.metadata_hash:
                    raise ValueError("metadata_changed")
                return
            self.decoder.metadata(body)
            counters = [CounterDefinition(c[0], description=c[2] if len(c) > 2 and isinstance(c[2], str) else "")
                        for c in self.decoder.counters]
            self.writer = ResultWriter(self.path, self.format, counters, self.header.get("device"), self.header["session"])
            self.output = WindowOutput(self.writer, self.console, self.stream)
            self.windows = WindowAggregator(counters, self.output.emit, self.header["session"]["report_interval"])
            self.metadata_hash = signature
        elif kind == 1:
            if self.windows is None:
                raise ValueError("samples_before_metadata")
            for sample in self.decoder.samples(count, body):
                self.windows.add(sample)
                self.sample_count += 1
                self.sample_span_s = max(self.sample_span_s, sample["elapsed_ns"] / 1e9)
        else:
            raise ValueError("unsupported_gpu_message_type")

    def finish(self, reason="finished", failed=False, details=None):
        if self.writer is None:
            raise ValueError("no_counter_metadata")
        self.windows.finish()
        if details is not None:
            details.update(self.statistics())
        return self.writer.finish(reason, failed or self.failed, details)

    def statistics(self):
        return {"sample_count": self.sample_count, "sample_span_s": self.sample_span_s,
                "metadata_received": self.metadata_hash is not None,
                "row_count": self.writer.rows if self.writer else 0,
                "has_values": self.writer.has_values if self.writer else False,
                "console_error": self.output.console_error if self.output else None}

    def close(self):
        if self.writer is not None:
            self.writer.close()


def recover_result_json(source, output):
    """流式恢复完整解析行；不修补中间损坏，不修改源文件。
    Stream complete parsed rows; reject interior corruption and leave the source untouched.
    """
    with Path(source).open("rb") as handle:
        first = handle.readline().decode("utf-8")
        suffix = ',"rows":[\n'
        if not first.endswith(suffix):
            raise ValueError("incomplete parsed-result header")
        header = json.loads(first[:-len(suffix)] + "}")
        if header.get("schema") != RESULT_SCHEMA or header.get("aggregation") != AGGREGATION:
            raise ValueError("unsupported parsed-result schema")
        counters = [CounterDefinition(**c) for c in header["counters"]]
        writer = ResultWriter(output, "json", counters, header.get("device"), header)
        try:
            for raw_line in handle:
                # 先检查换行再解码，末尾可能只写出了中文字符的部分 UTF-8 字节。
                # Check framing before decoding: the tail may split a multibyte UTF-8 character.
                if not raw_line.endswith(b"\n"):
                    break
                line = raw_line.decode("utf-8")
                if line.startswith('],"result":'):
                    json.loads('{"rows":[]' + line[1:])
                    if handle.read(1):
                        raise ValueError("trailing data after result footer")
                    break
                prefix = "," if writer.rows else ""
                if not line.startswith(prefix + "{"):
                    raise ValueError("invalid parsed-result row framing")
                writer.write(WindowRecord(**json.loads(line[len(prefix):])))
            return writer.finish("recovered_after_interruption", failed=True)
        finally:
            writer.close()


@dataclass
class RunResult:
    path: Path | None
    status: str
    details: dict = field(default_factory=dict)


def make_header(numer=1, denom=1, report_interval=1.0, device=None, source="device"):
    return {"schema_version": 1, "timebase": {"numer": numer, "denom": denom},
            "device": dict(device or {}, scope="device"),
            "session": {"started_at": utc_now(), "report_interval": report_interval,
                        "source": source, "dependencies": {
                            p: importlib.metadata.version(p) for p in ("pymobiledevice3", "quickjs-ng")}},
            "validation": "GPU-only path validated on iPhone 13 Pro / iOS 18.5; other devices require validation"}


@contextlib.contextmanager
def sigint_stop_event():
    """兼容 Windows 的信号桥接，不依赖仅 Unix 支持的 add_signal_handler。
    Windows-compatible signal bridge; no Unix-only add_signal_handler required.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    old_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        yield stop
    finally:
        signal.signal(signal.SIGINT, old_handler)


class CaptureStopped(Exception):
    """初始化阶段的用户停止。 / User stop during initialization."""


async def stage_wait(awaitable, timeout, stage, stop=None):
    """给异步阶段设预算；不无限等待取消清理。 / Bound async stages without unbounded cancellation waits."""
    if timeout <= 0 or (stop is not None and stop.is_set()):
        if asyncio.isfuture(awaitable):
            awaitable.cancel()
        if hasattr(awaitable, "close"):
            awaitable.close()
        if stop is not None and stop.is_set():
            raise CaptureStopped("user_stop_during_" + stage)
        raise TimeoutError(stage + "_timeout")
    task = asyncio.ensure_future(awaitable)
    stopper = asyncio.create_task(stop.wait()) if stop is not None else None
    try:
        done, _ = await asyncio.wait([task, stopper] if stopper else [task],
                                     timeout=max(0, timeout), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        if stopper in done:
            raise CaptureStopped("user_stop_during_" + stage)
        raise TimeoutError(stage + "_timeout")
    except Exception as exc:
        exc.gpu_stage = stage
        raise
    finally:
        if stopper is not None:
            stopper.cancel()
        if not task.done():
            task.cancel()
            # 回收迟到的异常，不把超时任务当作成功。 / Consume late errors without treating timeout as success.
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


def _capture_worker_main(requests, replies, output, header, format, console):
    """子进程内唯一线程拥有 QuickJS 与文件；IPC 不写原始数据。
    One child thread owns QuickJS and files; IPC never persists raw data.
    """
    # Windows Ctrl+C 会广播到同控制台子进程，必须由父进程独占处理并安排保存。
    # Windows broadcasts Ctrl+C to console children; only the parent may handle it and coordinate saving.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    pipeline = None
    try:
        pipeline = ParsedPipeline(output, header, format, console)
        replies.put({"ok": True, "statistics": pipeline.statistics()})
        while True:
            command, args = requests.get()
            try:
                if command == "process":
                    pipeline.process(*args)
                    result = None
                elif command == "finish":
                    result = pipeline.finish(*args)
                else:
                    raise ValueError("unknown_worker_command")
                replies.put({"ok": True, "statistics": pipeline.statistics(),
                             "result": None if result is None else {"path": str(result.path), "status": result.status}})
                if command == "finish":
                    break
            except Exception as exc:
                replies.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:512],
                             "statistics": pipeline.statistics()})
    except Exception as exc:
        replies.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:512]})
    finally:
        if pipeline is not None:
            pipeline.close()


class CaptureWorker:
    """单个在途请求；超时后可终止工作进程，避免文件 I/O 挂住退出。
    One request in flight; terminate the worker on timeout rather than hang on file I/O.
    """
    def __init__(self, output, header, format, console):
        ctx = multiprocessing.get_context("spawn")
        self.requests, self.replies = ctx.Queue(maxsize=1), ctx.Queue(maxsize=1)
        self.requests.cancel_join_thread()
        self.process = ctx.Process(target=_capture_worker_main,
                                   args=(self.requests, self.replies, output, header, format, console), daemon=True)
        self.statistics = {"sample_count": 0, "sample_span_s": 0, "row_count": 0,
                           "has_values": False, "metadata_received": False, "console_error": None}
        self.broken = False
        self.closed = False
        self.process.start()

    async def receive(self, timeout):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                response = self.replies.get_nowait()
            except queue.Empty:
                if not self.process.is_alive():
                    self.broken = True
                    raise RuntimeError("capture_worker_exited")
                if asyncio.get_running_loop().time() >= deadline:
                    self.broken = True
                    raise TimeoutError("capture_worker_timeout")
                await asyncio.sleep(.01)
                continue
            self.statistics.update(response.get("statistics", {}))
            if not response["ok"]:
                raise RuntimeError(response["error"])
            return response.get("result")

    async def call(self, command, *args, timeout=10):
        if timeout <= 0:
            raise TimeoutError("capture_worker_budget_exhausted")
        if self.broken:
            raise RuntimeError("capture_worker_unavailable")
        self.requests.put_nowait((command, args))
        try:
            return await self.receive(timeout)
        except asyncio.CancelledError:
            self.broken = True
            raise

    async def close(self):
        if self.closed:
            return
        if self.process.is_alive():
            self.process.terminate()
        deadline = asyncio.get_running_loop().time() + 1
        while self.process.is_alive() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.01)
        if self.process.is_alive():
            self.process.kill()
            raise TimeoutError("capture_worker_termination_timeout")
        self.process.join(timeout=0)
        self.process.close()
        self.requests.close()
        self.replies.close()
        self.closed = True


def processing_chunks(payload, limit=512):
    """仅分割主机解码工作，保持设备记录原序；不改变采样或网络协议。
    Split host decoding work in record order without changing sampling or the wire protocol.
    """
    if (not isinstance(payload, list) or len(payload) < 5 or payload[0] != 1
            or type(payload[3]) is not int or payload[3] <= limit
            or not isinstance(payload[4], (bytes, bytearray))
            or not payload[4] or len(payload[4]) % payload[3]):
        yield payload
        return
    count, data = payload[3:5]
    width = len(data) // count
    for start in range(0, count, limit):
        size = min(limit, count - start)
        chunk = list(payload)
        chunk[3:5] = [size, data[start * width:(start + size) * width]]
        yield chunk


async def collect_parsed(service, worker, output, format="csv", stop_event=None,
                         timeout=10, idle_timeout=30, drain_timeout=2, shutdown_timeout=25,
                         cleanup=None, on_started=None):
    """正式采集生命周期；收尾共享预算，错误不能跳过停止或关闭。
    Production lifecycle with a shared shutdown budget and unconditional stop/close attempts.
    """
    loop = asyncio.get_running_loop()
    stop = stop_event or asyncio.Event()
    started = last_message = loop.time()
    reason, errors, lifecycle = "user_stop", [], []
    processing_ok = True
    pending_chunks = None
    shutdown_deadline = None
    progress = CaptureProgress(lambda: worker.statistics)
    progress.phase = "正在启动 / starting"
    progress_task = asyncio.create_task(progress.run())
    debug = getattr(service, "debug", None)

    def failure(stage, exc):
        if len(errors) < 16:
            errors.append(f"{stage}: {type(exc).__name__}: {exc}"[:512])

    def budget():
        return max(0, min(timeout, shutdown_deadline - loop.time())) if shutdown_deadline else timeout

    async def rpc(name, operation, can_stop=False):
        try:
            reply = await stage_wait(operation(), budget(), name, stop if can_stop else None)
            if debug is not None:
                debug.record("rpc." + name, reply)
            if isinstance(reply, NSError):
                raise ValueError(f"device_error: {reply.domain}/{reply.code}")
            lifecycle.append({"stage": name, "status": "ok", "samples_processed": worker.statistics["sample_count"]})
            return reply
        except BaseException:
            lifecycle.append({"stage": name, "status": "error", "samples_processed": worker.statistics["sample_count"]})
            raise

    async def process(payload, interruptible=False):
        nonlocal processing_ok, pending_chunks
        if not processing_ok:
            return
        previous_phase = progress.phase
        if isinstance(payload, list) and len(payload) >= 5 and payload[0] == 1:
            progress.phase = "解析设备批次 / processing batch"
        try:
            chunks = iter(processing_chunks(payload))
            for chunk in chunks:
                await worker.call("process", chunk, timeout=budget())
                if interruptible and (stop.is_set() or service.closed.is_set()):
                    # 当前子批确认后立即允许 stop；剩余早期记录必须先于后续通知和 flush 解码。
                    # Allow stop after the current chunk; decode its remainder before later notifications and flush.
                    pending_chunks = chunks
                    return False
            return True
        except Exception:
            processing_ok = False
            raise
        finally:
            progress.phase = previous_phase

    async def drain(grace):
        until = min(shutdown_deadline, loop.time() + grace)
        while processing_ok and (not service.events.empty() or (loop.time() < until and not service.closed.is_set())):
            if loop.time() >= shutdown_deadline:
                raise TimeoutError("drain_budget_exhausted")
            try:
                payload = await asyncio.wait_for(service.events.get(), min(.1, max(.001, until - loop.time())))
            except asyncio.TimeoutError:
                continue
            await process(payload)

    try:
        await rpc("start", service.start, can_stop=True)
        progress.phase = "等待首批 / waiting for first batch"
        if on_started:
            on_started.set()
        print("已启动 GPU Counter，等待设备批次 / GPU started; waiting for device batches", file=sys.stderr, flush=True)
        while True:
            now = loop.time()
            if debug is not None:
                debug.check()
            if stop.is_set():
                reason = "user_stop"
                break
            if service.closed.is_set():
                raise ConnectionError(service.failure or "connection_closed")
            if not worker.statistics["metadata_received"] and now - started > timeout:
                raise TimeoutError("metadata_timeout")
            if not worker.statistics["sample_count"] and now - started > max(120, timeout):
                raise TimeoutError("first_sample_timeout")
            if service.events.empty() and now - last_message >= idle_timeout:
                raise TimeoutError("notification_idle_timeout")
            try:
                payload = await asyncio.wait_for(service.events.get(), .1)
            except asyncio.TimeoutError:
                continue
            last_message = loop.time()
            if not await process(payload, interruptible=True):
                if service.closed.is_set() and not stop.is_set():
                    raise ConnectionError(service.failure or "connection_closed")
                break
    except CaptureStopped:
        reason = "user_stop"
    except asyncio.CancelledError as exc:
        reason = "cancelled"
        failure("capture", exc)
        processing_ok = not worker.broken
    except Exception as exc:
        reason = "capture_error"
        failure("capture", exc)
    finally:
        elapsed = loop.time() - started
        shutdown_started = loop.time()
        shutdown_deadline = shutdown_started + shutdown_timeout
        progress.phase = "正在收尾 / draining"
        print("正在停止并保存尾部数据 / Stopping and draining GPU data", file=sys.stderr, flush=True)
        try:
            await rpc("stop", service.stop)
        except Exception as exc:
            failure("stop", exc)
        try:
            if pending_chunks is not None and processing_ok:
                for chunk in pending_chunks:
                    await process(chunk)
            await drain(drain_timeout)
            # 只有早期消息全部解码后才能取尾；解析失效时保留已有结果并停止消费。
            # Fetch the tail only after earlier messages; stop consuming after parsing becomes invalid.
            if processing_ok:
                remaining = await rpc("flush", service.flush)
                if isinstance(remaining, list) and (not remaining or isinstance(remaining[0], list)):
                    for message in remaining:
                        await process(message)
                elif remaining is not None:
                    await process(remaining)
                await drain(drain_timeout)
        except Exception as exc:
            failure("drain_or_flush", exc)
        if service.failure:
            failure("service", ConnectionError(service.failure))
        if cleanup is not None:
            progress.phase = "关闭连接 / closing connection"
            try:
                await stage_wait(cleanup(), budget(), "connection_close")
            except Exception as exc:
                failure("connection_close", exc)
        stats = dict(worker.statistics)
        if elapsed >= 5 and stats["sample_span_s"] < elapsed - 2:
            failure("coverage", ValueError(f"samples={stats['sample_span_s']:.3f}s, collection={elapsed:.3f}s"))
        details = {"stop_reason": reason, "collection_elapsed_s": elapsed, **stats,
                   "errors": errors, "lifecycle": lifecycle}
        if isinstance(service.events, GpuEventQueue):
            details["receive_queue"] = {"peak_messages": service.events.peak_messages,
                                        "peak_payload_bytes": service.events.peak_bytes,
                                        "max_messages": service.events.maxsize,
                                        "max_payload_bytes": service.events.max_bytes}
        result = None
        progress.phase = "完成文件 / finalizing output"
        try:
            if worker.broken or loop.time() >= shutdown_deadline:
                raise TimeoutError("finalization_budget_exhausted_or_worker_unavailable")
            saved = await worker.call("finish", reason, bool(errors), details, timeout=budget())
            details.update(worker.statistics)
            result = RunResult(Path(saved["path"]), saved["status"], details)
        except Exception as exc:
            failure("finalize", exc)
        finally:
            try:
                await worker.close()
            except Exception as exc:
                failure("worker_close", exc)
        if result is None:
            path = Path(output) if Path(output).exists() else Path(str(output) + ".part")
            result = RunResult(path if path.exists() else None, "partial" if stats["has_values"] else "failed", details)
        details["shutdown_elapsed_s"] = loop.time() - shutdown_started
        if errors and result.status == "completed":
            result.status = "partial"
        if details.get("console_error"):
            print("终端输出已关闭 / Console disabled: " + details["console_error"], file=sys.stderr)
        for error in errors:
            print("采集异常 / Capture error: " + error, file=sys.stderr)
        if errors:
            print("下一步 / Next: " + error_guidance(RuntimeError(errors[0])), file=sys.stderr)
        progress.stopped.set()
        await progress_task
        return result


@contextlib.asynccontextmanager
async def connect_device(args):
    """仅对上游公开 API 做轻量编排，此处不实现传输协议。
    Thin orchestration of upstream public APIs; no transport implementation here.
    """
    ensure_transport()
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
    from pymobiledevice3.tunneld.api import get_tunneld_devices
    from pymobiledevice3 import usbmux

    async with contextlib.AsyncExitStack() as stack:
        if args.rsd:
            provider = await stack.enter_async_context(RemoteServiceDiscoveryService((args.rsd[0], int(args.rsd[1]))))
        elif args.tunnel:
            providers = await get_tunneld_devices()
            for candidate in providers:
                stack.push_async_callback(candidate.close)
            selected = [p for p in providers if not args.udid or p.udid == args.udid]
            if len(selected) != 1:
                raise ValueError("no_device_in_tunneld" if not selected else
                                 "multiple_devices: " + ", ".join(p.udid for p in selected))
            provider = selected[0]
        else:
            serial = args.udid
            if not serial:
                devices = await usbmux.select_devices_by_connection_type(connection_type="USB")
                if len(devices) != 1:
                    raise ValueError("no_device_connected" if not devices else
                                     "multiple_devices: " + ", ".join(d.serial for d in devices))
                serial = devices[0].serial
            kwargs = {"serial": serial}
            if getattr(args, "command", None) == "doctor":
                kwargs["autopair"] = False
            lockdown = await stack.enter_async_context(await create_using_usbmux(**kwargs))
            if getattr(lockdown, "paired", True) is False:
                raise ValueError("trust_required")
            if int(lockdown.product_version.split(".")[0]) >= 17:
                # Windows 上旧版 iOS 的隧道前置准备由 pymobiledevice3 CLI 管理。
                # Older Windows tunnel prerequisites are managed by pymobiledevice3 CLI.
                provider = await stack.enter_async_context(PreferredRsdTunnel(serial=serial))
            else:
                provider = lockdown
        if args.udid and provider.udid != args.udid:
            raise ValueError("selected_device_mismatch")
        yield provider


async def close_gpu_connection(dvt):
    """先停读再交给库关闭，避免已完成的读回调在库 feed_eof 后继续投递。
    Pause reads before library teardown so completed reads cannot deliver data after its feed_eof.
    """
    try:
        connection = getattr(dvt, "_dtx", None)
        writer = getattr(connection, "_writer", None)
        if writer is not None:
            # _writer 来自锁定的 pymobiledevice3 版本；实际停止读取使用 asyncio 公共接口。
            # _writer belongs to the pinned pymobiledevice3 version; pausing uses asyncio's public API.
            writer.transport.pause_reading()
    finally:
        await dvt.close()


async def prepare_gpu(args, stack, stop, configure=True, report=None):
    """采集与检查共用设备服务准备，不启动任何性能采样器。
    Shared service preparation for capture and inspection; no performance sampler starts here.
    """
    ensure_transport()
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService

    def checked(stage, detail):
        if report:
            report(stage, "ok", detail)

    provider = await stage_wait(stack.enter_async_context(connect_device(args)), args.connect_timeout, "connect", stop)
    model = getattr(provider, "product_type", None)
    checked("设备连接与信任 / connection", f"{model or 'unknown'} / iOS {provider.product_version}")
    dvt = DvtProvider(provider)
    stack.push_async_callback(close_gpu_connection, dvt)
    await stage_wait(dvt.connect(), args.timeout, "dvt_connect", stop)
    checked("开发者服务 / developer service", "DVT connected")
    service = await stage_wait(dvt.dtx.open_channel(GpuService), args.timeout, "gpu_channel", stop)
    debug = getattr(args, "debug_recorder", None)
    service.debug = debug
    info = await stage_wait(service.device_info(), args.timeout, "gpu_info", stop)
    if debug is not None:
        debug.record("rpc.device_info", info)
    candidates = info if isinstance(info, list) else [info]
    if not candidates or any(not isinstance(c, dict) for c in candidates):
        raise ValueError("invalid_gpu_device_info")
    selected = [c for c in candidates if args.accelerator_id is None or c.get("accelerator-id") == args.accelerator_id]
    if len(selected) != 1:
        raise ValueError("multiple_gpu_candidates: " + ", ".join(str(c.get("accelerator-id")) for c in candidates))
    gpu = selected[0]
    profiles = gpu.get("supported-counter-profiles")
    if profiles is not None and args.counter_profile not in profiles:
        raise ValueError(f"unsupported_counter_profile: requested={args.counter_profile}, supported={profiles}")
    checked("GPU 能力 / GPU capability", f"{gpu.get('device-name', 'unknown')}, profiles={profiles or 'unknown'}")
    device_info = await stage_wait(stack.enter_async_context(DeviceInfo(dvt)), args.timeout, "timebase_channel", stop)
    mach = await stage_wait(device_info.mach_time_info(), args.timeout, "mach_timebase", stop)
    if debug is not None:
        debug.record("rpc.mach_time_info", mach)
    if len(mach) < 3 or int(mach[1]) <= 0 or int(mach[2]) <= 0:
        raise ValueError("invalid_mach_timebase")
    checked("设备时间基准 / timebase", f"{mach[1]}/{mach[2]}")
    if configure:
        reply = await stage_wait(service.configure(PInt64(gpu["accelerator-id"]), PInt64(args.counter_profile),
                                                   PInt64(args.sampling_interval), PInt64(args.window_limit), PInt32(-1)),
                                 args.timeout, "configure", stop)
        if debug is not None:
            debug.record("rpc.configure", reply)
        if isinstance(reply, NSError) or reply is False:
            raise ValueError("gpu_configuration_rejected")
    verified = (model == "iPhone14,2" and provider.product_version == "18.5" and gpu.get("device-name") == "A15"
                and args.counter_profile == 3 and args.sampling_interval == 0 and args.window_limit == -1)
    device = {"model": model, "ios_version": provider.product_version, "gpu_name": gpu.get("device-name"),
              "compatibility": "validated_baseline" if verified else "unverified"}
    checked("兼容范围 / compatibility", "已验证基线 / validated baseline" if verified else "未验证 / unverified")
    if isinstance(provider, RemoteServiceDiscoveryService) and provider.lockdown is not None:
        # GPU 已有独立 DTX 通道，后续服务端口来自缓存的 RSD 信息，不再使用远程 lockdown 查询。
        # GPU owns a separate DTX channel; cached RSD discovery suffices without further remote lockdown queries.
        # 在初始化结束时调用库的幂等 close，避免把闲置连接保留至长采集结束后才关闭。
        # Use the library's idempotent close at initialization instead of retaining an idle connection until teardown.
        await stage_wait(provider.lockdown.close(), args.timeout, "initial_remote_lockdown_close", stop)
    return service, mach, device


def local_checks():
    """只读依赖诊断，使用当前解释器，不安装组件。 / Read-only checks of the active interpreter, without installation."""
    checks = [("Python", "ok" if sys.version_info[:2] == (3, 12) else "warning" if sys.version_info[:2] > (3, 12)
               else "failed", sys.version.split()[0] + " / " + sys.executable),
              ("平台 / platform", "ok" if platform.system() == "Windows" and platform.machine().lower() in ("amd64", "x86_64")
               else "warning", platform.system() + " / " + platform.machine())]
    for name, expected in (("pymobiledevice3", "11.39.0"), ("quickjs-ng", "0.18.0.1")):
        try:
            version = importlib.metadata.version(name)
            checks.append((name, "ok" if version == expected else "warning", f"installed={version}, baseline={expected}"))
        except importlib.metadata.PackageNotFoundError:
            checks.append((name, "failed", "未安装 / missing"))
    if DTX_IMPORT_ERROR:
        checks.append(("DTX 导入 / import", "failed", DTX_IMPORT_ERROR))
    try:
        import quickjs
        if quickjs.Context().eval("1+1") != 2:
            raise ValueError("unexpected QuickJS result")
        checks.append(("QuickJS 引擎 / engine", "ok", "本机计算可用 / local evaluation works"))
    except Exception as exc:
        checks.append(("QuickJS 引擎 / engine", "failed", str(exc)))
    return checks


async def devices_command(args):
    """保留不可读取设备的 UDID，单台失败不隐去其他设备。
    Retain unreadable devices' UDIDs; one failed lookup must not hide other devices.
    """
    ensure_transport()
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux

    failures = False
    rows = []
    with sigint_stop_event() as stop:
        devices = await stage_wait(usbmux.list_devices(), args.timeout, "usbmux_list", stop)
        for device in devices:
            if args.connection != "all" and device.connection_type != args.connection:
                continue
            info = {"udid": device.serial, "connection": device.connection_type,
                    "name": "unknown", "model": "unknown", "ios": "unknown", "state": "unknown"}
            lockdown = None
            try:
                lockdown = await stage_wait(create_using_usbmux(serial=device.serial, autopair=False,
                                                               connection_type=device.connection_type),
                                            args.timeout, "device_information", stop)
                values = getattr(lockdown, "all_values", {})
                info.update(name=values.get("DeviceName", "unknown"), model=lockdown.product_type,
                            ios=lockdown.product_version, state="paired" if lockdown.paired else "unpaired")
            except CaptureStopped:
                raise
            except Exception as exc:
                failures = True
                info["state"] = "信息不可读 / information unavailable"
                print_error(exc)
            finally:
                if lockdown is not None:
                    try:
                        await stage_wait(lockdown.close(), min(args.timeout, 5), "device_information_close")
                    except Exception as exc:
                        failures = True
                        print_error(exc)
            rows.append(info)
    output = csv.writer(sys.stdout, lineterminator="\n")
    output.writerow(["名称 / Name", "型号 / Model", "iOS", "连接 / Connection", "UDID", "配对 / Pairing"])
    for row in rows:
        output.writerow([row[k] for k in ("name", "model", "ios", "connection", "udid", "state")])
    if not rows:
        print_error(ValueError("no_device_connected"))
    else:
        print(f"发现 / found {len(rows)} 条连接；采集用 --udid 选择，兼容状态以 doctor/counters 为准。", file=sys.stderr)
    return RunResult(None, "failed" if not rows else "partial" if failures else "completed", {"devices": rows})


def metadata_definitions(body):
    """只读取名称与说明，不执行或输出设备公式。 / Read names/descriptions without running or printing formulas."""
    definitions = body.get("0", body.get(0)) if isinstance(body, dict) else body
    if not isinstance(definitions, list) or any(not isinstance(c, list) or not c for c in definitions):
        raise ValueError("invalid_counter_metadata")
    counters = [CounterDefinition(c[0], description=c[2] if len(c) > 2 and isinstance(c[2], str) else "") for c in definitions]
    counter_names(counters)
    return counters


async def inspection_command(args):
    """doctor 不启动采样；counters 只等待元数据并可靠停止。
    Doctor never starts sampling; counters waits only for metadata and always attempts stop.
    """
    checks = []

    def report(name, status, detail):
        checks.append({"check": name, "status": status, "detail": str(detail)})
        if args.command == "doctor":
            print(f"[{status.upper()}] {name}: {detail}", flush=True)

    for check in local_checks():
        report(*check)
    if any(c["status"] == "failed" for c in checks):
        if any(c["check"] == "Python" and c["status"] == "failed" for c in checks):
            print_error(ValueError("unsupported_python"))
        else:
            print_error(ImportError("本机 Python/依赖检查失败 / local runtime check failed"))
        return RunResult(None, "failed", {"checks": checks})
    stack = contextlib.AsyncExitStack()
    service = None
    attempted_start = False
    counters = None
    device = None
    interrupted = False
    with sigint_stop_event() as stop:
        try:
            service, _, device = await prepare_gpu(args, stack, stop, configure=args.command == "counters", report=report)
            if args.command == "counters":
                attempted_start = True
                reply = await stage_wait(service.start(), args.timeout, "counter_metadata_start", stop)
                if isinstance(reply, NSError) or reply is False:
                    raise ValueError("gpu_counter_start_rejected")
                deadline = asyncio.get_running_loop().time() + args.timeout
                while counters is None:
                    if service.closed.is_set():
                        raise ConnectionError(service.failure or "gpu_connection_closed")
                    payload = await stage_wait(service.events.get(), deadline - asyncio.get_running_loop().time(), "counter_metadata", stop)
                    if isinstance(payload, NSError):
                        raise ValueError(f"device_error: {payload.domain}/{payload.code}")
                    if isinstance(payload, list) and len(payload) >= 5 and payload[0] == 0:
                        counters = metadata_definitions(payload[4])
                    elif not (isinstance(payload, list) and len(payload) == 2 and payload[0] in (2, 3)):
                        raise ValueError("unexpected_notification_before_metadata")
        except CaptureStopped:
            interrupted = True
        except Exception as exc:
            report(getattr(exc, "gpu_stage", "检查 / inspection"), "failed", str(exc))
            print_error(exc)
        finally:
            if attempted_start:
                try:
                    reply = await stage_wait(service.stop(), args.timeout, "counter_metadata_stop")
                    if isinstance(reply, NSError) or reply is False:
                        raise ValueError("gpu_counter_stop_rejected")
                    # stop 回复不是异步通知成功的保证；检查短宽限期内的错误，样本仅丢弃。
                    # A stop reply does not confirm asynchronous success; check grace-period errors and discard samples.
                    until = asyncio.get_running_loop().time() + min(.2, args.timeout)
                    while asyncio.get_running_loop().time() < until:
                        try:
                            payload = await asyncio.wait_for(service.events.get(), max(.001, until - asyncio.get_running_loop().time()))
                        except asyncio.TimeoutError:
                            break
                        if isinstance(payload, NSError):
                            raise ValueError(f"device_error: {payload.domain}/{payload.code}")
                        if (counters is not None and isinstance(payload, list) and len(payload) >= 5 and payload[0] == 0
                                and metadata_definitions(payload[4]) != counters):
                            raise ValueError("metadata_changed")
                    if service.failure:
                        raise ConnectionError(service.failure)
                except Exception as exc:
                    report("停止 / stop", "failed", str(exc))
                    print_error(exc)
            try:
                await stage_wait(stack.aclose(), min(args.timeout, 5), "inspection_close")
            except Exception as exc:
                report("关闭 / close", "failed", str(exc))
                print_error(exc)
    if interrupted:
        raise CaptureStopped("inspection_interrupted")
    failed = any(c["status"] == "failed" for c in checks)
    if counters is not None:
        output = csv.writer(sys.stdout, lineterminator="\n")
        output.writerow(["Counter", "单位 / Unit", "说明 / Description"])
        for counter in counters:
            output.writerow([counter.name, counter.unit or "unknown / 未证实", counter.description])
        print(f"{len(counters)} 项指标；兼容 / compatibility={device['compatibility']}。只读取元数据，未保存样本。", file=sys.stderr)
    if args.command == "doctor" and not failed:
        print("服务检查通过；未启动采样，不代表数据完整性或跨设备验收。下一步运行 counters 或 capture。", file=sys.stderr)
    return RunResult(None, "failed" if failed else "completed", {"checks": checks})


async def live(args):
    format = getattr(args, "format", "csv")
    preflight_result_path(args.output, format)
    args.debug_recorder = None
    stack = contextlib.AsyncExitStack()
    worker = None
    handed_off = False
    with sigint_stop_event() as stop:
        try:
            if getattr(args, "debug", False):
                root = getattr(args, "debug_directory", None) or (Path("captures") / datetime.now().strftime("%Y%m%d_%H%M%S_%f") / "debug")
                debug = DebugRecorder(root, args.output)
                args.debug_recorder = debug
                stack.push_async_callback(debug.close)
                await stage_wait(debug.start(args.timeout), args.timeout, "debug_start", stop)
                print(f"Debug 原始数据 / Raw debug data: {debug.root.resolve()}", file=sys.stderr, flush=True)
            print("正在连接设备 / Connecting to device", file=sys.stderr, flush=True)
            service, mach, device = await prepare_gpu(args, stack, stop)
            print("兼容基线 / Compatibility: " + ("已验证设备 / verified device" if device["compatibility"] == "validated_baseline"
                                                 else "未验证设备 / unverified device"), file=sys.stderr, flush=True)
            header = make_header(int(mach[1]), int(mach[2]), args.report_interval, device)
            if getattr(args, "debug_recorder", None) is not None:
                args.debug_recorder.record("session", header)
            worker = CaptureWorker(args.output, header, format, args.console)
            await stage_wait(worker.receive(args.timeout), args.timeout, "worker_start", stop)
            handed_off = True
            return await collect_parsed(service, worker, args.output, format, stop_event=stop, timeout=args.timeout,
                                        idle_timeout=getattr(args, "idle_timeout", 30),
                                        shutdown_timeout=getattr(args, "shutdown_timeout", 25),
                                        cleanup=stack.aclose, on_started=getattr(args, "on_started", None))
        finally:
            if not handed_off:
                if worker is not None:
                    await worker.close()
                try:
                    await stage_wait(stack.aclose(), min(args.timeout, 5), "initialization_cleanup")
                except Exception as exc:
                    print(f"初始化清理失败 / Initialization cleanup failed: {exc}", file=sys.stderr)


def run_capture(awaitable):
    """CLI 的有界事件循环收尾；避免 asyncio.run 无限等待拒绝取消的后台任务。
    Bounded CLI loop cleanup avoids asyncio.run waiting forever on cancellation-resistant tasks.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = None
    try:
        result = loop.run_until_complete(awaitable)
        return result
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                _, remaining = loop.run_until_complete(asyncio.wait(pending, timeout=1))
                if remaining:
                    print("后台任务取消超时 / Background cancellation timed out", file=sys.stderr)
                    if result is not None:
                        result.details.setdefault("errors", []).append("background_cancellation_timeout")
                        if result.status == "completed":
                            result.status = "partial"
        finally:
            loop.close()
            asyncio.set_event_loop(None)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                     epilog="建议流程 / Workflow: devices → doctor → counters → capture；Ctrl+C 停止录制")
    # 内部固定策略不是命令行选项；不向操作者暴露协议参数或定时录制。
    # Fixed runtime policy is not a CLI option; operators do not configure protocol details or timed capture.
    parser.set_defaults(timeout=10, connect_timeout=30, shutdown_timeout=25, idle_timeout=30,
                        report_interval=1, counter_profile=3, sampling_interval=0, window_limit=-1,
                        tunnel=False, rsd=None, accelerator_id=None, connection="all")
    sub = parser.add_subparsers(dest="command", required=True, title="命令 / Commands")
    capture = sub.add_parser("capture", allow_abbrev=False,
                             help="持续采集，Ctrl+C 结束并保存 / capture until Ctrl+C")
    doctor = sub.add_parser("doctor", allow_abbrev=False,
                            help="检查环境与设备服务 / check prerequisites")
    counters = sub.add_parser("counters", allow_abbrev=False,
                              help="查看设备提供的指标 / list available counters")
    devices = sub.add_parser("devices", allow_abbrev=False,
                             help="查看连接的设备 / list connected devices")
    for command in (capture, doctor, counters):
        command.add_argument("--udid", help="多设备时选择 UDID；单设备自动选择 / select a device when several are connected")
    capture.add_argument("--output", type=Path, help="保存路径；默认自动创建，不覆盖旧文件 / output path")
    capture.add_argument("--format", choices=("csv", "json"), default="csv", help="文件格式，默认 CSV / output format")
    capture.add_argument("--console", choices=("key", "all", "none"), default="key",
                         help="终端显示重点/全部/不显示数据；文件始终全量 / terminal counter selection")
    capture.add_argument("--debug", action="store_true",
                         help="额外在 captures 保存原始元数据、JS 和采样字节 / save raw GPU data under captures")
    for command in (parser, capture, doctor, counters, devices):
        command._positionals.title = "位置参数 / Arguments"
        command._optionals.title = "选项 / Options"
    return parser


def main():
    # 管道重定向也固定使用 UTF-8，确保 Windows 下双语帮助和日志能被一致读取。
    # Use UTF-8 for redirected streams too, keeping bilingual help and logs consistent on Windows.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "capture":
        run_directory = Path("captures") / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        if args.output is None:
            args.output = run_directory / ("gpu." + args.format)
        if args.debug:
            args.debug_directory = run_directory / "debug"
        if args.output.suffix.lower() != "." + args.format:
            parser.error("后缀须匹配 --format；JSON 请加 --format json / output suffix must match format")
    try:
        if args.command == "devices":
            result = run_capture(devices_command(args))
        elif args.command in ("doctor", "counters"):
            result = run_capture(inspection_command(args))
        else:
            result = run_capture(live(args))
            print(f"已保存 / Saved: {result.path.resolve() if result.path else '无结果文件 / no result file'} (status={result.status})",
                  file=sys.stderr)
            if result.details:
                print(f"已解析样本 / samples={result.details.get('sample_count', 0)}，"
                      f"窗口 / rows={result.details.get('row_count', 0)}，"
                      f"设备跨度 / span={result.details.get('sample_span_s', 0):.3f}s", file=sys.stderr)
            if result.path and result.path.suffix == ".part":
                print("保留了中断结果；使用 tools/recover_results.py 恢复到新路径 / Recover the .part file to a new path.", file=sys.stderr)
        return 0 if result.status == "completed" else 1
    except (KeyboardInterrupt, CaptureStopped):
        print("操作已中断 / Operation interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print_error(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
