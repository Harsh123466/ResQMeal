from fastapi import APIRouter, Depends, HTTPException
from uuid import uuid4
from ..database import connect, stamp
from ..dependencies import current_user, user_public
from ..schemas import AuthLogin, AuthRegister
from ..services.common import notification
from ..security import hash_password, issue_token, verify_password

router = APIRouter()

@router.post("/api/auth/register", status_code=201)
def register(data: AuthRegister):
    email = data.email.strip().lower()
    uid = str(uuid4())
    with connect() as db:
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "An account with this email already exists")
        db.execute("INSERT INTO users (id,email,name,role,password_hash,created_at) VALUES (?,?,?,?,?,?)",
                   (uid, email, data.name.strip(), data.role, hash_password(data.password), stamp()))
        notification(db, uid, "account", "Welcome to AnnSetu", "Your account is ready. Complete your profile to get started.")
        user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return {"access_token": issue_token(uid, data.role), "token_type": "bearer", "user": user_public(user)}


@router.post("/api/auth/login")
def login(data: AuthLogin):
    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (data.email.strip().lower(),)).fetchone()
        if not user or not verify_password(data.password, user["password_hash"]):
            raise HTTPException(401, "Email or password is incorrect")
        if user["role"] not in ("donor", "recipient", "admin"):
            raise HTTPException(403, "This account role is no longer supported")
    return {"access_token": issue_token(user["id"], user["role"]), "token_type": "bearer", "user": user_public(user)}


@router.get("/api/auth/me")
def who_am_i(user: dict = Depends(current_user)):
    return user
