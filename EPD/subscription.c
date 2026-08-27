/*
 * Subscription data model — Flash persistence helpers.
 *
 * The subscription dataset is stored in the second-to-last Flash page
 * (BLE_EPD_CONFIG_ADDR is the last page).  A simple 32-bit sum checksum
 * guards the data integrity.
 */
#include <string.h>
#include "nordic_common.h"
#include "nrf_error.h"
#include "fstorage.h"
#include "subscription.h"
#include "nrf.h"

/* Store subscription data in the second-to-last flash page. */
#define SUBSCRIPTION_DATA_ADDR  (NRF_FICR->CODEPAGESIZE * (NRF_FICR->CODESIZE - 2))

static void sub_fs_evt_handler(fs_evt_t const * const evt, fs_ret_t result)
{
    (void)evt;
    (void)result;
}

FS_REGISTER_CFG(fs_config_t sub_fs_config) =
{
    .callback  = sub_fs_evt_handler,
    .num_pages = 1,
};

uint32_t subscription_checksum(const subscription_data_t *data)
{
    uint32_t sum = 0;
    const uint8_t *p = (const uint8_t *)data;
    /* Sum all bytes except the last 4 (the checksum field itself). */
    uint16_t len = (uint16_t)(sizeof(subscription_data_t) - sizeof(uint32_t));
    for (uint16_t i = 0; i < len; i++)
    {
        sum += p[i];
    }
    return sum;
}

uint32_t subscription_load(subscription_data_t *data)
{
    memcpy(data, (const void *)SUBSCRIPTION_DATA_ADDR, sizeof(subscription_data_t));
    return NRF_SUCCESS;
}

uint32_t subscription_save(const subscription_data_t *data)
{
    /* Align size up to 4 bytes for fstorage word write. */
    uint16_t len = (uint16_t)((sizeof(subscription_data_t) + sizeof(uint32_t) - 1) / sizeof(uint32_t));
    uint32_t err_code = fs_erase(&sub_fs_config, sub_fs_config.p_start_addr, 1, NULL);
    if (err_code != NRF_SUCCESS)
    {
        return err_code;
    }
    return fs_store(&sub_fs_config, sub_fs_config.p_start_addr, (const uint32_t *)data, len, NULL);
}
