from fastapi import APIRouter, Depends
from ..database import connect
from ..dependencies import current_user

router = APIRouter()

@router.get("/api/impact")
def impact(user: dict = Depends(current_user)):
    with connect() as db:
        row=db.execute("""SELECT
          (SELECT COUNT(*) FROM donations) AS total,
          (SELECT COUNT(DISTINCT donation_id) FROM portions WHERE status='received') AS completed,
          (SELECT COALESCE(SUM(quantity_kg),0) FROM portions WHERE status='received') AS received_kg,
          (SELECT COALESCE(SUM(quantity_kg),0) FROM portions WHERE status IN ('picked_up','received')) AS rescued_kg,
          (SELECT COUNT(DISTINCT donation_id) FROM portions WHERE status='received') AS rescued,
          (SELECT COUNT(*) FROM donations WHERE status='at_risk') AS at_risk""").fetchone()
        total,completed,received_kg,rescued_kg,rescues,at_risk=(row[k] for k in (('total','completed','received_kg','rescued_kg','rescued','at_risk') if isinstance(row,dict) else range(6)))
        return {"donations_listed":total,"completed_donations":completed,"meals_redistributed":round(received_kg/.4),"quantity_redistributed_kg":round(rescued_kg,2),"estimated_food_waste_avoided_kg":round(rescued_kg,2),"estimated_co2e_avoided_kg":round(rescued_kg*2.5,2),"successful_rescues":rescues,"at_risk_donations":at_risk}
