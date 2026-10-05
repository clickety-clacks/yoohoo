"""One-request/one-response JSON CLI."""
from __future__ import annotations

from dataclasses import replace
import json
import os
import selectors
import stat
import sys
import time
from typing import Any, BinaryIO, Mapping, Sequence

from .linux import LinuxCollector
from .model import Limits, Request, RequestError, parse_request
from .resolver import Resolver

VERSION = "0.1.0"
_HARD_REQUEST_BYTES = 262_144
_HARD_STDOUT_BYTES = 1_048_576
_HARD_STDERR_BYTES = 16_384


def _reason(code: str, source: str, message: str,
            retryable: bool = False) -> dict[str, Any]:
    return {
        "code": code,
        "source": source,
        "message": message[:512] or code,
        "retryable": retryable,
    }


def _error_response(
    status: str,
    code: str,
    source: str,
    message: str,
    *,
    request_id: str | None = None,
    operation: str | None = None,
    relation: str | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "agent-window-resolver.response.v1",
        "requestId": request_id,
        "operation": operation,
        "status": status,
        "candidates": [],
        "evidence": [],
        "reasons": [_reason(code, source, message, retryable)],
    }
    if relation in {"visible_exact", "linked_client"}:
        result["requestedRelation"] = relation
    return result


def _encode(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8") + b"\n"


def _safe_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return None
    return value


def _recovered(raw: Any) -> tuple[str | None, str | None, str | None]:
    if not isinstance(raw, Mapping):
        return None, None, None
    request_id = _safe_text(raw.get("requestId"), 128)
    operation_value = raw.get("operation")
    relation_value = raw.get("requestedRelation")
    operation = (
        operation_value if isinstance(operation_value, str)
        and operation_value in {"resolve", "revalidate", "verify-target", "match"}
        else None
    )
    relation = (
        relation_value if isinstance(relation_value, str)
        and relation_value in {"visible_exact", "linked_client"}
        else None
    )
    return request_id, operation, relation


def _read_bounded(stream: BinaryIO, maximum: int, timeout: float) -> bytes:
    try:
        descriptor = stream.fileno()
        mode = os.fstat(descriptor).st_mode
    except (AttributeError, OSError):
        return stream.read(maximum + 1)
    if stat.S_ISREG(mode):
        return stream.read(maximum + 1)
    previous = os.get_blocking(descriptor)
    select = selectors.DefaultSelector()
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        os.set_blocking(descriptor, False)
        select.register(descriptor, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select(remaining):
                raise TimeoutError("stdin framing deadline expired")
            try:
                chunk = os.read(descriptor, min(8192, maximum + 1 - len(output)))
            except BlockingIOError:
                continue
            if not chunk:
                return bytes(output)
            output.extend(chunk)
            if len(output) > maximum:
                return bytes(output)
    finally:
        select.close()
        os.set_blocking(descriptor, previous)


def _exit_code(response: Mapping[str, Any]) -> int:
    status = response.get("status")
    reasons = response.get("reasons")
    first = reasons[0] if isinstance(reasons, list) and reasons else {}
    code = first.get("code") if isinstance(first, Mapping) else None
    source = first.get("source") if isinstance(first, Mapping) else None
    if code == "deadline_exceeded":
        return 5
    if code in {"dependency_missing", "dependency_incompatible"}:
        return 3
    if status == "invalid":
        return 4 if source == "internal" or code == "output_limit_exceeded" else 2
    if status == "unreachable" and source == "internal":
        return 4
    return 0


def run(
    argv: Sequence[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    stderr: BinaryIO | None = None,
    collector: Any = None,
    resolver: Resolver | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    input_stream = sys.stdin.buffer if stdin is None else stdin
    output_stream = sys.stdout.buffer if stdout is None else stdout
    error_stream = sys.stderr.buffer if stderr is None else stderr
    if args == ["--version"]:
        output_stream.write(("agent-window-resolver " + VERSION + "\n").encode())
        return 0
    if args:
        response = _error_response(
            "invalid", "invalid_arguments", "request",
            "the JSON CLI accepts no arguments",
        )
        output_stream.write(_encode(response))
        return 2
    started = time.monotonic()
    try:
        raw_bytes = _read_bounded(input_stream, _HARD_REQUEST_BYTES, 20.0)
    except TimeoutError as error:
        response = _error_response(
            "unreachable", "deadline_exceeded", "request", str(error), retryable=True
        )
        output_stream.write(_encode(response))
        return 5
    if len(raw_bytes) > _HARD_REQUEST_BYTES:
        response = _error_response(
            "invalid", "request_too_large", "request",
            "request exceeds the 256 KiB hard ceiling",
        )
        output_stream.write(_encode(response))
        return 2
    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        response = _error_response(
            "invalid", "invalid_json", "request", str(error)
        )
        output_stream.write(_encode(response))
        return 2
    request_id, operation, relation = _recovered(raw)
    try:
        request = parse_request(raw)
    except (RequestError, TypeError, ValueError) as error:
        code = error.code if isinstance(error, RequestError) else "invalid_request"
        response = _error_response(
            "invalid", code, "request", str(error),
            request_id=request_id, operation=operation, relation=relation,
        )
        output_stream.write(_encode(response))
        return 2
    if len(raw_bytes) > request.limits.max_request_bytes:
        response = _error_response(
            "invalid", "request_too_large", "request",
            "request exceeds caller maxRequestBytes",
            request_id=request.request_id, operation=request.operation,
            relation=request.requested_relation,
        )
        output_stream.write(_encode(response))
        return 2
    total_deadline_ms = min(20_000, request.limits.deadline_ms)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    remaining_ms = total_deadline_ms - elapsed_ms
    if remaining_ms <= 0:
        response = _error_response(
            "unreachable", "deadline_exceeded", "internal",
            "overall request deadline expired", request_id=request.request_id,
            operation=request.operation, relation=request.requested_relation,
            retryable=True,
        )
        output_stream.write(_encode(response))
        return 5
    request = replace(
        request,
        limits=replace(request.limits, deadline_ms=max(1, remaining_ms)),
    )
    active_resolver = resolver if resolver is not None else Resolver()
    active_collector = collector if collector is not None else LinuxCollector()
    response = active_resolver.resolve(request, active_collector
    )
    if (time.monotonic() - started) * 1000 >= total_deadline_ms:
        response = _error_response(
            "unreachable", "deadline_exceeded", "internal",
            "overall request deadline expired", request_id=request.request_id,
            operation=request.operation, relation=request.requested_relation,
            retryable=True,
        )
    try:
        encoded = _encode(response)
    except (TypeError, ValueError, UnicodeEncodeError):
        response = _error_response(
            "invalid", "internal_error", "internal",
            "response could not be encoded", request_id=request.request_id,
            operation=request.operation, relation=request.requested_relation,
        )
        encoded = _encode(response)
    if len(encoded) > request.limits.max_stdout_bytes:
        response = _error_response(
            "invalid", "output_limit_exceeded", "internal",
            "complete response exceeds maxStdoutBytes",
            request_id=request.request_id, operation=request.operation,
            relation=request.requested_relation,
        )
        encoded = _encode(response)
        if len(encoded) > request.limits.max_stdout_bytes:
            message = b"output limit prevented a protocol response\n"
            error_stream.write(message[:request.limits.max_stderr_bytes])
            return 4
    output_stream.write(encoded)
    return _exit_code(response)


def main() -> None:
    raise SystemExit(run())


__all__ = ["VERSION", "main", "run"]
