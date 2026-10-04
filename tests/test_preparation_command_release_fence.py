from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_release_artifact import verify_release_artifact
from mcpme.preparation_release_policy import POLICY_PATH
from mcpme.preparation_verifier import VerificationReport
from mcpme.contracts import validate_package
from test_preparation_runner import controlled, foundation, group_reaper, started, finished
from test_preparation_release_artifact import artifact
from test_public_preparation_admission import legacy_admission
from test_preparation_command_owner import owner_at, native

@pytest.mark.parametrize('damage', ['policy', 'seal', 'policy-inode'])
async def test_runner_command_probe_callback_rejects_held_release_mutation(controlled, artifact, monkeypatch, damage):
    runner, imports, recipes, draft, raw, resources, released = controlled
    proof = verify_release_artifact(artifact[1], POLICY_PATH, b'{}')
    runner.public_admission = replace(legacy_admission(recipes, raw), artifact=proof)
    require = runner._require_admission
    def family_at_verification(descriptor):
        result = require(descriptor)
        jobs = runner.store.list()
        if jobs and jobs[0]['state'] == 'verifying':
            return SimpleNamespace(preparation_owner_kind='command-v3')
        return result
    monkeypatch.setattr(runner, '_require_admission', family_at_verification)
    admitted = []
    class ProbeOwner:
        def __init__(self, *args, check, **kwargs):
            self.check = check
        async def close(self): pass
        def confirm_completed(self): return True
    monkeypatch.setattr('mcpme.preparation_command_owner.CommandPreparationOwner', ProbeOwner)
    async def verify(root, descriptor, workspace, *, timeout_seconds, owner):
        # Match the real verifier's full initial check, then change the held
        # artifact before using the exact callback wired by production _work.
        await runner._check_release()
        path = artifact[0]/'Contents/_CodeSignature/CodeResources' if damage == 'seal' else artifact[2]
        data = path.read_bytes() if damage == 'policy-inode' else b'changed synthetic artifact'
        if damage == 'policy-inode': path.unlink()
        path.write_bytes(data)
        try:
            owner.check()
        except PreparationError:
            pass
        else:
            admitted.append(True)
        package = validate_package(root)
        return VerificationReport(package.digest, 1, 1, '2025-11-25', 4096)
    monkeypatch.setattr(runner, '_verify', verify)
    try:
        job = await started(controlled)
        result = await finished(runner, job['job_id'])
        assert result['state'] == 'failed' and result['ownership_settled']
        assert not resources and not imports.inspect(draft['draft_id'])['attachment']
        assert not admitted, 'release mutation admitted by command probe callback'
    finally:
        await runner.close()


@pytest.mark.parametrize('damage', [None, 'policy', 'seal', 'policy-inode'])
async def test_actual_native_boundary_uses_runner_release_fence(native, controlled, artifact, monkeypatch, tmp_path, damage):
    """Actual unsigned helper; synthetic artifact signature and build only.

    The production runner constructs the check. A real command probe owner
    receives that same check and reaches its last synchronous native callback.
    No child is created after revocation; the positive really runs the target.
    """
    runner, imports, recipes, draft, raw, resources, released = controlled
    proof = verify_release_artifact(artifact[1], POLICY_PATH, b'{}')
    runner.public_admission = replace(legacy_admission(recipes, raw), artifact=proof)
    require = runner._require_admission
    def family_at_verification(descriptor):
        result = require(descriptor)
        jobs = runner.store.list()
        if jobs and jobs[0]['state'] == 'verifying':
            return SimpleNamespace(preparation_owner_kind='command-v3')
        return result
    monkeypatch.setattr(runner, '_require_admission', family_at_verification)
    checks, completed_checks = [], []
    class ProbeOwner:
        def __init__(self, *args, check, **kwargs): self.check = check
        async def close(self): pass
        def confirm_completed(self): return True
    monkeypatch.setattr('mcpme.preparation_command_owner.CommandPreparationOwner', ProbeOwner)
    async def verify(root, descriptor, workspace, *, timeout_seconds, owner):
        await runner._check_release()
        target = tmp_path / 'native-probe'
        target.mkdir()
        # owner_at retains the original class imported by its test module.
        actual = owner_at(target, native)
        actual.check = owner.check
        final = actual.executor.before_native_create
        spawned = []
        import mcpme.command_execution_v2 as execution
        original_popen = execution.subprocess.Popen
        def popen(*args, **kwargs):
            spawned.append(True)
            return original_popen(*args, **kwargs)
        monkeypatch.setattr(execution.subprocess, 'Popen', popen)
        def last_boundary(spec):
            checks.append(True)
            if damage is not None:
                path = artifact[0]/'Contents/_CodeSignature/CodeResources' if damage == 'seal' else artifact[2]
                data = path.read_bytes() if damage == 'policy-inode' else b'changed synthetic artifact'
                if damage == 'policy-inode': path.unlink()
                path.write_bytes(data)
            return final(spec)
        actual.executor.before_native_create = last_boundary
        try:
            handle = await actual.session(actual.package)
            if damage is None:
                await actual.invoke(handle, 'run', {'ok': True}, 1)
                assert spawned == [True]
            else:
                with pytest.raises(PreparationError):
                    await actual.invoke(handle, 'run', {'ok': True}, 1)
                assert not spawned
        finally:
            await actual.close()
        assert actual.confirm_completed() and not actual.store.rows
        assert actual.executor.reconcile() == {'pending': 0, 'reserved_units': 0}
        completed_checks.append(True)
        package = validate_package(root)
        return VerificationReport(package.digest, 1, 1, '2025-11-25', 4096)
    monkeypatch.setattr(runner, '_verify', verify)
    try:
        job = await started(controlled)
        result = await finished(runner, job['job_id'])
        assert checks == [True]
        assert completed_checks == [True]
        assert result['state'] == ('succeeded' if damage is None else 'failed')
        assert result['ownership_settled'] and not result['reservation_retained'] and not resources
        assert bool(imports.inspect(draft['draft_id'])['attachment']) is (damage is None)
    finally:
        await runner.close()
