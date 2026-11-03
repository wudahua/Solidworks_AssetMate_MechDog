"""Small dependency-free performance recorder for drawing-generation A/B tests."""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class PerfRecorder:
    def __init__(self, variant, metadata=None):
        self.run_id = str(uuid.uuid4())
        self.variant = str(variant)
        self.metadata = dict(metadata or {})
        self.timings_ms = {}
        self.counts = {}
        self.errors = []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._start = time.perf_counter()

    @contextmanager
    def span(self, name):
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - start) * 1000.0
            self.timings_ms[name] = round(self.timings_ms.get(name, 0.0) + elapsed, 3)

    def count(self, name, amount=1):
        self.counts[name] = self.counts.get(name, 0) + int(amount)

    def fail(self, code, detail=None):
        self.errors.append({"code": str(code), "detail": detail})

    def finish(self, success=True, correctness_pass=False):
        result = dict(self.metadata)
        result.update({
            "run_id": self.run_id,
            "variant": self.variant,
            "started_at": self.started_at,
            "success": bool(success),
            "correctness_pass": bool(correctness_pass),
            "timings_ms": self.timings_ms,
            "counts": self.counts,
            "errors": self.errors,
            "total_ms": round((time.perf_counter() - self._start) * 1000.0, 3),
        })
        return result

    @staticmethod
    def append_jsonl(path, result):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
