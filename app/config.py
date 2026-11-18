import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    LAB_NAME = "血缘水印台"
    SECRET = os.environ.get("SECRET_KEY", "cc-watermark-dev-secret")
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    DEFAULT_DB = str(ROOT / "instance" / "watermark_lab.db")

    # 追索深度限制：默认只放行 2 层，超深下游一律拦截。
    DEFAULT_DEPTH_LIMIT = 2
    MIN_DEPTH_LIMIT = 1
    MAX_DEPTH_LIMIT = 8
    DEPTH_HARD_CAP = 100

    @classmethod
    def database(cls) -> str:
        return os.environ.get("DATABASE_PATH", cls.DEFAULT_DB)
