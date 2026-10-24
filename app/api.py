"""HTTP routes under /api/v1 used by the browser frontend."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse

from . import models as M
from .db import now_iso
from .services import analysis, anonymization as anon, core, readable
from .services.export import EXPORT_TYPES, run_export
from .services.importer import ImportError_, decode_upload, parse_transcript, ALLOWED_ROLES

log = logging.getLogger("qda.api")
router = APIRouter(prefix="/api/v1")

ERR = {422: {"model": M.Message}, 404: {"model": M.Message}}


def _proj(request: Request):
    return request.app.state.project


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, core.NotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, (core.ValidationError, ImportError_, ValueError)):
        return HTTPException(422, str(exc))
    raise exc


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------

@router.get("/revision", response_model=M.Revision, tags=["project"])
def get_revision(request: Request):
    return {"revision": _proj(request).db.revision()}


@router.get("/settings", tags=["project"])
def get_settings(request: Request) -> dict[str, Any]:
    p = _proj(request)
    settings = {r["key"]: r["value"] for r in p.db.query("SELECT key, value FROM settings")}
    return {
        "settings": settings,
        "project_file": str(p.db.path),
        "backups_dir": str(p.backups.backups_dir),
        "exports_dir": str(p.exports_dir),
        "last_backup": p.backups.last_backup.isoformat() if p.backups.last_backup else None,
        "backup_error": p.backups.last_error,
        "shortcuts": p.shortcuts(),
        "role_categories": ALLOWED_ROLES,
        "agencies": core.AGENCY_ORDER,
        "agency_meanings": core.AGENCY_MEANINGS,
        "version": p.version,
    }


@router.put("/settings/{key}", tags=["project"], responses=ERR)
def put_setting(key: str, body: M.SettingValue, request: Request) -> dict[str, Any]:
    p = _proj(request)
    allowed = {"pilot_phase", "backup_interval_min", "secondary_backup_dir", "last_used_code_id",
               "last_full_export", "ui_state"}
    if key not in allowed:
        raise HTTPException(422, f"Unknown setting {key!r}. Allowed: {', '.join(sorted(allowed))}.")
    value = body.value
    if key == "pilot_phase":
        value = "true" if str(value).lower() in ("true", "1", "yes") else "false"
    if key == "backup_interval_min":
        try:
            value = str(max(1, int(value)))
        except (TypeError, ValueError):
            raise HTTPException(422, "backup_interval_min must be an integer number of minutes.")
    if key == "secondary_backup_dir" and value:
        try:
            Path(str(value)).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(422, f"Cannot create secondary backup folder: {exc}")
    p.db.set_setting(key, "" if value is None else str(value))
    return {"key": key, "value": p.db.setting(key)}


@router.get("/dashboard", tags=["project"])
def dashboard(request: Request) -> dict[str, Any]:
    p = _proj(request)
    db = p.db
    interviews = core.list_interviews(db)
    codes = core.code_list(db)
    incomplete = [{"id": c["id"], "code_key": c["code_key"], "name": c["name"],
                   "missing": [f for f in ("definition", "anchor_example") if not c[f].strip()]}
                  for c in codes if not c["definition"].strip() or not c["anchor_example"].strip()]
    total_turns = sum(i["respondent_turns"] for i in interviews) or 0
    done = sum(1 for i in interviews if i["status"] == "done")
    last_export = db.setting("last_full_export", "")
    reminder = True
    if last_export:
        try:
            import datetime as _dt
            reminder = (_dt.datetime.now() - _dt.datetime.fromisoformat(last_export)).days >= 7
        except ValueError:
            reminder = True
    backups = p.backups.list_backups()
    return {
        "interviews": interviews,
        "progress": {"interviews": len(interviews), "done": done,
                     "in_progress": sum(1 for i in interviews if i["status"] == "in-progress"),
                     "segments": sum(i["segment_count"] for i in interviews), "respondent_turns": total_turns},
        "codebook": {"codes": len(codes), "incomplete": incomplete},
        "backup": {"last_backup": backups[0]["created_at"] if backups else None, "count": len(backups),
                   "folder": str(p.backups.backups_dir), "error": p.backups.last_error,
                   "secondary": db.setting("secondary_backup_dir", "")},
        "weekly_reminder": reminder, "last_full_export": last_export or None,
        "pilot_phase": db.setting("pilot_phase", "false") == "true",
        "undo": db.undo_state(),
    }


@router.post("/backup", tags=["project"])
def backup_now(request: Request) -> dict[str, Any]:
    p = _proj(request)
    target = p.backup("manual")
    return {"filename": target.name, "path": str(target), "backups": p.backups.list_backups()}


@router.get("/backups", tags=["project"])
def list_backups(request: Request) -> list[dict[str, Any]]:
    return _proj(request).backups.list_backups()


@router.post("/restore", tags=["project"], responses=ERR)
def restore(body: M.RestoreRequest, request: Request) -> dict[str, Any]:
    p = _proj(request)
    try:
        return p.restore(body.filename)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc))


# ---------------------------------------------------------------------------
# interviews
# ---------------------------------------------------------------------------

@router.get("/interviews", tags=["interviews"])
def list_interviews(request: Request) -> list[dict[str, Any]]:
    return core.list_interviews(_proj(request).db)


@router.get("/interviews/{interview_id}", tags=["interviews"], responses=ERR)
def get_interview(interview_id: str, request: Request, pass_id: int | None = None) -> dict[str, Any]:
    try:
        return core.get_interview(_proj(request).db, interview_id, pass_id)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.post("/interviews/import", tags=["interviews"], responses=ERR,
             description="Multipart upload of one .md transcript. dry_run=true returns the preview without writing. "
                         "mode='reimport' replaces the text of an existing interview and re-attaches codings by "
                         "paragraph index + text match.")
async def import_interview(request: Request, file: UploadFile = File(...), dry_run: bool = Form(False),
                           mode: str = Form("new"), anonymize: bool = Form(False)) -> dict[str, Any]:
    p = _proj(request)
    data = await file.read()
    try:
        text = decode_upload(data, file.filename or "")
        parsed = parse_transcript(text, file.filename or "")
        report = core.import_transcript(p.db, parsed, file.filename or "", mode=mode, anonymize=anonymize,
                                        dry_run=dry_run)
    except (ImportError_, core.ValidationError) as exc:
        raise HTTPException(422, str(exc))
    if not dry_run:
        p.backup("after import")
    return report


@router.patch("/interviews/{interview_id}", tags=["interviews"], responses=ERR)
def patch_interview(interview_id: str, body: M.InterviewPatch, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.patch_interview(tx, interview_id, body.status, body.notes, body.role_label, body.role_category)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.post("/interviews/{interview_id}/locate", tags=["interviews"], response_model=list[M.LocateHit], responses=ERR)
def locate(interview_id: str, body: M.LocateRequest, request: Request):
    db = _proj(request).db
    if not db.one("SELECT 1 FROM interviews WHERE id=?", (interview_id,)):
        raise HTTPException(404, f"Interview {interview_id} not found.")
    try:
        return core.locate(db, interview_id, body.text, body.paragraph_index, body.para_no, body.timestamp)
    except core.ValidationError as exc:
        raise _handle(exc)


@router.post("/interviews/{interview_id}/passes", tags=["reliability"], responses=ERR)
def start_pass(interview_id: str, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.start_recode_pass(tx, interview_id)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.post("/passes/{pass_id}/finish", tags=["reliability"], responses=ERR)
def finish_pass(pass_id: int, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.finish_pass(tx, pass_id)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.get("/reliability/{interview_id}", tags=["reliability"], responses=ERR)
def reliability(interview_id: str, request: Request, pass_id: int | None = None) -> dict[str, Any]:
    try:
        return analysis.reliability_report(_proj(request).db, interview_id, pass_id)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.put("/reliability/{pass_id}/notes/{paragraph_id}", tags=["reliability"], responses=ERR)
def reliability_note(pass_id: int, paragraph_id: int, body: M.ReliabilityNote, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.save_reliability_note(tx, pass_id, paragraph_id, body.note)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


# ---------------------------------------------------------------------------
# codes
# ---------------------------------------------------------------------------

@router.get("/codes", tags=["codes"])
def get_codes(request: Request, include_retired: bool = False) -> dict[str, Any]:
    db = _proj(request).db
    return {"tree": core.code_tree(db, include_retired), "flat": core.code_list(db, include_retired),
            "last_used_code_id": int(db.setting("last_used_code_id") or 0) or None}


@router.post("/codes", tags=["codes"], responses=ERR)
def create_code(body: M.CodeCreate, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.create_code(tx, body.name, body.definition, body.coding_rule, body.parent_id, body.code_key,
                                    body.type, body.anchor_example, body.color, body.reason)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.patch("/codes/{code_id}", tags=["codes"], responses=ERR)
def patch_code(code_id: int, body: M.CodePatch, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    changes = body.model_dump(exclude_unset=True)
    changes.pop("reason", None)
    reparent = changes.pop("reparent", False)
    if "parent_id" in changes and not reparent and changes["parent_id"] is None:
        changes.pop("parent_id")
    try:
        with db.write() as tx:
            return core.patch_code(tx, code_id, changes, body.reason)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.post("/codes/{code_id}/retire", tags=["codes"], responses=ERR)
def retire_code(code_id: int, body: M.RetireRequest, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.retire_code(tx, code_id, body.reason, body.reassign)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.post("/codes/merge", tags=["codes"], responses=ERR)
def merge_codes(body: M.MergeRequest, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.merge_codes(tx, body.source_id, body.target_id, body.reason)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.get("/codes/{code_id}/segments", tags=["codes"], responses=ERR)
def code_segments(code_id: int, request: Request, include_sub: bool = False) -> list[dict[str, Any]]:
    db = _proj(request).db
    if not db.one("SELECT 1 FROM codes WHERE id=?", (code_id,)):
        raise HTTPException(404, f"Code {code_id} not found.")
    return core.code_segments(db, code_id, include_sub)


@router.get("/changelog", tags=["codes"])
def get_changelog(request: Request, code_id: int | None = None) -> list[dict[str, Any]]:
    return core.changelog(_proj(request).db, code_id)


# ---------------------------------------------------------------------------
# segments
# ---------------------------------------------------------------------------

@router.post("/segments", tags=["segments"], responses=ERR, status_code=201,
             description="Creates a coded segment. Rows carry 'segment_ref', the paragraph 'timestamp', "
                         "'en_original' and 'quotation_block' (none | partial | full, derived from [NO-QUOTE] "
                         "ranges). quote_candidate=true is refused with 422 when quotation_block != none.")
def create_segment(body: M.SegmentCreate, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.create_segment(tx, body.paragraph_id, body.start_char, body.end_char,
                                       [c.model_dump() for c in body.codes], body.comment, body.quote_candidate,
                                       body.coding_pass_id)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.post("/segments/batch", tags=["segments"], responses=ERR,
             description="Atomic list of coding operations (all-or-nothing, one undo entry).")
def batch(body: M.BatchRequest, request: Request) -> list[dict[str, Any]]:
    db = _proj(request).db
    ops = [op.model_dump(exclude_unset=True) for op in body.operations]
    for op in ops:
        if op.get("codes") is not None:
            op["codes"] = [dict(c) for c in op["codes"]]
    try:
        with db.write() as tx:
            return core.batch(tx, ops)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.get("/segments/{segment_id}", tags=["segments"], responses=ERR)
def get_segment(segment_id: int, request: Request) -> dict[str, Any]:
    try:
        return core.get_segment(_proj(request).db, segment_id)
    except core.NotFound as exc:
        raise _handle(exc)


@router.patch("/segments/{segment_id}", tags=["segments"], responses=ERR)
def patch_segment(segment_id: int, body: M.SegmentPatch, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.patch_segment(tx, segment_id, body.model_dump(exclude_unset=True))
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.delete("/segments/{segment_id}", tags=["segments"], responses=ERR)
def delete_segment(segment_id: int, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            core.delete_segment(tx, segment_id)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)
    return {"deleted": segment_id}


@router.post("/segments/{segment_id}/codes", tags=["segments"], responses=ERR)
def add_segment_code(segment_id: int, body: M.CodeAssignment, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.add_segment_code(tx, segment_id, body.code_id, body.valence, body.agency)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.patch("/segments/{segment_id}/codes/{code_id}", tags=["segments"], responses=ERR)
def patch_segment_code(segment_id: int, code_id: int, body: M.SegmentCodePatch, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    data = body.model_dump(exclude_unset=True)
    try:
        with db.write() as tx:
            return core.patch_segment_code(tx, segment_id, code_id, data.get("valence"), data.get("agency"),
                                           "agency" in data)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.delete("/segments/{segment_id}/codes/{code_id}", tags=["segments"], responses=ERR)
def remove_segment_code(segment_id: int, code_id: int, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.remove_segment_code(tx, segment_id, code_id)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


# ---------------------------------------------------------------------------
# interactions / confidential / memos
# ---------------------------------------------------------------------------

@router.get("/interactions", tags=["interactions"])
def list_interactions(request: Request, interview_id: str | None = None, country: str | None = None):
    return core.list_interactions(_proj(request).db, interview_id, country)


@router.post("/interactions", tags=["interactions"], responses=ERR, status_code=201)
def create_interaction(body: M.InteractionCreate, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.create_interaction(tx, body.model_dump())
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.patch("/interactions/{interaction_id}", tags=["interactions"], responses=ERR)
def patch_interaction(interaction_id: int, body: M.InteractionPatch, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.patch_interaction(tx, interaction_id, body.model_dump(exclude_unset=True))
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.delete("/interactions/{interaction_id}", tags=["interactions"], responses=ERR)
def delete_interaction(interaction_id: int, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            core.delete_interaction(tx, interaction_id)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)
    return {"deleted": interaction_id}


@router.post("/confidential", tags=["confidential"], responses=ERR, status_code=201)
def create_confidential(body: M.ConfidentialCreate, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.create_confidential(tx, body.paragraph_id, body.start_char, body.end_char,
                                            body.delete_overlapping)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.delete("/confidential/{range_id}", tags=["confidential"], responses=ERR)
def delete_confidential(range_id: int, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            core.delete_confidential(tx, range_id)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)
    return {"deleted": range_id}


@router.get("/memos", tags=["memos"])
def list_memos(request: Request, scope: str | None = None, ref_id: str | None = None):
    return core.list_memos(_proj(request).db, scope, ref_id)


@router.post("/memos", tags=["memos"], responses=ERR, status_code=201)
def create_memo(body: M.MemoCreate, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.create_memo(tx, body.scope, body.ref_id, body.text)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.put("/memos/{memo_id}", tags=["memos"], responses=ERR)
def update_memo(memo_id: int, body: M.MemoUpdate, request: Request) -> dict[str, Any]:
    db = _proj(request).db
    try:
        with db.write() as tx:
            return core.update_memo(tx, memo_id, body.text)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.get("/memos/{memo_id}/versions", tags=["memos"], responses=ERR)
def get_memo_versions(memo_id: int, request: Request) -> list[dict[str, Any]]:
    try:
        return core.memo_versions(_proj(request).db, memo_id)
    except core.NotFound as exc:
        raise _handle(exc)


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------

@router.get("/undo/state", tags=["undo"])
def undo_state(request: Request) -> dict[str, Any]:
    return _proj(request).db.undo_state()


@router.post("/undo", tags=["undo"])
def undo(request: Request) -> dict[str, Any]:
    db = _proj(request).db
    r = db.undo()
    return {"ok": r is not None, **(r or {}), **db.undo_state()}


@router.post("/redo", tags=["undo"])
def redo(request: Request) -> dict[str, Any]:
    db = _proj(request).db
    r = db.redo()
    return {"ok": r is not None, **(r or {}), **db.undo_state()}


# ---------------------------------------------------------------------------
# search / retrieval / analysis
# ---------------------------------------------------------------------------

@router.get("/search", tags=["analysis"], responses=ERR)
def search(request: Request, q: str = Query(..., min_length=1), country: str | None = None,
           speaker: str | None = None, limit: int = 500):
    try:
        return analysis.search(_proj(request).db, q, country, speaker, limit)
    except core.ValidationError as exc:
        raise _handle(exc)


@router.get("/retrieve", tags=["analysis"], responses=ERR)
def retrieve(request: Request, code_id: int | None = None, include_sub: bool = False, country: str | None = None,
             role_category: str | None = None, pilot: bool | None = None, valence: int | None = None,
             agency: str | None = None, quote_candidate: bool | None = None, interview_id: str | None = None,
             q: str | None = None):
    try:
        return analysis.retrieve(_proj(request).db, code_id, include_sub, country, role_category, pilot, valence,
                                 agency, quote_candidate, interview_id, q)
    except core.ValidationError as exc:
        raise _handle(exc)


@router.get("/analysis/{kind}", tags=["analysis"], responses=ERR,
            description="kind = matrix | crosstab | cooccurrence | interactions | agency | barriers | learning | crosscase")
def analysis_view(kind: str, request: Request, country: str | None = None, role_category: str | None = None,
                  pilot: bool | None = None, with_children: bool = False, attribute: str = "country",
                  code_id: int | None = None, include_sub: bool = True) -> dict[str, Any]:
    db = _proj(request).db
    try:
        if kind == "matrix":
            return analysis.matrix(db, country, role_category, pilot, with_children)
        if kind == "crosstab":
            return analysis.crosstab(db, attribute, pilot)
        if kind == "cooccurrence":
            return analysis.cooccurrence(db, country)
        if kind == "interactions":
            return analysis.interactions_view(db)
        if kind == "agency":
            return analysis.agency_matrix(db)
        if kind == "barriers":
            return analysis.barriers_view(db)
        if kind == "learning":
            return analysis.learning_view(db)
        if kind == "crosscase":
            if code_id is None:
                raise HTTPException(422, "code_id is required for the cross-case view.")
            return analysis.crosscase(db, code_id, include_sub)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)
    raise HTTPException(404, f"Unknown analysis view {kind!r}.")


# ---------------------------------------------------------------------------
# anonymization
# ---------------------------------------------------------------------------

@router.get("/anonymization", tags=["anonymization"])
def get_anonymization(request: Request) -> list[dict[str, Any]]:
    return anon.list_mapping(_proj(request).db)


@router.post("/anonymization", tags=["anonymization"], responses=ERR, status_code=201)
def add_anonymization(body: M.AnonymizationCreate, request: Request) -> dict[str, Any]:
    try:
        return anon.add_mapping(_proj(request).db, body.term, body.replacement)
    except core.ValidationError as exc:
        raise _handle(exc)


@router.delete("/anonymization/{mapping_id}", tags=["anonymization"], responses=ERR)
def delete_anonymization(mapping_id: int, request: Request) -> dict[str, Any]:
    try:
        anon.delete_mapping(_proj(request).db, mapping_id)
    except core.NotFound as exc:
        raise _handle(exc)
    return {"deleted": mapping_id}


@router.post("/anonymization/scan", tags=["anonymization"])
def scan_anonymization(request: Request, interview_id: str | None = None) -> list[dict[str, Any]]:
    return anon.scan(_proj(request).db, interview_id)


@router.post("/anonymization/apply", tags=["anonymization"], responses=ERR,
             description="Replaces the selected hits in the stored transcript text. A backup is created first; "
                         "segment offsets are shifted; the undo history is cleared.")
def apply_anonymization(body: M.AnonymizationApply, request: Request) -> dict[str, Any]:
    p = _proj(request)
    p.backup("before anonymization")
    return anon.apply(p.db, body.hits, body.interview_id)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@router.get("/export/types", tags=["export"])
def export_types() -> list[str]:
    return EXPORT_TYPES


@router.get("/exports", tags=["export"])
def list_exports(request: Request) -> list[dict[str, Any]]:
    p = _proj(request)
    out = []
    if p.exports_dir.exists():
        for d in sorted(p.exports_dir.iterdir(), reverse=True):
            if d.is_dir():
                files = [f.relative_to(d).as_posix() for f in d.rglob("*") if f.is_file()]
                out.append({"name": d.name, "path": str(d), "files": sorted(files),
                            "has_warnings": (d / "WARNINGS.md").exists()})
    return out[:50]


@router.post("/export", tags=["export"], responses=ERR)
def export(body: M.ExportRequest, request: Request) -> dict[str, Any]:
    p = _proj(request)
    try:
        return run_export(p.db, p.exports_dir, body.types, body.quote_segment_ids)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# ---------------------------------------------------------------------------
# readable interview reports (one HTML page per interview, main coding pass)
# ---------------------------------------------------------------------------

@router.get("/readable", tags=["export"], response_class=HTMLResponse,
            description="Overview page of the readable interview reports (rendered live, nothing is written).")
def readable_index(request: Request) -> HTMLResponse:
    db = _proj(request).db
    return HTMLResponse(readable.render_index(db, link=lambda iid: f"/api/v1/readable/{iid}"))


@router.get("/readable/{interview_id}", tags=["export"], response_class=HTMLResponse, responses=ERR,
            description="Readable report of one interview: transcript with highlighted codings and a coding column. "
                        "Main coding pass only; the interview memo follows the transcript; comments and confidential text are never included.")
def readable_interview(interview_id: str, request: Request) -> HTMLResponse:
    try:
        return HTMLResponse(readable.render_interview(_proj(request).db, interview_id, index_href="/api/v1/readable"))
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)


@router.post("/readable/export", tags=["export"], responses=ERR,
             description="Writes <ID>.html per selected interview plus index.html to "
                         "exports/<timestamp>/readable_reports/ (null = all interviews).")
def readable_export(body: M.ReadableExportRequest, request: Request) -> dict[str, Any]:
    p = _proj(request)
    try:
        return readable.export_readable(p.db, p.exports_dir, body.interview_ids)
    except (core.NotFound, core.ValidationError) as exc:
        raise _handle(exc)
