CC = cc
CFLAGS = -Wall -Wextra -std=c11

test: test_ringbuffer
	./test_ringbuffer

test_ringbuffer: test_ringbuffer.c ringbuffer.c ringbuffer.h
	$(CC) $(CFLAGS) -o test_ringbuffer test_ringbuffer.c ringbuffer.c

clean:
	rm -f test_ringbuffer

.PHONY: test clean
