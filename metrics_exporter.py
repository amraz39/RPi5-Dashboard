############################################################################################################
# Runs on RPi5
#
# v2.6
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

                    name = c.get("name", "").strip()

                    # Filter out docker stats artifacts:
                    #   "--"  -- transitional/partial entry emitted between refresh cycles
                    #   ""    -- empty name, invalid entry
                    if not name or name == "--":
                        continue

                    # Filter out zero-value ghost entries (all metrics are 0B/0).
                    # These appear when a container is in a transitional state
                    # and docker stats emits a placeholder row with no real data.
                    raw_mem = c.get("mem_usage", "").strip()
                    raw_cpu = c.get("cpu", "").strip()
                    if raw_mem in ("0B / 0B", "0B") and raw_cpu in ("0%", "0.00%"):
                        continue

                    c["cpu"]      = float(raw_cpu.replace("%", "").strip() or 0)
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


def docker_container_states():
    """
    Returns the CURRENT Docker state for every container.

    docker stats is intentionally used only for live resource statistics.
    It is NOT authoritative for container existence/state because the stats
    stream can stop reporting a container while its last cached entry remains.

    Returns:
        dict: {container_name: docker_state}
              docker_state is normally one of:
              running, exited, created, paused, restarting, dead
        None: if the Docker query itself fails.
    """
    try:
        out = subprocess.check_output(
            [
                "docker", "ps", "-a",
                "--format", "{{.Names}}\t{{.State}}"
            ],
            text=True,
            timeout=5
        )

        states = {}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue

            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue

            name, state = parts
            name = name.strip()
            state = state.strip().lower()

            if name:
                states[name] = state

        return states

    except Exception as e:
        print("Docker state query error:", e)
        return None


def stopped_containers(states=None):
    """
    Returns containers that currently exist but are not running.

    The Docker state query is authoritative. This prevents a stale entry
    left in _docker_cache by docker stats from being reported as running.
    """
    if states is None:
        states = docker_container_states()

    if states is None:
        return []

    stopped = []

    for name, state in states.items():
        if state == "running":
            continue

        stopped.append({
            "name":      name,
            "cpu":       0,
            "mem_perc":  0,
            "mem_used":  "—",
            "mem_limit": "—",
            "status":    "stopped",
            "net_io":    "—",
            "block_io":  "—"
        })

    return stopped


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


def wifi_signal():
    """
    Reads WiFi signal strength and link quality from /proc/net/wireless.

    /proc/net/wireless columns (after the two header lines):
      iface | status | link | level | noise | ...
      link  — link quality count (0–70 on most drivers)
      level — signal level in dBm (negative value, e.g. -36)
      noise — noise floor in dBm (-256 means not available on this driver)

    Returns a dict with:
      interface  — e.g. "wlan0"
      rssi_dbm   — signal level in dBm  (e.g. -36)
      quality    — 0–100 % derived from link quality count (link/70 * 100)
      noise_dbm  — noise floor in dBm (None if driver reports -256 sentinel)
    or None if no wireless interface is found.
    """
    try:
        with open("/proc/net/wireless") as f:
            lines = f.readlines()

        # First two lines are headers; data starts at line index 2
        for line in lines[2:]:
            parts = line.split()
            if not parts:
                continue

            iface = parts[0].rstrip(":")

            # link quality — raw count (0–70 typical); clamp to 100 %
            link_raw = float(parts[2].rstrip("."))
            quality  = min(100, int(link_raw / 70.0 * 100))

            # signal level in dBm — already negative on this driver
            level_raw = float(parts[3].rstrip("."))
            rssi_dbm  = int(level_raw) if level_raw < 0 else int(level_raw) - 256

            # noise floor — -256 is a driver sentinel meaning "not available"
            noise_raw = float(parts[4].rstrip("."))
            noise_dbm = None if noise_raw == -256 else (
                int(noise_raw) if noise_raw < 0 else int(noise_raw) - 256
            )

            return {
                "interface": iface,
                "rssi_dbm":  rssi_dbm,
                "quality":   quality,
                "noise_dbm": noise_dbm,
            }

    except Exception as e:
        print("WiFi signal error:", e)

    return None


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

    # Docker stats provides resource values, but its stream/cache is NOT
    # authoritative for whether a container is still running.
    #
    # Always reconcile the cache against Docker's current container state.
    # This is what prevents a stopped/disabled container from remaining
    # falsely marked as "running" because its last stats entry is cached.
    states = docker_container_states()

    with _docker_cache_lock:
        if states is None:
            # Docker state could not be queried. Do not trust stale cached
            # "running" entries, because that could falsely report containers
            # as online.
            running = []
            stopped = []
        else:
            # Remove every cached container that is no longer running.
            for name in list(_docker_cache):
                if states.get(name) != "running":
                    del _docker_cache[name]

            # Only containers that Docker currently reports as running may
            # come from the docker stats cache.
            running = [
                c for c in _docker_cache.values()
                if states.get(c.get("name")) == "running"
            ]

            stopped = stopped_containers(states)

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
        "wifi":         wifi_signal(),
        "docker":       all_containers,
    })


# ── Startup ───────────────────────────────────────────────────────────────────

_start_docker_stream()

app.run(host="0.0.0.0", port=8765)