#!/usr/bin/env python3
"""Remap orphaned asset paths in an ACDSee Visual FoxPro database.

Reads the ACDSee database, finds assets whose files no longer exist at the
recorded path, searches for them under a new image root, and updates the
database to point to the current locations.

Resolution levels:
  Level 1 - Path prefix remapping: volume/drive changed or folder renamed.
            Same filename found at the same relative path under a new root.
  Level 2 - Individual file matching: file moved elsewhere. Matched by
            filename + metadata (file size, dimensions, EXIF datetime,
            camera make/model).
  Level 3 - Metadata-only matching: file renamed and/or modified. Matched
            by EXIF signature (DateTimeOriginal + camera make/model/lens).

Usage:
  python remap_assets.py <db_dir> <image_root> [--dry-run] [--level {1,2,3}]
"""

import argparse
import concurrent.futures
import datetime
import errno
import json
import logging
import os
import shutil
import struct
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    HAS_TKINTER = True
except ImportError:
    HAS_TKINTER = False

# ---------------------------------------------------------------------------
# Unicode & codepage constants
# ---------------------------------------------------------------------------
# VFP codepage byte → Python codec name.
# Codepage 127 (0x7F) means "system default" — cp1252 is the dominant
# Windows encoding for Western locales.  Other values are standard Windows
# code-page identifiers.
_VFP_CODEPAGES: dict[int, str] = {
    0x00: "ascii",
    0x01: "cp437",
    0x02: "cp850",
    0x03: "cp1252",
    0x7F: "cp1252",  # FoxPro "unknown / system default"
    0x8C: "cp1250",  # Central European
    0x7D: "cp1251",  # Cyrillic
    0x88: "cp1253",  # Greek
}


def _encoding_for_codepage(cp_byte: int) -> str:
    return _VFP_CODEPAGES.get(cp_byte, "cp1252")


def normalize_path(path: str) -> str:
    """Normalize a path string for comparison: NFC form, forward slashes."""
    if not path:
        return ""
    return unicodedata.normalize("NFC", path).replace("\\", "/")


def normalize_for_compare(text: str) -> str:
    """Unicode NFC normalise + lowercase for case‑insensitive comparison.

    Real camera EXIF is frequently NUL-padded to a fixed field width -- a
    Make of ``'HTC'`` arrives as ``'HTC'`` followed by 90 NUL bytes.  Those
    are stripped here so equality comparisons behave the way callers expect.
    """
    return unicodedata.normalize("NFC", text).replace("\x00", "").strip().casefold()


def volume_splits(path: str) -> list[tuple[str, str]]:
    """Candidate (volume_prefix, remainder) splits for a database path.

    Level 1 works by swapping a volume prefix for a new location, so it needs
    to know where the volume ends and the relative path begins.

    A drive letter yields exactly one split.  A UNC path yields two: the
    database records only the server as the FolderRoot (``\\\\Miracle``) and
    stores the share (``photo``) as the first folder, so the mount point may
    correspond to either ``\\\\server`` or ``\\\\server/share``.  The
    share-level split is offered first because mounting a single share is the
    common case.  Anything else (e.g. the 'Pixel 3' MTP root) is split after
    its first component.

    Returns [] when there is nothing below the volume to match on.
    """
    p = normalize_path(path)
    if not p:
        return []
    head = p.split("/")[0]

    if ":" in head:                      # C:/..., E:/...
        prefix, rest = p.split(":", 1)
        rest = rest.lstrip("/")
        return [(f"{prefix}:", rest)] if rest else []

    parts = [seg for seg in p.split("/") if seg]
    if not parts:
        return []

    if p.startswith("//"):               # \\server/share/...
        splits = []
        if len(parts) > 2:
            splits.append(("//" + "/".join(parts[:2]), "/".join(parts[2:])))
        if len(parts) > 1:
            splits.append(("//" + parts[0], "/".join(parts[1:])))
        return splits

    return [(parts[0], "/".join(parts[1:]))] if len(parts) > 1 else []

# ---------------------------------------------------------------------------
# Network filesystem resilience
# ---------------------------------------------------------------------------
# When the image root is on a network share (Synology NAS over OpenVPN / SMB),
# transient failures such as EIO, ESTALE, or ENETRESET are normal.
# We retry with backoff for a limited number of attempts.

_RETRYABLE_ERRS = (
    errno.EIO, errno.ESTALE, errno.ENETRESET, errno.ECONNRESET,
    errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.EAGAIN,
    errno.ENODEV, getattr(errno, "EREMOTEIO", None),  # macOS cluster I/O
    getattr(errno, "ENETDOWN", None),
)
_RETRYABLE_ERRS = tuple(e for e in _RETRYABLE_ERRS if e is not None)
_MAX_RETRIES = 3
_RETRY_DELAY = 0.5  # base seconds


def _is_retryable(exc: OSError) -> bool:
    return exc.errno in _RETRYABLE_ERRS


def retry_on_network_error(
    func: Callable[[], Any], max_retries: int = _MAX_RETRIES
) -> Any:
    """Call *func*, retrying on transient network filesystem errors."""
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except OSError as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt == max_retries:
                raise
            time.sleep(_RETRY_DELAY * (2 ** attempt))
        except Exception:
            raise
    raise last_exc  # type: ignore[misc]


def safe_isfile(path: str) -> bool:
    """os.path.isfile with network retry."""
    return retry_on_network_error(lambda: os.path.isfile(path))


def safe_isdir(path: str) -> bool:
    """os.path.isdir with network retry."""
    return retry_on_network_error(lambda: os.path.isdir(path))


def safe_getsize(path: str) -> int:
    """os.path.getsize with network retry."""
    return retry_on_network_error(lambda: os.path.getsize(path))


def safe_exists(path: str) -> bool:
    """os.path.exists with network retry."""
    return retry_on_network_error(lambda: os.path.exists(path))

# ---------------------------------------------------------------------------
# VFP DBF handler
# ---------------------------------------------------------------------------

# Shared field parser for dbfread (handles VFP types 7, B, I)
_JULIAN_EPOCH = datetime.datetime(1858, 11, 17)
_JULIAN_OFFSET = 2400001


import dbfread as _dbfread_pkg
from dbfread import FieldParser as _FieldParser

class _VfpFieldParser(_FieldParser):
    """FieldParser subclass that handles VFP-specific field types 7, B, I."""

    def parse7(self, field, data):
        if data == b"\x00" * 8:
            return None
        jd, ms = struct.unpack("<II", data)
        if jd == 0:
            return None
        try:
            return _JULIAN_EPOCH + datetime.timedelta(
                days=jd - _JULIAN_OFFSET, milliseconds=ms
            )
        except (ValueError, OverflowError):
            return None

    def parseB(self, field, data):
        return struct.unpack("<d", data)[0]

    def parseI(self, field, data):
        return struct.unpack("<i", data)[0]

class VfpFile:
    """Read and write Visual FoxPro DBF files.

    Uses dbfread for reading (handles all VFP types including type 7, B, I)
    and raw byte manipulation for writing (update / append)."""

    def __init__(self, path: str):
        self.path = path
        self._fh: Any = None
        self._parse_header()
        self.encoding = _encoding_for_codepage(self.codepage)

    # -- header parsing -------------------------------------------------------

    def _parse_header(self) -> None:
        with open(self.path, "rb") as f:
            hdr = f.read(32)
            self.version = hdr[0]
            self.record_count = struct.unpack("<I", hdr[4:8])[0]
            self.header_length = struct.unpack("<H", hdr[8:10])[0]
            self.record_length = struct.unpack("<H", hdr[10:12])[0]
            self.codepage = hdr[29]

        with open(self.path, "rb") as f:
            raw = f.read(self.header_length)[32:]
        self.fields: list[dict] = []
        off = 0
        while off < len(raw) and raw[off] != 0x0D:
            block = raw[off : off + 32]
            name = block[:11].split(b"\x00")[0].decode("ascii")
            ftype = block[11]
            displacement = struct.unpack("<I", block[12:16])[0]
            self.fields.append({
                "name": name,
                "type": ftype,
                "displacement": displacement,
                "length": block[16],
                "decimals": block[17],
                "flags": block[18],
            })
            off += 32
        self._compute_lengths()

    def _compute_lengths(self) -> None:
        for i, fld in enumerate(self.fields):
            if i < len(self.fields) - 1:
                fld["length"] = self.fields[i + 1]["displacement"] - fld["displacement"]
            else:
                fld["length"] = self.record_length - fld["displacement"]

    def field_offset(self, field_name: str) -> tuple[int, int]:
        for fld in self.fields:
            if fld["name"] == field_name:
                return fld["displacement"], fld["length"]
        raise KeyError(f"field {field_name!r} not found in {self.path}")

    # -- reading --------------------------------------------------------------

    def read_all(self) -> list[dict]:
        """Read every record via dbfread (handles VFP types transparently)."""
        import dbfread
        from dbfread import FieldParser

        table = dbfread.DBF(
            self.path,
            load=False,
            ignore_missing_memofile=True,
            parserclass=_VfpFieldParser,
            encoding=self.encoding,
            char_decode_errors="replace",
        )
        records: list[dict] = []
        for rec in table:
            d: dict = {}
            for key, value in rec.items():
                if isinstance(value, bytes):
                    try:
                        value = value.decode(self.encoding, errors="replace").strip()
                        if not value:
                            value = None
                    except Exception:
                        pass
                if isinstance(value, str):
                    value = value.strip()
                    if not value:
                        value = None
                if isinstance(value, float):
                    if key.endswith("_ID") and value == int(value):
                        value = int(value)
                d[key] = value
            records.append(d)
        return records

    # -- writing ---------------------------------------------------------------

    _ENCODE_FIELD: dict[int, Any] = {}

    def _encode(self, ftype: int, value, length: int) -> bytes:
        if ftype == 0x43:  # Character
            raw = (str(value) if value else "").encode(self.encoding, errors="replace")
            if len(raw) > length:
                raw = raw[:length]
            else:
                raw = raw.ljust(length, b" ")
            return raw
        elif ftype == 0x42:  # Double (B)
            return struct.pack("<d", float(value) if value is not None else 0.0)
        elif ftype == 0x49:  # Integer (I)
            return struct.pack("<i", int(value) if value is not None else 0)
        elif ftype == 0x37:  # VFP datetime type 7
            return b"\x00" * 8
        else:
            return bytes(length)

    def update_field(self, record_index: int, field_name: str, value) -> None:
        """Overwrite a single field in an existing record (0‑based index)."""
        disp, length = self.field_offset(field_name)
        ftype = next(f["type"] for f in self.fields if f["name"] == field_name)
        raw = self._encode(ftype, value, length)
        rec_off = self.header_length + record_index * self.record_length
        self._open_for_write()
        self._fh.seek(rec_off + disp)
        self._fh.write(raw)
        self._fh.flush()

    def append_record(self, values: dict) -> None:
        """Append a new record.  *values* maps field_name → Python value."""
        record = bytearray(b" " * self.record_length)
        record[0] = 0x20  # active record marker
        for fld in self.fields:
            name = fld["name"]
            if name in values:
                raw = self._encode(fld["type"], values[name], fld["length"])
                record[fld["displacement"] : fld["displacement"] + fld["length"]] = raw

        self.close()
        with open(self.path, "r+b") as fh:
            fh.seek(0, os.SEEK_END)
            # overwrite EOF marker if present
            pos = fh.tell()
            if fh.read(1) == b"\x1a":
                fh.seek(pos)
            fh.write(bytes(record))
            fh.write(b"\x1a")
            fh.seek(4)
            self.record_count += 1
            fh.write(struct.pack("<I", self.record_count))
            fh.flush()

    def _open_for_write(self) -> None:
        if self._fh is None:
            self._fh = open(self.path, "r+b")

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# ACDSee database abstraction
# ---------------------------------------------------------------------------

class AcdDatabase:
    """In‑memory representation of the relevant ACDSee VFP tables."""

    def __init__(self, db_dir: str):
        self.db_dir = db_dir
        self.folder_roots: dict[int, dict] = {}
        self.folders: dict[int, dict] = {}
        self.assets: list[dict] = []
        self.asset_exif: dict[int, dict] = {}
        self.exif_image: dict[int, dict] = {}
        self._col_map: dict[str, str] = {}   # COL00xxx → display name

    def _load_column_mapping(self) -> None:
        """Load FieldSetField + FieldSetTable to map COL00xxx → display names."""
        import dbfread

        log = logging.getLogger("remap")
        log.info("Loading column mappings...")

        table_names: dict[int, str] = {}
        path = os.path.join(self.db_dir, "FieldSetTable.dbf")
        if not os.path.isfile(path):
            return
        tbl_encoding = _encoding_for_codepage(VfpFile(path).codepage)
        tbl = dbfread.DBF(path, load=False, ignore_missing_memofile=True,
                          parserclass=_VfpFieldParser, encoding=tbl_encoding)
        for rec in tbl:
            table_names[int(rec["FS_TABL_ID"])] = (rec["NAME"] or "").strip()

        path = os.path.join(self.db_dir, "FieldSetField.dbf")
        if not os.path.isfile(path):
            return
        tbl = dbfread.DBF(path, load=False, ignore_missing_memofile=True,
                          parserclass=_VfpFieldParser, encoding=tbl_encoding)
        for rec in tbl:
            col_name = (rec["COL_NAME"] or "").strip()
            disp_name = (rec["DISP_NAME"] or "").strip()
            if col_name and disp_name:
                self._col_map[col_name] = disp_name

        log.info("Column mapping: %d entries", len(self._col_map))

    def _rename_record(self, record: dict) -> dict:
        """Return a new dict with COL00xxx keys renamed to display names."""
        if not self._col_map:
            return record
        result: dict = {}
        for key, value in record.items():
            # Only rename COL00xxx keys; leave already-named fields alone
            if key.startswith("COL"):
                new_key = self._col_map.get(key, key)
            else:
                new_key = key
            # Decode raw bytes from memo fields
            if isinstance(value, bytes):
                try:
                    value = value.decode("cp1252", errors="replace").strip()
                except Exception:
                    value = None
            # If a "_ID" field has float type, cast to int
            if new_key.endswith("_ID") and isinstance(value, float):
                if value == int(value):
                    value = int(value)
            # Strip string values
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    value = None
            result[new_key] = value
        return result

    def load(self) -> None:
        log = logging.getLogger("remap")
        self._load_column_mapping()

        for name, target in [
            ("FolderRoot", self.folder_roots),
            ("Folder",     self.folders),
        ]:
            log.info("Loading %s...", name)
            vfp = VfpFile(os.path.join(self.db_dir, f"{name}.dbf"))
            id_field = {"FolderRoot": "FOLD_RT_ID", "Folder": "FOLDER_ID"}[name]
            for rec in vfp.read_all():
                rec = self._rename_record(rec)
                target[int(rec[id_field])] = rec

        log.info("Loading Asset...")
        vfp = VfpFile(os.path.join(self.db_dir, "Asset.dbf"))
        self.assets = [self._rename_record(r) for r in vfp.read_all()]

        log.info("Loading AssetExif...")
        try:
            vfp = VfpFile(os.path.join(self.db_dir, "AssetExif.dbf"))
            for rec in vfp.read_all():
                rec = self._rename_record(rec)
                self.asset_exif[int(rec["ASSET_ID"])] = rec
        except FileNotFoundError:
            pass

        log.info("Loading ExifImage...")
        try:
            vfp = VfpFile(os.path.join(self.db_dir, "ExifImage.dbf"))
            for rec in vfp.read_all():
                rec = self._rename_record(rec)
                self.exif_image[int(rec["ASSET_ID"])] = rec
        except FileNotFoundError:
            pass

        log.info("Loaded %d roots, %d folders, %d assets, %d exif, %d exif_img",
                 len(self.folder_roots), len(self.folders), len(self.assets),
                 len(self.asset_exif), len(self.exif_image))

    # -- path construction ----------------------------------------------------

    def build_folder_path(self, folder_id: int) -> str:
        """Walk the PRNT_ID chain upward, returning a relative folder path."""
        parts: list[str] = []
        visited: set[int] = set()
        fid = folder_id
        while fid and fid not in visited and fid in self.folders:
            visited.add(fid)
            folder = self.folders[fid]
            name = folder.get("NAME", "")
            if name and name != "\\":
                parts.append(name)
            prnt = folder.get("PRNT_ID")
            if prnt and int(prnt) != 0:
                fid = int(prnt)
            else:
                break
        return "/".join(reversed(parts))

    def get_root_name(self, folder_id: int) -> str:
        folder = self.folders.get(folder_id)
        if folder:
            rt_id = int(folder.get("FOLD_RT_ID", 0))
            root = self.folder_roots.get(rt_id)
            if root:
                name = root.get("NAME", "") or ""
                if name and not name.endswith(("/", "\\")):
                    name += "/"
                return name
        return ""

    def build_asset_path(self, asset: dict) -> str:
        folder_id = int(asset.get("FOLDER_ID", 0))
        root = self.get_root_name(folder_id)
        folder_path = self.build_folder_path(folder_id)
        name = asset.get("NAME", "") or ""
        return f"{root}{folder_path}/{name}" if folder_path else f"{root}{name}"

    # -- metadata -------------------------------------------------------------

    def get_asset_metadata(self, asset: dict) -> dict:
        aid = int(asset.get("ASSET_ID", 0))
        meta: dict[str, Any] = {
            "asset_id": aid,
            "name": asset.get("NAME", ""),
            "size": int(float(asset.get("SIZE", 0) or 0)),
            "width": int(float(asset.get("WIDTH", 0) or 0)),
            "height": int(float(asset.get("HEIGHT", 0) or 0)),
            "exifdate": asset.get("EXIFDATE"),
            "camera_make": "",
            "camera_model": "",
            "date_time_original": "",
            "focal_length": "",
            "fnumber": "",
            "lens_model": "",
        }
        ae = self.asset_exif.get(aid)
        if ae:
            # Try multiple possible field names (different DB versions use different names)
            for target, candidates in [
                ("camera_make", ["Make", "CAMERA_MAKE", "Camera Make"]),
                ("camera_model", ["Model", "CAMERA_MODEL", "Camera Model"]),
            ]:
                for c in candidates:
                    val = ae.get(c)
                    if val is not None:
                        meta[target] = str(val).strip()
                        break

        ei = self.exif_image.get(aid)
        if ei:
            for target, candidates in [
                ("date_time_original", ["Date Time Original", "DATE_TIME_ORIGINAL", "DateTimeOriginal", "Date Time"]),
                ("lens_model", ["Lens Model", "LENS_MODEL", "LensModel"]),
                ("focal_length", ["Focal Length", "FOCAL_LENGTH", "FocalLength"]),
                ("fnumber", ["F Number", "F_NUMBER", "FNumber", "Aperture"]),
            ]:
                for c in candidates:
                    val = ei.get(c)
                    if val is not None and val != "":
                        meta[target] = str(val).strip()
                        break
        return meta

    # -- helpers for database write-back --------------------------------------

    def folder_chain_ids(self, start_folder_id: int) -> list[int]:
        """Return list of FOLDER_IDs from root to leaf (inclusive)."""
        chain: list[int] = []
        visited: set[int] = set()
        fid = start_folder_id
        while fid and fid not in visited and fid in self.folders:
            visited.add(fid)
            chain.append(fid)
            prnt = self.folders[fid].get("PRNT_ID")
            if prnt and int(prnt) != 0:
                fid = int(prnt)
            else:
                break
        chain.reverse()
        return chain

    def asset_indices_by_folder(self, folder_id: int) -> list[int]:
        """Return list indices into self.assets for all assets in a folder."""
        return [i for i, a in enumerate(self.assets)
                if int(a.get("FOLDER_ID", 0)) == folder_id]


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

# Directories excluded from scanning (Synology NAS common dirs)
_EXCLUDE_DIRS = {"@eaDir", "#recycle", ".Trashes", ".fseventsd", ".Spotlight-V100",
                 "System Volume Information", "$RECYCLE.BIN", ".DS_Store"}


def _should_skip_dir(dirname: str) -> bool:
    return dirname in _EXCLUDE_DIRS or dirname.startswith("._")


def _safe_walk(root: str):
    """os.walk with network-error resilience; skips unreadable dirs."""
    log = logging.getLogger("remap")
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            yield dirpath, dirnames, filenames
    except OSError as exc:
        log.warning("Filesystem error during walk of %s: %s", root, exc)


# ---------------------------------------------------------------------------
# PathMapper
# ---------------------------------------------------------------------------

class PathMapper:
    """Caches old‑prefix → new‑prefix mappings discovered during resolution."""

    def __init__(self):
        self.mappings: dict[str, str] = {}
        self._sorted: list[str] = []

    def add(self, old: str, new: str) -> None:
        old = old.replace("\\", "/").rstrip("/")
        new = new.replace("\\", "/").rstrip("/")
        if old not in self.mappings:
            self.mappings[old] = new
            self._sorted = sorted(self.mappings, key=len, reverse=True)

    def resolve(self, old_path: str) -> Optional[str]:
        """Return remapped path or None."""
        np = old_path.replace("\\", "/")
        for prefix in self._sorted:
            if np == prefix or np.startswith(prefix + "/"):
                rest = np[len(prefix):].lstrip("/")
                return f"{self.mappings[prefix]}/{rest}" if rest else self.mappings[prefix]
        return None

    def all_mappings(self) -> dict:
        return dict(self.mappings)


# ---------------------------------------------------------------------------
# EXIF reading
# ---------------------------------------------------------------------------

def read_exif(filepath: str) -> dict:
    """Return a dict of EXIF tags from *filepath* or an empty dict on failure."""
    try:
        from PIL import Image

        def _open():
            return Image.open(filepath)
        img = retry_on_network_error(_open, max_retries=2)
        exif = img._getexif()
        img.close()
        if not exif:
            return {}
        from PIL.ExifTags import Base as ExifBase
        tags = {v: k for k, v in ExifBase.items()}
        result: dict[str, str] = {}
        for tid, val in exif.items():
            name = tags.get(tid, f"tag_{tid}")
            if isinstance(val, bytes):
                try:
                    val = val.decode("utf-8", errors="replace").rstrip("\x00")
                except Exception:
                    val = val.hex()
            result[name] = str(val)
        return result
    except Exception:
        return {}


def _img_file_size(filepath: str) -> int:
    try:
        return safe_getsize(filepath)
    except OSError:
        return 0


def _build_signature(meta: dict) -> dict:
    """Return a dict of comparable EXIF values for Levels 2 / 3."""
    sig: dict[str, str] = {}
    for db_key, sig_key in [
        ("date_time_original", "DateTimeOriginal"),
        ("camera_make", "Make"),
        ("camera_model", "Model"),
        ("lens_model", "LensModel"),
    ]:
        v = (meta.get(db_key) or "")
        if v:
            sig[sig_key] = normalize_for_compare(str(v))
    for db_key, sig_key in [
        ("focal_length", "FocalLength"),
        ("fnumber", "FNumber"),
    ]:
        v = meta.get(db_key)
        if v is not None and v != "":
            sig[sig_key] = str(v).strip()
    return sig


def _signature_match_score(db_sig: dict, img_sig: dict) -> int:
    score = 0
    for key in db_sig:
        dv = db_sig.get(key) or ""
        iv = img_sig.get(key) or ""
        if dv and iv and normalize_for_compare(str(dv)) == normalize_for_compare(str(iv)):
            score += 1
    return score


# ---------------------------------------------------------------------------
# FileFinder
# ---------------------------------------------------------------------------

class FileFinder:
    """Locates files under an image root.

    When a manifest is attached, every existence check is answered from it
    instead of the filesystem.  Over SMB that is the difference between a
    network round-trip and a set lookup, and the manifest already enumerates
    the whole storage, so nothing is lost.
    """

    def __init__(self, image_root: str, manifest: Optional["ManifestIndex"] = None):
        self.root = os.path.abspath(image_root)
        self.manifest = manifest
        self._stem_index: dict[str, list[str]] = {}  # stem -> [full_path, ...]
        self._indexed = False

    def exists(self, path: str) -> bool:
        if self.manifest:
            return self.manifest.has_local(path)
        return safe_isfile(path)

    def build_index(self, workers: int = 4) -> None:
        if self._indexed:
            return
        log = logging.getLogger("remap")

        all_files: list[str] = []
        if self.manifest:
            log.info("Indexing files from manifest ...")
            all_files = list(self.manifest.meta)
        else:
            log.info("Indexing files under %s ...", self.root)
            for dirpath, dirnames, filenames in _safe_walk(self.root):
                dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
                for fn in filenames:
                    all_files.append(os.path.join(dirpath, fn))

        self._stem_index = {}
        batch_size = 5000
        for i in range(0, len(all_files), batch_size):
            batch = all_files[i:i + batch_size]
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(batch))) as ex:
                for stem, fp in ex.map(_index_one, batch):
                    self._stem_index.setdefault(stem, []).append(fp)

        self._indexed = True
        log.info("Indexed %d files (%d unique stems)", len(all_files), len(self._stem_index))

    def check_relative(self, rel_path: str) -> Optional[str]:
        """Return full path if *rel_path* exists under self.root, else None."""
        full = os.path.normpath(os.path.join(self.root, rel_path.lstrip("/\\")))
        return full if self.exists(full) else None

    def find_by_filename(self, filename: str) -> list[str]:
        stem = normalize_for_compare(os.path.splitext(filename)[0])
        candidates = self._stem_index.get(stem, [])
        lower_fn = normalize_for_compare(filename)
        return [c for c in candidates if normalize_for_compare(os.path.basename(c)) == lower_fn]


def _index_one(filepath: str) -> tuple[str, str]:
    stem = normalize_for_compare(os.path.splitext(os.path.basename(filepath))[0])
    return stem, filepath


# ---------------------------------------------------------------------------
# ManifestIndex — offline view of the image storage
# ---------------------------------------------------------------------------

class ManifestIndex:
    """A filename+size index built from an exiftool JSON manifest.

    Walking a NAS over SMB costs roughly 440 file-names per second, and
    opening files for EXIF costs about 2.4 per second — a full pass over a
    165k-asset library takes the better part of a day.  Running exiftool on
    the storage device itself and shipping back its JSON reduces the same
    work to a few seconds of local lookups.

    Generate one with, on the NAS:

        exiftool -r -q -j -n -FileSize -DateTimeOriginal -CreateDate \\
            -Make -Model -LensModel -FocalLength -FNumber \\
            -ImageWidth -ImageHeight /volume1/photo > exif_manifest.json
    """

    def __init__(self, manifest_path: str, image_root: str,
                 manifest_root: Optional[str] = None):
        self.manifest_path = manifest_path
        self.image_root = os.path.abspath(image_root)
        self.manifest_root = manifest_root
        self.by_name_size: dict[tuple[str, int], list[str]] = {}
        self.meta: dict[str, dict] = {}
        self._local_paths: set[str] = set()
        self.log = logging.getLogger("remap")

    # -- loading ----------------------------------------------------------

    def _is_noise(self, path: str) -> bool:
        """Reject Synology sidecars, system dirs, and the manifest itself.

        exiftool scans its own output when the manifest is written inside the
        tree being scanned, producing a record with nonsense EXIF parsed out
        of the JSON bytes.  Only that exact filename is excluded, so genuine
        .json assets in the library survive.
        """
        parts = path.split("/")
        if any(_should_skip_dir(seg) for seg in parts[:-1]):
            return True
        return parts[-1] == os.path.basename(self.manifest_path)

    def _derive_root(self, sources: list[str]) -> str:
        """Longest common directory prefix of the manifest's own paths."""
        if self.manifest_root:
            return self.manifest_root.rstrip("/")
        common = os.path.dirname(sources[0])
        for src in sources:
            while not src.startswith(common + "/"):
                parent = os.path.dirname(common)
                if parent == common:
                    return common
                common = parent
        return common

    def load(self) -> None:
        self.log.info("Loading manifest %s ...", self.manifest_path)
        with open(self.manifest_path) as fh:
            records = json.load(fh)

        kept = [r for r in records
                if r.get("SourceFile") and not self._is_noise(r["SourceFile"])]
        dropped = len(records) - len(kept)
        if not kept:
            raise ValueError(f"No usable records in {self.manifest_path}")

        explicit = bool(self.manifest_root)
        root = self._derive_root([r["SourceFile"] for r in kept])
        self.manifest_root = root

        if not explicit:
            tops = {r["SourceFile"][len(root):].lstrip("/").split("/")[0]
                    for r in kept}
            if len(tops) < 2:
                self.log.warning(
                    "Manifest root inferred as %s, but everything sits under a "
                    "single entry (%s) -- if that entry is a real folder rather "
                    "than the share itself, pass --manifest-root explicitly.",
                    root, next(iter(tops), "?"))

        for rec in kept:
            src = rec["SourceFile"]
            rel = src[len(root):].lstrip("/")
            local = os.path.join(self.image_root, rel.replace("/", os.sep))
            self.meta[local] = rec
            self._local_paths.add(local)
            key = (normalize_for_compare(os.path.basename(src)),
                   int(rec.get("FileSize") or 0))
            self.by_name_size.setdefault(key, []).append(local)

        self.log.info("Manifest: %d files indexed (%d sidecar/system entries "
                      "dropped), root %s -> %s",
                      len(kept), dropped, root, self.image_root)

    # -- lookups ----------------------------------------------------------

    def has_local(self, local_path: str) -> bool:
        return os.path.normpath(local_path) in self._local_paths

    def candidates(self, filename: str, size: int) -> list[str]:
        return self.by_name_size.get((normalize_for_compare(filename), size), [])


def _suffix_split(db_path: str, local_path: str, image_root: str
                  ) -> Optional[tuple[str, str]]:
    """Split a matched pair at their longest common trailing components.

    ``E:/Photos/photos/0ur camera/2014/x.jpg`` matched against
    ``<root>/0ur camera/prior to current year/2014/x.jpg`` yields
    ``('E:/Photos/photos/0ur camera', '0ur camera/prior to current year')`` —
    the reorganisation that took place above the shared tail.
    """
    dbp = [s for s in normalize_path(db_path).split("/") if s]
    rel = os.path.relpath(local_path, image_root).replace(os.sep, "/")
    nas = [s for s in rel.split("/") if s and s != ".."]
    if not dbp or not nas:
        return None
    i = 0
    while (i < min(len(dbp), len(nas))
           and normalize_for_compare(dbp[-1 - i]) == normalize_for_compare(nas[-1 - i])):
        i += 1
    return "/".join(dbp[:len(dbp) - i]), "/".join(nas[:len(nas) - i])


# ---------------------------------------------------------------------------
# Remapper — core resolution & write-back
# ---------------------------------------------------------------------------

class Remapper:
    """Orchestrates orphan detection, resolution, and database updates."""

    def __init__(
        self, db: AcdDatabase, db_dir: str, image_root: str,
        level: int = 1, dry_run: bool = True, workers: int = 4,
        log_dir: str = ".", manifest: Optional["ManifestIndex"] = None,
    ):
        self.db = db
        self.db_dir = db_dir
        self.level = level
        self.dry_run = dry_run
        self.workers = workers
        self.manifest = manifest
        self.finder = FileFinder(image_root, manifest=manifest)
        self.mapper = PathMapper()
        self.log = logging.getLogger("remap")
        self.stats: dict[str, int] = dict(total=0, present=0, orphan=0,
                                          level1=0, level2=0, level3=0,
                                          unresolved=0, errors=0)
        self.resolutions: list[dict] = []

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = os.path.join(log_dir, f"remap_{ts}.log")
        self.map_path = os.path.join(log_dir, f"remap_mappings_{ts}.json")

    # ------------------------------------------------------------------
    # Run pipeline
    # ------------------------------------------------------------------

    def run(self) -> None:
        t0 = time.time()
        self.log.info("=" * 60)
        self.log.info("ACDSee Asset Remap")
        self.log.info("Database:    %s", self.db_dir)
        self.log.info("Image root:  %s", self.finder.root)
        self.log.info("Level:       %d", self.level)
        self.log.info("Dry run:     %s", self.dry_run)
        self.log.info("=" * 60)

        self.db.load()

        # Identify file assets (SIZE > 0 skips folder placeholder rows)
        file_assets = [a for a in self.db.assets
                       if int(float(a.get("SIZE", 0) or 0)) > 0]
        self.stats["total"] = len(file_assets)

        # Separate present / orphan
        orphans: list[tuple[dict, dict]] = []
        for a in file_assets:
            meta = self.db.get_asset_metadata(a)
            path = meta["original_path"] = self.db.build_asset_path(a)
            if self._asset_present(path):
                self.stats["present"] += 1
            else:
                orphans.append((a, meta))
        self.stats["orphan"] = len(orphans)

        self.log.info("%d files, %d present, %d orphans",
                      self.stats["total"], self.stats["present"], self.stats["orphan"])

        if not orphans:
            self._finish(t0)
            return

        # Execute resolution strategies sequentially.  The manifest goes
        # first: it is the only one that verifies a match (filename AND
        # size) without touching the network.
        if self.manifest:
            self._resolve_manifest(orphans)
        self._resolve_level1(orphans)
        if self.level >= 2:
            self._resolve_level2(orphans)
        if self.level >= 3:
            self._resolve_level3(orphans)

        unresolved = [(a, m) for a, m in orphans if m.get("resolution_level") is None]
        self.stats["unresolved"] = len(unresolved)
        self._log_summary(unresolved)
        self._log_contention()
        self._write_mappings()

        if not self.dry_run and self.stats["level1"] + self.stats["level2"] + self.stats["level3"] > 0:
            self._apply_updates()
            self._cleanup_indexes()

        self._finish(t0)

    def _finish(self, t0: float) -> None:
        elapsed = time.time() - t0
        self.log.info("Elapsed: %.1f s  |  L1=%d  L2=%d  L3=%d  err=%d  unresolved=%d",
                      elapsed, self.stats["level1"], self.stats["level2"],
                      self.stats["level3"], self.stats["errors"], self.stats["unresolved"])

    def _asset_present(self, path: str) -> bool:
        # With a manifest, answer from the index instead of issuing a stat
        # per asset across the network.
        if self.manifest:
            for _prefix, rest in volume_splits(path):
                test = os.path.normpath(os.path.join(self.finder.root, rest))
                if self.manifest.has_local(test):
                    return True
            return False

        if safe_isfile(path):
            return True
        # Try stripping the volume prefix and testing under image root
        for _prefix, rest in volume_splits(path):
            test = os.path.normpath(os.path.join(self.finder.root, rest))
            if safe_isfile(test):
                return True
        # Try via PathMapper cache
        mapped = self.mapper.resolve(path)
        if mapped:
            m = mapped.replace("/", os.sep)
            if safe_isfile(m):
                return True
            f = os.path.join(self.finder.root, m.lstrip(os.sep))
            if safe_isfile(f):
                return True
        return False

    # ------------------------------------------------------------------
    # Manifest resolution — offline filename+size matching
    # ------------------------------------------------------------------

    def _resolve_manifest(self, orphans: list) -> None:
        """Resolve orphans against a pre-harvested manifest of the storage.

        Runs in three passes.  The first accepts only assets whose
        filename+size is unique in the manifest, which is a verified match
        rather than a guess.  The second learns directory-rewrite rules from
        those confident matches alone — never from ambiguous ones, which
        would be circular.  The third uses those rules to break ties among
        duplicate copies.  Anything still tied is left unresolved for the
        later levels rather than guessed at.
        """
        mf = self.manifest
        self.log.info("--- Manifest: filename + size matching ---")

        ambiguous: list[tuple[dict, dict, list[str]]] = []
        for a, meta in orphans:
            if meta.get("resolution_level"):
                continue
            name, size = meta.get("name") or "", meta.get("size") or 0
            if not name or not size:
                continue
            cands = mf.candidates(name, size)
            if not cands:
                continue
            if len(cands) == 1:
                self._record_manifest(meta, cands[0], 1, "manifest_unique")
            else:
                ambiguous.append((a, meta, cands))

        rules = self._learn_rules()
        self.log.info("Learned %d directory-rewrite rules from %d confident "
                      "matches; %d assets ambiguous",
                      len(rules), self.stats["level1"], len(ambiguous))

        tied = 0
        for a, meta, cands in ambiguous:
            # An exact positional match beats any learned rule: if one copy
            # sits exactly where the database says, relative to its volume,
            # that is the asset's own file and not a duplicate of it.
            exact = self._exact_position(meta["original_path"], cands)
            if exact:
                self._record_manifest(meta, exact, 1, "manifest_position")
                continue

            best, best_score, ties = None, 0, 0
            for cand in cands:
                score = self._rule_score(rules, meta["original_path"], cand)
                if score > best_score:
                    best, best_score, ties = cand, score, 1
                elif score == best_score and score > 0:
                    ties += 1
            if best and ties == 1:
                self._record_manifest(meta, best, 2, "manifest_rule")
            else:
                tied += 1
        if tied:
            self.log.info("  %d ambiguous assets left unresolved (no rule "
                          "preferred a single copy)", tied)

    def _exact_position(self, db_path: str, cands: list[str]) -> Optional[str]:
        """The candidate sitting exactly where the database says, if unique.

        Matches on the whole path below the volume rather than on a fixed
        location, because the image root may be the migrated volume itself
        or a directory holding several migrated volumes side by side.  A
        suffix shared by two candidates is ambiguous and yields nothing, so
        the rule tie-breaker still gets its turn.
        """
        tails = [normalize_for_compare(rest.replace("/", os.sep))
                 for _prefix, rest in volume_splits(db_path)]
        if not tails:
            return None
        hits = [c for c in cands
                if any(normalize_for_compare(c).endswith(os.sep + t)
                       for t in tails)]
        return hits[0] if len(hits) == 1 else None

    def _record_manifest(self, meta: dict, local_path: str,
                         level: int, method: str) -> None:
        meta["new_path"] = local_path
        meta["resolution_level"] = level
        meta["resolution_type"] = method
        self.stats[f"level{level}"] += 1
        split = _suffix_split(meta["original_path"], local_path, self.finder.root)
        if split and split[0]:
            new_prefix = os.path.join(self.finder.root,
                                      split[1].replace("/", os.sep))
            self.mapper.add(split[0], new_prefix)
        self.resolutions.append(dict(meta))

    def _learn_rules(self) -> dict[str, list[tuple[str, int]]]:
        """Weighted directory-rewrite rules, keyed by database prefix."""
        counts: dict[str, dict[str, int]] = {}
        for res in self.resolutions:
            if res.get("resolution_type") != "manifest_unique":
                continue
            split = _suffix_split(res["original_path"], res["new_path"],
                                  self.finder.root)
            if not split:
                continue
            db_prefix, nas_prefix = split
            bucket = counts.setdefault(db_prefix, {})
            bucket[nas_prefix] = bucket.get(nas_prefix, 0) + 1
        return {k: sorted(v.items(), key=lambda kv: -kv[1])
                for k, v in counts.items()}

    def _rule_score(self, rules: dict, db_path: str, candidate: str) -> int:
        """How strongly the learned rules endorse *candidate* for *db_path*.

        Matches the longest database prefix with a known rule, then checks
        whether the candidate sits where that rule predicts.  Returns the
        rule's weight, or 0 if no rule endorses it.
        """
        parts = [s for s in normalize_path(db_path).split("/") if s][:-1]
        cand_dir = os.path.dirname(os.path.relpath(candidate, self.finder.root))
        cand_dir = normalize_for_compare(cand_dir.replace(os.sep, "/").strip("./"))
        for k in range(len(parts), 0, -1):
            prefix = "/".join(parts[:k])
            if prefix not in rules:
                continue
            tail = "/".join(parts[k:])
            for nas_prefix, weight in rules[prefix]:
                predicted = "/".join(p for p in (nas_prefix, tail) if p)
                if normalize_for_compare(predicted.strip("/")) == cand_dir:
                    return weight
            return 0
        return 0

    # ------------------------------------------------------------------
    # Level 1 — path prefix remapping
    # ------------------------------------------------------------------

    def _resolve_level1(self, orphans: list) -> None:
        self.log.info("--- Level 1: path prefix remapping ---")
        # Phase A: try cached prefix mappings
        rem = []
        for a, meta in orphans:
            if meta.get("resolution_level"):
                continue
            path = meta["original_path"]
            if path:
                mapped = self.mapper.resolve(path)
                if mapped:
                    mp = mapped.replace("/", os.sep)
                    if self.finder.exists(mp):
                        self._record_l1(meta, mp, "cached_prefix", "", "")
                        continue
                    c = self.finder.check_relative(mapped.lstrip("/\\"))
                    if c:
                        self._record_l1(meta, c, "cached_prefix", "", "")
                        continue
            rem.append((a, meta))

        # Phase B: direct match (strip drive letter, test under image_root)
        still = []
        for a, meta in rem:
            orig = meta["original_path"]
            if self._match_direct(meta, orig):
                continue
            still.append((a, meta))

        # Phase C: probe first-level subdirs of image_root
        if still:
            self._probe_root_subdirs(still)

    def _match_direct(self, meta: dict, path: str) -> bool:
        """Strip the volume prefix, see if the rest exists under image_root."""
        for prefix, rest in volume_splits(path):
            candidate = self.finder.check_relative(rest)
            if candidate:
                self._record_l1(meta, candidate, "direct_match",
                                prefix, self.finder.root)
                return True
        return False

    def _probe_root_subdirs(self, unresolved: list) -> None:
        """For each remaining orphan, probe first-level subdirs of image_root."""
        self.log.info("Probing %d orphans against image-root subdirs...",
                      len(unresolved))
        try:
            entries = os.listdir(self.finder.root)
        except OSError:
            return
        subdirs = [e for e in entries
                   if safe_isdir(os.path.join(self.finder.root, e))
                   and not _should_skip_dir(e)]
        if not subdirs:
            return

        for a, meta in unresolved:
            path = meta["original_path"]
            if not path:
                continue
            for prefix, rest in volume_splits(path):
                hit = None
                for sd in subdirs:
                    candidate = os.path.normpath(
                        os.path.join(self.finder.root, sd, rest))
                    if self.finder.exists(candidate):
                        hit = (candidate, os.path.join(self.finder.root, sd))
                        break
                if hit:
                    self._record_l1(meta, hit[0], "probe_root", prefix, hit[1])
                    break

    def _record_l1(self, meta: dict, new_path: str, method: str,
                   old_prefix: str, new_prefix: str) -> None:
        meta["new_path"] = new_path
        meta["resolution_level"] = 1
        meta["resolution_type"] = method
        self.stats["level1"] += 1
        self.mapper.add(old_prefix, new_prefix)
        self.resolutions.append(dict(meta))
        self.log.info("  [L1 %s] %s -> %s", method, meta["original_path"], new_path)

    # ------------------------------------------------------------------
    # Level 2 — filename + metadata match
    # ------------------------------------------------------------------

    def _resolve_level2(self, orphans: list) -> None:
        self.log.info("--- Level 2: filename + metadata matching ---")
        self.finder.build_index(workers=self.workers)

        for a, meta in orphans:
            if meta.get("resolution_level"):
                continue
            filename = meta.get("name", "")
            if not filename:
                continue
            for candidate in self.finder.find_by_filename(filename):
                if self._metadata_ok(meta, candidate):
                    meta["new_path"] = candidate
                    meta["resolution_level"] = 2
                    meta["resolution_type"] = "name_metadata"
                    self.stats["level2"] += 1
                    self.resolutions.append(dict(meta))
                    self.log.info("  [L2] %s -> %s", meta["original_path"], candidate)
                    break

    def _file_facts(self, filepath: str) -> tuple[int, dict]:
        """Return (size, exif) for a candidate, from the manifest if we have one.

        Opening a file for EXIF across SMB costs roughly 400 ms; the manifest
        already carries the same tags, so this keeps level 2 and 3 offline.
        """
        if self.manifest:
            rec = self.manifest.meta.get(os.path.normpath(filepath))
            if rec is None:
                return 0, {}
            exif = {k: v for k, v in rec.items()
                    if k != "SourceFile" and v not in (None, "")}
            return int(rec.get("FileSize") or 0), exif
        return _img_file_size(filepath), read_exif(filepath)

    def _metadata_ok(self, meta: dict, filepath: str) -> bool:
        """Verify file size + key EXIF fields match the database record."""
        size, exif = self._file_facts(filepath)
        # File size
        if meta.get("size"):
            if size != meta["size"]:
                return False
        if not exif:
            return meta.get("size") is not None  # accept on size alone if no EXIF

        # Dimensions
        if meta.get("width") and exif.get("ImageWidth"):
            if str(meta["width"]) != str(exif["ImageWidth"]):
                return False
        if meta.get("height") and exif.get("ImageHeight"):
            if str(meta["height"]) != str(exif["ImageHeight"]):
                return False

        # EXIF DateTimeOriginal
        db_dt = str(meta.get("date_time_original") or "").replace(":", "-")[:10]  # date only
        ex_dt = str(exif.get("DateTimeOriginal") or "").replace(":", "-")[:10]
        if db_dt and ex_dt and db_dt != ex_dt:
            return False

        # Camera make / model
        for key, exif_key in [("camera_make", "Make"), ("camera_model", "Model")]:
            db_v = normalize_for_compare(str(meta.get(key) or ""))
            ex_v = normalize_for_compare(str(exif.get(exif_key) or ""))
            if db_v and ex_v and db_v not in ex_v and ex_v not in db_v:
                return False
        return True

    # ------------------------------------------------------------------
    # Level 3 — metadata‑only EXIF signature match
    # ------------------------------------------------------------------

    def _resolve_level3(self, orphans: list) -> None:
        self.log.info("--- Level 3: EXIF signature matching ---")

        remaining = [(a, m) for a, m in orphans if not m.get("resolution_level")]
        if not remaining:
            return

        # Build an EXIF index for every image under root.  From a manifest
        # this is instant; otherwise every file must be opened, which across
        # a network share runs at a few files per second.
        image_exif: dict[str, dict] = {}
        if self.manifest:
            self.log.info("Reading EXIF from manifest...")
            for local, rec in self.manifest.meta.items():
                e = {k: v for k, v in rec.items()
                     if k != "SourceFile" and v not in (None, "")}
                if e:
                    image_exif[local] = e
            all_imgs = list(self.manifest.meta)
        else:
            self.log.info("Scanning images for EXIF (this may take a while)...")
            all_imgs = _collect_images(self.finder.root)
            batch = 500
            for i in range(0, len(all_imgs), batch):
                chunk = all_imgs[i:i + batch]
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.workers, 8)) as ex:
                    fut = {ex.submit(read_exif, p): p for p in chunk}
                    for f in concurrent.futures.as_completed(fut):
                        p = fut[f]
                        try:
                            e = f.result()
                            if e:
                                image_exif[p] = e
                        except Exception:
                            pass

        self.log.info("Scanned %d images, %d with EXIF data", len(all_imgs), len(image_exif))

        # Signatures are derived once per image, not once per comparison: the
        # loop below is |unresolved| x |images|, so rebuilding them inline
        # costs hundreds of millions of redundant dict constructions.
        img_sigs = [(p, _build_signature(e)) for p, e in image_exif.items()]
        img_sigs = [(p, s) for p, s in img_sigs if s]

        for a, meta in remaining:
            db_sig = _build_signature(meta)
            if not db_sig:
                continue
            best, best_score = None, 0
            for img_path, img_sig in img_sigs:
                score = _signature_match_score(db_sig, img_sig)
                if score > best_score:
                    best_score, best = score, img_path
            if best and best_score >= 2:
                meta["new_path"] = best
                meta["resolution_level"] = 3
                meta["resolution_type"] = "signature"
                meta["confidence"] = best_score
                self.stats["level3"] += 1
                self.resolutions.append(dict(meta))
                self.log.info("  [L3 c=%d] %s -> %s", best_score, meta["original_path"], best)

    # ------------------------------------------------------------------
    # Apply database updates
    # ------------------------------------------------------------------

    def _apply_updates(self) -> None:
        """Write resolved paths back into the VFP database files."""
        self.log.info("Writing database changes...")
        asset_vfp = VfpFile(os.path.join(self.db_dir, "Asset.dbf"))
        folder_vfp = VfpFile(os.path.join(self.db_dir, "Folder.dbf"))

        # Build a lookup: (old_folder_id, old_name) -> asset index in self.db.assets
        asset_lookup: dict[int, list[int]] = {}
        for idx, a in enumerate(self.db.assets):
            fid = int(a.get("FOLDER_ID", 0))
            asset_lookup.setdefault(fid, []).append(idx)

        for res in self.resolutions:
            new_path = res.get("new_path", "")
            aid = res.get("asset_id")
            if not new_path or aid is None:
                continue

            new_dir = os.path.dirname(new_path)
            new_name = os.path.basename(new_path)

            # Find or create the folder hierarchy (returns (folder_id, root_id))
            result = self._ensure_folder_chain(new_dir, folder_vfp)
            if result is None:
                self.log.error("  Failed to create folder chain for: %s", new_dir)
                self.stats["errors"] += 1
                continue
            new_fid, new_rtid = result

            # Find the asset record index
            asset_idx = None
            for idx, a in enumerate(self.db.assets):
                if int(a.get("ASSET_ID", 0)) == aid:
                    asset_idx = idx
                    break

            if asset_idx is not None:
                try:
                    asset_vfp.update_field(asset_idx, "FOLDER_ID", new_fid)
                    old_name = res.get("name", "")
                    if new_name != old_name:
                        asset_vfp.update_field(asset_idx, "NAME", new_name)
                except Exception as exc:
                    self.log.error("  Update Asset %d failed: %s", aid, exc)
                    self.stats["errors"] += 1
                else:
                    self.log.debug("  Asset %d -> FOLDER_ID=%d %s", aid, new_fid, new_name)

        asset_vfp.close()
        folder_vfp.close()

    def _ensure_folder_chain(self, dir_path: str, folder_vfp: VfpFile) -> Optional[tuple[int, int]]:
        """Make sure a Folder hierarchy exists for *dir_path*.

        Returns (leaf_folder_id, root_id) or None."""
        image_root = os.path.normpath(self.finder.root)
        dir_path = os.path.normpath(dir_path)

        if dir_path.startswith(image_root):
            rel = dir_path[len(image_root):].lstrip(os.sep)
        else:
            rel = dir_path

        parts = [p for p in rel.split(os.sep) if p] if rel else []
        if not parts:
            # The path IS the image root – use an existing root folder
            return self._find_existing_root_folder()

        # Find which FolderRoot this path belongs under
        root_id = self._best_root_for(dir_path)
        root_fid = self._root_folder_id(root_id)
        if root_fid is None:
            self.log.error("  No root folder found for root_id=%d", root_id)
            return None

        current_parent = root_fid
        for part in parts:
            child = self._find_child_folder(current_parent, part)
            if child is not None:
                current_parent = child
            else:
                if self.dry_run:
                    self.log.debug("  [dry-run] would create folder: %s", part)
                    return None
                # Create new folder record
                max_id = max(self.db.folders.keys(), default=0)
                new_fid = max_id + 1
                new_rec = {
                    "NAME": part,
                    "FOLD_RT_ID": root_id,
                    "PRNT_ID": current_parent,
                    "FOLDER_ID": new_fid,
                    "FOLDR_TYPE": 0,
                    "IS_EXCLUDE": 0,
                    "ATTRS": 0,
                    "SORT": 0,
                    "ISSORTREV": 0,
                    "GROUPSET": 0,
                    "ISGROUPREV": 0,
                    "GROUPRES": 0,
                }
                folder_vfp.append_record(new_rec)
                self.db.folders[new_fid] = new_rec
                self.log.info("  Created folder: %s (ID=%d, parent=%d)", part, new_fid, current_parent)
                current_parent = new_fid

        return current_parent, root_id

    def _best_root_for(self, dir_path: str) -> int:
        dir_path = os.path.normpath(dir_path)
        for rt_id, root in self.db.folder_roots.items():
            if root.get("NAME") and dir_path.endswith(root["NAME"].rstrip(":").rstrip("\\")):
                return int(rt_id)
        return 1  # fallback

    def _root_folder_id(self, root_id: int) -> Optional[int]:
        for fid, fld in self.db.folders.items():
            if int(fld.get("FOLD_RT_ID", 0)) == root_id and int(fld.get("PRNT_ID", 0)) == 0:
                return fid
        return None

    def _find_existing_root_folder(self) -> Optional[tuple[int, int]]:
        for fid, fld in self.db.folders.items():
            if int(fld.get("PRNT_ID", 0)) == 0:
                return fid, int(fld.get("FOLD_RT_ID", 0))
        return None

    def _find_child_folder(self, parent_id: int, name: str) -> Optional[int]:
        for fid, fld in self.db.folders.items():
            if int(fld.get("PRNT_ID", 0)) == parent_id and fld.get("NAME", "") == name:
                return fid
        return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup_indexes(self) -> None:
        for fn in os.listdir(self.db_dir):
            if fn.endswith(".cdx"):
                p = os.path.join(self.db_dir, fn)
                try:
                    os.remove(p)
                    self.log.info("Removed index: %s", fn)
                except OSError as exc:
                    self.log.warning("Could not remove %s: %s", fn, exc)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _log_summary(self, unresolved: list) -> None:
        self.log.info("--- Results ---")
        for k, label in [
            ("total", "Total files"), ("present", "Present"),
            ("orphan", "Orphans"), ("level1", "Resolved (L1)"),
            ("level2", "Resolved (L2)"), ("level3", "Resolved (L3)"),
            ("unresolved", "Unresolved"), ("errors", "Errors"),
        ]:
            self.log.info("  %-18s %d", label, self.stats[k])
        if unresolved:
            self.log.info("--- Unresolved (first 50) ---")
            for _, meta in unresolved[:50]:
                self.log.info("  %s", meta.get("original_path", "?"))
            if len(unresolved) > 50:
                self.log.info("  ... and %d more", len(unresolved) - 50)

    def _log_contention(self) -> None:
        """Report target files claimed by more than one asset.

        The database catalogued the same photo from several drives while the
        migrated storage keeps a single copy, so a many-to-one outcome is
        expected rather than wrong -- but it means those assets will end up
        as multiple records pointing at one file, which is worth seeing
        before any writes happen.
        """
        counts: dict[str, int] = {}
        for res in self.resolutions:
            p = res.get("new_path")
            if p:
                counts[p] = counts.get(p, 0) + 1
        shared = {p: n for p, n in counts.items() if n > 1}
        if not shared:
            return
        self.log.info("%d target files are claimed by more than one asset "
                      "(%d resolutions, max %d on one file) — duplicate "
                      "catalogue entries collapsing onto one surviving copy",
                      len(shared), sum(shared.values()), max(shared.values()))

    def _write_mappings(self) -> None:
        out = {
            "image_root": self.finder.root,
            "timestamp": datetime.datetime.now().isoformat(),
            "stats": self.stats,
            "prefix_mappings": self.mapper.all_mappings(),
            # Every resolution is written: this file is the audit trail for
            # the changes _apply_updates makes, so truncating it would leave
            # most of the database edits unrecorded.
            "resolutions": self.resolutions,
        }
        with open(self.map_path, "w") as f:
            json.dump(out, f, indent=2, default=str)
        self.log.info("Mappings → %s (%d resolutions)",
                      self.map_path, len(self.resolutions))


def _collect_images(root: str) -> list[str]:
    """Return all image/video paths under *root*, skipping excluded dirs."""
    img_ext = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif",
               ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2",
               ".psd", ".heic", ".heif", ".webp", ".avi", ".mp4", ".mov",
               ".mkv", ".wmv", ".mpg", ".mpeg", ".mts", ".m2ts", ".3gp"}
    files: list[str] = []
    for dirpath, dirnames, filenames in _safe_walk(root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in img_ext:
                files.append(os.path.join(dirpath, fn))
    return files


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_database(db_dir: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(os.path.dirname(db_dir) or ".", f"backup_{ts}")
    log = logging.getLogger("remap")
    log.info("Backing up %s → %s ...", db_dir, dst)
    shutil.copytree(db_dir, dst)
    count = sum(1 for _ in Path(dst).rglob("*"))
    log.info("Backup complete (%d files)", count)
    return dst


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def setup_logging(log_path: str, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logger = logging.getLogger("remap")
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    fh = logging.FileHandler(log_path)
    fh.setLevel(level); fh.setFormatter(fmt); logger.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level); ch.setFormatter(logging.Formatter("%(message)s")); logger.addHandler(ch)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Remap orphaned asset paths in an ACDSee Visual FoxPro database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Resolution levels:\n"
               "  1  Path prefix remapping (volume/drive or folder renamed)\n"
               "  2  Individual file match by name + metadata (EXIF)\n"
               "  3  Metadata-only EXIF signature match (renamed/modified images)\n"
               "\n"
               "With --manifest, an offline filename+size pass runs first and\n"
               "resolves most assets without touching the network.\n")
    p.add_argument("db_dir", nargs="?", help="ACDSee database directory (.dbf/.fpt/.cdx)")
    p.add_argument("image_root", nargs="?", help="Root of current image storage")
    p.add_argument("--level", type=int, choices=[1, 2, 3], default=1,
                   help="Max resolution level (1)")
    p.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    p.add_argument("--no-backup", action="store_true", help="Skip backup")
    p.add_argument("--log-file", help="Custom log path")
    p.add_argument("--workers", type=int, default=4, help="Parallel workers (4)")
    p.add_argument("--manifest",
                   help="exiftool JSON manifest of the image storage; enables "
                        "offline filename+size resolution")
    p.add_argument("--manifest-root",
                   help="Path prefix inside the manifest that corresponds to "
                        "image_root (default: inferred)")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--gui", action="store_true", help="Launch tkinter GUI")
    args = p.parse_args()

    if args.gui:
        if not HAS_TKINTER:
            print("tkinter not available; install python3-tk."); sys.exit(1)
        run_gui(); return

    if not args.db_dir or not args.image_root:
        p.error("db_dir and image_root are required")
    if not os.path.isdir(args.db_dir):
        print(f"Error: not a directory: {args.db_dir}"); sys.exit(1)
    if not os.path.isdir(args.image_root):
        print(f"Error: not a directory: {args.image_root}"); sys.exit(1)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = args.log_file or os.path.join(os.getcwd(), f"remap_{ts}.log")
    setup_logging(log_path, args.verbose)
    log = logging.getLogger("remap")

    manifest = None
    if args.manifest:
        if not os.path.isfile(args.manifest):
            print(f"Error: no such manifest: {args.manifest}"); sys.exit(1)
        manifest = ManifestIndex(args.manifest, args.image_root,
                                 args.manifest_root)
        manifest.load()

    if not args.dry_run and not args.no_backup:
        backup_database(args.db_dir)

    db = AcdDatabase(args.db_dir)
    r = Remapper(db, args.db_dir, args.image_root,
                 level=args.level, dry_run=args.dry_run,
                 workers=args.workers, log_dir=os.getcwd(),
                 manifest=manifest)
    r.run()

    if args.dry_run:
        log.info("DRY RUN — no changes were made.")
    else:
        log.info("Done.  ACDSee will rebuild indexes on next start.")


# ---------------------------------------------------------------------------
# GUI (tkinter)
# ---------------------------------------------------------------------------

class RemapGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ACDSee Asset Remap")
        self.root.geometry("800x650")
        self._build()

    def _build(self) -> None:
        mf = ttk.Frame(self.root, padding=10); mf.pack(fill=tk.BOTH, expand=True)
        r = 0
        ttk.Label(mf, text="Database:").grid(row=r, column=0, sticky=tk.W, pady=2)
        self.db_var = tk.StringVar()
        ttk.Entry(mf, textvariable=self.db_var, width=70).grid(row=r, column=1, sticky=tk.W + tk.E, padx=(5, 0))
        ttk.Button(mf, text="Browse", command=lambda: self._browse(self.db_var)).grid(row=r, column=2, padx=5)

        r += 1
        ttk.Label(mf, text="Image Root:").grid(row=r, column=0, sticky=tk.W, pady=2)
        self.root_var = tk.StringVar()
        ttk.Entry(mf, textvariable=self.root_var, width=70).grid(row=r, column=1, sticky=tk.W + tk.E, padx=(5, 0))
        ttk.Button(mf, text="Browse", command=lambda: self._browse(self.root_var)).grid(row=r, column=2, padx=5)

        r += 1
        of = ttk.LabelFrame(mf, text="Options", padding=5)
        of.grid(row=r, column=0, columnspan=3, sticky=tk.W + tk.E, pady=10)
        ttk.Label(of, text="Level:").grid(row=0, column=0); self.lvl = tk.IntVar(value=1)
        ttk.Combobox(of, textvariable=self.lvl, values=[1, 2, 3], width=3, state="readonly").grid(row=0, column=1, padx=5)
        self.dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="Dry Run", variable=self.dry).grid(row=0, column=2, padx=5)
        self.bkup = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="Backup", variable=self.bkup).grid(row=0, column=3, padx=5)
        self.wv = tk.IntVar(value=4)
        ttk.Label(of, text="Workers:").grid(row=0, column=4, padx=(20, 0))
        ttk.Spinbox(of, textvariable=self.wv, from_=1, to=16, width=3).grid(row=0, column=5, padx=5)

        r += 1
        self.btn = ttk.Button(mf, text="Start", command=self._start)
        self.btn.grid(row=r, column=0, columnspan=3, pady=8)
        self.pb = ttk.Progressbar(mf, mode="indeterminate")
        self.pb.grid(row=r + 1, column=0, columnspan=3, sticky=tk.W + tk.E, pady=(0, 5))
        mf.columnconfigure(1, weight=1); mf.rowconfigure(r + 2, weight=1)

        lf = ttk.LabelFrame(mf, text="Log", padding=5)
        lf.grid(row=r + 2, column=0, columnspan=3, sticky=tk.NSEW, pady=5)
        self.log = tk.Text(lf, height=20, width=90, wrap=tk.NONE)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sy = ttk.Scrollbar(lf, command=self.log.yview); sy.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.configure(yscrollcommand=sy.set)

    def _browse(self, var: "tk.StringVar") -> None:
        d = filedialog.askdirectory()
        if d: var.set(d)

    def _log(self, msg: str) -> None:
        self.log.insert(tk.END, msg + "\n"); self.log.see(tk.END); self.root.update_idletasks()

    def _start(self) -> None:
        db = self.db_var.get().strip(); root = self.root_var.get().strip()
        if not os.path.isdir(db): messagebox.showerror("Error", "Invalid DB dir"); return
        if not os.path.isdir(root): messagebox.showerror("Error", "Invalid image root"); return

        self.btn.configure(state="disabled"); self.pb.start()
        try:
            dry = self.dry.get()
            if not dry and self.bkup.get():
                bd = backup_database(db); self._log(f"Backup: {bd}")
            self._log(f"Starting (level={self.lvl.get()}, dry_run={dry})...")

            import io
            buf = io.StringIO()
            sh = logging.StreamHandler(buf)
            sh.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger("remap"); logger.handlers.clear(); logger.addHandler(sh)
            logger.setLevel(logging.INFO)

            dbo = AcdDatabase(db)
            r = Remapper(dbo, db, root, level=self.lvl.get(), dry_run=dry,
                         workers=self.wv.get(), log_dir=os.getcwd())
            r.run()
            for line in buf.getvalue().split("\n"):
                if line.strip(): self._log(line.strip())
        except Exception as exc:
            self._log(f"ERROR: {exc}"); messagebox.showerror("Error", str(exc))
        finally:
            self.pb.stop(); self.btn.configure(state="normal")

    def run(self) -> None:
        self.root.mainloop()


def run_gui() -> None:
    RemapGUI().run()


if __name__ == "__main__":
    main()
