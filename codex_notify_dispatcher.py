#!/usr/bin/env python3
"""Fan out Codex turn notifications to Computer Use and Bark."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


KEYCHAIN_SERVICE = "codex-bark-push-url"
DEDUPE_WINDOW_SECONDS = 600
KIND_PRIORITY = {"complete": 0, "confirmation": 1, "authorization": 2}
FINAL_WATCH_TIMEOUT_SECONDS = 7200
FINAL_WATCH_POLL_SECONDS = 0.5
ROLLOUT_TAIL_BYTES = 8 * 1024 * 1024


def classify(message: str) -> tuple[str, str, str]:
    # Action requests normally appear near the end. Restricting the scan and
    # requiring an explicit call to action avoids matching explanatory prose.
    text = message.lower()[-1200:]
    authorization = (
        r"(?:是否|请问)(?:可以|能否)?.{0,8}(?:允许|批准|授权)|"
        r"(?:请|需要|需|麻烦)(?:你|您)?.{0,20}(?:批准|允许|授权)"
        r"(?:本次|该|这个|后|才能|请求|操作)|"
        r"(?:批准|允许|授权)(?:后|才能|方可).{0,12}(?:继续|执行)|"
        r"approval required|requires approval|please approve|allow this action"
    )
    confirmation = (
        r"(?:请|需要|麻烦)(?:你|您)?.{0,20}(?:确认|选择|提供|决定)|"
        r"(?:确认|选择|提供|决定)(?:后|完成后).{0,12}(?:回复|继续)|"
        r"回复.{0,12}(?:确认|选择|已完成|已保存)|"
        r"confirmation required|please confirm"
    )
    if re.search(authorization, text):
        return "authorization", "Codex：等待授权", "需要你在 Codex 中批准后继续"
    if re.search(confirmation, text):
        return "confirmation", "Codex：等待确认", "需要你返回 Codex 确认后继续"
    return "complete", "Codex：任务已完成", "任务已结束，请返回 Codex 查看结果"


def read_bark_url() -> str:
    account = os.environ.get("USER", "").strip()
    if not account:
        return ""
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            check=False,
            timeout=5,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    value = result.stdout.strip()
    if len(value) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", value):
        try:
            value = bytes.fromhex(value).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return value


def build_bark_url(base_url: str, title: str, body: str, kind: str) -> str:
    parsed = urllib.parse.urlsplit(base_url.strip())
    path_parts = [part for part in parsed.path.split("/") if part]
    if not parsed.scheme or not parsed.netloc or not path_parts:
        raise ValueError("Invalid Bark push URL")
    key = path_parts[0]
    path = "/".join(
        urllib.parse.quote(part, safe="") for part in (key, title, body)
    )
    level = "timeSensitive" if kind in {"authorization", "confirmation"} else "active"
    query = urllib.parse.urlencode({"group": "Codex", "level": level, "isArchive": "1"})
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, f"/{path}", query, ""))


def send_bark(kind: str, title: str, body: str) -> bool:
    base_url = read_bark_url()
    if not base_url:
        return False
    try:
        request = urllib.request.Request(
            build_bark_url(base_url, title, body, kind),
            headers={"User-Agent": "Codex-Bark-Notifier/1.0"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


def parse_payload(raw: str) -> dict:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def codex_home_path() -> Path:
    override = os.environ.get("CODEX_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".codex"


def thread_id_from_payload(payload: dict) -> str:
    return str(
        payload.get("thread-id")
        or payload.get("thread_id")
        or payload.get("threadId")
        or payload.get("conversation-id")
        or payload.get("conversation_id")
        or payload.get("conversationId")
        or payload.get("codex_thread_id")
        or ""
    ).strip()


def turn_id_from_payload(payload: dict) -> str:
    return str(
        payload.get("turn-id")
        or payload.get("turn_id")
        or payload.get("turnId")
        or payload.get("codex_turn_id")
        or ""
    ).strip()


def audit_notification(payload: dict) -> None:
    audit_path = codex_home_path() / "notifications" / "notification_audit.jsonl"
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if audit_path.exists() and audit_path.stat().st_size > 1024 * 1024:
            audit_path.replace(audit_path.with_suffix(".jsonl.old"))
        message = str(payload.get("last-assistant-message", ""))
        record = {
            "timestamp": time.time(),
            "keys": sorted(str(key) for key in payload),
            "type": str(payload.get("type", "")),
            "thread_id": thread_id_from_payload(payload),
            "turn_id": turn_id_from_payload(payload),
            "cwd_name": Path(str(payload.get("cwd", ""))).name,
            "message_length": len(message),
            "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        }
        with audit_path.open("a", encoding="utf-8") as handle:
            os.chmod(audit_path, 0o600)
            handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    except OSError:
        return


def rollout_path_for_thread(thread_id: str) -> Path | None:
    if not thread_id:
        return None
    codex_home = codex_home_path()
    databases = sorted(
        codex_home.glob("state_*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for database in databases:
        uri_path = urllib.parse.quote(str(database), safe="/")
        try:
            connection = sqlite3.connect(
                f"file:{uri_path}?mode=ro", uri=True, timeout=1
            )
            try:
                row = connection.execute(
                    "SELECT rollout_path FROM threads WHERE id = ?", (thread_id,)
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            continue
        if row and row[0]:
            path = Path(str(row[0])).expanduser()
            if path.is_file():
                return path

    sessions = codex_home / "sessions"
    try:
        return next(sessions.rglob(f"*{thread_id}.jsonl"))
    except (StopIteration, OSError):
        return None


def recent_thread_candidates(cwd: str, limit: int = 12) -> list[tuple[str, Path]]:
    codex_home = codex_home_path()
    databases = sorted(
        codex_home.glob("state_*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for database in databases:
        uri_path = urllib.parse.quote(str(database), safe="/")
        try:
            connection = sqlite3.connect(
                f"file:{uri_path}?mode=ro", uri=True, timeout=1
            )
            try:
                if cwd:
                    rows = connection.execute(
                        "SELECT id, rollout_path FROM threads WHERE cwd = ? "
                        "ORDER BY recency_at DESC LIMIT ?",
                        (cwd, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT id, rollout_path FROM threads "
                        "ORDER BY recency_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            continue
        candidates = []
        for thread_id, rollout_path in rows:
            path = Path(str(rollout_path)).expanduser()
            if path.is_file():
                candidates.append((str(thread_id), path))
        if candidates:
            return candidates
    return []


def recent_rollout_records(path: Path) -> list[dict]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - ROLLOUT_TAIL_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []

    if start:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""

    records: list[dict] = []
    for raw_line in data.splitlines():
        try:
            record = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)

    if start and not any(record.get("type") == "turn_context" for record in records):
        try:
            with path.open("r", encoding="utf-8") as handle:
                records = []
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") == "turn_context":
                        records = [record]
                    elif records:
                        records.append(record)
        except OSError:
            return []
    return records


def assistant_text(payload: dict) -> str:
    parts = []
    for item in payload.get("content", []):
        if isinstance(item, dict) and item.get("type") == "output_text":
            parts.append(str(item.get("text", "")))
    return "\n".join(part for part in parts if part).strip()


def rollout_contains_assistant_message(path: Path, message: str) -> bool:
    if not message:
        return False
    for record in reversed(recent_rollout_records(path)):
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if (
            record.get("type") == "event_msg"
            and payload.get("type") == "agent_message"
            and str(payload.get("message", "")) == message
        ):
            return True
        if (
            record.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "assistant"
            and assistant_text(payload) == message
        ):
            return True
    return False


def resolve_thread_id(payload: dict) -> str:
    direct = thread_id_from_payload(payload)
    if direct:
        return direct
    message = str(payload.get("last-assistant-message", ""))
    cwd = str(payload.get("cwd", "")).strip()
    matches = [
        thread_id
        for thread_id, path in recent_thread_candidates(cwd)
        if rollout_contains_assistant_message(path, message)
    ]
    return matches[0] if len(matches) == 1 else ""


def rollout_snapshot(path: Path) -> dict:
    latest_turn_id = ""
    final: dict | None = None
    task_complete = False

    for record in recent_rollout_records(path):
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "turn_context":
            latest_turn_id = str(payload.get("turn_id", ""))
            final = None
            task_complete = False
            continue
        if not latest_turn_id:
            continue

        if (
            record.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "assistant"
            and payload.get("phase") == "final_answer"
        ):
            metadata = payload.get("internal_chat_message_metadata_passthrough", {})
            message_turn_id = str(metadata.get("turn_id", "")) if isinstance(metadata, dict) else ""
            if not message_turn_id or message_turn_id == latest_turn_id:
                final = {
                    "id": str(payload.get("id", "")),
                    "turn_id": latest_turn_id,
                    "text": assistant_text(payload),
                    "timestamp": str(record.get("timestamp", "")),
                }
        elif record.get("type") == "event_msg" and payload.get("type") == "task_complete":
            if str(payload.get("turn_id", "")) == latest_turn_id:
                task_complete = True

    return {
        "rollout_path": str(path),
        "turn_id": latest_turn_id,
        "final": final,
        "task_complete": task_complete,
    }


def latest_thread_snapshot(thread_id: str) -> dict:
    path = rollout_path_for_thread(thread_id)
    if path is None:
        return {"rollout_path": "", "turn_id": "", "final": None, "task_complete": False}
    return rollout_snapshot(path)


def dedupe_state_path() -> Path:
    override = os.environ.get("CODEX_BARK_DEDUPE_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    return codex_home_path() / "notifications" / "recent_notifications.json"


def notification_key(payload: dict) -> str:
    explicit_identity = str(payload.get("_notification_identity", "")).strip()
    if explicit_identity:
        encoded = explicit_identity.encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    thread_id = thread_id_from_payload(payload)
    turn_id = turn_id_from_payload(payload)
    if thread_id or turn_id:
        identity = {"thread_id": thread_id, "turn_id": turn_id}
    else:
        identity = {
            "cwd": str(payload.get("cwd", "")),
            "message": str(payload.get("last-assistant-message", "")),
        }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reserve_notification(payload: dict, kind: str) -> tuple[bool, tuple[str, float] | None]:
    state_path = dedupe_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = notification_key(payload)
    now = time.time()

    with state_path.open("a+", encoding="utf-8") as handle:
        os.chmod(state_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        try:
            state = json.load(handle)
            if not isinstance(state, dict):
                state = {}
        except (json.JSONDecodeError, OSError):
            state = {}

        state = {
            item_key: item
            for item_key, item in state.items()
            if isinstance(item, dict) and now - float(item.get("timestamp", 0)) < 86400
        }
        previous = state.get(key)
        if previous and now - float(previous.get("timestamp", 0)) < DEDUPE_WINDOW_SECONDS:
            previous_kind = str(previous.get("kind", "complete"))
            if KIND_PRIORITY.get(kind, 0) <= KIND_PRIORITY.get(previous_kind, 0):
                return False, None

        state[key] = {"timestamp": now, "kind": kind}
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle, ensure_ascii=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
        return True, (key, now)


def release_notification(reservation: tuple[str, float]) -> None:
    state_path = dedupe_state_path()
    if not state_path.exists():
        return
    key, timestamp = reservation
    with state_path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = json.load(handle)
            current = state.get(key, {}) if isinstance(state, dict) else {}
        except (json.JSONDecodeError, OSError):
            return
        if float(current.get("timestamp", 0)) != timestamp:
            return
        state.pop(key, None)
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle, ensure_ascii=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())


def add_project_to_body(body: str, cwd: str) -> str:
    cwd = cwd.strip()
    return f"{body}\n项目：{Path(cwd).name}" if cwd else body


def dispatch_final(payload: dict, final: dict) -> bool:
    thread_id = thread_id_from_payload(payload)
    turn_id = str(final.get("turn_id", ""))
    message_id = str(final.get("id", ""))
    message = str(final.get("text", ""))
    identity = message_id or hashlib.sha256(message.encode("utf-8")).hexdigest()

    stable_payload = dict(payload)
    stable_payload["thread-id"] = thread_id
    stable_payload["turn-id"] = turn_id
    stable_payload["last-assistant-message"] = message
    stable_payload["_notification_identity"] = f"final:{thread_id}:{turn_id}:{identity}"

    kind, title, body = classify(message)
    should_send, reservation = reserve_notification(stable_payload, kind)
    if not should_send:
        return False
    body = add_project_to_body(body, str(payload.get("cwd", "")))
    if not send_bark(kind, title, body) and reservation is not None:
        release_notification(reservation)
        return False
    return True


def dispatch_missing_reply(payload: dict, turn_id: str) -> bool:
    thread_id = thread_id_from_payload(payload)
    stable_payload = dict(payload)
    stable_payload["_notification_identity"] = f"missing-final:{thread_id}:{turn_id}"
    should_send, reservation = reserve_notification(stable_payload, "complete")
    if not should_send:
        return False
    body = add_project_to_body(
        "任务已结束，但没有生成可见回复，请返回 Codex 检查",
        str(payload.get("cwd", "")),
    )
    if not send_bark("complete", "Codex：任务异常结束", body) and reservation is not None:
        release_notification(reservation)
        return False
    return True


def watch_lock_path(thread_id: str, turn_id: str) -> Path:
    digest = hashlib.sha256(f"{thread_id}:{turn_id}".encode("utf-8")).hexdigest()
    return codex_home_path() / "notifications" / "watch_locks" / f"{digest}.lock"


def run_final_watcher(thread_id: str, turn_id: str, cwd: str) -> int:
    lock_path = watch_lock_path(thread_id, turn_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        path = rollout_path_for_thread(thread_id)
        if path is None:
            return 0
        payload = {"thread-id": thread_id, "turn-id": turn_id, "cwd": cwd}
        deadline = time.monotonic() + FINAL_WATCH_TIMEOUT_SECONDS
        previous_signature: tuple[int, int] | None = None
        while time.monotonic() < deadline:
            try:
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                return 0
            if signature != previous_signature:
                previous_signature = signature
                snapshot = rollout_snapshot(path)
                if snapshot.get("turn_id") and snapshot.get("turn_id") != turn_id:
                    return 0
                final = snapshot.get("final")
                if isinstance(final, dict):
                    dispatch_final(payload, final)
                    return 0
                if snapshot.get("task_complete"):
                    return 0
            time.sleep(FINAL_WATCH_POLL_SECONDS)
    return 0


def spawn_final_watcher(thread_id: str, turn_id: str, cwd: str) -> None:
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--watch-final", thread_id, turn_id, cwd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return


def handle_notification(payload: dict) -> int:
    audit_notification(payload)
    event_type = str(payload.get("type", ""))
    if event_type == "approval-requested":
        immediate = dict(payload)
        immediate["_notification_identity"] = (
            f"approval:{thread_id_from_payload(payload)}:{turn_id_from_payload(payload)}"
        )
        should_send, reservation = reserve_notification(immediate, "authorization")
        if not should_send:
            return 0
        body = add_project_to_body(
            "需要你在 Codex 中批准后继续", str(payload.get("cwd", ""))
        )
        if not send_bark("authorization", "Codex：等待授权", body) and reservation is not None:
            release_notification(reservation)
        return 0

    thread_id = resolve_thread_id(payload)
    if thread_id:
        payload = dict(payload)
        payload["thread-id"] = thread_id
        snapshot = latest_thread_snapshot(thread_id)
        final = snapshot.get("final")
        if isinstance(final, dict):
            dispatch_final(payload, final)
            return 0
        turn_id = str(snapshot.get("turn_id", ""))
        if turn_id:
            if not snapshot.get("task_complete"):
                spawn_final_watcher(thread_id, turn_id, str(payload.get("cwd", "")))
            return 0

    # Unknown callbacks are unsafe to treat as completion events.
    return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--classify":
        print(classify(sys.argv[2])[0])
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "--test-push":
        kind = sys.argv[2]
        labels = {
            "complete": ("Codex：测试完成提醒", "Bark 完成提醒配置成功"),
            "confirmation": ("Codex：测试确认提醒", "Bark 等待确认提醒配置成功"),
            "authorization": ("Codex：测试授权提醒", "Bark 等待授权提醒配置成功"),
        }
        title, body = labels.get(kind, labels["complete"])
        return 0 if send_bark(kind, title, body) else 1
    if len(sys.argv) >= 3 and sys.argv[1] == "--inspect-thread":
        snapshot = latest_thread_snapshot(sys.argv[2])
        final = snapshot.get("final")
        if isinstance(final, dict):
            final = {key: value for key, value in final.items() if key != "text"}
        snapshot["final"] = final
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0
    if len(sys.argv) >= 5 and sys.argv[1] == "--watch-final":
        return run_final_watcher(sys.argv[2], sys.argv[3], sys.argv[4])

    raw_payload = sys.argv[-1] if len(sys.argv) > 1 else "{}"
    payload = parse_payload(raw_payload)
    return handle_notification(payload)


if __name__ == "__main__":
    raise SystemExit(main())
