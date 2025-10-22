#include "main.h"

#include <stdint.h>

void hil_init(void)
{
    /* Placeholder for initializing MCU peripherals used by the HIL bench. */
}

void hil_loop(void)
{
    /* Placeholder control loop; extend with real firmware logic. */
}

int main(void)
{
    hil_init();

    while (1)
    {
        hil_loop();
        /* Insert breakpoint or low-power wait here for debugging. */
    }

    return 0;
}
