############################################################################################################
# Runs on RPi5
#
# v2.2
############################################################################################################
#
# Metrics Exporter
# Exposes system metrics via HTTP
# on RPi5 run:
# python3 -u metrics_exporter.py
#
############################################################################################################
#
# PERFORMANCE NOTE
# ────────────────
# docker stats --no-stream blocks for ~2s waiting for a CPU measurement interval.
# Instead, this version runs `docker stats` in STREAMING mode in a background
# thread, continuously updating a per-container cache. The /metrics endpoint
# reads from that cache instantly — no subprocess wait per request.
#
############################################################################################################

from flask import Flask, jsonify
import psutil
import subprocess
import threading
import time
import json
import re

app = Flask(__name__)

net_prev  = psutil.net_io_counters()
disk_prev = psutil.disk_io_counters()
t_prev    = time.time()


# ── Docker stats cache ────────────────────────────────────────────────────────
#
# _docker_cache      : dict keyed by container name -> latest stats
# _docker_cache_lock : protects cache from concurrent read/write
#
# Updated continuously by _docker_stream_worker() daemon thread.
# /metrics reads instantly from cache — no subprocess wait.

_docker_cache      = {}   # { name: {...container dict...} }
_docker_cache_lock = threading.Lock()

# Strip ANSI escape codes
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]')


def _docker_stream_worker():
    """
    Runs docker stats in streaming mode.
    Each parsed line updates the per-container cache entry immediately.
    No block-boundary detection needed — every line is self-contained.
    """

    cmd = [
        "docker", "stats", "--no-trunc",
        "--format",
        '{"name":"{{.Name}}","cpu":"{{.CPUPerc}}",'
        '"mem_usage":"{{.MemUsage}}","mem_perc":"{{.MemPerc}}",'
        '"net_io":"{{.NetIO}}","block_io":"{{.BlockIO}}"}'
    ]

    while True:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True
            )

            for raw_line in proc.stdout:
                # Strip ANSI codes and whitespace
                line = _ANSI_RE.sub("", raw_line).strip()

                if not line or not line.startswith("{"):
                    continue

                try:
                    c = json.loads(line)

                    name = c.get("name", "")
                    if not name:
                        continue

                    c["cpu"]      = float(c["cpu"].replace("%", "").strip() or 0)
                    c["mem_perc"] = float(c["mem_perc"].replace("%", "").strip() or 0)

                    parts          = c["mem_usage"].split("/")
                    c["mem_used"]  = parts[0].strip() if parts else "?"
                    c["mem_limit"] = parts[1].strip() if len(parts) > 1 else "?"
                    del c["mem_usage"]

                    c["status"] = "running"

                    with _docker_cache_lock:
                        _docker_cache[name] = c

                except Exception as ex:
                    print("Docker stream parse error:", ex, repr(line))

            proc.wait()

        except Exception as e:
            print("Docker stream worker error:", e)

        # Process died — wait and restart
        time.sleep(3)


def _start_docker_stream():
    t = threading.Thread(target=_docker_stream_worker, daemon=True)
    t.start()
    print("Docker stats stream started")


def stopped_containers():
    """Returns list of stopped/exited container dicts."""
    try:
        out = subprocess.check_output(
            ["docker", "ps", "-a", "--filter", "status=exited",
             "--format", "{{.Names}}"],
            text=True, timeout=5
        )
        return [
            {
                "name":      n,
                "cpu":       0,
                "mem_perc":  0,
                "mem_used":  "—",
                "mem_limit": "—",
                "status":    "stopped",
                "net_io":    "—",
                "block_io":  "—"
            }
            for n in out.strip().splitlines() if n.strip()
        ]
    except:
        return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def cpu_temp():
    try:
        out = subprocess.check_output(["vcgencmd", "measure_temp"]).decode()
        return float(out.split("=")[1].replace("'C\n", ""))
    except:
        return None


def ssd_temp():
    try:
        out = subprocess.check_output(
            ["sudo", "smartctl", "-a", "/dev/nvme0"], text=True
        )
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Temperature:"):
                return int(line.split()[1])
    except Exception as e:
        print("SSD temp error:", e)
    return None


def cpu_freq():
    """Current CPU frequency in MHz."""
    try:
        freq = psutil.cpu_freq()
        return round(freq.current) if freq else None
    except:
        return None


def per_core_cpu():
    """List of per-core CPU usage percentages."""
    try:
        return psutil.cpu_percent(percpu=True)
    except:
        return []


def throttle_flags():
    """
    Returns a dict of Raspberry Pi throttle/undervoltage flags.
    Bit meanings from vcgencmd get_throttled:
      0  - under-voltage detected
      1  - arm frequency capped
      2  - currently throttled
      3  - soft temperature limit active
      16 - under-voltage has occurred
      17 - arm frequency capping has occurred
      18 - throttling has occurred
      19 - soft temperature limit has occurred
    """
    try:
        out = subprocess.check_output(["vcgencmd", "get_throttled"]).decode()
        val = int(out.strip().split("=")[1], 16)
        return {
            "raw":                 hex(val),
            "under_voltage_now":   bool(val & (1 << 0)),
            "freq_capped_now":     bool(val & (1 << 1)),
            "throttled_now":       bool(val & (1 << 2)),
            "soft_temp_limit_now": bool(val & (1 << 3)),
            "under_voltage_ever":  bool(val & (1 << 16)),
            "freq_capped_ever":    bool(val & (1 << 17)),
            "throttled_ever":      bool(val & (1 << 18)),
            "soft_temp_limit_ever":bool(val & (1 << 19)),
        }
    except Exception as e:
        print("Throttle error:", e)
        return {}


def format_uptime(seconds):
    seconds = int(seconds)
    days    = seconds // 86400
    hours   = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    return {"days": days, "hours": hours, "minutes": minutes, "total_seconds": seconds}


# ── Route ─────────────────────────────────────────────────────────────────────

@app.route("/metrics")
def metrics():
    global net_prev, disk_prev, t_prev

    now = time.time()
    dt  = max(now - t_prev, 1)

    net  = psutil.net_io_counters()
    disk = psutil.disk_io_counters()

    rx = (net.bytes_recv   - net_prev.bytes_recv)   / dt
    tx = (net.bytes_sent   - net_prev.bytes_sent)   / dt
    rd = (disk.read_bytes  - disk_prev.read_bytes)  / dt
    wr = (disk.write_bytes - disk_prev.write_bytes) / dt

    net_prev  = net
    disk_prev = disk
    t_prev    = now

    # Read running containers from cache (instant — no subprocess wait)
    with _docker_cache_lock:
        running = list(_docker_cache.values())

    stopped        = stopped_containers()

    # Exclude from stopped list any container already in running cache
    running_names  = {c["name"] for c in running}
    stopped        = [c for c in stopped if c["name"] not in running_names]

    all_containers = running + stopped

    return jsonify({
        # ── Core ──────────────────────────────────────────
        "cpu":          psutil.cpu_percent(),
        "cpu_temp":     cpu_temp(),
        "cpu_freq_mhz": cpu_freq(),
        "cpu_cores":    per_core_cpu(),
        "ram":          psutil.virtual_memory().percent,
        "ssd_temp":     ssd_temp(),
        "disk_used":    psutil.disk_usage("/").percent,
        "disk_read":    round(rd / 1024 / 1024, 2),
        "disk_write":   round(wr / 1024 / 1024, 2),
        "net_rx":       round(rx / 1024 / 1024, 2),
        "net_tx":       round(tx / 1024 / 1024, 2),

        # ── Extended ──────────────────────────────────────
        "uptime":       format_uptime(time.time() - psutil.boot_time()),
        "throttle":     throttle_flags(),
        "docker":       all_containers,
    })


# ── Startup ───────────────────────────────────────────────────────────────────

_start_docker_stream()

app.run(host="0.0.0.0", port=8765)
