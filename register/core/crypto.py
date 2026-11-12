"""Encryption at rest: a key derived from the password, AES-256-GCM per row.

- key = scrypt(password, salt, n=2^17, r=8, p=1), 32 bytes, held only in memory
- crypto.json (plaintext, committed) holds the KDF parameters, salt, a random key_id and a
  "check" value (a fixed text encrypted with the key) used to verify a password
- every encrypted item is {"k": key_id, "n": nonce, "c": ciphertext+tag} with a fresh random
  96-bit nonce; associated data binds it to its file path and key_id
- plaintexts are padded with spaces to a multiple of 64 bytes so lengths reveal little

A password change creates a new salt, key and key_id; nothing stored can turn an old password
into the current key.
"""
import base64
import hashlib
import json
import re
import secrets

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # checked with a clear message by require()
    AESGCM = InvalidTag = None

# 4: one row layout for every account type, with category, transfer and bank_id.
# 3: rows without those; 2: labels that named the project; 1: unencrypted (none are read now)
FORMAT_VERSION = 4
CRYPTO_FILE = "crypto.json"
CIPHER = "AES-256-GCM"
DEFAULT_N, R, P = 2 ** 17, 8, 1
# CHECK_TEXT and the aad() format are part of the encrypted format: changing either makes every
# home, and every version in its git history, undecryptable. tests/test_crypto.py pins them.
CHECK_TEXT = b"key check"
PAD = 64
_KEY_ID = re.compile(r"^[0-9a-f]{16}\Z")


class CryptoError(Exception):
    """code: unavailable / bad_password / wrong_key / corrupt / old_format."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def require():
    if AESGCM is None:
        raise CryptoError("unavailable", "encryption needs the Python 'cryptography' package "
                                         "(Debian/Ubuntu: apt install python3-cryptography)")


def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s):
    try:
        return base64.b64decode(s, validate=True)
    except (ValueError, TypeError):
        raise CryptoError("corrupt", "invalid base64 in encrypted data") from None


def aad(rel, key_id):
    return ("v%d|%s|%s" % (FORMAT_VERSION, rel, key_id)).encode("utf-8")


def pad(plaintext):
    return plaintext + b" " * (-len(plaintext) % PAD)


def derive(password, salt, n, r, p):
    if not isinstance(password, str) or not password:
        raise CryptoError("bad_password", "the password cannot be empty")
    try:
        raw = password.encode("utf-8")
    except UnicodeEncodeError:  # e.g. bytes the terminal could not decode: never a usable password
        raise CryptoError("bad_password", "the password contains characters that cannot be used") from None
    return hashlib.scrypt(raw, salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=32)


class Key:
    """An unlocked data key. key_id identifies which password era it belongs to."""

    def __init__(self, raw, key_id):
        require()
        self.key_id = key_id
        self._aead = AESGCM(raw)

    def encrypt(self, rel, plaintext):
        nonce = secrets.token_bytes(12)
        ct = self._aead.encrypt(nonce, pad(plaintext), aad(rel, self.key_id))
        return {"k": self.key_id, "n": _b64(nonce), "c": _b64(ct)}

    def decrypt(self, rel, item):
        """Return the plaintext (padding removed) of {"k","n","c"} or raise CryptoError."""
        if not isinstance(item, dict) or set(item) - {"k", "n", "c", "format_version"} or \
                not all(isinstance(item.get(x), str) for x in ("k", "n", "c")):
            raise CryptoError("corrupt", "%s: malformed encrypted item" % rel)
        if item["k"] != self.key_id:
            raise CryptoError("wrong_key", "%s contains data encrypted under a different password "
                                           "(key %s, current key %s)" % (rel, item["k"], self.key_id))
        try:
            plain = self._aead.decrypt(_unb64(item["n"]), _unb64(item["c"]), aad(rel, self.key_id))
        except (InvalidTag, ValueError):
            raise CryptoError("corrupt", "%s: encrypted data failed authentication "
                                         "(damaged, tampered with, or moved from another file)" % rel) from None
        return plain.rstrip(b" ")


# ---------------------------------------------------------------- crypto.json

def new_key(password, n=DEFAULT_N):
    """Create a fresh salt, key_id and key for a password. Returns (Key, crypto.json doc)."""
    require()
    salt = secrets.token_bytes(16)
    key_id = secrets.token_hex(8)
    key = Key(derive(password, salt, n, R, P), key_id)
    check = key.encrypt(CRYPTO_FILE, CHECK_TEXT)
    doc = {"format_version": FORMAT_VERSION, "cipher": CIPHER,
           "kdf": {"name": "scrypt", "n": n, "r": R, "p": P, "salt": _b64(salt)},
           "key_id": key_id, "check": {"n": check["n"], "c": check["c"]}}
    return key, doc


def unlock(password, doc):
    """Derive the key for a password and verify it against the doc's check value."""
    require()
    doc = validate_doc(doc)
    kdf = doc["kdf"]
    key = Key(derive(password, _unb64(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"]), doc["key_id"])
    try:
        ok = key.decrypt(CRYPTO_FILE, dict(doc["check"], k=doc["key_id"])) == CHECK_TEXT
    except CryptoError:
        ok = False
    if not ok:
        raise CryptoError("bad_password", "wrong password")
    return key


def validate_doc(doc):
    if isinstance(doc, dict) and doc.get("format_version") in (1, 2, 3):
        raise CryptoError("old_format", "this home uses data format %d, from an older version. Open it with "
                          "the register.pyz it was made with; to move the data, export it with that "
                          "version and import the export into a new home" % doc["format_version"])
    try:
        kdf = doc["kdf"]
        valid = (doc["format_version"] == FORMAT_VERSION and doc["cipher"] == CIPHER
                 and kdf["name"] == "scrypt" and all(isinstance(kdf[x], int) for x in ("n", "r", "p"))
                 and 2 ** 10 <= kdf["n"] <= 2 ** 20 and kdf["r"] == R and kdf["p"] == P
                 and isinstance(kdf["salt"], str) and isinstance(doc["key_id"], str)
                 and _KEY_ID.match(doc["key_id"]) and isinstance(doc["check"], dict))
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise CryptoError("corrupt", "crypto.json is missing or invalid")
    return doc


def loads_doc(data):
    try:
        return validate_doc(json.loads(data.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        raise CryptoError("corrupt", "crypto.json is not valid JSON") from None


def dumps_doc(doc):
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------- encoding helpers

def dumps_item(item):
    """One encrypted item as a compact JSON line (keys in a fixed order)."""
    ordered = {k: item[k] for k in ("format_version", "k", "n", "c") if k in item}
    return json.dumps(ordered, separators=(",", ":"))


def loads_item(text, rel):
    try:
        item = json.loads(text)
    except ValueError:
        raise CryptoError("corrupt", "%s: not valid JSON" % rel) from None
    if not isinstance(item, dict):
        raise CryptoError("corrupt", "%s: malformed encrypted item" % rel)
    return item
