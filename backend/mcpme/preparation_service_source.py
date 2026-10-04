"""Service intake is metadata, never a manufactured filesystem snapshot."""
from __future__ import annotations

import time
import uuid

from .imports import ImportError, IMPORT_SCHEMA_VERSION, MAX_DRAFTS, RETENTION_SECONDS, _choices, _remove_attachment_tree
from .preparation_service_contracts import ServiceConnectionV1, ServiceSourceV1


def source(value, secrets, *, validate_components):
    try:
        connection = ServiceConnectionV1.model_validate(value)
        validate_components(connection)
        metadata = secrets.get(connection.secret_references.api_key)
        if metadata["status"] != "active":
            raise ImportError("service_secret_unavailable")
        return ServiceSourceV1(kind="service_connection", contract_version=1,
            connection=connection.model_dump(), revision=connection.revision, files=[]).model_dump()
    except ImportError:
        raise
    except (ValueError, KeyError, TypeError):
        raise ImportError("invalid_service_connection") from None


def create(store, selected, *, name, intent="", start=False, start_policy="manual"):
    value = ServiceSourceV1.model_validate(selected).model_dump()
    now = time.time()
    draft = {"schema_version": IMPORT_SCHEMA_VERSION, "draft_id": str(uuid.uuid4()), "revision": 1,
        "name": name, "intent": intent, "document_role": None, "start": start, "start_policy": start_policy,
        "requirements": {}, "attachment": None, "state": "needs_information", "blockers": [],
        "operation_id": None, "operation_status": None, "result_slot_id": None, "created_at": now,
        "updated_at": now, "expires_at": now + RETENTION_SECONDS, "source": value}
    _choices(draft)
    with store._transaction():
        if store._connection.execute("SELECT count(*) FROM drafts").fetchone()[0] >= MAX_DRAFTS:
            raise ImportError("import_limit_exceeded")
        store._save(draft)
    return draft


def revise(store, draft_id, revision, selected):
    value = ServiceSourceV1.model_validate(selected).model_dump()
    with store._transaction():
        draft = store._get(draft_id, revision); store._mutable(draft)
        if draft["source"]["kind"] != "service_connection":
            raise ImportError("service_source_required")
        previous = draft["attachment"]
        draft.update(source=value, revision=draft["revision"] + 1, attachment=None,
            expires_at=time.time() + RETENTION_SECONDS)
        draft["requirements"] = {key: item for key, item in draft["requirements"].items()
                                 if key not in {"secret_references", "configuration"}}
        store._save(draft)
    if previous is not None:
        from pathlib import Path
        _remove_attachment_tree(Path(previous["path"]))
    return draft


def checked_source(draft):
    try:
        return ServiceSourceV1.model_validate(draft["source"])
    except (ValueError, KeyError, TypeError):
        from .preparation_contracts import PreparationError
        raise PreparationError("service_source_identity_mismatch") from None
