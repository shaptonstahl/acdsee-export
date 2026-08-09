# ACDSee Database Export

One-time export of all photo/video metadata from an ACDSee 2023 database into a portable SQLite file.

## Background

ACDSee stores metadata (ratings, categories, captions, EXIF, IPTC, GPS, face detections, etc.) in a proprietary Visual FoxPro database on the local machine. This project extracts that metadata into SQLite so it can be queried, migrated, or imported into other software without needing ACDSee.

## What's in the database

ACDSee's database is a directory of `.dbf` / `.cdx` / `.fpt` files (Visual FoxPro format). The export script reads all metadata tables and writes them to a single `.sqlite` file, preserving all records losslessly. Thumbnail and face-embedding binary data is intentionally skipped.

### Tables exported (non-empty)

| Table | Records | Description |
|-------|--------:|-------------|
| Asset | 171,594 | Core file metadata: name, folder, dimensions, dates, rating, caption, notes, tags |
| AssetExif | 141,129 | Camera EXIF: make, model, orientation, maker notes |
| ExifImage | 139,051 | Detailed EXIF: exposure, aperture, focal length, ISO, lens info |
| AssetIPTC | 28,378 | IPTC: keywords, location, copyright, captions |
| ExifGPS | 14,843 | GPS coordinates and related fields |
| JoinCategoryAsset | 13,777 | Category-to-asset assignments |
| Face | 11,493 | Face bounding boxes per image |
| JoinAssetFTSWordTable | 11,194 | Full-text search word-to-asset index |
| AssetMedia | 10,476 | Video/audio properties (duration, frame rate, codec, MP3 tags) |
| Folder | 8,798 | Folder hierarchy |
| FTSWordTable | 2,975 | Full-text search word dictionary |
| LookupValueItem | 697 | Enum values assigned to assets |
| Category | 647 | Hierarchical categories (tree structure via `PRNT_ID`) |
| LookupListItem | 387 | Enum value definitions |
| FileType | 269 | File extension-to-type mappings |
| JoinAssetTypeFileType | 173 | Asset type-to-file type join |
| LookupList | 48 | Enum list definitions |
| FolderRoot | 35 | Drive/volume roots |
| CategoryRoot | 16 | Category root groups |
| Collection | 7 | Named collections |
| CollectionQuery | 6 | Smart collection query rules |

Plus several reference/config tables and two metadata tables (`_column_mapping`, `_export_info`).

### Tables skipped

| Table | Size | Reason |
|-------|-----:|--------|
| Thumb1, Thumb2, Thumb3 | ~6.9 GB | Cached thumbnail images |
| FaceThumbnail | 416 MB | Face crop images |
| FaceDescriptor | 12 MB | Face recognition embedding vectors |

## How it works

ACDSee uses Visual FoxPro field types that standard DBF readers don't handle. The export script (`export_to_sqlite.py`) includes a custom field parser for:

- **Type 7 (DateTime)**: 8 bytes split into Julian Day Number + milliseconds since midnight, converted to ISO 8601 strings
- **Type B (Double)**: used for IDs and numeric values; IDs are cast to integers
- **Type I (Integer)**: 32-bit signed integers

The script also:

- Renames opaque `COL00xxx` column names to human-readable names (e.g. `COL00064` becomes `Exposure_Time`) using ACDSee's internal `FieldSetField` mapping table
- Handles Windows cp1252 encoding with replacement for unmappable bytes
- Creates indexes on key join columns (`ASSET_ID`, `FOLDER_ID`, `CAT_ID`, etc.)
- Stores the original-to-display column name mapping in a `_column_mapping` table

## Usage

### Prerequisites

```
python3 -m venv .venv
source .venv/bin/activate
pip install dbfread
```

### Running the export

```
python export_to_sqlite.py
```

This reads from `acdsee_source/Default/` and writes `acdsee_export_<YYYYMMDD_HHMMSS>.sqlite` in the current directory. Pass an alternate source path as the first argument:

```
python export_to_sqlite.py /path/to/acdsee/Default
```

### Querying the export

```sql
-- Reconstruct full file paths
SELECT fr.NAME || '/' || f.NAME || '/' || a.NAME AS path,
       a.RATING, a.CAPTION, a.EXIFDATE
FROM Asset a
JOIN Folder f ON a.FOLDER_ID = f.FOLDER_ID
JOIN FolderRoot fr ON f.FOLD_RT_ID = fr.FOLD_RT_ID
WHERE a.WIDTH > 0
LIMIT 10;

-- List categories assigned to a file
SELECT c.NAME AS category
FROM JoinCategoryAsset jca
JOIN Category c ON jca.CAT_ID = c.CAT_ID
WHERE jca.ASSET_ID = 12345;

-- Find photos with GPS data
SELECT a.NAME, g.GPS_Latitude, g.GPS_Longitude
FROM ExifGPS g
JOIN Asset a ON g.ASSET_ID = a.ASSET_ID
WHERE g.GPS_Latitude IS NOT NULL;

-- Browse the category tree
SELECT c.CAT_ID, c.NAME, p.NAME AS parent
FROM Category c
LEFT JOIN Category p ON c.PRNT_ID = p.CAT_ID
ORDER BY c.CAT_ID;
```

## Source data

The `acdsee_source/Default/` directory contains a working copy of the ACDSee database from the source PC. It is not modified by the export script.

---

# Asset Path Remapping

Remap orphaned asset paths when images have moved to a new location.

When using ACDSee with external storage (NAS, USB drives, or migrated
volumes), the database records reference the original paths.  If a drive
letter changes, a volume is renamed, or images are moved to a new
filesystem, ACDSee loses the connection to those files.  `remap_assets.py`
finds the current locations of orphaned assets and updates the database
in place so that ACDSee sees them again.

## Resolution Levels

| Level | Strategy | Example |
|-------|----------|---------|
| **1** — Path prefix remap | Volume/drive letter changed or folder renamed.  The filename and relative path are unchanged. | `D:\Photos\2023\img.jpg` → `/nas/photos/Photos/2023/img.jpg` |
| **2** — File match by name + metadata | File moved elsewhere; confirmed by matching file size, dimensions, EXIF datetime, and camera make/model. | Same filename found in a different folder, verified via EXIF. |
| **3** — Metadata-only EXIF signature | File was renamed and/or modified (cropped, edited).  Matched on DateTimeOriginal + camera make/model. | Finds the same photo even if the filename and size differ. |

## Prerequisites

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```
python remap_assets.py <db_dir> <image_root> [options]
```

**Always run with `--dry-run` first** to preview changes before writing:

```
python remap_assets.py acdsee_source/Default /mnt/nas/photos --dry-run --level 1
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--level {1,2,3}` | `1` | Maximum resolution level |
| `--dry-run` | off | Report only; do not modify the database |
| `--no-backup` | off | Skip creating a database backup before writing |
| `--workers N` | `4` | Number of parallel workers for filesystem scanning |
| `--log-file PATH` | auto | Custom log file path |
| `--verbose` | off | Detailed diagnostic output |
| `--gui` | off | Launch tkinter GUI instead of CLI |

### Examples

```bash
# Preview what would change (safe, recommended first step)
python remap_assets.py acdsee_source/Default /mnt/nas/photos --level 2 --dry-run

# Apply changes with backup
python remap_assets.py acdsee_source/Default /mnt/nas/photos --level 2

# Skip backup (use with caution)
python remap_assets.py acdsee_source/Default /mnt/nas/photos --level 1 --no-backup
```

## How It Works

### Volume Identification

ACDSee identifies volumes by their Windows Volume Serial Number (stored
as `DISC_ID` in the `FolderRoot` table).  The database links asset
information to `volume_serial + path + filename`.  The remap tool uses
path and filename matching rather than serial number lookup, which makes
it robust across filesystem migrations.

### Filesystem Scanning

When `--level 2` or higher is used, the tool walks the entire image root
to build an index of filenames.  Directories named `@eaDir`, `#recycle`,
`.DS_Store`, `System Volume Information` and similar are automatically
skipped.

Network storage (NAS over OpenVPN/SMB) is supported with automatic retry
on transient I/O errors (EIO, ESTALE, ENETRESET).

### Database Updates

For Level 1 matches, the tool updates `FolderRoot.NAME` and propagates
prefix mappings through the cache so that all assets under the same
prefix are quickly resolved.  New folder records are created in the
`Folder` table hierarchy when needed.

For Level 2/3 matches, `Asset.FOLDER_ID` is updated (and `Asset.NAME` if
renamed).  After writing, `.cdx` index files are deleted; ACDSee rebuilds
them on next launch.

### Output Files

Two files are produced per run:

- `remap_YYYYMMDD_HHMMSS.log` — detailed resolution log with per-file info
- `remap_mappings_YYYYMMDD_HHMMSS.json` — discovered prefix mappings and resolved paths (for audit)

## Testing

```bash
python -m unittest test_remap_assets -v
```

Fast unit tests cover path mapping, directory filtering, signature
matching, and filesystem operations.  Database-loading integration tests
require the `acdsee_source/` directory and take longer to run.

## Running tests

```bash
python -m unittest test_remap_assets -v
```
