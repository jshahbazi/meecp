"""Exercise independent recipe races through native forms and a sealed host.

Private inherited pipes select trusted scheduling barriers in the development
runner factory. The lifecycle cases run against the reviewed public family and
retain explicit closed-policy checks as a separate historical control. Actual
policy, routes, native owners, verifier reports and attachment transactions
remain unchanged. Only bounded metadata and hashes leave the isolated workspace.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import select
import shutil
import sqlite3
import stat
import subprocess
import time
import uuid

import verify_phase3_native_onboarding as onboarding
import verify_phase3_public_management as management
import verify_phase3_public_native as public_native
from verify_phase3_primitives import _check_module_origins, _host_imports, _host_paths

CASES = ("disabled_queued", "revoked_verification", "revoked_attachment", "updated_active",
         "canceled_native_owner", "crashed_native_owner", "stale_revision", "stale_source", "capability_blocker", "native_management")


def recipe_manifest_path(root):
    from meecp.recipes import recipe_metadata_names
    names = {path.name: None for path in root.iterdir()}
    manifest_name, _ = recipe_metadata_names(names)
    return root / manifest_name
LIFECYCLE_POLICY_SHA256 = frozenset({
    "c8fa615def941e66e85a68ac0e7cc46dd7caf1797918fc03fdd5277ac2326522",
    "4c5569f10a50a360e5c8e1cffc26cf637f5795d3a2562fc635ca2d9d419e4375",
    "8a9a8fd69217352b49e0e338d4d652df8cebcac86609edae1190a88cd2d9a592",
    "5bb20b7a8cde39368a5cc3d03a92a4ed3f69d92ac56c317b6f696432863925ea",
    "29d0359e9846afff79061c0fd0d0bf265f88fd78deb6c62193af1ba614ca9dcc",
    "ca7d40f1dd0075e757597caea0b5437c87fc63437818e54f815666471bf242b2",
})
BARRIERS = {"disabled_queued": "scheduler_before_snapshot", "stale_source": "scheduler_before_snapshot",
    "revoked_verification": "actual_verifier_return_before_publication",
    "updated_active": "actual_verifier_return_before_publication", "stale_revision": "actual_verifier_return_before_publication",
    "revoked_attachment": "actual_staging_copy_before_admission",
    "canceled_native_owner": "native_launch_acknowledged_before_handle_consumption",
    "crashed_native_owner": "native_launch_acknowledged_before_handle_consumption"}
require = onboarding.require
digest = onboarding.digest
private_json = onboarding.private_json
write_private_json = onboarding.write_private_json


def manager_file_identity(path):
    """Metadata binding for a fresh manager generation; never reads its token."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before, named = os.fstat(descriptor), path.lstat()
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
            and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1
            and 0 < before.st_size <= 262144 and path.resolve() == path,
            "native_manager_file_invalid")
        def identity(value):
            return {"device": value.st_dev, "inode": value.st_ino, "bytes": value.st_size,
                "mode": stat.S_IMODE(value.st_mode), "modified_seconds": value.st_mtime_ns // 1_000_000_000,
                "modified_nanoseconds": value.st_mtime_ns % 1_000_000_000,
                "changed_seconds": value.st_ctime_ns // 1_000_000_000,
                "changed_nanoseconds": value.st_ctime_ns % 1_000_000_000}
        result = identity(before)
        require(result == identity(named) == identity(os.fstat(descriptor)), "native_manager_file_changed")
        return result
    finally:
        os.close(descriptor)


def native_management_fixture(base, state, recipes, forms=()):
    nonce = str(uuid.uuid4())
    marker = state / ("native-recipes-" + nonce + ".json")
    fixture = base / ("native-recipes-case-" + nonce + ".json")
    receipt = base / ("native-recipes-receipt-" + nonce + ".json")
    write_private_json(marker, {"schema_version": 1, "fixture_nonce": nonce,
        "expires_at": int(time.time()) + 120,
        "manager_file": manager_file_identity(state / "manager-connection.json")})
    write_private_json(fixture, {"schema_version": 1, "state_dir": str(state), "output_path": str(receipt),
        "fixture_nonce": nonce, "recipes": recipes, "forms": list(forms)})
    return fixture, receipt


def verify_sealed_host_payload(host):
    """Reject a changed or expanded payload before importing target modules."""
    return onboarding.verify_host_payload(host)


def verify_lifecycle_policy(host):
    """Bind lifecycle coverage to one reviewed policy byte image."""
    policy = host / management.POLICY_PATH
    observed = digest(policy)
    require(observed in LIFECYCLE_POLICY_SHA256, "lifecycle_policy_not_reviewed")
    return management.typed_policy_scope(management.active_policy(host)), observed


async def join_owned(task):
    joined = asyncio.gather(task, return_exceptions=True)
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            continue

# These hooks alter scheduling only. They cannot be selected by source content,
# recipe metadata, a request payload, or the ordinary bundled serve command.
BARRIER_HOST = """
import asyncio,logging,os,sys,threading
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
logging.disable(logging.CRITICAL)
from meecp.supervisor import serve
from meecp.preparation_runner import PreparationRunner,JournaledPreparationOwner
from meecp.preparation_resolution_runner import OwnedResolutionRunner
from meecp.preparation_resolved_python import ResolvedPythonComponent
mode=sys.argv[6]; control=int(sys.argv[7]); status=int(sys.argv[8])
released=threading.Event(); used=False
def listener():
 try:
  if os.read(control,1)==b'G': released.set()
 finally: os.close(control)
threading.Thread(target=listener,daemon=True).start()
def hold(signal=b'H'):
 global used
 if used: return
 used=True
 os.write(status,signal)
 if not released.wait(120): raise TimeoutError()
async def ahold(signal=b'H'): await asyncio.to_thread(hold,signal)
class QueueBarrier(asyncio.Semaphore):
 async def __aenter__(self):
  await ahold()
  return await super().__aenter__()
def preparation(*args,**kwargs):
 kwargs['enabled']=True
 current=ResolvedPythonComponent().reference.key
 selected=tuple(key for key in args[0].families if key==current)
 if len(selected)!=1: raise ValueError('reviewed_python_family_required')
 kwargs['enabled_families']=frozenset(selected)
 runner=PreparationRunner(*args,**kwargs)
 if mode in {'disabled_queued','stale_source'}: runner._serial=QueueBarrier(1)
 if mode in {'revoked_verification','updated_active','stale_revision'}:
  original=runner._verify
  async def verify(*args,**kwargs):
   report=await original(*args,**kwargs)
   await ahold()
   return report
  runner._verify=verify
 if mode=='revoked_attachment':
  original=runner.store.imports.stage_attachment
  def stage(*args,**kwargs):
   result=original(*args,**kwargs)
   hold()
   return result
  runner.store.imports.stage_attachment=stage
 if mode in {'canceled_native_owner','crashed_native_owner'}:
  original=JournaledPreparationOwner.launch
  async def launch(owner,*args,**kwargs):
   handle=await original(owner,*args,**kwargs)
   if owner.store is runner.store:
    live=handle.returncode is None and handle.wrapper.returncode is None
    await ahold(b'H'+(b'L' if live else b'C'))
   return handle
  JournaledPreparationOwner.launch=launch
 return runner
def resolution(*args,**kwargs):
 kwargs['enabled']=True
 return OwnedResolutionRunner(*args,**kwargs)
asyncio.run(serve(Path(sys.argv[3]),Path(sys.argv[4]),int(sys.argv[5]),
 preparation_runner_factory=preparation,resolution_runner_factory=resolution))
"""


class Barrier:
    """Exact inherited descriptor ownership, finite wait, idempotent release."""
    def __init__(self):
        self.control_read, self.control_write = os.pipe()
        self.status_read, self.status_write = os.pipe()
        self.released = False

    def child_started(self):
        for name in ("control_read", "status_write"):
            os.close(getattr(self, name))
            setattr(self, name, None)

    def wait(self, timeout=120):
        ready, _, _ = select.select([self.status_read], [], [], timeout)
        require(ready and os.read(self.status_read, 1) == b"H", "recipe_barrier_not_reached")

    async def await_hit(self, child_task, receipt_path, timeout=120):
        async with asyncio.timeout(timeout):
            while True:
                ready, _, _ = select.select([self.status_read], [], [], 0)
                if ready:
                    require(os.read(self.status_read, 1) == b"H", "recipe_barrier_not_reached")
                    return
                if child_task.done():
                    failure = onboarding.AcceptanceError("native_before_barrier_failed")
                    if receipt_path.is_file():
                        receipt = private_json(receipt_path, 256 * 1024)
                        failure.observation = {"native_stage": receipt.get("stage") if receipt.get("stage")
                            in onboarding.NATIVE_STAGES else "unknown", "native_receipt_sha256": digest(receipt_path),
                            "native_error_code": receipt.get("error_code") if isinstance(receipt.get("error_code"), str)
                            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", receipt["error_code"]) else "unknown"}
                    raise failure
                await asyncio.sleep(0.05)

    def release(self):
        if not self.released and self.control_write is not None:
            try:
                os.write(self.control_write, b"G")
            except BrokenPipeError:
                pass
            self.released = True

    def close(self):
        self.release()
        for name in ("control_read", "control_write", "status_read", "status_write"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)


class ReceiptHold:
    """Withhold and restore the exact owned inode using private directory FDs."""
    def __init__(self, state, job):
        from meecp.ownership_receipts import verify_receipt
        self.verify_receipt = verify_receipt
        connection = sqlite3.connect((state / "imports.sqlite").as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute("SELECT data FROM preparation_ownership WHERE job_id=?", (job["job_id"],)).fetchall()
        finally:
            connection.close()
        require(len(rows) == 1, "crash_ownership_record_mismatch")
        self.metadata = json.loads(rows[0][0])
        self.workspace = Path(job["workspace"])
        require(self.workspace.resolve() == self.workspace and self.workspace.is_relative_to(state),
            "crash_workspace_unowned")
        self.held_workspace = state / "withheld-native-proof"
        self.held_workspace.mkdir(mode=0o700)
        held_directory = self.held_workspace / "ownership-receipts"
        held_directory.mkdir(mode=0o700)
        source_directory = self.workspace / "ownership-receipts"
        directories = {}
        for directory in (self.workspace, self.held_workspace, source_directory, held_directory):
            details = directory.lstat()
            require(directory.resolve() == directory and stat.S_ISDIR(details.st_mode)
                and details.st_uid == os.getuid() and stat.S_IMODE(details.st_mode) == 0o700,
                "crash_receipt_directory_unsafe")
            directories[directory] = details.st_dev, details.st_ino
        self.name = self.metadata["instance_id"] + ".receipt"
        require(re.fullmatch(r"[0-9a-f]{32}\.receipt", self.name), "crash_receipt_identity_invalid")
        self.source_fd = self.held_fd = None
        self.withheld = False
        try:
            self.source_fd = os.open(source_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.held_fd = os.open(held_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for descriptor, directory in ((self.source_fd, source_directory), (self.held_fd, held_directory)):
                opened = os.fstat(descriptor)
                require((opened.st_dev, opened.st_ino) == directories[directory], "crash_receipt_directory_changed")
            details = os.stat(self.name, dir_fd=self.source_fd, follow_symlinks=False)
            require(stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()
                and stat.S_IMODE(details.st_mode) == 0o600 and details.st_nlink == 1 and details.st_size == 105,
                "crash_receipt_file_unsafe")
            self.identity = details.st_dev, details.st_ino
            os.rename(self.name, self.name, src_dir_fd=self.source_fd, dst_dir_fd=self.held_fd)
            self.withheld = True
            os.fsync(self.source_fd)
            os.fsync(self.held_fd)
            self.require_identity(self.held_fd)
        except BaseException:
            self.close()
            raise

    def require_identity(self, descriptor):
        details = os.stat(self.name, dir_fd=descriptor, follow_symlinks=False)
        require((details.st_dev, details.st_ino) == self.identity and stat.S_ISREG(details.st_mode)
            and details.st_uid == os.getuid() and stat.S_IMODE(details.st_mode) == 0o600 and details.st_nlink == 1,
            "crash_receipt_identity_changed")

    async def completed(self):
        async with asyncio.timeout(30):
            while not self.verify_receipt(self.held_workspace, self.metadata):
                await asyncio.sleep(0.02)
        self.require_identity(self.held_fd)
        self.proof_digest = digest(self.held_workspace / "ownership-receipts" / self.name)

    def restore(self):
        if not self.withheld:
            return
        self.require_identity(self.held_fd)
        # link is exclusive: a newly created/replaced original path is never
        # overwritten. Both links refer to the held inode until unlink settles.
        os.link(self.name, self.name, src_dir_fd=self.held_fd, dst_dir_fd=self.source_fd, follow_symlinks=False)
        os.unlink(self.name, dir_fd=self.held_fd)
        os.fsync(self.source_fd)
        os.fsync(self.held_fd)
        self.withheld = False
        self.require_identity(self.source_fd)
        require(self.verify_receipt(self.workspace, self.metadata)
            and digest(self.workspace / "ownership-receipts" / self.name) == self.proof_digest,
            "restored_native_proof_mismatch")

    def close(self):
        for name in ("source_fd", "held_fd"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)


class LifecycleHarness(onboarding.Harness):
    async def active_defaults(self):
        """Use the ordinary public launcher for the reviewed active family."""
        return await public_native.PublicPythonHarness.defaults(self)

    async def start_reviewed(self, state, environment, home):
        gate = Barrier()
        try:
            return await self.start_barrier(state, environment, home, "none", gate)
        finally:
            gate.close()

    async def start_barrier(self, state, environment, home, mode, barrier):
        argv = [str(self.host / "python/bin/python3.12"), "-I", "-S", "-B", "-c", BARRIER_HOST,
            str(self.host / "backend"), str(self.host / "python/site-packages"), str(state),
            str(self.reaper), str(os.getpid()), mode, str(barrier.control_read), str(barrier.status_write)]
        child = subprocess.Popen(argv, cwd=home, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(barrier.control_read, barrier.status_write))
        self.children.add(child)
        barrier.child_started()
        try:
            async with asyncio.timeout(30):
                while child.poll() is None:
                    if (state / "manager-connection.json").is_file():
                        try:
                            await self.cli(state, environment, home, "capabilities", "--json")
                            return child
                        except (onboarding.AcceptanceError, OSError):
                            pass
                    await asyncio.sleep(0.1)
            raise onboarding.AcceptanceError("acceptance_host_exited")
        except BaseException:
            barrier.release()
            await self.shutdown(child, state)
            raise

    async def request(self, state, route, body=None, expected_error=None):
        import httpx
        from meecp_local_transport import private_transport, socket_path
        connection = private_json(state / "manager-connection.json")
        async with httpx.AsyncClient(base_url=connection["endpoint"], trust_env=False, timeout=30,
                follow_redirects=False, transport=private_transport(socket_path(state, "manager")),
                headers={"Authorization": "Bearer " + connection["token"]}) as client:
            response = await client.request("GET" if body is None else "POST", route, json=body)
            require(len(response.content) <= onboarding.MAX_JSON_BYTES, "manager_response_limit")
            result = response.json()
            require(isinstance(result, dict), "manager_response_invalid")
            if expected_error is not None:
                require(response.status_code != 200 and result.get("error", {}).get("code") == expected_error,
                        "recipe_expected_rejection_mismatch")
            else:
                require(response.status_code == 200 and "error" not in result, "manager_request_failed")
            return result

    async def jobs(self, state, draft_id):
        result = await self.request(state, "/v1/preparations")
        return [item for item in result["preparations"] if item["import_id"] == draft_id]

    async def settled_job(self, state, draft_id):
        async with asyncio.timeout(30):
            while True:
                jobs = await self.jobs(state, draft_id)
                if len(jobs) == 1 and jobs[0]["state"] in {"failed", "canceled", "succeeded"}:
                    job = await self.request(state, "/v1/preparations/" + jobs[0]["job_id"])
                    if (job["ownership_settled"] and not job["reservation_retained"]
                            and onboarding.preparation_ownership_count(state, job["job_id"]) == 0
                            and not (state / "preparation-reservations" / (job["job_id"] + ".json")).exists()
                            and (job["workspace"] is None or not Path(job["workspace"]).exists())):
                        return job
                await asyncio.sleep(0.05)

    async def author(self, state, environment, home, base, recipe_id):
        bundle = base / "independent-recipe"
        scaffold = await self.cli(state, environment, home, "recipes", "scaffold", "--output", bundle,
            "--id", recipe_id, "--component", "resolved-python-v1", "--json")
        checked = await self.cli(state, environment, home, "recipes", "check", "--path", bundle, "--json")
        require(scaffold["digest"] == checked["digest"] and checked["valid"], "independent_recipe_check_failed")
        added = await self.manager(state, "/v1/recipes", {"path": str(bundle)})
        require(added["state"] == "proposed", "independent_recipe_not_proposed")
        return bundle, added

    async def native_management_case(self):
        started = time.monotonic()
        base, state, home, environment = self.new_case("native_management")
        child = await self.start(state, environment, home, enabled=False)
        try:
            cases, expected = [], []
            for label, operations in (
                ("deprecate", [("add", "proposed"), ("enable", "enabled"), ("refresh", "enabled"),
                    ("disable", "disabled"), ("enable", "enabled"), ("deprecate", "deprecated"), ("remove", "removed")]),
                ("revoke", [("add", "proposed"), ("enable", "enabled"), ("revoke", "revoked"), ("remove", "removed")])):
                recipe_id = "local.native-management-" + label
                bundle = base / ("recipe-" + label)
                scaffold = await self.cli(state, environment, home, "recipes", "scaffold", "--output", bundle,
                    "--id", recipe_id, "--component", "resolved-python-v1", "--json")
                checked = await self.cli(state, environment, home, "recipes", "check", "--path", bundle, "--json")
                require(scaffold["digest"] == checked["digest"] and checked["valid"] is True,
                    "native_recipe_check_failed")
                identity = {"recipe_id": recipe_id, "version": "1.0.0", "digest": checked["digest"]}
                cases.append({"folder": str(bundle), "recipe": identity,
                    "operations": [{"action": action, "expected_state": status} for action, status in operations]})
                expected.append(identity)
            fixture, receipt_path = native_management_fixture(base, state, cases)
            code, _ = await self.run([str(self.native), "--recipes-native-acceptance", str(fixture)], environment, home, timeout=180)
            require(code == 0 and receipt_path.is_file(), "native_recipe_management_failed")
            receipt = private_json(receipt_path, 262144)
            require(receipt.get("passed") is True and receipt.get("callbacks_joined") is True
                and receipt.get("production_handlers") is True and receipt.get("owner_private_socket") is True
                and receipt.get("requests_sent") == receipt.get("requests_completed")
                and receipt.get("execution_started_by_driver") is False, "native_recipe_management_receipt_invalid")
            before = [await self.manager(state, "/v1/recipes/inspect", {"recipe_id": item["recipe_id"],
                "version": item["version"]}) for item in expected]
            require(all(item["state"] == "removed" and item["digest"] == binding["digest"]
                for item, binding in zip(before, expected)), "native_recipe_management_identity_changed")
            require(not (await self.request(state, "/v1/preparations"))["preparations"]
                and onboarding.preparation_ownership_count(state) == 0, "native_recipe_management_executed")
            await self.shutdown(child, state)
            for item in cases:
                shutil.rmtree(Path(item["folder"]))
            child = await self.start(state, environment, home, enabled=False)
            retained_cases = [{"folder": None, "recipe": item,
                "operations": [{"action": "inspect", "expected_state": "removed"},
                    {"action": "refresh", "expected_state": "removed"}]} for item in expected]
            fixture2, receipt2_path = native_management_fixture(base, state, retained_cases)
            code, _ = await self.run([str(self.native), "--recipes-native-acceptance", str(fixture2)], environment, home, timeout=180)
            require(code == 0 and receipt2_path.is_file(), "native_recipe_management_restart_failed")
            receipt2 = private_json(receipt2_path, 262144)
            require(receipt2.get("passed") is True and receipt2.get("callbacks_joined") is True,
                "native_recipe_management_restart_receipt_invalid")
            after = [await self.manager(state, "/v1/recipes/inspect", {"recipe_id": item["recipe_id"],
                "version": item["version"]}) for item in expected]
            for old, new in zip(before, after):
                require(all(old[key] == new[key] for key in ("recipe_id", "version", "digest", "epoch", "state", "evidence")),
                    "native_recipe_management_restart_changed")
            return {"passed": True, "native_receipt_sha256": digest(receipt_path),
                "restart_native_receipt_sha256": digest(receipt2_path), "recipe_count": len(cases),
                "registry_changes": receipt["registry_changes"], "callbacks_joined": True,
                "authoring_bundles_removed": True, "removed_records_retained_after_restart": True,
                "zero_preparation_jobs": True, "zero_preparation_ownership": True,
                "simulated_dialog_folder_selection": True, "simulated_lifecycle_confirmation": True,
                "production_handlers_and_owner_socket": True, "seconds": round(time.monotonic() - started, 3)}
        except BaseException:
            self.lease["safe_to_remove"] = False
            raise
        finally:
            await self.shutdown(child, state)

    async def lifecycle_case(self, mode):
        self.failure_step = None
        started = time.monotonic()
        base, state, home, environment = self.new_case(mode)
        barrier = Barrier()
        child = await self.start_barrier(state, environment, home, mode, barrier)
        native_task = None
        native_result = None
        cancel_task = None
        held_proof = None
        try:
            source = base / "source"
            source.mkdir(mode=0o700)
            (source / "worker.py").write_text(onboarding.CALLABLE_SOURCE)
            (source / "LICENSE").write_text("Synthetic fixture dedicated to the public domain under CC0.\n")
            source_hashes = {path.name: digest(path) for path in source.iterdir()}
            recipe_id = "local.lifecycle-acceptance"
            bundle, recipe = await self.author(state, environment, home, base, recipe_id)
            recipe = await self.manager(state, "/v1/recipes/enable", {"recipe_id": recipe_id, "version": "1.0.0"})
            draft = await self.cli(state, environment, home, "imports", "create", "--source", source,
                "--name", "Independent lifecycle fixture", "--intent", "Expose the selected synthetic integer tool.",
                "--start", "--start-policy", "host", "--json")
            observed = await self.manager(state, "/v1/preparations/inspect-source",
                {"import_id": draft["draft_id"], "revision": draft["revision"]})
            candidates = [item for item in observed["candidates"] if item["kind"] == "python-callable"]
            require(len(candidates) == 1, "fixture_candidate_ambiguous")
            fixture, receipt_path = base / "native-case.json", base / "native-receipt.json"
            write_private_json(fixture, {"state_dir": str(state), "draft_id": draft["draft_id"],
                "output_path": str(receipt_path), "candidate_id": candidates[0]["candidate_id"],
                "recipe_id": recipe_id, "answers_by_label": {"Test input": {"n": 7},
                    "Expected test result": {"value": 14}, "Tool label": "Independent lifecycle fixture", "Decimal places": 3}})
            native_task = asyncio.create_task(self.run([str(self.native), "--preparation-native-acceptance", str(fixture)],
                environment, home, timeout=180))
            try:
                await barrier.await_hit(native_task, receipt_path)
            except onboarding.AcceptanceError as failure:
                if failure.code == "native_before_barrier_failed" and receipt_path.is_file():
                    native_receipt = private_json(receipt_path, 256 * 1024)
                    current = await self.request(state, "/v1/imports/" + draft["draft_id"])
                    form = await self.manager(state, "/v1/preparations/form", {
                        "import_id": draft["draft_id"], "revision": current["revision"],
                        "candidate_id": candidates[0]["candidate_id"],
                        "recipe": {"id": recipe_id, "version": "1.0.0", "digest": recipe["digest"]},
                        "resolution_digest": native_receipt.get("resolution_digest")})
                    answers = {field["id"]: field["value"] for field in form["fields"]
                        if field["kind"] != "schema-preview" and field["value"] is not None}
                    review = await self.manager(state, "/v1/preparations/plan-form", {
                        "import_id": draft["draft_id"], "revision": current["revision"],
                        "candidate_id": form["candidate_id"], "recipe": form["recipe"],
                        "form_digest": form["form_digest"], "answers": answers,
                        "resolution_digest": form["resolution_digest"]})
                    failure.observation.update(review_available=review["execution_available"],
                        review_blockers=review["blockers"], review_plan_present=review["plan_id"] is not None,
                        review_revision=review["revision"], form_revision=form["revision"],
                        review_memory_mb=review["resources"]["memory_mb"],
                        review_tools=len(review["tools"]), review_resolution_digest_present="resolution_digest" in review,
                        review_policy_schema=review["execution_policy"]["schema_version"])
                raise
            jobs = await self.jobs(state, draft["draft_id"])
            require(len(jobs) == 1, "recipe_barrier_job_missing")
            job = await self.request(state, "/v1/preparations/" + jobs[0]["job_id"])
            before = {"state": job["state"], "commands_finished": job["progress"].get("commands_finished", 0),
                "fixtures_checked": job["progress"].get("fixtures_checked", 0),
                "ownership_receipts": onboarding.preparation_ownership_count(state, job["job_id"]),
                "workspace_present": job.get("workspace") is not None}
            changed = None
            crash_evidence = None
            if mode == "disabled_queued":
                require(job["state"] == "queued" and before["commands_finished"] == 0
                    and before["ownership_receipts"] == 0 and not before["workspace_present"], "queued_start_executed")
                changed = await self.manager(state, "/v1/recipes/disable", {"recipe_id": recipe_id, "version": "1.0.0"})
            elif mode in {"revoked_verification", "revoked_attachment"}:
                require(job["state"] == ("attaching" if mode == "revoked_attachment" else "verifying"),
                    "recipe_barrier_wrong_state")
                changed = await self.manager(state, "/v1/recipes/revoke", {"recipe_id": recipe_id, "version": "1.0.0"})
            elif mode == "updated_active":
                require(job["state"] == "verifying", "recipe_barrier_wrong_state")
                update = base / "recipe-update"
                shutil.copytree(bundle, update)
                manifest_path = recipe_manifest_path(update)
                manifest = json.loads(manifest_path.read_bytes())
                manifest["version"] = "1.0.1"
                manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
                await self.cli(state, environment, home, "recipes", "lock", "--path", update, "--json")
                changed = await self.manager(state, "/v1/recipes", {"path": str(update)})
                changed = await self.manager(state, "/v1/recipes/enable", {"recipe_id": recipe_id, "version": "1.0.1"})
                await self.manager(state, "/v1/recipes/disable", {"recipe_id": recipe_id, "version": "1.0.0"})
            elif mode == "canceled_native_owner":
                before["held_native_process_live"] = os.read(barrier.status_read, 1) == b"L"
                require(job["state"] == "preparing" and before["ownership_receipts"] > 0
                    and before["held_native_process_live"],
                    "native_owner_not_launched")
                # Cancel waits for the held native owner; release concurrently.
                cancel_task = asyncio.create_task(self.manager(state, "/v1/preparations/" + job["job_id"] + "/cancel", {}))
                async with asyncio.timeout(10):
                    while True:
                        observed = await self.request(state, "/v1/preparations/" + job["job_id"])
                        if observed["state"] in {"cancel_requested", "canceled"}:
                            break
                        await asyncio.sleep(0.01)
                barrier.release()
                await cancel_task
                cancel_task = None
            elif mode == "crashed_native_owner":
                self.failure_step = "crash_before_withholding"
                before["held_native_process_live"] = os.read(barrier.status_read, 1) == b"L"
                require(job["state"] == "preparing" and before["ownership_receipts"] == 1
                    and before["held_native_process_live"],
                    "crash_native_owner_not_launched")
                held_proof = ReceiptHold(state, job)
                self.failure_step = "crash_receipt_withheld"
                child.kill()  # Exact held Popen, never a discovered process ID.
                await self.stop(child)
                self.failure_step = "crash_host_joined"
                native_clients = [owned for owned in self.children if owned.args[0] == str(self.native)]
                require(len(native_clients) == 1, "crash_native_client_identity_missing")
                await self.stop(native_clients[0])
                native_result = await native_task
                native_task = None
                self.failure_step = "crash_native_client_joined"
                await held_proof.completed()
                self.failure_step = "crash_actual_proof_completed"
                child = await self.start_reviewed(state, environment, home)
                self.failure_step = "crash_restarted_missing_proof"
                pending = await self.request(state, "/v1/preparations/" + job["job_id"])
                require(pending["state"] == "interrupted" and not pending["ownership_settled"]
                    and pending["reservation_retained"] and Path(pending["workspace"]).is_dir()
                    and onboarding.preparation_ownership_count(state, job["job_id"]) == 1
                    and len(list((state / "preparation-reservations").glob("*.json"))) == 1,
                    "missing_native_proof_released_authority")
                await self.request(state, "/v1/preparations/start", {"plan_id": job["authority_plan_id"],
                    "plan_digest": job["authority_plan_digest"], "idempotency_key": str(uuid.uuid4())},
                    expected_error="preparation_plan_consumed")
                self.failure_step = "crash_replay_denied"
                current = await self.request(state, "/v1/imports/" + draft["draft_id"])
                resolutions = await self.request(state, "/v1/resolutions")
                resolved = [item for item in resolutions["resolutions"] if item["import_id"] == draft["draft_id"]
                    and item["state"] == "succeeded"]
                require(len(resolved) == 1, "crash_resolution_binding_missing")
                form = await self.manager(state, "/v1/preparations/form", {
                    "import_id": draft["draft_id"], "revision": current["revision"],
                    "candidate_id": candidates[0]["candidate_id"], "recipe": job["binding"]["recipe"],
                    "resolution_digest": resolved[0]["result_digest"]})
                answers = {field["id"]: field["value"] for field in form["fields"]
                    if field["kind"] != "schema-preview" and field["value"] is not None}
                replacement = await self.manager(state, "/v1/preparations/plan-form", {
                    "import_id": draft["draft_id"], "revision": current["revision"],
                    "candidate_id": form["candidate_id"], "recipe": form["recipe"],
                    "form_digest": form["form_digest"], "answers": answers,
                    "resolution_digest": form["resolution_digest"]})
                require(replacement["execution_available"], "replacement_review_unavailable")
                self.failure_step = "crash_replacement_reviewed"
                await self.request(state, "/v1/preparations/start", {"plan_id": replacement["plan_id"],
                    "plan_digest": replacement["plan_digest"], "idempotency_key": str(uuid.uuid4())},
                    expected_error="preparation_in_progress")
                self.failure_step = "crash_replacement_denied"
                require(len(await self.jobs(state, draft["draft_id"])) == 1, "missing_proof_replayed_work")
                held_proof.restore()
                self.failure_step = "crash_actual_proof_restored"
                await self.manager(state, "/v1/preparations/" + job["job_id"] + "/recover", {})
                crash_evidence = {"missing_proof_state": pending["state"], "missing_proof_reservation_retained": True,
                    "missing_proof_workspace_retained": True, "missing_proof_ownership_receipts": 1,
                    "exact_native_proof_sha256": held_proof.proof_digest, "exact_receipt_inode_restored": True,
                    "replay_denied": True, "fresh_replacement_start_denied": True}
            elif mode == "stale_revision":
                current = await self.request(state, "/v1/imports/" + draft["draft_id"])
                await self.manager(state, "/v1/imports/" + draft["draft_id"] + "/revise",
                    {"revision": current["revision"], "name": "Revised lifecycle fixture"})
            elif mode == "stale_source":
                require(job["state"] == "queued" and before["ownership_receipts"] == 0,
                    "source_mutation_barrier_wrong_state")
                target = Path(draft["source"]["snapshot_path"]) / "worker.py"
                require(target.resolve() == target and target.is_relative_to(state)
                    and target.is_file() and not target.is_symlink(), "source_mutation_path_unowned")
                with target.open("a") as stream:
                    stream.write("# Synthetic stale snapshot mutation.\n")
            barrier.release()
            if native_result is None:
                native_result = await native_task
                native_task = None
            code, _ = native_result
            receipt = private_json(receipt_path, 256 * 1024) if receipt_path.is_file() else None
            require(receipt is not None or mode == "crashed_native_owner", "native_receipt_missing")
            if mode == "crashed_native_owner":
                final = await self.request(state, "/v1/preparations/" + job["job_id"])
                require(final["ownership_settled"] and not final["reservation_retained"], "restored_proof_unsettled")
            else:
                final = await self.settled_job(state, draft["draft_id"])
            current = await self.request(state, "/v1/imports/" + draft["draft_id"])
            success = mode == "updated_active"
            expected_error = {"disabled_queued": "recipe_disabled", "revoked_verification": "recipe_revoked",
                "revoked_attachment": "recipe_revoked", "stale_revision": "stale_import_revision",
                "stale_source": "source_identity_mismatch"}.get(mode)
            require(final["state"] == ("succeeded" if success else "canceled" if mode == "canceled_native_owner"
                else "interrupted" if mode == "crashed_native_owner" else "failed"),
                "recipe_terminal_state_mismatch")
            if expected_error:
                require(final.get("error_code") == expected_error, "recipe_terminal_code_mismatch")
            require((code == 0 and receipt is not None and receipt.get("passed") is True) == success,
                "native_lifecycle_outcome_mismatch")
            require((current.get("attachment") is not None) == success, "recipe_attachment_admission_mismatch")
            if mode == "disabled_queued":
                require(final["expected_package_digest"] is None and final["receipt_digest"] is None
                    and final["verification"] is None and final["workspace"] is None
                    and final["progress"].get("commands_finished", 0) == 0,
                    "denied_queue_published_result")
            require(final["binding"]["recipe"]["digest"] == recipe["digest"]
                and final["binding"]["recipe"]["version"] == "1.0.0", "active_recipe_rebound")
            require(onboarding.preparation_ownership_count(state, final["job_id"]) == 0
                and not list((state / "preparation-reservations").glob("*.json")), "recipe_settlement_retained")
            require(final["workspace"] is None or not Path(final["workspace"]).exists(), "recipe_workspace_retained")
            require({path.name: digest(path) for path in source.iterdir()} == source_hashes, "original_source_changed")
            # Native reports are bounded and contain metadata only. Persisted
            # job/recipe evidence must survive removal of original inputs.
            receipt_hash = digest(receipt_path) if receipt else None
            expected_recipe = await self.manager(state, "/v1/recipes/inspect", {"recipe_id": recipe_id, "version": "1.0.0"})
            shutil.rmtree(source)
            shutil.rmtree(bundle)
            if mode == "updated_active":
                shutil.rmtree(base / "recipe-update")
            await self.shutdown(child, state)
            child = await self.start_reviewed(state, environment, home)
            retained = await self.request(state, "/v1/preparations/" + final["job_id"])
            restarted_draft = await self.request(state, "/v1/imports/" + draft["draft_id"])
            require(restarted_draft["revision"] == current["revision"]
                and restarted_draft["source"]["revision"] == current["source"]["revision"]
                and restarted_draft.get("attachment") == current.get("attachment"), "recipe_restart_attachment_changed")
            inspected = await self.manager(state, "/v1/recipes/inspect", {"recipe_id": recipe_id, "version": "1.0.0"})
            require(retained["binding"] == final["binding"] and retained["state"] == final["state"]
                and retained["error_code"] == final["error_code"] and retained["ownership_settled"]
                and retained["verification"] == final["verification"]
                and retained["expected_package_digest"] == final["expected_package_digest"]
                and retained["receipt_digest"] == final["receipt_digest"] and retained["progress"] == final["progress"]
                and not retained["reservation_retained"], "recipe_restart_job_changed")
            require(inspected["digest"] == expected_recipe["digest"] and inspected["evidence"] == expected_recipe["evidence"]
                and inspected["state"] == expected_recipe["state"] and inspected["epoch"] == expected_recipe["epoch"]
                and (receipt is None or digest(receipt_path) == receipt_hash), "recipe_restart_evidence_changed")
            update_evidence = None
            if mode == "updated_active":
                updated = await self.manager(state, "/v1/recipes/inspect", {"recipe_id": recipe_id, "version": "1.0.1"})
                require(updated["digest"] == changed["digest"] and updated["evidence"] == changed["evidence"]
                    and updated["state"] == "enabled" and updated["epoch"] == changed["epoch"], "updated_recipe_restart_changed")
                update_evidence = {"version": updated["version"], "digest": updated["digest"], "epoch": updated["epoch"]}
            result = {"passed": True, "barrier": BARRIERS[mode], "barrier_observation": before,
                "native_stage": receipt["stage"] if receipt else "interrupted_at_native_launch",
                "native_receipt_sha256": receipt_hash, "native_client_exit_code": code,
                "native_receipt_bytes": receipt_path.stat().st_size if receipt else 0, "job_state": retained["state"],
                "error_code": retained["error_code"], "recipe_digest": recipe["digest"],
                "recipe_version": retained["binding"]["recipe"]["version"], "recipe_state_after_restart": inspected["state"],
                "recipe_epoch_after_restart": inspected["epoch"], "ownership_receipts_after_restart": 0,
                "reservation_files_after_restart": 0, "attachment_present": success,
                "verification_published": retained["verification"] is not None,
                "package_digest_published": retained["expected_package_digest"] is not None,
                "workspace_removed": True,
                "import_attachment_revision_retained_after_restart": True,
                "receipt_retained_after_restart": receipt is not None, "job_evidence_retained_after_restart": True,
                "original_inputs_removed": True,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
            if update_evidence:
                result["updated_recipe"] = update_evidence
            if crash_evidence:
                result["crash_recovery"] = crash_evidence
            return result
        finally:
            barrier.release()
            if held_proof is not None:
                if held_proof.withheld:
                    if hasattr(held_proof, "proof_digest") and held_proof.verify_receipt(
                            held_proof.held_workspace, held_proof.metadata):
                        held_proof.restore()
                        if child.poll() is None:
                            await self.manager(state, "/v1/preparations/" + job["job_id"] + "/recover", {})
                    else:
                        self.lease["safe_to_remove"] = False
                held_proof.close()
            if cancel_task is not None:
                await join_owned(cancel_task)
            if native_task is not None:
                await asyncio.gather(native_task, return_exceptions=True)
            await self.shutdown(child, state)
            barrier.close()

    async def capability_case(self):
        base, state, home, environment = self.new_case("capability_blocker")
        child = await self.start(state, environment, home, enabled=True)
        try:
            bundle = base / "unavailable-recipe"
            await self.cli(state, environment, home, "recipes", "scaffold", "--output", bundle,
                "--id", "local.unavailable-acceptance", "--component", "resolved-python-v1", "--json")
            manifest_path = recipe_manifest_path(bundle)
            manifest = json.loads(manifest_path.read_bytes())
            manifest["requirements"]["capabilities"].append("unavailable-acceptance-capability")
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
            await self.cli(state, environment, home, "recipes", "lock", "--path", bundle, "--json")
            added = await self.manager(state, "/v1/recipes", {"path": str(bundle)})
            require(added["state"] == "proposed" and added["blockers"] == ["recipe_capability_unavailable"],
                "capability_blocker_missing")
            await self.request(state, "/v1/recipes/enable", {"recipe_id": added["recipe_id"], "version": added["version"]},
                expected_error="recipe_capability_unavailable")
            require((await self.request(state, "/v1/preparations"))["preparations"] == []
                and onboarding.preparation_ownership_count(state) == 0, "blocked_recipe_created_job")
            await self.shutdown(child, state)
            child = await self.start(state, environment, home, enabled=True)
            retained = await self.manager(state, "/v1/recipes/inspect",
                {"recipe_id": added["recipe_id"], "version": added["version"]})
            require(retained["digest"] == added["digest"] and retained["state"] == "proposed"
                and retained["blockers"] == added["blockers"], "capability_restart_changed")
            return {"passed": True, "recipe_digest": added["digest"], "blocker": "recipe_capability_unavailable",
                "preparation_jobs": 0, "ownership_receipts": 0, "retained_after_restart": True}
        finally:
            await self.shutdown(child, state)


async def verify(host_path, native_path, report, cases=CASES):
    verify_sealed_host_payload(host_path)
    with onboarding.isolated_workspace(report) as (root, lease):
        host, native = onboarding.relocate_host_and_native(root, host_path, native_path, report)
        build = verify_sealed_host_payload(host)
        artifacts = build["artifact_sha256"]
        _host_paths(host)
        _host_imports(host)
        report.update(host_source_commit=build["source_commit"], host_build_sha256=digest(host / "build.json"),
            host_artifacts_checked=len(artifacts), native_binary_sha256=digest(native), relocated_host=True,
            minimal_environment=True, development_execution_only=True)
        report.update(harness_sha256=digest(Path(__file__).resolve()),
            onboarding_driver_sha256=digest(Path(onboarding.__file__).resolve()),
            barrier_factory_sha256=hashlib.sha256(BARRIER_HOST.encode()).hexdigest())
        harness = LifecycleHarness(root, host, native, lease)
        try:
            _check_module_origins(host)
            report["typed_policy_scope"], report["policy_sha256"] = verify_lifecycle_policy(host)
            report["active_policy_defaults"] = await harness.active_defaults()
            for case in cases:
                report["active_case"] = case
                if case == "capability_blocker":
                    report["cases"][case] = await harness.capability_case()
                elif case == "native_management":
                    report["cases"][case] = await harness.native_management_case()
                else:
                    report["cases"][case] = await harness.lifecycle_case(case)
            _check_module_origins(host)
        except BaseException:
            if getattr(harness, "failure_step", None) is not None:
                report["failure_step"] = harness.failure_step
            raise
        finally:
            try:
                await harness.cleanup()
            except BaseException:
                lease["safe_to_remove"] = False
                raise
            finally:
                report["owned_children_joined"] = not harness.children
        report.pop("active_case", None)
        if report.get("native_bundle_preserved"):
            onboarding.verify_native_app_signature(native)
        report["verified"] = report["owned_children_joined"] and all(item["passed"] for item in report["cases"].values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=CASES, action="append")
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "independent_recipe_lifecycle", "cases": {},
        "limits": ["development_barriers_only", "other_host_helper_crash_boundaries_unverified",
            "mismatched_native_receipt_unverified",
            "source_copy_mutation_races_unverified", "hard_aggregate_process_memory_unverified",
            "signed_final_bundle_unverified", "closed_historical_defaults_unverified",
            "private_barriers_and_factory_scope_unverified"]}
    try:
        asyncio.run(verify(args.host.resolve(strict=True), args.native.resolve(strict=True), report, args.case or CASES))
    except (Exception, KeyboardInterrupt) as error:
        error, nested_types = onboarding.bounded_failure(error)
        report["diagnostic"] = {"code": error.code if isinstance(error, onboarding.AcceptanceError) else "acceptance_failed",
            "type": type(error).__name__ if type(error).__name__ in {"AcceptanceError", "ValueError", "KeyError",
                "TypeError", "OSError", "FileNotFoundError", "TimeoutError", "KeyboardInterrupt"} else "Exception"}
        if nested_types:
            report["diagnostic"]["nested_types"] = nested_types
        if isinstance(error, onboarding.AcceptanceError) and hasattr(error, "observation"):
            report["native_failure"] = error.observation
    if args.output:
        try:
            write_private_json(args.output.absolute(), report)
        except Exception:
            report["verified"] = False
            report["report_write_error"] = "acceptance_report_write_failed"
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
