import os
import tempfile
import unittest
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app import database, main


class AnnSetuWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.original_database_url = database.DATABASE_URL
        database.DATABASE_URL = ""
        main.DB_PATH = Path(cls.temp_dir.name) / "test.db"
        os.environ["ANNSETU_ADMIN_EMAIL"] = "admin@example.org"
        os.environ["ANNSETU_ADMIN_PASSWORD"] = "admin-password-123"
        cls.client_context = TestClient(main.app)
        cls.client = cls.client_context.__enter__()
        cls.admin = cls.login("admin@example.org", "admin-password-123")
        cls.donor = cls.register("donor@example.org", "Food Donor", "donor")
        cls.recipients = []
        for i in range(3):
            account = cls.register(f"org{i}@example.org", f"Community Kitchen {i}", "recipient")
            response = cls.client.post("/api/recipients", headers=account, json={
                "name": f"Community Kitchen {i}", "contact": "+91 90000 00000",
                "address": "Sector 62, Noida", "latitude": 28.627 + i * .001, "longitude": 77.372,
                "capacity_kg": 40, "categories": ["veg"], "storage": ["ambient", "hot", "chilled"],
                "hours": "Daily, 9 am-8 pm", "evidence": "Registration certificate submitted for review",
                "reference_id": f"DEMO-{i}",
            })
            if response.status_code != 201:
                raise AssertionError(response.text)
            cls.client.post(f"/api/admin/recipients/{response.json()['id']}/review", headers=cls.admin,
                            json={"decision": "approved", "note": "Test approval"})
            cls.recipients.append((account, response.json()))

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        database.DATABASE_URL = cls.original_database_url
        cls.temp_dir.cleanup()

    @classmethod
    def register(cls, email, name, role):
        response = cls.client.post("/api/auth/register", json={"email": email, "name": name, "password": "long-test-password", "role": role})
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    @classmethod
    def login(cls, email, password):
        response = cls.client.post("/api/auth/login", json={"email": email, "password": password})
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    def donation(self, quantity=20, storage="ambient", temperature=23):
        prepared = datetime.now(timezone.utc) - timedelta(minutes=10)
        expires = datetime.now(timezone.utc) + timedelta(hours=3)
        return {"donor_name": "Untrusted form name", "food_name": "Vegetable rice", "category": "veg",
                "quantity_kg": quantity, "prepared_at": prepared.isoformat(), "expires_at": expires.isoformat(),
                "storage": storage, "temperature_c": temperature, "pickup_address": "Sector 62, Noida",
                "latitude": 28.627, "longitude": 77.372, "notes": "Covered containers"}

    def create_donation(self, **kwargs):
        return self.client.post("/api/donations", headers=self.donor, json=self.donation(**kwargs))

    def test_auth_roles_and_health(self):
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")
        self.assertEqual(self.client.get("/api/donations").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/register", json={"email":"volunteer@example.org","name":"Pickup Volunteer","password":"long-test-password","role":"volunteer"}).status_code, 422)
        self.assertEqual(self.client.post("/api/auth/login", json={"email":"donor@example.org","password":"wrong-password"}).status_code, 401)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.donor).status_code, 200)

    def test_private_documents_upload_and_access_control(self):
        recipient_user, profile = self.recipients[0]
        upload = self.client.post(f"/api/recipients/{profile['id']}/documents", headers=recipient_user,
                                  files={"file": ("registration.txt", b"Community Kitchen 0 DEMO-0 Registration certificate", "text/plain")})
        self.assertEqual(upload.status_code, 201)
        self.assertEqual(self.client.get(f"/api/recipients/{profile['id']}/documents", headers=self.donor).status_code, 403)
        downloaded = self.client.get(f"/api/recipients/{profile['id']}/documents/{upload.json()['id']}", headers=recipient_user)
        self.assertEqual(downloaded.content, b"Community Kitchen 0 DEMO-0 Registration certificate")
        analysis = self.client.post(f"/api/admin/recipients/{profile['id']}/verify", headers=self.admin)
        self.assertEqual(analysis.status_code, 200)
        self.assertEqual(analysis.json()["report"]["workflow"], "LangGraph evidence review")
        agent_names = {check["agent"] for check in analysis.json()["report"]["checks"]}
        self.assertIn("Document extraction agent", agent_names)
        self.assertIn("AI document review agent", agent_names)
        self.assertEqual(analysis.json()["report"]["checks"][-1]["status"], "disabled")

    def test_image_only_pdf_uses_configured_ocr_adapter(self):
        recipient_user, profile = self.recipients[1]
        pdf = BytesIO()
        writer = PdfWriter(); writer.add_blank_page(width=300, height=300); writer.write(pdf)
        upload = self.client.post(f"/api/recipients/{profile['id']}/documents", headers=recipient_user,
                                  files={"file": ("scan.pdf", pdf.getvalue(), "application/pdf")})
        self.assertEqual(upload.status_code, 201)
        os.environ["ANNSETU_OCR_URL"] = "https://ocr.example.test/extract"
        class OCRResponse:
            def raise_for_status(self): pass
            def json(self): return {"text": "Community Kitchen 1 DEMO-1 registration"}
        try:
            with patch("app.api.recipients.httpx.post", return_value=OCRResponse()) as ocr_call:
                result = self.client.post(f"/api/admin/recipients/{profile['id']}/verify", headers=self.admin)
            self.assertEqual(result.status_code, 200)
            ocr_call.assert_called_once()
            extraction = next(c for c in result.json()["report"]["checks"] if c["agent"] == "Document extraction agent")
            self.assertIn("1 of 1", extraction["detail"])
        finally:
            os.environ.pop("ANNSETU_OCR_URL", None)

    def test_opt_in_ai_review_contributes_structured_agent_result(self):
        from app import verification_agents
        class ModelResponse:
            def raise_for_status(self): pass
            def json(self): return {"output":[{"type":"message","content":[{"type":"output_text","text":'{"summary":"Documents are internally consistent.","flags":[]}'}]}]}
        old = {key: os.environ.get(key) for key in ("ANNSETU_ENABLE_LLM_REVIEW", "OPENAI_API_KEY")}
        os.environ["ANNSETU_ENABLE_LLM_REVIEW"] = "true"; os.environ["OPENAI_API_KEY"] = "test-key"
        try:
            with patch("app.verification_agents.httpx.post", return_value=ModelResponse()) as model_call:
                report = verification_agents.run_verification(organization="Example Kitchen", reference_id="REF-42", evidence="Certificate attached", documents=[])
            self.assertIn("AI document review agent", {check["agent"] for check in report["checks"]})
            self.assertIn("consistent", next(check["detail"] for check in report["checks"] if check["agent"] == "AI document review agent"))
            self.assertEqual(model_call.call_args.kwargs["json"]["store"], False)
        finally:
            for key, value in old.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value

    def test_split_matching_and_listing_scoped_delivery_code(self):
        with main.connect() as db:
            db.execute("DELETE FROM portions")
            db.execute("UPDATE recipients SET verification_status='approved'")
        created = self.create_donation(quantity=300).json()
        matched = self.client.post(f"/api/donations/{created['id']}/match", headers=self.donor)
        self.assertEqual(matched.status_code, 200)
        portions = matched.json()["portions"]
        self.assertGreater(len(portions), 1, matched.json())
        self.assertGreater(sum(p["quantity_kg"] for p in portions), 0)
        self.assertLessEqual(sum(p["quantity_kg"] for p in portions), 300)
        code_response = self.client.get(f"/api/donations/{created['id']}/delivery-code", headers=self.donor)
        self.assertEqual(code_response.status_code, 200)
        self.assertEqual(code_response.headers["cache-control"], "no-store")
        code = code_response.json()["code"]
        self.assertEqual(len(code), 16)
        self.assertNotIn("delivery_code", matched.json())
        another = self.create_donation(quantity=10).json()
        another_code = self.client.get(f"/api/donations/{another['id']}/delivery-code", headers=self.donor).json()["code"]
        self.assertNotEqual(code, another_code)
        portion = portions[0]
        recipient_user = next(x[0] for x in self.recipients if x[1]["id"] == portion["recipient_id"])
        self.assertEqual(self.client.get(f"/api/donations/{created['id']}/delivery-code", headers=self.recipients[1][0]).status_code, 403)
        self.assertEqual(self.client.get(f"/api/donations/{created['id']}/delivery-code", headers=self.admin).json()["code"], code)
        self.assertEqual(self.client.post(f"/api/portions/{portion['id']}/accept", headers=recipient_user).status_code, 200)
        self.assertEqual(self.client.get(f"/api/donations/{created['id']}/delivery-code", headers=recipient_user).json()["code"], code)
        pickup = self.client.post(f"/api/portions/{portion['id']}/confirm", headers=self.donor, json={"step":"pickup","code":code})
        self.assertEqual(pickup.status_code, 200, pickup.text)
        picked_portion = next(p for p in pickup.json()["portions"] if p["id"] == portion["id"])
        self.assertEqual(picked_portion["pickup_confirmed_by"], self.client.get("/api/auth/me", headers=self.donor).json()["id"])
        self.assertEqual(self.client.get(f"/api/donations/{created['id']}/delivery-code", headers=self.donor).json()["code"], code)
        self.assertEqual(self.client.get("/api/volunteers", headers=self.admin).status_code, 404)
        received = self.client.post(f"/api/portions/{portion['id']}/confirm", headers=recipient_user, json={"step":"receipt","code":""})
        self.assertEqual(received.status_code, 200)
        self.assertIn("received", [e["event_type"] for e in received.json()["timeline"]])

    def test_recipient_can_request_and_receive_an_available_donation(self):
        with main.connect() as db:
            db.execute("DELETE FROM portions")
            db.execute("UPDATE recipients SET verification_status='approved'")
        created = self.create_donation(quantity=8).json()
        recipient_user, profile = self.recipients[0]
        request = self.client.post(f"/api/donations/{created['id']}/request", headers=recipient_user)
        self.assertEqual(request.status_code, 201)
        portion = next(p for p in request.json()["portions"] if p["recipient_id"] == profile["id"])
        self.assertEqual(portion["status"], "offered")
        self.assertEqual(self.client.post(f"/api/portions/{portion['id']}/accept", headers=recipient_user).status_code, 200)
        notice_items = self.client.get("/api/notifications", headers=self.donor).json()["items"]
        self.assertTrue(any(n["kind"] == "recipient_request" for n in notice_items))
        self.assertEqual(self.client.post(f"/api/donations/{created['id']}/request", headers=self.donor).status_code, 403)

    def test_cancellation_rescue_and_notifications(self):
        created = self.create_donation(quantity=12).json()
        matched = self.client.post(f"/api/donations/{created['id']}/match", headers=self.donor).json()
        cancelled = matched["portions"][0]
        recipient_user = next(x[0] for x in self.recipients if x[1]["id"] == cancelled["recipient_id"])
        rescued = self.client.post(f"/api/portions/{cancelled['id']}/cancel", headers=recipient_user)
        self.assertEqual(rescued.status_code, 200)
        self.assertNotIn(cancelled["recipient_id"], {p["recipient_id"] for p in rescued.json()["recommendations"]})
        self.assertTrue(self.client.get("/api/notifications", headers=self.donor).json()["items"])

    def test_unsafe_food_and_recipient_review_permissions(self):
        created = self.create_donation(quantity=10, storage="hot", temperature=54).json()
        result = self.client.post(f"/api/donations/{created['id']}/match", headers=self.donor).json()
        self.assertEqual(result["status"], "at_risk")
        response = self.client.post("/api/recipients", headers=self.donor, json={"name":"Blocked Org","contact":"+91 90000 00001","address":"Noida Sector 62","latitude":28.6,"longitude":77.3,"capacity_kg":10,"categories":["veg"],"storage":["ambient"],"hours":"daily","evidence":"Supporting documents supplied for review"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post(f"/api/admin/recipients/{self.recipients[0][1]['id']}/review", headers=self.donor, json={"decision":"rejected"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
