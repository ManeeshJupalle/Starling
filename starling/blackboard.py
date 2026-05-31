"""Blackboard — durable shared state (SQLite).

The single source of truth for projects and tasks, and the reason project-mode
survives a crash. The scheduler reads *desired state* from here rather than holding
work in memory, so a restart resumes in-flight projects. See ARCHITECTURE.md §2.3
and the data shapes in §5.

Workers are stateless; everything durable lives here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from enum import Enum
from typing import Any, Optional

DEFAULT_DB_PATH = "starling.db"


class TaskStatus(str, Enum):
    """Lifecycle states a task moves through (ARCHITECTURE.md §3)."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    AWAITING_HUMAN = "awaiting_human"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    goal       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES projects(id),
    role        TEXT    NOT NULL,
    description TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    depends_on  TEXT    NOT NULL DEFAULT '[]',
    inputs      TEXT    NOT NULL DEFAULT '{}',
    output      TEXT,
    question    TEXT,
    checkpoint  TEXT,
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Blackboard:
    """SQLite-backed store for projects and tasks.

    A single connection guarded by a lock: the scheduler and channel loop touch the
    blackboard concurrently (§2.4), so access is serialized. ``check_same_thread`` is
    disabled because those two may live on different threads.
    """

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # --- projects ----------------------------------------------------------

    def create_project(self, chat_id: int, goal: str) -> int:
        """Create a project and return its id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO projects (chat_id, goal) VALUES (?, ?)",
                (chat_id, goal),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_project(self, project_id: int) -> Optional[dict[str, Any]]:
        """Return a project row as a dict (e.g. for its ``chat_id``), or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return dict(row) if row else None

    # --- tasks -------------------------------------------------------------

    def add_task(
        self,
        role: str,
        description: str,
        *,
        project_id: Optional[int] = None,
        depends_on: Optional[list[int]] = None,
        inputs: Optional[dict[str, Any]] = None,
    ) -> int:
        """Insert a task and return its id.

        A task with no dependencies starts ``ready``; otherwise it starts
        ``pending`` and is promoted later by :meth:`ready_tasks`.
        """
        depends_on = list(depends_on or [])
        inputs = inputs or {}
        status = TaskStatus.READY if not depends_on else TaskStatus.PENDING
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks (project_id, role, description, status, depends_on, inputs) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    role,
                    description,
                    status.value,
                    json.dumps(depends_on),
                    json.dumps(inputs),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def set_status(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        output: Any = None,
        question: Optional[str] = None,
        checkpoint: Any = None,
    ) -> None:
        """Update a task's status, optionally storing ``output``/``question``/``checkpoint``."""
        fields = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = [TaskStatus(status).value]
        if output is not None:
            fields.append("output = ?")
            params.append(json.dumps(output))
        if question is not None:
            fields.append("question = ?")
            params.append(question)
        if checkpoint is not None:
            fields.append("checkpoint = ?")
            params.append(json.dumps(checkpoint))
        params.append(task_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", params
            )
            self._conn.commit()

    def get_task(self, task_id: int) -> Optional[dict[str, Any]]:
        """Return a task as a dict (JSON fields decoded), or ``None`` if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def ready_tasks(self) -> list[dict[str, Any]]:
        """Promote pending tasks whose deps are all done, then return ready tasks.

        Called by the scheduler each tick: the promotion side effect is persisted so
        newly-unblocked work survives a restart.
        """
        with self._lock:
            done_ids = {
                row["id"]
                for row in self._conn.execute(
                    "SELECT id FROM tasks WHERE status = ?", (TaskStatus.DONE.value,)
                )
            }
            pending = self._conn.execute(
                "SELECT id, depends_on FROM tasks WHERE status = ?",
                (TaskStatus.PENDING.value,),
            ).fetchall()
            for row in pending:
                deps = json.loads(row["depends_on"])
                if all(dep in done_ids for dep in deps):
                    self._conn.execute(
                        "UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (TaskStatus.READY.value, row["id"]),
                    )
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY id",
                (TaskStatus.READY.value,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def project_tasks(self, project_id: int) -> list[dict[str, Any]]:
        """Return all tasks belonging to a project, ordered by id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY id", (project_id,)
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def reset_running(self) -> int:
        """Requeue tasks left ``running`` by a crash back to ``ready``; return count.

        A task is marked ``running`` before its model call and ``done`` only after the
        output is stored, so any task still ``running`` at startup was interrupted and
        must re-run. ``done`` tasks are untouched, so completed work is never redone.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE status = ?",
                (TaskStatus.READY.value, TaskStatus.RUNNING.value),
            )
            self._conn.commit()
            return cur.rowcount

    def awaiting_human(self, chat_id: int) -> Optional[dict[str, Any]]:
        """Return the task paused for a human decision in this chat, or ``None``.

        If more than one task is paused in the same chat, the most recently updated
        one is returned (the question the user is most likely answering).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT t.* FROM tasks t JOIN projects p ON t.project_id = p.id "
                "WHERE p.chat_id = ? AND t.status = ? "
                "ORDER BY t.updated_at DESC, t.id DESC LIMIT 1",
                (chat_id, TaskStatus.AWAITING_HUMAN.value),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._conn.close()

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task["depends_on"] = json.loads(task["depends_on"])
        task["inputs"] = json.loads(task["inputs"])
        task["output"] = json.loads(task["output"]) if task["output"] is not None else None
        task["checkpoint"] = json.loads(task["checkpoint"]) if task["checkpoint"] is not None else None
        return task
