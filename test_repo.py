"""Consistency checks across the repo's config files: things that must agree but live in
different places, so they quietly drift.   make test
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


class Pins(unittest.TestCase):
    def test_the_dockerfile_cli_version_matches_the_python_package_pin(self):
        pinned = re.search(r'"duckdb==([^"]+)"', read("pyproject.toml")).group(1)
        arg = re.search(r"^ARG DUCKDB_VERSION=(\S+)", read("Dockerfile"), flags=re.M).group(1)
        self.assertEqual(arg, pinned)

    def test_the_cli_tarballs_are_checksummed_for_both_architectures(self):
        dockerfile = read("Dockerfile")
        for arch in ("AMD64", "ARM64"):
            self.assertRegex(dockerfile, rf"^ARG DUCKDB_SHA256_{arch}=[0-9a-f]{{64}}$".replace("^", "(?m)^"))
        self.assertIn("sha256sum -c", dockerfile)

    def test_base_images_are_pinned_by_digest(self):
        self.assertRegex(read("Dockerfile"), r"(?m)^FROM python:[\w.-]+@sha256:[0-9a-f]{64}$")
        self.assertRegex(read("compose.prod.yaml"), r"image: cloudflare/cloudflared:[\w.]+@sha256:[0-9a-f]{64}")


class Secrets(unittest.TestCase):
    def test_env_files_are_kept_out_of_git_and_out_of_the_image(self):
        gitignore = read(".gitignore").splitlines()
        dockerignore = read(".dockerignore").splitlines()
        self.assertIn(".env", gitignore)
        self.assertIn(".env", dockerignore)
        self.assertIn("!.env.example", dockerignore)   # the template is harmless and may ship


if __name__ == "__main__":
    unittest.main()
