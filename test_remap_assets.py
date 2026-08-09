#!/usr/bin/env python3
"""Tests for the ACDSee asset path remapping tool."""

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from remap_assets import (
    VfpFile,
    AcdDatabase,
    PathMapper,
    _should_skip_dir,
    _build_signature,
    _signature_match_score,
    _safe_walk,
    _collect_images,
    read_exif,
    safe_isfile,
    safe_isdir,
    safe_exists,
    safe_getsize,
    retry_on_network_error,
    FileFinder,
    Remapper,
    backup_database,
)

# =============================================================================
# Helpers
# =============================================================================

class BaseTest(unittest.TestCase):
    DB_DIR = os.path.join(os.path.dirname(__file__), "acdsee_source", "Default")

    def setUp(self):
        self._tmpdirs: list[str] = []

    def tearDown(self):
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

    def mktemp(self) -> str:
        d = tempfile.mkdtemp(prefix="remap_test_")
        self._tmpdirs.append(d)
        return d


# =============================================================================
# VfpFile tests
# =============================================================================

class TestVfpFile(BaseTest):
    def test_read_folder_root(self):
        vf = VfpFile(os.path.join(self.DB_DIR, "FolderRoot.dbf"))
        recs = vf.read_all()
        self.assertGreater(len(recs), 0)
        self.assertIn("NAME", recs[0])
        self.assertIn("FOLD_RT_ID", recs[0])
        self.assertIn("DISC_ID", recs[0])

    def test_read_folder(self):
        vf = VfpFile(os.path.join(self.DB_DIR, "Folder.dbf"))
        recs = vf.read_all()
        self.assertGreater(len(recs), 8000)
        rec = recs[0]
        self.assertIn("FOLDER_ID", rec)
        self.assertIn("PRNT_ID", rec)

    def test_read_asset(self):
        vf = VfpFile(os.path.join(self.DB_DIR, "Asset.dbf"))
        recs = vf.read_all()
        self.assertGreater(len(recs), 100000)
        self.assertIn("ASSET_ID", recs[0])
        self.assertIn("NAME", recs[0])
        self.assertIn("SIZE", recs[0])

    def test_field_offsets(self):
        vf = VfpFile(os.path.join(self.DB_DIR, "FolderRoot.dbf"))
        disp, length = vf.field_offset("NAME")
        self.assertIsInstance(disp, int)
        self.assertGreater(disp, 0)

    def test_field_offset_raises_for_missing(self):
        vf = VfpFile(os.path.join(self.DB_DIR, "FolderRoot.dbf"))
        with self.assertRaises(KeyError):
            vf.field_offset("NONEXISTENT_FIELD")


# =============================================================================
# AcdDatabase tests
# =============================================================================

class TestAcdDatabase(BaseTest):
    def setUp(self):
        super().setUp()
        self.db = AcdDatabase(self.DB_DIR)
        self.db.load()

    def test_loads_all_tables(self):
        self.assertGreater(len(self.db.folder_roots), 0)
        self.assertGreater(len(self.db.folders), 1000)
        self.assertGreater(len(self.db.assets), 100000)
        self.assertGreater(len(self.db.asset_exif), 100000)
        self.assertGreater(len(self.db.exif_image), 100000)

    def test_column_mapping(self):
        self.assertGreater(len(self.db._col_map), 0)
        self.assertIn("COL00045", self.db._col_map)
        self.assertEqual(self.db._col_map["COL00045"], "Make")

    def test_build_folder_path(self):
        p = self.db.build_folder_path(1)  # root
        self.assertEqual(p, "")

        # Find a deeper folder
        for fid, fld in self.db.folders.items():
            prnt = int(fld.get("PRNT_ID", 0))
            if prnt != 0:
                p = self.db.build_folder_path(fid)
                self.assertTrue(p, f"Path should not be empty for FOLDER_ID={fid}")
                break

    def test_build_asset_path(self):
        for a in self.db.assets:
            if int(float(a.get("SIZE", 0) or 0)) > 0:
                path = self.db.build_asset_path(a)
                name = a.get("NAME", "")
                self.assertTrue(path.endswith(name),
                                f"Path '{path}' should end with '{name}'")
                break

    def test_get_asset_metadata_has_exif(self):
        # Find an asset with known camera metadata
        found = False
        for a in self.db.assets:
            meta = self.db.get_asset_metadata(a)
            if meta.get("camera_make"):
                self.assertIsInstance(meta["camera_make"], str)
                self.assertIsInstance(meta["camera_model"], str)
                self.assertEqual(meta["asset_id"], int(a.get("ASSET_ID", 0)))
                found = True
                break
        self.assertTrue(found, "Should find at least one asset with camera make")

    def test_get_asset_metadata_no_exif(self):
        # Assets without EXIF should return empty strings
        for a in self.db.assets:
            if not self.db.asset_exif.get(int(a.get("ASSET_ID", 0))):
                meta = self.db.get_asset_metadata(a)
                self.assertEqual(meta["camera_make"], "")
                self.assertEqual(meta["camera_model"], "")
                break


# =============================================================================
# PathMapper tests
# =============================================================================

class TestPathMapper(unittest.TestCase):
    def test_add_and_resolve_exact(self):
        pm = PathMapper()
        pm.add("/old_vol/photos", "/new_vol/archive/photos")
        r = pm.resolve("/old_vol/photos/2023/img.jpg")
        self.assertEqual(r, "/new_vol/archive/photos/2023/img.jpg")

    def test_resolve_no_match(self):
        pm = PathMapper()
        self.assertIsNone(pm.resolve("/anything/img.jpg"))

    def test_longest_prefix_wins(self):
        pm = PathMapper()
        pm.add("/old", "/new1")
        pm.add("/old/photos", "/new2/photos")
        r = pm.resolve("/old/photos/sub/img.jpg")
        self.assertEqual(r, "/new2/photos/sub/img.jpg")

    def test_empty_mappings(self):
        pm = PathMapper()
        self.assertEqual(pm.all_mappings(), {})


# =============================================================================
# Directory filtering
# =============================================================================

class TestDirFiltering(unittest.TestCase):
    def test_skip_dirs(self):
        for d in ["@eaDir", "#recycle", ".Trashes", ".fseventsd",
                  "System Volume Information", "$RECYCLE.BIN", ".DS_Store"]:
            self.assertTrue(_should_skip_dir(d), f"Should skip {d}")

    def test_not_skip_normal(self):
        for d in ["photos", "DCIM", "2024", "vacation"]:
            self.assertFalse(_should_skip_dir(d), f"Should NOT skip {d}")

    def test_skip_dot_underscore(self):
        self.assertTrue(_should_skip_dir("._hidden"))

    def test_safe_walk_exclude(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "photos", "@eaDir", "subdir"))
        os.makedirs(os.path.join(root, "photos", "#recycle"))
        os.makedirs(os.path.join(root, "photos", "good_dir"))
        try:
            paths = []
            for dp, dn, fn in _safe_walk(root):
                dn[:] = [d for d in dn if not _should_skip_dir(d)]
                paths.append(dp)
            self.assertIn(os.path.join(root, "photos", "good_dir"), paths)
            self.assertNotIn(os.path.join(root, "photos", "@eaDir", "subdir"), paths)
        finally:
            shutil.rmtree(root, ignore_errors=True)


# =============================================================================
# Signature matching
# =============================================================================

class TestSignatures(unittest.TestCase):
    def test_build_signature(self):
        meta = {
            "camera_make": "Canon",
            "camera_model": "EOS 5D Mark III",
            "date_time_original": "2020:01:15 12:00:00",
            "lens_model": "EF24-105mm",
            "focal_length": "50.0",
            "fnumber": "4.0",
        }
        sig = _build_signature(meta)
        self.assertEqual(sig["Make"], "canon")
        self.assertEqual(sig["Model"], "eos 5d mark iii")
        self.assertEqual(sig["DateTimeOriginal"], "2020:01:15 12:00:00")
        self.assertEqual(sig["LensModel"], "ef24-105mm")

    def test_signature_match_score(self):
        db = {"DateTimeOriginal": "2020-01-15", "Make": "nikon", "Model": "d850"}
        img = {"DateTimeOriginal": "2020-01-15", "Make": "Nikon", "Model": "D850"}
        self.assertEqual(_signature_match_score(db, img), 3)

    def test_signature_no_match(self):
        db = {"DateTimeOriginal": "2020-01-15", "Make": "canon"}
        img = {"DateTimeOriginal": "2020-06-01", "Make": "nikon"}
        self.assertEqual(_signature_match_score(db, img), 0)

    def test_signature_partial_match(self):
        db = {"DateTimeOriginal": "2020-01-15", "Make": "canon", "Model": "eos r5"}
        img = {"DateTimeOriginal": "2020-01-15", "Make": "Canon", "Model": "EOS R6"}
        self.assertEqual(_signature_match_score(db, img), 2)  # time + make


# =============================================================================
# FileFinder tests
# =============================================================================

class TestFileFinder(BaseTest):
    def test_exists(self):
        root = self.mktemp()
        fpath = os.path.join(root, "test.jpg")
        with open(fpath, "w") as f:
            f.write("data")
        ff = FileFinder(root)
        self.assertTrue(ff.exists(fpath))
        self.assertFalse(ff.exists(os.path.join(root, "nonexistent.jpg")))

    def test_check_relative(self):
        root = self.mktemp()
        sub = os.path.join("a", "b", "c.jpg")
        os.makedirs(os.path.join(root, "a", "b"))
        with open(os.path.join(root, sub), "w") as f:
            f.write("data")
        ff = FileFinder(root)
        result = ff.check_relative("a/b/c.jpg")
        self.assertIsNotNone(result)
        result2 = ff.check_relative("nonexistent.jpg")
        self.assertIsNone(result2)

    def test_build_index(self):
        root = self.mktemp()
        os.makedirs(os.path.join(root, "pics"))
        for fn in ["img001.jpg", "img002.png", "img003.jpg"]:
            with open(os.path.join(root, "pics", fn), "w") as f:
                f.write("data")
        ff = FileFinder(root)
        ff.build_index()
        matches = ff.find_by_filename("img002.png")
        self.assertEqual(len(matches), 1)
        self.assertIn("img002.png", matches[0])

    def test_find_by_filename_case_insensitive(self):
        root = self.mktemp()
        with open(os.path.join(root, "TestFile.JPG"), "w") as f:
            f.write("data")
        ff = FileFinder(root)
        ff.build_index()
        m = ff.find_by_filename("testfile.jpg")
        self.assertEqual(len(m), 1)


# =============================================================================
# Remapper integration tests
# =============================================================================

class TestRemapper(BaseTest):
    def setUp(self):
        super().setUp()
        self.db = AcdDatabase(self.DB_DIR)
        self.db.load()

    def test_level1_direct_match(self):
        """When the relative path exists directly under image_root, L1 resolves it."""
        root = self.mktemp()
        # Find an asset with a path we can replicate
        for a in self.db.assets:
            name = str(a.get("NAME", "") or "")
            if name.lower().endswith((".jpg", ".jpeg", ".png")):
                path = self.db.build_asset_path(a)
                if ":" in path:
                    _, rest = path.split(":", 1)
                    rest = rest.lstrip("/").lstrip("\\")
                    target = os.path.join(root, rest)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "w") as f:
                        f.write("x")
                    r = Remapper(self.db, self.DB_DIR, root, level=1, dry_run=True)
                    r.run()
                    # Should have at least the directly matched file as present or L1-resolved
                    present_or_resolved = r.stats["present"] + r.stats["level1"]
                    self.assertGreater(present_or_resolved, 0,
                                       f"At least the test file should be present or resolved")
                    return

    def test_level1_probe_root(self):
        """When a file is under a subdirectory of image_root, L1 probes to find it."""
        root = self.mktemp()
        subdir = "photo_archive"
        os.makedirs(os.path.join(root, subdir))

        for a in self.db.assets:
            name = str(a.get("NAME", "") or "")
            if name.lower().endswith((".jpg", ".jpeg")):
                path = self.db.build_asset_path(a)
                if ":" in path:
                    _, rest = path.split(":", 1)
                    rest = rest.lstrip("/").lstrip("\\")
                    # Place file under root/subdir/<rest>
                    target = os.path.join(root, subdir, rest)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "w") as f:
                        f.write("x")

                    r = Remapper(self.db, self.DB_DIR, root, level=1, dry_run=True)
                    r.run()
                    self.assertGreaterEqual(r.stats["level1"], 1,
                                            "L1 should find the file via probe_root")
                    # Verify prefix mapping was cached
                    mappings = r.mapper.all_mappings()
                    self.assertGreater(len(mappings), 0)
                    return

    def test_dry_run_does_not_modify_files(self):
        """In dry-run mode, no DBF files should be written."""
        # Make a copy of the DB for safety
        import shutil
        tmp_db = self.mktemp()
        shutil.copytree(self.DB_DIR, os.path.join(tmp_db, "Default"))

        db2 = AcdDatabase(os.path.join(tmp_db, "Default"))
        db2.load()

        r = Remapper(db2, os.path.join(tmp_db, "Default"),
                     "/nonexistent_root", level=1, dry_run=True)
        r.run()

        # No CDX files should have been deleted
        cdx_files = [f for f in os.listdir(os.path.join(tmp_db, "Default"))
                     if f.endswith(".cdx")]
        self.assertGreater(len(cdx_files), 0, "CDX files should remain in dry-run")

    def test_dry_run_logs_unresolved(self):
        """In dry-run with no files, all should be unresolved."""
        r = Remapper(self.db, self.DB_DIR, "/nonexistent_root",
                     level=1, dry_run=True)
        r.run()
        self.assertGreater(r.stats["unresolved"], 0)
        self.assertGreater(r.stats["orphan"], 0)


# =============================================================================
# PathMapper cache test
# =============================================================================

class TestPathMapperCache(BaseTest):
    def test_mapping_applied_to_subsequent_orphans(self):
        """After one file is found, its prefix mapping helps find others."""
        db = AcdDatabase(self.DB_DIR)
        db.load()

        root = self.mktemp()
        subdir = "vol_archive"
        os.makedirs(os.path.join(root, subdir))

        # Place TWO files from the same drive under root/subdir
        files_placed = 0
        found_drive = None
        for a in db.assets:
            if files_placed >= 2:
                break
            name = str(a.get("NAME", "") or "")
            if not name.lower().endswith((".jpg", ".jpeg")):
                continue
            path = db.build_asset_path(a)
            if ":" not in path:
                continue
            drive, rest = path.split(":", 1)
            rest = rest.lstrip("/").lstrip("\\")
            if found_drive and drive != found_drive:
                continue
            found_drive = drive

            target = os.path.join(root, subdir, rest)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as f:
                f.write("x")
            files_placed += 1

        self.assertEqual(files_placed, 2,
                         "Should have placed 2 test files from same drive")

        r = Remapper(db, self.DB_DIR, root, level=1, dry_run=True)
        r.run()
        self.assertGreaterEqual(r.stats["level1"], 1,
                                "At least one file should be resolved by L1")


# =============================================================================
# Network resilience tests
# =============================================================================

class TestNetworkResilience(unittest.TestCase):
    def test_retry_success_first_try(self):
        called = [0]
        def func():
            called[0] += 1
            return "ok"
        result = retry_on_network_error(func)
        self.assertEqual(result, "ok")
        self.assertEqual(called[0], 1)

    def test_safe_getsize(self):
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(b"hello world")
            path = tf.name
        try:
            self.assertEqual(safe_getsize(path), 11)
        finally:
            os.unlink(path)

    def test_safe_isfile(self):
        self.assertTrue(safe_isfile(__file__))
        self.assertFalse(safe_isfile("/nonexistent/path/file.xyz"))


# =============================================================================
# Requirements checks
# =============================================================================

class TestRequirements(unittest.TestCase):
    def test_dbfread_available(self):
        import dbfread
        self.assertIsNotNone(dbfread)

    def test_pillow_available(self):
        from PIL import Image
        self.assertIsNotNone(Image)


# =============================================================================
# CLI parsing (basic smoke test)
# =============================================================================

class TestCLI(unittest.TestCase):
    def test_imports_work(self):
        """Ensure the module imports without errors."""
        import remap_assets
        self.assertIsNotNone(remap_assets.main)


if __name__ == "__main__":
    unittest.main(verbosity=2)
