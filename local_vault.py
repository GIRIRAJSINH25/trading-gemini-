import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

from dotenv import dotenv_values


SECRET_KEYS = {
    "ANGELONE_CLIENT_CODE",
    "ANGELONE_PASSWORD",
    "ANGELONE_TOTP_SECRET",
    "ANGELONE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SMTP_PASSWORD",
}
LOCAL_DATA_PATH = Path(
    os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
)
VAULT_PATH = LOCAL_DATA_PATH / "MarketExpertGemini" / "secrets.dpapi"
ENV_PATH = Path(__file__).with_name(".env")
VAULT_MAGIC = b"MARKET-EXPERT-DPAPI-V1\n"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _require_windows():
    if os.name != "nt":
        raise RuntimeError(
            "The local secrets vault uses Windows DPAPI and is available only on Windows."
        )


def _blob(data):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect_data(plaintext):
    _require_windows()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, source_buffer = _blob(plaintext)
    output = _DataBlob()
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "Market Expert Gemini local credentials",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
        del source_buffer


def _unprotect_data(ciphertext):
    _require_windows()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, source_buffer = _blob(ciphertext)
    output = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        ctypes.byref(description),
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(output.pbData)
        del source_buffer


def load_secrets(vault_path=VAULT_PATH):
    path = Path(vault_path)
    if not path.exists():
        return {}
    try:
        contents = path.read_bytes()
        if not contents.startswith(VAULT_MAGIC):
            raise ValueError("Unrecognized encrypted vault format.")
        values = json.loads(_unprotect_data(contents[len(VAULT_MAGIC):]).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Could not read local encrypted secrets vault {path}: {exc}") from exc
    if not isinstance(values, dict) or any(
        key not in SECRET_KEYS or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise RuntimeError("Local encrypted secrets vault contains invalid entries.")
    return values


def _write_secrets(values, vault_path):
    path = Path(vault_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = VAULT_MAGIC + _protect_data(
        json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(encrypted)
        os.replace(temporary, path)
    except OSError as exc:
        raise RuntimeError(f"Could not write local encrypted secrets vault {path}: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def save_secrets(values, vault_path=VAULT_PATH):
    if any(key not in SECRET_KEYS or not isinstance(value, str) or not value for key, value in values.items()):
        raise ValueError("Only non-empty configured secret values can be stored in the local vault.")
    merged = load_secrets(vault_path)
    merged.update(values)
    _write_secrets(merged, vault_path)
    if load_secrets(vault_path) != merged:
        raise RuntimeError("Encrypted vault verification failed.")
    return len(merged)


def migrate_env_secrets(env_path=ENV_PATH, vault_path=VAULT_PATH):
    """Encrypt configured secrets, verify the vault, then remove their .env entries."""
    env_file = Path(env_path)
    vault_file = Path(vault_path)
    if not env_file.exists():
        raise FileNotFoundError(f"Local .env file does not exist: {env_file}")
    try:
        existing = load_secrets(vault_file)
        configured = dotenv_values(env_file, interpolate=False)
        migrated = dict(existing)
        for key in SECRET_KEYS:
            value = configured.get(key)
            if value and not value.startswith("YOUR_"):
                migrated[key] = value

        lines = env_file.read_text(encoding="utf-8").splitlines(keepends=True)
        sanitized = []
        removed_keys = set()
        for line in lines:
            assignment = line.split("=", 1)[0].strip() if "=" in line else ""
            if assignment in SECRET_KEYS:
                removed_keys.add(assignment)
            else:
                sanitized.append(line)

        if migrated:
            _write_secrets(migrated, vault_file)
            verified = load_secrets(vault_file)
            if verified != migrated:
                raise RuntimeError("Encrypted vault verification failed; .env was not changed.")

        temporary_env = env_file.with_name(env_file.name + ".tmp")
        try:
            temporary_env.write_text("".join(sanitized), encoding="utf-8", newline="")
            os.replace(temporary_env, env_file)
        except OSError as exc:
            raise RuntimeError(
                "Secrets were encrypted, but .env could not be sanitized. "
                f"Remove secret entries from {env_file} after resolving the file error."
            ) from exc
        finally:
            if temporary_env.exists():
                temporary_env.unlink()
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not migrate local .env secrets: {exc}") from exc
    return len(migrated), len(removed_keys)


def load_secrets_into_environment(vault_path=VAULT_PATH):
    values = load_secrets(vault_path)
    os.environ.update(values)
    return values
