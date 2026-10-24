from dataclasses import dataclass, field
from pathlib import Path
import os
import json
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("BM_DATA_DIR", "data")).resolve()
    )
    admin_password_hash: str = field(
        default_factory=lambda: os.getenv("BM_ADMIN_PASSWORD_HASH", "")
    )
    signer_token: str = field(default_factory=lambda: os.getenv("BM_SIGNER_TOKEN", ""))
    signer_thumbprint: str = field(
        default_factory=lambda: (
            os.getenv("BM_SIGNER_THUMBPRINT", "").replace(" ", "").upper()
        )
    )
    secure_cookie: bool = field(
        default_factory=lambda: os.getenv("BM_COOKIE_SECURE", "1") == "1"
    )
    send_enabled: bool = field(
        default_factory=lambda: os.getenv("BM_ALLOW_EXTERNAL_WRITES", "0") == "1"
    )
    base_url: str = field(
        default_factory=lambda: os.getenv(
            "BM_PUBLIC_URL", "https://marking.internal"
        ).rstrip("/")
    )
    true_url: str = field(
        default_factory=lambda: os.getenv(
            "BM_TRUE_URL", "https://markirovka.crpt.ru"
        ).rstrip("/")
    )
    oms_url: str = field(
        default_factory=lambda: os.getenv(
            "BM_OMS_URL", "https://suzgrid.crpt.ru"
        ).rstrip("/")
    )

    def __post_init__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for url in [self.true_url, self.oms_url]:
            p = urlparse(url)
            if (
                p.scheme != "https"
                or p.username
                or p.query
                or p.fragment
                or p.path not in ("", "/")
                or not any(
                    p.hostname and (p.hostname == h or p.hostname.endswith("." + h))
                    for h in ("crpt.ru", "crptech.ru")
                )
            ):
                raise ValueError(
                    "Адреса интеграций: только HTTPS-хосты crpt.ru/crptech.ru без пути"
                )

    def secrets(self):
        path = self.data_dir / "tokens.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def save_token(self, kind, token, expires=None):
        value = self.secrets()
        value[kind] = {
            "token": token,
            "expires": expires,
            "acquired": datetime.now(timezone.utc).isoformat(),
        }
        fd, name = tempfile.mkstemp(dir=self.data_dir, prefix=".tokens-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(value, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.data_dir / "tokens.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)
