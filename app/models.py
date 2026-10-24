"""Pydantic request/response models for the routes under /api/v1."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Country = Literal["DE", "CN"]
# Agency value list; null / '' = unset.  Meanings in
# ``app.services.core.AGENCY_MEANINGS`` and ``GET /settings``.
Agency = Literal["GOV-CENTRAL", "GOV-PROV", "REGULATOR", "GRID", "SOE", "MARKET", "ASSOC", "RESEARCH", "MULTI"]
Mechanism = Literal["inducing", "blocking"]
Status = Literal["not-started", "in-progress", "done"]


class Message(BaseModel):
    detail: str = Field(examples=["Selection overlaps a confidential range; confidential text cannot be coded."])


class Revision(BaseModel):
    revision: int = Field(examples=[42])


class SettingValue(BaseModel):
    value: str | int | bool | None = Field(examples=["true"])


class InterviewPatch(BaseModel):
    status: Status | None = Field(default=None, examples=["done"])
    notes: str | None = Field(default=None, examples=["Pilot interview; codebook revised afterwards."])
    role_label: str | None = Field(default=None, description="Short role label shown in the reports; must not be empty.",
                                   examples=["Manager at a provincial grid company's demand-response unit"])
    role_category: str | None = Field(default=None, description="Actor group; one of the allowed role categories "
                                      "(GET /settings → role_categories).", examples=["Grid operator, energy company"])


class LocateRequest(BaseModel):
    text: str = Field(examples=["prequalification takes months"])
    paragraph_index: int | None = Field(default=None, description="0-based index over all paragraphs (I and R).")
    para_no: int | None = Field(default=None, description="Respondent paragraph number ¶n as shown in the UI.")
    timestamp: str | None = Field(default=None, description="Exact match on the paragraph timestamp (hh:mm:ss).",
                                  examples=["00:04:05"])


class LocateHit(BaseModel):
    paragraph_id: int
    paragraph_index: int
    para_no: int | None
    timestamp: str | None = Field(default=None, description="Paragraph timestamp hh:mm:ss (null for old imports).")
    start_char: int
    end_char: int


class CodeAssignment(BaseModel):
    code_id: int = Field(examples=[5])
    valence: Literal[-1, 0, 1] = Field(default=0, examples=[-1])
    agency: Agency | None = Field(
        default=None, examples=["MARKET"],
        description="Which actor type brought about the situation or could change it (Huang & Malkin 2026): "
                    "GOV-CENTRAL = central government / NDRC / NEA / BMWK / legislator; GOV-PROV = province, "
                    "federal state, municipality; REGULATOR = BNetzA and similar; GRID = grid companies, TSO, DSO; "
                    "SOE = state-owned enterprises other than grid; MARKET = private market actors incl. "
                    "aggregators, banks, customers; ASSOC = associations; RESEARCH = academia; MULTI = explicitly "
                    "several actor types jointly (co-steering). null = unset.")


class SegmentCreate(BaseModel):
    paragraph_id: int = Field(examples=[12])
    start_char: int = Field(description="Unicode code-point offset into paragraphs.text (inclusive).", examples=[0])
    end_char: int = Field(description="Unicode code-point offset (exclusive).", examples=[57])
    codes: list[CodeAssignment] = Field(min_length=1)
    comment: str = Field(default="", examples=["(F4- BAR) prequalification blocks market entry; agency REGULATOR."])
    quote_candidate: bool = Field(
        default=False, description="Refused (422) when the segment overlaps a [NO-QUOTE] passage (quotation_block != none).")
    coding_pass_id: int | None = Field(default=None, description="Defaults to the interview's primary pass.")


class SegmentPatch(BaseModel):
    start_char: int | None = None
    end_char: int | None = None
    comment: str | None = None
    quote_candidate: bool | None = None


class SegmentCodePatch(BaseModel):
    valence: Literal[-1, 0, 1] | None = None
    agency: Agency | Literal[""] | None = Field(default=None, examples=["GOV-PROV"],
                                                description="'' or null clears the agency when the field is present.")


class BatchOperation(BaseModel):
    op: Literal["create_segment", "patch_segment", "delete_segment", "add_code", "patch_code", "remove_code"]
    paragraph_id: int | None = None
    segment_id: int | None = None
    start_char: int | None = None
    end_char: int | None = None
    codes: list[CodeAssignment] | None = None
    code_id: int | None = None
    valence: Literal[-1, 0, 1] | None = None
    agency: Agency | Literal[""] | None = None
    comment: str | None = None
    quote_candidate: bool | None = None
    coding_pass_id: int | None = None


class BatchRequest(BaseModel):
    operations: list[BatchOperation] = Field(min_length=1)


class CodeCreate(BaseModel):
    name: str = Field(examples=["Provincial pilot fatigue"])
    definition: str = Field(examples=["Statements that repeated provincial pilots without market follow-up reduce willingness to participate."])
    coding_rule: str = Field(examples=["Code only when the respondent links pilots explicitly to declining participation."])
    parent_id: int | None = Field(default=None, examples=[22])
    code_key: str | None = Field(default=None, examples=["EMT-PILOTFATIGUE"])
    type: Literal["function", "structural", "context", "crosscutting", "learning", "role", "inductive"] | None = None
    anchor_example: str = ""
    color: str | None = Field(default=None, examples=["#ffbb78"])
    reason: str = Field(default="", examples=["Emerged in DE-00 and CN-00"])


class CodePatch(BaseModel):
    name: str | None = None
    definition: str | None = None
    coding_rule: str | None = None
    anchor_example: str | None = None
    color: str | None = None
    parent_id: int | None = Field(default=None, description="Set to move the code; omit to keep.")
    reparent: bool = Field(default=False, description="Must be true when parent_id is meant to change (allows moving to root with parent_id=null).")
    position: int | None = Field(default=None, description="New index among siblings (reorder).")
    reason: str = Field(default="", examples=["Definition sharpened after pilot phase"])


class RetireRequest(BaseModel):
    reason: str = Field(examples=["Not used after pilot phase"])
    reassign: Literal["keep", "parent"] = "keep"


class MergeRequest(BaseModel):
    source_id: int
    target_id: int
    reason: str = Field(examples=["Both codes describe the same prequalification barrier"])


class InteractionCreate(BaseModel):
    from_code_id: int = Field(examples=[5])
    to_code_id: int = Field(examples=[6])
    mechanism: Mechanism = Field(examples=["blocking"])
    country: Country | None = Field(default=None, description="Derived from the segment/interview when omitted.")
    interview_id: str | None = None
    segment_id: int | None = None
    description: str = ""
    condition: str = Field(default="", examples=["if metering rules are not clarified, no market entry"])


class InteractionPatch(BaseModel):
    from_code_id: int | None = None
    to_code_id: int | None = None
    mechanism: Mechanism | None = None
    country: Country | None = None
    interview_id: str | None = None
    segment_id: int | None = None
    description: str | None = None
    condition: str | None = None


class ConfidentialCreate(BaseModel):
    paragraph_id: int
    start_char: int
    end_char: int
    delete_overlapping: bool = Field(default=False, description="Confirm deletion of coded segments that overlap the range.")


class MemoCreate(BaseModel):
    scope: Literal["code", "interview", "segment", "general"]
    ref_id: str | None = Field(default=None, examples=["DE-00"])
    text: str = ""


class MemoUpdate(BaseModel):
    text: str


class RestoreRequest(BaseModel):
    filename: str = Field(examples=["project_2026-09-23_141500.db"])


class AnonymizationCreate(BaseModel):
    term: str = Field(examples=["Müller"])
    replacement: str = Field(examples=["[Person A]"])


class AnonymizationApply(BaseModel):
    hits: list[dict[str, Any]] | None = Field(default=None, description="Selected hits from /anonymization/scan; null = all.")
    interview_id: str | None = None


class ReliabilityNote(BaseModel):
    note: str


class ReadableExportRequest(BaseModel):
    interview_ids: list[str] | None = Field(default=None, description="Interviews to export; null = all.",
                                            examples=[["DE-00", "CN-00"]])


class ExportRequest(BaseModel):
    types: list[str] | None = Field(default=None, description="Subset of export types; null = all.",
                                    examples=[["codebook", "quote_bank"]])
    quote_segment_ids: list[int] | None = Field(default=None, description="Restrict the quote bank to these segments.")
