import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("worker_patch", ROOT / "deploy/railway/patch-worker-idle.py")
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)


class WorkerIdlePatchTests(unittest.TestCase):
    def source(self):
        return ("function createCatalogPool() {\n" + PATCH.ANCHOR + "\n"
                "releaseResources: capture.release; requestPool.run({ value: workerInput });\n}")

    def test_patch_is_narrow_idempotent_and_has_an_operator_rollback(self):
        source = self.source()
        actual = PATCH.transform(source)
        self.assertEqual(PATCH.transform(actual), actual)
        self.assertEqual(actual, PATCH.MARKER + "\n" + source.replace(PATCH.ANCHOR, PATCH.REPLACEMENT))
        self.assertIn('GENAIE_CATALOG_IDLE_RETIREMENT === "0" ? 0 : 60_000', actual)

    def test_incompatible_or_duplicated_vendor_code_is_rejected(self):
        for source in [self.source().replace("value: workerInput", "value: other"),
                       self.source().replace("idleTimeoutMs: 0", "idleTimeoutMs: 10"),
                       self.source() * 2]:
            with self.assertRaises(ValueError):
                PATCH.transform(source)

    def test_version_guard_and_installed_patch_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "dist").mkdir()
            path = root / "dist/prepared-model-catalog-worker-fixture.mjs"
            path.write_text(self.source())
            (root / "package.json").write_text(json.dumps({"version": PATCH.VERSION}))
            with self.assertRaises(ValueError):
                PATCH.patch(root, check=True)
            PATCH.patch(root)
            PATCH.patch(root, check=True)
            before = path.read_bytes()
            (root / "package.json").write_text('{"version":"other"}')
            with self.assertRaises(ValueError):
                PATCH.patch(root)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
