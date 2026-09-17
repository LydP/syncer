import pytest

from syncer.storage import ensure_storage_layout


@pytest.fixture
def layout(tmp_path):
    return ensure_storage_layout(tmp_path)


@pytest.fixture
def master_and_replica(tmp_path):
    """The ordinary one-master/one-replica pair, both already created, with
    the replica's landing subfolder for the (dir-type, "master"-named) master
    pre-created too, since check.py namespaces a dir master's content under
    <replica>/<master-basename>/.
    """
    master = tmp_path / "master"
    replica = tmp_path / "replica"
    master.mkdir()
    replica.mkdir()
    (replica / "master").mkdir()
    return master, replica
