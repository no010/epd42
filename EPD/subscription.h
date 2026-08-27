#ifndef SUBSCRIPTION_H__
#define SUBSCRIPTION_H__

#include <stdint.h>

/* Validity marker stored in Flash. */
#define SUBSCRIPTION_VALID_MARKER  0xA5u

/* Maximum number of subscription items rendered on screen. */
#define SUBSCRIPTION_MAX_ITEMS     3u

/* Single subscription item (e.g. Copilot Coding, Copilot Chat). */
typedef struct
{
    char     plan_name[16];  /* NUL-terminated plan label, e.g. "Copilot Pro" */
    uint32_t quota_total;    /* Total quota (requests, tokens, …)              */
    uint32_t quota_used;     /* Consumed quota                                 */
    uint32_t balance;        /* Balance * 100 (integer cents), 0 if N/A        */
    char     unit[4];        /* Unit string: "req", "tkn", "$", …             */
    uint8_t  valid;          /* 0xA5 = valid entry                             */
} __attribute__((packed)) subscription_item_t;

/* Full subscription dataset persisted in Flash. */
typedef struct
{
    uint32_t            refresh_interval_sec;             /* Refresh period, default 1800 s (30 min) */
    uint8_t             item_count;                       /* Number of valid items (0-3)             */
    subscription_item_t items[SUBSCRIPTION_MAX_ITEMS];
    char                last_update[16];                  /* "MM-DD HH:MM\0" display string          */
    uint8_t             valid_marker;                     /* SUBSCRIPTION_VALID_MARKER when valid    */
    uint8_t             _pad[3];                          /* align to 4 bytes                        */
    uint32_t            checksum;                         /* Simple sum checksum                     */
} __attribute__((packed)) subscription_data_t;

/* Load subscription data from Flash.  Returns 0 on success. */
uint32_t subscription_load(subscription_data_t *data);

/* Save subscription data to Flash.  Returns 0 on success. */
uint32_t subscription_save(const subscription_data_t *data);

/* Compute checksum over all fields except the checksum itself. */
uint32_t subscription_checksum(const subscription_data_t *data);

#endif /* SUBSCRIPTION_H__ */
