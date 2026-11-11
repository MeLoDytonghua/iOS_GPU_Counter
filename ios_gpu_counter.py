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
# 协议上下文字段直接取当前值，不按普通累计计数器求差分。
# Protocol context fields use the current value rather than an ordinary accumulator delta.
SPECIAL_FIELDS = {
    "GRC_TIMESTAMP", "GRC_GPU_CYCLES", "GRC_ENCODER_ID",
    "GRC_KICK_TRACE_ID", "GRC_SAMPLE_TYPE",
}
KEY_COUNTERS = {
    "Vertex Occupancy", "Fragment Occupancy", "Compute Occupancy",
    "ALU Utilization", "GPU Read Bandwidth", "GPU Write Bandwidth",
}


# 阅读入口：GpuCounterApp.run → CaptureSession.run → DeviceSession.open → CaptureSession.collect_until_stopped。
# Reading route: GpuCounterApp.run → CaptureSession.run → DeviceSession.open → CaptureSession.collect_until_stopped.
# 主进程通信；解析子进程持有 QuickJS/结果文件；可选 Debug 子进程保存原始载荷。
# The parent owns transport; one child owns QuickJS/results; an optional debug child archives raw payloads.

# 1. 公共序列化与错误提示 / Shared serialization and error messages
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
    """将元数据转成可计算指纹的 JSON 值；完整原始类型由 DebugArchive 保留。
    Convert metadata to JSON values for fingerprinting; DebugArchive preserves original types.
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


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def require_transport_dependency():
    """在访问设备前检查 DTX 依赖；缺依赖时仍允许 CLI 帮助加载。
    Check DTX dependencies before device access while allowing CLI help to load without them.
    """
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


def print_status(message, stream=None):
    """状态输出不可写时继续收尾；数据输出由 CaptureOutput 单独处理。
    Keep cleanup running if status output is unavailable; CaptureOutput handles data output separately.
    """
    try:
        print(message, file=stream if stream is not None else sys.stderr, flush=True)
    except (OSError, UnicodeError, ValueError):
        pass


def print_error(exc):
    """显示异常所在阶段及下一步建议；不修改异常或决定采集状态。
    Show the failing stage and next steps without modifying the exception or deciding capture
    status.
    """
    phase = getattr(exc, "gpu_stage", "")
    print_status(f"错误 / Error [{phase or type(exc).__name__}]: {type(exc).__name__}: {exc}")
    print_status("下一步 / Next: " + error_guidance(exc))


# 2. 主进程进度与有界接收队列 / Parent-side progress and bounded receive queue
class CaptureStatusReporter:
    """只在等待状态切换及数据到达时提示，不定时刷新。
    Report waiting transitions and incoming data only, without periodic refresh.
    """
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stderr
        self.waiting = False

    def wait(self):
        if not self.waiting:
            self.waiting = True
            print_status("[等待数据 / Waiting for data]", self.stream)

    def received(self, count):
        self.waiting = False
        print_status(f"[收到数据 / Data received] samples: {count}", self.stream)


class GpuMessageQueue(asyncio.Queue):
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
        """估算队列载荷预算，包含 NSError 内嵌内容；不是精确的进程内存计量。
        Estimate the queue payload budget including nested NSError content, not exact process memory
        usage.
        """
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
        """同时检查消息数与字节预算；满队列抛错，不移除旧消息腾空间。
        Check message and byte limits together; raise on overflow without evicting older messages.
        """
        size = self.weight(item)
        if self.payload_bytes + size > self.max_bytes:
            raise asyncio.QueueFull
        super().put_nowait(item)
        self.payload_bytes += size
        self.peak_bytes = max(self.peak_bytes, self.payload_bytes)
        self.peak_messages = max(self.peak_messages, self.qsize())

    def get_nowait(self):
        """取出一条消息并释放其预算；此时消息尚不一定已完成解析。
        Remove one message and release its budget; parsing may still be pending.
        """
        item = super().get_nowait()
        self.payload_bytes -= self.weight(item)
        return item


# 3. 可选 Debug：接收原始载荷，独立进程写盘 / Optional debug: archive raw payloads in a separate process
class DebugArchive:
    """仅在调试子进程写入未经解析的服务载荷。 / Write unparsed service payloads only in the debug child process."""
    def __init__(self, root, output):
        """排他创建本次 Debug 目录及日志；已存在目录直接拒绝，避免覆盖证据。
        Exclusively create this run's debug directory and journal; reject existing directories to
        avoid overwriting evidence.
        """
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
        """先保存二进制/脚本，再提交引用它们的消息行。
        Write binary/script files before committing the message line that references them.
        """
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
        """关闭日志并写最终记录状态；closed 表示原始记录结束，不代表采集质量完整。
        Close the journal and save recording state; closed means recording ended, not complete
        capture quality.
        """
        self.journal.close()
        self.session.update(state=state, ended_at=utc_now())
        if error:
            self.session["error"] = error
        self.save_session()


def _debug_writer_entry(requests, replies, root, output):
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
        """创建并启动原始写入子进程；需 await start 确认文件初始化成功后才能使用。
        Create and launch the raw-writer process; await start to confirm file initialization before
        use.
        """
        self.root = Path(root)
        ctx = multiprocessing.get_context("spawn")
        self.requests, self.replies = ctx.Queue(maxsize=2048), ctx.Queue()
        self.requests.cancel_join_thread()
        self.process = ctx.Process(target=_debug_writer_entry,
                                   args=(self.requests, self.replies, root, output), daemon=True)
        self.pending_bytes = 0
        self.record_count = 0
        self.max_bytes = 64 * 1024 * 1024
        self.ready = self.finished = self.disposed = False
        self.failure = None
        self.process.start()

    def check(self):
        """非阻塞读取回执、释放字节预算并传播后台失败；失败状态不会被后续检查清除。
        Read acknowledgments without blocking, release byte budgets and propagate persistent
        background failures.
        """
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
        """等待子进程 ready 回执，不再次创建进程；timeout 是等待就绪的秒数。
        Wait for the child ready acknowledgment without launching another process; timeout is
        readiness wait time in seconds.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.ready:
            self.check()
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("debug_start_timeout")
            await asyncio.sleep(.01)

    def record(self, event, payload):
        """只入队不执行文件 I/O；确认回执释放已排队的字节预算。
        Enqueue without file I/O; write acknowledgments release the queued byte budget.
        """
        self.check()
        if self.finished or self.disposed:
            raise RuntimeError("debug_recorder_closed")
        size = GpuMessageQueue.weight(payload)
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
        """提交最终消息数，等待所有已接受记录写完；超时/失败时仍回收子进程与队列。
        Submit the final message count and await accepted records; reclaim the child and queues even
        on failure or timeout.
        """
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


# 4. GPU 服务接口：库负责 DTX 编解码，本类只声明业务方法 / GPU API: the library owns DTX encoding; this class declares methods
class GpuCounterService(DTXService):
    """RPC 的 await 返回对应回复；采样通过 notification 独立到达。
    Each awaited RPC returns its reply; sample notifications arrive independently.
    """
    IDENTIFIER = GPU_CHANNEL

    def __init__(self, ctx):
        super().__init__(ctx)
        self.events = GpuMessageQueue()
        self.closed = asyncio.Event()
        self.failure: str | None = None
        self.debug = None

    @dtx_method("requestDeviceGPUInfo")
    async def device_info(self): ...

    # 装饰器把调用编码为设备 RPC；类型声明控制协议整数宽度，函数体不是空操作。
    # The decorator encodes a device RPC; annotations set protocol integer widths, so the stub is not a no-op.
    @dtx_method("configureCounters:counterProfile:interval:windowLimit:tracingPID:")
    async def configure(self, accelerator: PInt64, profile: PInt64, interval: PInt64,
                        window: PInt64, pid: PInt32): ...

    @dtx_method("startCollectingCounters")
    async def start(self): ...

    # stop 后仍须排空已收通知并 flush；仅收到 stop 回复不能证明尾部已经到齐。
    # Drain notifications and flush after stop; a stop reply alone does not prove the tail has arrived.
    @dtx_method("stopCollectingCounters")
    async def stop(self): ...

    # 仅在停止流程调用，返回值可能包含多个尾包。 / Call only during shutdown; the reply may contain multiple tail messages.
    @dtx_method("flushRemainingData")
    async def flush(self): ...

    def enqueue(self, payload):
        """原始记录先入 Debug 队列，再入解析队列；任一队列失败会通知会话停止。
        Queue raw recording before parsing; failure in either queue signals the session to stop.
        """
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
        """接收库已解码的异步服务载荷；这里仅入队，不执行设备公式。
        Receive asynchronously delivered, library-decoded payloads; enqueue only, without executing
        formulas.
        """
        self.enqueue(payload)

    @dtx_on_dispatch
    async def dispatch(self, selector, *args):
        # 保留未知分发消息供诊断，不猜测其负载布局。
        # Preserve unknown dispatches for diagnostics; do not guess their payload layout.
        self.enqueue({"dispatch_selector": selector, "arguments": list(args)})

    def on_closed(self, reason=""):
        """保存关闭原因并唤醒会话；已经接收的消息仍留在队列中等待收尾。
        Store the close reason and notify the session while retaining queued messages for draining.
        """
        self.failure = self.failure or (reason or "connection_closed")
        # 不清除已经接收的样本。 / Never purge already received samples.
        self.closed.set()


# 5. 从样本字节到指标值 / From sample bytes to metric values
class DeviceFormulaEvaluator:
    """仅在采集管线的专用工作线程中创建和使用。
    Created and used only on the pipeline's dedicated worker thread.
    """

    def __init__(self, source, gpu_info, counters, numer, denom):
        """在解析进程的同一线程创建 QuickJS，加载设备参数和源码并限制资源；只调用定义表选定的函数。
        Create QuickJS on the parsing thread, load device inputs/source and bound resources; call
        only definition-selected functions.
        """
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
        """注入本样本变量并调用元数据指定的函数；不自行推导指标公式。
        Inject this sample's inputs and call metadata-selected functions without inventing metric formulas.
        """
        values = dict(values)
        values.update(MTLStat_nSec=interval_ns, SAMPLE_INTERVAL_USEC=interval_ns / 1000,
                      _LastTimestamp=previous_ns)
        return json.loads(self.run(dumps({"globals": values, "functions": self.functions})))


class GpuSampleDecoder:
    """跨包保存前值：configure_metadata 绑定字段，decode_samples 解包并计算指标。
    Retain previous records across packets: configure_metadata binds fields and decode_samples unpacks and computes metrics.
    """
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

    def configure_metadata(self, payload):
        """配置字段、显示定义和公式；0/1/2/3 分别为显示定义/原始字段/JS/GPU 参数。重置跨包前值，返回 None。
        Configure display definitions, raw fields, JS and GPU parameters from keys 0/1/2/3. Reset
        previous-record state and return None.
        """
        self.version += 1
        self.previous = None
        self.engine = None
        self.engine_error = None
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
                self.engine = DeviceFormulaEvaluator(source, gpu, self.counters, self.numer, self.denom)
            except Exception as exc:
                self.engine_error = f"formula_initialization: {exc}"

    def decode_samples(self, count, data):
        """按 count 和字段数校验小端 uint64 字节长度，逐条生成样本字典并保留跨包前值。布局损坏抛错；数值异常通过 status/errors 返回。
        Validate little-endian uint64 byte length against count/fields, yield sample dictionaries
        and retain cross-packet state. Malformed layouts raise; numeric failures return
        status/errors.
        """
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
            # 倒序或同时间戳的异常记录不替换基线；递增记录可作为下一条的原始基线。
            # Backwards or equal-time invalid records must not replace the baseline; increasing records can seed the next delta.
            if previous is None or timestamp > previous[0]:
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
            values = self._prepare_formula_inputs(raw, previous, sample)
            if values is not None:
                self._evaluate_counters(values, previous[0], sample)
            yield sample

    def _prepare_formula_inputs(self, raw, previous, sample):
        """按字段定义差分并生成 _norm；异常时标记样本并返回 None。
        Difference fields and generate _norm inputs; mark invalid samples and return None.
        """
        error = None
        values = {}
        for i, (name, instantaneous, *_) in enumerate(self.raw_fields):
            value = raw[i]
            if name not in SPECIAL_FIELDS and not instantaneous:
                # previous 是完整记录，字段下标比 raw 多一个时间戳位置。 / previous includes the timestamp, so its field index is offset by one.
                value -= previous[i + 1]
                if value < 0:
                    # 不猜测计数位宽或回绕原因，所有设备统一跳过该样本。
                    # Never guess counter width or the cause of a decrease; skip this sample on every device.
                    error = "counter_reset_or_wrap"
                    sample["errors"][name] = "counter_decreased"
            # 使用元数据名称建立 JS 变量；raw 不包含记录开头的时间戳。
            # Build JS variables from metadata names; raw excludes the leading record timestamp.
            values[name.replace(" ", "_").replace(":", "_")] = value
        if any(abs(v) > SAFE_INTEGER for v in values.values()):
            error = error or "unsafe_javascript_integer"
        if error:
            sample["status"] = error
            return None
        cycles = values.get("GRC_GPU_CYCLES")
        if cycles is None or cycles <= 0:
            sample["status"] = "missing_or_zero_gpu_cycles"
            return None
        # 同时保留原值/差分值与每周期归一化值，由设备 JS 选择。
        # Keep both raw/delta and per-cycle normalized values for the device JS to choose.
        values.update({k + "_norm": v / cycles for k, v in list(values.items())})
        return values

    def _evaluate_counters(self, values, previous_timestamp, sample):
        """调用设备公式并按设备 scale/maximum 显示，不加入机型公式。
        Call device formulas and apply their scale/maximum without model-specific formulas.
        """
        try:
            if self.engine is None:
                raise ValueError(self.engine_error or "formula engine unavailable")
            # 保留设备的绝对纳秒时间，以兼容设备公式。
            # Absolute device nanoseconds are retained for device formula compatibility.
            previous_ns = previous_timestamp * self.numer / self.denom
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


# 6. 窗口与输出：共享一份 WindowRecord，文件与终端不各自重算 / Windows and output share one WindowRecord without recalculation
RESULT_SCHEMA = "ios_gpu_counter.rows.v2"
AGGREGATION = "arithmetic_mean_of_valid_samples"


@dataclass(frozen=True)
class CounterDefinition:
    """仅保存解释结果所需的定义，不包含设备公式。 / Result definitions exclude device formulas."""
    name: str
    unit: str | None = None
    description: str = ""


def validate_counter_definitions(counters):
    """检查名称唯一及定义类型，返回保持设备顺序的名称列表；写入器与聚合器共用此契约。
    Validate unique names and definition types, returning device-ordered names shared by writers and
    aggregators.
    """
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
    adjusted_sample_count: int  # 读取旧结果用；当前采集恒为 0。 / Retained for old results; current captures always emit 0.
    valid_counts: dict[str, int]
    values: dict[str, float | None]

    def validate(self, names):
        """校验时间、字段集合、有效分母和质量状态一致性；写入与恢复都使用相同检查。
        Validate consistency of time, field sets, denominators and quality for both writing and
        recovery.
        """
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
        """依据样本数与各指标有效数判断窗口质量，独立于尾窗是否满一秒。
        Derive window quality from sample and per-counter valid counts, independently of tail-window
        duration.
        """
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
        self.names = validate_counter_definitions(counters)
        self.interval, self.emit = interval, emit
        self.bucket = None
        self.last_time = None
        self.finished = False
        self._reset()

    def _reset(self):
        self.sums = dict.fromkeys(self.names, 0.0)
        self.counts = dict.fromkeys(self.names, 0)
        self.samples = self.baselines = self.duplicates = self.invalid = 0

    def _emit(self, end, partial):
        eligible = self.samples - self.baselines - self.duplicates
        record = WindowRecord(
            self.bucket * self.interval, end, partial, self.samples,
            WindowRecord.quality(self.samples, eligible, self.invalid, self.counts),
            self.baselines, self.duplicates, self.invalid, 0,
            dict(self.counts), {n: self.sums[n] / self.counts[n] if self.counts[n] else None for n in self.names})
        record.validate(self.names)
        self.emit(record)
        self._reset()

    def add(self, sample):
        """按设备相对结束时间归入窗口；仅累计有效指标值，后续样本才确认空窗，倒序记录不回写历史。
        Bucket by device-relative sample end time; sum valid values, confirm gaps only with later
        samples and never rewrite past windows for reversed records.
        """
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
        """只输出一次当前尾窗，结束时间取最后观测值；不补造最后样本之后的数据。
        Emit the current tail once, ending at the last observation without inventing later data.
        """
        if not self.finished and self.bucket is not None:
            self._emit(self.last_time, True)
        self.finished = True


def check_output_path(output, format):
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
        """验证输出路径与字段，排他打开 CSV 或 JSON .part；仅保存窗口结果，不持有整场记录。
        Validate the path/fields and exclusively open CSV or JSON .part; persist windows without
        retaining the full recording.
        """
        self.names = validate_counter_definitions(counters)
        if format == "csv" and "time_ms" in self.names:
            raise ValueError("csv_time_column_conflict: time_ms")
        self.format = format
        # 白名单阻止 header 中的原始消息、公式等意外落盘。
        # Allow lists prevent accidental persistence of raw metadata or formulas.
        device, session = device or {}, session or {}
        device_keys = ("name", "model", "ios_version", "gpu_name", "compatibility")
        header = {"schema": RESULT_SCHEMA, "aggregation": AGGREGATION,
                       "device": {k: device[k] for k in device_keys if isinstance(device.get(k), str)},
                       "scope": "device", "source": session.get("source", "unknown"),
                       "started_at": session.get("started_at", utc_now()),
                       "report_interval": session.get("report_interval", 1.0),
                       "counters": [asdict(c) for c in counters]}
        if (not isinstance(header["source"], str) or not isinstance(header["started_at"], str)
                or not isinstance(header["report_interval"], (int, float))
                or not math.isfinite(header["report_interval"]) or header["report_interval"] <= 0):
            raise ValueError("invalid result header")
        header_text = dumps(header)
        self.output = check_output_path(output, format)
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
        """先校验完整窗口再写入并 flush；写盘失败后关闭文件，禁止继续写入或完成。
        Validate a complete window before writing/flushing; close on write failure and reject
        further writes or finalization.
        """
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
        """根据有效值与质量确定状态，写 JSON 尾部并完成重命名；返回 CommandResult，不覆盖已有文件。
        Derive status from valid values/quality, write the JSON footer and rename; return
        CommandResult without overwriting existing files.
        """
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
        return CommandResult(self.output, status)

    def close(self):
        """仅释放文件句柄，不补尾部、不重命名；异常路径可保留 .part 供恢复。
        Release the handle without adding a footer or renaming; failure paths may retain .part for
        recovery.
        """
        self.handle.close()


class CaptureOutput:
    """先落文件再显示同一结果；终端故障不改变数据质量。
    Persist before displaying the same record; console failures do not change data quality.
    """
    def __init__(self, writer, console="key", stream=None):
        if console not in ("key", "all", "none"):
            raise ValueError("invalid console mode")
        self.writer, self.console = writer, console
        self.stream = stream if stream is not None else sys.stdout
        self.names = writer.names if console == "all" else sorted(KEY_COUNTERS)
        self.console_error = None

    def emit(self, record):
        """先持久化 WindowRecord，再向 stdout 显示所选指标；终端失败仅禁用展示，文件仍保留。
        Persist WindowRecord before displaying selected metrics on stdout; terminal failure disables
        display while retaining the file.
        """
        self.writer.write(record)
        if self.console == "none" or self.console_error:
            return
        try:
            # 指标放在 values 中，避免原名与时间/status 键冲突；保留原始数值精度。
            # Nest counters under values to avoid time/status key collisions and preserve numeric precision.
            row = {"start_s": record.start_s, "end_s": record.end_s, "status": record.status,
                   "values": {name: record.values.get(name) for name in self.names}}
            print(json.dumps(row, ensure_ascii=False, allow_nan=False), file=self.stream, flush=True)
        except (OSError, UnicodeError, ValueError) as exc:
            self.console_error = type(exc).__name__


class CaptureDataPipeline:
    """解析输出管线，负责指标解码与窗口写入；不写原始归档。
    Parsed output pipeline for metric decoding and window writing; no raw archive.
    """
    def __init__(self, output, header, format="csv", console="key", stream=None):
        self.path = check_output_path(output, format)
        self.header, self.format, self.console, self.stream = header, format, console, stream
        self.decoder = GpuSampleDecoder(**header["timebase"])
        self.writer = self.windows = self.output = None
        self.metadata_hash = None
        self.failed = False
        self.sample_count = 0
        self.sample_span_s = 0.0
        self.sample_errors = {}

    def report_sample_error(self, sample):
        """输出有界错误提示，不让连续异常刷屏。 / Report bounded errors without flooding on repeated invalid samples."""
        reason = sample["status"]
        if reason in ("valid", "baseline", "duplicate"):
            return
        count = self.sample_errors.get(reason, 0) + 1
        self.sample_errors[reason] = count
        if count == 1 or count % 1000 == 0:
            detail = "; ".join(f"{name}: {error}" for name, error in list(sample.get("errors", {}).items())[:3])[:256]
            action = "跳过异常指标值 / Invalid counter values skipped" if sample["values"] else "跳过异常样本 / Invalid sample skipped"
            print_status(f"[ERROR] {action}: reason={reason}, sample={self.sample_count + 1}, "
                         f"time_s={sample['elapsed_ns'] / 1e9:.6f}, count={count}; {detail}")

    def process(self, payload):
        """处理一条元数据、采样或控制载荷；处理异常会记为会话失败并向调用者传播。
        Process one metadata, sample or control payload; mark processing failure and propagate it to
        the caller.
        """
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
        # 这里只解释库解码后的 GPU 载荷，未知布局不能猜测后继续。
        # Interpret GPU payloads after library decoding; never guess an unknown layout and continue.
        kind, _, _, count, body = payload[:5]
        if kind == 0:
            signature = hashlib.sha256(dumps(json_safe(body)).encode()).hexdigest()
            if self.metadata_hash is not None:
                if signature != self.metadata_hash:
                    raise ValueError("metadata_changed")
                return
            self.decoder.configure_metadata(body)
            counters = [CounterDefinition(c[0], description=c[2] if len(c) > 2 and isinstance(c[2], str) else "")
                        for c in self.decoder.counters]
            self.writer = ResultWriter(self.path, self.format, counters, self.header.get("device"), self.header["session"])
            self.output = CaptureOutput(self.writer, self.console, self.stream)
            self.windows = WindowAggregator(counters, self.output.emit, self.header["session"]["report_interval"])
            self.metadata_hash = signature
        elif kind == 1:
            if self.windows is None:
                raise ValueError("samples_before_metadata")
            for sample in self.decoder.decode_samples(count, body):
                self.report_sample_error(sample)
                self.windows.add(sample)
                self.sample_count += 1
                self.sample_span_s = max(self.sample_span_s, sample["elapsed_ns"] / 1e9)
        else:
            raise ValueError("unsupported_gpu_message_type")

    def finish(self, reason="finished", failed=False, details=None):
        """输出尾窗和异常汇总，将最终统计写入 details，再完成结果文件；必须已收到元数据。
        Emit the tail/error summary, update details with final statistics and finalize the result;
        metadata must already exist.
        """
        if self.writer is None:
            raise ValueError("no_counter_metadata")
        self.windows.finish()
        if self.sample_errors:
            summary = ", ".join(f"{reason}={count}" for reason, count in self.sample_errors.items())
            print_status(f"[ERROR] 异常数据汇总 / Invalid data summary: {summary}；"
                         "可用 --debug 保存原始数据 / Use --debug to retain raw data.")
        if details is not None:
            details.update(self.statistics())
        return self.writer.finish(reason, failed or self.failed, details)

    def statistics(self):
        """返回已处理记录和已写窗口的摘要；sample_count 包含异常/基线，不是有效值数量。
        Return processed-record and written-window statistics; sample_count includes
        invalid/baseline records, not just valid values.
        """
        return {"sample_count": self.sample_count, "sample_span_s": self.sample_span_s,
                "metadata_received": self.metadata_hash is not None,
                "row_count": self.writer.rows if self.writer else 0,
                "has_values": self.writer.has_values if self.writer else False,
                "sample_errors": dict(self.sample_errors),
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


# 7. 停止事件、超时与解析工作进程 / Stop events, timeouts and the parsing worker process
@dataclass
class CommandResult:
    """命令结果：path 可为空，status 决定退出码，details 保存已处理统计与错误。
    Command outcome: path may be absent, status determines the exit code, and details holds statistics/errors.
    """
    path: Path | None
    status: str
    details: dict = field(default_factory=dict)


def build_session_metadata(numer=1, denom=1, report_interval=1.0, device=None, source="device"):
    """创建内部会话配置；ResultWriter 仅导出公开格式允许的字段。
    Build internal session configuration; ResultWriter exports only public-format fields.
    """
    return {"schema_version": 1, "timebase": {"numer": numer, "denom": denom},
            "device": dict(device or {}, scope="device"),
            "session": {"started_at": utc_now(), "report_interval": report_interval,
                        "source": source, "dependencies": {
                            p: importlib.metadata.version(p) for p in ("pymobiledevice3", "quickjs-ng")}}}


@contextlib.contextmanager
def capture_stop_signal():
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


async def wait_for_stage(awaitable, timeout, stage, stop=None):
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


def _sample_worker_entry(requests, replies, output, header, format, console):
    """解析子进程单线程持有 QuickJS 与结果文件；原始记录由独立 Debug 进程负责。
    One parsing-child thread owns QuickJS/results; a separate debug process owns raw recording.
    """
    # Windows Ctrl+C 会广播到同控制台子进程，必须由父进程独占处理并安排保存。
    # Windows broadcasts Ctrl+C to console children; only the parent may handle it and coordinate saving.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    pipeline = None
    try:
        pipeline = CaptureDataPipeline(output, header, format, console)
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


class SampleProcessingWorker:
    """单个在途请求；超时后可终止工作进程，避免文件 I/O 挂住退出。
    One request in flight; terminate the worker on timeout rather than hang on file I/O.
    """
    def __init__(self, output, header, format, console):
        """用 Windows spawn 启动解析进程；子进程内部创建 QuickJS/文件，父进程只持有队列与统计。
        Spawn the parser process; the child creates QuickJS/files while the parent holds only queues
        and statistics.
        """
        ctx = multiprocessing.get_context("spawn")
        self.requests, self.replies = ctx.Queue(maxsize=1), ctx.Queue(maxsize=1)
        self.requests.cancel_join_thread()
        self.process = ctx.Process(target=_sample_worker_entry,
                                   args=(self.requests, self.replies, output, header, format, console), daemon=True)
        self.statistics = {"sample_count": 0, "sample_span_s": 0, "row_count": 0,
                           "has_values": False, "metadata_received": False, "console_error": None}
        self.broken = False
        self.closed = False
        self.process.start()

    async def receive(self, timeout):
        """在秒级预算内等待就绪或请求回复并更新统计；退出/超时将通道标为不可继续使用。
        Await readiness or a reply within a seconds-based budget and update statistics; process
        exit/timeout marks the channel unusable.
        """
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
        """发送一个命令并等其回复，同一实例只允许一个在途请求；取消后不再复用该通道。
        Send one command and await its reply, allowing only one in-flight request per instance; do
        not reuse the channel after cancellation.
        """
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
        """回收进程和队列，不代替 finish；调用方应先请求完成结果，失败时可强制终止进程。
        Reclaim the process/queues without replacing finish; callers first request result
        finalization and may terminate on failure.
        """
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


def split_sample_batch(payload, limit=512):
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


def check_runtime_requirements():
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


def read_counter_definitions(body):
    """只读取名称与说明，不执行或输出设备公式。 / Read names/descriptions without running or printing formulas."""
    definitions = body.get("0", body.get(0)) if isinstance(body, dict) else body
    if not isinstance(definitions, list) or any(not isinstance(c, list) or not c for c in definitions):
        raise ValueError("invalid_counter_metadata")
    counters = [CounterDefinition(c[0], description=c[2] if len(c) > 2 and isinstance(c[2], str) else "") for c in definitions]
    validate_counter_definitions(counters)
    return counters


# 8. 设备会话：连接、初始化与资源释放 / Device session: connection, initialization and resource cleanup
class DeviceSession:
    """拥有设备连接和 GPU 通道；open 初始化，close 释放所有已注册资源。
    Own the device connection and GPU channel; open initializes and close releases registered resources.
    """
    def __init__(self, options):
        """只创建本会话的资源容器，不连接设备；open 获取的资源都由 close 统一释放。
        Create resource ownership without connecting; close releases all resources acquired by open.
        """
        self.options = options
        self.resources = contextlib.AsyncExitStack()
        self.service_provider = None
        self.dvt_provider = None
        self.gpu_service = None
        self.timebase_info = None
        self.device_summary = None
        self.report_callback = None

    async def open(self, stop_event, configure=True, report=None, debug=None):
        """初始化 GPU 通道及时间基准并返回 self；成功后可读 gpu_service/timebase_info/device_summary。configure=False 只探测；report 接收检查结果，debug 记录原始回复。失败也需调用 close。
        Initialize GPU/timebase and return self, exposing gpu_service/timebase_info/device_summary.
        configure=False probes only; report receives checks and debug records replies. Call close
        even after failure.
        """
        self.report_callback = report
        require_transport_dependency()
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService

        provider = await wait_for_stage(self.connect_provider(), self.options.connect_timeout, "connect", stop_event)
        model = getattr(provider, "product_type", None)
        self.report_check("设备连接与信任 / connection", f"{model or 'unknown'} / iOS {provider.product_version}")
        self.dvt_provider = dvt = DvtProvider(provider)
        self.resources.push_async_callback(self.close_gpu_channel)
        await wait_for_stage(dvt.connect(), self.options.timeout, "dvt_connect", stop_event)
        self.report_check("开发者服务 / developer service", "DVT connected")
        service = await wait_for_stage(dvt.dtx.open_channel(GpuCounterService), self.options.timeout, "gpu_channel", stop_event)
        service.debug = debug
        info = await wait_for_stage(service.device_info(), self.options.timeout, "gpu_info", stop_event)
        if debug is not None:
            debug.record("rpc.device_info", info)
        candidates = info if isinstance(info, list) else [info]
        if not candidates or any(not isinstance(c, dict) for c in candidates):
            raise ValueError("invalid_gpu_device_info")
        if len(candidates) != 1:
            raise ValueError("multiple_gpu_candidates: " + ", ".join(str(c.get("accelerator-id")) for c in candidates))
        gpu = candidates[0]
        profiles = gpu.get("supported-counter-profiles")
        if profiles is not None and self.options.counter_profile not in profiles:
            raise ValueError(f"unsupported_counter_profile: requested={self.options.counter_profile}, supported={profiles}")
        self.report_check("GPU 能力 / GPU capability", f"{gpu.get('device-name', 'unknown')}, profiles={profiles or 'unknown'}")
        device_info = await wait_for_stage(self.resources.enter_async_context(DeviceInfo(dvt)), self.options.timeout, "timebase_channel", stop_event)
        mach = await wait_for_stage(device_info.mach_time_info(), self.options.timeout, "mach_timebase", stop_event)
        if debug is not None:
            debug.record("rpc.mach_time_info", mach)
        if len(mach) < 3 or int(mach[1]) <= 0 or int(mach[2]) <= 0:
            raise ValueError("invalid_mach_timebase")
        self.report_check("设备时间基准 / timebase", f"{mach[1]}/{mach[2]}")
        if configure:
            reply = await wait_for_stage(service.configure(PInt64(gpu["accelerator-id"]), PInt64(self.options.counter_profile),
                                                       PInt64(self.options.sampling_interval), PInt64(self.options.window_limit), PInt32(-1)),
                                     self.options.timeout, "configure", stop_event)
            if debug is not None:
                debug.record("rpc.configure", reply)
            if isinstance(reply, NSError) or reply is False:
                raise ValueError("gpu_configuration_rejected")
        # 仅标注既有验收范围，不选择公式、不修正数值，也不拒绝其他设备。
        # Label prior acceptance scope only; never select formulas, correct values or reject other devices here.
        verified = (model == "iPhone14,2" and provider.product_version == "18.5" and gpu.get("device-name") == "A15"
                    and self.options.counter_profile == 3 and self.options.sampling_interval == 0 and self.options.window_limit == -1)
        device = {"model": model, "ios_version": provider.product_version, "gpu_name": gpu.get("device-name"),
                  "compatibility": "validated_baseline" if verified else "unverified"}
        self.report_check("兼容范围 / compatibility", "已验证基线 / validated baseline" if verified else "未验证 / unverified")
        if isinstance(provider, RemoteServiceDiscoveryService) and provider.lockdown is not None:
            # GPU 已有独立 DTX 通道，后续服务端口来自缓存的 RSD 信息，不再使用远程 lockdown 查询。
            # GPU owns a separate DTX channel; cached RSD discovery suffices without further remote lockdown queries.
            # 在初始化结束时调用库的幂等 close，避免把闲置连接保留至长采集结束后才关闭。
            # Use the library's idempotent close at initialization instead of retaining an idle connection until teardown.
            await wait_for_stage(provider.lockdown.close(), self.options.timeout, "initial_remote_lockdown_close", stop_event)
        self.gpu_service = service
        self.timebase_info = mach
        self.device_summary = device
        return self

    async def connect_provider(self):
        """建立库的服务入口，并把连接资源登记到本会话。
        Connect the library service provider and register its resources with this session.
        """
        require_transport_dependency()
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
        from pymobiledevice3 import usbmux

        # 显式 UDID 优先；没有选择时只接受唯一 USB 设备。 / Prefer an explicit UDID; otherwise require exactly one USB device.
        serial = self.options.udid
        if not serial:
            devices = await usbmux.select_devices_by_connection_type(connection_type="USB")
            if len(devices) != 1:
                raise ValueError("no_device_connected" if not devices else
                                 "multiple_devices: " + ", ".join(d.serial for d in devices))
            serial = devices[0].serial
        kwargs = {"serial": serial}
        if getattr(self.options, "command", None) == "doctor":
            kwargs["autopair"] = False
        lockdown = await self.resources.enter_async_context(await create_using_usbmux(**kwargs))
        if getattr(lockdown, "paired", True) is False:
            raise ValueError("trust_required")

        # 版本决定传输入口，协议细节交给库；这不是指标数值特判。
        # OS version selects the transport; the library owns protocol details, not metric corrections.
        if int(lockdown.product_version.split(".")[0]) >= 17:
            provider = await self.resources.enter_async_context(PreferredRsdTunnel(serial=serial))
        else:
            provider = lockdown
        if self.options.udid and provider.udid != self.options.udid:
            raise ValueError("selected_device_mismatch")
        self.service_provider = provider
        return provider

    async def close(self):
        """按资源登记的逆序关闭，支持重复调用；不在此发送 GPU stop 或取尾，这由采集会话协调。
        Close registered resources in reverse order, allowing repeated calls; the capture session
        coordinates GPU stop and tail retrieval separately.
        """
        await self.resources.aclose()

    async def close_gpu_channel(self):
        """先暂停读取，再关闭 DTX，避免迟到数据进入已结束的流。
        Pause reads before closing DTX so late data cannot enter an ended stream.
        """
        try:
            connection = getattr(self.dvt_provider, "_dtx", None)
            writer = getattr(connection, "_writer", None)
            if writer is not None:
                # _writer 来自锁定的 pymobiledevice3 版本；实际停止读取使用 asyncio 公共接口。
                # _writer belongs to the pinned pymobiledevice3 version; pausing uses asyncio's public API.
                writer.transport.pause_reading()
        finally:
            await self.dvt_provider.close()

    def report_check(self, stage, detail):
        if self.report_callback is not None:
            self.report_callback(stage, "ok", detail)


# 9. 采集会话：显式拥有状态、工作进程和完整收尾 / Capture session: explicit state, worker ownership and complete shutdown
class CaptureSession:
    """拥有一次录制的设备、工作进程与状态；run 是唯一正式采集入口。
    Own one recording's device, workers and state; run is the production capture entrypoint.
    """
    def __init__(self, options, drain_grace_seconds=2):
        """保存单次录制配置与状态，不访问设备；drain_grace_seconds 是取尾阶段等待迟到通知的宽限秒数。
        Store one recording's options/state without device access; drain_grace_seconds bounds
        waiting for late notifications while draining.
        """
        self.options = options
        self.device_session = DeviceSession(options)
        self.gpu_service = None
        self.processing_worker = None
        self.debug_recorder = None
        self.stop_event = None
        self.on_started = getattr(options, "on_started", None)
        self.drain_grace_seconds = drain_grace_seconds
        self.status_reporter = CaptureStatusReporter()
        # 以下为单次录制状态，不跨 run 复用；连接和子进程在 initialize 时才创建。
        # The following state belongs to one recording, not repeated runs; initialize creates connections/workers.
        self.stop_reason = "user_stop"
        self.errors = []
        self.lifecycle = []
        self.can_process_samples = True
        self.remaining_batch_chunks = None
        self.shutdown_deadline = None

    async def run(self):
        """初始化成功后进入采集；任何初始化失败仍释放本会话已获得的资源。
        Enter collection after initialization; release acquired resources on any initialization failure.
        """
        check_output_path(self.options.output, self.options.format)
        with capture_stop_signal() as self.stop_event:
            try:
                await self.initialize()
            except BaseException:
                await self.close_initialization_resources()
                raise
            return await self.collect_until_stopped()

    async def initialize(self):
        """先启用可选原始记录，再连接设备并启动解析进程。
        Start optional raw recording, then connect the device and launch the parsing worker.
        """
        options = self.options
        if getattr(options, "debug", False):
            directory = getattr(options, "debug_directory", None) or (
                Path("captures") / datetime.now().strftime("%Y%m%d_%H%M%S_%f") / "debug")
            self.debug_recorder = DebugRecorder(directory, options.output)
            await wait_for_stage(self.debug_recorder.start(options.timeout), options.timeout, "debug_start", self.stop_event)
            print_status(f"Debug 原始数据 / Raw debug data: {self.debug_recorder.root.resolve()}")
        print_status("正在连接设备 / Connecting to device")
        await self.device_session.open(self.stop_event, debug=self.debug_recorder)
        self.gpu_service = self.device_session.gpu_service
        device = self.device_session.device_summary
        print_status("兼容基线 / Compatibility: " + ("已验证设备 / verified device" if device["compatibility"] == "validated_baseline"
                                                   else "未验证设备 / unverified device"))
        mach = self.device_session.timebase_info
        session_metadata = build_session_metadata(int(mach[1]), int(mach[2]), options.report_interval, device)
        if self.debug_recorder is not None:
            self.debug_recorder.record("session", session_metadata)
        self.processing_worker = SampleProcessingWorker(options.output, session_metadata, options.format, options.console)
        await wait_for_stage(self.processing_worker.receive(options.timeout), options.timeout, "worker_start", self.stop_event)

    async def collect_until_stopped(self):
        """仅用于已初始化的本次会话：启动采样、接收消息，用户停止或异常都进入 shutdown 并返回 CommandResult。run 负责正式初始化。
        For an initialized session only: start and receive samples, then route user stop/errors
        through shutdown to CommandResult. run owns production initialization.
        """
        self.loop = asyncio.get_running_loop()
        self.capture_started_at = self.last_message_at = self.loop.time()
        if self.stop_event is None:
            self.stop_event = asyncio.Event()
        try:
            await self.start_sampling()
            await self.receive_samples()
        except CaptureStopped:
            self.stop_reason = "user_stop"
        except asyncio.CancelledError as exc:
            self.stop_reason = "cancelled"
            self.record_error("capture", exc)
            self.can_process_samples = not self.processing_worker.broken
        except Exception as exc:
            self.stop_reason = "capture_error"
            self.record_error("capture", exc)
        finally:
            return await self.shutdown()

    async def start_sampling(self):
        """启动回复确认后设置可选 on_started 事件；通知可能已经入队，回复不代表已收到有效样本。
        Set optional on_started after the start reply; notifications may already be queued and the
        reply does not prove valid samples arrived.
        """
        await self.call_gpu("start", self.gpu_service.start, can_stop=True)
        if self.on_started:
            self.on_started.set()
        print_status("GPU Counter 已启动 / GPU started")

    async def receive_samples(self):
        """等待设备推送，不主动周期 flush；停止与连接关闭都在子批之间响应。
        Wait for device pushes without periodic flush; respond to stop/disconnect between chunks.
        """
        while True:
            now = self.loop.time()
            if self.debug_recorder is not None:
                self.debug_recorder.check()
            if self.stop_event.is_set():
                self.stop_reason = "user_stop"
                break
            if self.gpu_service.closed.is_set():
                raise ConnectionError(self.gpu_service.failure or "connection_closed")
            if not self.processing_worker.statistics["metadata_received"] and now - self.capture_started_at > self.options.timeout:
                raise TimeoutError("metadata_timeout")
            if not self.processing_worker.statistics["sample_count"] and now - self.capture_started_at > max(120, self.options.timeout):
                raise TimeoutError("first_sample_timeout")
            if self.gpu_service.events.empty() and now - self.last_message_at >= self.options.idle_timeout:
                raise TimeoutError("notification_idle_timeout")
            if self.gpu_service.events.empty():
                self.status_reporter.wait()
            try:
                payload = await asyncio.wait_for(self.gpu_service.events.get(), .1)
            except asyncio.TimeoutError:
                continue
            self.last_message_at = self.loop.time()
            if not await self.process_message(payload, interruptible=True):
                if self.gpu_service.closed.is_set() and not self.stop_event.is_set():
                    raise ConnectionError(self.gpu_service.failure or "connection_closed")
                break

    async def process_message(self, payload, interruptible=False, announce=True):
        """消息只提示一次；子批完成后才能中断，并保留余下子批。
        Announce a message once; interrupt only after a chunk finishes and retain remaining chunks.
        """
        if not self.can_process_samples:
            return
        if (announce and isinstance(payload, list) and len(payload) >= 5 and payload[0] == 1
                and type(payload[3]) is int and payload[3] > 0):
            self.status_reporter.received(payload[3])
        try:
            chunks = iter(split_sample_batch(payload))
            for chunk in chunks:
                await self.processing_worker.call("process", chunk, timeout=self.remaining_timeout())
                if interruptible and (self.stop_event.is_set() or self.gpu_service.closed.is_set()):
                    # 当前子批确认后立即允许 stop；剩余早期记录必须先于后续通知和 flush 解码。
                    # Allow stop after the current chunk; decode its remainder before later notifications and flush.
                    self.remaining_batch_chunks = chunks
                    return False
            return True
        except Exception:
            self.can_process_samples = False
            raise

    async def shutdown(self):
        """停止→早期数据排空→取尾→关闭连接→完成文件，失败阶段分别记录。
        Stop, drain early data, fetch the tail, close connections and finalize, recording each failed stage.
        """
        self.collection_elapsed_seconds = self.loop.time() - self.capture_started_at
        self.shutdown_started_at = self.loop.time()
        self.shutdown_deadline = self.shutdown_started_at + self.options.shutdown_timeout
        print_status("正在停止并保存尾部数据 / Stopping and draining GPU data")
        try:
            await self.call_gpu("stop", self.gpu_service.stop)
        except Exception as exc:
            self.record_error("stop", exc)
        try:
            if self.remaining_batch_chunks is not None and self.can_process_samples:
                for chunk in self.remaining_batch_chunks:
                    await self.process_message(chunk, announce=False)
            await self.drain_received_messages(self.drain_grace_seconds)
            # 只有早期消息全部解码后才能取尾；解析失效时保留已有结果并停止消费。
            # Fetch the tail only after earlier messages; stop consuming after parsing becomes invalid.
            if self.can_process_samples:
                remaining = await self.call_gpu("flush", self.gpu_service.flush)
                if isinstance(remaining, list) and (not remaining or isinstance(remaining[0], list)):
                    for message in remaining:
                        await self.process_message(message)
                elif remaining is not None:
                    await self.process_message(remaining)
                await self.drain_received_messages(self.drain_grace_seconds)
        except Exception as exc:
            self.record_error("drain_or_flush", exc)
        if self.gpu_service.failure:
            self.record_error("service", ConnectionError(self.gpu_service.failure))
        try:
            await wait_for_stage(self.close_device_resources(), self.remaining_timeout(), "connection_close")
        except Exception as exc:
            self.record_error("connection_close", exc)
        return await self.finalize_output()

    async def drain_received_messages(self, grace):
        """消费已接收通知，并在截止时间内短暂等待迟到消息。
        Consume received notifications and briefly wait for late messages within the deadline.
        """
        until = min(self.shutdown_deadline, self.loop.time() + grace)
        while self.can_process_samples and (not self.gpu_service.events.empty() or (self.loop.time() < until and not self.gpu_service.closed.is_set())):
            if self.loop.time() >= self.shutdown_deadline:
                raise TimeoutError("drain_budget_exhausted")
            try:
                payload = await asyncio.wait_for(self.gpu_service.events.get(), min(.1, max(.001, until - self.loop.time())))
            except asyncio.TimeoutError:
                continue
            await self.process_message(payload)

    async def finalize_output(self):
        """完成解析文件；失败时保留已存在结果并保证解析进程退出。
        Finalize parsed output; preserve existing results on failure and always close the parsing worker.
        """
        stats = dict(self.processing_worker.statistics)
        if self.collection_elapsed_seconds >= 5 and stats["sample_span_s"] < self.collection_elapsed_seconds - 2:
            self.record_error("coverage", ValueError(f"samples={stats['sample_span_s']:.3f}s, collection={self.collection_elapsed_seconds:.3f}s"))
        details = {"stop_reason": self.stop_reason, "collection_elapsed_s": self.collection_elapsed_seconds, **stats,
                   "errors": self.errors, "lifecycle": self.lifecycle}
        if isinstance(self.gpu_service.events, GpuMessageQueue):
            details["receive_queue"] = {"peak_messages": self.gpu_service.events.peak_messages,
                                        "peak_payload_bytes": self.gpu_service.events.peak_bytes,
                                        "max_messages": self.gpu_service.events.maxsize,
                                        "max_payload_bytes": self.gpu_service.events.max_bytes}
        result = None
        try:
            if self.processing_worker.broken or self.loop.time() >= self.shutdown_deadline:
                raise TimeoutError("finalization_budget_exhausted_or_worker_unavailable")
            saved = await self.processing_worker.call("finish", self.stop_reason, bool(self.errors), details, timeout=self.remaining_timeout())
            details.update(self.processing_worker.statistics)
            result = CommandResult(Path(saved["path"]), saved["status"], details)
        except Exception as exc:
            self.record_error("finalize", exc)
        finally:
            try:
                await self.processing_worker.close()
            except Exception as exc:
                self.record_error("worker_close", exc)
        if result is None:
            path = Path(self.options.output) if Path(self.options.output).exists() else Path(str(self.options.output) + ".part")
            result = CommandResult(path if path.exists() else None, "partial" if stats["has_values"] else "failed", details)
        details["shutdown_elapsed_s"] = self.loop.time() - self.shutdown_started_at
        if self.errors and result.status == "completed":
            result.status = "partial"
        if details.get("console_error"):
            print_status("终端输出已关闭 / Console disabled: " + details["console_error"])
        for error in self.errors:
            print_status("采集异常 / Capture error: " + error)
        if self.errors:
            print_status("下一步 / Next: " + error_guidance(RuntimeError(self.errors[0])))
        return result

    async def close_device_resources(self):
        """先关闭设备通道，再关闭原始记录；前者失败也必须尝试后者。
        Close device channels before raw recording, attempting both even if the first fails.
        """
        try:
            await self.device_session.close()
        finally:
            if self.debug_recorder is not None:
                await self.debug_recorder.close()

    async def close_initialization_resources(self):
        """清理未完成初始化的会话，不掩盖原始初始化异常。
        Clean up incomplete initialization without masking its original error.
        """
        try:
            if self.processing_worker is not None:
                await self.processing_worker.close()
        except Exception as error:
            print_status(f"工作进程清理失败 / Worker cleanup failed: {error}")
        try:
            await wait_for_stage(self.close_device_resources(), min(self.options.timeout, 5), "initialization_cleanup")
        except Exception as error:
            print_status(f"初始化清理失败 / Initialization cleanup failed: {error}")

    async def call_gpu(self, name, operation, can_stop=False):
        """在当前阶段预算内调用 GPU 方法，记录 RPC 回复和生命周期；can_stop 仅允许用户停止中断等待。
        Call a GPU method within the current budget, recording its reply/lifecycle; can_stop allows
        user stop to interrupt the wait.
        """
        try:
            reply = await wait_for_stage(operation(), self.remaining_timeout(), name, self.stop_event if can_stop else None)
            if self.debug_recorder is not None:
                self.debug_recorder.record("rpc." + name, reply)
            if isinstance(reply, NSError):
                raise ValueError(f"device_error: {reply.domain}/{reply.code}")
            self.lifecycle.append({"stage": name, "status": "ok", "samples_processed": self.processing_worker.statistics["sample_count"]})
            return reply
        except BaseException:
            self.lifecycle.append({"stage": name, "status": "error", "samples_processed": self.processing_worker.statistics["sample_count"]})
            raise

    def remaining_timeout(self):
        """收尾阶段的每个操作共用截止时间，不为每一步重新计时。
        Every shutdown operation shares one deadline instead of resetting its budget.
        """
        return max(0, min(self.options.timeout, self.shutdown_deadline - self.loop.time())) if self.shutdown_deadline else self.options.timeout

    def record_error(self, stage, error):
        """有界保留失败阶段用于最终状态与提示，最多16条、每条512字符，不在此抛出。
        Retain bounded stage errors for final status/messages, at most 16 entries of 512 characters,
        without raising here.
        """
        if len(self.errors) < 16:
            self.errors.append(f"{stage}: {type(error).__name__}: {error}"[:512])


# 10. 应用入口：命令分发、检查和事件循环 / Application entrypoint: command dispatch, inspection and event loop
class GpuCounterApp:
    """用户命令的统一入口，负责调度与退出码，不直接拥有采集连接。
    Unified user-command entrypoint for dispatch and exit codes, without owning capture connections.
    """
    def __init__(self, options=None):
        """保存命令配置，不产生设备或进程副作用；options 可供内部检查调用，CLI run 会解析实际参数。
        Store command options without device/process side effects; options supports internal
        inspection calls while CLI run parses actual arguments.
        """
        self.options = options
        self.inspection_checks = []

    def run(self):
        """解析命令行并返回退出码，业务资源由对应会话对象管理。
        Parse CLI options and return an exit code; session objects own business resources.
        """
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        parser = self.create_argument_parser()
        self.options = args = parser.parse_args()
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
                result = self.run_async_command(self.list_devices())
            elif args.command in ("doctor", "counters"):
                result = self.run_async_command(self.inspect_device())
            else:
                result = self.run_async_command(CaptureSession(args).run())
                print_status(f"已保存 / Saved: {result.path.resolve() if result.path else '无结果文件 / no result file'} (status={result.status})")
                if result.details:
                    print_status(f"已解析样本 / samples={result.details.get('sample_count', 0)}，"
                          f"窗口 / rows={result.details.get('row_count', 0)}，"
                          f"设备跨度 / span={result.details.get('sample_span_s', 0):.3f}s")
                if result.path and result.path.suffix == ".part":
                    print_status("保留了中断结果；使用 tools/recover_results.py 恢复到新路径 / Recover the .part file to a new path.")
            return 0 if result.status == "completed" else 1
        except (KeyboardInterrupt, CaptureStopped):
            print_status("操作已中断 / Operation interrupted")
            return 130
        except Exception as exc:
            print_error(exc)
            return 1

    @staticmethod
    def create_argument_parser():
        """定义公开命令及内部固定策略，只构建解析器；不会连接设备、创建输出或启动采集。
        Define public commands and fixed internal policy by building a parser only; do not connect,
        create output or start sampling.
        """
        parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                         epilog="建议流程 / Workflow: devices → doctor → counters → capture；Ctrl+C 停止录制")
        # 内部固定策略不是命令行选项；不向操作者暴露协议参数或定时录制。
        # Fixed runtime policy is not a CLI option; operators do not configure protocol details or timed capture.
        parser.set_defaults(timeout=10, connect_timeout=30, shutdown_timeout=25, idle_timeout=30,
                            report_interval=1, counter_profile=3, sampling_interval=0, window_limit=-1)
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

    @staticmethod
    def run_async_command(awaitable):
        """运行单个命令并有界取消后台任务，避免事件循环退出无限等待。
        Run one command and bound background cancellation so event-loop shutdown cannot wait forever.
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
                        print_status("后台任务取消超时 / Background cancellation timed out")
                        if result is not None:
                            result.details.setdefault("errors", []).append("background_cancellation_timeout")
                            if result.status == "completed":
                                result.status = "partial"
            finally:
                loop.close()
                asyncio.set_event_loop(None)

    async def list_devices(self):
        """枚举连接；某台信息不可读时仍展示其标识，且不触发新配对。
        Enumerate connections, retaining identifiers on lookup failure without initiating pairing.
        """
        require_transport_dependency()
        from pymobiledevice3 import usbmux
        from pymobiledevice3.lockdown import create_using_usbmux

        failures = False
        rows = []
        with capture_stop_signal() as stop:
            devices = await wait_for_stage(usbmux.list_devices(), self.options.timeout, "usbmux_list", stop)
            for device in devices:
                info = {"udid": device.serial, "connection": device.connection_type,
                        "name": "unknown", "model": "unknown", "ios": "unknown", "state": "unknown"}
                lockdown = None
                try:
                    lockdown = await wait_for_stage(create_using_usbmux(serial=device.serial, autopair=False,
                                                                   connection_type=device.connection_type),
                                                self.options.timeout, "device_information", stop)
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
                            await wait_for_stage(lockdown.close(), min(self.options.timeout, 5), "device_information_close")
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
            print_status(f"发现 / found {len(rows)} 条连接；采集用 --udid 选择，兼容状态以 doctor/counters 为准。")
        return CommandResult(None, "failed" if not rows else "partial" if failures else "completed", {"devices": rows})

    async def inspect_device(self):
        """doctor 检查服务，counters 只读取元数据；两者共享设备会话并可靠关闭。
        Doctor checks services and counters reads metadata only; both share device-session cleanup.
        """
        self.inspection_checks = []
        for check in check_runtime_requirements():
            self.report_inspection(*check)
        if any(c["status"] == "failed" for c in self.inspection_checks):
            if any(c["check"] == "Python" and c["status"] == "failed" for c in self.inspection_checks):
                print_error(ValueError("unsupported_python"))
            else:
                print_error(ImportError("本机 Python/依赖检查失败 / local runtime check failed"))
            return CommandResult(None, "failed", {"checks": self.inspection_checks})
        device_session = DeviceSession(self.options)
        service = None
        attempted_start = False
        counters = None
        device = None
        interrupted = False
        with capture_stop_signal() as stop:
            try:
                await device_session.open(stop, configure=self.options.command == "counters", report=self.report_inspection)
                service = device_session.gpu_service
                device = device_session.device_summary
                if self.options.command == "counters":
                    attempted_start = True
                    reply = await wait_for_stage(service.start(), self.options.timeout, "counter_metadata_start", stop)
                    if isinstance(reply, NSError) or reply is False:
                        raise ValueError("gpu_counter_start_rejected")
                    deadline = asyncio.get_running_loop().time() + self.options.timeout
                    while counters is None:
                        if service.closed.is_set():
                            raise ConnectionError(service.failure or "gpu_connection_closed")
                        payload = await wait_for_stage(service.events.get(), deadline - asyncio.get_running_loop().time(), "counter_metadata", stop)
                        if isinstance(payload, NSError):
                            raise ValueError(f"device_error: {payload.domain}/{payload.code}")
                        if isinstance(payload, list) and len(payload) >= 5 and payload[0] == 0:
                            counters = read_counter_definitions(payload[4])
                        elif not (isinstance(payload, list) and len(payload) == 2 and payload[0] in (2, 3)):
                            raise ValueError("unexpected_notification_before_metadata")
            except CaptureStopped:
                interrupted = True
            except Exception as exc:
                self.report_inspection(getattr(exc, "gpu_stage", "检查 / inspection"), "failed", str(exc))
                print_error(exc)
            finally:
                if attempted_start:
                    try:
                        reply = await wait_for_stage(service.stop(), self.options.timeout, "counter_metadata_stop")
                        if isinstance(reply, NSError) or reply is False:
                            raise ValueError("gpu_counter_stop_rejected")
                        # stop 回复不是异步通知成功的保证；检查短宽限期内的错误，样本仅丢弃。
                        # A stop reply does not confirm asynchronous success; check grace-period errors and discard samples.
                        until = asyncio.get_running_loop().time() + min(.2, self.options.timeout)
                        while asyncio.get_running_loop().time() < until:
                            try:
                                payload = await asyncio.wait_for(service.events.get(), max(.001, until - asyncio.get_running_loop().time()))
                            except asyncio.TimeoutError:
                                break
                            if isinstance(payload, NSError):
                                raise ValueError(f"device_error: {payload.domain}/{payload.code}")
                            if (counters is not None and isinstance(payload, list) and len(payload) >= 5 and payload[0] == 0
                                    and read_counter_definitions(payload[4]) != counters):
                                raise ValueError("metadata_changed")
                        if service.failure:
                            raise ConnectionError(service.failure)
                    except Exception as exc:
                        self.report_inspection("停止 / stop", "failed", str(exc))
                        print_error(exc)
                try:
                    await wait_for_stage(device_session.close(), min(self.options.timeout, 5), "inspection_close")
                except Exception as exc:
                    self.report_inspection("关闭 / close", "failed", str(exc))
                    print_error(exc)
        if interrupted:
            raise CaptureStopped("inspection_interrupted")
        failed = any(c["status"] == "failed" for c in self.inspection_checks)
        if counters is not None:
            output = csv.writer(sys.stdout, lineterminator="\n")
            output.writerow(["Counter", "单位 / Unit", "说明 / Description"])
            for counter in counters:
                output.writerow([counter.name, counter.unit or " / ", counter.description])
            print_status(f"{len(counters)} 项指标；兼容 / compatibility={device['compatibility']}。只读取元数据，未保存样本。")
        if self.options.command == "doctor" and not failed:
            print_status("服务检查通过；未启动采样，不代表数据完整性或跨设备验收。下一步运行 counters 或 capture。")
        return CommandResult(None, "failed" if failed else "completed", {"checks": self.inspection_checks})

    def report_inspection(self, name, status, detail):
        self.inspection_checks.append({"check": name, "status": status, "detail": str(detail)})
        if self.options.command == "doctor":
            print(f"[{status.upper()}] {name}: {detail}", flush=True)


def main():
    """保持脚本入口简单：应用对象执行命令并返回进程退出码。
    Keep the script entrypoint small: the application executes the command and returns its exit
    code.
    """
    return GpuCounterApp().run()


if __name__ == "__main__":
    raise SystemExit(main())
