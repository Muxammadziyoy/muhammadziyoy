"""wMaktab - bridge between Flask and the C++ statistics engine.

`compute()` sends grouped values to the compiled `statistics.cpp` program and
returns its JSON result.  The program is compiled automatically (g++, clang++
or c++) the first time it is needed.  If no compiler is available, or the
binary fails, an equivalent pure-Python implementation is used so the site
keeps working; `engine()` reports which one is active.
"""
import json
import math
import os
import shutil
import subprocess
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(BASE_DIR, "statistics.cpp")
BINARY = os.path.join(BASE_DIR, "bin", "wmaktab_stats" + (".exe" if os.name == "nt" else ""))

_lock = threading.Lock()
_ready = None  # None = not tried yet, True/False after the first build attempt


def build(force=False):
    """Compile statistics.cpp if needed. Returns True when the binary is usable."""
    global _ready
    with _lock:
        if not force and _ready is not None:
            return _ready
        up_to_date = (
            os.path.exists(BINARY)
            and os.path.exists(SOURCE)
            and os.path.getmtime(BINARY) >= os.path.getmtime(SOURCE)
        )
        if up_to_date and not force:
            _ready = True
            return True
        compiler = shutil.which("g++") or shutil.which("clang++") or shutil.which("c++")
        if not compiler or not os.path.exists(SOURCE):
            _ready = False
            return False
        os.makedirs(os.path.dirname(BINARY), exist_ok=True)
        try:
            result = subprocess.run(
                [compiler, "-O2", "-std=c++17", "-o", BINARY, SOURCE],
                capture_output=True, text=True, timeout=180,
            )
            _ready = result.returncode == 0 and os.path.exists(BINARY)
        except (OSError, subprocess.SubprocessError):
            _ready = False
        return _ready


def engine():
    """'cpp' when the compiled engine is in use, otherwise 'python'."""
    return "cpp" if build() else "python"


def compute(groups):
    """groups: {key: [numbers]} -> {key: stats dict} (keys without values are omitted).

    Keys must not contain whitespace.
    """
    groups = {k: v for k, v in groups.items() if v}
    if not groups:
        return {}
    if build():
        payload = "\n".join(f"{key} {value}" for key, values in groups.items() for value in values)
        try:
            proc = subprocess.run(
                [BINARY], input=payload, capture_output=True, text=True, timeout=15
            )
            if proc.returncode == 0:
                return json.loads(proc.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return _compute_python(groups)


def _to_scale(x):
    return max(1, min(5, math.floor(x + 0.5)))


def _compute_python(groups):
    """Pure-Python twin of statistics.cpp (same formulas, same output shape)."""
    out = {}
    for key, raw_values in groups.items():
        values = [float(v) for v in raw_values if math.isfinite(float(v))]
        n = len(values)
        if n == 0:
            continue
        ordered = sorted(values)
        mean = sum(values) / n
        median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
        stddev = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1)) if n > 1 else 0.0
        dist = {str(k): 0 for k in range(1, 6)}
        for x in values:
            dist[str(_to_scale(x))] += 1
        mode = 1
        for k in range(1, 6):
            if dist[str(k)] > dist[str(mode)]:
                mode = k
        out[key] = {
            "count": n,
            "mean": round(mean, 2),
            "median": round(median, 2),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "stddev": round(stddev, 2),
            "mode": mode,
            "mark": _to_scale(mean),
            "quality_rate": round(100.0 * sum(1 for x in values if math.floor(x + 0.5) >= 4) / n, 2),
            "pass_rate": round(100.0 * sum(1 for x in values if math.floor(x + 0.5) >= 3) / n, 2),
            "dist": dist,
        }
    return out
