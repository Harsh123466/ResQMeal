import hashlib
import json
import os
import re
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from cryptography.fernet import InvalidToken
from pypdf import PdfReader
from ..database import MAX_COLLECTION_RADIUS_KM, connect, stamp
from ..dependencies import admin_user, current_user, document_cipher
from ..schemas import RecipientIn, ReviewIn
from ..services.common import distance_km, event, notification, recipient_owned_by, row_dict
from ..verification_agents import run_verification

router = APIRouter()

@router.get("/api/recipients")
def recipients(status: str | None = None, user: dict = Depends(current_user)):
    with connect() as db:
        query = "SELECT * FROM recipients"
        args: tuple = ()
        if user["role"] == "recipient":
            query += " WHERE user_id=?"
            args = (user["id"],)
        elif user["role"] == "donor":
            query += " WHERE verification_status='approved'"
        elif user["role"] != "admin":
            query += " WHERE verification_status='approved'"
        if status:
            query += " AND verification_status = ?" if " WHERE " in query else " WHERE verification_status = ?"
            args = (*args, status)
        rows = [row_dict(r) for r in db.execute(query + " ORDER BY name", args)]
        if user["role"] == "donor":
            locations = db.execute("SELECT lat,lon FROM donations WHERE donor_id=?", (user["id"],)).fetchall()
            if not locations:
                return []
            nearby = []
            for recipient in rows:
                distance = min(distance_km(location["lat"], location["lon"], recipient["lat"], recipient["lon"]) for location in locations)
                if distance <= MAX_COLLECTION_RADIUS_KM:
                    recipient["distance_km"] = round(distance, 1)
                    nearby.append(recipient)
            return nearby
        return rows


@router.post("/api/recipients", status_code=201)
def create_recipient(data: RecipientIn, user: dict = Depends(current_user)):
    if user["role"] != "recipient":
        raise HTTPException(403, "Sign in with a recipient account to register an organization")
    rid = str(uuid4())
    with connect() as db:
        if db.execute("SELECT 1 FROM recipients WHERE user_id=?", (user["id"],)).fetchone():
            raise HTTPException(409, "This account already has an organization profile")
        db.execute("""INSERT INTO recipients
          (id,name,contact,address,lat,lon,capacity_kg,categories,storage,hours,verification_status,evidence,reference_id,created_at,user_id)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (rid,data.name,data.contact,data.address,data.latitude,data.longitude,data.capacity_kg,json.dumps(data.categories),json.dumps(data.storage),data.hours,"pending",data.evidence,data.reference_id,stamp(),user["id"]))
        event(db, "", "verification_submitted", f"{data.name} submitted documents for admin review")
        row = db.execute("SELECT * FROM recipients WHERE id=?", (rid,)).fetchone()
    return row_dict(row)


@router.post("/api/recipients/{recipient_id}/documents", status_code=201)
async def upload_recipient_document(recipient_id: str, file: UploadFile = File(...), user: dict = Depends(current_user)):
    with connect() as db:
        recipient = recipient_owned_by(db, recipient_id, user)
        if user["role"] not in ("recipient", "admin"):
            raise HTTPException(403, "Recipient or administrator access is required")
        if user["role"] == "recipient" and recipient["user_id"] != user["id"]:
            raise HTTPException(403, "You can only upload documents for your own organization")
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".txt"):
        raise HTTPException(415, "Upload a PDF or plain text document")
    content = await file.read(8 * 1024 * 1024 + 1)
    await file.close()
    if not content or len(content) > 8 * 1024 * 1024:
        raise HTTPException(413, "Documents must be between 1 byte and 8 MB")
    if suffix == ".pdf":
        try:
            reader = PdfReader(BytesIO(content))
            extracted = "\n".join(page.extract_text() or "" for page in reader.pages).strip()[:100_000]
        except Exception as exc:
            raise HTTPException(400, "This PDF could not be read; try exporting it again") from exc
    else:
        try:
            extracted = content.decode("utf-8-sig").strip()[:100_000]
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "Text documents must use UTF-8 encoding") from exc
    cipher = document_cipher()
    document_id = str(uuid4())
    with connect() as db:
        db.execute("""INSERT INTO documents
          (id,recipient_id,owner_id,filename,content_type,encrypted_data,sha256,extracted_text,created_at)
          VALUES (?,?,?,?,?,?,?,?,?)""",
          (document_id,recipient_id,user["id"],filename,
           "application/pdf" if suffix == ".pdf" else "text/plain",
           cipher.encrypt(content),hashlib.sha256(content).hexdigest(),cipher.encrypt(extracted.encode("utf-8")),stamp()))
        db.execute("UPDATE recipients SET verification_status='pending',verification_report='{}' WHERE id=?", (recipient_id,))
        event(db, "", "verification_document_uploaded", f"A private document was uploaded for {recipient['name']}")
    return {"id":document_id,"filename":filename,"content_type":"application/pdf" if suffix==".pdf" else "text/plain","sha256":hashlib.sha256(content).hexdigest(),"created_at":stamp()}


@router.get("/api/recipients/{recipient_id}/documents")
def list_recipient_documents(recipient_id: str, user: dict = Depends(current_user)):
    with connect() as db:
        recipient_owned_by(db, recipient_id, user)
        return [dict(row) for row in db.execute("SELECT id,filename,content_type,sha256,created_at FROM documents WHERE recipient_id=? ORDER BY created_at DESC", (recipient_id,))]


@router.get("/api/recipients/{recipient_id}/documents/{document_id}")
def download_recipient_document(recipient_id: str, document_id: str, user: dict = Depends(current_user)):
    with connect() as db:
        recipient_owned_by(db, recipient_id, user)
        row = db.execute("SELECT * FROM documents WHERE id=? AND recipient_id=?", (document_id,recipient_id)).fetchone()
        if not row:
            raise HTTPException(404, "Document not found")
    try:
        content = document_cipher().decrypt(row["encrypted_data"])
    except InvalidToken as exc:
        raise HTTPException(500, "Document decryption failed; check ANNSETU_DOCUMENT_KEY") from exc
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", row["filename"])
    return Response(content, media_type=row["content_type"], headers={"Content-Disposition": f'attachment; filename="{safe_name}"', "X-Content-Type-Options": "nosniff"})


@router.delete("/api/recipients/{recipient_id}/documents/{document_id}")
def delete_recipient_document(recipient_id: str, document_id: str, user: dict = Depends(current_user)):
    with connect() as db:
        recipient_owned_by(db, recipient_id, user)
        result = db.execute("DELETE FROM documents WHERE id=? AND recipient_id=?", (document_id,recipient_id))
        if not result.rowcount:
            raise HTTPException(404,"Document not found")
        db.execute("UPDATE recipients SET verification_report='{}',verification_status='pending' WHERE id=?",(recipient_id,))
    return {"ok":True}


@router.get("/api/admin/recipients/{recipient_id}/documents")
def admin_documents(recipient_id: str, user: dict = Depends(admin_user)):
    with connect() as db:
        recipient = recipient_owned_by(db, recipient_id, user)
        return [dict(row) for row in db.execute("SELECT id,filename,content_type,sha256,created_at FROM documents WHERE recipient_id=? ORDER BY created_at DESC", (recipient_id,))]


@router.post("/api/admin/recipients/{recipient_id}/verify")
def verify_recipient_documents(recipient_id: str, user: dict = Depends(admin_user)):
    with connect() as db:
        recipient = recipient_owned_by(db, recipient_id, user)
        docs = db.execute("SELECT id,filename,encrypted_data,extracted_text FROM documents WHERE recipient_id=?", (recipient_id,)).fetchall()
        cipher = document_cipher()
        extracted = []
        for doc in docs:
            try:
                text = cipher.decrypt(doc["extracted_text"]).decode("utf-8") if doc["extracted_text"] else ""
            except (InvalidToken, UnicodeDecodeError):
                text = ""
            ocr_endpoint = os.getenv("ANNSETU_OCR_URL", "").strip()
            if not text and ocr_endpoint and doc["filename"].lower().endswith(".pdf"):
                try:
                    original = cipher.decrypt(doc["encrypted_data"])
                    headers = {"Content-Type": "application/pdf"}
                    ocr_token = os.getenv("ANNSETU_OCR_TOKEN", "").strip()
                    if ocr_token:
                        headers["Authorization"] = f"Bearer {ocr_token}"
                    result = httpx.post(ocr_endpoint, content=original, headers=headers, timeout=25)
                    result.raise_for_status()
                    text = str(result.json().get("text", "")).strip()[:100_000]
                except Exception:
                    text = ""
            extracted.append({"filename":doc["filename"],"text":text})
        report = run_verification(organization=recipient["name"],reference_id=recipient["reference_id"],evidence=recipient["evidence"],documents=extracted)
        db.execute("UPDATE recipients SET verification_report=? WHERE id=?", (json.dumps(report),recipient_id))
        event(db,"","verification_analyzed",f"Verification workflow completed for {recipient['name']}")
        return {"recipient":row_dict(db.execute("SELECT * FROM recipients WHERE id=?",(recipient_id,)).fetchone()),"report":report}


@router.post("/api/admin/recipients/{recipient_id}/review")
def review_recipient(recipient_id: str, data: ReviewIn, user: dict = Depends(admin_user)):
    with connect() as db:
        recipient = db.execute("SELECT * FROM recipients WHERE id=?", (recipient_id,)).fetchone()
        if not recipient:
            raise HTTPException(404, "Recipient not found")
        if recipient["verification_status"] != "pending":
            raise HTTPException(409, "Only pending applications can be reviewed")
        db.execute("UPDATE recipients SET verification_status=? WHERE id=?", (data.decision, recipient_id))
        event(db, "", "verification_reviewed", f"{recipient['name']}: {data.decision}. {data.note}".strip())
        if recipient["user_id"]:
            notification(db,recipient["user_id"],"verification",f"Organization {data.decision}",data.note or f"Your organization was {data.decision} after administrator review.")
        return row_dict(db.execute("SELECT * FROM recipients WHERE id=?", (recipient_id,)).fetchone())
