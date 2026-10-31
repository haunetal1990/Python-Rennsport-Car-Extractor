#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rennsport -> Blender Fahrzeug-Konverter
========================================

Erzeugt aus den entpackten Rennsport-Dateien (rennsport_unpak.py) eine fertige
.blend-Datei pro Fahrzeug - Meshes, Materialien, Texturen, Teile an ihrer Position.

Benutzung (normales Python, Blender wird automatisch im Hintergrund gestartet):

    python rennsport_blender.py BMW_M4_GT3
    python rennsport_blender.py BMW_M4_GT3 -o D:\\Autos
    python rennsport_blender.py BMW_M4_GT3 Audi_R8_LMS_GT3 Praga_R_1
    python rennsport_blender.py --list                 -> verfuegbare Fahrzeuge
    python rennsport_blender.py BMW_M4_GT3 --pack      -> Texturen in die .blend packen
    python rennsport_blender.py BMW_M4_GT3 --damage    -> Schadensteile zusaetzlich (ausgeblendet)

Beim ersten Start wird nach dem Content-Ordner der entpackten PAK und nach
blender.exe gefragt (sofern nicht automatisch gefunden). Beides landet in
"rennsport_blender.ini" neben diesem Script.

Direkt in Blender geht auch:
    blender -b -P rennsport_blender.py -- BMW_M4_GT3 --content "...\\Rennsport\\Content" -o D:\\Autos

Keine zusaetzlichen Pakete noetig (Blender bringt numpy mit).
"""

import argparse
import configparser
import glob
import os
import re
import struct
import subprocess
import sys
import time
import zlib

SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
INI_PATH = os.path.join(SCRIPT_DIR, "rennsport_blender.ini")

try:
    import bpy  # noqa: F401
    IN_BLENDER = True
except ImportError:
    IN_BLENDER = False


def log(msg=""):
    print(msg, flush=True)


# ===========================================================================
#  UE5-Paket (.uasset / .uexp / .ubulk) - unversioned, cooked
# ===========================================================================
class Reader:
    __slots__ = ("b", "p")

    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def read(self, n):
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.b, self.p)[0]
        self.p += 4
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.b, self.p)[0]
        self.p += 4
        return v

    def i64(self):
        v = struct.unpack_from("<q", self.b, self.p)[0]
        self.p += 8
        return v

    def fstr(self):
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            return self.read(n)[:-1].decode("latin1")
        return self.read(-n * 2)[:-2].decode("utf-16-le")


class Package:
    """Liest Name-Map, Imports, Exports und Data-Resources eines UE5-Pakets."""

    def __init__(self, base):
        self.base = base                         # Pfad ohne Endung
        self.name = os.path.basename(base)
        with open(base + ".uasset", "rb") as fh:
            b = fh.read()
        r = Reader(b)
        r.u32()
        legacy = r.i32()
        if legacy != -4:
            r.i32()
        r.i32()                                  # UE4 version
        if legacy <= -8:
            r.i32()                              # UE5 version
        r.i32()                                  # licensee
        cv = r.i32()
        r.p += 20 * cv
        self.header_size = r.i32()
        r.fstr()
        self.flags = r.u32()
        nc, no = r.i32(), r.i32()
        r.i32(); r.i32()                         # soft object paths
        r.i32(); r.i32()                         # gatherable text
        ec, eo, ic, io, dep = (r.i32() for _ in range(5))
        r.i32(); r.i32(); r.i32(); r.i32()       # soft pkg refs, searchable, thumbnails
        r.p += 16                                # guid
        gen = r.i32()
        r.p += 8 * gen
        for _ in range(2):                       # engine versions
            r.p += 10
            r.fstr()
        r.u32()                                  # compression flags
        r.i32()                                  # compressed chunks (immer 0)
        r.u32()                                  # package source
        for _ in range(r.i32()):
            r.fstr()
        r.i32()                                  # asset registry
        r.i64()                                  # bulk data start
        r.i32()                                  # world tile
        nchunks = r.i32()                        # chunk ids
        r.p += 4 * nchunks
        r.i32(); r.i32()                         # preload deps
        r.i32()                                  # names referenced
        r.i64()                                  # payload toc
        dro = r.i32()

        r.p = no
        self.names = []
        for _ in range(nc):
            self.names.append(r.fstr())
            r.p += 4                             # hash
        r.p = io
        self.imports = []
        for _ in range(ic):
            r.p += 8                             # class package
            cls = self._fname(r)
            outer = r.i32()
            obj = self._fname(r)
            r.p += 4                             # bImportOptional
            self.imports.append((cls, outer, obj))
        esize = (dep - eo) // ec if ec else 0
        self.exports = []
        for k in range(ec):
            r.p = eo + k * esize
            ci = r.i32(); r.i32(); r.i32(); oi = r.i32()
            nm = self._fname(r)
            r.u32()
            size = r.i64()
            off = r.i64()
            self.exports.append({"cls": ci, "outer": oi, "name": nm, "size": size, "off": off})
        self.resources = []
        if 0 < dro < len(b):
            r.p = dro
            r.u32()
            for _ in range(r.i32()):
                fl = r.u32(); so = r.i64(); r.i64(); ss = r.i64(); r.i64(); r.i32(); lf = r.u32()
                self.resources.append({"offset": so, "size": ss, "flags": fl, "legacy": lf})
        self._uexp = None
        self._ubulk = None

    def _fname(self, r):
        i, n = r.i32(), r.i32()
        s = self.names[i] if 0 <= i < len(self.names) else "?"
        return s if n == 0 else "%s_%d" % (s, n - 1)

    @property
    def uexp(self):
        if self._uexp is None:
            with open(self.base + ".uexp", "rb") as fh:
                self._uexp = fh.read()
        return self._uexp

    def ubulk(self):
        if self._ubulk is None:
            p = self.base + ".ubulk"
            self._ubulk = open(p, "rb").read() if os.path.isfile(p) else b""
        return self._ubulk

    def ref_name(self, idx):
        if idx < 0 and -idx - 1 < len(self.imports):
            return self.imports[-idx - 1][2]
        if idx > 0 and idx - 1 < len(self.exports):
            return self.exports[idx - 1]["name"]
        return None

    def ref_class(self, idx):
        if idx < 0 and -idx - 1 < len(self.imports):
            return self.imports[-idx - 1][0]
        if idx > 0 and idx - 1 < len(self.exports):
            return self.ref_name(self.exports[idx - 1]["cls"])
        return None

    def import_package(self, idx):
        """Paketpfad (/Game/...) eines Imports."""
        while idx < 0:
            cls, outer, obj = self.imports[-idx - 1]
            if outer == 0:
                return obj
            idx = outer
        return None

    def export_data(self, cls_name):
        for i, e in enumerate(self.exports):
            if self.ref_name(e["cls"]) == cls_name:
                s = e["off"] - self.header_size
                return i + 1, self.uexp[s:s + e["size"]]
        return None, None

    def exports_of(self, cls_name):
        out = []
        for i, e in enumerate(self.exports):
            if self.ref_name(e["cls"]) == cls_name:
                s = e["off"] - self.header_size
                out.append((i + 1, e, self.uexp[s:s + e["size"]]))
        return out

    def bulk(self, res_index, data=None, pos=None):
        """Liefert (bytes, neue_pos). Inline-Daten folgen im Stream direkt nach dem Index."""
        res = self.resources[res_index]
        if res["legacy"] & 0x100:                # Payload in separater Datei (.ubulk)
            if res["legacy"] & 0x800:            # optional (.uptnl)
                p = self.base + ".uptnl"
                if not os.path.isfile(p):
                    return None, pos
                with open(p, "rb") as fh:
                    fh.seek(res["offset"])
                    return fh.read(res["size"]), pos
            ub = self.ubulk()
            return ub[res["offset"]:res["offset"] + res["size"]], pos
        if data is not None and pos is not None:
            return data[pos:pos + res["size"]], pos + res["size"]
        return None, pos


def game_path_to_file(content_root, game_path):
    """/Game/Vehicles/X/Y  ->  <Content>/Vehicles/X/Y (ohne Endung)"""
    if not game_path or not game_path.startswith("/Game/"):
        return None
    return os.path.join(content_root, *game_path[6:].split("/"))


# ===========================================================================
#  StaticMesh
# ===========================================================================
class MeshData:
    def __init__(self):
        self.name = ""
        self.positions = None      # (n,3) float32, UE-Koordinaten (cm)
        self.normals = None        # (n,3) float32
        self.uvs = []              # Liste (n,2) float32
        self.colors = None         # (n,4) uint8 oder None
        self.indices = None        # (t,3) uint32
        self.sections = []         # (material_index, first_index, num_tris)
        self.materials = []        # Liste (mi_name, game_path|None, slot_name)
        self.sockets = []          # Liste (name, (x,y,z), (pitch,yaw,roll))


def _np():
    import numpy as np
    return np


def _parse_mesh_buffers(d, pos):
    np = _np()
    m = {}
    pos += 2                                     # strip flags
    stride, nv = struct.unpack_from("<ii", d, pos); pos += 8
    es, cnt = struct.unpack_from("<ii", d, pos); pos += 8
    m["P"] = np.frombuffer(d, np.float32, nv * 3, pos).reshape(nv, 3).copy()
    pos += es * cnt
    pos += 2                                     # strip flags
    ntc, nv2, full_uv, hi_tan = struct.unpack_from("<IIii", d, pos); pos += 16
    es, cnt = struct.unpack_from("<ii", d, pos); pos += 8
    if hi_tan:
        t = np.frombuffer(d, np.int16, cnt * es // 2, pos).reshape(nv, -1)
        m["N"] = t[:, 4:7].astype(np.float32) / 32767.0
    else:
        t = np.frombuffer(d, np.int8, cnt * es, pos).reshape(nv, -1)
        m["N"] = t[:, 4:7].astype(np.float32) / 127.0
    pos += es * cnt
    es, cnt = struct.unpack_from("<ii", d, pos); pos += 8
    if full_uv:
        uv = np.frombuffer(d, np.float32, cnt * 2, pos)
    else:
        uv = np.frombuffer(d, np.float16, cnt * 2, pos).astype(np.float32)
    uv = uv.reshape(nv, ntc, 2)
    m["UV"] = [uv[:, k, :].copy() for k in range(ntc)]
    pos += es * cnt
    pos += 2                                     # color strip flags
    cstride, cnv = struct.unpack_from("<ii", d, pos); pos += 8
    m["C"] = None
    if cnv > 0:
        es, cnt = struct.unpack_from("<ii", d, pos); pos += 8
        c = np.frombuffer(d, np.uint8, cnt * es, pos).reshape(cnv, 4)
        m["C"] = c[:, [2, 1, 0, 3]].copy()       # BGRA -> RGBA
        pos += es * cnt
    b32 = struct.unpack_from("<i", d, pos)[0]; pos += 4
    es, cnt = struct.unpack_from("<ii", d, pos); pos += 8
    if b32:
        idx = np.frombuffer(d, np.uint32, cnt * es // 4, pos)
    else:
        idx = np.frombuffer(d, np.uint16, cnt * es // 2, pos).astype(np.uint32)
    m["I"] = idx.reshape(-1, 3).copy()
    return m


def _find_static_materials(pkg, d):
    """Native StaticMaterials-Liste (je 36 Byte) im Export suchen."""
    mat_imports = {-(i + 1) for i, (c, o, n) in enumerate(pkg.imports)
                   if c in ("MaterialInstanceConstant", "Material", "MaterialInstanceDynamic")}
    if not mat_imports:
        return None
    nn = len(pkg.names)
    best = None
    for mi in mat_imports:
        pat = struct.pack("<i", mi)
        st = 0
        while True:
            c = d.find(pat, st)
            if c < 0:
                break
            st = c + 1
            for k in range(64):
                start = c - 36 * k - 4
                if start < 0:
                    break
                n = struct.unpack_from("<i", d, start)[0]
                if not (k < n <= 64) or start + 4 + n * 36 > len(d):
                    continue
                ok = True
                entries = []
                for j in range(n):
                    o = start + 4 + j * 36
                    m, ni, num = struct.unpack_from("<iii", d, o)
                    if not ((m in mat_imports or m == 0) and 0 <= ni < nn and 0 <= num < 1000):
                        ok = False
                        break
                    entries.append((m, pkg.names[ni]))
                if ok and (best is None or start > best[0]):
                    best = (start, entries)
                break
    return best[1] if best else None


def read_mesh_sockets(pkg):
    """UStaticMeshSocket (unversioned): Header-Fragment, SocketName, RelativeLocation[, RelativeRotation]"""
    out = []
    for _, e, d in pkg.exports_of("StaticMeshSocket"):
        try:
            hdr = struct.unpack_from("<H", d, 4)[0]
            skip, last, nval = hdr & 0x7F, hdr & 0x100, hdr >> 9
            if skip != 0 or not last or nval < 2:
                continue
            ni, num = struct.unpack_from("<ii", d, 6)
            loc = struct.unpack_from("<3d", d, 14)
            rot = struct.unpack_from("<3d", d, 38) if nval >= 3 else (0.0, 0.0, 0.0)
            out.append((pkg.names[ni], loc, rot))
        except (struct.error, IndexError):
            continue
    return out


def ue_rotator_to_blender_matrix(pitch, yaw, roll):
    """FRotator (Grad) -> 3x3 Rotationsmatrix im Blender-Raum (Y gespiegelt)."""
    import math
    p, y, r = (math.radians(v) for v in (pitch, yaw, roll))
    sp, cp, sy, cy, sr, cr = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(r), math.cos(r)
    rows = [(cp * cy, cp * sy, sp),
            (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp),
            (-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp)]
    R = [[rows[c][r_] for c in range(3)] for r_ in range(3)]       # Spalten = UE-Achsen
    S = (1, -1, 1)
    return [[S[i] * R[i][j] * S[j] for j in range(3)] for i in range(3)]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def load_static_mesh(base):
    pkg = Package(base)
    _, d = pkg.export_data("StaticMesh")
    if d is None:
        raise ValueError("kein StaticMesh-Export")
    body = [i + 1 for i, e in enumerate(pkg.exports) if pkg.ref_name(e["cls"]) == "BodySetup"]
    nav = [i + 1 for i, e in enumerate(pkg.exports) if pkg.ref_name(e["cls"]) == "NavCollision"]
    pat = struct.pack("<iii", 1, body[0] if body else 0, nav[0] if nav else 0)

    result = None
    st = 0
    while result is None:
        a = d.find(pat, st)
        if a < 0:
            break
        st = a + 1
        try:
            pos = a + 12 + 16                    # + LightingGuid
            nsock = struct.unpack_from("<i", d, pos)[0]
            if not 0 <= nsock < 256:
                continue
            pos += 4 + 4 * nsock
            nlod = struct.unpack_from("<i", d, pos)[0]
            if not 1 <= nlod <= 8:
                continue
            pos += 4 + 2
            nsec = struct.unpack_from("<i", d, pos)[0]
            if not 1 <= nsec <= 256:
                continue
            pos += 4
            ssize = None
            for S in (40, 36, 44, 32, 48, 28):
                fi, ok = 0, True
                for k in range(nsec):
                    mi, f, nt = struct.unpack_from("<iII", d, pos + k * S)
                    if f != fi or not 0 <= mi < 256:
                        ok = False
                        break
                    fi = f + 3 * nt
                if ok:
                    ssize = S
                    break
            if ssize is None:
                continue
            secs = [struct.unpack_from("<iII", d, pos + k * ssize) for k in range(nsec)]
            pos += nsec * ssize
            _maxdev, cooked_out, inlined = struct.unpack_from("<fii", d, pos)
            pos += 12
            if cooked_out:
                continue
            if inlined:
                bufs = _parse_mesh_buffers(d, pos)
            else:
                ri = struct.unpack_from("<i", d, pos)[0]
                blob, _ = pkg.bulk(ri, d, pos + 4)
                if not blob:
                    continue
                bufs = _parse_mesh_buffers(blob, 0)
            result = (secs, bufs)
        except (struct.error, ValueError, IndexError):
            continue
    if result is None:
        raise ValueError("Mesh-Struktur nicht erkannt")

    secs, b = result
    m = MeshData()
    m.name = pkg.name
    m.positions, m.normals, m.uvs, m.colors, m.indices = b["P"], b["N"], b["UV"], b["C"], b["I"]
    m.sections = [(mi, f, nt) for mi, f, nt in secs]
    m.sockets = read_mesh_sockets(pkg)
    sm = _find_static_materials(pkg, d)
    if sm:
        for idx, slot in sm:
            m.materials.append((pkg.ref_name(idx) if idx else None,
                                pkg.import_package(idx) if idx else None, slot))
    else:                                        # Fallback: Import-Reihenfolge
        for i, (c, o, n) in enumerate(pkg.imports):
            if c in ("MaterialInstanceConstant", "Material"):
                m.materials.append((n, pkg.import_package(-(i + 1)), n))
    return m


# ===========================================================================
#  Skelett (Bone-Positionen fuer bewegliche Teile)
# ===========================================================================
def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def _qrot(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    tx = 2 * (y * vz - z * vy)
    ty = 2 * (z * vx - x * vz)
    tz = 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty),
            vy + w * ty + (z * tx - x * tz),
            vz + w * tz + (x * ty - y * tx))


def load_bones(base):
    """{bone_name_lower: (name, (x,y,z) Welt in UE-cm)}"""
    pkg = Package(base)
    for cls in ("SkeletalMesh", "Skeleton"):
        _, d = pkg.export_data(cls)
        if d is None:
            continue
        nn = len(pkg.names)
        for i in range(0, len(d) - 16):
            n = struct.unpack_from("<i", d, i)[0]
            if not 2 < n < 1000 or i + 4 + n * 12 + 4 > len(d):
                continue
            ok, bones = True, []
            for k in range(n):
                ni, num, par = struct.unpack_from("<iii", d, i + 4 + k * 12)
                if not (0 <= ni < nn and 0 <= num < 100000 and -1 <= par < k) or (k == 0 and par != -1):
                    ok = False
                    break
                bones.append((pkg.names[ni] + ("_%d" % (num - 1) if num else ""), par))
            if not ok:
                continue
            j = i + 4 + n * 12
            if struct.unpack_from("<i", d, j)[0] != n:
                continue
            world = []
            for k in range(n):
                t = struct.unpack_from("<10d", d, j + 4 + k * 80)
                q, loc, sc = t[0:4], t[4:7], t[7:10]
                par = bones[k][1]
                if par < 0:
                    world.append((q, loc))
                else:
                    pq, pl = world[par]
                    rl = _qrot(pq, loc)
                    world.append((_qmul(pq, q), (pl[0] + rl[0], pl[1] + rl[1], pl[2] + rl[2])))
            return {name.lower(): (name, world[k][1]) for k, (name, _) in enumerate(bones)}
    return {}


# Teilname (ohne "SM_<Auto>_") -> Bone-Kandidaten; "mirror": rechte Seite duplizieren
PART_BONES = [
    ("rear_door_left", ["rear_door_left"], None),
    ("rear_door_right", ["rear_door_right"], None),
    ("door_left", ["door_left"], None),
    ("door_right", ["door_right"], None),
    ("front_left_caliper", ["wheel_front_left_turn", "wheel_front_left_spin", "wheel_front_spin_l"], None),
    ("front_right_caliper", ["wheel_front_right_turn", "wheel_front_right_spin", "wheel_front_spin_r"], None),
    ("rear_left_caliper", ["wheel_back_left_spin", "wheel_back_spin_l"], None),
    ("rear_right_caliper", ["wheel_back_right_spin", "wheel_back_spin_r"], None),
    ("front_brake_disc", ["wheel_front_left_turn", "wheel_front_spin_l"], ["wheel_front_right_turn", "wheel_front_spin_r"]),
    ("front_break_disc", ["wheel_front_left_turn", "wheel_front_spin_l"], ["wheel_front_right_turn", "wheel_front_spin_r"]),
    ("rear_brake_disc", ["wheel_back_left_spin", "wheel_back_spin_l"], ["wheel_back_right_spin", "wheel_back_spin_r"]),
    ("rear_break_disc", ["wheel_back_left_spin", "wheel_back_spin_l"], ["wheel_back_right_spin", "wheel_back_spin_r"]),
    ("front_wheel", ["wheel_front_left_turn", "wheel_front_spin_l"], ["wheel_front_right_turn", "wheel_front_spin_r"]),
    ("rear_wheel", ["wheel_back_left_spin", "wheel_back_spin_l"], ["wheel_back_right_spin", "wheel_back_spin_r"]),
    ("front_fender_left", ["fender_front_left"], None),
    ("front_fender_right", ["fender_front_right"], None),
    ("rear_fender_left", ["fender_rear_left"], None),
    ("rear_fender_right", ["fender_rear_right"], None),
    ("fender_left", ["fender_front_left", "fender_left"], None),
    ("fender_right", ["fender_front_right", "fender_right"], None),
    ("front_bumper", ["bumper_front"], None),
    ("bumper_front", ["bumper_front"], None),
    ("rear_bumper", ["bumper_rear", "bimper_rear"], None),
    ("bumper_rear", ["bumper_rear", "bimper_rear"], None),
    ("front_hood", ["hood_front", "front_hood"], None),
    ("hood", ["hood_front", "front_hood"], None),
    ("rear_boot", ["boot_rear", "rear_boot"], None),
    ("boot", ["boot_rear"], None),
    ("rear_spoiler", ["spoiler_back"], None),
    ("spoiler", ["spoiler_back"], None),
    ("rear_diffuser", ["diffuser_back", "diffuser"], None),
    ("diffuser", ["diffuser_back", "diffuser"], None),
    ("wing_mirror_left", ["wing_mirror_left"], None),
    ("wing_mirror_right", ["wing_mirror_right"], None),
    ("mirror_left", ["wing_mirror_left"], None),
    ("mirror_right", ["wing_mirror_right"], None),
    ("canopy_left", ["rear_canopy_left"], None),
    ("canopy_right", ["rear_canopy_right"], None),
    ("canopy", ["canopy"], None),
]

SKIP_PARTS = ("_blur", "collision")
# Teile, die im Spiel per Blueprint platziert werden (kein Bone im Skelett)
LOCAL_PARTS = ("steering_wheel", "pedal", "wiper", "paddle", "shifter", "gear_lever", "handbrake", "display")


_SYN = {"back": "rear", "break": "brake", "bimper": "bumper", "exhust": "exhaust", "bonnet": "hood",
        "trunk": "boot", "wing": "wing", "fenders": "fender", "mirrors": "mirror", "doors": "door",
        "pedals": "pedal", "accelerate": "acceleration", "accelerator": "acceleration", "throttle": "acceleration",
        "gas": "acceleration", "accel": "acceleration", "brakes": "brake", "wipers": "wiper",
        "steeringwheel": "steeringwheel", "handbreak": "handbrake", "l": "left", "r": "right"}
_POS_WORDS = {"front", "rear", "left", "right", "top", "bottom", "inner", "outer", "upper", "lower", "center"}
_WHEEL_WORDS = {"wheel", "brake", "disc", "caliper", "tyre", "tire", "rim", "hub"}


def _token_list(name):
    t = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]
    return [_SYN.get(w, w) for w in t if not w.isdigit()]


def _tokens(name):
    return frozenset(_token_list(name))


def name_suffixes(name, min_words=1):
    """Alle Namensenden: 'SM_X_Interior_Pedal_Clutch' -> ..., 'interior_pedal_clutch', 'pedal_clutch', 'clutch'"""
    t = _token_list(name)
    out = []
    for k in range(len(t)):
        if len(t) - k < min_words:
            break
        out.append(t[k:])
        if t[k] in _POS_WORDS:          # Positionswoerter nie wegkuerzen (Rear_Fender != Fender)
            break
    return out


def part_placement(part_name, car, bones):
    """-> (loc_left, loc_right_or_None, bone) in UE-cm, oder (None, None, None)"""
    s = part_name.lower()
    pre = ("sm_" + car + "_").lower()
    if s.startswith(pre):
        s = s[len(pre):]
    # 1) generisch: gleiche Wortmenge am Namensende (Reihenfolge egal, Praefix egal),
    #    z.B. front_fender_left == fender_front_left, Huyndai_..._Rear_Door_Left == rear_door_left
    bone_sets = [(_tokens(bname), bname, loc) for bl, (bname, loc) in sorted(bones.items())]
    bone_sets = [b for b in bone_sets if "end" not in b[0]]
    for suf in name_suffixes(s, 2):
        toks = frozenset(suf)
        if toks & _WHEEL_WORDS:
            continue
        for bt, bname, loc in bone_sets:
            if bt == toks:
                return loc, None, bname
    # 1b) 'Front' weglassen (Bone heisst nur 'Fender_Left'); 'Rear'/'Back' nie
    for suf in name_suffixes(s, 2):
        toks = frozenset(suf)
        if "front" not in toks or toks & _WHEEL_WORDS:
            continue
        t2 = toks - {"front"}
        if len(t2) < 2:
            continue
        for bt, bname, loc in bone_sets:
            if bt == t2:
                return loc, None, bname
    # 2) Tabelle (Raeder, Bremsen, Abkuerzungen)
    for key, cand, mirror in sorted(PART_BONES, key=lambda x: -len(x[0])):
        if s == key or s.endswith("_" + key) or s.startswith(key + "_") or ("_" + key + "_") in s:
            for c in cand:
                if c.lower() in bones:
                    right = None
                    if mirror:
                        for mc in mirror:
                            if mc.lower() in bones:
                                right = bones[mc.lower()][1]
                                break
                    return bones[c.lower()][1], right, bones[c.lower()][0]
            return None, None, None
    return None, None, None


# ===========================================================================
#  Texturen (Texture2D) -> RGBA numpy
# ===========================================================================
_PF_RE = re.compile(rb"PF_[A-Za-z0-9_]+\x00")


def load_texture(base, max_size=0):
    """-> dict(name, format, width, height, rgba(np.uint8 HxWx4), is_normal)"""
    pkg = Package(base)
    _, d = pkg.export_data("Texture2D")
    if d is None:
        raise ValueError("kein Texture2D-Export")
    for m in _PF_RE.finditer(d):
        s = m.start()
        slen = struct.unpack_from("<i", d, s - 4)[0]
        if slen != m.end() - s:
            continue
        sx, sy, packed = struct.unpack_from("<iiI", d, s - 16)
        fmt = d[s:m.end() - 1].decode()
        pos = m.end()
        if packed & (1 << 30):                   # HasOptData
            pos += 8
        first_mip, nmips = struct.unpack_from("<ii", d, pos)
        pos += 8
        mips = []
        for _ in range(nmips):
            ri = struct.unpack_from("<i", d, pos)[0]
            pos += 4
            data, pos = pkg.bulk(ri, d, pos)
            w, h, z = struct.unpack_from("<iii", d, pos)
            pos += 12
            mips.append((w, h, data))
        chosen = None
        for w, h, data in mips:
            if data and (not max_size or max(w, h) <= max_size):
                chosen = (w, h, data)
                break
        if chosen is None:
            chosen = next(((w, h, dt) for w, h, dt in mips if dt), None)
        if chosen is None:
            raise ValueError("keine Mip-Daten gefunden")
        w, h, data = chosen
        rgba = decode_pixels(fmt, data, w, h)
        return {"name": pkg.name, "format": fmt, "width": w, "height": h, "rgba": rgba}
    raise ValueError("Pixelformat nicht gefunden")


def _bc_layout(blocks, w, h, bw, bh, ch):
    np = _np()
    img = blocks.reshape(bh, bw, 4, 4, ch).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, ch)
    return img[:h, :w]


def _bc1_colors(blk, force4):
    np = _np()
    c0 = blk[:, 0].astype(np.uint32) | (blk[:, 1].astype(np.uint32) << 8)
    c1 = blk[:, 2].astype(np.uint32) | (blk[:, 3].astype(np.uint32) << 8)
    bits = (blk[:, 4].astype(np.uint32) | (blk[:, 5].astype(np.uint32) << 8) |
            (blk[:, 6].astype(np.uint32) << 16) | (blk[:, 7].astype(np.uint32) << 24))

    def rgb(c):
        r = ((c >> 11) & 31) * 255 // 31
        g = ((c >> 5) & 63) * 255 // 63
        b = (c & 31) * 255 // 31
        return np.stack([r, g, b], 1).astype(np.int32)

    p0, p1 = rgb(c0), rgb(c1)
    n = len(blk)
    pal = np.zeros((n, 4, 4), np.int32)
    pal[:, 0, :3], pal[:, 1, :3] = p0, p1
    pal[:, :, 3] = 255
    four = (c0 > c1) | force4
    pal[:, 2, :3] = np.where(four[:, None], (2 * p0 + p1) // 3, (p0 + p1) // 2)
    pal[:, 3, :3] = np.where(four[:, None], (p0 + 2 * p1) // 3, 0)
    pal[:, 3, 3] = np.where(four, 255, 0)
    sel = ((bits[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3).astype(np.int32)
    sel += (np.arange(n, dtype=np.int32) * 4)[:, None]
    return pal.clip(0, 255).astype(np.uint8).reshape(-1, 4)[sel]


def _bc4_values(blk):
    np = _np()
    a0 = blk[:, 0].astype(np.int32)
    a1 = blk[:, 1].astype(np.int32)
    bits = np.zeros(len(blk), np.uint64)
    for k in range(6):
        bits |= blk[:, 2 + k].astype(np.uint64) << np.uint64(8 * k)
    pal = np.zeros((len(blk), 8), np.int32)
    pal[:, 0], pal[:, 1] = a0, a1
    big = a0 > a1
    for k in range(1, 7):
        v8 = ((7 - k) * a0 + k * a1) // 7
        pal[:, k + 1] = np.where(big, v8, pal[:, k + 1])
    for k in range(1, 5):
        v6 = ((5 - k) * a0 + k * a1) // 5
        pal[:, k + 1] = np.where(big, pal[:, k + 1], v6)
    pal[:, 6] = np.where(big, pal[:, 6], 0)
    pal[:, 7] = np.where(big, pal[:, 7], 255)
    sel = ((bits[:, None] >> (np.arange(16, dtype=np.uint64) * np.uint64(3))) & np.uint64(7)).astype(np.int32)
    sel += (np.arange(len(blk), dtype=np.int32) * 8)[:, None]
    return pal.clip(0, 255).astype(np.uint8).ravel()[sel]


def decode_pixels(fmt, data, w, h):
    np = _np()
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    buf = np.frombuffer(data, np.uint8)
    if fmt in ("PF_DXT1", "PF_BC1"):
        blk = buf[:bw * bh * 8].reshape(-1, 8)
        return _bc_layout(_bc1_colors(blk, False), w, h, bw, bh, 4)
    if fmt in ("PF_DXT5", "PF_BC3", "PF_DXT3"):
        blk = buf[:bw * bh * 16].reshape(-1, 16)
        col = _bc1_colors(blk[:, 8:], True)
        if fmt == "PF_DXT3":
            a = blk[:, :8]
            alpha = np.stack([(a[:, k // 2] >> (4 * (k % 2))) & 15 for k in range(16)], 1) * 17
            col[:, :, 3] = alpha.astype(np.uint8)
        else:
            col[:, :, 3] = _bc4_values(blk[:, :8])
        return _bc_layout(col, w, h, bw, bh, 4)
    if fmt in ("PF_BC4", "PF_ATI1"):
        blk = buf[:bw * bh * 8].reshape(-1, 8)
        r = _bc4_values(blk)
        out = np.stack([r, r, r, np.full_like(r, 255)], 2)
        return _bc_layout(out, w, h, bw, bh, 4)
    if fmt in ("PF_BC5", "PF_ATI2"):
        blk = buf[:bw * bh * 16].reshape(-1, 16)
        r = _bc4_values(blk[:, :8]).astype(np.float32)
        g = _bc4_values(blk[:, 8:]).astype(np.float32)
        x, y = r / 127.5 - 1, g / 127.5 - 1
        z = np.sqrt(np.clip(1 - x * x - y * y, 0, 1))
        b = (z * 127.5 + 127.5)
        out = np.stack([r, g, b, np.full_like(r, 255)], 2).clip(0, 255).astype(np.uint8)
        return _bc_layout(out, w, h, bw, bh, 4)
    if fmt == "PF_B8G8R8A8":
        return buf[:w * h * 4].reshape(h, w, 4)[:, :, [2, 1, 0, 3]].copy()
    if fmt == "PF_R8G8B8A8":
        return buf[:w * h * 4].reshape(h, w, 4).copy()
    if fmt in ("PF_G8", "PF_L8", "PF_A8"):
        g = buf[:w * h].reshape(h, w)
        return np.stack([g, g, g, np.full_like(g, 255)], 2)
    raise ValueError("Pixelformat %s wird (noch) nicht unterstuetzt" % fmt)


def write_png(path, rgba, keep_alpha=True):
    np = _np()
    h, w = rgba.shape[:2]
    if keep_alpha:
        raw, ctype, ch = rgba, 6, 4
    else:
        raw, ctype, ch = rgba[:, :, :3], 2, 3
    rows = np.empty((h, w * ch + 1), np.uint8)
    rows[:, 0] = 0
    rows[:, 1:] = np.ascontiguousarray(raw).reshape(h, w * ch)

    def chunk(t, b):
        c = struct.pack(">I", len(b)) + t + b
        return c + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)))
        fh.write(chunk(b"IEND", b""))


# ===========================================================================
#  Materialinstanzen (Texturparameter ohne Mapping-Datei)
# ===========================================================================
class MaterialInfo:
    def __init__(self, name):
        self.name = name
        self.textures = {}         # Parametername -> /Game/... Texturpfad
        self.vectors = {}          # Parametername -> (r,g,b,a)
        self.scalars = {}          # Parametername -> float
        self.master = ""           # Name des Master-Materials
        self.chain = []            # Eltern-Kette


def _scan_texture_params(pkg, d):
    nn = len(pkg.names)
    tex_imports = {-(i + 1) for i, (c, o, n) in enumerate(pkg.imports) if c.startswith("Texture")}
    out = {}
    for i in range(0, len(d) - 17):
        ni, num = struct.unpack_from("<ii", d, i)
        if not (0 <= ni < nn and num == 0):
            continue
        if d[i + 8] > 2:
            continue
        idx, obj = struct.unpack_from("<ii", d, i + 9)
        if idx == -1 and obj in tex_imports:
            pname = pkg.names[ni]
            if pname not in out:
                out[pname] = pkg.import_package(obj)
    return out


def _scan_scalar_params(pkg, d):
    """FScalarParameterValue: [00 07 00 07][FName][u8 Assoc][i32 -1][f32][16 B GUID]"""
    import math
    nn = len(pkg.names)
    out = {}
    pre = b"\x00\x07\x00\x07"
    st = 0
    while True:
        i = d.find(pre, st)
        if i < 0 or i + 4 + 17 + 16 > len(d):
            break
        st = i + 1
        o = i + 4
        ni, num = struct.unpack_from("<ii", d, o)
        if not (0 <= ni < nn and num == 0) or d[o + 8] > 2:
            continue
        idx, val = struct.unpack_from("<if", d, o + 9)
        if idx != -1 or not math.isfinite(val) or abs(val) > 1e6:
            continue
        if struct.unpack_from("<i", d, o + 13)[0] < 0 and abs(val) < 1e-30:
            continue
        out.setdefault(pkg.names[ni], val)
    return out


def _scan_vector_params(pkg, d):
    nn = len(pkg.names)
    out = {}
    cand = {i for i, n in enumerate(pkg.names) if re.search(r"colou?r|tint", n, re.I)}
    if not cand:
        return out
    for i in range(0, len(d) - 29):
        ni, num = struct.unpack_from("<ii", d, i)
        if ni not in cand or num != 0 or d[i + 8] > 2:
            continue
        idx = struct.unpack_from("<i", d, i + 9)[0]
        if idx != -1:
            continue
        r, g, b, a = struct.unpack_from("<4f", d, i + 13)
        if all(0.0 <= v <= 20.0 for v in (r, g, b)) and 0.0 <= a <= 1.0001:
            out.setdefault(pkg.names[ni], (r, g, b, a))
    return out


def load_material(content_root, game_path, cache, depth=0):
    if game_path in cache:
        return cache[game_path]
    name = game_path.rsplit("/", 1)[-1]
    info = MaterialInfo(name)
    cache[game_path] = info
    base = game_path_to_file(content_root, game_path)
    if not base or not os.path.isfile(base + ".uasset"):
        return info
    try:
        pkg = Package(base)
    except Exception:
        return info
    _, d = pkg.export_data("MaterialInstanceConstant")
    if d is None:
        info.master = name                       # ist selbst ein Master-Material
        return info
    parent = None
    for i, (c, o, n) in enumerate(pkg.imports):
        if c in ("Material", "MaterialInstanceConstant") and n != name:
            parent = (c, pkg.import_package(-(i + 1)), n)
            break
    if parent and depth < 6:
        pinfo = load_material(content_root, parent[1], cache, depth + 1) if parent[1] else None
        if pinfo:
            info.textures.update(pinfo.textures)
            info.vectors.update(pinfo.vectors)
            info.scalars.update(pinfo.scalars)
            info.master = pinfo.master or parent[2]
            info.chain = [parent[2]] + pinfo.chain
        else:
            info.master = parent[2]
    info.textures.update(_scan_texture_params(pkg, d))
    info.vectors.update(_scan_vector_params(pkg, d))
    tex_names = set(info.textures)
    info.scalars.update({k: v for k, v in _scan_scalar_params(pkg, d).items() if k not in tex_names})
    return info


def classify_textures(info, params=None):
    """-> dict(base=..., normal=..., orm=..., emissive=...) Pfade.
    params (dict) bekommt je Rolle den Parameternamen."""
    roles = {}
    if params is None:
        params = {}

    def bad(n):
        n = n.lower()
        return any(k in n for k in ("damage", "dirt", "rain", "flake", "scratch", "bottom", "dust", "mud",
                                    "default_", "dummy"))

    order = sorted(info.textures.items(), key=lambda kv: kv[0].lower())
    for pname, path in order:
        if not path:
            continue
        pl = pname.lower()
        tl = path.rsplit("/", 1)[-1].lower()
        if bad(pl) or bad(tl):
            continue
        if ("albedo" in pl or "basecolor" in pl or "base color" in pl or "diffuse" in pl) and "base" not in roles:
            roles["base"] = path
            params["base"] = pname
        elif "normal" in pl and "normal" not in roles:
            roles["normal"] = path
            params["normal"] = pname
        elif re.search(r"(^|[^a-z])orm([^a-z]|$)", pl) and "orm" not in roles:
            roles["orm"] = path
            params["orm"] = pname
        elif "emissive" in pl and "emissive" not in roles:
            roles["emissive"] = path
            params["emissive"] = pname
    for pname, path in order:                    # Fallback ueber Texturnamen
        if not path:
            continue
        tl = path.rsplit("/", 1)[-1].lower()
        if bad(tl):
            continue
        if re.search(r"_(a|d|albedo|diffuse|basecolor)$", tl):
            roles.setdefault("base", path)
        elif re.search(r"_n$", tl):
            roles.setdefault("normal", path)
        elif re.search(r"_orm$", tl):
            roles.setdefault("orm", path)
        elif "emissive" in tl:
            roles.setdefault("emissive", path)
    return roles


def uv_tiling(info):
    """Kachelung fuer '...tile'-Texturen (z.B. Carbon: 'UV Tile' = 120)."""
    for k, v in sorted(info.scalars.items()):
        kl = k.lower()
        if any(b in kl for b in ("damage", "dirt", "rain", "flake", "sticker", "scratch")):
            continue
        if re.search(r"uv\s*til|tiling|tile\s*scale|uv\s*scale", kl) and 0.01 < v < 10000:
            return v
    return None


def material_kind(info):
    s = " ".join([info.name, info.master] + info.chain).lower()
    if "glass" in s or "window" in s and "sticker" not in s:
        return "glass"
    if any(k in s for k in ("sticker", "decal", "net", "screws", "grille", "transparent", "masked", "shadow")):
        return "masked"
    return "opaque"


# ===========================================================================
#  Fahrzeug sammeln (ohne Blender testbar)
# ===========================================================================
def split_vehicle_arg(arg):
    """Pfad zu einem Fahrzeugordner -> (content_root|None, fahrzeugname)"""
    a = arg.strip().strip('"').rstrip("\\/")
    if os.path.isdir(a) and (os.sep in a or "/" in a):
        a = os.path.abspath(a)
        parent = os.path.dirname(a)
        content = os.path.dirname(parent) if os.path.basename(parent).lower() == "vehicles" else None
        return content, os.path.basename(a)
    return None, a


def find_vehicle_dir(content_root, car):
    c2, car = split_vehicle_arg(car)
    if c2:
        content_root = c2
    vdir = os.path.join(content_root, "Vehicles")
    if os.path.isdir(os.path.join(vdir, car)):
        return os.path.join(vdir, car), car
    if os.path.isdir(vdir):
        for d in os.listdir(vdir):
            if d.lower() == car.lower():
                return os.path.join(vdir, d), d
    return None, car


def list_vehicles(content_root):
    vdir = os.path.join(content_root, "Vehicles")
    out = []
    if os.path.isdir(vdir):
        for d in sorted(os.listdir(vdir)):
            if glob.glob(os.path.join(vdir, d, "SM_*.uasset")):
                out.append(d)
    return out


def _needs_offset(m, bone_loc):
    """True, wenn das Mesh lokal um seinen Drehpunkt modelliert ist (dann auf Bone verschieben).
    False, wenn es schon im Fahrzeugraum nahe am Bone liegt (sonst doppelte Verschiebung)."""
    np = _np()
    c = (m.positions.min(0) + m.positions.max(0)) / 2.0
    b = np.array(bone_loc, dtype=np.float64)
    return float(np.linalg.norm(c)) <= float(np.linalg.norm(c - b))


def find_static_meshes(folder):
    """Alle StaticMesh-Assets im Ordner (auch ohne 'SM_'-Praefix, z.B. Audi_R8_..._Net)."""
    out = []
    for f in sorted(glob.glob(os.path.join(folder, "*.uasset"))):
        n = os.path.basename(f).lower()
        if n.startswith(("t_", "mi_", "m_", "sk_", "mm_", "abp_", "bp_")):
            continue
        if n.startswith("sm_"):
            out.append(f)
            continue
        try:
            pkg = Package(f[:-7])
            if any(pkg.ref_name(e["cls"]) == "StaticMesh" for e in pkg.exports):
                out.append(f)
        except Exception:
            pass
    return out


def find_generated_wheels(content_root, car):
    """Content/Vehicles/All_Cars_Rim_Generated_Wheels/Generated_Wheels/<Auto>/SM_*_Front/Rear_Wheel"""
    base = os.path.join(content_root, "Vehicles", "All_Cars_Rim_Generated_Wheels", "Generated_Wheels")
    if not os.path.isdir(base):
        return []
    want = _norm(car)
    for d in sorted(os.listdir(base)):
        if _norm(d) == want:
            out = []
            for f in find_static_meshes(os.path.join(base, d)):
                t = set(_token_list(os.path.basename(f)[:-7]))
                if "wheel" in t and ("front" in t or "rear" in t) and "blur" not in t:
                    out.append(f)
            return out
    return []


def collect_vehicle(content_root, car, with_damage=False, use_generated_wheels=True):
    vdir, car = find_vehicle_dir(content_root, car)
    if not vdir:
        raise FileNotFoundError("Fahrzeugordner '%s' nicht gefunden unter %s" % (car, os.path.join(content_root, "Vehicles")))
    content_root = os.path.dirname(os.path.dirname(os.path.abspath(vdir)))
    bones = {}
    sk = os.path.join(vdir, "SK_%s" % car)
    cands = [sk, sk + "_Skeleton"]
    cands += [f[:-7] for f in sorted(glob.glob(os.path.join(vdir, "**", "SK_*.uasset"), recursive=True))
              if "physics" not in f.lower() and "suspension" not in f.lower()]
    if not any(os.path.isfile(c + ".uasset") for c in cands):
        log("  [i] Kein Skelett im Fahrzeugordner - suche im Content nach SK_%s* ..." % car)
        for f in sorted(glob.glob(os.path.join(content_root, "**", "SK_%s*.uasset" % car), recursive=True)):
            if "physics" not in f.lower() and "suspension" not in f.lower():
                cands.append(f[:-7])
                log("      gefunden: %s" % os.path.relpath(f, content_root))
    for cand in cands:
        if os.path.isfile(cand + ".uasset"):
            try:
                bones = load_bones(cand)
            except Exception as ex:
                log("  [!] Skelett %s: %s" % (os.path.basename(cand), ex))
            if bones:
                break
    tech = [f[:-7] for f in sorted(glob.glob(os.path.join(vdir, "**", "SK_*Suspension_Technical.uasset"), recursive=True))]
    for t in tech:
        try:
            tb = load_bones(t)
        except Exception:
            tb = {}
        added = 0
        for k, v in tb.items():
            if k not in bones:
                bones[k] = v
                added += 1
        if added:
            log("  [i] %d Bones aus %s ergaenzt" % (added, os.path.basename(t)))
    log("  Skelett: %d Bones" % len(bones))
    if not bones:
        log("  [!] Kein Skelett gelesen - bewegliche Teile (Tueren, Raeder ...) bleiben im Ursprung!")

    parts = []
    files = find_static_meshes(vdir)
    if use_generated_wheels:
        gen = find_generated_wheels(content_root, car)
        if gen:
            keys = {frozenset(_token_list(os.path.basename(g)[:-7])) & {"front", "rear", "wheel"} for g in gen}
            files = [f for f in files
                     if (frozenset(_token_list(os.path.basename(f)[:-7])) & {"front", "rear", "wheel"}) not in keys
                     or "blur" in f.lower()]
            files += gen
            log("  [i] Raeder aus All_Cars_Rim_Generated_Wheels: %s" % ", ".join(os.path.basename(g)[:-7] for g in gen))
    if with_damage:
        files += find_static_meshes(os.path.join(vdir, "Damage_DD_Parts"))
    for f in files:
        base = f[:-7]
        name = os.path.basename(base)
        if any(k in name.lower() for k in SKIP_PARTS):
            continue
        group = "Damage" if "Damage_DD_Parts" in f else "Main"
        try:
            m = load_static_mesh(base)
        except Exception as ex:
            log("  [!] %s: %s" % (name, ex))
            continue
        left, right, bone = part_placement(name, car, bones)
        if left is not None and not _needs_offset(m, left):
            log("  [i] %s liegt bereits im Fahrzeugraum (Bone %s ignoriert)" % (name, bone))
            left, right, bone = None, None, None
        parts.append({"mesh": m, "group": group, "loc": left, "loc_right": right, "bone": bone, "rot": None})

    # Teile ohne Bone: gleichnamigen Socket in einem anderen Mesh suchen (z.B. Interior -> SteeringWheel)
    sockets = {}
    for p in parts:
        if p["group"] != "Main":
            continue
        owner_loc = p["loc"] or (0.0, 0.0, 0.0)
        prio = len(p["mesh"].sockets)          # Haupt-Interior hat die meisten Sockets
        for sname, sloc, srot in p["mesh"].sockets:
            key = "".join(_token_list(sname))
            if key not in sockets or prio > sockets[key][4]:
                wl = (owner_loc[0] + sloc[0], owner_loc[1] + sloc[1], owner_loc[2] + sloc[2])
                sockets[key] = (sname, wl, srot, p["mesh"].name, prio)
    pre = ("sm_" + car + "_").lower()
    for p in parts:
        if p["loc"] is not None:
            continue
        nm = p["mesh"].name.lower()
        suffix = nm[len(pre):] if nm.startswith(pre) else nm
        hit = None
        for suf in name_suffixes(suffix, 1):
            key = "".join(suf)
            if len(key) >= 4 and key not in ("left", "right", "front", "rear") and key in sockets:
                hit = sockets[key]
                break
        if hit and hit[3] != p["mesh"].name:
            if _needs_offset(p["mesh"], hit[1]):
                p["loc"], p["rot"], p["bone"] = hit[1], hit[2], "Socket %s (%s)" % (hit[0], hit[3])
            else:
                p["bone"] = "liegt bereits im Fahrzeugraum (Socket %s)" % hit[0]
    for p in parts:
        m = p["mesh"]
        tag = ("  -> " + p["bone"]) if p["bone"] else ""
        if not p["bone"] and p["group"] == "Main" and (BlenderBuilder._looks_local(m) or
                                                       any(k in m.name.lower() for k in LOCAL_PARTS)):
            tag = "  [!] OHNE POSITION"
        log("  Mesh %-45s %6d Vert. %d Mat.%s" % (m.name, len(m.positions), len(m.materials), tag))
    return car, vdir, parts


# ===========================================================================
#  Blender-Teil
# ===========================================================================
def _socket(node, *names):
    for n in names:
        s = node.inputs.get(n)
        if s is not None:
            return s
    return None


class BlenderBuilder:
    def __init__(self, content_root, out_dir, car, tex_dir, pack=False, max_tex=0):
        self.content_root = content_root
        self.out_dir = out_dir
        self.car = car
        self.tex_dir = tex_dir
        self.pack = pack
        self.max_tex = max_tex
        self.mat_cache = {}
        self.mi_cache = {}
        self.img_cache = {}

    # ---- Texturen -----------------------------------------------------------
    def image(self, game_path, non_color=False):
        import bpy
        key = (game_path, non_color)
        if key in self.img_cache:
            return self.img_cache[key]
        img = None
        base = game_path_to_file(self.content_root, game_path)
        name = game_path.rsplit("/", 1)[-1]
        png = os.path.join(self.tex_dir, name + ".png")
        try:
            if not os.path.isfile(png):
                if not base or not os.path.isfile(base + ".uasset"):
                    raise FileNotFoundError("nicht entpackt: " + game_path)
                t = load_texture(base, self.max_tex)
                rgba = t["rgba"]
                if non_color and t["format"] in ("PF_BC5", "PF_ATI2") or name.lower().endswith("_n"):
                    rgba = rgba.copy()
                    rgba[:, :, 1] = 255 - rgba[:, :, 1]          # DirectX -> OpenGL
                has_alpha = bool((rgba[:, :, 3] < 250).any())
                write_png(png, rgba, keep_alpha=has_alpha)
            img = bpy.data.images.load(png, check_existing=True)
            if non_color:
                img.colorspace_settings.name = "Non-Color"
        except Exception as ex:
            log("    [!] Textur %s: %s" % (name, ex))
        self.img_cache[key] = img
        return img

    # ---- Materialien ----------------------------------------------------------
    def material(self, mi_name, game_path):
        import bpy
        key = game_path or mi_name or "None"
        if key in self.mat_cache:
            return self.mat_cache[key]
        mat = bpy.data.materials.new(mi_name or "Material")
        mat.use_nodes = True
        self.mat_cache[key] = mat
        info = load_material(self.content_root, game_path, self.mi_cache) if game_path else MaterialInfo(mi_name or "")
        role_params = {}
        roles = classify_textures(info, role_params)
        kind = material_kind(info)
        tiling = uv_tiling(info)
        mat["rennsport_scalars"] = ", ".join("%s=%g" % kv for kv in sorted(info.scalars.items()))
        mat["rennsport_master"] = info.master or ""
        mat["rennsport_textures"] = ", ".join("%s=%s" % (k, v.rsplit("/", 1)[-1]) for k, v in sorted(info.textures.items()))

        nt = mat.node_tree
        nodes, links = nt.nodes, nt.links
        bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
        out = next((n for n in nodes if n.type == "OUTPUT_MATERIAL"), None)
        if bsdf is None:
            bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        if out is None:
            out = nodes.new("ShaderNodeOutputMaterial")
        if not out.inputs["Surface"].is_linked:
            links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
        bsdf.location = (300, 0)
        out.location = (600, 0)
        y = 300

        mapping = []
        tiled_paths = {p for n, p in info.textures.items() if p and "til" in n.lower()}

        def tex_node(path, non_color, role=None):
            nonlocal y
            img = self.image(path, non_color)
            if img is None:
                return None
            n = nodes.new("ShaderNodeTexImage")
            n.image = img
            n.location = (-500, y)
            y -= 300
            if tiling and (path in tiled_paths or (role and "til" in role_params.get(role, "").lower())):
                if not mapping:
                    tc = nodes.new("ShaderNodeTexCoord")
                    tc.location = (-1100, 300)
                    mp = nodes.new("ShaderNodeMapping")
                    mp.location = (-850, 300)
                    mp.inputs["Scale"].default_value = (tiling, tiling, 1.0)
                    mp.label = "UV Tile %g" % tiling
                    links.new(tc.outputs["UV"], mp.inputs["Vector"])
                    mapping.append(mp)
                links.new(mapping[0].outputs["Vector"], n.inputs["Vector"])
            return n

        base_node = tex_node(roles["base"], False, "base") if "base" in roles else None
        if base_node:
            links.new(base_node.outputs["Color"], _socket(bsdf, "Base Color"))
            if kind == "masked" and base_node.image and base_node.image.depth in (32, 64, 128):
                links.new(base_node.outputs["Alpha"], _socket(bsdf, "Alpha"))
        else:
            col = None
            for k, v in info.vectors.items():
                col = v
                break
            if col:
                _socket(bsdf, "Base Color").default_value = (col[0], col[1], col[2], 1.0)

        if "orm" in roles:
            n = tex_node(roles["orm"], True, "orm")
            if n:
                try:
                    sep = nodes.new("ShaderNodeSeparateColor")
                    outs = ("Red", "Green", "Blue")
                except RuntimeError:
                    sep = nodes.new("ShaderNodeSeparateRGB")
                    outs = ("R", "G", "B")
                sep.location = (-150, n.location[1])
                links.new(n.outputs["Color"], sep.inputs[0])
                links.new(sep.outputs[outs[1]], _socket(bsdf, "Roughness"))
                links.new(sep.outputs[outs[2]], _socket(bsdf, "Metallic"))

        if "normal" in roles:
            n = tex_node(roles["normal"], True, "normal")
            if n:
                nm = nodes.new("ShaderNodeNormalMap")
                nm.location = (-150, n.location[1])
                links.new(n.outputs["Color"], nm.inputs["Color"])
                links.new(nm.outputs["Normal"], _socket(bsdf, "Normal"))

        if "emissive" in roles:
            n = tex_node(roles["emissive"], False)
            if n:
                s = _socket(bsdf, "Emission Color", "Emission")
                if s:
                    links.new(n.outputs["Color"], s)
                st = _socket(bsdf, "Emission Strength")
                if st:
                    st.default_value = 0.0       # bei Bedarf in Blender hochdrehen

        cc = {k.lower(): v for k, v in info.scalars.items() if k.lower().startswith("clearcoat")}
        if cc:
            cw = _socket(bsdf, "Coat Weight", "Clearcoat")
            if cw:
                cw.default_value = 1.0
            cr = _socket(bsdf, "Coat Roughness", "Clearcoat Roughness")
            if cr and "clearcoat roughness" in cc:
                cr.default_value = max(0.0, min(1.0, cc["clearcoat roughness"]))

        if kind == "glass":
            col = next(iter(info.vectors.values()), None)
            if col and not base_node:
                _socket(bsdf, "Base Color").default_value = (col[0], col[1], col[2], 1.0)
            elif not base_node:
                _socket(bsdf, "Base Color").default_value = (0.02, 0.02, 0.02, 1.0)
            _socket(bsdf, "Roughness").default_value = 0.05
            if not base_node:
                _socket(bsdf, "Alpha").default_value = 0.35
            elif not _socket(bsdf, "Alpha").is_linked and base_node.image and base_node.image.depth in (32, 64, 128):
                links.new(base_node.outputs["Alpha"], _socket(bsdf, "Alpha"))
            if hasattr(mat, "blend_method"):
                mat.blend_method = "BLEND"
            if hasattr(mat, "shadow_method"):
                mat.shadow_method = "HASHED"
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "BLENDED"
        elif kind == "masked" and _socket(bsdf, "Alpha").is_linked:
            if hasattr(mat, "blend_method"):
                mat.blend_method = "CLIP"
            if hasattr(mat, "shadow_method"):
                mat.shadow_method = "CLIP"
        return mat

    # ---- Meshes ---------------------------------------------------------------
    def mesh(self, m):
        import bpy
        np = _np()
        P = m.positions.astype(np.float32) * 0.01
        P[:, 1] *= -1                            # UE (linkshaendig) -> Blender
        tris = m.indices[:, [0, 2, 1]].astype(np.int32)  # Wicklung wegen Spiegelung umdrehen
        mat_all = np.zeros(len(tris), np.int32)
        for mi, first, ntri in m.sections:
            mat_all[first // 3:first // 3 + ntri] = mi
        keep = ((tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2]) &
                (tris.max(1) < len(P)))
        tris, mat_idx = tris[keep], mat_all[keep]
        nv, nt = len(P), len(tris)
        me = bpy.data.meshes.new(m.name)
        loop_v = tris.ravel()
        try:
            me.vertices.add(nv)
            me.vertices.foreach_set("co", P.ravel())
            me.loops.add(nt * 3)
            me.loops.foreach_set("vertex_index", loop_v)
            me.polygons.add(nt)
            me.polygons.foreach_set("loop_start", np.arange(0, nt * 3, 3, dtype=np.int32))
            try:
                me.polygons.foreach_set("loop_total", np.full(nt, 3, np.int32))
            except (AttributeError, TypeError, RuntimeError):
                pass
            me.update(calc_edges=True)
        except Exception:
            me = bpy.data.meshes.new(m.name)
            me.from_pydata(P.tolist(), [], tris.tolist())
            me.update(calc_edges=True)

        nslots = max(len(m.materials), int(mat_idx.max()) + 1 if nt else 1)
        for k in range(nslots):
            if k < len(m.materials):
                mi_name, gp, slot = m.materials[k]
                me.materials.append(self.material(mi_name or slot, gp))
            else:
                me.materials.append(None)
        me.polygons.foreach_set("material_index", mat_idx)
        me.polygons.foreach_set("use_smooth", np.ones(nt, bool))

        for k, uv in enumerate(m.uvs):
            lay = me.uv_layers.new(name="UV%d" % k if k else "UVMap")
            if lay is None:
                break
            u = uv.copy()
            u[:, 1] = 1.0 - u[:, 1]
            lay.data.foreach_set("uv", u[loop_v].ravel())

        if m.colors is not None:
            try:
                ca = me.color_attributes.new("Col", "BYTE_COLOR", "POINT")
                ca.data.foreach_set("color", (m.colors.astype(np.float32) / 255.0).ravel())
            except Exception:
                pass

        N = m.normals.copy()
        N[:, 1] *= -1
        ln = np.linalg.norm(N, axis=1, keepdims=True)
        N = N / np.maximum(ln, 1e-6)
        if hasattr(me, "use_auto_smooth"):
            me.use_auto_smooth = True
        try:
            me.normals_split_custom_set_from_vertices(N.tolist())
        except Exception as ex:
            log("    [!] Normalen %s: %s" % (m.name, ex))
        me.update()
        return me

    def build(self, parts):
        import bpy
        import math
        root = bpy.data.collections.new(self.car)
        bpy.context.scene.collection.children.link(root)
        colls = {"Main": root}
        if any(p["group"] == "Damage" for p in parts):
            dc = bpy.data.collections.new(self.car + "_Damage")
            root.children.link(dc)
            colls["Damage"] = dc
        unplaced = None

        for p in parts:
            m = p["mesh"]
            log("  Baue %s" % m.name)
            me = self.mesh(m)
            obj = bpy.data.objects.new(m.name, me)
            coll = colls[p["group"]]
            if p["loc"] is not None:
                x, y, z = p["loc"]
                obj.location = (x * 0.01, -y * 0.01, z * 0.01)
                obj["rennsport_bone"] = p["bone"]
                if p.get("rot") and any(abs(v) > 1e-6 for v in p["rot"]):
                    from mathutils import Matrix
                    obj.rotation_euler = Matrix(ue_rotator_to_blender_matrix(*p["rot"])).to_euler("XYZ")
            elif p["group"] == "Main" and (self._looks_local(m) or
                                           any(k in m.name.lower() for k in LOCAL_PARTS)):
                if unplaced is None:
                    unplaced = bpy.data.collections.new(self.car + "_Ohne_Position")
                    root.children.link(unplaced)
                coll = unplaced
            coll.objects.link(obj)
            if p["loc_right"] is not None:
                o2 = bpy.data.objects.new(m.name + "_Right", me)
                x, y, z = p["loc_right"]
                o2.location = (x * 0.01, -y * 0.01, z * 0.01)
                o2.rotation_euler = (0.0, 0.0, math.pi)
                coll.objects.link(o2)
        if "Damage" in colls:
            colls["Damage"].hide_render = True
            lc = bpy.context.view_layer.layer_collection.children[root.name].children.get(colls["Damage"].name)
            if lc:
                lc.exclude = True

    @staticmethod
    def _looks_local(m):
        np = _np()
        c = (m.positions.min(0) + m.positions.max(0)) / 2
        ext = m.positions.max(0) - m.positions.min(0)
        return bool(np.linalg.norm(c) < 25 and ext.max() < 80)


def setup_viewport():
    """Gespeicherte Datei oeffnet mit 'Material Preview' und Blick auf das Auto."""
    import bpy
    from mathutils import Vector, Euler
    pts = []
    for o in bpy.data.objects:
        if o.type == "MESH" and o.visible_get():
            for c in o.bound_box:
                pts.append(o.matrix_world @ Vector(c))
    if pts:
        lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        center, size = (lo + hi) / 2, max((hi - lo).length, 1.0)
    else:
        center, size = Vector((0, 0, 0)), 5.0
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                try:
                    space.shading.type = "MATERIAL"
                    space.clip_end = max(space.clip_end, size * 20)
                    r3d = space.region_3d
                    if r3d:
                        r3d.view_location = center
                        r3d.view_distance = size * 1.1
                        r3d.view_rotation = Euler((1.15, 0.0, 0.75)).to_quaternion()
                        r3d.view_perspective = "PERSP"
                except Exception:
                    pass


def blender_main(argv):
    import bpy
    ap = argparse.ArgumentParser(prog="rennsport_blender (Blender)")
    ap.add_argument("cars", nargs="+")
    ap.add_argument("--content", required=True)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--pack", action="store_true")
    ap.add_argument("--damage", action="store_true")
    ap.add_argument("--max-tex", type=int, default=0)
    ap.add_argument("--car-wheels", action="store_true")
    args = ap.parse_args(argv)
    os.makedirs(args.output, exist_ok=True)
    ok = 0
    for car in args.cars:
        t0 = time.time()
        log("\n=== %s ===" % car)
        try:
            bpy.ops.wm.read_factory_settings(use_empty=True)
            car_name, vdir, parts = collect_vehicle(args.content, car, args.damage, not args.car_wheels)
            if not parts:
                log("  [!] keine Meshes gefunden")
                continue
            tex_dir = os.path.join(args.output, car_name + "_textures")
            os.makedirs(tex_dir, exist_ok=True)
            b = BlenderBuilder(args.content, args.output, car_name, tex_dir, args.pack, args.max_tex)
            b.build(parts)
            scene = bpy.context.scene
            scene.unit_settings.system = "METRIC"
            setup_viewport()
            out = os.path.join(args.output, car_name + ".blend")
            if args.pack:
                bpy.ops.file.pack_all()
            bpy.ops.wm.save_as_mainfile(filepath=out, compress=True)
            if not args.pack:
                try:
                    bpy.ops.file.make_paths_relative()
                    bpy.ops.wm.save_as_mainfile(filepath=out, compress=True)
                except Exception:
                    pass
            log("  -> %s  (%.1fs)" % (out, time.time() - t0))
            ok += 1
        except Exception as ex:
            import traceback
            log("  [FEHLER] %s\n%s" % (ex, traceback.format_exc()))
    log("\nFertig: %d von %d Fahrzeugen." % (ok, len(args.cars)))


# ===========================================================================
#  Launcher (normales Python)
# ===========================================================================
def load_cfg():
    cfg = configparser.ConfigParser()
    if os.path.isfile(INI_PATH):
        cfg.read(INI_PATH, encoding="utf-8")
    if not cfg.has_section("Paths"):
        cfg.add_section("Paths")
    return cfg


def save_cfg(cfg):
    with open(INI_PATH, "w", encoding="utf-8") as fh:
        fh.write("; Rennsport -> Blender Einstellungen\n")
        fh.write("; content = ...\\Rennsport-Windows_extracted\\Rennsport\\Content\n")
        fh.write("; blender = Pfad zu blender.exe (4.0 empfohlen)\n")
        fh.write("; output  = Standard-Ausgabeordner (optional)\n\n")
        cfg.write(fh)


def _norm_content(p):
    p = os.path.abspath(p.strip().strip('"'))
    for cand in (p, os.path.join(p, "Content"), os.path.join(p, "Rennsport", "Content")):
        if os.path.isdir(os.path.join(cand, "Vehicles")):
            return cand
    return None


def find_content(cfg):
    c = cfg.get("Paths", "content", fallback="").strip()
    if c and _norm_content(c):
        return _norm_content(c)
    for d in [os.getcwd(), SCRIPT_DIR]:
        for p in glob.glob(os.path.join(d, "*_extracted")) + [d]:
            n = _norm_content(p)
            if n:
                return n
    return None


def find_blender(cfg):
    b = cfg.get("Paths", "blender", fallback="").strip().strip('"')
    if b and os.path.isfile(b):
        return b
    cands = []
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        cands += glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe"))
    cands += glob.glob(r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe")
    for drive in "DEFGH":
        cands += glob.glob(drive + r":\*\steamapps\common\Blender\blender.exe")
        cands += glob.glob(drive + r":\*\*\steamapps\common\Blender\blender.exe")
    cands += ["/usr/bin/blender", "/Applications/Blender.app/Contents/MacOS/Blender"]
    cands = [c for c in cands if os.path.isfile(c)]

    def ver(p):
        m = re.search(r"Blender\s*(\d+)\.(\d+)", p)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    cands.sort(key=lambda p: (ver(p) != (4, 0), [-x for x in ver(p)]))
    return cands[0] if cands else None


def ask(prompt):
    try:
        return input(prompt).strip().strip('"')
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAbgebrochen.")


def launcher_main():
    ap = argparse.ArgumentParser(description="Rennsport-Fahrzeug -> .blend")
    ap.add_argument("cars", nargs="*", help="Fahrzeugordner, z.B. BMW_M4_GT3")
    ap.add_argument("-o", "--output", help="Ausgabeordner")
    ap.add_argument("--list", action="store_true", help="verfuegbare Fahrzeuge anzeigen")
    ap.add_argument("--pack", action="store_true", help="Texturen in die .blend packen")
    ap.add_argument("--damage", action="store_true", help="Schadensteile (Damage_DD_Parts) mit importieren")
    ap.add_argument("--max-tex", type=int, default=0, help="max. Texturgroesse, z.B. 2048 (0 = voll)")
    ap.add_argument("--car-wheels", action="store_true",
                    help="Raeder aus dem Fahrzeugordner statt All_Cars_Rim_Generated_Wheels verwenden")
    ap.add_argument("--content", help="Content-Ordner der entpackten PAK")
    ap.add_argument("--blender", help="Pfad zu blender.exe")
    args = ap.parse_args()

    cfg = load_cfg()
    from_path = None
    cars = []
    for c in args.cars:
        cr, name = split_vehicle_arg(c)
        if cr and not from_path:
            from_path = cr
        cars.append(name)
    args.cars = cars
    if args.content:
        content = _norm_content(args.content)
    elif from_path:
        content = _norm_content(from_path)
    else:
        content = find_content(cfg)
    while not content:
        p = ask("Pfad zum entpackten Ordner (z.B. ...\\Rennsport-Windows_extracted): ")
        content = _norm_content(p)
        if not content:
            print("  Dort gibt es keinen 'Vehicles'-Ordner.")
    if cfg.get("Paths", "content", fallback="") != content:
        cfg.set("Paths", "content", content)
        save_cfg(cfg)

    if args.list or not args.cars:
        cars = list_vehicles(content)
        print("Verfuegbare Fahrzeuge (%d):" % len(cars))
        for c in cars:
            print("  " + c)
        if not args.cars:
            if args.list:
                return
            c = ask("\nFahrzeug(e) eingeben (mit Leerzeichen getrennt): ")
            args.cars = c.split()
            if not args.cars:
                return

    blender = args.blender or find_blender(cfg)
    while not blender or not os.path.isfile(blender):
        blender = ask("Pfad zu blender.exe (Blender 4.0): ")
        if os.path.isdir(blender):
            blender = os.path.join(blender, "blender.exe")
    if cfg.get("Paths", "blender", fallback="") != blender:
        cfg.set("Paths", "blender", blender)
        save_cfg(cfg)

    out = args.output or cfg.get("Paths", "output", fallback="").strip() or os.path.join(os.getcwd(), "Blender_Export")
    out = os.path.abspath(out)
    cmd = [blender, "-b", "--factory-startup", "-noaudio", "-P", SCRIPT_PATH, "--",
           *args.cars, "--content", content, "-o", out]
    if args.pack:
        cmd.append("--pack")
    if args.damage:
        cmd.append("--damage")
    if args.max_tex:
        cmd += ["--max-tex", str(args.max_tex)]
    if args.car_wheels:
        cmd.append("--car-wheels")
    print("Blender : %s" % blender)
    print("Content : %s" % content)
    print("Ausgabe : %s\n" % out)
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    for line in proc.stdout:
        # Blender-eigenes Rauschen ausblenden
        if line.startswith(("Blender ", "Read prefs", "Read blend", "Info:", "Warning: Unable", "Color management",
                            "TBBmalloc")):
            continue
        sys.stdout.write(line)
    proc.wait()
    print("\nGesamtzeit %.1fs" % (time.time() - t0))
    if os.name == "nt" and len(sys.argv) <= 2:
        try:
            input("Enter zum Beenden...")
        except EOFError:
            pass


if __name__ == "__main__":
    if IN_BLENDER:
        argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
        blender_main(argv)
    else:
        launcher_main()
