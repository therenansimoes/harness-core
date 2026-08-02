import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from solution import add

def test_add_basic():
    assert add(2, 3) == 5

def test_add_negative():
    assert add(-1, 1) == 0

if __name__ == "__main__":
    test_add_basic()
    test_add_negative()
    print("OK")
