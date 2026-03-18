"""30天试用期验证模块"""

import json
import ntplib
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet

from config import TRIAL_DAYS, NTP_SERVERS, TRIAL_DATA_FILE
from machine_id import get_encryption_key


def _get_data_dir() -> Path:
    """获取数据存储目录"""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    data_dir = base / "pdf2dxf"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _get_ntp_time() -> float | None:
    """从NTP服务器获取当前时间戳"""
    client = ntplib.NTPClient()
    for server in NTP_SERVERS:
        try:
            response = client.request(server, timeout=3)
            return response.tx_time
        except Exception:
            continue
    return None


def _encrypt_data(data: dict) -> bytes:
    """加密试用数据"""
    fernet = Fernet(get_encryption_key())
    json_bytes = json.dumps(data).encode()
    return fernet.encrypt(json_bytes)


def _decrypt_data(encrypted: bytes) -> dict | None:
    """解密试用数据"""
    try:
        fernet = Fernet(get_encryption_key())
        json_bytes = fernet.decrypt(encrypted)
        return json.loads(json_bytes.decode())
    except Exception:
        return None


def _load_trial_data() -> dict | None:
    """读取本地试用数据"""
    data_file = _get_data_dir() / TRIAL_DATA_FILE
    if not data_file.exists():
        return None
    try:
        encrypted = data_file.read_bytes()
        return _decrypt_data(encrypted)
    except Exception:
        return None


def _save_trial_data(data: dict):
    """保存试用数据到本地"""
    data_file = _get_data_dir() / TRIAL_DATA_FILE
    encrypted = _encrypt_data(data)
    data_file.write_bytes(encrypted)


def check_trial() -> tuple[bool, int, str]:
    """
    检查试用期状态

    Returns:
        (is_valid, remaining_days, message)
        - is_valid: 是否可以使用
        - remaining_days: 剩余天数
        - message: 状态描述
    """
    # 1. 尝试获取NTP时间
    ntp_time = _get_ntp_time()
    trial_data = _load_trial_data()

    if ntp_time is not None:
        current_time = ntp_time

        # 首次运行，记录安装时间
        if trial_data is None:
            trial_data = {
                "install_time": current_time,
                "last_check": current_time,
            }
            _save_trial_data(trial_data)
            return True, TRIAL_DAYS, f"试用期已激活，有效期 {TRIAL_DAYS} 天"

        install_time = trial_data.get("install_time", current_time)
        elapsed_days = (current_time - install_time) / 86400
        remaining = max(0, int(TRIAL_DAYS - elapsed_days))

        # 更新上次验证时间
        trial_data["last_check"] = current_time
        _save_trial_data(trial_data)

        if remaining > 0:
            return True, remaining, f"试用期剩余 {remaining} 天"
        else:
            return False, 0, "试用期已到期"

    # 2. NTP失败，使用本地缓存
    if trial_data is not None:
        install_time = trial_data.get("install_time", 0)
        last_check = trial_data.get("last_check", 0)



        # 使用上次验证时间来估算
        elapsed_days = (last_check - install_time) / 86400
        remaining = max(0, int(TRIAL_DAYS - elapsed_days))

        if remaining > 0:
            return True, remaining, f"离线模式 - 试用期剩余约 {remaining} 天"
        else:
            return False, 0, "试用期已到期"

    # 3. 无缓存且无网络
    return False, 0, "无法验证试用期，请检查网络连接"
