from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException

from ..database import stamp

def notification(db, user_id: str | None, kind: str, title: str, body: str) -> None:
    if user_id:
        db.execute("INSERT INTO notifications (id,user_id,kind,title,body,created_at) VALUES (?,?,?,?,?,?)",
                   (str(uuid4()), user_id, kind, title[:100], body[:500], stamp()))


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def row_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    for field in ("categories", "storage"):
        if field in item:
            item[field] = json.loads(item[field])
    if "verification_report" in item and isinstance(item["verification_report"], str):
        try:
            item["verification_report"] = json.loads(item["verification_report"])
        except json.JSONDecodeError:
            item["verification_report"] = {}
    if "verification_report" in item and item["verification_report"]:
        report = item["verification_report"]
        checks = report.get("checks", [])
        report.setdefault("document_details", next((c.get("detail", "") for c in checks if c.get("agent") == "Document extraction agent"), "Document review is available."))
        report.setdefault("registration_reference", next((c.get("detail", "") for c in checks if c.get("agent") == "Reference consistency agent"), "Reference check not run."))
        report.setdefault("official_record_check", report.get("registry", {}).get("detail", "Unconfirmed"))
    if "evidence" in item and "verification_status" in item and not item.get("verification_report"):
        item["verification_report"] = {
            "document_details": "provided" if len(item.get("evidence", "").strip()) >= 10 else "incomplete",
            "registration_reference": "provided" if item.get("reference_id", "").strip() else "not provided",
            "official_record_check": "unconfirmed — no official registry API is connected",
            "human_review": "required" if item["verification_status"] == "pending" else item["verification_status"],
            "recommendation": "Review submitted evidence; do not treat a reference ID as government endorsement.",
        }
    return item


def event(db, donation_id: str, event_type: str, detail: str, portion_id: str | None = None):
    db.execute("INSERT INTO events (donation_id,portion_id,event_type,detail,created_at) VALUES (?,?,?,?,?)",
               (donation_id, portion_id, event_type, detail, stamp()))


def recipient_owned_by(db, recipient_id: str, user: dict) -> sqlite3.Row:
    recipient = db.execute("SELECT * FROM recipients WHERE id=?", (recipient_id,)).fetchone()
    if not recipient:
        raise HTTPException(404, "Organization profile not found")
    if user["role"] != "admin" and recipient["user_id"] != user["id"]:
        raise HTTPException(403, "You can only access documents for your own organization")
    return recipient


def donation_view(db, donation_id: str):
    donation = db.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()
    if not donation:
        raise HTTPException(404, "Donation not found")
    result = dict(donation)
    result.pop("donor_id", None)
    result.pop("delivery_code", None)
    portions = db.execute("""SELECT p.id,p.donation_id,p.recipient_id,p.quantity_kg,p.status,p.pickup_at,p.received_at,p.created_at,
      p.pickup_confirmed_by,r.name AS recipient_name,r.contact AS recipient_contact,r.address AS recipient_address,
      r.lat AS destination_lat,r.lon AS destination_lon
      FROM portions p JOIN recipients r ON r.id=p.recipient_id
      WHERE p.donation_id=? ORDER BY p.created_at""", (donation_id,)).fetchall()
    result["portions"] = [dict(p) for p in portions]
    reserved = sum(p["quantity_kg"] for p in result["portions"] if p["status"] in ("offered", "accepted", "picked_up"))
    delivered = sum(p["quantity_kg"] for p in result["portions"] if p["status"] == "received")
    result["reserved_quantity_kg"] = round(reserved, 2)
    result["delivered_quantity_kg"] = round(delivered, 2)
    result["available_quantity_kg"] = round(max(0, donation["quantity_kg"] - reserved - delivered), 2)
    result["timeline"] = [dict(e) for e in db.execute("SELECT * FROM events WHERE donation_id=? ORDER BY created_at DESC", (donation_id,))]
    return result


def donation_views(db, donation_ids: list[str]):
    """Load donation cards in three queries instead of three queries per card."""
    if not donation_ids:
        return []
    marks = ",".join("?" for _ in donation_ids)
    donations = {row["id"]: dict(row) for row in db.execute(
        f"SELECT * FROM donations WHERE id IN ({marks})", tuple(donation_ids))}
    for item in donations.values():
        item.pop("donor_id", None)
        item.pop("delivery_code", None)
    portions = db.execute(f"""SELECT p.id,p.donation_id,p.recipient_id,p.quantity_kg,p.status,p.pickup_at,p.received_at,p.created_at,
      p.pickup_confirmed_by,r.name AS recipient_name,r.contact AS recipient_contact,r.address AS recipient_address,
      r.lat AS destination_lat,r.lon AS destination_lon
      FROM portions p JOIN recipients r ON r.id=p.recipient_id
      WHERE p.donation_id IN ({marks}) ORDER BY p.created_at""", tuple(donation_ids)).fetchall()
    events = db.execute(f"SELECT * FROM events WHERE donation_id IN ({marks}) ORDER BY created_at DESC", tuple(donation_ids)).fetchall()
    for item in donations.values():
        item["portions"] = []
        item["timeline"] = []
    for row in portions:
        donations[row["donation_id"]]["portions"].append(dict(row))
    for row in events:
        donations[row["donation_id"]]["timeline"].append(dict(row))
    for item in donations.values():
        reserved = sum(p["quantity_kg"] for p in item["portions"] if p["status"] in ("offered", "accepted", "picked_up"))
        delivered = sum(p["quantity_kg"] for p in item["portions"] if p["status"] == "received")
        item["reserved_quantity_kg"] = round(reserved, 2)
        item["delivered_quantity_kg"] = round(delivered, 2)
        item["available_quantity_kg"] = round(max(0, item["quantity_kg"] - reserved - delivered), 2)
    return [donations[did] for did in donation_ids if did in donations]


def donation_owned_by(db, donation_id: str, user: dict) -> sqlite3.Row:
    donation = db.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()
    if not donation:
        raise HTTPException(404,"Donation not found")
    if user["role"] != "admin" and donation["donor_id"] != user["id"]:
        raise HTTPException(403,"You can only manage donations listed by your account")
    return donation


def safety_eligible(donation: sqlite3.Row, recipient: sqlite3.Row, current: datetime) -> tuple[bool, str]:
    if recipient["verification_status"] != "approved": return False, "Recipient is not verified"
    if donation["category"] not in json.loads(recipient["categories"]): return False, "Food category is not accepted"
    if donation["storage"] not in json.loads(recipient["storage"]): return False, "Storage capability does not match"
    deadline = datetime.fromisoformat(donation["expires_at"])
    if deadline <= current: return False, "Consumption deadline has passed"
    if donation["storage"] == "hot" and (donation["temperature_c"] is None or donation["temperature_c"] < 60):
        return False, "Hot food is below the 60°C holding threshold"
    if donation["storage"] == "chilled" and (donation["temperature_c"] is None or donation["temperature_c"] > 5):
        return False, "Chilled food is above the 5°C holding threshold"
    return True, "Eligible"


def portion_participant(db, portion_id: str, user: dict) -> tuple[sqlite3.Row,sqlite3.Row,sqlite3.Row]:
    portion=db.execute("SELECT * FROM portions WHERE id=?",(portion_id,)).fetchone()
    if not portion: raise HTTPException(404,"Allocation not found")
    donation=db.execute("SELECT * FROM donations WHERE id=?",(portion["donation_id"],)).fetchone()
    recipient=db.execute("SELECT * FROM recipients WHERE id=?",(portion["recipient_id"],)).fetchone()
    if user["role"]=="admin" or donation["donor_id"]==user["id"] or recipient["user_id"]==user["id"]:
        return portion,donation,recipient
    raise HTTPException(403,"You are not part of this handover")
