"""机器特征码生成模块 - 用于绑定试用期到特定机器"""

import hashlib
import platform
import subprocess
import uuid


def _get_mac_address() -> str:
    """获取MAC地址"""
    mac = uuid.getnode()
    return ':'.join(f'{(mac >> i) & 0xff:02x}' for i in range(40, -1, -8))


def _get_cpu_id() -> str:
    """获取CPU标识"""
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["wmic", "cpu", "get", "ProcessorId"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
            return lines[-1] if len(lines) > 1 else "unknown"
        elif system == "Linux":
            result = subprocess.run(
                ["cat", "/proc/cpuinfo"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split('\n'):
                if 'model name' in line:
                    return line.split(':')[1].strip()
            return "unknown"
    except Exception:
        return "unknown"
    return "unknown"


def _get_disk_serial() -> str:
    """获取磁盘序列号"""
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["wmic", "diskdrive", "get", "SerialNumber"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
            return lines[-1] if len(lines) > 1 else "unknown"
        elif system == "Linux":
            result = subprocess.run(
                ["lsblk", "-no", "SERIAL"],
                capture_output=True, text=True, timeout=5
            )
            serial = result.stdout.strip().split('\n')[0].strip()
            return serial if serial else "unknown"
    except Exception:
        return "unknown"
    return "unknown"


def get_machine_id() -> str:
    """生成机器唯一特征码（SHA256哈希）"""
    raw = f"{_get_mac_address()}|{_get_cpu_id()}|{_get_disk_serial()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_encryption_key() -> bytes:
    """从机器特征码派生加密密钥（用于Fernet）"""
    import base64
    machine_id = get_machine_id()
    # Fernet需要32字节的url-safe base64编码密钥
    key_bytes = hashlib.sha256(machine_id.encode()).digest()
    return base64.urlsafe_b64encode(key_bytes)
