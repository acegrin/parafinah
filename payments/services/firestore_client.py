import firebase_admin
from firebase_admin import credentials, firestore
from django.conf import settings

_app = None
_db = None


def get_firestore():
    global _app, _db

    if _db:
        return _db

    if not firebase_admin._apps:
        cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT)
        _app = firebase_admin.initialize_app(cred)

    _db = firestore.client()
    return _db
