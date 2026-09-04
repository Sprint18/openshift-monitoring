from datetime import datetime, timedelta, timezone

from app.db.database import Database
from app.db.image_parser import parse_image_reference
from app.db.repository import SnapshotRepository


def snapshot() -> dict[str, int | None]:
    return {
        "nodes_total": 6,
        "nodes_ready": 5,
        "pods_total": 120,
        "pods_running": 110,
        "namespaces_total": 24,
        "deployments_total": None,
        "statefulsets_total": None,
        "daemonsets_total": None,
        "routes_total": None,
        "pvcs_total": None,
        "events_warning": None,
        "collection_duration_ms": 1234,
    }


def image_record() -> dict[str, str]:
    return {
        "namespace": "sandbox-app",
        "kind": "ReplicaSet",
        "workload": "api-7d9f",
        "container": "api",
        "image": "registry.example.com/team/api:1.2.3@sha256:abc123",
    }


def test_database_creation_wal_and_schema_migration(tmp_path) -> None:
    path = tmp_path / "nested" / "kocc.db"
    database = Database(path)
    database.initialize()
    database.initialize()

    assert path.is_file()
    with database.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        versions = connection.execute(
            "SELECT version FROM schema_version"
        ).fetchall()
        assert [row[0] for row in versions] == [1, 2]
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "schema_version", "cluster_snapshot", "workload_image_snapshot", "portal_user"
    } <= tables


def test_snapshot_insert_interval_and_image_repository(tmp_path) -> None:
    repository = SnapshotRepository(Database(tmp_path / "kocc.db"))
    first_time = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)

    assert repository.save_snapshot(
        "kkbocptest1", snapshot(), [image_record()], first_time
    ) is True
    assert repository.save_snapshot(
        "kkbocptest1", snapshot(), [image_record()], first_time + timedelta(minutes=4)
    ) is False
    assert repository.save_snapshot(
        "kkbocptest1", snapshot(), [image_record()], first_time + timedelta(minutes=5)
    ) is True

    with repository.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM cluster_snapshot").fetchone()[0] == 2
        image = connection.execute(
            "SELECT registry, repository, tag, digest FROM workload_image_snapshot LIMIT 1"
        ).fetchone()
    assert tuple(image) == (
        "registry.example.com", "team/api", "1.2.3", "sha256:abc123",
    )


def test_snapshot_survives_repository_recreation(tmp_path) -> None:
    path = tmp_path / "kocc.db"
    SnapshotRepository(Database(path)).save_snapshot(
        "kkbocptest1", snapshot(), [],
        datetime(2026, 8, 26, tzinfo=timezone.utc),
    )
    restarted = SnapshotRepository(Database(path))
    restarted.initialize()
    with restarted.database.connect() as connection:
        row = connection.execute(
            "SELECT cluster, nodes_total FROM cluster_snapshot"
        ).fetchone()
    assert tuple(row) == ("kkbocptest1", 6)


def test_image_parser_supports_registry_tag_digest_and_docker_defaults() -> None:
    parsed = parse_image_reference(
        "registry.example.com:5000/team/api:2.0@sha256:deadbeef"
    )
    assert parsed.registry == "registry.example.com:5000"
    assert parsed.repository == "team/api"
    assert parsed.tag == "2.0"
    assert parsed.digest == "sha256:deadbeef"

    default = parse_image_reference("nginx:1.27")
    assert default.registry == "docker.io"
    assert default.repository == "library/nginx"
    assert default.tag == "1.27"
    assert default.digest is None
