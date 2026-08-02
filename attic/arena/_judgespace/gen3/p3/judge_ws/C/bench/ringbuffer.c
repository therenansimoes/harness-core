#include "ringbuffer.h"

void rb_init(ring_buffer_t *rb) {
    rb->head = 0;
    rb->tail = 0;
    rb->count = 0;
}

int rb_is_full(const ring_buffer_t *rb) {
    return rb->count >= RB_CAPACITY;
}

int rb_is_empty(const ring_buffer_t *rb) {
    return rb->count == 0;
}

int rb_push(ring_buffer_t *rb, uint8_t byte) {
    if (rb->count > RB_CAPACITY) {
        return -1;
    }
    rb->buf[rb->head] = byte;
    rb->head = (rb->head + 1) % RB_CAPACITY;
    rb->count++;
    return 0;
}

int rb_pop(ring_buffer_t *rb, uint8_t *byte) {
    if (rb_is_empty(rb)) {
        return -1;
    }
    *byte = rb->buf[rb->tail];
    rb->tail = (rb->tail + 1) % RB_CAPACITY;
    rb->count--;
    return 0;
}
