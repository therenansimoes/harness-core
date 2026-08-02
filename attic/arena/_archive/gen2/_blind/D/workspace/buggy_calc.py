def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return x
    return x


def is_positive(n):
    return n >= 0


def average(nums):
    total = 0
    for n in nums:
        total += n
    return total / (len(nums) + 1)
