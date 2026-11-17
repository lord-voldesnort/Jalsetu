import json
import logging
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.session import engine, SessionLocal
from app.db.models import (AuditEventModel, DemoUserModel, FieldModel, ObservationModel,
                           ScenarioModel, ServiceAreaModel, SupplyPeriodModel, WaterRequestModel)
from app.services.data_loader import data_loader

logger = logging.getLogger(__name__)


def init_db(db: Session = None) -> None:
    """Create database tables if they do not exist."""
    Base.metadata.create_all(bind=engine)


def seed_database(db: Session) -> dict:
    """Populate database with fixtures from fields.geojson, observations.json, scenarios.json idempotently."""
    init_db(db)

    # 1. Seed Fields
    fields_data = data_loader.load_fields()
    fields_created = 0
    fields_existing = 0

    for feat in fields_data.features:
        p = feat.properties
        existing = db.query(FieldModel).filter(FieldModel.field_id == p.id).first()
        if existing:
            existing.name = p.name
            existing.crop = p.crop
            existing.stage = p.stage
            existing.area_acres = p.area_acres
            existing.area_ha = p.area_ha or round(p.area_acres * 0.404686, 3)
            existing.geometry_geojson = json.dumps(feat.geometry.coordinates)
            fields_existing += 1
        else:
            new_field = FieldModel(
                field_id=p.id,
                name=p.name,
                crop=p.crop,
                stage=p.stage,
                area_acres=p.area_acres,
                area_ha=p.area_ha or round(p.area_acres * 0.404686, 3),
                geometry_geojson=json.dumps(feat.geometry.coordinates)
            )
            db.add(new_field)
            fields_created += 1

    db.commit()

    # 2. Seed Observations (if empty for fields)
    obs_data = data_loader.load_observations()
    obs_count = db.query(ObservationModel).count()
    obs_created = 0

    if obs_count == 0:
        obs_models = [
            ObservationModel(
                field_id=o.field_id,
                timestamp=o.timestamp,
                source=o.source,
                variable=o.variable,
                value=o.value,
                unit=o.unit,
                quality_flag=o.quality_flag,
                age_days=o.age_days
            )
            for o in obs_data
        ]
        db.bulk_save_objects(obs_models)
        db.commit()
        obs_created = len(obs_models)

    # 3. Seed Scenarios
    scenarios_data = data_loader.load_scenarios()
    scenarios_created = 0

    for s in scenarios_data:
        existing_scen = db.query(ScenarioModel).filter(ScenarioModel.id == s.id).first()
        if not existing_scen:
            db.add(
                ScenarioModel(
                    id=s.id,
                    name=s.name,
                    title=s.title,
                    description=s.description,
                    scenario_type=s.scenario_type,
                    intensity=s.intensity or "moderate",
                    raw_json=s.model_dump_json()
                )
            )
            scenarios_created += 1

    db.commit()

    workflow_seeded = seed_water_workflow(db)
    summary = {
        "fields_loaded": fields_created + fields_existing,
        "fields_created": fields_created,
        "observations_loaded": obs_created if obs_created > 0 else obs_count,
        "scenarios_loaded": len(scenarios_data),
        "scenarios_created": scenarios_created,
        "workflow_demo_ready": workflow_seeded
    }
    return summary


def seed_water_workflow(db: Session) -> bool:
    """Create clearly illustrative, role-separated demo accounts and requests once."""
    from datetime import date, timedelta
    from app.api.v1.endpoints.water_workflow import hash_password, _period_event

    if db.query(ServiceAreaModel).first():
        return False
    area = ServiceAreaModel(name="Pimpri Canal Demo Area")
    db.add(area)
    db.flush()
    account_specs = [
        ("manager", "Water Manager", "water_manager", "manager"),
        ("farmer-a", "Farmer A", "farmer_a", "farmer"),
        ("farmer-b", "Farmer B", "farmer_b", "farmer"),
        ("farmer-c", "Farmer C", "farmer_c", "farmer"),
    ]
    users = {}
    for username, name, password, role in account_specs:
        record = DemoUserModel(username=username, display_name=name,
            password_hash=hash_password(password), role=role, service_area_id=area.id)
        db.add(record)
        users[username] = record
    db.flush()
    today = date.today()
    period = SupplyPeriodModel(service_area_id=area.id,
        start_date=today.isoformat(), end_date=(today + timedelta(days=60)).isoformat(),
        available_m3=10000, status="open", created_by=users["manager"].id)
    db.add(period)
    db.flush()
    _period_event(db, period, users["manager"], "period_created", "Illustrative demo supply pool created.",
                  {"available_m3": period.available_m3, "start_date": period.start_date, "end_date": period.end_date})
    examples = [
        ("farmer-a", 6000, 10),
        ("farmer-b", 5000, 3),
        ("farmer-c", 4000, 6),
    ]
    for username, amount, offset in examples:
        user = users[username]
        req = WaterRequestModel(request_code=f"JS-DEMO-{username[-1].upper()}",
            farmer_id=user.id, service_area_id=area.id, supply_period_id=period.id,
            requested_m3=amount, purpose="irrigation",
            start_date=(today + timedelta(days=offset)).isoformat(),
            end_date=(today + timedelta(days=offset + 4)).isoformat(),
            reason="Illustrative hackathon demo request.", status="Submitted")
        db.add(req)
        db.flush()
        event_data = {"request_id": req.id, "actor_id": user.id, "actor_role": "farmer",
            "event_type": "request_submitted", "old_status": None, "new_status": "Submitted",
            "reason": req.reason, "metadata": {"requested_m3": amount, "purpose": "irrigation", "supply_period_id": period.id},
            "previous_hash": "GENESIS"}
        import hashlib, json
        event_hash = hashlib.sha256(json.dumps(event_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        db.add(AuditEventModel(request_id=req.id, actor_id=user.id, actor_role="farmer",
            event_type="request_submitted", old_status=None, new_status="Submitted",
            reason=req.reason, metadata_json=json.dumps(event_data["metadata"], sort_keys=True),
            previous_hash="GENESIS", event_hash=event_hash))
    db.commit()
    return True


if __name__ == "__main__":
    db = SessionLocal()
    try:
        res = seed_database(db)
        print(f"Database seeded successfully: {res}")
    finally:
        db.close()
