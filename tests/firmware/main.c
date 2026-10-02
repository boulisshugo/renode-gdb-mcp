/* Minimal Cortex-M firmware used by the integration tests and the Renode example.
 * Memory map matches demo.repl and QEMU's lm3s6965evb: flash @0x0, RAM @0x20000000. */
#include <stdint.h>

extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;

struct sensor {
    uint32_t id;
    int32_t value;
};

volatile uint32_t counter;
volatile struct sensor sensor = { .id = 7, .value = -3 };

__attribute__((noinline)) int32_t compute(int32_t x)
{
    int32_t y = x * 2;
    return y + 1;
}

__attribute__((noinline)) void tick(void)
{
    counter++;
    sensor.value = compute((int32_t)counter);
}

int main(void)
{
    for (;;) {
        tick();
    }
}

void Reset_Handler(void)
{
    uint32_t *src = &_sidata, *dst = &_sdata;
    while (dst < &_edata) *dst++ = *src++;
    for (dst = &_sbss; dst < &_ebss;) *dst++ = 0;
    main();
}

void Default_Handler(void) { for (;;) {} }

__attribute__((section(".isr_vector"), used))
const void *vectors[16] = {
    &_estack, Reset_Handler, Default_Handler, Default_Handler,
    Default_Handler, Default_Handler, Default_Handler, 0, 0, 0, 0,
    Default_Handler, Default_Handler, 0, Default_Handler, Default_Handler,
};
