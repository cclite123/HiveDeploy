import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), f"hivedeploy-tests-{os.getpid()}.db"))

from app.image_management import (
    execute_cleanup,
    normalize_registry,
    order_image_sources,
    preview_cleanup,
    resolve_image_registries,
    seed_default_image_sources,
)


class FakeImage:
    def __init__(self, image_id, tag, created, size=100):
        self.id = image_id
        self.tags = [tag]
        self.attrs = {"Created": created, "Size": size}


class FakeContainer:
    def __init__(self, image):
        self.image = image
        self.attrs = {"Image": image.id}


class FakeContainers:
    def __init__(self, responses=None):
        self.responses = list(responses or [[]])
        self.calls = 0

    def list(self, all=False):
        assert all is True
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


class FakeImages:
    def __init__(self, images):
        self._images = images
        self.removed = []

    def list(self):
        return self._images

    def remove(self, image_id, **kwargs):
        self.removed.append((image_id, kwargs))


class FakeClient:
    def __init__(self, images, container_responses=None):
        self.images = FakeImages(images)
        self.containers = FakeContainers(container_responses)


class ImageSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.database import engine
        from app.models import Base
        Base.metadata.create_all(bind=engine)

    def setUp(self):
        from app.database import SessionLocal
        from app.models import ImageSource
        db = SessionLocal()
        db.query(ImageSource).delete()
        db.commit(); db.close()

    def test_legacy_sources_are_seeded_in_original_order(self):
        from app.database import SessionLocal
        from app.models import ImageSource
        seed_default_image_sources()
        db = SessionLocal()
        rows = db.query(ImageSource).order_by(ImageSource.priority).all()
        self.assertEqual("", rows[0].registry)
        self.assertTrue(rows[0].is_official)
        self.assertEqual("docker.1ms.run", rows[1].registry)
        self.assertEqual([None, "docker.1ms.run"], resolve_image_registries()[:2])
        db.close()

    def test_registry_normalization_and_validation(self):
        self.assertEqual("mirror.example.com:5000/cache", normalize_registry("mirror.EXAMPLE.com:5000/cache/"))
        for invalid in (
            "https://mirror.example.com", "user:pass@mirror.example.com",
            "mirror.example.com?token=x", "mirror.example.com:99999", "bad host",
            "bad..example.com", "999.999.999.999",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_registry(invalid)

    def test_selected_source_is_first_then_automatic_fallback(self):
        sources = [
            SimpleNamespace(id=1, enabled=True, is_default=True, priority=10),
            SimpleNamespace(id=2, enabled=True, is_default=False, priority=1),
            SimpleNamespace(id=3, enabled=True, is_default=False, priority=2),
        ]
        self.assertEqual([1, 2, 3], [item.id for item in order_image_sources(sources)])
        self.assertEqual([3, 1, 2], [item.id for item in order_image_sources(sources, 3)])

    def test_disabled_source_cannot_be_selected(self):
        sources = [SimpleNamespace(id=1, enabled=False, is_default=False, priority=1)]
        with self.assertRaises(ValueError):
            order_image_sources(sources, 1)

    def test_pull_falls_back_after_selected_source_failure(self):
        from app.docker_manager import pull_with_fallback

        class Api:
            def __init__(self):
                self.pulls = []
                self.tags = []

            def pull(self, image, stream, decode):
                self.pulls.append(image)
                if image.startswith("selected.example/"):
                    raise RuntimeError("network timeout")
                return iter([{"status": "Pull complete", "id": "layer"}])

            def tag(self, image, target):
                self.tags.append((image, target))

        client = SimpleNamespace(api=Api())
        with patch("app.docker_manager.time.sleep"):
            result = pull_with_fallback(
                client, "soulter/astrbot:latest", lambda *_: None,
                registries=["selected.example", "fallback.example", None],
            )
        self.assertEqual("fallback.example/soulter/astrbot:latest", result)
        self.assertEqual(
            ["selected.example/soulter/astrbot:latest", "fallback.example/soulter/astrbot:latest"],
            client.api.pulls,
        )


class ImageCleanupTests(unittest.TestCase):
    def setUp(self):
        self.new = FakeImage("sha256:new", "soulter/astrbot:latest", "2026-08-05T12:00:00Z", 200)
        self.old = FakeImage("sha256:old", "mirror/soulter/astrbot:v1", "2026-07-01T12:00:00Z", 100)

    def test_latest_is_kept_and_only_unreferenced_old_image_is_candidate(self):
        result = preview_cleanup(FakeClient([self.old, self.new]))
        reasons = {entry["image_id"]: entry["reason"] for entry in result["entries"]}
        self.assertEqual("latest", reasons[self.new.id])
        self.assertEqual("old_unused", reasons[self.old.id])
        self.assertEqual(1, result["candidate_count"])

    def test_running_or_stopped_container_reference_keeps_old_image(self):
        client = FakeClient([self.old, self.new], [[FakeContainer(self.old)]])
        result = preview_cleanup(client)
        old = next(entry for entry in result["entries"] if entry["image_id"] == self.old.id)
        self.assertEqual("old_but_in_use", old["reason"])
        self.assertFalse(old["delete_candidate"])

    def test_unknown_project_images_never_become_candidates(self):
        unknown = FakeImage("sha256:other", "postgres:17", "2020-01-01T00:00:00Z")
        result = preview_cleanup(FakeClient([unknown, self.old, self.new]))
        self.assertNotIn(unknown.id, {entry["image_id"] for entry in result["entries"]})

    def test_new_reference_before_delete_skips_image(self):
        client = FakeClient(
            [self.old, self.new],
            [[], [FakeContainer(self.old)]],
        )
        result = execute_cleanup(client)
        self.assertEqual([], client.images.removed)
        self.assertEqual("new_reference_detected", result["skipped"][0]["reason"])

    def test_unused_old_image_is_removed_without_force(self):
        client = FakeClient([self.old, self.new], [[], []])
        result = execute_cleanup(client)
        self.assertEqual(
            [(self.old.id, {"force": False, "noprune": True})],
            client.images.removed,
        )
        self.assertEqual(100, result["released_bytes"])


if __name__ == "__main__":
    unittest.main()
