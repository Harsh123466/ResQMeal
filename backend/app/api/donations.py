import json
import secrets
from datetime import datetime
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from ..database import MAX_COLLECTION_RADIUS_KM, connect, now, stamp
from ..dependencies import current_user
from ..schemas import DonationIn, DonationRequestIn
from ..services.common import distance_km, donation_owned_by, donation_view, donation_views, event, notification, row_dict, safety_eligible

router = APIRouter()

@router.get("/api/donations")
def donations(status: str | None = None, user: dict = Depends(current_user)):
    if user["role"] not in ("donor", "recipient", "admin"):
        raise HTTPException(403, "This account role is not enabled for food listings")
    with connect() as db:
        # PostgreSQL requires ORDER BY expressions to appear in a DISTINCT select list.
        query = "SELECT DISTINCT d.id, d.created_at FROM donations d"
        clauses, args = [], []
        if user["role"] == "donor":
            clauses.append("d.donor_id=?"); args.append(user["id"])
        elif user["role"] == "recipient":
            org = db.execute("SELECT id FROM recipients WHERE user_id=?", (user["id"],)).fetchone()
            if org:
                query += " LEFT JOIN portions p ON p.donation_id=d.id"
                clauses.append("(d.status IN ('open','at_risk') OR p.recipient_id=?)"); args.append(org["id"])
            else:
                clauses.append("d.status IN ('open','at_risk')")
        if status:
            clauses.append("d.status=?"); args.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        ids = [r[0] for r in db.execute(query + where + " ORDER BY d.created_at DESC", tuple(args))]
        items = donation_views(db, ids)
        if user["role"] == "recipient":
            recipient = db.execute("SELECT * FROM recipients WHERE user_id=?", (user["id"],)).fetchone()
            if recipient:
                existing = {r[0] for r in db.execute("SELECT donation_id FROM portions WHERE recipient_id=?", (recipient["id"],)).fetchall()}
                recipient_allocated = db.execute("SELECT COALESCE(SUM(quantity_kg),0) FROM portions WHERE recipient_id=? AND status IN ('offered','accepted','picked_up')", (recipient["id"],)).fetchone()[0]
                for item in items:
                    item["portions"] = [p for p in item["portions"] if p["recipient_id"] == recipient["id"]]
                current_time = now()
                def is_current_listing(item):
                    if item["status"] not in ("open", "at_risk", "matched"):
                        return False
                    try:
                        deadline = datetime.fromisoformat(item["expires_at"])
                        if deadline.tzinfo is None:
                            deadline = deadline.replace(tzinfo=current_time.tzinfo)
                        if deadline <= current_time:
                            return False
                    except (TypeError, ValueError):
                        return False
                    delivered = item.get("delivered_quantity_kg", 0)
                    if delivered >= item["quantity_kg"] - .01:
                        return False
                    has_active_handover = any(p["status"] in ("offered", "accepted", "picked_up") for p in item["portions"])
                    return item["available_quantity_kg"] > .01 or has_active_handover

                # Recipient accounts browse current, requestable listings and their active handovers.
                # Completed and expired donation history remains available to donors and admins.
                items = [item for item in items if is_current_listing(item) and (any(p["recipient_id"] == recipient["id"] for p in item["portions"]) or distance_km(
                    item["lat"], item["lon"], recipient["lat"], recipient["lon"]
                ) <= MAX_COLLECTION_RADIUS_KM)]
            else:
                items = []
            for item in items:
                reason = ""
                distance = None
                if not recipient:
                    reason = "Register your organization profile to request food"
                elif recipient["verification_status"] != "approved":
                    reason = "Your organization must be approved before requesting food"
                elif item["status"] not in ("open", "at_risk"):
                    reason = "This donation is no longer accepting requests"
                else:
                    eligible, reason = safety_eligible(item, recipient, now())
                    if eligible:
                        reason = ""
                    distance = distance_km(item["lat"], item["lon"], recipient["lat"], recipient["lon"])
                    if eligible and distance > MAX_COLLECTION_RADIUS_KM:
                        reason = "Outside the 35 km collection area"
                    elif eligible and item["id"] in existing:
                        reason = "Your organization already has an offer for this donation"
                    elif eligible:
                        if min(item["available_quantity_kg"], recipient["capacity_kg"] - recipient_allocated) <= .01:
                            reason = "No donation quantity or organization capacity remains"
                item["request_eligibility"] = {"eligible": not reason, "reason": reason, "distance_km": round(distance, 1) if distance is not None else None}
        return items


@router.post("/api/donations", status_code=201)
def create_donation(data: DonationIn, user: dict = Depends(current_user)):
    if user["role"] != "donor":
        raise HTTPException(403, "Sign in with a donor account to list food")
    did = "AN-" + secrets.token_hex(4).upper()
    delivery_code = secrets.token_hex(8).upper()
    with connect() as db:
        db.execute("""INSERT INTO donations
          (id,donor_name,food_name,category,quantity_kg,prepared_at,expires_at,storage,temperature_c,pickup_address,lat,lon,notes,status,created_at,donor_id,delivery_code)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (did,user["name"],data.food_name,data.category,data.quantity_kg,stamp(data.prepared_at),stamp(data.expires_at),data.storage,data.temperature_c,data.pickup_address,data.latitude,data.longitude,data.notes,"open",stamp(),user["id"],delivery_code))
        event(db,did,"listed",f"{data.quantity_kg:g} kg of {data.food_name} listed by {user['name']}")
        return donation_view(db,did)


@router.get("/api/donations/{donation_id}/delivery-code")
def donation_delivery_code(donation_id: str, user: dict = Depends(current_user)):
    with connect() as db:
        donation = db.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()
        if not donation:
            raise HTTPException(404, "Donation not found")
        authorized = bool(db.execute("""SELECT 1 FROM portions p JOIN recipients r ON r.id=p.recipient_id
            WHERE p.donation_id=? AND r.user_id=? AND p.status IN ('accepted','picked_up','received') LIMIT 1""",
            (donation_id, user["id"])).fetchone()) if user["role"] == "recipient" else False
        if not authorized:
            raise HTTPException(403, "Only the assigned recipient organization can view this delivery code")
        return JSONResponse({"donation_id": donation_id, "code": donation["delivery_code"]}, headers={"Cache-Control": "no-store"})


@router.post("/api/donations/{donation_id}/match")
def match_donation(donation_id: str, user: dict = Depends(current_user)):
    with connect() as db:
        if getattr(db, "is_postgres", False):
            db.execute("SELECT id FROM donations WHERE id=? FOR UPDATE", (donation_id,))
        else:
            db.execute("BEGIN IMMEDIATE")
        donation = db.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()
        if not donation: raise HTTPException(404, "Donation not found")
        is_participating_recipient = user["role"]=="recipient" and db.execute("""SELECT 1 FROM portions p JOIN recipients r ON r.id=p.recipient_id
          WHERE p.donation_id=? AND r.user_id=? LIMIT 1""",(donation_id,user["id"])).fetchone()
        if user["role"] not in ("admin","donor") and not is_participating_recipient:
            raise HTTPException(403,"Only the donor, assigned recipient, or administrator can run rescue matching")
        if user["role"]=="donor" and donation["donor_id"]!=user["id"]:
            raise HTTPException(403,"You can only match donations listed by your account")
        # A partially allocated donation can still have free stock after one
        # allocation is delivered or cancelled. Allow matching that remainder
        # even if an earlier match left the parent status as "matched".
        if donation["status"] not in ("open", "at_risk", "matched"): raise HTTPException(409, "Donation is not available for matching")
        if datetime.fromisoformat(donation["expires_at"]) <= now(): raise HTTPException(409, "Donation deadline has passed")
        current_allocated = db.execute("SELECT COALESCE(SUM(quantity_kg),0) FROM portions WHERE donation_id=? AND status IN ('offered','accepted','picked_up','received')", (donation_id,)).fetchone()[0]
        remaining = max(0, donation["quantity_kg"] - current_allocated)
        if remaining <= 0.01: raise HTTPException(409, "Donation is already fully allocated")
        candidates = []
        excluded = set()
        # A recipient that completed or cancelled an earlier allocation may
        # still be eligible for the remaining quantity. Exclude only recipients
        # who already have an active offer or handover for this donation.
        prior = db.execute("SELECT recipient_id FROM portions WHERE donation_id=? AND status IN ('offered','accepted','picked_up')", (donation_id,)).fetchall()
        excluded.update(x[0] for x in prior)
        rejection_counts = {}
        def reject(reason):
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        for recipient in db.execute("SELECT * FROM recipients WHERE verification_status='approved'"):
            if recipient["id"] in excluded:
                reject("already has an active offer or handover")
                continue
            okay, reason = safety_eligible(donation, recipient, now())
            if not okay:
                reject(reason)
                continue
            distance = distance_km(donation["lat"],donation["lon"],recipient["lat"],recipient["lon"])
            if distance > MAX_COLLECTION_RADIUS_KM:
                reject("outside the 35 km collection area")
                continue
            allocated = db.execute("SELECT COALESCE(SUM(quantity_kg),0) FROM portions WHERE recipient_id=? AND status IN ('offered','accepted','picked_up')",(recipient["id"],)).fetchone()[0]
            capacity = max(0, recipient["capacity_kg"] - allocated)
            if capacity <= 0:
                reject("no remaining organization capacity")
                continue
            hours_left = max(.05,(datetime.fromisoformat(donation["expires_at"]) - now()).total_seconds()/3600)
            urgency = max(0, 1 - hours_left / 24)
            score = 55 * max(0, 1 - distance / MAX_COLLECTION_RADIUS_KM) + 25 * min(1, capacity / max(1,remaining)) + 20 * urgency
            candidates.append((score,distance,recipient,capacity))
        candidates.sort(key=lambda x:(-x[0],x[1]))
        if not candidates:
            db.execute("UPDATE donations SET status='at_risk' WHERE id=?",(donation_id,))
            event(db,donation_id,"no_match","No verified, safe and available recipient found within 35 km")
            result=donation_view(db,donation_id)
            result["recommendations"]=[]
            if rejection_counts:
                reasons = sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))
                detail = "; ".join(f"{count} {reason}" for reason, count in reasons[:3])
                result["matching_message"] = f"No eligible NGO found for the remaining {remaining:g} kg. Checked {sum(rejection_counts.values())} approved organization(s): {detail}."
            else:
                result["matching_message"] = "No approved NGO profiles are available to check for this donation."
            return result
        created=[]
        for score,distance,recipient,capacity in candidates:
            if remaining <= .01: break
            amount=min(remaining,capacity)
            pid=str(uuid4())
            legacy_otp=f"{secrets.randbelow(1000000):06d}"
            db.execute("INSERT INTO portions (id,donation_id,recipient_id,quantity_kg,status,otp,created_at) VALUES (?,?,?,?,?,?,?)",(pid,donation_id,recipient["id"],amount,"offered",legacy_otp,stamp()))
            notification(db,recipient["user_id"],"donation_offer","New food donation offer",f"{amount:g} kg of {donation['food_name']} is available for pickup at {donation['pickup_address']}.")
            created.append({"id":pid,"recipient_id":recipient["id"],"recipient_name":recipient["name"],"quantity_kg":round(amount,2),"distance_km":round(distance,1),"score":round(score),"status":"offered"})
            remaining -= amount
        db.execute("UPDATE donations SET status=? WHERE id=?",("matched" if remaining <= .01 else "open",donation_id))
        event(db,donation_id,"matched",f"Matched {sum(x['quantity_kg'] for x in created):g} kg across {len(created)} verified recipient(s)" + (f"; {remaining:g} kg remains unallocated" if remaining > .01 else ""))
        result=donation_view(db,donation_id)
        result["recommendations"]=created
        result["unallocated_kg"]=round(remaining,2)
        return result


@router.post("/api/donations/{donation_id}/request", status_code=201)
def request_donation(donation_id: str, data: DonationRequestIn, user: dict = Depends(current_user)):
    if user["role"] != "recipient":
        raise HTTPException(403, "Only a recipient organization can request a donation")
    with connect() as db:
        if getattr(db, "is_postgres", False):
            db.execute("SELECT id FROM donations WHERE id=? FOR UPDATE", (donation_id,))
        else:
            db.execute("BEGIN IMMEDIATE")
        recipient = db.execute("SELECT * FROM recipients WHERE user_id=?", (user["id"],)).fetchone()
        if not recipient:
            raise HTTPException(409, "Create your organization profile before requesting food")
        if recipient["verification_status"] != "approved":
            raise HTTPException(403, "Your organization must be approved before receiving food")
        donation = db.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()
        if not donation:
            raise HTTPException(404, "Donation not found")
        if donation["status"] not in ("open", "at_risk", "matched"):
            raise HTTPException(409, "This donation is no longer accepting recipient requests")
        okay, reason = safety_eligible(donation, recipient, now())
        if not okay:
            raise HTTPException(409, reason)
        distance = distance_km(donation["lat"], donation["lon"], recipient["lat"], recipient["lon"])
        if distance > MAX_COLLECTION_RADIUS_KM:
            raise HTTPException(409, "This donation is outside the 35 km collection area")
        previous = db.execute("SELECT status FROM portions WHERE donation_id=? AND recipient_id=?", (donation_id, recipient["id"])).fetchone()
        if previous:
            raise HTTPException(409, "Your organization already has an allocation for this donation")
        donation_allocated = db.execute("SELECT COALESCE(SUM(quantity_kg),0) FROM portions WHERE donation_id=? AND status IN ('offered','accepted','picked_up','received')", (donation_id,)).fetchone()[0]
        recipient_allocated = db.execute("SELECT COALESCE(SUM(quantity_kg),0) FROM portions WHERE recipient_id=? AND status IN ('offered','accepted','picked_up')", (recipient["id"],)).fetchone()[0]
        available = max(0, donation["quantity_kg"] - donation_allocated)
        capacity = max(0, recipient["capacity_kg"] - recipient_allocated)
        quantity = float(data.quantity_kg)
        if quantity > available + .001:
            raise HTTPException(409, f"Only {max(0, available):g} kg is currently available")
        if quantity > capacity + .001:
            raise HTTPException(409, f"Your organization has capacity for {max(0, capacity):g} kg")
        if quantity <= 0:
            raise HTTPException(409, "No donation quantity or organization capacity remains")
        portion_id = str(uuid4())
        legacy_otp = f"{secrets.randbelow(1000000):06d}"
        db.execute("INSERT INTO portions (id,donation_id,recipient_id,quantity_kg,status,otp,created_at) VALUES (?,?,?,?,?,?,?)",
                   (portion_id, donation_id, recipient["id"], quantity, "offered", legacy_otp, stamp()))
        db.execute("UPDATE donations SET status=? WHERE id=?", ("matched" if quantity >= available - .01 else "open", donation_id))
        event(db, donation_id, "recipient_requested", f"{recipient['name']} requested {quantity:g} kg", portion_id)
        notification(db, donation["donor_id"], "recipient_request", "A recipient requested your donation", f"{recipient['name']} requested {quantity:g} kg of {donation['food_name']}.")
        return donation_view(db, donation_id)
