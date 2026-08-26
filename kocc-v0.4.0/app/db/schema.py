from __future__ import annotations

from collections.abc import Sequence


MIGRATIONS: Sequence[tuple[int, str]] = (
    (1, """
        CREATE TABLE IF NOT EXISTS cluster_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time TEXT NOT NULL,
            cluster TEXT NOT NULL,
            nodes_total INTEGER,
            nodes_ready INTEGER,
            pods_total INTEGER,
            pods_running INTEGER,
            namespaces_total INTEGER,
            deployments_total INTEGER,
            statefulsets_total INTEGER,
            daemonsets_total INTEGER,
            routes_total INTEGER,
            pvcs_total INTEGER,
            events_warning INTEGER,
            collection_duration_ms INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_cluster_snapshot_cluster_time
            ON cluster_snapshot(cluster, snapshot_time DESC);

        CREATE TABLE IF NOT EXISTS workload_image_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time TEXT NOT NULL,
            cluster TEXT NOT NULL,
            namespace TEXT NOT NULL,
            kind TEXT NOT NULL,
            workload TEXT NOT NULL,
            container TEXT NOT NULL,
            image TEXT NOT NULL,
            registry TEXT,
            repository TEXT,
            tag TEXT,
            digest TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_workload_image_cluster_time
            ON workload_image_snapshot(cluster, snapshot_time DESC);
    """),
)
