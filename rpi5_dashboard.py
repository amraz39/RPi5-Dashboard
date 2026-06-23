############################################################################################################
# Runs on Windows PC
#
# v2.1
############################################################################################################
#
# RPi5 Dashboard
# Displays system metrics from RPi5
# 
# python3 -u rpi5_dashboard.py
# or:
# RPi5_dashboard.exe
#
############################################################################################################

import sys
import json
import collections
import requests

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QThread, Signal, QObject
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QPainterPath, QFont
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QGridLayout,
    QVBoxLayout, QHBoxLayout, QSizePolicy, QFrame,
    QComboBox, QScrollArea, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView
)

# ── Config ────────────────────────────────────────────────────────────────────
cfg    = json.load(open("config.json"))
ALERTS = cfg.get("alerts", {"cpu_temp": 75, "ssd_temp": 75, "ram": 90})
HOSTS  = cfg.get("hosts", [])
if not HOSTS:
    HOSTS = [{
        "label": cfg["rpi"].get("label", cfg["rpi"]["host"]),
        "host":  cfg["rpi"]["host"],
        "port":  cfg["rpi"]["port"],
    }]

REFRESH_MS = cfg["rpi"]["refresh_seconds"] * 1000
HISTORY    = 60

# ── Palette ───────────────────────────────────────────────────────────────────
BG       = QColor("#0b0f19")
CARD_BDR = QColor("#1e2a3a")
ACCENT   = QColor("#38bdf8")
ACCENT2  = QColor("#818cf8")
WARN     = QColor("#f59e0b")
DANGER   = QColor("#ef4444")
OK       = QColor("#22c55e")
TEXT_PRI = QColor("#f1f5f9")

# Distinct colours per core
CORE_COLORS = [
    QColor("#38bdf8"),  # core 0 – sky blue
    QColor("#a78bfa"),  # core 1 – violet
    QColor("#34d399"),  # core 2 – emerald
    QColor("#fb923c"),  # core 3 – orange
    QColor("#f472b6"),  # core 4 – pink
    QColor("#facc15"),  # core 5 – yellow
    QColor("#60a5fa"),  # core 6 – blue
    QColor("#4ade80"),  # core 7 – green
]

ICONS = {
    "cpu":       "⚙",
    "cpu_temp":  "🌡",
    "ssd_temp":  "💾",
    "ram":       "🧠",
    "disk_used": "📀",
    "disk_read": "⬇",
    "disk_write":"⬆",
    "net_rx":    "↓",
    "net_tx":    "↑",
}


# ── Background fetch worker ───────────────────────────────────────────────────
class Fetcher(QObject):
    result = Signal(dict)
    failed = Signal()

    def __init__(self, url):
        super().__init__()
        self.url = url

    def fetch(self):
        try:
            data = requests.get(self.url, timeout=3).json()
            self.result.emit(data)
        except Exception as ex:
            print("Fetch error:", ex)
            self.failed.emit()


# ── Sparkline ─────────────────────────────────────────────────────────────────
class Sparkline(QWidget):
    def __init__(self, color=ACCENT, y_max=None, height=44, parent=None):
        super().__init__(parent)
        self.data  = collections.deque([0.0] * HISTORY, maxlen=HISTORY)
        self.color = color
        self.y_max = y_max
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def push(self, value: float):
        self.data.append(value)
        self.update()

    def reset(self):
        self.data = collections.deque([0.0] * HISTORY, maxlen=HISTORY)
        self.update()

    def paintEvent(self, _):
        vals = list(self.data)
        w, h = self.width(), self.height()
        lo   = 0.0
        hi   = self.y_max if self.y_max else (max(vals) or 1.0)

        def pt(i, v):
            x = i / (len(vals) - 1) * w if len(vals) > 1 else 0
            y = h - (v - lo) / (hi - lo) * (h - 4) - 2
            return QPointF(x, y)

        path = QPainterPath()
        path.moveTo(QPointF(0, h))
        for i, v in enumerate(vals):
            path.lineTo(pt(i, v))
        path.lineTo(QPointF(w, h))
        path.closeSubpath()

        c    = self.color
        fill = QLinearGradient(0, 0, 0, h)
        fill.setColorAt(0, QColor(c.red(), c.green(), c.blue(), 70))
        fill.setColorAt(1, QColor(c.red(), c.green(), c.blue(), 0))

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillPath(path, QBrush(fill))

        lp = QPainterPath()
        lp.moveTo(pt(0, vals[0]))
        for i, v in enumerate(vals[1:], 1):
            lp.lineTo(pt(i, v))
        p.strokePath(lp, QPen(self.color, 1.5))


# ── Arc Gauge ─────────────────────────────────────────────────────────────────
class ArcGauge(QWidget):
    def __init__(self, max_val=100, warn=75, danger=90, parent=None):
        super().__init__(parent)
        self.value   = 0.0
        self.max_val = max_val
        self.warn    = warn
        self.danger  = danger
        self.setFixedSize(100, 62)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def set_value(self, v: float):
        self.value = max(0.0, min(float(v), self.max_val))
        self.update()

    def _color(self):
        pct = self.value / self.max_val * 100
        if pct >= self.danger: return DANGER
        if pct >= self.warn:   return WARN
        return OK

    def paintEvent(self, _):
        w, h   = self.width(), self.height()
        cx, cy = w / 2, h - 4
        r      = min(w, h * 2) / 2 - 8
        rect   = QRectF(cx - r, cy - r, r * 2, r * 2)
        p      = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        p.setPen(QPen(CARD_BDR, 7, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(rect, 180 * 16, -180 * 16)

        span = (self.value / self.max_val) * 180
        p.setPen(QPen(self._color(), 7, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(rect, 180 * 16, int(-span * 16))

        p.setPen(QPen(TEXT_PRI))
        p.setFont(QFont("Segoe UI", 10, QFont.Bold))
        p.drawText(QRectF(0, cy - 16, w, 18),
                   Qt.AlignHCenter | Qt.AlignVCenter,
                   f"{self.value:.0f}")


# ── Per-core sparkline panel ──────────────────────────────────────────────────
class CoreSparklines(QFrame):
    """
    Dynamically creates one labelled sparkline per CPU core.
    Laid out in a 2-column grid so it stays compact.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background:transparent;")
        self._sparks: list[tuple[QLabel, QLabel, Sparkline]] = []
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 4, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(4)

    def _build(self, n: int):
        # Clear old widgets
        for lbl, val, sp in self._sparks:
            lbl.deleteLater(); val.deleteLater(); sp.deleteLater()
        self._sparks.clear()

        for i in range(n):
            col_offset = (i % 2) * 3          # 2 cores per row, 3 cols each
            row        = i // 2

            color = CORE_COLORS[i % len(CORE_COLORS)]

            # "C0" label
            lbl = QLabel(f"C{i}")
            lbl.setStyleSheet(
                f"font-size:9px; font-weight:700; color:{color.name()}; background:transparent;"
            )
            lbl.setFixedWidth(18)
            self._grid.addWidget(lbl, row, col_offset, Qt.AlignVCenter)

            # current value
            val_lbl = QLabel("0%")
            val_lbl.setStyleSheet(
                "font-size:9px; color:#94a3b8; background:transparent;"
            )
            val_lbl.setFixedWidth(30)
            self._grid.addWidget(val_lbl, row, col_offset + 1, Qt.AlignVCenter)

            # sparkline
            spark = Sparkline(color=color, y_max=100.0, height=28)
            self._grid.addWidget(spark, row, col_offset + 2)

            self._sparks.append((lbl, val_lbl, spark))

    def update_cores(self, values: list):
        if len(values) != len(self._sparks):
            self._build(len(values))
        for i, v in enumerate(values):
            _, val_lbl, spark = self._sparks[i]
            spark.push(float(v))
            col = DANGER.name() if v > 80 else WARN.name() if v > 60 else "#94a3b8"
            val_lbl.setText(f"{v:.0f}%")
            val_lbl.setStyleSheet(
                f"font-size:9px; color:{col}; background:transparent;"
            )

    def reset(self):
        for _, val_lbl, spark in self._sparks:
            spark.reset()
            val_lbl.setText("0%")


# ── MetricCard ────────────────────────────────────────────────────────────────
GAUGE_KEYS = {"cpu", "cpu_temp", "ssd_temp", "ram", "disk_used"}

class MetricCard(QFrame):
    def __init__(self, title, key, unit, warn=None, danger=None, y_max=None):
        super().__init__()
        self.key    = key
        self.unit   = unit
        self.warn   = warn
        self.danger = danger

        self.setObjectName("card")
        self.setStyleSheet("""
            QFrame#card {
                background:#111827;
                border:1px solid #1e2a3a;
                border-radius:16px;
            }
        """)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(4)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        icon = QLabel(ICONS.get(key, "•"))
        icon.setStyleSheet("font-size:16px; color:#38bdf8; background:transparent;")
        icon.setFixedWidth(22)
        hdr.addWidget(icon)
        tlbl = QLabel(title.upper())
        tlbl.setStyleSheet(
            "font-size:9px; letter-spacing:2px; color:#64748b; "
            "font-weight:600; background:transparent;"
        )
        hdr.addWidget(tlbl)
        hdr.addStretch()
        self.dot = QLabel("●")
        self.dot.setStyleSheet("font-size:9px; color:#22c55e; background:transparent;")
        hdr.addWidget(self.dot)
        root.addLayout(hdr)

        # Gauge + value row
        mid = QHBoxLayout()
        mid.setSpacing(8)
        if key in GAUGE_KEYS:
            gw = warn   if warn   else 75
            gd = danger if danger else 90
            self.gauge = ArcGauge(max_val=100, warn=gw, danger=gd)
            mid.addWidget(self.gauge, alignment=Qt.AlignLeft | Qt.AlignVCenter)
        else:
            self.gauge = None
        self.val_lbl = QLabel("—")
        self.val_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.val_lbl.setStyleSheet(
            "font-size:26px; font-weight:700; color:#f1f5f9; background:transparent;"
        )
        mid.addWidget(self.val_lbl, 1)
        root.addLayout(mid)

        # Per-core sparklines (CPU card only)
        if key == "cpu":
            self.core_sparks = CoreSparklines()
            root.addWidget(self.core_sparks)
        else:
            self.core_sparks = None

        # Overall sparkline
        self.spark = Sparkline(
            color=ACCENT if key in GAUGE_KEYS else ACCENT2,
            y_max=y_max
        )
        root.addWidget(self.spark)

        ul = QLabel(unit)
        ul.setAlignment(Qt.AlignRight)
        ul.setStyleSheet("font-size:9px; color:#475569; background:transparent;")
        root.addWidget(ul)

    def update_value(self, raw: float):
        txt = f"{raw:.1f}" if self.unit in {"%", "°C"} else f"{raw:.2f}"
        self.val_lbl.setText(txt)
        if self.warn and self.danger:
            col = "#ef4444" if raw >= self.danger else "#f59e0b" if raw >= self.warn else "#f1f5f9"
            self.val_lbl.setStyleSheet(
                f"font-size:26px; font-weight:700; color:{col}; background:transparent;"
            )
        if self.gauge:
            self.gauge.set_value(raw)
        self.spark.push(raw)
        self.dot.setStyleSheet("font-size:9px; color:#22c55e; background:transparent;")

    def set_offline(self):
        self.val_lbl.setText("—")
        self.val_lbl.setStyleSheet(
            "font-size:26px; font-weight:700; color:#334155; background:transparent;"
        )
        self.dot.setStyleSheet("font-size:9px; color:#ef4444; background:transparent;")
        if self.gauge:
            self.gauge.set_value(0)
        self.spark.reset()
        if self.core_sparks:
            self.core_sparks.reset()


# ── InfoTile ──────────────────────────────────────────────────────────────────
class InfoTile(QFrame):
    def __init__(self, title, icon):
        super().__init__()
        self.setObjectName("infotile")
        self.setStyleSheet("""
            QFrame#infotile {
                background:#111827;
                border:1px solid #1e2a3a;
                border-radius:16px;
            }
        """)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(90)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        ilbl = QLabel(icon)
        ilbl.setStyleSheet("font-size:15px; color:#38bdf8; background:transparent;")
        hdr.addWidget(ilbl)
        tlbl = QLabel(title.upper())
        tlbl.setStyleSheet(
            "font-size:9px; letter-spacing:2px; color:#64748b; "
            "font-weight:600; background:transparent;"
        )
        hdr.addWidget(tlbl)
        hdr.addStretch()
        lay.addLayout(hdr)

        self.body = QLabel("—")
        self.body.setStyleSheet(
            "font-size:14px; font-weight:600; color:#f1f5f9; background:transparent;"
        )
        self.body.setWordWrap(True)
        lay.addWidget(self.body)

    def set_text(self, text, color="#f1f5f9"):
        self.body.setText(text)
        self.body.setStyleSheet(
            f"font-size:14px; font-weight:600; color:{color}; background:transparent;"
        )

    def clear(self):
        self.body.setText("—")
        self.body.setStyleSheet(
            "font-size:14px; font-weight:600; color:#334155; background:transparent;"
        )


# ── Docker table ──────────────────────────────────────────────────────────────
class DockerTable(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("dockercard")
        self.setStyleSheet("""
            QFrame#dockercard {
                background:#111827;
                border:1px solid #1e2a3a;
                border-radius:16px;
            }
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        hdr = QHBoxLayout()
        icon = QLabel("🐳")
        icon.setStyleSheet("font-size:16px; background:transparent;")
        hdr.addWidget(icon)
        tlbl = QLabel("DOCKER CONTAINERS")
        tlbl.setStyleSheet(
            "font-size:9px; letter-spacing:2px; color:#64748b; "
            "font-weight:600; background:transparent;"
        )
        hdr.addWidget(tlbl)
        hdr.addStretch()
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet("font-size:9px; color:#475569; background:transparent;")
        hdr.addWidget(self.count_lbl)
        lay.addLayout(hdr)

        cols = ["Container", "Status", "CPU %", "Mem %", "Mem Used", "Net I/O", "Block I/O"]
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(cols)):
            self.table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setStyleSheet("""
            QTableWidget {
                background: transparent;
                color: #f1f5f9;
                font-size: 12px;
                border: none;
                outline: none;
            }
            QHeaderView::section {
                background: #0b0f19;
                color: #475569;
                font-size: 9px;
                letter-spacing: 1px;
                font-weight: 600;
                border: none;
                padding: 4px 6px;
            }
            QTableWidget::item {
                padding: 5px 6px;
                border-bottom: 1px solid #1e2a3a;
            }
            QTableWidget::item:selected { background: #1e3a5f; }
            QScrollBar:vertical {
                background: #0b0f19; width: 6px; border-radius: 3px;
            }
            QScrollBar::handle:vertical {
                background: #1e2a3a; border-radius: 3px;
            }
        """)
        lay.addWidget(self.table)

    def clear(self):
        """Clear all rows and reset counter label."""
        self.table.setRowCount(0)
        self.count_lbl.setText("—")
        row_h = self.table.verticalHeader().defaultSectionSize()
        hdr_h = self.table.horizontalHeader().height()
        self.table.setFixedHeight(hdr_h + row_h)

    def update_containers(self, containers: list):
        running = [c for c in containers if c.get("status") == "running"]
        stopped = [c for c in containers if c.get("status") != "running"]
        self.count_lbl.setText(f"▶ {len(running)} running  ■ {len(stopped)} stopped")
        self.table.setRowCount(0)
        for c in sorted(containers, key=lambda x: x.get("status") != "running"):
            row = self.table.rowCount()
            self.table.insertRow(row)
            is_running = c.get("status") == "running"

            n = QTableWidgetItem(c.get("name", "?"))
            n.setForeground(QColor("#f1f5f9" if is_running else "#475569"))
            self.table.setItem(row, 0, n)

            s = QTableWidgetItem("● running" if is_running else "■ stopped")
            s.setForeground(QColor("#22c55e" if is_running else "#ef4444"))
            self.table.setItem(row, 1, s)

            cpu_val = c.get("cpu", 0)
            ci = QTableWidgetItem(f"{cpu_val:.1f}%" if is_running else "—")
            ci.setForeground(QColor("#ef4444" if cpu_val > 80 else
                                    "#f59e0b" if cpu_val > 50 else "#f1f5f9"))
            self.table.setItem(row, 2, ci)

            mem_val = c.get("mem_perc", 0)
            mi = QTableWidgetItem(f"{mem_val:.1f}%" if is_running else "—")
            mi.setForeground(QColor("#ef4444" if mem_val > 80 else
                                    "#f59e0b" if mem_val > 50 else "#f1f5f9"))
            self.table.setItem(row, 3, mi)

            self.table.setItem(row, 4, QTableWidgetItem(c.get("mem_used",  "—") if is_running else "—"))
            self.table.setItem(row, 5, QTableWidgetItem(c.get("net_io",    "—") if is_running else "—"))
            self.table.setItem(row, 6, QTableWidgetItem(c.get("block_io",  "—") if is_running else "—"))

        row_h = self.table.verticalHeader().defaultSectionSize()
        hdr_h = self.table.horizontalHeader().height()
        self.table.setFixedHeight(
            min(400, self.table.rowCount() * row_h + hdr_h + 4)
        )


# ── Header ────────────────────────────────────────────────────────────────────
class HeaderBar(QWidget):
    def __init__(self, on_host_change):
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedHeight(52)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(10)

        pi = QLabel("🍓")
        pi.setStyleSheet("font-size:26px; background:transparent;")
        lay.addWidget(pi)

        title = QLabel("RPi5  Monitor")
        title.setStyleSheet(
            "font-size:18px; font-weight:700; color:#f1f5f9; "
            "letter-spacing:1px; background:transparent;"
        )
        lay.addWidget(title)
        lay.addStretch()

        if len(HOSTS) > 1:
            host_lbl = QLabel("HOST:")
            host_lbl.setStyleSheet(
                "font-size:9px; letter-spacing:2px; color:#475569; background:transparent;"
            )
            lay.addWidget(host_lbl)

            self.host_combo = QComboBox()
            self.host_combo.setStyleSheet("""
                QComboBox {
                    background:#1e2a3a; color:#f1f5f9;
                    border:1px solid #334155; border-radius:8px;
                    padding:4px 10px; font-size:12px; min-width:180px;
                }
                QComboBox::drop-down { border:none; }
                QComboBox QAbstractItemView {
                    background:#1e2a3a; color:#f1f5f9;
                    selection-background-color:#38bdf8;
                    border:1px solid #334155;
                }
            """)
            for h in HOSTS:
                self.host_combo.addItem(
                    f"{h.get('label', h['host'])}  ({h['host']}:{h['port']})"
                )
            self.host_combo.currentIndexChanged.connect(on_host_change)
            lay.addWidget(self.host_combo)
        else:
            self.host_combo = None
            hi = QLabel(f"{HOSTS[0]['host']}:{HOSTS[0]['port']}")
            hi.setStyleSheet("font-size:11px; color:#475569; background:transparent;")
            lay.addWidget(hi)

        self.conn_lbl = QLabel("● CONNECTING")
        self.conn_lbl.setStyleSheet(
            "font-size:11px; color:#f59e0b; font-weight:600; background:transparent;"
        )
        lay.addWidget(self.conn_lbl)

    def set_online(self, online: bool):
        if online:
            self.conn_lbl.setText("● LIVE")
            self.conn_lbl.setStyleSheet(
                "font-size:11px; color:#22c55e; font-weight:600; background:transparent;"
            )
        else:
            self.conn_lbl.setText("● OFFLINE")
            self.conn_lbl.setStyleSheet(
                "font-size:11px; color:#ef4444; font-weight:600; background:transparent;"
            )


def section_label(text):
    w = QWidget()
    w.setAttribute(Qt.WA_TranslucentBackground)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(4, 6, 4, 2)
    lbl = QLabel(text)
    lbl.setStyleSheet(
        "font-size:9px; letter-spacing:3px; color:#334155; "
        "font-weight:700; background:transparent;"
    )
    lay.addWidget(lbl)
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet("color:#1e2a3a;")
    lay.addWidget(line, 1)
    return w


# ── Main Dashboard ────────────────────────────────────────────────────────────
class Dashboard(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RPi5 Dashboard")
        self.resize(1200, 900)
        self.setStyleSheet(f"background:{BG.name()};")

        self._host_idx = 0
        self._thread   = None
        self._fetcher  = None

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        self._root = QVBoxLayout(inner)
        self._root.setContentsMargins(18, 14, 18, 14)
        self._root.setSpacing(8)
        scroll.setWidget(inner)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.header = HeaderBar(self._on_host_change)
        outer.addWidget(self.header)
        outer.addWidget(scroll)

        self._build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._trigger_fetch)
        self.timer.start(REFRESH_MS)
        self._trigger_fetch()

    def _build_ui(self):
        root = self._root

        root.addWidget(section_label("SYSTEM"))
        sys_grid = QGridLayout()
        sys_grid.setSpacing(10)
        self.tiles: dict[str, MetricCard] = {}
        system_tiles = [
            ("CPU Usage", "cpu",       "%",   None,               None,                  100.0),
            ("CPU Temp",  "cpu_temp",  "°C",  ALERTS["cpu_temp"], ALERTS["cpu_temp"]+10, 100.0),
            ("SSD Temp",  "ssd_temp",  "°C",  ALERTS["ssd_temp"], ALERTS["ssd_temp"]+10, 100.0),
            ("RAM",       "ram",       "%",   ALERTS["ram"],       95,                   100.0),
            ("Disk Used", "disk_used", "%",   80,                  95,                   100.0),
        ]
        for col, (title, key, unit, w, d, ym) in enumerate(system_tiles):
            card = MetricCard(title, key, unit, warn=w, danger=d, y_max=ym)
            sys_grid.addWidget(card, 0, col)
            self.tiles[key] = card
        root.addLayout(sys_grid)

        root.addWidget(section_label("SYSTEM INFO"))
        info_row = QHBoxLayout()
        info_row.setSpacing(10)
        self.uptime_tile   = InfoTile("Uptime",   "⏱")
        self.freq_tile     = InfoTile("CPU Freq", "⚡")
        self.throttle_tile = InfoTile("Throttle", "⚠")
        info_row.addWidget(self.uptime_tile)
        info_row.addWidget(self.freq_tile)
        info_row.addWidget(self.throttle_tile)
        root.addLayout(info_row)

        root.addWidget(section_label("DISK & NETWORK I/O"))
        io_grid = QGridLayout()
        io_grid.setSpacing(10)
        io_tiles = [
            ("Disk Read",  "disk_read",  "MB/s", None, None, None),
            ("Disk Write", "disk_write", "MB/s", None, None, None),
            ("Net RX",     "net_rx",     "MB/s", None, None, None),
            ("Net TX",     "net_tx",     "MB/s", None, None, None),
        ]
        for col, (title, key, unit, w, d, ym) in enumerate(io_tiles):
            card = MetricCard(title, key, unit, warn=w, danger=d, y_max=ym)
            io_grid.addWidget(card, 0, col)
            self.tiles[key] = card
        root.addLayout(io_grid)

        root.addWidget(section_label("DOCKER"))
        self.docker_table = DockerTable()
        root.addWidget(self.docker_table)
        root.addStretch()

    def _current_url(self):
        h = HOSTS[self._host_idx]
        return f"http://{h['host']}:{h['port']}/metrics"

    def _trigger_fetch(self):
        if self._thread and self._thread.isRunning():
            return
        self._thread  = QThread()
        self._fetcher = Fetcher(self._current_url())
        self._fetcher.moveToThread(self._thread)
        self._thread.started.connect(self._fetcher.fetch)
        self._fetcher.result.connect(self._on_data)
        self._fetcher.failed.connect(self._on_fail)
        self._fetcher.result.connect(self._thread.quit)
        self._fetcher.failed.connect(self._thread.quit)
        self._thread.start()

    def _on_data(self, d: dict):
        self.header.set_online(True)

        mapping = {
            "cpu":        d.get("cpu", 0),
            "cpu_temp":   d.get("cpu_temp", 0),
            "ssd_temp":   d.get("ssd_temp", 0),
            "ram":        d.get("ram", 0),
            "disk_used":  d.get("disk_used", 0),
            "disk_read":  d.get("disk_read", 0),
            "disk_write": d.get("disk_write", 0),
            "net_rx":     d.get("net_rx", 0),
            "net_tx":     d.get("net_tx", 0),
        }
        for key, val in mapping.items():
            self.tiles[key].update_value(float(val or 0))

        # Per-core sparklines
        cores = d.get("cpu_cores", [])
        if cores and self.tiles["cpu"].core_sparks:
            self.tiles["cpu"].core_sparks.update_cores(cores)

        ut = d.get("uptime", {})
        if ut:
            self.uptime_tile.set_text(
                f"{ut.get('days',0)}d  {ut.get('hours',0)}h  {ut.get('minutes',0)}m"
            )

        freq = d.get("cpu_freq_mhz")
        if freq:
            self.freq_tile.set_text(f"{freq} MHz")

        thr = d.get("throttle", {})
        if thr:
            issues = []
            if thr.get("under_voltage_now"):  issues.append("Under-voltage!")
            if thr.get("throttled_now"):       issues.append("Throttled!")
            if thr.get("freq_capped_now"):     issues.append("Freq capped")
            if thr.get("soft_temp_limit_now"): issues.append("Temp limit")
            past = []
            if thr.get("throttled_ever"):      past.append("throttled")
            if thr.get("under_voltage_ever"):  past.append("undervolt")
            if issues:
                self.throttle_tile.set_text("  ".join(issues), color="#ef4444")
            elif past:
                self.throttle_tile.set_text("OK now\nPast: " + ", ".join(past), color="#f59e0b")
            else:
                self.throttle_tile.set_text("✓ All clear", color="#22c55e")

        self.docker_table.update_containers(d.get("docker", []))

    def _on_fail(self):
        self.header.set_online(False)
        for tile in self.tiles.values():
            tile.set_offline()
        self.uptime_tile.clear()
        self.freq_tile.clear()
        self.throttle_tile.clear()
        self.docker_table.clear()

    def _on_host_change(self, idx: int):
        self._host_idx = idx
        for tile in self.tiles.values():
            tile.set_offline()
        self.uptime_tile.clear()
        self.freq_tile.clear()
        self.throttle_tile.clear()
        self.docker_table.clear()
        self._trigger_fetch()


app = QApplication(sys.argv)
app.setFont(QFont("Segoe UI", 10))
w = Dashboard()
w.show()
app.exec()
