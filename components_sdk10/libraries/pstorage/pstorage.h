/**
 * Minimal pstorage stub for S110/SDK10 ble_advertising compatibility.
 * This application uses fstorage (SDK12) for Flash persistence; pstorage
 * is only needed by ble_advertising.c to query pending flash operations.
 * The stub always reports no pending operations.
 */
#ifndef PSTORAGE_H__
#define PSTORAGE_H__

#include <stdint.h>
#include "nrf_error.h"

/**@brief Return the number of pending flash storage operations.
 *
 * Always returns 0 because this application uses fstorage, not pstorage.
 */
static inline uint32_t pstorage_access_status_get(uint32_t * p_count)
{
    *p_count = 0;
    return NRF_SUCCESS;
}

#endif /* PSTORAGE_H__ */
