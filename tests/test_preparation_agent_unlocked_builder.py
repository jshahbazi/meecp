"""Actual no-execution copies and destination-continuity controls."""
from pathlib import Path

import pytest

from mcpme.preparation_agent_adapter_store import CODE_PATH
from mcpme.preparation_agent_unlocked_builder import copy_unlocked_snapshot
from mcpme.preparation_agent_unlocked_contracts import DEPENDENCY_NAMESPACE
from mcpme.preparation_contracts import PreparationError
from test_preparation_agent_unlocked_store import foundation, synthetic_resolution


def inputs(foundation,tmp_path):
    imports,recipes,draft,store,job,plan,proposal = foundation
    staged,resolving,resolution,directory = synthetic_resolution(foundation,tmp_path)
    completed = store.materialize(staged,resolving,resolution,directory,draft,lambda:None)
    result,path = store.resolve(completed["id"],completed["result_digest"],draft)
    parent = tmp_path/"private-copy"; parent.mkdir(mode=0o700)
    return draft,result,path,parent/"source"


@pytest.mark.parametrize("foundation",[{"unknown.payload":b"\x00\xffsynthetic\n"}],indirect=True)
def test_copy_preserves_original_and_separate_derived_inputs(foundation,tmp_path):
    draft,result,path,destination = inputs(foundation,tmp_path)
    records = copy_unlocked_snapshot(draft,destination,result,path,lambda:None)
    assert {item["path"] for item in records} == {item["path"] for item in draft["source"]["files"]}|{
        CODE_PATH,DEPENDENCY_NAMESPACE+"/pyproject.toml",DEPENDENCY_NAMESPACE+"/uv.lock"}
    assert (destination/"worker.py").read_bytes() == (Path(draft["source"]["snapshot_path"])/"worker.py").read_bytes()
    assert not (Path(draft["source"]["snapshot_path"])/"uv.lock").exists()
    assert (destination/"unknown.payload").read_bytes() == b"\x00\xffsynthetic\n"
    assert (destination/DEPENDENCY_NAMESPACE/"uv.lock").read_bytes() == (path/"uv.lock").read_bytes()
    assert all((destination/item["path"]).stat().st_mode & 0o777 == 0o600 for item in records)


@pytest.mark.parametrize("change",["nested_destination","root_destination","original","derived"])
def test_copy_rejects_post_write_namespace_or_source_substitution(foundation,tmp_path,monkeypatch,change):
    from mcpme import preparation_agent_unlocked_builder as module
    draft,result,path,destination = inputs(foundation,tmp_path)
    original = module._write_file
    def altered(root,name,data):
        original(root,name,data)
        if name != DEPENDENCY_NAMESPACE+"/uv.lock":
            return
        if change == "nested_destination":
            selected = destination/DEPENDENCY_NAMESPACE
            selected.rename(tmp_path/"held-nested"); selected.mkdir(mode=0o700)
        elif change == "root_destination":
            destination.rename(tmp_path/"held-root"); destination.mkdir(mode=0o700)
        else:
            selected = (Path(draft["source"]["snapshot_path"])/"worker.py") if change == "original" else path/CODE_PATH
            selected.write_bytes(selected.read_bytes()+b"# changed\n")
    monkeypatch.setattr(module,"_write_file",altered)
    with pytest.raises(PreparationError):
        copy_unlocked_snapshot(draft,destination,result,path,lambda:None)
