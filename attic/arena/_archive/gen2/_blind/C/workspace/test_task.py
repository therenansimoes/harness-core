import sys
from task import add, clamp


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-5, 0, 10) == 0
    assert clamp(15, 0, 10) == 10


TESTS = [test_add, test_clamp]

if __name__ == "__main__":
    results = {}
    for t in TESTS:
        try:
            t()
            results[t.__name__] = True
        except AssertionError:
            results[t.__name__] = False
    passed = sum(1 for v in results.values() if v)
    detail = " ".join(f"{k}={v}" for k, v in results.items())
    print(f"RESULTS {passed}/{len(results)} {detail}")
    sys.exit(0 if passed == len(results) else 1)
