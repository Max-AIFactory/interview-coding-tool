"""Preloaded deductive category system."""
from __future__ import annotations

from .db import Database, now_iso

# (code_key, name, parent_key, type, definition, coding_rule, anchor_example, color)
SEED_CODES: list[tuple] = [
    # --- Bergek et al. (2008) functions, own F1/F1b split ---------------
    ("F1", "Knowledge Development", None, "function", "", "", "", "#1f77b4"),
    ("F1b", "Knowledge Diffusion", None, "function", "", "", "", "#17becf"),
    ("F2", "Influence on the Direction of Search", None, "function", "", "", "", "#9467bd"),
    ("F3", "Entrepreneurial Experimentation", None, "function", "", "", "", "#ff7f0e"),
    ("F4", "Market Formation", None, "function", "", "", "", "#2ca02c"),
    ("F5", "Legitimation", None, "function", "", "", "", "#d62728"),
    ("F6", "Resource Mobilization", None, "function", "", "", "", "#8c564b"),
    ("F7", "Development of Positive Externalities (incl. collective interest representation)", None, "function",
     "", "", "", "#e377c2"),
    # --- supporting deductive categories --------------------------------
    ("STR", "Structural Components", None, "structural", "", "", "", "#7f7f7f"),
    ("STR-A", "Actors", "STR", "structural", "", "", "", "#7f7f7f"),
    ("STR-N", "Networks", "STR", "structural", "", "", "", "#7f7f7f"),
    ("STR-I", "Institutions", "STR", "structural", "", "", "", "#7f7f7f"),
    ("CTX", "Context Structure", None, "context", "", "", "", "#bcbd22"),
    ("CTX-TIS", "Other TIS", "CTX", "context", "", "", "", "#bcbd22"),
    ("CTX-SEC", "Sectoral context", "CTX", "context", "", "", "", "#bcbd22"),
    ("CTX-GEO", "Geographical context", "CTX", "context", "", "", "", "#bcbd22"),
    ("CTX-POL", "Political context", "CTX", "context", "", "", "", "#bcbd22"),
    ("BAR", "Barrier / Blocking Mechanism", None, "crosscutting", "", "", "", "#000000"),
    ("INT", "Function Interaction / Cycle", None, "crosscutting", "", "", "", "#555555"),
    ("ML", "Mutual Learning / Transferability", None, "learning", "", "", "", "#00a86b"),
    ("ROLE", "Role / Background of Interviewee", None, "role", "", "", "", "#aaaaaa"),
    ("EMT", "Emergent Themes", None, "inductive", "", "", "", "#ffbb78"),
]


def seed_codebook(db: Database) -> int:
    """Insert the deductive codebook if the codes table is empty. Returns number inserted."""
    existing = db.scalar("SELECT COUNT(*) FROM codes")
    if existing:
        return 0
    ids: dict[str, int] = {}
    n = 0
    with db.write("seed codebook", record_undo=False) as tx:
        for order, (key, name, parent_key, ctype, definition, rule, anchor, color) in enumerate(SEED_CODES):
            row = tx.insert("codes", {
                "code_key": key, "name": name,
                "parent_id": ids.get(parent_key) if parent_key else None,
                "type": ctype, "definition": definition, "coding_rule": rule,
                "anchor_example": anchor, "color": color, "sort_order": order,
                "active": 1, "created_at": now_iso(), "retired_at": None,
            })
            ids[key] = row["id"]
            n += 1
        for k, v in {
            "pilot_phase": "false",
            "backup_interval_min": "15",
            "secondary_backup_dir": "",
            "last_used_code_id": "",
            "last_full_export": "",
        }.items():
            tx.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    return n


FUNCTION_ORDER = ["F1", "F1b", "F2", "F3", "F4", "F5", "F6", "F7"]
