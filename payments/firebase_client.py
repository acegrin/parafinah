import firebase_admin
from firebase_admin import credentials, firestore, auth

# Only initialize once (avoid re-init when Django reloads)
if not firebase_admin._apps:
    cred = credentials.Certificate("/home/parafinah/parafinah_backend/firebase_key.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()
firebase_auth = auth
