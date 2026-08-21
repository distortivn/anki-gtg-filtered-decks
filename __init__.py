# GTG Filtered Deck Creator  –  nested structure edition
# v2: persistent settings, course-based day tracking, one-click repeat

from aqt import mw
from aqt.utils import showInfo
from aqt.qt import *
from datetime import datetime, timedelta
import re
import time as _time

ADDON = __name__

# ── Regex ─────────────────────────────────────────────────────────────────────

# Timeslot leaf: 06am-07am, 08:30am-09:30am
_SLOT_RE = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}$")
# Numeric leaf (deck number inside a timeslot): 1, 2, 3 …
_NUM_RE  = re.compile(r"^\d+$")


# ── Config persistence ──────────────────────────────────────────────────────────
# Anki stores addon config as: config.json (shipped defaults) merged with
# meta.json (the user's saved overrides). getConfig/writeConfig read & write
# that merged/override layer for us — this is what makes settings "stick".

DEFAULT_CONFIG = {
    "gtg_filter_configs": {},
    "last_used": {
        "filter": "",
        "start_hour": 10,
        "end_hour": 22,
        "cards_per_hour": 45,
        "decks_per_hour": 3,
        "parent_deck": "",
        "day_name": "day1",
        "naming_word": "Day",
        "course_id": "",
    },
    "naming_presets": ["Day", "Session", "Round", "Phase", "Sprint", "Wave", "Batch", "Cycle", "Stage", "Leg"],
    "courses": {},
}

def load_config():
    cfg = mw.addonManager.getConfig(ADDON) or {}
    changed = False
    for key, default_val in DEFAULT_CONFIG.items():
        if key not in cfg:
            cfg[key] = default_val.copy() if isinstance(default_val, dict) else list(default_val) if isinstance(default_val, list) else default_val
            changed = True
    cfg.setdefault("last_used", {})
    for key, default_val in DEFAULT_CONFIG["last_used"].items():
        cfg["last_used"].setdefault(key, default_val)
    if changed:
        mw.addonManager.writeConfig(ADDON, cfg)
    return cfg

def save_config(cfg):
    mw.addonManager.writeConfig(ADDON, cfg)

def _ceil_div(a, b):
    if not b:
        return 1
    return -(-a // b)

def _remember_naming_word(cfg, word):
    presets = cfg.setdefault("naming_presets", [])
    if word and word not in presets:
        presets.append(word)


# ── Course model ─────────────────────────────────────────────────────────────

def create_course(cfg, display_name, filter_str, total_cards, hours_per_day,
                   cards_per_hour, decks_per_hour, naming_word, parent_deck):
    per_day    = hours_per_day * cards_per_hour
    total_days = _ceil_div(total_cards, per_day)
    base = re.sub(r"[^A-Za-z0-9]+", "", display_name).lower() or "course"
    cid, n = base, 2
    courses = cfg.setdefault("courses", {})
    while cid in courses:
        cid = f"{base}{n}"
        n += 1
    courses[cid] = {
        "display_name":   display_name,
        "filter":         filter_str,
        "total_cards":    total_cards,
        "hours_per_day":  hours_per_day,
        "cards_per_hour": cards_per_hour,
        "decks_per_hour": decks_per_hour,
        "total_days":     total_days,
        "current_day":    0,
        "naming_word":    naming_word,
        "parent_deck":    parent_deck,
        "created_ts":     _time.time(),
    }
    _remember_naming_word(cfg, naming_word)
    save_config(cfg)
    return cid


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_time(dt):
    """24h zero-padded so deck names sort lexicographically in Anki browser."""
    return f"{dt.hour:02d}:{dt.minute:02d}" if dt.minute else f"{dt.hour:02d}:00"

def time_slot_label(hour_offset, start_hour):
    base  = datetime(2000, 1, 1, start_hour, 0)
    start = base + timedelta(hours=hour_offset)
    end   = start + timedelta(hours=1)
    return f"{fmt_time(start)}-{fmt_time(end)}"

def _full_name(did):
    try:
        deck = mw.col.decks.get(did)
        return deck.get("name", "") if deck else ""
    except Exception:
        return ""

def get_deck_id_by_name(full_name):
    for d in mw.col.decks.all_names_and_ids():
        if _full_name(d.id) == full_name:
            return d.id
    return None

def ensure_normal_deck(full_name):
    """Create a normal (non-filtered) deck by full path if it doesn't exist."""
    existing = get_deck_id_by_name(full_name)
    if existing is not None:
        return existing
    did = mw.col.decks.id(full_name)   # creates all ancestors automatically
    return did

def empty_deck(did) -> int:
    import time as _t
    try:
        deck = mw.col.decks.get(did)
        if not deck or not deck.get("dyn", 0):
            return 0
        count = mw.col.db.scalar(f"SELECT count() FROM cards WHERE did = {did}") or 0
        if count == 0:
            return 0
        emptied = False
        for fn in (
            lambda: mw.col.sched.empty_filtered_deck(did),
            lambda: mw.col.backend.empty_filtered_deck(did),
        ):
            try:
                fn(); emptied = True; break
            except Exception:
                pass
        if not emptied:
            mw.col.db.execute(
                f"UPDATE cards SET did = odid, odid = 0, mod = ?, usn = -1 "
                f"WHERE did = {did} AND odid != 0", int(_t.time())
            )
            mw.col.db.execute(
                f"UPDATE cards SET odid = 0, mod = ?, usn = -1 "
                f"WHERE did = {did} AND odid = 0", int(_t.time())
            )
        return count
    except Exception:
        return 0


# ── GTG Day-group scanner ─────────────────────────────────────────────────────

def get_gtg_day_groups():
    """
    Find every "day subdeck" that was created by this plugin (nested structure),
    plus any manually-created non-filter subdecks that themselves contain
    filter decks matching the same pattern.

    Nested target structure:
        Parent::DayName::HH am-HH am::1
        Parent::DayName::HH am-HH am::2
        ...

    Returns:
        dict { "Parent::DayName": [(did, full_name), ...] }
        where the list contains every filtered deck (the numbered leaves) plus
        the day container itself (as a non-filter entry) so deletion is complete.
    """
    groups = {}   # day_path -> [(did, full_name), ...]

    for d in mw.col.decks.all_names_and_ids():
        try:
            deck = mw.col.decks.get(d.id)
        except Exception:
            continue
        if not deck:
            continue

        full_name = deck.get("name", "") or _full_name(d.id)
        parts = full_name.split("::")

        # ── Case A: numbered filter leaf  Parent::Day::Slot::N ──────────────
        # e.g. Mining2::day3::06am-07am::1
        if (deck.get("dyn", 0)
                and len(parts) >= 4
                and _SLOT_RE.match(parts[-2])
                and _NUM_RE.match(parts[-1])):
            day_path = "::".join(parts[:-2])   # Mining2::day3
            groups.setdefault(day_path, []).append((d.id, full_name))
            continue

        # ── Case B: timeslot filter leaf (no number level)  Parent::Day::Slot ─
        # e.g. Mining2::day3::06am-07am  (older/manual creation)
        if (deck.get("dyn", 0)
                and len(parts) >= 3
                and _SLOT_RE.match(parts[-1])):
            day_path = "::".join(parts[:-1])   # Mining2::day3
            groups.setdefault(day_path, []).append((d.id, full_name))
            continue

    return groups


# ── Day Picker Dialog ─────────────────────────────────────────────────────────

class DayPickerDialog(QDialog):
    """
    Pick a GTG day group (e.g. Mining2::day3) from a dropdown.
    Shows a preview of every filter deck inside it.
    """
    def __init__(self, title="Target GTG Day", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(540)
        self.setMinimumHeight(420)
        self.day_key = None
        self.targets = []   # filtered leaf decks inside this day

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select a GTG day to target:"))

        self.groups = get_gtg_day_groups()

        self.combo = QComboBox()
        if not self.groups:
            self.combo.addItem("— no GTG day decks found —", None)
        else:
            for key in sorted(self.groups):
                n = len(self.groups[key])
                self.combo.addItem(f"{key}  ({n} filter deck{'s' if n!=1 else ''})", key)

        layout.addWidget(self.combo)

        self.preview = QListWidget()
        self.preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        layout.addWidget(self.preview)

        self.status = QLabel("")
        layout.addWidget(self.status)

        btns = QHBoxLayout()
        self.ok_btn = QPushButton("Confirm")
        self.ok_btn.clicked.connect(self.accept)
        ca = QPushButton("Cancel")
        ca.clicked.connect(self.reject)
        btns.addWidget(self.ok_btn); btns.addWidget(ca)
        layout.addLayout(btns)

        self.combo.currentIndexChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self):
        self.preview.clear()
        self.ok_btn.setEnabled(False)
        key = self.combo.currentData()
        if not key or key not in self.groups:
            self.status.setText("No group selected.")
            return
        def _deck_sort_key(item):
            parts = item[1].split("::")
            # find the timeslot part and numeric leaf
            slot_idx = next((i for i, p in enumerate(parts) if _SLOT_RE.match(p)), None)
            if slot_idx is None:
                return (999, 0, item[1])
            slot = parts[slot_idx]
            num  = int(parts[slot_idx + 1]) if slot_idx + 1 < len(parts) and _NUM_RE.match(parts[slot_idx + 1]) else 0
            # parse start hour from slot e.g. "10am-11am" or "01pm-02pm"
            start_str = slot.split("-")[0]  # "10:00"
            hour24 = int(start_str.split(":")[0])
            return (hour24, num, item[1])
        members = sorted(self.groups[key], key=_deck_sort_key)
        for _, name in members:
            self.preview.addItem(name)
        self.status.setText(f"✅ {len(members)} filtered deck(s) inside  '{key}'")
        self.day_key = key
        self.targets = members
        self.ok_btn.setEnabled(True)

    def accept(self):
        if not self.targets:
            return
        super().accept()


# ── Deck Picker (parent selection when creating) ───────────────────────────────

class DeckPickerDialog(QDialog):
    def __init__(self, title="Select Parent Deck", parent=None, preselect=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.selected_deck = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select a parent deck (or Top Level for none):"))

        self.list = QListWidget()
        self.list.addItem("— Top Level (no parent) —")
        names = sorted([d["name"] for d in mw.col.decks.all() if not d.get("dyn", 0)],
                        key=str.lower)
        for name in names:
            self.list.addItem(name)

        target_row = 0
        if preselect:
            for i in range(1, self.list.count()):
                if self.list.item(i).text() == preselect:
                    target_row = i
                    break
        self.list.setCurrentRow(target_row)
        self.list.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self.list)

        btns = QHBoxLayout()
        ok = QPushButton("Select"); ok.clicked.connect(self.accept)
        ca = QPushButton("Cancel"); ca.clicked.connect(self.reject)
        btns.addWidget(ok); btns.addWidget(ca)
        layout.addLayout(btns)

    def accept(self):
        row = self.list.currentRow()
        self.selected_deck = "" if row == 0 else self.list.currentItem().text()
        super().accept()


# ── Shared dark theme for the newer dialogs ─────────────────────────────────────

_FPD_STYLE = """
QDialog {
    background: #1e1e2e;
}
QLabel#section_label {
    color: #a0a0b8;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1px;
    text-transform: uppercase;
    padding: 0;
    margin-top: 4px;
}
QLabel#count_label {
    font-size: 12px;
    padding: 2px 0;
}
QPushButton.preset {
    background: #2a2a3e;
    color: #c8c8e0;
    border: 1px solid #3a3a54;
    border-radius: 5px;
    padding: 5px 10px;
    font-size: 12px;
}
QPushButton.preset:hover {
    background: #35355a;
    border-color: #6060aa;
    color: #ffffff;
}
QPushButton.preset:pressed {
    background: #4040aa;
    color: #ffffff;
}
QComboBox {
    background: #2a2a3e;
    color: #c8c8e0;
    border: 1px solid #3a3a54;
    border-radius: 5px;
    padding: 5px 8px;
    font-size: 12px;
    selection-background-color: #4040aa;
}
QComboBox:focus {
    border-color: #6060cc;
}
QComboBox QAbstractItemView {
    background: #2a2a3e;
    color: #c8c8e0;
    selection-background-color: #4040aa;
    border: 1px solid #3a3a54;
}
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QLineEdit {
    background: #2a2a3e;
    color: #e0e0f8;
    border: 1px solid #3a3a54;
    border-radius: 5px;
    padding: 7px 10px;
    font-size: 13px;
    font-family: monospace;
    selection-background-color: #4040aa;
}
QLineEdit:focus {
    border-color: #6060cc;
    background: #30304a;
}
QSpinBox {
    background: #2a2a3e;
    color: #e0e0f8;
    border: 1px solid #3a3a54;
    border-radius: 5px;
    padding: 5px 8px;
    font-size: 13px;
}
QSpinBox:focus {
    border-color: #6060cc;
}
QListWidget {
    background: #24243a;
    color: #d0d0e8;
    border: 1px solid #3a3a54;
    border-radius: 5px;
}
QPushButton#load_btn {
    background: #3a3a60;
    color: #c8c8f0;
    border: 1px solid #5050aa;
    border-radius: 5px;
    padding: 5px 14px;
    font-size: 12px;
    min-width: 60px;
}
QPushButton#load_btn:hover {
    background: #4a4a80;
    color: #ffffff;
    border-color: #7070cc;
}
QPushButton#ok_btn {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #5555cc, stop:1 #4040aa);
    color: #ffffff;
    border: none;
    border-radius: 5px;
    padding: 7px 22px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#ok_btn:hover {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #6666dd, stop:1 #5555bb);
}
QPushButton#cancel_btn {
    background: #2a2a3e;
    color: #888899;
    border: 1px solid #3a3a54;
    border-radius: 5px;
    padding: 7px 18px;
    font-size: 13px;
}
QPushButton#cancel_btn:hover {
    background: #35354a;
    color: #aaaacc;
}
QFrame#divider {
    color: #3a3a54;
}
"""


# ── Filter Picker ─────────────────────────────────────────────────────────────

class FilterPickerDialog(QDialog):
    """
    Pick an Anki search string as the card source for GTG deck creation / refill.
    Sets  self.selected_filter  on accept.
    """

    def __init__(self, title="Pick Card Filter", parent=None, default_filter=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(540)
        self.setStyleSheet(_FPD_STYLE)
        self.selected_filter = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        # ── Section: Presets ──────────────────────────────────────────────────
        lbl_presets = QLabel("QUICK PRESETS")
        lbl_presets.setObjectName("section_label")
        root.addWidget(lbl_presets)

        preset_data = [
            ("is:learn",          "is:learn"),
            ("is:due",            "is:due"),
            ("is:new",            "is:new"),
            ("learn + due",       "(is:learn or is:due)"),
            ("is:learn  (deck…)", None),
            ("is:due  (deck…)",   None),
        ]
        _normal_decks = sorted(
            [d["name"] for d in mw.col.decks.all() if not d.get("dyn", 0)],
            key=str.lower
        )

        grid = QGridLayout()
        grid.setSpacing(6)
        for idx, (label, query) in enumerate(preset_data):
            btn = QPushButton(label)
            btn.setProperty("class", "preset")
            btn.setFixedHeight(30)
            if query is not None:
                btn.clicked.connect(lambda _=False, q=query: self.search_edit.setText(q))
            else:
                suffix = "is:learn" if "learn" in label else "is:due"
                btn.clicked.connect(lambda _=False, s=suffix, dl=_normal_decks:
                                    self._pick_deck_preset(s, dl))
            grid.addWidget(btn, idx // 3, idx % 3)
        root.addLayout(grid)

        # ── Divider ───────────────────────────────────────────────────────────
        div = QFrame()
        div.setObjectName("divider")
        div.setFrameShape(QFrame.Shape.HLine)
        div.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(div)

        # ── Section: Deck + state ─────────────────────────────────────────────
        lbl_deck = QLabel("SOURCE DECK")
        lbl_deck.setObjectName("section_label")
        root.addWidget(lbl_deck)

        deck_row = QHBoxLayout()
        deck_row.setSpacing(8)

        self.deck_combo = QComboBox()
        self.deck_combo.setEditable(True)
        self.deck_combo.addItems(_normal_decks)

        self.state_combo = QComboBox()
        self.state_combo.setFixedWidth(160)
        self.state_combo.addItems(["is:learn", "is:due", "is:new", "(is:learn or is:due)"])

        load_btn = QPushButton("Load →")
        load_btn.setObjectName("load_btn")
        load_btn.setFixedHeight(34)
        load_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        load_btn.clicked.connect(self._load_from_deck_combo)

        deck_row.addWidget(self.deck_combo, 1)
        deck_row.addWidget(self.state_combo)
        deck_row.addWidget(load_btn)
        root.addLayout(deck_row)

        # ── Divider ───────────────────────────────────────────────────────────
        div2 = QFrame()
        div2.setObjectName("divider")
        div2.setFrameShape(QFrame.Shape.HLine)
        div2.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(div2)

        # ── Section: Search string ────────────────────────────────────────────
        lbl_search = QLabel("SEARCH STRING")
        lbl_search.setObjectName("section_label")
        root.addWidget(lbl_search)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('deck:"MiningPartIII" is:learn')
        self.search_edit.setFixedHeight(36)
        root.addWidget(self.search_edit)

        self.count_lbl = QLabel("Cards matched: —")
        self.count_lbl.setObjectName("count_label")
        root.addWidget(self.count_lbl)

        self.search_edit.textChanged.connect(self._update_count)

        # ── Buttons ───────────────────────────────────────────────────────────
        root.addSpacing(4)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.setFixedHeight(34)
        cancel_btn.clicked.connect(self.reject)

        ok_btn = QPushButton("Use this filter")
        ok_btn.setObjectName("ok_btn")
        ok_btn.setFixedHeight(34)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)

        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        root.addLayout(btns)

        # Pre-fill from last time
        if default_filter:
            self.search_edit.setText(default_filter)
            self._update_count(default_filter)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _pick_deck_preset(self, suffix, deck_list):
        dlg = QDialog(self)
        dlg.setWindowTitle("Pick deck")
        dlg.setMinimumWidth(360)
        dlg.setStyleSheet(_FPD_STYLE)
        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(14, 12, 14, 12)
        vl.setSpacing(8)
        lbl = QLabel("SOURCE DECK")
        lbl.setObjectName("section_label")
        vl.addWidget(lbl)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(deck_list)
        vl.addWidget(combo)
        hl = QHBoxLayout()
        hl.setSpacing(8)
        hl.addStretch()
        ca = QPushButton("Cancel")
        ca.setObjectName("cancel_btn")
        ca.setFixedHeight(32)
        ca.clicked.connect(dlg.reject)
        ok = QPushButton("Select")
        ok.setObjectName("ok_btn")
        ok.setFixedHeight(32)
        ok.setDefault(True)
        ok.clicked.connect(dlg.accept)
        hl.addWidget(ca)
        hl.addWidget(ok)
        vl.addLayout(hl)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            deck = combo.currentText().strip()
            if deck:
                self.search_edit.setText(f'deck:"{deck}" {suffix}')

    def _load_from_deck_combo(self):
        deck  = self.deck_combo.currentText().strip()
        state = self.state_combo.currentText().strip()
        if deck:
            self.search_edit.setText(f'deck:"{deck}" {state}')

    def _update_count(self, text):
        text = text.strip()
        if not text:
            self.count_lbl.setText("Cards matched: —")
            self.count_lbl.setStyleSheet("color: #666680; font-size: 12px;")
            return
        try:
            n = len(mw.col.find_cards(text))
            if n > 0:
                self.count_lbl.setText(f"✓  {n} cards matched")
                self.count_lbl.setStyleSheet("color: #50cc88; font-size: 12px; font-weight: 600;")
            else:
                self.count_lbl.setText("✗  No cards matched")
                self.count_lbl.setStyleSheet("color: #cc5050; font-size: 12px; font-weight: 600;")
        except Exception:
            self.count_lbl.setText("⚠  Invalid search")
            self.count_lbl.setStyleSheet("color: #cc8830; font-size: 12px;")

    def accept(self):
        q = self.search_edit.text().strip()
        if not q:
            showInfo("Enter a search string first.")
            return
        self.selected_filter = q
        super().accept()


# ── New-course form ──────────────────────────────────────────────────────────

class CourseFormDialog(QDialog):
    """
    Configure a brand-new course: total card pool, pace, and the word used
    for day-deck names ("Day 3/7", "Session 3/7", ...). Whatever naming word
    you type gets remembered as a preset for next time.
    """
    def __init__(self, default_name, default_total, default_hours, default_cph,
                 default_dph, naming_presets, default_naming, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Course")
        self.setMinimumWidth(460)
        self.setStyleSheet(_FPD_STYLE)
        self.result_name = self.result_total = None
        self.result_hours = self.result_cph = self.result_dph = None
        self.result_naming = default_naming or "Day"

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(8)

        def _lbl(text):
            l = QLabel(text)
            l.setObjectName("section_label")
            return l

        root.addWidget(_lbl("COURSE NAME"))
        self.name_edit = QLineEdit(default_name)
        root.addWidget(self.name_edit)

        root.addWidget(_lbl("TOTAL CARDS IN COURSE"))
        self.total_spin = QSpinBox()
        self.total_spin.setRange(1, 1_000_000)
        self.total_spin.setValue(max(1, default_total))
        root.addWidget(self.total_spin)

        row = QHBoxLayout()
        col1 = QVBoxLayout()
        col1.addWidget(_lbl("HOURS / DAY"))
        self.hours_spin = QSpinBox()
        self.hours_spin.setRange(1, 24)
        self.hours_spin.setValue(max(1, default_hours))
        col1.addWidget(self.hours_spin)
        col2 = QVBoxLayout()
        col2.addWidget(_lbl("CARDS / HOUR"))
        self.cph_spin = QSpinBox()
        self.cph_spin.setRange(1, 10000)
        self.cph_spin.setValue(max(1, default_cph))
        col2.addWidget(self.cph_spin)
        row.addLayout(col1)
        row.addLayout(col2)
        root.addLayout(row)

        root.addWidget(_lbl("DECKS / HOUR"))
        self.dph_spin = QSpinBox()
        self.dph_spin.setRange(1, 60)
        self.dph_spin.setValue(max(1, default_dph))
        root.addWidget(self.dph_spin)

        div = QFrame()
        div.setObjectName("divider")
        div.setFrameShape(QFrame.Shape.HLine)
        div.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(div)

        root.addWidget(_lbl("DAY NAMING WORD"))
        grid = QGridLayout()
        grid.setSpacing(6)
        presets = naming_presets or ["Day"]
        for idx, word in enumerate(presets):
            btn = QPushButton(word)
            btn.setProperty("class", "preset")
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda _=False, w=word: self.naming_edit.setText(w))
            grid.addWidget(btn, idx // 4, idx % 4)
        root.addLayout(grid)

        self.naming_edit = QLineEdit(default_naming or "Day")
        self.naming_edit.setPlaceholderText("Day / Session / Round / Sprint / your own word…")
        root.addWidget(self.naming_edit)

        self.est_lbl = QLabel("")
        self.est_lbl.setObjectName("count_label")
        self.est_lbl.setStyleSheet("color: #50cc88; font-weight: 600;")
        root.addWidget(self.est_lbl)
        for w in (self.total_spin, self.hours_spin, self.cph_spin):
            w.valueChanged.connect(self._update_estimate)
        self._update_estimate()

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("Create Course")
        ok_btn.setObjectName("ok_btn")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        root.addLayout(btns)

    def _update_estimate(self):
        total   = self.total_spin.value()
        per_day = self.hours_spin.value() * self.cph_spin.value()
        days    = _ceil_div(total, per_day)
        self.est_lbl.setText(f"≈ {days} day(s) at this pace  ({per_day} cards/day)")

    def accept(self):
        name = self.name_edit.text().strip()
        if not name:
            showInfo("Enter a course name.")
            return
        naming = self.naming_edit.text().strip() or "Day"
        self.result_name   = name
        self.result_total  = self.total_spin.value()
        self.result_hours  = self.hours_spin.value()
        self.result_cph    = self.cph_spin.value()
        self.result_dph    = self.dph_spin.value()
        self.result_naming = naming
        super().accept()


# ── Day / Course picker (replaces the old plain "day name" prompt) ─────────────

class CourseDayDialog(QDialog):
    """
    Decide the day-container name. Either:
      - continue an existing course (auto "Day N/Total" naming, no typing)
      - start a brand-new course (opens CourseFormDialog, becomes Day 1)
      - go fully manual (free-text day name, old behaviour)
    """
    def __init__(self, cfg, total_available, num_hours, cards_per_hour, decks_per_hour,
                 parent_deck, filter_str, title="Day / Course", parent=None):
        super().__init__(parent)
        self.cfg             = cfg
        self.total_available = total_available
        self.num_hours       = num_hours
        self.cards_per_hour  = cards_per_hour
        self.decks_per_hour  = decks_per_hour
        self.parent_deck     = parent_deck
        self.filter_str      = filter_str

        self.result_day_name  = None
        self.result_course_id = None

        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        self.setStyleSheet(_FPD_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        lbl = QLabel("DAY CONTAINER")
        lbl.setObjectName("section_label")
        root.addWidget(lbl)

        self.combo = QComboBox()
        self.combo.addItem("— Manual name —", "__manual__")
        self.combo.addItem("+ Start new course…", "__new__")
        for cid, c in sorted(self.cfg.get("courses", {}).items(),
                              key=lambda kv: kv[1].get("display_name", "")):
            nd = c.get("current_day", 0) + 1
            td = c.get("total_days", 1)
            self.combo.addItem(
                f"{c.get('display_name', cid)}  —  next: {c.get('naming_word','Day')} {nd}/{td}",
                cid
            )
        root.addWidget(self.combo)

        last = self.cfg.get("last_used", {})
        self.manual_edit = QLineEdit(last.get("day_name", "day1"))
        self.manual_edit.setPlaceholderText("e.g. day1  or  Monday")
        root.addWidget(self.manual_edit)

        self.preview = QLabel("")
        self.preview.setObjectName("count_label")
        self.preview.setWordWrap(True)
        root.addWidget(self.preview)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("Continue")
        ok_btn.setObjectName("ok_btn")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        root.addLayout(btns)

        self.combo.currentIndexChanged.connect(self._on_combo_change)

        # Preselect last-used course if it still exists
        last_course = last.get("course_id", "")
        if last_course and last_course in self.cfg.get("courses", {}):
            idx = self.combo.findData(last_course)
            if idx >= 0:
                self.combo.setCurrentIndex(idx)
        self._on_combo_change()

    def _on_combo_change(self):
        data = self.combo.currentData()
        self.manual_edit.setVisible(data == "__manual__")
        if data == "__manual__":
            self.preview.setText("Type any day-container name.")
        elif data == "__new__":
            self.preview.setText("You'll configure the course after clicking Continue.")
        else:
            c = self.cfg.get("courses", {}).get(data, {})
            nd = c.get("current_day", 0) + 1
            td = c.get("total_days", 1)
            self.preview.setText(
                f"Will create:  {c.get('naming_word','Day')} {nd}/{td}   "
                f"({c.get('total_cards','?')} cards over {td} days total)"
            )

    def _suggest_course_name(self):
        base = re.sub(r"[^A-Za-z0-9]+", "", self.parent_deck or "Course") or "Course"
        return f"{base}{self.total_available}"

    def accept(self):
        data = self.combo.currentData()

        if data == "__manual__":
            name = self.manual_edit.text().strip()
            if not name:
                showInfo("Enter a day name.")
                return
            self.result_day_name  = name
            self.result_course_id = None
            super().accept()
            return

        if data == "__new__":
            form = CourseFormDialog(
                default_name=self._suggest_course_name(),
                default_total=self.total_available,
                default_hours=self.num_hours,
                default_cph=self.cards_per_hour,
                default_dph=self.decks_per_hour,
                naming_presets=self.cfg.get("naming_presets", []),
                default_naming=self.cfg.get("last_used", {}).get("naming_word", "Day"),
                parent=self,
            )
            if form.exec() != QDialog.DialogCode.Accepted:
                return   # stay open so they can pick again
            data = create_course(
                self.cfg,
                display_name=form.result_name,
                filter_str=self.filter_str,
                total_cards=form.result_total,
                hours_per_day=form.result_hours,
                cards_per_hour=form.result_cph,
                decks_per_hour=form.result_dph,
                naming_word=form.result_naming,
                parent_deck=self.parent_deck,
            )

        c = self.cfg.get("courses", {}).get(data)
        if not c:
            showInfo("Course not found — pick again.")
            return
        nd = c.get("current_day", 0) + 1
        td = c.get("total_days", 1)
        self.result_day_name  = f"{c.get('naming_word','Day')} {nd}/{td}"
        self.result_course_id = data
        super().accept()


# ── Manage Courses ────────────────────────────────────────────────────────────

class ManageCoursesDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Manage Courses")
        self.setMinimumWidth(540)
        self.setMinimumHeight(360)
        self.setStyleSheet(_FPD_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(8)

        lbl = QLabel("YOUR COURSES")
        lbl.setObjectName("section_label")
        root.addWidget(lbl)

        self.list = QListWidget()
        root.addWidget(self.list)
        self._reload()

        btns = QHBoxLayout()
        reset_btn = QPushButton("Reset day counter")
        reset_btn.setObjectName("load_btn")
        reset_btn.clicked.connect(self._reset_selected)
        delete_btn = QPushButton("Delete course")
        delete_btn.setObjectName("cancel_btn")
        delete_btn.clicked.connect(self._delete_selected)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("ok_btn")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(reset_btn)
        btns.addWidget(delete_btn)
        btns.addStretch()
        btns.addWidget(close_btn)
        root.addLayout(btns)

    def _reload(self):
        self.list.clear()
        courses = self.cfg.get("courses", {})
        if not courses:
            self.list.addItem("— no courses yet —")
            return
        for cid, c in sorted(courses.items(), key=lambda kv: kv[1].get("display_name", "")):
            nd = c.get("current_day", 0)
            td = c.get("total_days", 1)
            item = QListWidgetItem(
                f"{c.get('display_name', cid)}   —   {nd}/{td} day(s) used   —   "
                f"{c.get('total_cards','?')} cards total   —   naming: \"{c.get('naming_word','Day')}\""
            )
            item.setData(Qt.ItemDataRole.UserRole, cid)
            self.list.addItem(item)

    def _selected_cid(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _reset_selected(self):
        cid = self._selected_cid()
        if not cid or cid not in self.cfg.get("courses", {}):
            return
        self.cfg["courses"][cid]["current_day"] = 0
        save_config(self.cfg)
        self._reload()

    def _delete_selected(self):
        cid = self._selected_cid()
        if not cid or cid not in self.cfg.get("courses", {}):
            return
        name = self.cfg["courses"][cid].get("display_name", cid)
        if QMessageBox.question(
            self, "Delete course",
            f"Delete course '{name}'?\n(Existing day decks already created are not touched.)"
        ) != QMessageBox.StandardButton.Yes:
            return
        del self.cfg["courses"][cid]
        if self.cfg.get("last_used", {}).get("course_id") == cid:
            self.cfg["last_used"]["course_id"] = ""
        save_config(self.cfg)
        self._reload()


def manage_courses():
    cfg = load_config()
    dlg = ManageCoursesDialog(cfg, parent=mw)
    dlg.exec()


# ── Quick Create confirmation ───────────────────────────────────────────────────

class QuickCreateDialog(QDialog):
    """One-line summary + a single confirm button — the 'repeat yesterday' path."""
    def __init__(self, filter_str, total_available, start_hour, end_hour,
                 cards_per_hour, decks_per_hour, parent_deck, day_name, course_label, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Quick Create — Repeat Last")
        self.setMinimumWidth(480)
        self.setStyleSheet(_FPD_STYLE)
        self.edit_instead = False

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(8)

        lbl = QLabel("REPEATING LAST SETUP")
        lbl.setObjectName("section_label")
        root.addWidget(lbl)

        parent_disp = parent_deck or "(top level)"
        summary = (
            f"Filter:   {filter_str}\n"
            f"Cards matched:   {total_available}\n"
            f"Time:   {start_hour:02d}:00 – {end_hour:02d}:00\n"
            f"Rate:   {cards_per_hour}/hr across {decks_per_hour} deck(s)/hr\n"
            f"Parent:   {parent_disp}\n"
            f"Day:   {day_name}\n"
            f"Course:   {course_label}"
        )
        body = QLabel(summary)
        body.setStyleSheet("color: #d8d8f0; font-size: 13px; font-family: monospace;")
        body.setWordWrap(True)
        root.addWidget(body)

        note = QLabel("This will overwrite any existing deck with this exact day name.")
        note.setStyleSheet("color: #cc8830; font-size: 11px;")
        note.setWordWrap(True)
        root.addWidget(note)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)
        edit_btn = QPushButton("Edit Instead")
        edit_btn.setObjectName("load_btn")
        edit_btn.clicked.connect(self._edit_instead)
        ok_btn = QPushButton("Create ✓")
        ok_btn.setObjectName("ok_btn")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btns.addWidget(cancel_btn)
        btns.addWidget(edit_btn)
        btns.addWidget(ok_btn)
        root.addLayout(btns)

    def _edit_instead(self):
        self.edit_instead = True
        self.accept()


# ── Shared deck-building routine (used by both Create and Quick Create) ────────

def _execute_creation(total_available, today_count, card_ids, start_hour, end_hour,
                       cards_per_hour, decks_per_hour, parent_deck, day_name):
    num_hours      = end_hour - start_hour
    cards_per_deck = cards_per_hour // decks_per_hour

    day_path = f"{parent_deck}::{day_name}" if parent_deck else day_name

    # Wipe any existing filter decks inside this day path, then remove the container
    _nuke_day(day_path)
    ensure_normal_deck(day_path)

    num_decks = num_hours * decks_per_hour
    created = 0
    errors  = 0
    for i in range(num_decks):
        start = i * cards_per_deck
        end   = min(start + cards_per_deck, today_count)
        batch = card_ids[start:end]
        cid_filter  = " OR ".join(f"cid:{cid}" for cid in batch)
        hour_offset = i // decks_per_hour
        slot        = time_slot_label(hour_offset, start_hour)
        deck_num    = (i % decks_per_hour) + 1

        # Structure: Mining2::day3::06am-07am::1
        slot_path = f"{day_path}::{slot}"
        ensure_normal_deck(slot_path)      # create timeslot container if needed
        dname = f"{slot_path}::{deck_num}"

        try:
            did  = mw.col.decks.new_filtered(dname)
            deck = mw.col.decks.get(did)
            deck["terms"]   = [[cid_filter, cards_per_deck, 0]]
            deck["resched"] = True
            mw.col.decks.save(deck)
            mw.col.sched.rebuild_filtered_deck(did)
            created += 1
        except Exception:
            errors += 1

    mw.reset()

    loc       = f"Under: {day_path}"
    error_msg = f"\nErrors: {errors}" if errors else ""

    showInfo(
        f"Done!\n\n"
        f"Cards scheduled: {today_count} / {total_available} available\n"
        f"Decks created: {created}\n"
        f"{cards_per_deck} cards/deck  ×  {decks_per_hour} decks/hour  =  {cards_per_hour} cards/hour\n"
        f"Time: {start_hour:02d}:00 → {end_hour:02d}:00  ({num_hours}h)\n"
        f"{loc}{error_msg}\n\n"
        f"Delete the day when done — cards return automatically."
    )
    return day_path, created, errors


def _bump_trailing_number(name):
    """'day3' -> 'day4'. Used to auto-advance manual (non-course) day names on repeat."""
    m = re.search(r"(\d+)$", name)
    if not m:
        return f"{name} 2"
    n = int(m.group(1)) + 1
    return name[:m.start()] + str(n)


# ── Create ────────────────────────────────────────────────────────────────────

def create_gtg_decks():
    cfg  = load_config()
    last = cfg.get("last_used", {})

    fpick = FilterPickerDialog(
        title="GTG Deck Creator — Pick Filter", parent=mw,
        default_filter=last.get("filter", "")
    )
    if fpick.exec() != QDialog.DialogCode.Accepted or not fpick.selected_filter:
        return
    filter_str = fpick.selected_filter

    card_ids = mw.col.find_cards(filter_str)
    total_available = len(card_ids)
    if not total_available:
        showInfo("No cards found matching that filter.\nCheck your search.")
        return

    card_ids = list(card_ids)

    start_hour, ok = QInputDialog.getInt(
        mw, "GTG Deck Creator",
        f"Cards available: {total_available}\n\nStart hour? (24h)  e.g. 10 = 10:00",
        value=last.get("start_hour", 10), min=0, max=23
    )
    if not ok:
        return

    end_hour, ok = QInputDialog.getInt(
        mw, "GTG Deck Creator",
        "End hour? (24h)  e.g. 22 = 22:00",
        value=max(start_hour + 1, last.get("end_hour", 22)), min=start_hour + 1, max=24
    )
    if not ok:
        return

    cards_per_hour, ok = QInputDialog.getInt(
        mw, "GTG Deck Creator",
        "Cards per hour?",
        value=min(last.get("cards_per_hour", 45), total_available), min=1, max=total_available
    )
    if not ok:
        return

    decks_per_hour, ok = QInputDialog.getInt(
        mw, "GTG Deck Creator",
        "Decks per hour?  (cards split evenly across them)",
        value=last.get("decks_per_hour", 3), min=1, max=60
    )
    if not ok:
        return

    num_hours      = end_hour - start_hour
    cards_per_deck = cards_per_hour // decks_per_hour
    if cards_per_deck < 1:
        showInfo("Cards per hour must be >= decks per hour.")
        return
    today_count = min(num_hours * cards_per_hour, total_available)
    card_ids    = card_ids[:today_count]

    # Parent deck picker  (e.g. "Mining2")
    picker = DeckPickerDialog(parent=mw, preselect=last.get("parent_deck", ""))
    if picker.exec() != QDialog.DialogCode.Accepted:
        return
    parent_deck = picker.selected_deck   # "" means top level

    # Day / course picker  (replaces the old free-text "day name" prompt)
    cday = CourseDayDialog(
        cfg, total_available=total_available, num_hours=num_hours,
        cards_per_hour=cards_per_hour, decks_per_hour=decks_per_hour,
        parent_deck=parent_deck, filter_str=filter_str, parent=mw
    )
    if cday.exec() != QDialog.DialogCode.Accepted or not cday.result_day_name:
        return
    day_name  = cday.result_day_name
    course_id = cday.result_course_id

    _execute_creation(
        total_available, today_count, card_ids, start_hour, end_hour,
        cards_per_hour, decks_per_hour, parent_deck, day_name
    )

    # ── Persist everything for next time ────────────────────────────────────
    cfg = load_config()
    naming_word = last.get("naming_word", "Day")
    if course_id and course_id in cfg.get("courses", {}):
        naming_word = cfg["courses"][course_id].get("naming_word", naming_word)
        cfg["courses"][course_id]["current_day"] = cfg["courses"][course_id].get("current_day", 0) + 1

    cfg["last_used"] = {
        "filter":         filter_str,
        "start_hour":     start_hour,
        "end_hour":       end_hour,
        "cards_per_hour": cards_per_hour,
        "decks_per_hour": decks_per_hour,
        "parent_deck":    parent_deck,
        "day_name":       day_name,
        "course_id":      course_id or "",
        "naming_word":    naming_word,
    }
    save_config(cfg)


# ── Quick Create (repeat last config, one click) ────────────────────────────────

def repeat_last_gtg():
    cfg  = load_config()
    last = cfg.get("last_used", {})
    filter_str = last.get("filter", "")

    if not filter_str:
        showInfo(
            "No previous configuration found yet.\n"
            "Run 'Create GTG Filtered Decks' once first — Quick Create repeats it from then on."
        )
        return

    card_ids = list(mw.col.find_cards(filter_str))
    total_available = len(card_ids)
    if not total_available:
        showInfo(f"No cards currently match your last filter:\n{filter_str}")
        return

    start_hour     = last.get("start_hour", 10)
    end_hour       = last.get("end_hour", 22)
    cards_per_hour = min(last.get("cards_per_hour", 45), total_available)
    decks_per_hour = last.get("decks_per_hour", 3)
    parent_deck    = last.get("parent_deck", "")
    course_id      = last.get("course_id", "")

    num_hours      = end_hour - start_hour
    cards_per_deck = cards_per_hour // decks_per_hour
    if num_hours <= 0 or cards_per_deck < 1:
        showInfo("Last configuration looks invalid — run the full creator once to reset it.")
        return

    today_count = min(num_hours * cards_per_hour, total_available)
    card_ids    = card_ids[:today_count]

    # Work out today's day name
    courses = cfg.get("courses", {})
    if course_id and course_id in courses:
        c  = courses[course_id]
        nd = c.get("current_day", 0) + 1
        td = c.get("total_days", 1)
        day_name     = f"{c.get('naming_word', 'Day')} {nd}/{td}"
        course_label = f"{c.get('display_name', course_id)}  ({nd}/{td} days)"
    else:
        day_name     = _bump_trailing_number(last.get("day_name", "day1"))
        course_label = "— none (manual name, auto-advanced) —"

    dlg = QuickCreateDialog(
        filter_str=filter_str, total_available=total_available,
        start_hour=start_hour, end_hour=end_hour,
        cards_per_hour=cards_per_hour, decks_per_hour=decks_per_hour,
        parent_deck=parent_deck, day_name=day_name, course_label=course_label,
        parent=mw
    )
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    if dlg.edit_instead:
        create_gtg_decks()
        return

    _execute_creation(
        total_available, today_count, card_ids, start_hour, end_hour,
        cards_per_hour, decks_per_hour, parent_deck, day_name
    )

    cfg = load_config()
    cfg["last_used"]["day_name"] = day_name
    if course_id and course_id in cfg.get("courses", {}):
        cfg["courses"][course_id]["current_day"] = cfg["courses"][course_id].get("current_day", 0) + 1
    save_config(cfg)


def _nuke_day(day_path):
    """Empty + delete all filter decks under day_path, then delete day_path itself."""
    prefix = day_path + "::"
    for d in mw.col.decks.all_names_and_ids():
        fname = _full_name(d.id)
        if fname != day_path and not fname.startswith(prefix):
            continue
        try:
            deck = mw.col.decks.get(d.id)
            if deck and deck.get("dyn", 0):
                empty_deck(d.id)
            mw.col.decks.remove([d.id])
        except Exception:
            pass


# ── Empty only ────────────────────────────────────────────────────────────────

def empty_gtg_decks():
    dlg = DayPickerDialog(title="Empty GTG Day", parent=mw)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return

    cards_returned = 0
    emptied = 0
    failed  = []
    for did, name in dlg.targets:
        try:
            n = empty_deck(did)
            cards_returned += n
            emptied += 1
        except Exception:
            failed.append(name)

    mw.reset()
    fail_msg = ("\n⚠️ Failed:\n" + "\n".join(failed)) if failed else ""
    showInfo(
        f"Empty complete.\n\n"
        f"Decks emptied: {emptied}\n"
        f"Cards returned: {cards_returned}"
        f"{fail_msg}"
    )


# ── Delete only (no empty — cards were already returned or you don't care) ────

def delete_gtg_decks_only():
    dlg = DayPickerDialog(title="Delete GTG Day (no empty)", parent=mw)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return

    day_key = dlg.day_key
    _nuke_day(day_key)
    mw.reset()
    showInfo(f"Deleted day:\n{day_key}\n\n(Cards were returned automatically.)")


# ── Empty + Delete ────────────────────────────────────────────────────────────

def empty_and_delete_gtg_decks():
    dlg = DayPickerDialog(title="Empty + Delete GTG Day", parent=mw)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return

    day_key        = dlg.day_key
    cards_returned = 0
    for did, _ in dlg.targets:
        cards_returned += empty_deck(did)

    _nuke_day(day_key)
    mw.reset()
    showInfo(
        f"Done.\n\n"
        f"Cards returned: {cards_returned}\n"
        f"Day deleted: {day_key}"
    )



# ── Refill ────────────────────────────────────────────────────────────────────

def refill_gtg_day():
    dlg = DayPickerDialog(title="Refill GTG Day", parent=mw)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return

    targets = dlg.targets   # [(did, full_name), ...] — existing filter decks in order

    fpick = FilterPickerDialog(title="Refill GTG Day — Pick Filter", parent=mw)
    if fpick.exec() != QDialog.DialogCode.Accepted or not fpick.selected_filter:
        return
    filter_str = fpick.selected_filter

    # Sort targets first
    def _sort_key(item):
        parts = item[1].split("::")
        slot_idx = next((i for i, p in enumerate(parts) if _SLOT_RE.match(p)), None)
        if slot_idx is None:
            return (999, 0)
        num = int(parts[slot_idx + 1]) if slot_idx + 1 < len(parts) and _NUM_RE.match(parts[slot_idx + 1]) else 0
        hour24 = int(parts[slot_idx].split("-")[0].split(":")[0])
        return (hour24, num)
    targets = sorted(targets, key=_sort_key)

    # Empty ALL decks first so their cards return to source before we query
    for did, _ in targets:
        empty_deck(did)
    card_ids = list(mw.col.find_cards(filter_str))
    if not card_ids:
        showInfo("No cards found matching that filter.")
        return

    cards_per_deck, ok = QInputDialog.getInt(
        mw, "Refill GTG Day",
        f"Cards available: {len(card_ids)}\n\nCards per deck?",
        value=15, min=1, max=len(card_ids)
    )
    if not ok:
        return

    refilled = 0
    errors   = 0
    for i, (did, name) in enumerate(targets):
        start = i * cards_per_deck
        if start >= len(card_ids):
            break
        batch = card_ids[start : start + cards_per_deck]
        cid_filter = " OR ".join(f"cid:{cid}" for cid in batch)
        try:
            deck = mw.col.decks.get(did)
            deck["terms"]   = [[cid_filter, cards_per_deck, 0]]
            deck["resched"] = True
            mw.col.decks.save(deck)
            mw.col.sched.rebuild_filtered_deck(did)
            refilled += 1
        except Exception:
            errors += 1

    mw.reset()
    err_msg = f"\nErrors: {errors}" if errors else ""
    showInfo(
        f"Refill complete.\n\n"
        f"Decks refilled: {refilled}\n"
        f"Cards per deck: {cards_per_deck}"
        f"{err_msg}"
    )

# ── Menu ──────────────────────────────────────────────────────────────────────

def setup_menu():
    menu = mw.form.menuTools
    gtg  = QMenu("GTG Deck Creator", mw)
    menu.addMenu(gtg)

    actions = [
        ("Create GTG Filtered Decks",       create_gtg_decks),
        ("⚡ Quick Create (Repeat Last)",    repeat_last_gtg),
        ("Refill GTG Day",                  refill_gtg_day),
        (None, None),
        ("Manage Courses",                  manage_courses),
        (None, None),
        ("Empty GTG Day",                   empty_gtg_decks),
        ("Delete GTG Day",                  delete_gtg_decks_only),
        ("Empty + Delete GTG Day",          empty_and_delete_gtg_decks),
    ]
    for label, fn in actions:
        if label is None:
            gtg.addSeparator()
        else:
            a = QAction(label, mw)
            a.triggered.connect(fn)
            if "Quick Create" in label:
                a.setShortcut("Ctrl+Alt+Q")
            gtg.addAction(a)


setup_menu()