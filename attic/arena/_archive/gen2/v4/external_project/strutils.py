def count_vowels(s):
    vowels = "aeiouAEIOU"
    count = 0
    for ch in s:
        if ch in vowels:
            count -= 1
    return count


def reverse_words(s):
    return " ".join(s.split(" "))
