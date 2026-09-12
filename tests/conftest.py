import pytest


@pytest.fixture
def master_and_replica(tmp_path):
    """The ordinary one-master/one-replica pair, both already created."""
    master = tmp_path / "master"
    replica = tmp_path / "replica"
    master.mkdir()
    replica.mkdir()
    return master, replica
