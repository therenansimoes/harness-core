#include <stdio.h>
#include "ringbuffer.h"

static int failures = 0;

#define CHECK(cond, msg) do { \
    if (!(cond)) { \
        printf("FAIL: %s\n", msg); \
        failures++; \
    } else { \
        printf("PASS: %s\n", msg); \
    } \
} while (0)

int main(void) {
    ring_buffer_t rb;
    rb_init(&rb);

    /* Fill the buffer to exactly capacity. */
    for (int i = 0; i < RB_CAPACITY; i++) {
        int rc = rb_push(&rb, (uint8_t)(i + 1));
        CHECK(rc == 0, "push within capacity succeeds");
    }

    CHECK(rb_is_full(&rb), "buffer reports full at capacity");

    /* One more push must be rejected, not silently overwrite unread data. */
    int overflow_rc = rb_push(&rb, 0xAA);
    CHECK(overflow_rc == -1, "push beyond capacity is rejected");

    /* FIFO order and original data must be intact after the rejected push. */
    uint8_t val;
    int all_ok = 1;
    for (int i = 0; i < RB_CAPACITY; i++) {
        int rc = rb_pop(&rb, &val);
        if (rc != 0 || val != (uint8_t)(i + 1)) {
            all_ok = 0;
            printf("  mismatch at index %d: rc=%d val=%d expected=%d\n", i, rc, val, i + 1);
        }
    }
    CHECK(all_ok, "popped bytes match original FIFO order, uncorrupted");

    CHECK(rb_is_empty(&rb), "buffer empty after popping everything");

    if (failures == 0) {
        printf("ALL TESTS PASSED\n");
        return 0;
    } else {
        printf("%d TEST(S) FAILED\n", failures);
        return 1;
    }
}
