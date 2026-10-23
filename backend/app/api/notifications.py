from fastapi import APIRouter, Depends, HTTPException
from ..database import connect, stamp
from ..dependencies import current_user

router = APIRouter()

@router.get("/api/notifications")
def get_notifications(user: dict = Depends(current_user)):
    with connect() as db:
        items = [dict(row) for row in db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],))]
        unread = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (user["id"],)).fetchone()[0]
        return {"items": items, "unread": unread}


@router.post("/api/notifications/{notification_id}/read")
def read_notification(notification_id: str, user: dict = Depends(current_user)):
    with connect() as db:
        result = db.execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=?", (stamp(), notification_id, user["id"]))
        if result.rowcount == 0:
            raise HTTPException(404, "Notification not found")
        return {"ok": True}
