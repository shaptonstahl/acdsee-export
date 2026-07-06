#!/usr/bin/env python3
"""Export ACDSee Visual FoxPro database to SQLite."""

import os
import re
import sqlite3
import struct
import sys
from datetime import datetime, timedelta

import dbfread
from dbfread import FieldParser

DB_DIR = "acdsee_source/Default"

SKIP_TABLES = {"Thumb1", "Thumb2", "Thumb3", "FaceThumbnail", "FaceDescriptor"}

# VFP type 7 is a raw 8-byte double encoding a datetime.
# It's NOT Julian day + milliseconds packed as two uint32s — it's actually
# the same format as a dBASE timestamp: the 8 bytes split into two 32-bit
# integers: the first is a Julian Day Number, the second is milliseconds
# since midnight.
JULIAN_EPOCH = datetime(
    year=-4713 + 4714, month=11, day=24
)  # Julian day 0 = Nov 24, 4714 BC → use offset


def julian_to_datetime(julian_day, milliseconds):
    """Convert Julian Day Number + milliseconds to ISO 8601 string."""
    if julian_day == 0:
        return None
    # Julian Day Number to calendar date using standard algorithm
    # Reference: Meeus, Astronomical Algorithms
    l = julian_day + 68569
    n = (4 * l) // 146097
    l = l - (146097 * n + 3) // 4
    i = (4000 * (l + 1)) // 1461001
    l = l - (1461 * i) // 4 + 31
    j = (80 * l) // 2447
    day = l - (2447 * j) // 80
    l = j // 11
    month = j + 2 - 12 * l
    year = 100 * (n - 49) + i + l

    hours = milliseconds // 3_600_000
    remainder = milliseconds % 3_600_000
    minutes = remainder // 60_000
    remainder = remainder % 60_000
    seconds = remainder // 1000
    ms = remainder % 1000

    try:
        dt = datetime(year, month, day, hours, minutes, seconds, ms * 1000)
        return dt.isoformat()
    except (ValueError, OverflowError):
        return f"{year:04d}-{month:02d}-{day:02d}T{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def parse_vfp_timestamp(raw_double_bytes):
    """Parse a VFP type-7 field from its raw 8 bytes."""
    if raw_double_bytes == b"\x00" * 8:
        return None
    julian_day, milliseconds = struct.unpack("<II", raw_double_bytes)
    if julian_day == 0:
        return None
    return julian_to_datetime(julian_day, milliseconds)


class VFPFieldParser(FieldParser):
    """Extended field parser supporting Visual FoxPro field types."""

    def parse7(self, field, data):
        """Type 7: DateTime stored as Julian day + milliseconds."""
        return parse_vfp_timestamp(data)

    def parseB(self, field, data):
        """Type B: Double."""
        return struct.unpack("<d", data)[0]

    def parseI(self, field, data):
        """Type I: 32-bit integer."""
        return struct.unpack("<i", data)[0]


def load_column_mapping(db_dir):
    """Load FieldSetField + FieldSetTable to map COL00xxx → display names."""
    # Load table ID → table name
    table_names = {}
    tbl = dbfread.DBF(
        os.path.join(db_dir, "FieldSetTable.dbf"),
        load=False,
        ignore_missing_memofile=True,
        parserclass=VFPFieldParser,
    )
    for rec in tbl:
        tid = int(rec["FS_TABL_ID"])
        table_names[tid] = rec["NAME"].strip()

    # Load column mappings
    col_map = {}  # col_name → display_name
    col_details = []  # for _column_mapping table
    tbl = dbfread.DBF(
        os.path.join(db_dir, "FieldSetField.dbf"),
        load=False,
        ignore_missing_memofile=True,
        parserclass=VFPFieldParser,
    )
    for rec in tbl:
        col_name = rec["COL_NAME"].strip()
        disp_name = rec["DISP_NAME"].strip()
        table_id = int(rec["FS_TABL_ID"])
        table_name = table_names.get(table_id, f"table_{table_id}")
        col_map[col_name] = disp_name
        col_details.append(
            {
                "original_name": col_name,
                "display_name": disp_name,
                "table_id": table_id,
                "table_name": table_name,
                "data_type": rec["DATA_TYPE"],
            }
        )

    return col_map, col_details


def sanitize_column_name(name):
    """Make a display name safe for use as a SQLite column name."""
    # Replace problematic characters with underscores
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    # Collapse multiple underscores
    name = re.sub(r"_+", "_", name)
    # Strip leading/trailing underscores
    name = name.strip("_")
    # Prefix with underscore if starts with digit
    if name and name[0].isdigit():
        name = "_" + name
    return name


def convert_record(record, field_types, col_rename_map):
    """Convert a DBF record dict, applying type conversions and column renames."""
    converted = {}
    for key, value in record.items():
        # Rename COL00xxx columns
        new_key = col_rename_map.get(key, key)

        # Cast double-encoded IDs to integers
        if key.endswith("_ID") and isinstance(value, float):
            value = int(value) if value == value else None  # NaN check

        # Cast other double fields that look like enums/codes to int if they're whole numbers
        if isinstance(value, float) and key not in col_rename_map:
            if value == int(value) and field_types.get(key) == "B":
                value = int(value)

        # Strip whitespace from string fields
        if isinstance(value, str):
            value = value.strip()
            if not value:
                value = None

        # Handle bytes (memo fields that couldn't decode)
        if isinstance(value, bytes):
            try:
                value = value.decode("cp1252", errors="replace").strip()
                if not value:
                    value = None
            except Exception:
                value = value.hex()

        converted[new_key] = value

    return converted


def get_sqlite_type(dbf_type, field_name=""):
    """Map DBF field type to SQLite type, with ID column override."""
    # ID columns are stored as doubles in VFP but should be integers
    if field_name.endswith("_ID"):
        return "INTEGER"
    return {
        "C": "TEXT",
        "M": "TEXT",
        "N": "REAL",
        "F": "REAL",
        "B": "REAL",
        "I": "INTEGER",
        "L": "INTEGER",
        "D": "TEXT",
        "T": "TEXT",
        "7": "TEXT",  # timestamps → ISO 8601 text
    }.get(dbf_type, "TEXT")


def export_table(db_dir, table_name, sqlite_conn, col_rename_map):
    """Export a single DBF table to SQLite. Returns record count."""
    dbf_path = os.path.join(db_dir, f"{table_name}.dbf")
    if not os.path.exists(dbf_path):
        return 0

    try:
        table = dbfread.DBF(
            dbf_path,
            load=False,
            ignore_missing_memofile=True,
            parserclass=VFPFieldParser,
            encoding="cp1252",
            char_decode_errors="replace",
        )
    except Exception as e:
        print(f"  ERROR opening {table_name}: {e}")
        return 0

    # Build field info and column rename map for this table
    field_types = {}
    columns = []
    sqlite_types = []
    for f in table.fields:
        field_types[f.name] = f.type
        col_name = col_rename_map.get(f.name, f.name)
        # Deduplicate: if rename conflicts, append original
        if col_name in columns and col_name != f.name:
            col_name = f"{col_name}__{f.name}"
        columns.append(col_name)
        sqlite_types.append(get_sqlite_type(f.type, f.name))

    # Create SQLite table
    col_defs = ", ".join(
        f'"{c}" {t}' for c, t in zip(columns, sqlite_types)
    )
    sqlite_conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    sqlite_conn.execute(f'CREATE TABLE "{table_name}" ({col_defs})')

    # Insert records
    placeholders = ", ".join(["?"] * len(columns))
    insert_sql = f'INSERT INTO "{table_name}" VALUES ({placeholders})'

    count = 0
    batch = []
    errors = 0
    for rec in table:
        try:
            converted = convert_record(rec, field_types, col_rename_map)
            row = tuple(converted.get(c, None) for c in columns)
            batch.append(row)
            count += 1
            if len(batch) >= 5000:
                sqlite_conn.executemany(insert_sql, batch)
                batch = []
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  WARNING: record {count + errors}: {e}")
            continue

    if batch:
        sqlite_conn.executemany(insert_sql, batch)

    sqlite_conn.commit()

    if errors:
        print(f"  {table_name}: {count} records exported, {errors} errors")
    return count


def main():
    db_dir = DB_DIR
    if len(sys.argv) > 1:
        db_dir = sys.argv[1]

    export_time = datetime.now()
    timestamp_str = export_time.strftime("%Y%m%d_%H%M%S")
    output_file = f"acdsee_export_{timestamp_str}.sqlite"

    print(f"ACDSee Database Export")
    print(f"Source: {os.path.abspath(db_dir)}")
    print(f"Output: {output_file}")
    print(f"Time:   {export_time.isoformat()}")
    print()

    # Load column name mappings
    print("Loading column name mappings...")
    col_map, col_details = load_column_mapping(db_dir)
    # Build rename map: COL00xxx → sanitized display name
    col_rename_map = {}
    for orig, display in col_map.items():
        if orig.startswith("COL"):
            col_rename_map[orig] = sanitize_column_name(display)

    # Discover all DBF tables
    dbf_files = sorted(
        f[:-4]
        for f in os.listdir(db_dir)
        if f.endswith(".dbf")
    )

    # Open SQLite
    conn = sqlite3.connect(output_file)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Export each table
    record_counts = {}
    for table_name in dbf_files:
        if table_name in SKIP_TABLES:
            print(f"  SKIP {table_name} (thumbnail/embedding data)")
            continue

        print(f"  Exporting {table_name}...", end=" ", flush=True)
        count = export_table(db_dir, table_name, conn, col_rename_map)
        record_counts[table_name] = count
        print(f"{count:,} records")

    # Write _column_mapping metadata table
    print("\nWriting metadata tables...")
    conn.execute("DROP TABLE IF EXISTS _column_mapping")
    conn.execute("""
        CREATE TABLE _column_mapping (
            original_name TEXT,
            display_name TEXT,
            table_id INTEGER,
            table_name TEXT,
            data_type INTEGER
        )
    """)
    conn.executemany(
        "INSERT INTO _column_mapping VALUES (?, ?, ?, ?, ?)",
        [
            (d["original_name"], d["display_name"], d["table_id"], d["table_name"], d["data_type"])
            for d in col_details
        ],
    )

    # Write _export_info metadata table
    conn.execute("DROP TABLE IF EXISTS _export_info")
    conn.execute("""
        CREATE TABLE _export_info (
            table_name TEXT,
            record_count INTEGER,
            export_time TEXT,
            source_path TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO _export_info VALUES (?, ?, ?, ?)",
        [
            (name, count, export_time.isoformat(), os.path.abspath(db_dir))
            for name, count in record_counts.items()
        ],
    )

    # Create indexes on key columns
    print("Creating indexes...")
    index_columns = {
        "Asset": ["ASSET_ID", "FOLDER_ID", "FILE_TP_ID"],
        "ExifImage": ["ASSET_ID"],
        "AssetExif": ["ASSET_ID"],
        "AssetIPTC": ["ASSET_ID"],
        "ExifGPS": ["ASSET_ID"],
        "AssetMedia": ["ASSET_ID"],
        "Face": ["ASSET_ID", "FACE_ID", "DESC_ID"],
        "Folder": ["FOLDER_ID", "FOLD_RT_ID", "PRNT_ID"],
        "FolderRoot": ["FOLD_RT_ID"],
        "Category": ["CAT_ID", "PRNT_ID", "CAT_ROOTID"],
        "CategoryRoot": ["CAT_ROOTID"],
        "JoinCategoryAsset": ["CAT_ID", "ASSET_ID"],
        "JoinKeywordAsset": ["KW_ID", "ASSET_ID"],
        "JoinAssetFTSWordTable": ["ASSET_ID", "FTSWORD_ID"],
        "FTSWordTable": ["FTSWORD_ID"],
        "Collection": ["COL_ID"],
        "CollectionAsset": ["COL_ID", "ASSET_ID"],
        "CollectionQuery": ["COL_ID"],
        "LookupValueItem": ["ASSET_ID", "FLD_SET_ID", "LLITEM_ID"],
        "FileType": ["FILE_TP_ID"],
    }

    for table_name, cols in index_columns.items():
        if table_name not in record_counts:
            continue
        for col in cols:
            idx_name = f"idx_{table_name}_{col}"
            try:
                conn.execute(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table_name}" ("{col}")')
            except sqlite3.OperationalError:
                pass  # column might not exist in this version

    conn.commit()
    conn.close()

    # Summary
    total = sum(record_counts.values())
    file_size = os.path.getsize(output_file)
    print(f"\nDone! Exported {total:,} records across {len(record_counts)} tables")
    print(f"Output: {output_file} ({file_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
