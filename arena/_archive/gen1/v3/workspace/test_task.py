import sys
from task import add


def test_add():
    assert add(2, 3) == 5, f"add(2,3)={add(2,3)} expected 5"
    assert add(-1, 1) == 0, f"add(-1,1)={add(-1,1)} expected 0"


if __name__ == "__main__":
    try:
        test_add()
    except AssertionError as e:
        print("FAIL:", e)
        sys.exit(1)
    print("OK")
    sys.exit(0)
