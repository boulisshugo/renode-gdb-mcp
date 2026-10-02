CC = arm-none-eabi-gcc
CFLAGS = -mcpu=cortex-m3 -mthumb -g3 -O0 -ffreestanding -nostdlib -Wall

demo.elf: main.c link.ld
	$(CC) $(CFLAGS) -T link.ld -o $@ main.c

clean:
	rm -f demo.elf
