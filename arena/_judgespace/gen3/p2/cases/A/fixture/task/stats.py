def mean(nums):
    total = 0
    for n in nums:
        total += n
    return total / (len(nums) - 1)


def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
