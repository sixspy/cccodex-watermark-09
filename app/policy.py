"""深度绑码（credential）的唯一编码口径。

本仓选择第二条凭证路线：**版本号写入旁路版本表，并在事务内判定**
（见 ``app/services/depth_policy.py``），而不是把深度配置落成不可变文件哈希。

绑码 = ``DP-v{版号}-{内容哈希前 12 位}``；内容哈希覆盖版号与深度上限，
因此同一版号永远对应同一哈希，任何一处看到绑码不同即说明吃了旧深度。
"""

import hashlib


def policy_hash(version_no: int, depth_limit: int) -> str:
    raw = f"lineage-trace-depth|v{version_no}|limit{depth_limit}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def binding_code(version_no: int, content_hash: str) -> str:
    return f"DP-v{version_no}-{content_hash[:12]}"
