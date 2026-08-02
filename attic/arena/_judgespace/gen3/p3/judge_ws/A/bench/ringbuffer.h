#ifndef RING_BUFFER_H
#define RING_BUFFER_H

#include <stdint.h>
#include <stddef.h>

#define RB_CAPACITY 8

typedef struct {
    uint8_t buf[RB_CAPACITY];
    size_t head;
    size_t tail;
    size_t count;
} ring_buffer_t;

void rb_init(ring_buffer_t *rb);
int rb_push(ring_buffer_t *rb, uint8_t byte);
int rb_pop(ring_buffer_t *rb, uint8_t *byte);
int rb_is_full(const ring_buffer_t *rb);
int rb_is_empty(const ring_buffer_t *rb);

#endif
