from __future__ import annotations

import hashlib
import json
import secrets
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload

from app.db.models import (AuditEventModel, DemoUserModel, DeliveryModel, SupplyPeriodModel,
                           SupplyPeriodEventModel, WaterRequestModel, WorkflowSessionModel)
from app.db.session import get_db
from app.services.request_allocator import RULES, allocate, explain

router = APIRouter()
_pool_lock = threading.RLock()


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt, expected = encoded.split("$", 2)
        return scheme == "scrypt" and secrets.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), expected)
    except (ValueError, TypeError):
        return False


class LoginBody(BaseModel):
    username: str
    password: str


class PeriodBody(BaseModel):
    start_date: date
    end_date: date
    available_m3: int = Field(gt=0)

    @model_validator(mode="after")
    def dates_ordered(self):
        if self.end_date < self.start_date:
            raise ValueError("End date must be on or after the start date.")
        return self


class RequestBody(BaseModel):
    requested_m3: int = Field(gt=0)
    purpose: str = Field(min_length=1)
    start_date: date
    end_date: date
    reason: str = Field(min_length=1, max_length=1000)
    area_acres: Optional[float] = Field(default=None, gt=0)
    crop: Optional[str] = Field(default=None, max_length=80)
    confirm_duplicate: bool = False

    @model_validator(mode="after")
    def validate_dates_reason(self):
        if self.end_date < self.start_date:
            raise ValueError("End date must be on or after the start date.")
        if not self.reason.strip():
            raise ValueError("Please add a short reason for the request.")
        if self.purpose not in {"irrigation", "livestock", "other"}:
            raise ValueError("Choose irrigation, livestock, or other as the purpose.")
        return self


class DecisionBody(BaseModel):
    action: str
    reason: str = Field(min_length=1, max_length=1000)
    approved_m3: Optional[int] = Field(default=None, ge=0)
    rule: Optional[str] = None
    snapshot: Optional[dict] = None

    @model_validator(mode="after")
    def reason_nonblank(self):
        if not self.reason.strip():
            raise ValueError("A decision reason is required.")
        return self


class ClarificationReply(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


class DeliveryBody(BaseModel):
    delivered_m3: int = Field(gt=0)
    delivered_at: Optional[datetime] = None
    note: str = ""


class ReceiptBody(BaseModel):
    action: str
    received_m3: Optional[int] = Field(default=None, ge=0)
    note: str = ""


class ResolveBody(BaseModel):
    note: str = Field(min_length=1, max_length=1000)


def _now():
    return datetime.now(timezone.utc)


def _user_from_token(authorization: Optional[str], db: Session) -> DemoUserModel:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in with a JalSetu demo account.")
    token_hash = hashlib.sha256(authorization.split(" ", 1)[1].encode()).hexdigest()
    session = db.query(WorkflowSessionModel).filter_by(token_hash=token_hash).first()
    if not session or session.expires_at.replace(tzinfo=timezone.utc) <= _now():
        if session:
            db.delete(session); db.commit()
        raise HTTPException(status_code=401, detail="Your session expired. Please sign in again.")
    user = db.query(DemoUserModel).filter_by(id=session.user_id, active=True).first()
    if not user:
        raise HTTPException(status_code=401, detail="This demo account is no longer active.")
    return user


def current_user(authorization: Optional[str] = Header(default=None), db: Session = Depends(get_db)):
    return _user_from_token(authorization, db)


def require_role(user: DemoUserModel, role: str):
    if user.role != role:
        raise HTTPException(status_code=403, detail=f"This action is available to the {role} role only.")


def _event(db: Session, request: WaterRequestModel, user: DemoUserModel, event_type: str,
           reason: str = "", old_status: Optional[str] = None, new_status: Optional[str] = None,
           metadata: Optional[dict] = None):
    prior = db.query(AuditEventModel).filter_by(request_id=request.id).order_by(AuditEventModel.created_at.desc(), AuditEventModel.id.desc()).first()
    previous = prior.event_hash if prior else "GENESIS"
    body = {"request_id": request.id, "actor_id": user.id, "actor_role": user.role,
            "event_type": event_type, "old_status": old_status, "new_status": new_status,
            "reason": reason, "metadata": metadata or {}, "previous_hash": previous}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    db.add(AuditEventModel(request_id=request.id, actor_id=user.id, actor_role=user.role,
        event_type=event_type, old_status=old_status, new_status=new_status,
        reason=reason, metadata_json=json.dumps(metadata or {}, sort_keys=True),
        previous_hash=previous, event_hash=digest))


def _period_event(db: Session, period: SupplyPeriodModel, user: DemoUserModel,
                  event_type: str, reason: str, payload: Optional[dict] = None):
    prior = db.query(SupplyPeriodEventModel).filter_by(period_id=period.id).order_by(
        SupplyPeriodEventModel.created_at.desc(), SupplyPeriodEventModel.id.desc()).first()
    previous = prior.event_hash if prior else "GENESIS"
    data = {"period_id": period.id, "actor_id": user.id, "actor_role": user.role,
            "event_type": event_type, "reason": reason, "payload": payload or {},
            "previous_hash": previous}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    db.add(SupplyPeriodEventModel(period_id=period.id, actor_id=user.id, actor_role=user.role,
        event_type=event_type, reason=reason, payload_json=json.dumps(payload or {}, sort_keys=True),
        previous_hash=previous, event_hash=digest))


def _serialize_request(request: WaterRequestModel, db: Session, viewer: DemoUserModel):
    deliveries = db.query(DeliveryModel).filter_by(request_id=request.id).order_by(DeliveryModel.created_at.asc()).all()
    events = db.query(AuditEventModel).filter_by(request_id=request.id).order_by(AuditEventModel.created_at.asc(), AuditEventModel.id.asc()).all()
    farmer = db.query(DemoUserModel).filter_by(id=request.farmer_id).first()
    output = {"id": request.id, "request_code": request.request_code,
        "farmer_name": farmer.display_name if viewer.role == "manager" else None,
        "requested_m3": request.requested_m3, "purpose": request.purpose,
        "start_date": request.start_date, "end_date": request.end_date, "reason": request.reason,
        "area_acres": request.area_acres, "crop": request.crop,
        "area_label": "Self-declared · unverified" if request.area_acres or request.crop else None,
        "status": request.status, "approved_m3": request.approved_m3,
        "decision_reason": request.decision_reason, "created_at": request.created_at.isoformat(),
        "deliveries": [{"id": d.id, "delivered_m3": d.delivered_m3, "delivered_at": d.delivered_at,
            "note": d.note, "receipt_status": d.receipt_status, "farmer_report_m3": d.farmer_report_m3,
            "discrepancy_note": d.discrepancy_note, "resolution_note": d.resolution_note,
            "over_approved": sum(x.delivered_m3 for x in deliveries) > request.approved_m3} for d in deliveries],
        "delivered_m3": sum(d.delivered_m3 for d in deliveries),
        "events": [{"id": e.id, "type": e.event_type, "actor_role": e.actor_role,
            "created_at": e.created_at.isoformat(), "reason": e.reason,
            "old_status": e.old_status, "new_status": e.new_status,
            "metadata": json.loads(e.metadata_json)} for e in events]}
    return output


def _get_request(db: Session, request_id: str, user: DemoUserModel):
    req = db.query(WaterRequestModel).filter_by(id=request_id, service_area_id=user.service_area_id).first()
    if not req or (user.role == "farmer" and req.farmer_id != user.id):
        raise HTTPException(status_code=404, detail="Request not found in your account or service area.")
    return req


def _totals(db: Session, period: SupplyPeriodModel):
    requests = db.query(WaterRequestModel).filter_by(supply_period_id=period.id).all()
    return {"requested_m3": sum(r.requested_m3 for r in requests),
            "approved_m3": sum(r.approved_m3 for r in requests if r.status in ("Approved", "Partially approved", "Approved quantity revised")),
            "request_count": len(requests)}


@router.post("/login")
def login(body: LoginBody, db: Session = Depends(get_db)):
    user = db.query(DemoUserModel).filter_by(username=body.username, active=True).first()
    if not user or not check_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Account or password did not match the demo credentials.")
    token = secrets.token_urlsafe(36)
    db.add(WorkflowSessionModel(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                                expires_at=_now().replace(tzinfo=None) + timedelta(hours=8)))
    db.commit()
    return {"token": token, "expires_in_hours": 8, "user": {"id": user.id, "username": user.username,
            "display_name": user.display_name, "role": user.role, "service_area_id": user.service_area_id}}


@router.get("/me")
def me(user: DemoUserModel = Depends(current_user)):
    return {"id": user.id, "username": user.username, "display_name": user.display_name,
            "role": user.role, "service_area_id": user.service_area_id}


@router.post("/logout")
def logout(authorization: Optional[str] = Header(default=None), db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    token = authorization.split(" ", 1)[1]
    db.query(WorkflowSessionModel).filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).delete()
    db.commit()
    return {"ok": True}


@router.get("/periods")
def periods(db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    rows = db.query(SupplyPeriodModel).filter_by(service_area_id=user.service_area_id).order_by(SupplyPeriodModel.start_date.desc()).all()
    if user.role == "farmer":
        return [{"id": p.id, "start_date": p.start_date, "end_date": p.end_date, "status": p.status} for p in rows]
    return [{"id": p.id, "start_date": p.start_date, "end_date": p.end_date,
             "available_m3": p.available_m3, "status": p.status, **_totals(db, p),
             "events": [{"type": e.event_type, "reason": e.reason, "payload": json.loads(e.payload_json),
                         "created_at": e.created_at.isoformat()} for e in db.query(SupplyPeriodEventModel).filter_by(period_id=p.id).order_by(SupplyPeriodEventModel.created_at.asc()).all()]} for p in rows]


@router.post("/periods", status_code=201)
def create_period(body: PeriodBody, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "manager")
    existing = db.query(SupplyPeriodModel).filter_by(service_area_id=user.service_area_id, status="open").all()
    if any(body.start_date.isoformat() <= p.end_date and body.end_date.isoformat() >= p.start_date for p in existing):
        raise HTTPException(status_code=409, detail="Supply periods in this service area cannot overlap.")
    period = SupplyPeriodModel(service_area_id=user.service_area_id, start_date=body.start_date.isoformat(),
        end_date=body.end_date.isoformat(), available_m3=body.available_m3, created_by=user.id)
    db.add(period); db.commit(); db.refresh(period)
    _period_event(db, period, user, "period_created", "Declared supply period created.",
                  {"available_m3": period.available_m3, "start_date": period.start_date, "end_date": period.end_date})
    db.commit()
    return {"id": period.id, "start_date": period.start_date, "end_date": period.end_date,
            "available_m3": period.available_m3, "status": period.status, "requested_m3": 0, "approved_m3": 0}


@router.patch("/periods/{period_id}")
def update_period(period_id: str, body: dict, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "manager")
    p = db.query(SupplyPeriodModel).filter_by(id=period_id, service_area_id=user.service_area_id).first()
    if not p:
        raise HTTPException(404, "Supply period not found.")
    if body.get("action") == "close":
        p.status = "closed"
        undecided = db.query(WaterRequestModel).filter(WaterRequestModel.supply_period_id == p.id,
            WaterRequestModel.status.in_(["Submitted", "Clarification requested"])).all()
        system_user = user
        for req in undecided:
            old = req.status; req.status = "Expired (no decision)"
            _event(db, req, system_user, "period_closed_expiry", "Supply period closed; no decision recorded.", old, req.status)
        _period_event(db, p, user, "period_closed", "Supply period closed; no new requests or approvals are accepted.", {"expired_requests": len(undecided)})
        db.commit()
    elif body.get("action") == "pool":
        amount = body.get("available_m3")
        reason = str(body.get("reason", "")).strip()
        if not isinstance(amount, int) or amount <= 0 or not reason:
            raise HTTPException(422, "A positive whole-m³ pool and a reason are required.")
        approved = _totals(db, p)["approved_m3"]
        if amount < approved:
            raise HTTPException(409, f"Pool cannot be lowered below the {approved:,} m³ already approved. Record reasoned corrections first.")
        old = p.available_m3; p.available_m3 = amount
        _period_event(db, p, user, "pool_changed", reason, {"old_available_m3": old, "available_m3": amount})
        db.commit()
        return {"ok": True, "old_available_m3": old, "available_m3": amount, "reason": reason}
    else:
        raise HTTPException(422, "Choose action 'close' or 'pool'.")
    return {"ok": True, "period_id": p.id, "status": p.status}


@router.get("/requests")
def list_requests(db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    query = db.query(WaterRequestModel).filter_by(service_area_id=user.service_area_id)
    if user.role == "farmer":
        query = query.filter_by(farmer_id=user.id)
    rows = query.order_by(WaterRequestModel.created_at.desc()).all()
    return [_serialize_request(r, db, user) for r in rows]


@router.post("/requests", status_code=201)
def create_request(body: RequestBody, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "farmer")
    period = db.query(SupplyPeriodModel).filter_by(service_area_id=user.service_area_id, status="open").all()
    covering = [p for p in period if body.start_date.isoformat() >= p.start_date and body.end_date.isoformat() <= p.end_date]
    if not covering:
        raise HTTPException(422, "No open supply period covers those dates. If dates cross periods, submit two requests.")
    period = covering[0]
    duplicate = db.query(WaterRequestModel).filter(WaterRequestModel.farmer_id == user.id,
        WaterRequestModel.purpose == body.purpose,
        WaterRequestModel.start_date <= body.end_date.isoformat(),
        WaterRequestModel.end_date >= body.start_date.isoformat(),
        WaterRequestModel.status.notin_(["Withdrawn", "Rejected", "Expired (no decision)"])).first()
    if duplicate and not body.confirm_duplicate:
        raise HTTPException(409, detail={"message": "You already have a request for this purpose with overlapping dates. Submit another anyway?", "duplicate_request_code": duplicate.request_code})
    req = WaterRequestModel(request_code=f"JS-{uuid.uuid4().hex[:8].upper()}", farmer_id=user.id,
        service_area_id=user.service_area_id, supply_period_id=period.id, requested_m3=body.requested_m3,
        purpose=body.purpose, start_date=body.start_date.isoformat(), end_date=body.end_date.isoformat(),
        reason=body.reason.strip(), area_acres=body.area_acres, crop=body.crop.strip() if body.crop else None)
    db.add(req); db.flush()
    _event(db, req, user, "request_submitted", req.reason, None, "Submitted",
           {"requested_m3": req.requested_m3, "purpose": req.purpose, "supply_period_id": period.id})
    db.commit(); db.refresh(req)
    return _serialize_request(req, db, user)


@router.put("/requests/{request_id}")
def edit_request(request_id: str, body: RequestBody, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "farmer")
    req = _get_request(db, request_id, user)
    if req.status not in ("Submitted", "Clarification requested"):
        raise HTTPException(409, "A request can only be edited before a decision.")
    periods = db.query(SupplyPeriodModel).filter_by(service_area_id=user.service_area_id, status="open").all()
    covering = [p for p in periods if body.start_date.isoformat() >= p.start_date and body.end_date.isoformat() <= p.end_date]
    if not covering:
        raise HTTPException(422, "No open supply period covers those dates. If dates cross periods, submit two requests.")
    duplicate = db.query(WaterRequestModel).filter(WaterRequestModel.farmer_id == user.id,
        WaterRequestModel.id != req.id, WaterRequestModel.purpose == body.purpose,
        WaterRequestModel.start_date <= body.end_date.isoformat(), WaterRequestModel.end_date >= body.start_date.isoformat(),
        WaterRequestModel.status.notin_(["Withdrawn", "Rejected", "Expired (no decision)"])).first()
    if duplicate and not body.confirm_duplicate:
        raise HTTPException(409, detail={"message": "You already have a request for this purpose with overlapping dates. Confirm the edit anyway?", "duplicate_request_code": duplicate.request_code})
    old = req.status
    req.supply_period_id = covering[0].id
    req.requested_m3 = body.requested_m3; req.purpose = body.purpose
    req.start_date = body.start_date.isoformat(); req.end_date = body.end_date.isoformat()
    req.reason = body.reason.strip(); req.area_acres = body.area_acres
    req.crop = body.crop.strip() if body.crop else None; req.status = "Submitted"
    _event(db, req, user, "request_edited", "Farmer updated request details.", old, req.status,
           {"requested_m3": req.requested_m3, "purpose": req.purpose,
            "start_date": req.start_date, "end_date": req.end_date, "reason": req.reason})
    db.commit(); return _serialize_request(req, db, user)


@router.get("/allocation/{period_id}")
def allocation_support(period_id: str, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "manager")
    period = db.query(SupplyPeriodModel).filter_by(id=period_id, service_area_id=user.service_area_id).first()
    if not period:
        raise HTTPException(404, "Supply period not found.")
    open_requests = db.query(WaterRequestModel).filter(WaterRequestModel.supply_period_id == period.id,
        WaterRequestModel.status.in_(["Submitted", "Clarification requested"])).order_by(WaterRequestModel.created_at.asc()).all()
    approved = _totals(db, period)["approved_m3"]
    remaining = max(0, period.available_m3 - approved)
    inputs = [{"id": r.id, "request_code": r.request_code, "farmer": r.farmer.display_name,
        "requested_m3": r.requested_m3, "start_date": r.start_date,
        "created_at": r.created_at.isoformat()} for r in open_requests]
    reqs = [{"id": r.id, "requested_m3": r.requested_m3, "start_date": r.start_date,
             "created_at": r.created_at.isoformat()} for r in open_requests]
    outcomes = {}
    for rule in RULES:
        output = allocate(reqs, remaining, rule)
        outcomes[rule] = {"label": RULES[rule], "allocations": output,
            "total_m3": sum(output.values()),
            "explanation": explain(rule, remaining, sum(r["requested_m3"] for r in reqs), sum(output.values()))}
    return {"period_id": period.id, "declared_pool_m3": period.available_m3,
        "already_approved_m3": approved, "remaining_pool_m3": remaining,
        "total_demand_m3": sum(r.requested_m3 for r in open_requests),
        "requests": inputs, "outcomes": outcomes,
        "explanation_source": "template", "ai_claim": "AI-generated language explanation is not enabled in this demo; this fixed template cannot change any number."}


@router.post("/requests/{request_id}/decision")
def decide(request_id: str, body: DecisionBody, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "manager")
    req = _get_request(db, request_id, user)
    allowed = {"approve": "Approved", "partial": "Partially approved", "reject": "Rejected", "clarify": "Clarification requested", "revise": "Approved quantity revised"}
    if body.action not in allowed:
        raise HTTPException(422, "Choose approve, partial, reject, clarify, or revise.")
    approved_statuses = ("Approved", "Partially approved", "Approved quantity revised")
    pending_statuses = ("Submitted", "Clarification requested")
    if body.action == "revise":
        if req.status not in approved_statuses:
            raise HTTPException(409, "Only an existing approval can be corrected.")
    elif req.status not in pending_statuses:
        raise HTTPException(409, f"A request in {req.status} cannot transition to a new decision.")
    if body.action == "clarify":
        if req.status not in ("Submitted", "Clarification requested"):
            raise HTTPException(409, "Clarification is only available before a decision.")
        old = req.status; req.status = allowed[body.action]; req.decision_reason = body.reason.strip()
        _event(db, req, user, "clarification_requested", body.reason.strip(), old, req.status)
        db.commit(); return _serialize_request(req, db, user)
    if body.action in ("approve", "partial", "revise"):
        if body.approved_m3 is None:
            raise HTTPException(422, "Enter an approved quantity in whole m³.")
        if body.approved_m3 > req.requested_m3:
            raise HTTPException(422, "Approved quantity cannot exceed the request.")
        if body.action == "partial" and not (0 < body.approved_m3 < req.requested_m3):
            raise HTTPException(422, "Partial approval must be above zero and below the requested amount.")
        if body.action == "approve" and body.approved_m3 <= 0:
            raise HTTPException(422, "Approval must be greater than zero.")
        period = db.query(SupplyPeriodModel).filter_by(id=req.supply_period_id).first()
        if not period or period.status != "open":
            raise HTTPException(409, "The supply period is closed; no new approval can be recorded.")
        # SQLite's BEGIN IMMEDIATE locks the write transaction across workers; databases
        # with row locks use SELECT FOR UPDATE. Auth reads are committed before locking.
        with _pool_lock:
            db.commit()
            if db.bind.dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
                period = db.query(SupplyPeriodModel).filter_by(id=req.supply_period_id).first()
            else:
                period = db.query(SupplyPeriodModel).filter_by(id=req.supply_period_id).with_for_update().first()
            req = _get_request(db, request_id, user)
            if period.status != "open":
                db.rollback(); raise HTTPException(409, "The supply period has been closed.")
            other_approved = db.query(WaterRequestModel).filter(WaterRequestModel.supply_period_id == period.id,
                WaterRequestModel.id != req.id,
                WaterRequestModel.status.in_(["Approved", "Partially approved", "Approved quantity revised"])).with_entities(WaterRequestModel.approved_m3).all()
            used = sum(x[0] for x in other_approved)
            if used + body.approved_m3 > period.available_m3:
                db.rollback(); raise HTTPException(409, f"Pool cap reached: {used:,} m³ is already approved of {period.available_m3:,} m³.")
            old, old_amount = req.status, req.approved_m3
            req.status = allowed[body.action]
            req.approved_m3 = body.approved_m3
            req.decision_reason = body.reason.strip()
            snapshot = body.snapshot or {}
            snapshot.update({"rule": body.rule, "requested_m3": req.requested_m3,
                "approved_m3": body.approved_m3, "pool_m3": period.available_m3,
                "suggested_m3": snapshot.get("suggested_m3")})
            event_type = "approval_correction" if body.action == "revise" else "decision"
            _event(db, req, user, event_type, body.reason.strip(), old, req.status,
                   {"previous_approved_m3": old_amount, "approved_m3": body.approved_m3, "recommendation_snapshot": snapshot})
            db.commit()
    else:
        if req.status not in ("Submitted", "Clarification requested"):
            raise HTTPException(409, "This request already has a decision.")
        old = req.status; req.status = allowed[body.action]; req.approved_m3 = 0
        req.decision_reason = body.reason.strip()
        _event(db, req, user, "decision", body.reason.strip(), old, req.status)
        db.commit()
    return _serialize_request(req, db, user)


@router.post("/requests/{request_id}/reply")
def reply(request_id: str, body: ClarificationReply, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "farmer")
    req = _get_request(db, request_id, user)
    if req.status != "Clarification requested":
        raise HTTPException(409, "This request is not awaiting clarification.")
    old = req.status; req.status = "Submitted"; req.reason = body.note.strip()
    _event(db, req, user, "clarification_reply", body.note.strip(), old, req.status)
    db.commit(); return _serialize_request(req, db, user)


@router.post("/requests/{request_id}/withdraw")
def withdraw(request_id: str, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "farmer")
    req = _get_request(db, request_id, user)
    if req.status not in ("Submitted", "Clarification requested"):
        raise HTTPException(409, "Only a request without a decision can be withdrawn.")
    old = req.status; req.status = "Withdrawn"
    _event(db, req, user, "withdrawal", "Withdrawn by farmer.", old, req.status)
    db.commit(); return _serialize_request(req, db, user)


@router.post("/requests/{request_id}/deliveries", status_code=201)
def record_delivery(request_id: str, body: DeliveryBody, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "manager")
    req = _get_request(db, request_id, user)
    if req.status not in ("Approved", "Partially approved", "Approved quantity revised"):
        raise HTTPException(409, "Deliveries can be recorded only for an approved request.")
    note = body.note.strip()
    if sum(d.delivered_m3 for d in db.query(DeliveryModel).filter_by(request_id=req.id).all()) + body.delivered_m3 > req.approved_m3 and not note:
        raise HTTPException(422, "A note is required when recorded deliveries exceed the approved amount.")
    delivery = DeliveryModel(request_id=req.id, recorded_by=user.id, delivered_m3=body.delivered_m3,
        delivered_at=(body.delivered_at or _now()).isoformat(), note=note)
    db.add(delivery)
    _event(db, req, user, "delivery_recorded", note, req.status, req.status, {"delivered_m3": body.delivered_m3})
    db.commit(); return _serialize_request(req, db, user)


@router.post("/deliveries/{delivery_id}/receipt")
def receipt(delivery_id: str, body: ReceiptBody, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "farmer")
    delivery = db.query(DeliveryModel).filter_by(id=delivery_id).first()
    if not delivery:
        raise HTTPException(404, "Delivery not found.")
    req = _get_request(db, delivery.request_id, user)
    if delivery.receipt_status != "Recorded":
        raise HTTPException(409, "This delivery has already been acknowledged or reported.")
    if body.action == "acknowledge":
        delivery.receipt_status = "Acknowledged"
        event = "receipt_acknowledged"
    elif body.action == "discrepancy":
        if body.received_m3 is None or not body.note.strip():
            raise HTTPException(422, "Enter the amount received and explain the discrepancy.")
        delivery.receipt_status = "Discrepancy reported"
        delivery.farmer_report_m3 = body.received_m3
        delivery.discrepancy_note = body.note.strip()
        event = "discrepancy_reported"
    else:
        raise HTTPException(422, "Choose acknowledge or discrepancy.")
    _event(db, req, user, event, body.note.strip(), req.status, req.status,
           {"delivery_id": delivery.id, "recorded_m3": delivery.delivered_m3, "reported_m3": body.received_m3})
    db.commit(); return _serialize_request(req, db, user)


@router.post("/deliveries/{delivery_id}/resolve")
def resolve_discrepancy(delivery_id: str, body: ResolveBody, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "manager")
    delivery = db.query(DeliveryModel).filter_by(id=delivery_id).first()
    if not delivery:
        raise HTTPException(404, "Delivery not found.")
    req = _get_request(db, delivery.request_id, user)
    if delivery.receipt_status != "Discrepancy reported":
        raise HTTPException(409, "This delivery has no open discrepancy.")
    delivery.receipt_status = "Discrepancy resolved"; delivery.resolution_note = body.note.strip()
    _event(db, req, user, "discrepancy_resolved", body.note.strip(), req.status, req.status, {"delivery_id": delivery.id})
    db.commit(); return _serialize_request(req, db, user)


@router.get("/ledger/verify/{request_id}")
def verify_ledger(request_id: str, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    req = _get_request(db, request_id, user)
    rows = db.query(AuditEventModel).filter_by(request_id=req.id).order_by(AuditEventModel.created_at.asc(), AuditEventModel.id.asc()).all()
    previous = "GENESIS"; intact = True
    for row in rows:
        body = {"request_id": row.request_id, "actor_id": row.actor_id, "actor_role": row.actor_role,
            "event_type": row.event_type, "old_status": row.old_status, "new_status": row.new_status,
            "reason": row.reason, "metadata": json.loads(row.metadata_json), "previous_hash": previous}
        expected = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if row.previous_hash != previous or row.event_hash != expected:
            intact = False; break
        previous = row.event_hash
    return {"request_id": req.request_code, "status": "intact" if intact else "broken",
            "events_checked": len(rows), "limitation": "Tamper evidence, not tamper proof; a database administrator could rebuild the chain."}


@router.get("/ledger/verify-period/{period_id}")
def verify_period_ledger(period_id: str, db: Session = Depends(get_db), user: DemoUserModel = Depends(current_user)):
    require_role(user, "manager")
    period = db.query(SupplyPeriodModel).filter_by(id=period_id, service_area_id=user.service_area_id).first()
    if not period:
        raise HTTPException(404, "Supply period not found in your service area.")
    rows = db.query(SupplyPeriodEventModel).filter_by(period_id=period.id).order_by(
        SupplyPeriodEventModel.created_at.asc(), SupplyPeriodEventModel.id.asc()).all()
    previous = "GENESIS"; intact = True
    for row in rows:
        body = {"period_id": row.period_id, "actor_id": row.actor_id, "actor_role": row.actor_role,
            "event_type": row.event_type, "reason": row.reason,
            "payload": json.loads(row.payload_json), "previous_hash": previous}
        expected = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        if row.previous_hash != previous or row.event_hash != expected:
            intact = False; break
        previous = row.event_hash
    return {"period_id": period.id, "status": "intact" if intact else "broken",
            "events_checked": len(rows), "limitation": "Tamper evidence, not tamper proof; a database administrator could rebuild the chain."}
