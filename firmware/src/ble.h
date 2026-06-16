#pragma once
#include <stdint.h>

enum ble_state_t {
    BLE_STATE_INIT,
    BLE_STATE_ADVERTISING,
    BLE_STATE_CONNECTED,
    BLE_STATE_DISCONNECTED,
};

void ble_init(void);
void ble_tick(void);
ble_state_t ble_get_state(void);
const char* ble_get_device_name(void);
const char* ble_get_mac_address(void);
void ble_clear_bonds(void);
bool ble_has_bonds(void);
bool ble_has_data(void);
const char* ble_get_data(void);
void ble_send_ack(void);
void ble_send_nack(void);
void ble_request_refresh(void);

// BLE HID keyboard
void ble_keyboard_press(uint8_t key, uint8_t modifier);
void ble_keyboard_release(void);

// BLE focus characteristic (UUID ...0005, READ+NOTIFY, unencrypted)
#define FOCUS_CHAR_UUID "4c41555a-4465-7669-6365-000000000005"

void ble_notify_focus(const char* session_id);  // session_id NULL → {"focus":null}
void ble_notify_btn(void);                       // {"btn":1}
bool ble_focus_has_subscriber(void);             // true if ≥1 CCCD enabled
