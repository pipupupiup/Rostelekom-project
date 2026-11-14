from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from ado.data.errors import BusinessValidationError, InputFormatError
from ado.routing.cache import OsrmMatrixCache
from ado.routing.matrix import MatrixTravelProvider, TravelMatrix, UnknownPointError
from ado.routing.osrm import OsrmConfig, OsrmError, OsrmTableClient

# ------------------------------------------------------------------ матрица организаторов


def test_spec_matrix_example_asymmetric(spec: dict) -> None:
    m = TravelMatrix.from_api(spec["components"]["examples"]["TravelMatrixExample"]["value"])
    p = MatrixTravelProvider(m)
    assert p.travel_sec("SC-01", "B-00412") == 1080
    assert p.travel_sec("B-00412", "SC-01") == 1110  # A→B ≠ B→A
    assert p.distance_m("B-00415", "B-00412") == 820
    assert p.travel_sec("B-00412", "B-00412") == 0
    assert m.hourly_factors == {8: 1.35, 13: 1.1}
    assert m.diagnostics() == []


def test_matrix_unknown_point_and_coverage(spec: dict) -> None:
    m = TravelMatrix.from_api(spec["components"]["examples"]["TravelMatrixExample"]["value"])
    with pytest.raises(UnknownPointError):
        m.travel_sec("SC-01", "B-99999")
    with pytest.raises(BusinessValidationError, match="нет 1 точек"):
        m.check_covers(["SC-01", "B-99999"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("durations_sec"),
        lambda r: r["durations_sec"].pop(),
        lambda r: r["durations_sec"][0].append(1),
        lambda r: r["distances_m"][1].__setitem__(0, -5),
        lambda r: r["durations_sec"][2].__setitem__(1, 1.5),
        lambda r: r["points"].__setitem__(2, "SC-01"),
    ],
)
def test_matrix_format_errors(spec: dict, mutate) -> None:
    raw = json.loads(json.dumps(spec["components"]["examples"]["TravelMatrixExample"]["value"]))
    mutate(raw)
    with pytest.raises(InputFormatError):
        TravelMatrix.from_api(raw)


# ------------------------------------------------------------------ mock OSRM


class FakeOsrm:
    """Mock OSRM Table API. Время асимметрично: «в гору» (рост широты) на 37,4 с дольше."""

    def __init__(self, unreachable: set[tuple[float, float]] | None = None, fail: int | None = None) -> None:
        self.calls: list[str] = []
        self.unreachable = {(round(a, 6), round(b, 6)) for a, b in (unreachable or set())}
        self.fail = fail
        self.max_coords_seen = 0

    @staticmethod
    def duration(a: tuple[float, float], b: tuple[float, float]) -> float:
        if a == b:
            return 0.0
        base = abs(a[0] - b[0]) * 100_000 + abs(a[1] - b[1]) * 60_000
        return base + (37.4 if b[0] > a[0] else 0.0)

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        if self.fail is not None:
            return self.fail, b'{"code":"InvalidQuery"}'
        u = urlsplit(url)
        coords_part = u.path.split("/")[-1]
        coords = [(float(c.split(",")[1]), float(c.split(",")[0])) for c in coords_part.split(";")]
        self.max_coords_seen = max(self.max_coords_seen, len(coords))
        q = parse_qs(u.query)
        assert q["annotations"] == ["duration,distance"]
        src = [int(x) for x in q["sources"][0].split(";")]
        dst = [int(x) for x in q["destinations"][0].split(";")]
        durs, dists = [], []
        for i in src:
            drow, mrow = [], []
            for j in dst:
                a, b = coords[i], coords[j]
                if (round(a[0], 6), round(a[1], 6)) in self.unreachable or (
                    round(b[0], 6),
                    round(b[1], 6),
                ) in self.unreachable:
                    drow.append(None)
                    mrow.append(None)
                else:
                    d = self.duration(a, b)
                    drow.append(d)
                    mrow.append(d * 11.1)
            durs.append(drow)
            dists.append(mrow)
        return 200, json.dumps({"code": "Ok", "durations": durs, "distances": dists}).encode()


def pts(n: int, offset: int = 0) -> dict[str, tuple[float, float]]:
    return {
        f"B-{i + offset:05d}": (56.80 + 0.003 * (i + offset), 60.55 + 0.002 * ((i + offset) % 7))
        for i in range(n)
    }


def test_osrm_units_rounding_and_direction() -> None:
    fake = FakeOsrm()
    client = OsrmTableClient(OsrmConfig(base_url="http://x"), transport=fake)
    a, b = (56.80, 60.60), (56.81, 60.60)
    block = client.table([a, b], [a, b])
    # 0.01° широты × 100000 = 1000 с; «в гору» +37.4 → 1037 (half-up округление до секунд)
    assert block.durations == [[0, 1037], [1000, 0]]
    assert block.distances[0][1] == round(1037.4 * 11.1)  # метры, не секунды


def test_osrm_http_error_and_bad_code() -> None:
    with pytest.raises(OsrmError, match="HTTP 400"):
        OsrmTableClient(transport=FakeOsrm(fail=400)).table([(56.8, 60.6)], [(56.9, 60.6)])

    def bad(url: str, timeout: float):
        return 200, b'{"code":"NoTable","message":"x"}'

    with pytest.raises(OsrmError, match="NoTable"):
        OsrmTableClient(transport=bad).table([(56.8, 60.6)], [(56.9, 60.6)])


def test_osrm_timeout_retries_then_fails() -> None:
    calls = {"n": 0}

    def timeout(url: str, t: float):
        calls["n"] += 1
        raise TimeoutError("timed out")

    client = OsrmTableClient(OsrmConfig(retries=2, timeout_sec=0.5), transport=timeout)
    with pytest.raises(OsrmError, match="недоступен"):
        client.table([(56.8, 60.6)], [(56.9, 60.6)])
    assert calls["n"] == 3


def test_batched_matrix_respects_table_size() -> None:
    fake = FakeOsrm()
    client = OsrmTableClient(OsrmConfig(max_table_size=10), transport=fake)
    coords = list(pts(23).values())
    block = client.matrix(coords)
    assert fake.max_coords_seen <= 10
    assert len(fake.calls) == 5 * 5  # ceil(23/5)² блоков
    for i in (0, 7, 22):
        for j in (0, 13, 22):
            assert block.durations[i][j] == int(FakeOsrm.duration(coords[i], coords[j]) + 0.5)


def test_cache_dedup_reuse_and_incremental() -> None:
    fake = FakeOsrm()
    cache = OsrmMatrixCache(OsrmTableClient(OsrmConfig(max_table_size=100), transport=fake))
    base = pts(10)
    base["SC-01"] = base["B-00003"]  # совпадающие координаты → одна строка
    assert cache.add_points(base) == 10
    first_calls = len(fake.calls)
    assert cache.travel_sec("SC-01", "B-00003") == 0
    assert cache.travel_sec("SC-01", "B-00007") == cache.travel_sec("B-00003", "B-00007")

    assert cache.add_points(base) == 0  # повторно — без запросов
    assert len(fake.calls) == first_calls

    assert cache.add_points(pts(2, offset=50)) == 2  # только недостающие элементы
    assert len(fake.calls) == first_calls + 2  # новые×все, старые×новые
    new, old = "B-00050", "B-00004"
    assert cache.travel_sec(new, old) == int(FakeOsrm.duration(pts(1, 50)[new], base[old]) + 0.5)
    assert cache.travel_sec(old, new) != cache.travel_sec(new, old)


def test_cache_lookups_never_call_network() -> None:
    fake = FakeOsrm()
    cache = OsrmMatrixCache(OsrmTableClient(transport=fake))
    cache.add_points(pts(15))
    n = len(fake.calls)
    ids = cache.point_ids
    total = 0
    for _ in range(3):  # имитация перебора маршрутов
        for a in ids:
            for b in ids:
                total += cache.travel_sec(a, b) or 0
    assert len(fake.calls) == n and total > 0
    with pytest.raises(UnknownPointError):
        cache.travel_sec("B-00000", "B-77777")


def test_unreachable_pair_is_explicit() -> None:
    p = pts(3)
    fake = FakeOsrm(unreachable={p["B-00002"]})
    cache = OsrmMatrixCache(OsrmTableClient(transport=fake))
    cache.add_points(p)
    assert cache.travel_sec("B-00000", "B-00002") is None
    assert cache.distance_m("B-00002", "B-00001") is None
    snap = cache.snapshot()
    assert snap.travel_sec("B-00000", "B-00002") is None
    assert any("недостижимых" in i.message for i in snap.diagnostics())


def test_cache_consistent_after_osrm_failure() -> None:
    fake = FakeOsrm()
    cache = OsrmMatrixCache(OsrmTableClient(transport=fake))
    cache.add_points(pts(3))
    fake.fail = 500
    with pytest.raises(OsrmError):
        cache.add_points(pts(2, offset=40))
    assert not cache.has_point("B-00040")
    fake.fail = None
    assert cache.add_points(pts(2, offset=40)) == 2
    assert cache.travel_sec("B-00040", "B-00000") is not None


@pytest.mark.skipif(not os.environ.get("ADO_OSRM_URL"), reason="интеграционный тест: задайте ADO_OSRM_URL")
def test_osrm_integration_real_service() -> None:
    client = OsrmTableClient(OsrmConfig(base_url=os.environ["ADO_OSRM_URL"], timeout_sec=10))
    ekb = [(56.8389, 60.6057), (56.8941, 60.6105), (56.7900, 60.6300)]
    block = client.matrix(ekb)
    assert block.durations[0][0] == 0
    assert all(
        v is not None and v > 0 for i, row in enumerate(block.durations) for j, v in enumerate(row) if i != j
    )
