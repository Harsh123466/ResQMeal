import secrets
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from ..database import connect, now, stamp
from ..dependencies import current_user
from ..schemas import ConfirmIn
from ..services.common import donation_owned_by, donation_view, event, notification
from .donations import match_donation

router = APIRouter()


@router.post("/api/portions/{portion_id}/accept")
def accept_portion(portion_id: str, user: dict = Depends(current_user)):
    with connect() as db:
        portion=db.execute("SELECT * FROM portions WHERE id=?",(portion_id,)).fetchone()
        if not portion: raise HTTPException(404,"Allocation not found")
        if user["role"] != "recipient": raise HTTPException(403,"A recipient account must accept this allocation")
        org=db.execute("SELECT * FROM recipients WHERE id=?",(portion["recipient_id"],)).fetchone()
        if not org or org["user_id"] != user["id"]: raise HTTPException(403,"This allocation belongs to another organization")
        if portion["status"]!="offered": raise HTTPException(409,"Only offered allocations can be accepted")
        db.execute("UPDATE portions SET status='accepted' WHERE id=?", (portion_id,))
        event(db,portion["donation_id"],"accepted","Recipient accepted the allocation",portion_id)
        donor=db.execute("SELECT donor_id FROM donations WHERE id=?",(portion["donation_id"],)).fetchone()
        notification(db,donor["donor_id"],"allocation_accepted","Donation offer accepted",f"{org['name']} accepted {portion['quantity_kg']:g} kg.")
        return donation_view(db,portion["donation_id"])


@router.post("/api/portions/{portion_id}/cancel")
def cancel_portion(portion_id: str, user: dict = Depends(current_user)):
    with connect() as db:
        portion=db.execute("SELECT * FROM portions WHERE id=?",(portion_id,)).fetchone()
        if not portion: raise HTTPException(404,"Allocation not found")
        assigned=db.execute("SELECT user_id FROM recipients WHERE id=?",(portion["recipient_id"],)).fetchone()
        donation=donation_owned_by(db,portion["donation_id"],user) if user["role"]=="donor" else None
        if user["role"] not in ("donor","recipient","admin") or (user["role"]=="recipient" and (not assigned or assigned["user_id"]!=user["id"])):
            raise HTTPException(403,"You cannot cancel another organization’s allocation")
        if user["role"]=="admin": donation=db.execute("SELECT * FROM donations WHERE id=?",(portion["donation_id"],)).fetchone()
        if donation is None: donation=db.execute("SELECT * FROM donations WHERE id=?",(portion["donation_id"],)).fetchone()
        if portion["status"] not in ("offered","accepted"): raise HTTPException(409,"This allocation can no longer be cancelled")
        db.execute("UPDATE portions SET status='cancelled' WHERE id=?",(portion_id,))
        db.execute("UPDATE donations SET status='at_risk' WHERE id=?",(portion["donation_id"],))
        event(db,portion["donation_id"],"cancelled","Recipient cancelled; searching for a replacement",portion_id)
        notification(db,donation["donor_id"],"rescue_started","A donation needs a new recipient","A recipient cancelled. AnnSetu is looking for a replacement partner.")
    return match_donation(portion["donation_id"],user)


@router.post("/api/portions/{portion_id}/confirm")
def confirm_portion(portion_id: str, data: ConfirmIn, user: dict = Depends(current_user)):
    with connect() as db:
        if getattr(db, "is_postgres", False):
            portion=db.execute("SELECT * FROM portions WHERE id=? FOR UPDATE",(portion_id,)).fetchone()
        else:
            db.execute("BEGIN IMMEDIATE")
            portion=db.execute("SELECT * FROM portions WHERE id=?",(portion_id,)).fetchone()
        if not portion: raise HTTPException(404,"Allocation not found")
        donation=db.execute("SELECT * FROM donations WHERE id=?" + (" FOR UPDATE" if getattr(db, "is_postgres", False) else ""),(portion["donation_id"],)).fetchone()
        recipient=db.execute("SELECT * FROM recipients WHERE id=?",(portion["recipient_id"],)).fetchone()
        if data.step=="pickup" and (user["role"]!="donor" or donation["donor_id"]!=user["id"]):
            raise HTTPException(403,"Only the donor who listed this food can verify pickup")
        if data.step=="receipt" and user["role"]!="admin" and (user["role"]!="recipient" or recipient["user_id"]!=user["id"]):
            raise HTTPException(403,"The assigned recipient confirms receipt")
        if data.step == "receipt" and portion["status"] == "received":
            return donation_view(db, portion["donation_id"])
        if data.step=="pickup":
            if datetime.fromisoformat(donation["expires_at"]) <= now(): raise HTTPException(409,"The safe consumption deadline has passed; this food can no longer be picked up")
            if not secrets.compare_digest(donation["delivery_code"], data.code.upper()):
                raise HTTPException(400,"That confirmation code is not correct")
        if data.step=="pickup":
            if portion["status"]!="accepted": raise HTTPException(409,"Accept this allocation before confirming pickup")
            db.execute("UPDATE portions SET status='picked_up',pickup_at=?,pickup_confirmed_by=? WHERE id=?",(stamp(),user["id"],portion_id))
            event(db,portion["donation_id"],"picked_up","Donor verified the listing delivery code",portion_id)
            notification(db,recipient["user_id"],"pickup_confirmed","Pickup confirmed",f"Pickup for {donation['food_name']} was confirmed. Please confirm receipt after handover.")
        else:
            if portion["status"]!="picked_up": raise HTTPException(409,"Pickup must be confirmed before receipt")
            db.execute("UPDATE portions SET status='received',received_at=? WHERE id=?",(stamp(),portion_id))
            event(db,portion["donation_id"],"received","Recipient confirmed receipt",portion_id)
            notification(db,donation["donor_id"],"receipt_confirmed","Food handover complete",f"{recipient['name']} confirmed receipt of {portion['quantity_kg']:g} kg of {donation['food_name']}.")
            remaining=db.execute("SELECT COALESCE(SUM(quantity_kg),0) FROM portions WHERE donation_id=? AND status='received'",(portion["donation_id"],)).fetchone()[0]
            if remaining >= donation["quantity_kg"]-.01:
                db.execute("UPDATE donations SET status='completed' WHERE id=?",(portion["donation_id"],))
            else:
                reserved=db.execute("SELECT COALESCE(SUM(quantity_kg),0) FROM portions WHERE donation_id=? AND status IN ('offered','accepted','picked_up')",(portion["donation_id"],)).fetchone()[0]
                available=max(0,donation["quantity_kg"]-remaining-reserved)
                db.execute("UPDATE donations SET status=? WHERE id=?",("open" if available > .01 else "matched",portion["donation_id"]))
        return donation_view(db,portion["donation_id"])


