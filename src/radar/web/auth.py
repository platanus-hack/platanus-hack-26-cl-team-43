"""Magic-link login and signed session cookie helpers."""
from itsdangerous import BadData, URLSafeTimedSerializer

COOKIE = "radar_session"
LOGIN_MAX_AGE = 15 * 60
SESSION_MAX_AGE = 7 * 24 * 3600


def _ser(secret_key: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=salt)


def is_allowed(settings, email: str) -> bool:
    e = email.strip().lower()
    return bool(e) and e in {a.strip().lower() for a in settings.allowed_emails}


def make_login_token(settings, email: str) -> str:
    return _ser(settings.secret_key, "login").dumps(email.strip().lower())


def read_login_token(settings, token: str) -> str | None:
    try:
        return _ser(settings.secret_key, "login").loads(token, max_age=LOGIN_MAX_AGE)
    except BadData:
        return None


def make_session(settings, email: str) -> str:
    return _ser(settings.secret_key, "session").dumps(email.strip().lower())


def read_session(settings, value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _ser(settings.secret_key, "session").loads(value, max_age=SESSION_MAX_AGE)
    except BadData:
        return None
