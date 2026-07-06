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
