from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.database import Database
from app.db.image_parser import parse_image_reference


logger = logging.getLogger("kocc.sqlite")
MINIMUM_SNAPSHOT_INTERVAL = timedelta(minutes=5)


class SnapshotRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize()

    def save_snapshot(
        self,
        cluster: str,
        snapshot: dict[str, Any],
        workload_images: list[dict[str, str]],
        collected_at: datetime | None = None,
    ) -> bool:
        started = time.perf_counter()
        now = collected_at or datetime.now(timezone.utc)
        snapshot_time = now.astimezone(timezone.utc).isoformat()
        self.initialize()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                "SELECT snapshot_time FROM cluster_snapshot WHERE cluster=? "
                "ORDER BY snapshot_time DESC LIMIT 1",
                (cluster,),
            ).fetchone()
            if latest:
                latest_time = datetime.fromisoformat(latest["snapshot_time"])
                if now.astimezone(timezone.utc) - latest_time < MINIMUM_SNAPSHOT_INTERVAL:
                    return False
            connection.execute("""
                INSERT INTO cluster_snapshot (
                    snapshot_time, cluster, nodes_total, nodes_ready,
                    pods_total, pods_running, namespaces_total,
                    deployments_total, statefulsets_total, daemonsets_total,
                    routes_total, pvcs_total, events_warning,
                    collection_duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot_time, cluster,
                snapshot.get("nodes_total"), snapshot.get("nodes_ready"),
                snapshot.get("pods_total"), snapshot.get("pods_running"),
                snapshot.get("namespaces_total"),
                snapshot.get("deployments_total"),
                snapshot.get("statefulsets_total"),
                snapshot.get("daemonsets_total"), snapshot.get("routes_total"),
                snapshot.get("pvcs_total"), snapshot.get("events_warning"),
                snapshot.get("collection_duration_ms"),
            ))
            for item in workload_images:
                parsed = parse_image_reference(item["image"])
                connection.execute("""
                    INSERT INTO workload_image_snapshot (
                        snapshot_time, cluster, namespace, kind, workload,
                        container, image, registry, repository, tag, digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    snapshot_time, cluster, item["namespace"], item["kind"],
                    item["workload"], item["container"], item["image"],
                    parsed.registry, parsed.repository, parsed.tag, parsed.digest,
                ))
        duration_ms = round((time.perf_counter() - started) * 1000)
        logger.info(
            "sqlite_snapshot_saved cluster=%s images=%s", cluster,
            len(workload_images),
        )
        logger.info(
            "sqlite_snapshot_duration cluster=%s duration_ms=%s",
            cluster, duration_ms,
        )
        return True
