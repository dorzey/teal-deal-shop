"""Static checks on the pages. The storefront and the dashboard both run the Haybarn build
of DuckDB-Wasm, and the version is pinned in several places in each (preloads, the import,
the bundle URLs). If those drift apart a page quietly loads two copies of the engine, or an
extension that doesn't match — so check they agree.   make test
"""
import os
import re
import unittest

PUBLIC = os.path.join(os.path.dirname(__file__), "public")
PIN = re.compile(r"@haybarn/haybarn-wasm@([0-9][^/'\"]*)")


def page(name):
    with open(os.path.join(PUBLIC, name)) as f:
        return f.read()


class HaybarnPin(unittest.TestCase):
    def versions(self, name):
        return set(PIN.findall(page(name)))

    def test_each_page_pins_exactly_one_version(self):
        for name in ("index.html", "dashboard.html"):
            with self.subTest(page=name):
                self.assertEqual(len(self.versions(name)), 1, f"{name}: {self.versions(name)}")

    def test_both_pages_pin_the_same_version(self):
        self.assertEqual(self.versions("index.html"), self.versions("dashboard.html"))

    def test_the_pin_is_an_exact_release_not_a_floating_tag(self):
        (version,) = self.versions("index.html")
        self.assertRegex(version, r"^\d+\.\d+\.\d+(-rc\d+)?$")

    def test_the_storefront_no_longer_loads_the_old_upstream_build(self):
        self.assertNotIn("@duckdb/duckdb-wasm", page("index.html"))


class Preloads(unittest.TestCase):
    def test_the_storefront_preloads_the_files_its_engine_requests(self):
        html = page("index.html")
        (version,) = set(PIN.findall(html))
        self.assertIn(f"@haybarn/haybarn-wasm@{version}/dist/duckdb-base.wasm", html.split("<style>")[0])
        for ext in ("parquet", "fts", "vss"):
            self.assertIn(f"/core/v2.0.0/wasm_eh/{ext}.duckdb_extension.wasm", html.split("<style>")[0])


if __name__ == "__main__":
    unittest.main()
