import pytest

from storage.base import StorageError
from storage.cold import ColdTier
from storage.disk import DiskTier
from storage.memory import MemoryTier


@pytest.fixture(params=["memory", "disk", "cold"])
def backend(request, tmp_path):
    if request.param == "memory":
        return MemoryTier(latency_ms=0)
    factory = DiskTier if request.param == "disk" else ColdTier
    return factory(tmp_path / request.param, latency_ms=0)


async def test_backend_crud_and_accounting(backend):
    assert await backend.get("missing") is None
    assert not await backend.contains("missing")
    await backend.put("a", b"hello")
    await backend.put("b", b"")
    assert await backend.get("a") == b"hello"
    assert await backend.contains("b")
    assert backend.bytes_used == 5
    assert backend.object_count == 2
    await backend.put("a", b"hi")
    assert backend.bytes_used == 2
    assert backend.object_count == 2
    await backend.delete("a")
    await backend.delete("a")
    assert backend.bytes_used == 0
    assert backend.object_count == 1
    assert await backend.get("a") is None


async def test_disk_keys_cannot_escape_directory(tmp_path):
    tier = DiskTier(tmp_path / "warm", latency_ms=0)
    for key in ("../../outside", "CON", "a/b", "ünicode", "a\\b"):
        await tier.put(key, key.encode())
        assert await tier.get(key) == key.encode()
    assert len(list((tmp_path / "warm").glob("*.blob"))) == 5
    assert not (tmp_path / "outside").exists()
    assert not list((tmp_path / "warm").glob("*.tmp"))


async def test_disk_backend_reindexes_physical_payloads(tmp_path):
    first = DiskTier(tmp_path, latency_ms=0)
    await first.put("a", b"abc")
    reopened = DiskTier(tmp_path, latency_ms=0)
    assert reopened.bytes_used == 3
    assert reopened.object_count == 1
    assert await reopened.get("a") == b"abc"


async def test_atomic_replace_failure_preserves_previous_payload(tmp_path, monkeypatch):
    tier = DiskTier(tmp_path, latency_ms=0)
    await tier.put("a", b"original")

    def fail_replace(_source, _destination):
        raise OSError("injected atomic replacement failure")

    monkeypatch.setattr("storage.disk.os.replace", fail_replace)
    with pytest.raises(StorageError, match="disk write failed"):
        await tier.put("a", b"replacement")
    assert await tier.get("a") == b"original"
    assert tier.bytes_used == len(b"original")
    assert tier.object_count == 1
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("kind,delay", [(MemoryTier, 1), (DiskTier, 15), (ColdTier, 80)])
async def test_simulated_delay(kind, delay, tmp_path, monkeypatch):
    observed = []

    async def fake_sleep(seconds):
        observed.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    tier = kind() if kind is MemoryTier else kind(tmp_path)
    await tier.put("x", b"a")
    await tier.get("x")
    await tier.delete("x")
    assert observed == [delay / 1000] * 3
