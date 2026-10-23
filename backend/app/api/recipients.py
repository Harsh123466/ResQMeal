import hashlib
import html
import json
import os
import re
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from cryptography.fernet import InvalidToken
from starlette.concurrency import run_in_threadpool
from ..database import MAX_COLLECTION_RADIUS_KM, connect, stamp
from ..dependencies import admin_user, current_user, document_cipher
from ..schemas import RecipientIn, ReviewIn
from ..services.common import distance_km, event, notification, recipient_owned_by, row_dict
from ..verification_agents import extract_uploaded_document, run_verification

router = APIRouter()

def recipient_response(row):
    item = row_dict(row)
    encrypted_pan = item.pop("pan_encrypted", None)
    item["has_pan_number"] = bool(encrypted_pan)
    return item

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
        rows = [recipient_response(r) for r in db.execute(query + " ORDER BY name", args)]
        if user["role"] == "donor":
            for recipient in rows:
                for private_field in ("evidence", "reference_id", "darpan_id", "has_pan_number", "fcra_or_80g", "daily_meal_capacity", "verification_report"):
                    recipient.pop(private_field, None)
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
    encrypted_pan = document_cipher().encrypt(data.pan_number.strip().upper().encode("utf-8")) if data.pan_number.strip() else None
    with connect() as db:
        if db.execute("SELECT 1 FROM recipients WHERE user_id=?", (user["id"],)).fetchone():
            raise HTTPException(409, "This account already has an organization profile")
        db.execute("""INSERT INTO recipients
          (id,name,contact,address,lat,lon,capacity_kg,categories,storage,hours,verification_status,evidence,reference_id,darpan_id,created_at,user_id,organization_type,pan_encrypted,fcra_or_80g,daily_meal_capacity)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (rid,data.name,data.contact,data.address,data.latitude,data.longitude,data.capacity_kg,json.dumps(data.categories),json.dumps(data.storage),data.hours,"pending",data.evidence,data.reference_id,data.darpan_id.strip().upper(),stamp(),user["id"],data.organization_type,encrypted_pan,data.fcra_or_80g.strip(),data.daily_meal_capacity))
        event(db, "", "verification_submitted", f"{data.name} submitted documents for admin review")
        row = db.execute("SELECT * FROM recipients WHERE id=?", (rid,)).fetchone()
    return recipient_response(row)


@router.put("/api/recipients/{recipient_id}")
def resubmit_recipient(recipient_id: str, data: RecipientIn, user: dict = Depends(current_user)):
    if user["role"] != "recipient":
        raise HTTPException(403, "Sign in with the organization account to update its application")
    with connect() as db:
        recipient = recipient_owned_by(db, recipient_id, user)
        if recipient["verification_status"] not in ("rejected", "pending"):
            raise HTTPException(409, "Only rejected or pending applications can be corrected and resubmitted")
        encrypted_pan = document_cipher().encrypt(data.pan_number.strip().upper().encode("utf-8")) if data.pan_number.strip() else None
        db.execute("""UPDATE recipients SET
          name=?,organization_type=?,contact=?,address=?,lat=?,lon=?,capacity_kg=?,categories=?,storage=?,hours=?,
          verification_status='pending',evidence=?,reference_id=?,darpan_id=?,
          pan_encrypted=COALESCE(?,pan_encrypted),fcra_or_80g=?,daily_meal_capacity=?,verification_report='{}'
          WHERE id=? AND user_id=?""",
          (data.name,data.organization_type,data.contact,data.address,data.latitude,data.longitude,data.capacity_kg,
           json.dumps(data.categories),json.dumps(data.storage),data.hours,data.evidence,data.reference_id,
           data.darpan_id.strip().upper(),encrypted_pan,data.fcra_or_80g.strip(),data.daily_meal_capacity,
           recipient_id,user["id"]))
        event(db, "", "verification_resubmitted", f"{data.name} corrected and resubmitted its organization application")
        updated = db.execute("SELECT * FROM recipients WHERE id=?", (recipient_id,)).fetchone()
    return recipient_response(updated)


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
    allowed_extensions = {".pdf", ".txt", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
    if suffix not in allowed_extensions:
        raise HTTPException(415, "Upload a PDF, UTF-8 text file, or supported image")
    content = await file.read(12 * 1024 * 1024 + 1)
    await file.close()
    if not content or len(content) > 12 * 1024 * 1024:
        raise HTTPException(413, "Documents must be between 1 byte and 12 MB")
    extracted, extraction_method, extraction_error = await run_in_threadpool(extract_uploaded_document, content, filename)
    cipher = document_cipher()
    document_id = str(uuid4())
    with connect() as db:
        document_count = db.execute("SELECT COUNT(*) FROM documents WHERE recipient_id=?", (recipient_id,)).fetchone()[0]
        if document_count >= 8:
            raise HTTPException(413, "An organization can upload up to 8 supporting documents")
        media_type = "application/pdf" if suffix == ".pdf" else "text/plain" if suffix == ".txt" else f"image/{'jpeg' if suffix in ('.jpg', '.jpeg') else 'tiff' if suffix in ('.tif', '.tiff') else suffix[1:]}"
        db.execute("""INSERT INTO documents
          (id,recipient_id,owner_id,filename,content_type,encrypted_data,sha256,extracted_text,created_at,extraction_method,extraction_error)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
          (document_id,recipient_id,user["id"],filename,
           media_type, cipher.encrypt(content),hashlib.sha256(content).hexdigest(),
           cipher.encrypt(extracted.encode("utf-8")),stamp(),extraction_method,extraction_error))
        db.execute("UPDATE recipients SET verification_status='pending',verification_report='{}' WHERE id=?", (recipient_id,))
        event(db, "", "verification_document_uploaded", f"A private document was uploaded for {recipient['name']}")
    return {"id":document_id,"filename":filename,"content_type":media_type,"sha256":hashlib.sha256(content).hexdigest(),"extraction_method":extraction_method,"extraction_error":extraction_error,"created_at":stamp()}


@router.get("/api/recipients/{recipient_id}/documents")
def list_recipient_documents(recipient_id: str, user: dict = Depends(current_user)):
    with connect() as db:
        recipient_owned_by(db, recipient_id, user)
        return [dict(row) for row in db.execute("SELECT id,filename,content_type,sha256,created_at,extraction_method,extraction_error FROM documents WHERE recipient_id=? ORDER BY created_at DESC", (recipient_id,))]


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
        return [dict(row) for row in db.execute("SELECT id,filename,content_type,sha256,created_at,extraction_method,extraction_error FROM documents WHERE recipient_id=? ORDER BY created_at DESC", (recipient_id,))]


@router.post("/api/admin/recipients/{recipient_id}/verify")
def verify_recipient_documents(recipient_id: str, user: dict = Depends(admin_user)):
    with connect() as db:
        recipient = recipient_owned_by(db, recipient_id, user)
        docs = db.execute("SELECT id,filename,encrypted_data,extracted_text,extraction_method,extraction_error FROM documents WHERE recipient_id=?", (recipient_id,)).fetchall()
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
            extraction_error = doc["extraction_error"] or ("No readable text was extracted from this document." if not text else "")
            extracted.append({"filename":doc["filename"],"text":text,"extraction_method":doc["extraction_method"],"extraction_error":extraction_error})
        try:
            pan_number = cipher.decrypt(recipient["pan_encrypted"]).decode("utf-8") if recipient["pan_encrypted"] else ""
        except (InvalidToken, UnicodeDecodeError):
            pan_number = ""
        report = run_verification(
            organization=recipient["name"],
            reference_id=recipient["reference_id"],
            evidence=recipient["evidence"],
            documents=extracted,
            organization_type=recipient["organization_type"],
            darpan_id=recipient["darpan_id"],
            pan_number=pan_number,
            fcra_or_80g=recipient["fcra_or_80g"],
            daily_meal_capacity=recipient["daily_meal_capacity"],
            address=recipient["address"],
        )
        db.execute("UPDATE recipients SET verification_report=? WHERE id=?", (json.dumps(report),recipient_id))
        event(db,"","verification_analyzed",f"Verification workflow completed for {recipient['name']}")
        return {"recipient":recipient_response(db.execute("SELECT * FROM recipients WHERE id=?",(recipient_id,)).fetchone()),"report":report}


@router.get("/api/admin/recipients/{recipient_id}/verification-report.pdf")
def download_verification_report(recipient_id: str, user: dict = Depends(admin_user)):
    with connect() as db:
        recipient = db.execute("SELECT * FROM recipients WHERE id=?", (recipient_id,)).fetchone()
        if not recipient:
            raise HTTPException(404, "Organization not found")
        try:
            report = json.loads(recipient["verification_report"] or "{}")
        except json.JSONDecodeError:
            report = {}
        docs = [dict(row) for row in db.execute(
            "SELECT filename,extraction_method,extraction_error FROM documents WHERE recipient_id=? ORDER BY created_at",
            (recipient_id,),
        )]
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise HTTPException(503, "Install reportlab from backend/requirements.txt to export PDF reports") from exc

    def safe(value):
        return html.escape(str(value if value is not None else "Not provided").encode("ascii", "replace").decode("ascii"), quote=True).replace("\n", "<br/>")

    buffer = BytesIO()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="AnnSetuSmall", parent=styles["BodyText"], fontSize=8, leading=11, textColor=colors.HexColor("#52615e")))
    story = [Paragraph("AnnSetu AI - Organization verification", styles["Title"]), Paragraph("Confidential administrator review report", styles["AnnSetuSmall"]), Spacer(1, 8)]
    overview = [
        ("Organization", recipient["name"]),
        ("Type", recipient["organization_type"].replace("_", " ").title()),
        ("Application status", recipient["verification_status"]),
        ("Registration reference", recipient["reference_id"] or "Not provided"),
        ("NGO Darpan ID", recipient["darpan_id"] or "Not provided"),
        ("Human review", "Administrator decision required"),
    ]
    overview_rows = [[Paragraph(safe(label), styles["AnnSetuSmall"]), Paragraph(safe(value), styles["BodyText"])] for label, value in overview]
    story.append(Table(overview_rows, colWidths=[42*mm, 130*mm], style=TableStyle([
        ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#f0f6f3")), ("BOX", (0,0), (-1,-1), .5, colors.HexColor("#dce6e2")),
        ("INNERGRID", (0,0), (-1,-1), .3, colors.HexColor("#e5ece9")), ("VALIGN", (0,0), (-1,-1), "TOP"),
    ])))
    story.extend([Spacer(1, 8), Paragraph("Evidence summary", styles["Heading2"]), Paragraph(safe(report.get("summary") or report.get("recommendation") or "Analyze this application in the verification desk to generate findings."), styles["BodyText"])])
    if report.get("trust_score") is not None:
        story.append(Paragraph(safe(f"TrustVerify evidence consistency: {report['trust_score']}/100 ({report.get('risk_level', 'unassessed')} flagged risk). This is not identity or registration confirmation."), styles["BodyText"]))
    story.extend([Spacer(1, 6), Paragraph("Document field checks", styles["Heading2"])])
    checks = report.get("document_field_checks") or []
    if checks:
        rows = [[Paragraph("Field", styles["AnnSetuSmall"]), Paragraph("Result", styles["AnnSetuSmall"]), Paragraph("Details", styles["AnnSetuSmall"])]]
        rows.extend([[Paragraph(safe(check.get("field")), styles["AnnSetuSmall"]), Paragraph(safe(check.get("result")), styles["AnnSetuSmall"]), Paragraph(safe(check.get("detail")), styles["AnnSetuSmall"])] for check in checks])
        story.append(Table(rows, colWidths=[38*mm, 27*mm, 107*mm], repeatRows=1, style=TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f0f6f3")), ("BOX", (0,0), (-1,-1), .5, colors.HexColor("#dce6e2")),
            ("INNERGRID", (0,0), (-1,-1), .3, colors.HexColor("#e5ece9")), ("VALIGN", (0,0), (-1,-1), "TOP"),
        ])))
    else:
        story.append(Paragraph("No document field checks have been recorded.", styles["BodyText"]))
    story.extend([Spacer(1, 6), Paragraph("Agent-by-agent results", styles["Heading2"])])
    agent_results = report.get("agent_results") or []
    if not agent_results:
        agent_results = [
            {"agent": check.get("agent", "Verification agent"), "status": check.get("status", "unknown"), "detail": check.get("detail", "No details returned."), "source": "local LangGraph", "result": {}}
            for check in report.get("checks", [])
        ]
        external_result = report.get("external_agents") or {}
        agent_results.extend([
            {"agent": "Groq document extraction agent", "status": "completed" if external_result.get("extraction") else external_result.get("status", "not_run"), "detail": external_result.get("summary", "No result recorded."), "source": "Groq", "result": external_result.get("extraction") or {}},
            {"agent": "Tavily government-domain search agent", "status": "sources_found" if external_result.get("sources") else external_result.get("status", "not_run"), "detail": f"{len(external_result.get('sources') or [])} official-domain search result(s).", "source": "Tavily", "result": {"source_scope": external_result.get("source_scope", []), "sources": external_result.get("sources", [])}},
            {"agent": "Groq government evidence review agent", "status": (external_result.get("review") or {}).get("status", "not_run"), "detail": (external_result.get("review") or {}).get("summary", "Review was not run."), "source": "Groq", "result": external_result.get("review") or {}},
        ])
    if agent_results:
        def add_agent_value(label, value):
            pretty_label = label.replace("_", " ").strip().title()
            if value is None or value == "":
                story.append(Paragraph(safe(f"{pretty_label}: Not found or not returned"), styles["AnnSetuSmall"]))
            elif isinstance(value, dict):
                if not value:
                    story.append(Paragraph(safe(f"{pretty_label}: No additional details"), styles["AnnSetuSmall"]))
                for key, nested in value.items():
                    add_agent_value(f"{pretty_label} / {key}", nested)
            elif isinstance(value, (list, tuple)):
                if not value:
                    story.append(Paragraph(safe(f"{pretty_label}: None reported"), styles["AnnSetuSmall"]))
                for index, nested in enumerate(value, 1):
                    add_agent_value(f"{pretty_label} {index}", nested)
            else:
                story.append(Paragraph(safe(f"{pretty_label}: {value}"), styles["AnnSetuSmall"]))

        for result in agent_results:
            status = str(result.get("status", "unknown")).replace("_", " ").title()
            heading = f"{result.get('agent', 'Verification agent')} - {status}"
            if result.get("source"):
                heading += f" ({result['source']})"
            story.append(Paragraph(safe(heading), styles["Heading3"]))
            story.append(Paragraph(safe(result.get("detail") or "No detail returned by this agent."), styles["AnnSetuSmall"]))
            details = result.get("result") or {}
            if isinstance(details, dict):
                for key, value in details.items():
                    add_agent_value(key, value)
    else:
        story.append(Paragraph("No agent-level results are recorded. Analyze the application to create a complete report.", styles["BodyText"]))
    story.extend([Spacer(1, 6), Paragraph("Uploaded documents", styles["Heading2"])])
    for doc in docs:
        detail = f"{doc['filename']} - {doc['extraction_method'] or 'Extraction not run'}"
        if doc["extraction_error"]:
            detail += f": {doc['extraction_error']}"
        story.append(Paragraph(safe(detail), styles["AnnSetuSmall"]))
    if not docs:
        story.append(Paragraph("No registration documents were uploaded.", styles["BodyText"]))
    external = report.get("external_agents") or {}
    story.extend([Spacer(1, 6), Paragraph("Government-domain research", styles["Heading2"]), Paragraph(safe(external.get("summary") or "No external research result is recorded."), styles["BodyText"])])
    if external.get("review"):
        for label, key in (("Conflict", "conflicts"), ("Check not completed", "checks_not_completed")):
            for item in external["review"].get(key, []):
                story.append(Paragraph(safe(f"{label}: {item}"), styles["AnnSetuSmall"]))
    for source in external.get("sources", []):
        host = (urlparse(source.get("url", "")).hostname or "").lower()
        if host == "gov.in" or host.endswith(".gov.in") or host == "nic.in" or host.endswith(".nic.in"):
            story.append(Paragraph(safe(f"{source.get('title')}: {source.get('url')}"), styles["AnnSetuSmall"]))
    story.extend([Spacer(1, 8), Paragraph("Limitations", styles["Heading2"]), Paragraph("OCR may omit or misread text. AI extraction and web search are supporting evidence only. Search results are leads, not direct registry confirmation. A missing result does not prove an organization is unregistered. The final decision belongs to a human administrator.", styles["AnnSetuSmall"])])
    SimpleDocTemplate(buffer, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm, topMargin=17*mm, bottomMargin=20*mm, title="Organization verification review", author="AnnSetu AI").build(story)
    return Response(buffer.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="annsetu-verification-{recipient_id[:8]}.pdf"', "Cache-Control": "no-store"})


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
        return recipient_response(db.execute("SELECT * FROM recipients WHERE id=?", (recipient_id,)).fetchone())
