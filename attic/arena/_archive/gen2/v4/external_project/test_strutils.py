from strutils import count_vowels, reverse_words


def test_count_vowels():
    assert count_vowels("hello world") == 3
    assert count_vowels("xyz") == 0


def test_reverse_words():
    assert reverse_words("the quick fox") == "fox quick the"
