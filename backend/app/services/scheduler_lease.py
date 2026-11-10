"""Database-backed singleton lease shared by scheduler replicas."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import SessionLocal, write_scope
from app.models.base import utcnow
from app.models.scheduler import SchedulerLease


class SqlSchedulerLease:
    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory

    def _write(self):
        return write_scope(self._session_factory)

    def _read_session(self) -> Session:
        factory = self._session_factory or SessionLocal
        return factory()

    def acquire(self, name: str, owner_id: str, *, lease_seconds: int) -> bool:
        now = utcnow()
        expires_at = now + timedelta(seconds=max(1, int(lease_seconds)))
        with self._write() as session:
            updated = (
                session.query(SchedulerLease)
                .filter(SchedulerLease.name == name)
                .filter(
                    or_(
                        SchedulerLease.owner_id.is_(None),
                        SchedulerLease.lease_expires_at.is_(None),
                        SchedulerLease.lease_expires_at <= now,
                        SchedulerLease.owner_id == owner_id,
                    )
                )
                .update(
                    {
                        SchedulerLease.owner_id: owner_id,
                        SchedulerLease.lease_expires_at: expires_at,
                        SchedulerLease.heartbeat_at: now,
                        SchedulerLease.last_run_started_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if updated == 1:
                return True
            try:
                session.add(
                    SchedulerLease(
                        name=name,
                        owner_id=owner_id,
                        lease_expires_at=expires_at,
                        heartbeat_at=now,
                        last_run_started_at=now,
                        last_result={},
                    )
                )
                session.flush()
                return True
            except IntegrityError:
                session.rollback()
                return False

    def renew(self, name: str, owner_id: str, *, lease_seconds: int) -> bool:
        now = utcnow()
        with self._write() as session:
            updated = (
                session.query(SchedulerLease)
                .filter(SchedulerLease.name == name)
                .filter(SchedulerLease.owner_id == owner_id)
                .filter(SchedulerLease.lease_expires_at.isnot(None))
                .filter(SchedulerLease.lease_expires_at > now)
                .update(
                    {
                        SchedulerLease.lease_expires_at: now
                        + timedelta(seconds=max(1, int(lease_seconds))),
                        SchedulerLease.heartbeat_at: now,
                    },
                    synchronize_session=False,
                )
            )
            return updated == 1

    def record_result(
        self,
        name: str,
        owner_id: str,
        *,
        status: str,
        result: dict[str, Any],
        error: str | None = None,
    ) -> bool:
        now = utcnow()
        with self._write() as session:
            updated = (
                session.query(SchedulerLease)
                .filter(SchedulerLease.name == name)
                .filter(SchedulerLease.owner_id == owner_id)
                .update(
                    {
                        SchedulerLease.last_run_finished_at: now,
                        SchedulerLease.last_status: status[:32],
                        SchedulerLease.last_error: (error or "")[:1000] or None,
                        SchedulerLease.last_result: result,
                        SchedulerLease.heartbeat_at: now,
                    },
                    synchronize_session=False,
                )
            )
            return updated == 1

    def release(self, name: str, owner_id: str) -> bool:
        now = utcnow()
        with self._write() as session:
            updated = (
                session.query(SchedulerLease)
                .filter(SchedulerLease.name == name)
                .filter(SchedulerLease.owner_id == owner_id)
                .update(
                    {
                        SchedulerLease.owner_id: None,
                        SchedulerLease.lease_expires_at: None,
                        SchedulerLease.heartbeat_at: now,
                    },
                    synchronize_session=False,
                )
            )
            return updated == 1

    def status(self, name: str) -> dict[str, Any] | None:
        session = self._read_session()
        try:
            row = session.get(SchedulerLease, name)
            if row is None:
                return None
            return {
                "name": row.name,
                "owner_id": row.owner_id,
                "lease_expires_at": row.lease_expires_at,
                "heartbeat_at": row.heartbeat_at,
                "last_run_started_at": row.last_run_started_at,
                "last_run_finished_at": row.last_run_finished_at,
                "last_status": row.last_status,
                "last_error": row.last_error,
                "last_result": dict(row.last_result or {}),
            }
        finally:
            session.close()


__all__ = ["SqlSchedulerLease"]
