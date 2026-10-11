"""Pure scheduling and persistence; the website remains the source of truth."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import json
import os
import re
import time


class NeedsAttention(RuntimeError):
    """Stop automatic activity and retain enough evidence for the operator."""


def normalized(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("（", "(").replace("）", ")")


def seconds(value: str) -> int:
    pieces = value.strip().split(":")
    if len(pieces) not in (2, 3) or any(not x.isdigit() for x in pieces):
        raise ValueError(f"无法解析时长: {value!r}")
    numbers = list(map(int, pieces))
    if numbers[-1] >= 60 or (len(numbers) == 3 and numbers[-2] >= 60):
        raise ValueError(f"无效时长: {value!r}")
    return numbers[-1] + numbers[-2] * 60 + (numbers[0] * 3600 if len(numbers) == 3 else 0)


def duration(value: float) -> str:
    value = max(0, int(value))
    return f"{value // 3600:02}:{value // 60 % 60:02}:{value % 60:02}"


def safe_url(url: str) -> str:
    parsed = urlsplit(url)
    allowed = {"id", "courseId", "lessonId", "taskId", "unitId"}
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if k in allowed])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


@dataclass
class Course:
    key: str
    title: str
    module: str
    required: bool
    learned: int
    target: int
    tab: str = "未学"
    page: int = 1
    row: int = 0
    url: str = ""

    @property
    def complete(self) -> bool:
        return self.learned >= self.target

    @property
    def remaining(self) -> int:
        return max(0, self.target - self.learned)

    @property
    def kind_hint(self) -> str:
        if '题库' in self.module:
            return 'questionbank'
        return "video" if any(x in self.module for x in ("微课堂", "视频")) else "document"

    @classmethod
    def from_row(cls, raw: dict, tab: str, page: int) -> Course:
        times = re.findall(r"\d+:\d{2}:\d{2}", raw["progress"])
        if len(times) != 2:
            raise NeedsAttention(f"课程时长格式未识别: {raw['title']}")
        title = re.sub(r"^\s*(必学|选学)\s*", "", raw["title"]).strip()
        if not title or title.endswith(("...", "…")):
            raise NeedsAttention("课程名为空或实际文本被截断，不能可靠去重。请检查页面诊断。")
        target = seconds(times[1])
        if target <= 0:
            raise NeedsAttention(f"课程要求时长无效: {title}")
        url = safe_url(raw.get("url", ""))
        identity = raw.get("id") or url or normalized(title + "|" + raw["module"])
        key = sha256(str(identity).encode("utf-8")).hexdigest()[:24]
        return cls(key, title, raw["module"].strip(), bool(raw["required"]),
                   seconds(times[0]), target, tab, page, raw["row"], url)


@dataclass
class Totals:
    required_learned: int
    required_target: int
    optional_learned: int
    optional_target: int

    @property
    def met(self) -> bool:
        return (self.required_learned >= self.required_target
                and self.optional_learned >= self.optional_target)


def merge_courses(courses: list[Course]) -> list[Course]:
    merged: dict[str, Course] = {}
    for course in courses:
        previous = merged.get(course.key)
        if previous:
            if (previous.title, previous.module, previous.target, previous.required) != (
                    course.title, course.module, course.target, course.required):
                raise NeedsAttention("同一课程身份对应不同信息，已暂停以防错学。")
            if previous.tab == course.tab:
                raise NeedsAttention(f"同一列表出现重复课程身份: {course.title}")
            if course.learned > previous.learned:
                merged[course.key] = course
        else:
            merged[course.key] = course
    return list(merged.values())


class State:
    def __init__(self, path: Path, task: str, account: str):
        self.path = path
        self.data = {"schema": 1, "task": task, "account": account,
                     "courses": {}, "validation": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            if (self.data.get("schema"), self.data.get("task"), self.data.get("account")) != (
                    1, task, account):
                raise NeedsAttention("进度文件与当前任务或账号标签不匹配。")

    def save(self):
        write_json(self.path, self.data)

    def pending(self) -> dict:
        return {k: v for k, v in self.data["courses"].items() if v["status"] == "pending"}

    def mark_pending(self, course: Course, observation: dict, now: float | None = None):
        previous = self.data['courses'].get(course.key)
        history = list(previous.get('history', [])) if previous else []
        if previous:
            history.append({k: v for k, v in previous.items() if k != 'history'})
        self.data["courses"][course.key] = {
            "status": "pending", "course": asdict(course),
            "before_seconds": course.learned, "submitted_at": time.time() if now is None else now,
            "observation": observation,
            "history": history,
        }
        self.data.pop('active', None)
        self.save()

    def begin(self, course: Course, pilot: bool, kind='unknown'):
        self.data['active'] = {'course': asdict(course), 'pilot': pilot, 'kind': kind}
        self.save()

    def recover_interrupted(self):
        active = self.data.get('active')
        if active:
            self.mark_pending(Course(**active['course']), {
                'kind': active['kind'], 'pilot': False,
                'reason': 'previous_process_interrupted_wait_for_server'})

    def reconcile(self, courses: list[Course], now: float, timeout: float, delay: float = 0) -> list[str]:
        current = {c.key: c for c in courses}
        confirmed = []
        errors = []
        for key, record in self.data["courses"].items():
            course = current.get(key)
            if record["status"] == "confirmed" and course and not course.complete:
                record["status"] = "incomplete"
            if record["status"] != "pending":
                continue
            if now - record['submitted_at'] < delay:
                continue
            if course and (course.complete or course.learned > record["before_seconds"]):
                record.update(status="confirmed" if course.complete else "incomplete",
                              after_seconds=course.learned, checked_at=now)
                confirmed.append(key)
                observation = record["observation"]
                if (observation.get("pilot") and course.complete and observation.get("reason") in
                        {"site_timer_reached", "video_ended_wait_for_server",
                         "observed_playing_interval_wait_for_server"}):
                    self.data["validation"][observation["kind"]] = {
                        "status": "passed", "course": course.title, "key": key,
                        "before_seconds": record["before_seconds"], "after_seconds": course.learned,
                        "checked_at": now, "observation": observation,
                    }
            elif now - record["submitted_at"] >= timeout:
                errors.append(record["course"]["title"])
        self.save()
        if errors:
            raise NeedsAttention("超过确认期限仍未看到网站记录增加: " + "、".join(errors))
        return confirmed

    @property
    def validated(self) -> bool:
        return all(self.data["validation"].get(k, {}).get("status") == "passed"
                   for k in ("document", "video"))

    def retry_expired(self, courses: list[Course], now: float, timeout: float) -> list[str]:
        """Only called after an explicit resume; keep evidence of failed confirmation."""
        current = {c.key: c for c in courses}
        retried = []
        for key, record in self.pending().items():
            course = current.get(key)
            if (course and now - record['submitted_at'] >= timeout and
                    course.learned <= record['before_seconds'] and not course.complete):
                record['status'] = 'incomplete'
                record['confirmation_failed_at'] = now
                record['website_seconds_at_retry'] = course.learned
                retried.append(key)
        self.save()
        return retried


def select_course(courses: list[Course], totals: Totals, state: State) -> Course | None:
    pending = state.pending()
    available = [c for c in courses if not c.complete and c.key not in pending]
    required = [c for c in available if c.required]
    if required:
        return required[0]
    # Pending required entries must be confirmed before starting optional material.
    if any(x["course"]["required"] for x in pending.values()):
        return None
    projected = sum(max(0, x["course"]["target"] - x["before_seconds"])
                    for x in pending.values() if not x["course"]["required"])
    if totals.optional_learned + projected >= totals.optional_target:
        return None
    return next((c for c in available if not c.required), None)


class TimerTarget:
    """Interpret the first lesson timer without double-counting prior credit."""
    def __init__(self, baseline: int, target: int, first: int):
        self.mode = "session" if first < baseline else "cumulative_or_zero_baseline"
        self.threshold = target - baseline if self.mode == "session" else target
        self.first = first

    def met(self, observed: int) -> bool:
        return observed >= self.threshold
