#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rennsport PAK-Entpacker (UE 4.27, Custom-Pak "Version 13")
==========================================================

Python-Portierung von "unreal_tournament_4_0.4.27e_rennsport.bms" (QuickBMS).
QuickBMS wird NICHT mehr benoetigt.

Ablauf beim ersten Start:
  - Das Script fragt nach dem AES-Key (64 Hex-Zeichen, "0x..." oder "\\x11\\x22..." geht auch)
  - Der Key wird in "rennsport_unpak.ini" neben diesem Script gespeichert
  - Bei jedem weiteren Start wird der Key automatisch aus der INI gelesen

Benutzung:
  python rennsport_unpak.py                         -> fragt nach der .pak
  python rennsport_unpak.py pakchunk0-WindowsNoEditor.pak
  python rennsport_unpak.py spiel.pak -o D:\\Export
  python rennsport_unpak.py spiel.pak --list        -> nur Dateiliste
  python rennsport_unpak.py spiel.pak --filter "*/Cars/*" --filter "*.uasset"
  python rennsport_unpak.py --reset-key             -> Key neu eingeben

  (Unter Windows kann man die .pak auch einfach auf das Script ziehen.)

Abhaengigkeiten:
  Windows: KEINE - AES laeuft ueber die Windows-eigene bcrypt.dll.
           (pycryptodome / cryptography werden genutzt, falls vorhanden.)
  Oodle:   oo2core_*_win64.dll wird automatisch gesucht (Script-Ordner, Spielordner,
           FModel, Steam-Spiele). Wird sie nicht gefunden, fragt das Script einmal
           nach dem Pfad und speichert ihn in der INI.
"""

import argparse
import configparser
import ctypes
import fnmatch
import glob
import os
import re
import struct
import sys
import time
import zlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
INI_PATH = os.path.join(SCRIPT_DIR, "rennsport_unpak.ini")

# --------------------------------------------------------------------------
# Konstanten aus dem BMS-Script
# --------------------------------------------------------------------------
FOOTER_FROM_END = 0xDC          # "goto -0xdc"  (custom version 13)
IV_FILES = bytes.fromhex("7C8D5C6C7052A2410CA5C370B99D233B")  # IV  -> Dateidaten (aes, CBC)
IV_INDEX = bytes.fromhex("757CE47962F4C728F989836F")          # IV1 -> Primary Index (AES-256-GCM)
IV_DIRS = bytes.fromhex("D1DC32328EB37FE27649C9DC")           # IV2 -> Directory Index (AES-256-GCM)

SKIP_OFFSETS = (0x80000000, 0x7FFFFFFF)


# --------------------------------------------------------------------------
# Krypto
# --------------------------------------------------------------------------
class _WinAES:
    """AES ueber die Windows-eigene bcrypt.dll (keine Installation noetig)."""

    def __init__(self, key: bytes):
        from ctypes import wintypes
        self.b = b = ctypes.WinDLL("bcrypt")
        b.BCryptOpenAlgorithmProvider.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.ULONG]
        b.BCryptSetProperty.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
        b.BCryptGenerateSymmetricKey.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                                 wintypes.ULONG, ctypes.c_char_p, wintypes.ULONG, wintypes.ULONG]
        for fn in (b.BCryptEncrypt, b.BCryptDecrypt):
            fn.argtypes = [ctypes.c_void_p, ctypes.c_char_p, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p,
                           wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), wintypes.ULONG]
        self.keys = {}
        for mode in ("ECB", "CBC"):
            alg = ctypes.c_void_p()
            self._ok(b.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "AES", None, 0))
            val = ctypes.create_unicode_buffer("ChainingMode" + mode)
            self._ok(b.BCryptSetProperty(alg, "ChainingMode", val, ctypes.sizeof(val), 0))
            hkey = ctypes.c_void_p()
            self._ok(b.BCryptGenerateSymmetricKey(alg, ctypes.byref(hkey), None, 0, key, len(key), 0))
            self.keys[mode] = (alg, hkey)

    @staticmethod
    def _ok(status):
        if status != 0:
            raise OSError(f"bcrypt-Fehler 0x{status & 0xFFFFFFFF:08X}")

    def _run(self, encrypt, mode, data, iv=None):
        from ctypes import wintypes
        hkey = self.keys[mode][1]
        out = ctypes.create_string_buffer(len(data))
        res = wintypes.ULONG()
        ivbuf = ctypes.create_string_buffer(iv, len(iv)) if iv else None
        fn = self.b.BCryptEncrypt if encrypt else self.b.BCryptDecrypt
        self._ok(fn(hkey, data, len(data), None, ivbuf, len(iv) if iv else 0, out, len(data), ctypes.byref(res), 0))
        return out.raw[:res.value]

    def ecb_encrypt(self, d):    return self._run(True, "ECB", d)
    def ecb_decrypt(self, d):    return self._run(False, "ECB", d)
    def cbc_decrypt(self, d, iv): return self._run(False, "CBC", d, iv)


def _xor(a: bytes, b: bytes) -> bytes:
    n = len(a)
    return (int.from_bytes(a, "little") ^ int.from_bytes(b[:n], "little")).to_bytes(n, "little")


def _gf_mult(x, y):
    r, z = 0xE1 << 120, 0
    for i in range(127, -1, -1):
        if (y >> i) & 1:
            z ^= x
        x = (x >> 1) ^ r if x & 1 else x >> 1
    return z


class Crypto:
    """
    AES-Backend-Reihenfolge:
      1. pycryptodome   (falls installiert)
      2. cryptography   (falls installiert)
      3. Windows bcrypt (immer vorhanden unter Windows -> keine Installation noetig)
    """

    INDEX_VARIANTS = ("gcm12", "ctr1", "ctr0", "gcm16", "ecb", "cbc_iv0", "raw")

    def __init__(self, key: bytes):
        self.key = key
        self.backend = None
        try:
            from Crypto.Cipher import AES
            self._AES = AES
            self.backend = "pycryptodome"
            return
        except ImportError:
            pass
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            self._Cipher, self._alg, self._modes = Cipher, algorithms, modes
            self.backend = "cryptography"
            return
        except ImportError:
            pass
        if os.name == "nt":
            self._win = _WinAES(key)
            self.backend = "windows-bcrypt"
            return
        sys.exit("FEHLER: Kein AES verfuegbar. Unter Linux/macOS bitte 'pip install pycryptodome'.")

    # ---- Grundbausteine ----------------------------------------------------
    def ecb_encrypt(self, data):
        if self.backend == "pycryptodome":
            return self._AES.new(self.key, self._AES.MODE_ECB).encrypt(data)
        if self.backend == "cryptography":
            e = self._Cipher(self._alg.AES(self.key), self._modes.ECB()).encryptor()
            return e.update(data) + e.finalize()
        return self._win.ecb_encrypt(data)

    def _ecb_decrypt(self, data):
        if self.backend == "pycryptodome":
            return self._AES.new(self.key, self._AES.MODE_ECB).decrypt(data)
        if self.backend == "cryptography":
            d = self._Cipher(self._alg.AES(self.key), self._modes.ECB()).decryptor()
            return d.update(data) + d.finalize()
        return self._win.ecb_decrypt(data)

    def _cbc_decrypt(self, data, iv16):
        if self.backend == "pycryptodome":
            return self._AES.new(self.key, self._AES.MODE_CBC, iv=iv16).decrypt(data)
        if self.backend == "cryptography":
            d = self._Cipher(self._alg.AES(self.key), self._modes.CBC(iv16)).decryptor()
            return d.update(data) + d.finalize()
        return self._win.cbc_decrypt(data, iv16)

    def ctr(self, data, counter16, inc32=False):
        """CTR-Modus. inc32=True: nur die unteren 32 Bit zaehlen (wie bei GCM)."""
        if not data:
            return data
        if self.backend == "pycryptodome" and not inc32:
            from Crypto.Util import Counter
            c = Counter.new(128, initial_value=int.from_bytes(counter16, "big"))
            return self._AES.new(self.key, self._AES.MODE_CTR, counter=c).decrypt(data)
        if self.backend == "cryptography" and not inc32:
            d = self._Cipher(self._alg.AES(self.key), self._modes.CTR(counter16)).decryptor()
            return d.update(data) + d.finalize()
        # generisch: Zaehlerbloecke per ECB verschluesseln und XOR
        nblk = (len(data) + 15) // 16
        out = bytearray()
        start = int.from_bytes(counter16, "big")
        hi, lo = start >> 32, start & 0xFFFFFFFF
        step = 1 << 16                       # 1 MB pro Durchgang
        for b0 in range(0, nblk, step):
            cnt = min(step, nblk - b0)
            if inc32:
                blocks = b"".join(((hi << 32) | ((lo + b0 + i) & 0xFFFFFFFF)).to_bytes(16, "big") for i in range(cnt))
            else:
                blocks = b"".join((start + b0 + i).to_bytes(16, "big") for i in range(cnt))
            ks = self.ecb_encrypt(blocks)
            chunk = data[b0 * 16:(b0 + cnt) * 16]
            out += _xor(chunk, ks)
        return bytes(out)

    # ---- Modi aus dem BMS --------------------------------------------------
    def gcm(self, data: bytes, iv12: bytes) -> bytes:
        """AES-256-GCM ohne Tag-Pruefung (wie QuickBMS) = CTR ab Zaehler IV||00000002."""
        return self.ctr(data, iv12 + b"\x00\x00\x00\x02", inc32=True)

    def gcm_any_iv(self, data, iv):
        """GCM mit IV != 12 Byte -> J0 per GHASH."""
        h = int.from_bytes(self.ecb_encrypt(b"\x00" * 16), "big")
        padded = iv + b"\x00" * ((-len(iv)) % 16) + (len(iv) * 8).to_bytes(16, "big")
        y = 0
        for i in range(0, len(padded), 16):
            y = _gf_mult(y ^ int.from_bytes(padded[i:i + 16], "big"), h)
        j0 = y
        ctr0 = (j0 & ~0xFFFFFFFF) | ((j0 + 1) & 0xFFFFFFFF)
        return self.ctr(data, ctr0.to_bytes(16, "big"), inc32=True)

    def _block(self, data, fn):
        n = len(data) & ~15
        return data if n == 0 else fn(data[:n]) + data[n:]

    def index_decrypt(self, variant, data, iv12):
        if variant == "gcm12":
            return self.gcm(data, iv12)
        if variant == "ctr1":
            return self.ctr(data, iv12 + b"\x00\x00\x00\x01")
        if variant == "ctr0":
            return self.ctr(data, iv12 + b"\x00\x00\x00\x00")
        if variant == "gcm16":
            return self.gcm_any_iv(data, iv12 + b"\x00" * 4)
        if variant == "ecb":
            return self._block(data, self._ecb_decrypt)
        if variant == "cbc_iv0":
            return self._block(data, lambda d: self._cbc_decrypt(d, iv12 + b"\x00" * 4))
        return data

    def cbc(self, data: bytes, iv16: bytes) -> bytes:
        """'encryption aes KEY IV' -> AES-256-CBC. Rest < 16 Byte bleibt unveraendert."""
        return self._block(data, lambda d: self._cbc_decrypt(d, iv16))


# --------------------------------------------------------------------------
# Dekompression
# --------------------------------------------------------------------------
def _steam_libraries():
    libs = []
    if os.name != "nt":
        return libs
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
    except OSError:
        steam = r"C:\Program Files (x86)\Steam"
    libs.append(steam)
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, encoding="utf-8", errors="replace") as fh:
            for m in re.finditer(r'"path"\s+"([^"]+)"', fh.read()):
                p = m.group(1).replace("\\\\", "\\")
                if p not in libs:
                    libs.append(p)
    except OSError:
        pass
    return [p for p in libs if os.path.isdir(p)]


class Decompressor:
    def __init__(self, oodle_hint=None, pak_dir=None):
        self._oodle = None
        self._oodle_tried = False
        self.oodle_hint = oodle_hint
        self.pak_dir = pak_dir
        self._zstd = None
        self._lz4 = None

    def _find_oodle(self):
        cands = []
        if self.oodle_hint:
            cands.append(self.oodle_hint)
        dirs = [SCRIPT_DIR]
        if self.pak_dir:
            d = self.pak_dir
            for _ in range(5):          # .pak liegt meist in <Spiel>\Rennsport\Content\Paks
                dirs.append(d)
                d = os.path.dirname(d)
            # typische UE-Orte
            dirs += glob.glob(os.path.join(d, "**", "Binaries", "Win64"), recursive=False)
        # FModel legt die DLL hier ab
        for env in ("APPDATA", "LOCALAPPDATA"):
            if os.environ.get(env):
                dirs.append(os.path.join(os.environ[env], "FModel", "Output", ".data"))
        pats = ["oo2core_*_win64.dll", "oo2core_*.dll", "liboo2corelinux64.so*"]
        for dd in dirs:
            for p in pats:
                cands += sorted(glob.glob(os.path.join(dd, p)), reverse=True)
        # Steam-Bibliotheken: viele Spiele liefern die DLL mit
        for lib in _steam_libraries():
            common = os.path.join(lib, "steamapps", "common")
            for sub in ("*", os.path.join("*", "*", "Binaries", "Win64"), os.path.join("*", "Binaries", "Win64")):
                cands += sorted(glob.glob(os.path.join(common, sub, "oo2core_*_win64.dll")), reverse=True)
        return cands

    def available(self):
        return self._load_oodle() is not None

    def set_dll(self, path):
        self.oodle_hint = path
        self._oodle_tried = False
        self._oodle = None
        return self.available()

    def _load_oodle(self):
        if self._oodle_tried:
            return self._oodle
        self._oodle_tried = True
        for path in self._find_oodle():
            try:
                lib = ctypes.CDLL(path)
                fn = lib.OodleLZ_Decompress
                fn.restype = ctypes.c_ssize_t
                fn.argtypes = [ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_ssize_t,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_int]
                self._oodle = fn
                print(f"[i] Oodle geladen: {path}")
                return fn
            except (OSError, AttributeError):
                continue
        return None

    def decompress(self, method: str, data: bytes, out_size: int) -> bytes:
        if method == "zlib":
            return zlib.decompress(data)
        if method == "gzip":
            return zlib.decompress(data, 16 + zlib.MAX_WBITS)
        if method == "oodle":
            fn = self._load_oodle()
            if fn is None:
                raise RuntimeError("Oodle-DLL (oo2core_*_win64.dll) nicht gefunden - siehe INI [Settings] oodle_dll")
            src = ctypes.create_string_buffer(data, len(data))
            dst = ctypes.create_string_buffer(out_size)
            r = fn(src, len(data), dst, out_size, 1, 0, 0, None, 0, None, None, None, 0, 3)
            if r != out_size:
                raise RuntimeError(f"Oodle-Fehler (Ergebnis {r}, erwartet {out_size})")
            return dst.raw
        if method == "zstd":
            if self._zstd is None:
                try:
                    import zstandard
                except ImportError:
                    raise RuntimeError("zstd benoetigt: pip install zstandard")
                self._zstd = zstandard.ZstdDecompressor()
            return self._zstd.decompress(data, max_output_size=out_size)
        if method == "lz4":
            if self._lz4 is None:
                try:
                    import lz4.block
                except ImportError:
                    raise RuntimeError("lz4 benoetigt: pip install lz4")
                self._lz4 = lz4.block
            return self._lz4.decompress(data, uncompressed_size=out_size)
        raise RuntimeError(f"Unbekannte Kompression '{method}'")


# --------------------------------------------------------------------------
# Kleiner Binaer-Leser
# --------------------------------------------------------------------------
class Reader:
    def __init__(self, buf: bytes, pos: int = 0):
        self.buf, self.pos = buf, pos

    def read(self, n):
        if self.pos + n > len(self.buf) or n < 0:
            raise EOFError(f"Lesefehler bei 0x{self.pos:X} (+{n})")
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def u8(self):  return self.read(1)[0]
    def u32(self): return struct.unpack("<I", self.read(4))[0]
    def i32(self): return struct.unpack("<i", self.read(4))[0]
    def u64(self): return struct.unpack("<Q", self.read(8))[0]

    def fstring(self):
        """UE FString: int32 Laenge (negativ = UTF-16), inkl. Null-Terminator."""
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            return self.read(n).split(b"\x00", 1)[0].decode("utf-8", "replace")
        raw = self.read(-n * 2)
        return raw.decode("utf-16-le", "replace").split("\x00", 1)[0]


def cstr(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("ascii", "replace").strip().lower()


# --------------------------------------------------------------------------
# PAK
# --------------------------------------------------------------------------
class PakEntry:
    __slots__ = ("name", "offset")

    def __init__(self, name, offset):
        self.name, self.offset = name, offset


class RennsportPak:
    def __init__(self, path: str, crypto: Crypto, decomp: Decompressor):
        self.path = path
        self.crypto = crypto
        self.decomp = decomp
        self.f = open(path, "rb")
        self.size = os.path.getsize(path)
        self.entries = []
        self.skipped = []
        self.comp = ["", ""]   # COMP1, COMP2
        self._read_footer()
        self._read_index()

    def close(self):
        self.f.close()

    def _pread(self, off, n):
        self.f.seek(off)
        b = self.f.read(n)
        if len(b) != n:
            raise EOFError(f"Datei zu kurz bei 0x{off:X}")
        return b

    # ---- Footer ------------------------------------------------------------
    def _read_footer(self):
        magic_off = self.size - FOOTER_FROM_END
        if magic_off < 0:
            raise ValueError("Datei ist zu klein fuer eine PAK.")
        foot = self._pread(magic_off - 1, FOOTER_FROM_END + 1)
        r = Reader(foot, 1)
        self.magic = r.u32()
        self.version = r.u32()
        self.index_offset = r.u64()
        r.u64()                     # SIZE (wird im BMS ueberschrieben)
        r.read(20)                  # HASH
        self.index_size = magic_off - self.index_offset - 1
        self.index_encrypted = foot[0] != 0
        self.magic_off = magic_off

        if r.pos < len(foot):
            check = foot[r.pos]
            if check <= 1:
                r.pos += 1
            self.comp[0] = cstr(r.read(32)) if r.pos + 32 <= len(foot) else ""
            self.comp[1] = cstr(r.read(32)) if r.pos + 32 <= len(foot) else ""

        if self.magic != 0x5A6F12E1:
            print(f"[!] Ungewoehnliche Magic 0x{self.magic:08X} (Standard UE: 0x5A6F12E1) - versuche trotzdem.")
        if self.version < 3:
            raise ValueError(f"PAK-Version {self.version} wird von diesem Script nicht unterstuetzt.")
        if not (0 < self.index_offset < magic_off):
            raise ValueError("Index-Offset ungueltig - falsche Datei oder anderes Pak-Format.")

    # ---- Index -------------------------------------------------------------
    def _read_index(self):
        raw = self._pread(self.index_offset, self.index_size)

        # Variante automatisch erkennen: plausibler Mount Point am Anfang?
        variant = None
        probe = raw[:64]
        tried = []
        for v in Crypto.INDEX_VARIANTS:
            dec = self.crypto.index_decrypt(v, probe, IV_INDEX)
            n = struct.unpack_from("<i", dec, 0)[0]
            tried.append((v, dec[:24].hex(" ")))
            if 1 < n <= 56 and dec[3 + n] == 0 and all(32 <= c < 127 for c in dec[4:3 + n]):
                variant = v
                break
        if variant is None:
            self._dump_debug(raw, tried)
            raise ValueError("Index laesst sich mit keiner Variante entschluesseln - AES-Key pruefen "
                             "(--reset-key). Diagnose-Ausgabe oben bitte schicken.")
        self.variant = variant
        print(f"[i] Index-Entschluesselung: {variant}")

        # MEMORY_FILE5: Abbild der PAK ab index_offset
        mem = bytearray(self.crypto.index_decrypt(variant, raw, IV_INDEX))

        r = Reader(mem)
        mount = r.fstring()
        self.mount_point = mount
        n_files = r.u32()
        r.read(12)                          # PathHashSeed + bHasPathHashIndex
        r.u64(); r.u64()                    # HASHES_OFFSET / HASHES_SIZE
        r.read(40)
        names_abs = r.u64()                 # Full Directory Index (absolut in der PAK)
        names_size = r.u64()
        r.read(40)
        base_index_off = r.pos              # Beginn der Encoded Entries
        self.n_files = n_files

        names_rel = names_abs - self.index_offset
        if not (0 <= names_rel and names_abs + names_size <= self.size):
            raise ValueError("Directory-Index ausserhalb der Datei - falscher AES-Key?")

        # Directory-Index wird immer mit IV2 entschluesselt (wie im BMS) und an
        # passender Stelle in MEMORY_FILE5 geschrieben ("append 1").
        dirs_raw = self._pread(names_abs, names_size)
        dirs = self.crypto.index_decrypt(variant, dirs_raw, IV_DIRS)
        if variant != "raw":
            cnt = struct.unpack_from("<I", dirs, 0)[0] if len(dirs) >= 4 else 0
            if cnt == 0 or cnt > 1_000_000:
                # Fallback: alle Varianten fuer den Directory-Index pruefen
                for v in Crypto.INDEX_VARIANTS:
                    t = self.crypto.index_decrypt(v, dirs_raw[:64], IV_DIRS)
                    c = struct.unpack_from("<I", t, 0)[0]
                    nl = struct.unpack_from("<i", t, 4)[0]
                    if 0 < c <= 1_000_000 and 0 < nl < 512:
                        print(f"[i] Directory-Index: Variante {v}")
                        dirs = self.crypto.index_decrypt(v, dirs_raw, IV_DIRS)
                        break
        end = names_rel + names_size
        if end > len(mem):
            mem.extend(b"\x00" * (end - len(mem)))
        mem[names_rel:end] = dirs

        # Basis-Pfad (Mount Point)
        prefix = mount
        if prefix.startswith("../../../"):
            prefix = prefix[9:]
        prefix = prefix.lstrip("/")

        d = Reader(bytes(mem), names_rel)
        dir_count = d.u32()
        if dir_count > 10_000_000:
            raise ValueError("Unplausibler Directory-Index - falscher AES-Key?")
        er = Reader(r.buf)
        for _ in range(dir_count):
            dir_name = d.fstring()
            files = d.u32()
            for _ in range(files):
                fname = d.fstring()
                full = (prefix + dir_name + fname).replace("\\", "/").lstrip("/")
                off = d.u32()
                if off in SKIP_OFFSETS:
                    self.skipped.append(full)
                    continue
                er.pos = base_index_off + off
                flags = er.u32()
                if (flags & 0x3F) == 0x3F:
                    er.u32()                        # CHUNK_SIZE (wird aus Inline-Header gelesen)
                if (flags >> 28) == 0xE:
                    entry_off = er.u32()
                else:
                    entry_off = er.u64()
                self.entries.append(PakEntry(full, entry_off))

    def _dump_debug(self, raw, tried):
        foot = self._pread(self.magic_off - 1, FOOTER_FROM_END + 1)
        print("\n----- DIAGNOSE (bitte komplett schicken) -----")
        print(f"Dateigroesse : {self.size} (0x{self.size:X})")
        print(f"Magic-Offset : 0x{self.magic_off:X}   Magic 0x{self.magic:08X}  Version {self.version}")
        print(f"Index-Offset : 0x{self.index_offset:X}   Index-Groesse {self.index_size}")
        print(f"Index-Flag   : {foot[0]}   Kompression: {self.comp}")
        print(f"Key (Anfang) : {self.crypto.key[:4].hex().upper()}...  Backend: {self.crypto.backend}")
        print("Footer (ab Flag-Byte):")
        for i in range(0, 96, 16):
            print("   " + foot[i:i + 16].hex(" "))
        print("Index roh (erste 48 Byte):")
        for i in range(0, 48, 16):
            print("   " + raw[i:i + 16].hex(" "))
        print("Varianten (erste 24 Byte):")
        for v, h in tried:
            print(f"   {v:8s} {h}")
        print("----------------------------------------------\n")

    # ---- Kompressionstyp (COMPRESSION_TYPE im BMS) -------------------------
    def _method(self, zip_id):
        c1, c2 = self.comp
        method = "zlib"
        if zip_id == 1 and c1:
            method = c1
        elif zip_id == 2 and c2:
            method = c2
        elif zip_id in (3, 4, 0x10):
            method = "oodle"
        return method

    # ---- Datei lesen -------------------------------------------------------
    def read_file(self, e: PakEntry) -> bytes:
        hdr = self._pread(e.offset, 0x400)
        r = Reader(hdr)
        rel = r.u64()
        zsize = r.u64()
        size = r.u64()
        zip_id = r.u32()
        r.read(20)
        chunks = []
        if zip_id != 0:
            n = r.u32()
            need = r.pos + n * 16 + 5
            if need > len(hdr):
                hdr = self._pread(e.offset, need)
                r.buf = hdr
            for _ in range(n):
                a, b = r.u64(), r.u64()
                chunks.append((a, b))
        encrypted = r.u8() != 0
        chunk_size = r.u32() or 0x10000
        data_off = e.offset + r.pos + rel

        if chunks:
            xsizes = []
            for a, b in chunks:
                cz = b - a
                xsizes.append((cz + 15) & ~15 if encrypted else cz)
            total = sum(xsizes)
            if encrypted:
                total = max(total, (zsize + 15) & ~15)
            blob = self._pread(data_off, total)
            if encrypted:
                blob = self.crypto.cbc(blob, IV_FILES)
            method = self._method(zip_id)
            out = bytearray()
            remaining = size
            pos = 0
            for (a, b), xs in zip(chunks, xsizes):
                cz = b - a
                usize = min(chunk_size, remaining)
                part = blob[pos:pos + cz]
                if zip_id == 0:
                    out += part[:usize]
                else:
                    out += self.decomp.decompress(method, part, usize)
                remaining -= usize
                pos += xs
            return bytes(out[:size])

        xsize = (zsize + 15) & ~15 if encrypted else zsize
        blob = self._pread(data_off, xsize)
        if encrypted:
            blob = self.crypto.cbc(blob, IV_FILES)
        blob = blob[:zsize]
        if zip_id == 0:
            return blob[:size] if size else blob
        return self.decomp.decompress(self._method(zip_id), blob, size)


# --------------------------------------------------------------------------
# INI / AES-Key
# --------------------------------------------------------------------------
def parse_key(text: str) -> bytes:
    t = text.strip().strip('"').strip("'")
    if "\\x" in t:
        t = t.replace("\\x", "")
    t = re.sub(r"^0x", "", t, flags=re.I)
    t = re.sub(r"[\s,:\-]", "", t)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", t):
        raise ValueError("Der AES-Key muss 32 Byte = 64 Hex-Zeichen lang sein.")
    return bytes.fromhex(t)


def load_config():
    cfg = configparser.ConfigParser()
    if os.path.isfile(INI_PATH):
        cfg.read(INI_PATH, encoding="utf-8")
    for sec in ("AES", "Settings"):
        if not cfg.has_section(sec):
            cfg.add_section(sec)
    return cfg


def save_config(cfg):
    with open(INI_PATH, "w", encoding="utf-8") as fh:
        fh.write("; Rennsport PAK-Entpacker - Einstellungen\n")
        fh.write("; key        = AES-256-Key als Hex (0x + 64 Zeichen)\n")
        fh.write("; oodle_dll  = optionaler Pfad zur oo2core_*_win64.dll\n")
        fh.write("; output_dir = optionaler Standard-Ausgabeordner\n\n")
        cfg.write(fh)


def get_key(cfg, reset=False) -> bytes:
    stored = cfg.get("AES", "key", fallback="").strip()
    if stored and not reset:
        try:
            return parse_key(stored)
        except ValueError:
            print(f"[!] Key in {os.path.basename(INI_PATH)} ist ungueltig - bitte neu eingeben.")
    print("Es ist noch kein AES-Key hinterlegt.")
    while True:
        try:
            text = input("AES-Key eingeben (0x... / 64 Hex-Zeichen): ")
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nAbgebrochen.")
        try:
            key = parse_key(text)
            break
        except ValueError as ex:
            print(f"  {ex}")
    cfg.set("AES", "key", "0x" + key.hex().upper())
    save_config(cfg)
    print(f"[i] Key gespeichert in: {INI_PATH}")
    return key


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def safe_join(root, rel):
    rel = rel.replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    return os.path.join(root, *parts)


def main():
    ap = argparse.ArgumentParser(description="Rennsport PAK-Entpacker (ersetzt QuickBMS + BMS-Script)")
    ap.add_argument("pak", nargs="*", help=".pak-Datei(en)")
    ap.add_argument("-o", "--output", help="Ausgabeordner (Standard: <pakname>_extracted neben der .pak)")
    ap.add_argument("-l", "--list", action="store_true", help="nur Dateien auflisten")
    ap.add_argument("-f", "--filter", action="append", help="Wildcard-Filter, mehrfach moeglich (z.B. \"*.uasset\")")
    ap.add_argument("--overwrite", action="store_true", help="vorhandene Dateien ueberschreiben")
    ap.add_argument("--reset-key", action="store_true", help="AES-Key neu eingeben")
    args = ap.parse_args()

    cfg = load_config()
    key = get_key(cfg, reset=args.reset_key)
    crypto = Crypto(key)

    paks = list(args.pak)
    if not paks:
        if args.reset_key:
            return
        try:
            p = input("Pfad zur .pak-Datei: ").strip().strip('"')
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nAbgebrochen.")
        paks = [p]

    filters = [f.lower().replace("\\", "/") for f in (args.filter or [])]
    out_default = args.output or cfg.get("Settings", "output_dir", fallback="").strip() or None
    oodle_hint = cfg.get("Settings", "oodle_dll", fallback="").strip() or None

    for pak_path in paks:
        if not os.path.isfile(pak_path):
            print(f"[!] Datei nicht gefunden: {pak_path}")
            continue
        print(f"\n=== {pak_path} ({human(os.path.getsize(pak_path))}) ===")
        decomp = Decompressor(oodle_hint, os.path.dirname(os.path.abspath(pak_path)))
        try:
            pak = RennsportPak(pak_path, crypto, decomp)
        except (ValueError, EOFError, struct.error) as ex:
            print(f"[!] Konnte Index nicht lesen: {ex}")
            continue

        print(f"Version {pak.version}, Index {'verschluesselt' if pak.index_encrypted else 'offen'}, "
              f"Kompression: {pak.comp[0] or '-'} / {pak.comp[1] or '-'}")
        print(f"Mount Point: {pak.mount_point}   Dateien: {len(pak.entries)} (Index meldet {pak.n_files})")

        if "oodle" in pak.comp and not args.list and not decomp.available():
            print("\n[!] Diese PAK ist mit Oodle komprimiert, die oo2core_*_win64.dll wurde nicht gefunden.")
            print("    Ohne sie koennen nur die unkomprimierten Dateien entpackt werden.")
            while True:
                try:
                    p = input("    Pfad zur oo2core_*_win64.dll (Enter = ueberspringen): ").strip().strip('"')
                except (EOFError, KeyboardInterrupt):
                    p = ""
                if not p:
                    break
                if os.path.isdir(p):
                    found = sorted(glob.glob(os.path.join(p, "oo2core_*.dll")), reverse=True)
                    p = found[0] if found else p
                if os.path.isfile(p) and decomp.set_dll(p):
                    cfg.set("Settings", "oodle_dll", os.path.abspath(p))
                    save_config(cfg)
                    oodle_hint = os.path.abspath(p)
                    print(f"[i] Pfad in {os.path.basename(INI_PATH)} gespeichert.")
                    break
                print("    Datei nicht gefunden oder keine gueltige Oodle-DLL.")

        entries = pak.entries
        if filters:
            entries = [e for e in entries if any(fnmatch.fnmatch(e.name.lower(), f) for f in filters)]
            print(f"Nach Filter: {len(entries)} Dateien")

        if args.list:
            for e in entries:
                print(f"  0x{e.offset:010X}  {e.name}")
            pak.close()
            continue

        base = os.path.splitext(os.path.basename(pak_path))[0]
        out_root = os.path.join(out_default, base) if out_default and len(paks) > 1 else (
            out_default or os.path.join(os.path.dirname(os.path.abspath(pak_path)), base + "_extracted"))
        os.makedirs(out_root, exist_ok=True)
        print(f"Ausgabe: {out_root}\n")

        ok = fail = skip = 0
        total_bytes = 0
        errors = []
        t0 = time.time()
        n = len(entries)
        for i, e in enumerate(entries, 1):
            dst = safe_join(out_root, e.name)
            if not args.overwrite and os.path.exists(dst):
                skip += 1
                continue
            try:
                data = pak.read_file(e)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as fh:
                    fh.write(data)
                ok += 1
                total_bytes += len(data)
            except Exception as ex:
                fail += 1
                errors.append((e.name, str(ex)))
            if i % 200 == 0 or i == n:
                print(f"\r  {i}/{n}  ({ok} ok, {fail} Fehler, {skip} uebersprungen)", end="", flush=True)
        print(f"\n\nFertig in {time.time() - t0:.1f}s - {ok} Dateien, {human(total_bytes)}")

        if pak.skipped:
            print(f"Ignorierte Eintraege (Offset 0x80000000/0x7FFFFFFF): {len(pak.skipped)}")
        if errors:
            log = os.path.join(out_root, "_fehler.txt")
            with open(log, "w", encoding="utf-8") as fh:
                for name, msg in errors:
                    fh.write(f"{name}\t{msg}\n")
            print(f"{fail} Fehler - Details in {log}")
            for name, msg in errors[:5]:
                print(f"  {name}: {msg}")
        pak.close()

    if os.name == "nt" and not sys.stdin.closed and len(sys.argv) <= 2:
        try:
            input("\nEnter zum Beenden...")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
