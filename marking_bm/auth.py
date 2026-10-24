import base64
import hashlib
import hmac
import secrets
import time


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return (
        "scrypt$"
        + base64.b64encode(salt).decode()
        + "$"
        + base64.b64encode(digest).decode()
    )


def verify_password(password, encoded):
    try:
        kind, salt, digest = encoded.split("$")
        if kind != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=16384, r=8, p=1
        )
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def new_session(db, username, admin):
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
    db.execute(
        "INSERT INTO sessions VALUES(?,?,?,?,?)",
        (digest(token), username, int(admin), csrf, time.time() + 8 * 3600),
    )
    return token, csrf
