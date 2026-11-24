"""CPU and process details for Blurt's performance report (Linux reads these from /proc)."""
import os
import platform
import subprocess


def _sysctl(name):
    try:
        r = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def cpu_info():
    info = {"model": _sysctl("machdep.cpu.brand_string") or platform.machine(),
            "mhz": None, "logical": os.cpu_count() or 1, "physical": None}
    try:
        info["physical"] = int(_sysctl("hw.physicalcpu"))
    except ValueError:
        pass
    perf = _sysctl("hw.perflevel0.physicalcpu")   # Apple silicon: performance cores
    eff = _sysctl("hw.perflevel1.physicalcpu")    # ...and efficiency cores
    if perf and eff:
        info["model"] += f" ({perf} performance + {eff} efficiency cores)"
    return info


def performance_cores():
    """Physical performance cores (all physical cores on Intel Macs)."""
    for name in ("hw.perflevel0.physicalcpu", "hw.physicalcpu"):
        try:
            return int(_sysctl(name))
        except ValueError:
            continue
    return os.cpu_count() or 1


def _ps(pid, field):
    pid = os.getpid() if pid == "self" else int(pid)
    try:
        r = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True,
                           text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""


def process_cpu_seconds(pid="self"):
    text = _ps(pid, "time")  # [[dd-]hh:]mm:ss.cc
    if not text:
        return None
    try:
        days, _, text = text.rpartition("-")
        seconds = 0.0
        for part in text.split(":"):
            seconds = seconds * 60 + float(part)
        return seconds + (int(days) * 86400 if days else 0)
    except ValueError:
        return None


def rss_mb(pid="self"):
    text = _ps(pid, "rss")  # kilobytes
    try:
        return int(text) // 1024
    except ValueError:
        return None
